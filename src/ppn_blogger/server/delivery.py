"""Sending an issue, and remembering what happened.

The rule this module exists to keep: **a delivery failure must never destroy or
sink a generated issue.** The issue and its rendered body are committed long
before anything is sent (see ``newsletter_runs``), every send is wrapped so it
cannot raise, and a channel that is having a bad day leaves a row saying so with
a Retry button next to it. Losing an issue that cost model calls to a transient
outage is the failure mode being designed out — the same reasoning as
``build_cover`` and the WordPress push.

Ordering matters and is deliberate: every ``deliveries`` row is written as
``pending`` **before** the first send. Intent durable before side effect, the
same discipline as ``reviews.decide``. A process that dies halfway leaves rows
showing exactly how far it got, rather than a silence that could mean anything.

Retries only ever apply to failures that might resolve themselves. A bad address,
an unapproved template, a blocked bot — the channel marks those ``permanent`` and
they go straight to failed, because retrying them three times just delays the
moment someone notices.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, true

from ..settings import get_settings
from .channels import CHANNELS, IssuePayload, RecipientRef, channel
from .db import Delivery, NewsletterIssue, Recipient, as_utc, session, utcnow

logger = logging.getLogger("ppn.server.delivery")

PENDING, SENT, FAILED, SKIPPED = "pending", "sent", "failed", "skipped"

# Attempt 1 waits 2 minutes, then 10, then an hour. Long enough that a provider
# outage has a chance to end, short enough that a morning issue still lands in
# the morning.
BACKOFF_MINUTES = (2, 10, 60)


async def deliver_issue(issue_id: int, *, only_pending: bool = False) -> dict[str, Any]:
    """Send one issue to everyone it is for.

    ``only_pending`` is the retry path: it picks up rows that already exist and
    are due, rather than creating a second set.
    """
    from . import newsletters

    settings = get_settings().delivery
    issue = await newsletters.get_issue(issue_id)
    if issue is None:
        raise KeyError(f"No issue {issue_id}")
    if issue["status"] == "skipped":
        raise ValueError("This issue was skipped — there is nothing to send.")

    payload = await _payload(issue_id)

    if not only_pending:
        created = await _materialise(issue_id, int(issue["newsletter_id"]))
        if created == 0:
            raise ValueError(
                "No recipients for this newsletter. Add one, or use the "
                "'copy out by hand' channel."
            )
        await _set_issue_status(issue_id, "sending")

    rows = await _due_rows(issue_id)
    if not rows:
        await _settle(issue_id)
        return await summary(issue_id)

    limit = asyncio.Semaphore(max(1, settings.concurrency))

    async def one(row_id: int) -> None:
        async with limit:
            await _attempt(row_id, payload)

    await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)
    await _settle(issue_id)

    result = await summary(issue_id)
    logger.info(
        "issue %d: %d sent, %d failed, %d still pending",
        issue_id,
        result["sent"],
        result["failed"],
        result["pending"],
    )
    return result


async def _attempt(delivery_id: int, payload: IssuePayload) -> None:
    """One send. Records the outcome whatever it is; never raises."""
    settings = get_settings().delivery
    now = utcnow()

    async with session() as s:
        row = await s.get(Delivery, delivery_id)
        if row is None or row.status != PENDING:
            return
        target = RecipientRef(id=row.recipient_id, channel=row.channel, address="", name="")
        if row.recipient_id is not None:
            recipient = await s.get(Recipient, row.recipient_id)
            if recipient is not None:
                target.address = recipient.address
                target.name = recipient.name
        attempts = row.attempts

    impl = channel(target.channel)
    if impl is None:
        await _record(delivery_id, ok=False, error=f"unknown channel {target.channel!r}", permanent=True)
        return
    if not impl.is_configured:
        # Skipped, not failed: nothing is broken, the channel simply is not set
        # up, and a red row would send someone looking for a fault.
        await _record(delivery_id, ok=False, error=impl.status_detail, skipped=True)
        return

    try:
        result = await impl.send(payload, target)
    except Exception as exc:  # noqa: BLE001 - belt and braces; a channel already promises not to
        logger.exception("channel %s raised", target.channel)
        result = type("R", (), {"ok": False, "provider_message_id": "", "error": f"{type(exc).__name__}: {exc}", "permanent": False})()

    give_up = result.permanent or (attempts + 1) >= settings.max_attempts
    await _record(
        delivery_id,
        ok=result.ok,
        error=result.error,
        provider_message_id=result.provider_message_id,
        permanent=give_up,
        retry_at=None if result.ok or give_up else now + timedelta(minutes=_backoff(attempts)),
    )

    if not result.ok and result.permanent and target.id is not None:
        # A permanently bad address stops being retried on every future issue,
        # without being deleted — the operator decides whether to remove it.
        async with session() as s:
            recipient = await s.get(Recipient, target.id)
            if recipient is not None:
                recipient.failed_at = now
                recipient.last_error = result.error[:400]
                await s.commit()


def _backoff(attempts: int) -> int:
    return BACKOFF_MINUTES[min(attempts, len(BACKOFF_MINUTES) - 1)]


async def _record(
    delivery_id: int,
    *,
    ok: bool,
    error: str = "",
    provider_message_id: str = "",
    permanent: bool = False,
    skipped: bool = False,
    retry_at: Any = None,
) -> None:
    now = utcnow()
    async with session() as s:
        row = await s.get(Delivery, delivery_id)
        if row is None:
            return
        row.attempts += 1
        row.last_attempt_at = now
        if row.first_attempt_at is None:
            row.first_attempt_at = now
        row.error = error[:500]
        if ok:
            row.status = SENT
            row.sent_at = now
            row.provider_message_id = provider_message_id[:200]
            row.next_retry_at = None
        elif skipped:
            row.status = SKIPPED
            row.next_retry_at = None
        elif permanent:
            row.status = FAILED
            row.next_retry_at = None
        else:
            row.status = PENDING
            row.next_retry_at = retry_at
        await s.commit()


async def _materialise(issue_id: int, newsletter_id: int) -> int:
    """Write every delivery row as pending before a single send goes out.

    Broadcast channels (web push, manual) get one row with no recipient — they
    have no per-recipient target, and one row per subscribed browser would be a
    fiction the push service does not support anyway.
    """
    async with session() as s:
        existing = await s.scalar(
            select(func.count()).select_from(Delivery).where(Delivery.issue_id == issue_id)
        )
        if existing:
            return int(existing)

        rows = list(
            (
                await s.execute(
                    select(Recipient).where(
                        Recipient.enabled == true(), Recipient.failed_at.is_(None)
                    )
                )
            ).scalars()
        )

        created = 0
        seen_broadcast: set[str] = set()
        for recipient in rows:
            wanted = recipient.newsletter_ids or []
            if wanted and newsletter_id not in wanted:
                continue
            impl = channel(recipient.channel)
            if impl is not None and impl.broadcast:
                if recipient.channel in seen_broadcast:
                    continue
                seen_broadcast.add(recipient.channel)
                s.add(Delivery(issue_id=issue_id, recipient_id=None, channel=recipient.channel))
            else:
                s.add(
                    Delivery(
                        issue_id=issue_id, recipient_id=recipient.id, channel=recipient.channel
                    )
                )
            created += 1
        await s.commit()
    return created


async def _due_rows(issue_id: int) -> list[int]:
    now = utcnow()
    async with session() as s:
        rows = list(
            (
                await s.execute(
                    select(Delivery).where(
                        Delivery.issue_id == issue_id, Delivery.status == PENDING
                    )
                )
            ).scalars()
        )
    out = []
    for row in rows:
        due = as_utc(row.next_retry_at)
        if due is None or due <= now:
            out.append(row.id)
    return out


async def _settle(issue_id: int) -> None:
    """Move the issue to its final state once no delivery is still in flight.

    `sent` if anything landed, `failed` only if everything failed. Never back to
    `draft`: an issue that has been out, even partially, is not a draft any more.
    """
    async with session() as s:
        rows = list(
            (await s.execute(select(Delivery).where(Delivery.issue_id == issue_id))).scalars()
        )
        issue = await s.get(NewsletterIssue, issue_id)
        if issue is None:
            return
        if any(r.status == PENDING for r in rows):
            issue.status = "sending"
        elif any(r.status == SENT for r in rows):
            issue.status = "sent"
            issue.sent_at = utcnow()
        elif rows and all(r.status == SKIPPED for r in rows):
            # Nothing was configured to send on. The issue is untouched and can
            # be sent later, so it goes back to being reviewable.
            issue.status = "ready"
        elif rows:
            issue.status = "failed"
        await s.commit()


async def _set_issue_status(issue_id: int, status: str) -> None:
    async with session() as s:
        row = await s.get(NewsletterIssue, issue_id)
        if row is not None:
            row.status = status
            await s.commit()


async def _payload(issue_id: int) -> IssuePayload:
    from .. import newsletter_render as render
    from . import newsletters

    issue = await newsletters.get_issue(issue_id)
    assert issue is not None
    composed = {
        "subject": issue["subject"],
        "preheader": issue["preheader"],
        "intro": issue["intro"],
        "sections": _sections_from_items(issue["items"]),
    }
    async with session() as s:
        row = await s.get(NewsletterIssue, issue_id)
    app_url = get_settings().delivery.app_url.rstrip("/")
    return IssuePayload(
        id=issue_id,
        newsletter_name=issue["newsletter_name"],
        subject=issue["subject"],
        preheader=issue["preheader"],
        html=(row.html if row else "") or "",
        text_body=(row.text_body if row else "") or "",
        markdown=issue["markdown"],
        short=render.render_short(composed, name=issue["newsletter_name"]),
        item_count=issue["item_count"],
        url=f"{app_url}/newsletters/issues/{issue_id}" if app_url else "",
    )


def _sections_from_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild the section shape the renderers take, from the stored items."""
    out: list[dict[str, Any]] = []
    for item in items:
        section = next((s for s in out if s["id"] == item["section"]), None)
        if section is None:
            section = {"id": item["section"], "title": item["section"], "items": []}
            out.append(section)
        section["items"].append(item)
    return out


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


async def summary(issue_id: int) -> dict[str, Any]:
    rows = await list_deliveries(issue_id)
    return {
        "issue_id": issue_id,
        "total": len(rows),
        "sent": sum(1 for r in rows if r["status"] == SENT),
        "failed": sum(1 for r in rows if r["status"] == FAILED),
        "pending": sum(1 for r in rows if r["status"] == PENDING),
        "skipped": sum(1 for r in rows if r["status"] == SKIPPED),
        "deliveries": rows,
    }


async def list_deliveries(issue_id: int) -> list[dict[str, Any]]:
    async with session() as s:
        rows = (
            await s.execute(
                select(Delivery, Recipient.name, Recipient.address)
                .join(Recipient, Recipient.id == Delivery.recipient_id, isouter=True)
                .where(Delivery.issue_id == issue_id)
                .order_by(Delivery.id)
            )
        ).all()
    return [
        {
            "id": d.id,
            "channel": d.channel,
            "status": d.status,
            "attempts": d.attempts,
            "error": d.error,
            "provider_message_id": d.provider_message_id,
            "recipient_id": d.recipient_id,
            "recipient": name or address or "everyone",
            "sent_at": (as_utc(d.sent_at).isoformat() if d.sent_at else None),
            "next_retry_at": (as_utc(d.next_retry_at).isoformat() if d.next_retry_at else None),
        }
        for d, name, address in rows
    ]


async def retry_issue(issue_id: int) -> dict[str, Any]:
    """Put failed rows back to pending and send them again.

    Only failed rows: a delivery that already succeeded is never repeated, or a
    retry after a partial failure would send twice to everyone it worked for.
    """
    async with session() as s:
        rows = list(
            (
                await s.execute(
                    select(Delivery).where(
                        Delivery.issue_id == issue_id, Delivery.status == FAILED
                    )
                )
            ).scalars()
        )
        for row in rows:
            row.status = PENDING
            row.attempts = 0
            row.next_retry_at = None
            row.error = ""
        await s.commit()
    if rows:
        await _set_issue_status(issue_id, "sending")
    return await deliver_issue(issue_id, only_pending=True)


async def due_retries(limit: int = 200) -> list[int]:
    """Issue ids with a delivery whose retry time has come."""
    now = utcnow()
    async with session() as s:
        rows = list(
            (
                await s.execute(
                    select(Delivery.issue_id)
                    .where(Delivery.status == PENDING, Delivery.next_retry_at.is_not(None))
                    .limit(limit)
                )
            ).scalars()
        )
        due = []
        for issue_id in dict.fromkeys(rows):
            pending = list(
                (
                    await s.execute(
                        select(Delivery.next_retry_at).where(
                            Delivery.issue_id == issue_id, Delivery.status == PENDING
                        )
                    )
                ).scalars()
            )
            if any((as_utc(t) or now) <= now for t in pending):
                due.append(issue_id)
    return due


async def test_send(recipient_id: int, issue_id: int) -> dict[str, Any]:
    """Send one issue to one recipient, without touching the issue's own state.

    What you use before trusting a channel: it proves the credentials and the
    address work, and leaves no delivery row behind to confuse the record.
    """
    async with session() as s:
        recipient = await s.get(Recipient, recipient_id)
    if recipient is None:
        raise KeyError(f"No recipient {recipient_id}")

    impl = channel(recipient.channel)
    if impl is None:
        raise ValueError(f"Unknown channel {recipient.channel!r}")
    if not impl.is_configured:
        raise ValueError(impl.status_detail)

    payload = await _payload(issue_id)
    result = await impl.send(
        payload,
        RecipientRef(
            id=recipient.id,
            channel=recipient.channel,
            address=recipient.address,
            name=recipient.name,
        ),
    )
    return {"ok": result.ok, "error": result.error, "detail": result.provider_message_id}


async def clear_deliveries(issue_id: int) -> None:
    async with session() as s:
        await s.execute(delete(Delivery).where(Delivery.issue_id == issue_id))
        await s.commit()


__all__ = [
    "CHANNELS",
    "deliver_issue",
    "due_retries",
    "list_deliveries",
    "retry_issue",
    "summary",
    "test_send",
]
