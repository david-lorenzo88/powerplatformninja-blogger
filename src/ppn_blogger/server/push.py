"""Web Push: subscriptions, and telling a phone a run has finished.

The point of this module is the closed-tab case. SSE only reaches a browser that
currently has ``/api/runs/{id}/events`` open, and a write run takes the better
part of an hour — nobody watches it. Push is how the phone says "your draft is
ready" or "three sites are waiting for a verdict" while the app is shut.

Nothing here may raise. It follows the same doctrine as cover generation and the
WordPress push: bookkeeping must never sink a finished run. A push service being
slow, a key being wrong, a subscription having expired — none of that is a reason
to lose a draft the crew spent forty minutes producing.

The style mirrors ``config_store.py``: module-level ``async`` functions over
``async with session()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select

from ..settings import get_settings
from .db import PushSubscription, session, utcnow

logger = logging.getLogger("ppn.server.push")

# A subscription the push service has retired. Both mean the same thing — this
# endpoint will never deliver again — so the row goes rather than accumulating
# failures forever.
GONE = {404, 410}


async def subscribe(endpoint: str, p256dh: str, auth: str, user_agent: str = "") -> int:
    """Record a browser's grant, or refresh the one already on file.

    Select-then-write rather than a dialect upsert: the same portability choice
    catalog.py makes, so this behaves identically on SQLite and Azure SQL.
    Re-subscribing is idempotent — browsers re-issue the same endpoint on every
    load, and a duplicate would mean two notifications for one run.
    """
    async with session() as s:
        row = (
            await s.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
        ).scalar_one_or_none()
        if row is None:
            row = PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth)
            s.add(row)
        row.p256dh = p256dh
        row.auth = auth
        row.user_agent = user_agent[:300]
        row.failures = 0
        await s.commit()
        return row.id


async def unsubscribe(endpoint: str) -> bool:
    async with session() as s:
        row = (
            await s.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
        ).scalar_one_or_none()
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
        return True


async def count() -> int:
    async with session() as s:
        rows = (await s.execute(select(PushSubscription.id))).scalars().all()
        return len(rows)


def _send_one(subscription: dict[str, Any], payload: str) -> int:
    """Blocking send. Returns the push service's status code, 0 on an unknown error.

    pywebpush is synchronous (it is built on requests), so every call to this
    goes through asyncio.to_thread. Imported lazily so the dependency is only
    needed when push is actually configured.
    """
    from pywebpush import WebPushException, webpush

    push = get_settings().push
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=push.private_key,
            vapid_claims={"sub": push.subject},
            timeout=10,
        )
        return 200
    except WebPushException as exc:  # noqa: BLE001
        return getattr(exc.response, "status_code", 0) or 0


async def notify(title: str, body: str, url: str, tag: str = "") -> int:
    """Send to every registered browser. Returns how many were delivered.

    Retired subscriptions are pruned as they are discovered — a phone that was
    reset, or an app removed from the home screen, would otherwise be retried on
    every run forever.
    """
    push = get_settings().push
    if not push.is_configured:
        return 0

    async with session() as s:
        rows = (await s.execute(select(PushSubscription))).scalars().all()
        targets = [
            (
                r.id,
                {
                    "endpoint": r.endpoint,
                    "keys": {"p256dh": r.p256dh, "auth": r.auth},
                },
            )
            for r in rows
        ]
    if not targets:
        return 0

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or url})
    results = await asyncio.gather(
        *(asyncio.to_thread(_send_one, sub, payload) for _, sub in targets),
        return_exceptions=True,
    )

    delivered = 0
    dead: list[int] = []
    async with session() as s:
        for (row_id, _), outcome in zip(targets, results, strict=True):
            code = 0 if isinstance(outcome, BaseException) else outcome
            row = await s.get(PushSubscription, row_id)
            if row is None:
                continue
            if code in GONE:
                dead.append(row_id)
                await s.delete(row)
            elif 200 <= code < 300:
                delivered += 1
                row.last_ok_at = utcnow()
                row.failures = 0
            else:
                row.failures += 1
        await s.commit()

    if dead:
        logger.info("pruned %d expired push subscription(s)", len(dead))
    return delivered


# -- What a finished run says -------------------------------------------------


def _describe(kind: str, status: str, label: str, result: dict[str, Any] | None) -> tuple[str, str, str]:
    """(title, body, url) for a run that has just reached a terminal state.

    Exactly one notification per run, including the exploration case: a sweep
    that stops for approval finishes `succeeded` with `awaiting_source_approval`
    in its result, and *that* is the thing worth waking someone for — not a
    generic "run finished" that says nothing about the verdict being blocked on
    them.
    """
    result = result or {}
    name = label or kind

    if result.get("awaiting_source_approval"):
        new = result.get("new_count", 0)
        total = result.get("candidate_count", 0)
        return (
            "Sites awaiting your verdict",
            f"{name}: {total} site{'' if total == 1 else 's'} found, {new} new. "
            "Nothing reaches the topic editor until you approve.",
            f"/source-reviews/{result.get('review_id', '')}".rstrip("/"),
        )

    if status != "succeeded":
        return (f"Run {status}", name, "/runs")

    if kind == "write":
        approved = result.get("approved")
        score = result.get("score")
        verdict = "APPROVED" if approved else "NOT APPROVED"
        title = result.get("title") or name
        detail = f"{verdict}" + (f" · score {score}" if score is not None else "")
        post_id = result.get("post_id")
        return ("Draft ready", f"{title} — {detail}", "/drafts" if not post_id else "/drafts")

    if kind in {"suggest", "shortlist"}:
        n = len(result.get("suggestions") or [])
        return ("Topics ready", f"{name}: {n} suggestion{'' if n == 1 else 's'}", "/topic-ideas")

    return ("Run finished", name, "/runs")


async def notify_run_finished(
    kind: str, status: str, label: str, result: dict[str, Any] | None
) -> None:
    """Fire-and-forget notification for a terminal run. Never raises."""
    try:
        # A run the operator cancelled themselves needs no announcement — they
        # were looking at it a second ago.
        if status == "cancelled":
            return
        title, body, url = _describe(kind, status, label, result)
        sent = await notify(title, body, url)
        if sent:
            logger.info("push: notified %d device(s) — %s", sent, title)
    except Exception:  # noqa: BLE001
        logger.exception("push notification failed")
