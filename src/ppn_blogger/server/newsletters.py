"""Newsletter definitions, their schedules, and the issues they produce.

Two things here are worth reading before changing anything.

**`candidates()` is the whole composition policy, in plain Python.** Which
articles exist, which are recent enough, which have already been used, and how
many may come from one feed — all decided here, before any model is involved.
The editor is handed a numbered list and can only choose from it. That is the
same discipline as `sources.py`: the model judges, the code decides what it is
allowed to judge.

**`next_due` is pure.** Given a newsletter and a moment it returns the next fire
time, with no I/O, so the schedule can be tested across a DST boundary without a
database. The scheduler claims that time with a compare-and-swap, exactly as it
does for the system jobs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, desc, func, select, true

from ..settings import get_settings
from .db import (
    Article,
    Delivery,
    Feed,
    FeedGroupMember,
    Newsletter,
    NewsletterGroup,
    NewsletterIssue,
    NewsletterIssueItem,
    Recipient,
    as_utc,
    session,
    utcnow,
)

logger = logging.getLogger("ppn.server.newsletters")

# An issue that reached one of these has spent its articles; anything else — a
# draft the operator discarded, a failed run — must not burn them permanently.
SPENT_STATUSES = ("ready", "sending", "sent")

SCHEDULE_KINDS = ("manual", "interval", "daily", "weekly", "monthly")


# ---------------------------------------------------------------------------
# Schedules — pure
# ---------------------------------------------------------------------------


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001 - an unknown zone must not break a schedule
        logger.warning("unknown timezone %r on a newsletter — scheduling in UTC", name)
        return None


def next_due(newsletter: dict[str, Any], *, after: datetime) -> datetime | None:
    """When this newsletter should next generate, or None if it never should.

    Computed in the newsletter's own zone and returned as aware UTC. `manual`
    returns None: it only ever runs when someone presses the button.
    """
    kind = str(newsletter.get("schedule_kind", "manual"))
    if kind == "manual" or not newsletter.get("enabled", True):
        return None

    if kind == "interval":
        minutes = max(1, int(newsletter.get("interval_minutes", 0) or 0))
        return after + timedelta(minutes=minutes)

    zone = _zone(str(newsletter.get("timezone", "UTC")))
    local = after.astimezone(zone) if zone else after
    hour = max(0, min(23, int(newsletter.get("hour_local", 7) or 0)))
    minute = max(0, min(59, int(newsletter.get("minute_local", 0) or 0)))
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if kind == "daily":
        # Anchored to a wall-clock time rather than expressed as a 24-hour
        # interval: an interval is measured from the last *generation*, so a
        # daily issue would walk a little later every day and land at a
        # different hour within a week. A reader expects the letter at the same
        # time each morning, and DST is handled by computing in the local zone.
        if target <= local:
            target = target + timedelta(days=1)
    elif kind == "weekly":
        wanted = max(0, min(6, int(newsletter.get("weekday", 0) or 0)))
        ahead = (wanted - target.weekday()) % 7
        target = target + timedelta(days=ahead)
        if target <= local:
            target = target + timedelta(days=7)
    elif kind == "monthly":
        day = max(1, min(28, int(newsletter.get("day_of_month", 1) or 1)))
        # Capped at 28 rather than clamped per month: "the 31st" silently
        # meaning "the 28th" in February is a schedule that lies about itself.
        target = target.replace(day=day)
        if target <= local:
            target = (target.replace(day=1) + timedelta(days=32)).replace(day=day)
    else:
        return None

    return target.astimezone(ZoneInfo("UTC")) if zone else target


def upcoming(newsletter: dict[str, Any], *, count: int = 3, after: datetime | None = None) -> list[str]:
    """The next few fire times — so a schedule can be checked before it is trusted."""
    moment = after or utcnow()
    out: list[str] = []
    for _ in range(count):
        nxt = next_due(newsletter, after=moment)
        if nxt is None:
            break
        out.append(nxt.isoformat())
        moment = nxt + timedelta(seconds=1)
    return out


# ---------------------------------------------------------------------------
# Candidates — the composition policy
# ---------------------------------------------------------------------------


async def candidates(newsletter_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """The articles the next issue would draw from. No model is involved.

    Backs `GET /newsletters/{id}/preview`, which is the cheapest way to tune a
    newsletter: change the groups or the window and see exactly what changes,
    for free.
    """
    moment = now or utcnow()
    row = await get(newsletter_id)
    if row is None:
        raise KeyError(f"No newsletter {newsletter_id}")

    window_from = moment - timedelta(hours=max(1, int(row["lookback_hours"] or 168)))
    group_ids = row["group_ids"]
    if not group_ids:
        return {
            "newsletter_id": newsletter_id,
            "window_from": window_from.isoformat(),
            "window_to": moment.isoformat(),
            "candidates": [],
            "reason": "this newsletter has no feed groups yet",
        }

    async with session() as s:
        used = set(
            (
                await s.execute(
                    select(NewsletterIssueItem.article_id)
                    .join(NewsletterIssue, NewsletterIssue.id == NewsletterIssueItem.issue_id)
                    .where(
                        NewsletterIssueItem.newsletter_id == newsletter_id,
                        NewsletterIssue.status.in_(SPENT_STATUSES),
                    )
                )
            ).scalars()
        )

        rows = (
            await s.execute(
                select(Article, Feed.name)
                .join(Feed, Feed.id == Article.feed_id)
                .join(FeedGroupMember, FeedGroupMember.feed_id == Article.feed_id)
                .where(
                    FeedGroupMember.group_id.in_(group_ids),
                    func.coalesce(Article.published_at, Article.fetched_at) >= window_from,
                )
                .order_by(desc(func.coalesce(Article.published_at, Article.fetched_at)))
            )
        ).all()

    max_per_feed = max(1, int(row["max_per_feed"] or 3))
    per_feed: dict[int, int] = {}
    seen_urls: set[str] = set()
    out: list[dict[str, Any]] = []

    for article, feed_name in rows:
        if article.id in used:
            continue
        # The same story syndicated by two feeds is one item.
        if article.url_hash in seen_urls:
            continue
        if per_feed.get(article.feed_id, 0) >= max_per_feed:
            continue
        seen_urls.add(article.url_hash)
        per_feed[article.feed_id] = per_feed.get(article.feed_id, 0) + 1
        published = as_utc(article.published_at) or as_utc(article.fetched_at)
        out.append(
            {
                "id": article.id,
                "title": article.title,
                "url": article.url,
                "source": feed_name or article.domain,
                "published": published.date().isoformat() if published else "",
                "summary": article.summary or "",
            }
        )

    return {
        "newsletter_id": newsletter_id,
        "window_from": window_from.isoformat(),
        "window_to": moment.isoformat(),
        "candidates": out,
        "already_used": len(used),
        "max_items": row["max_items"],
        # There is no minimum any more: anything at all is an issue, and only an
        # empty window is a skip.
        "enough": bool(out),
    }


# ---------------------------------------------------------------------------
# Newsletters
# ---------------------------------------------------------------------------


async def list_newsletters() -> list[dict[str, Any]]:
    async with session() as s:
        rows = list((await s.execute(select(Newsletter).order_by(Newsletter.name))).scalars())
        groups = await _groups_for(s, [r.id for r in rows])
        counts = dict(
            (
                await s.execute(
                    select(NewsletterIssue.newsletter_id, func.count()).group_by(
                        NewsletterIssue.newsletter_id
                    )
                )
            ).all()
        )
    return [_dict(r, groups.get(r.id, []), int(counts.get(r.id, 0))) for r in rows]


async def get(newsletter_id: int) -> dict[str, Any] | None:
    async with session() as s:
        row = await s.get(Newsletter, newsletter_id)
        if row is None:
            return None
        groups = await _groups_for(s, [row.id])
        count = await s.scalar(
            select(func.count())
            .select_from(NewsletterIssue)
            .where(NewsletterIssue.newsletter_id == newsletter_id)
        )
    return _dict(row, groups.get(row.id, []), int(count or 0))


async def create(name: str, **fields: Any) -> dict[str, Any]:
    from slugify import slugify

    label = (name or "").strip()
    if not label:
        raise ValueError("A newsletter needs a name.")
    slug = slugify(label)[:120] or "newsletter"

    async with session() as s:
        clash = (
            await s.execute(select(Newsletter).where(Newsletter.slug == slug))
        ).scalar_one_or_none()
        if clash is not None:
            raise ValueError(f"A newsletter called {clash.name!r} already exists.")
        row = Newsletter(slug=slug, name=label[:200])
        _apply(row, fields)
        s.add(row)
        # Flush before computing the due time: column defaults (schedule_kind,
        # hour_local, timezone…) are applied by the INSERT, and next_due reads
        # them. Without this a weekly created in one call would schedule 00:00
        # instead of 07:00, because `int(None or 0)` is 0.
        await s.flush()
        if fields.get("group_ids"):
            await _set_groups(s, row.id, list(fields["group_ids"]))
        await _refresh_due(s, row)
        await s.commit()
    result = await get(row.id)
    assert result is not None
    logger.info("newsletter created: %s", label)
    return result


async def update(newsletter_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    async with session() as s:
        row = await s.get(Newsletter, newsletter_id)
        if row is None:
            raise KeyError(f"No newsletter {newsletter_id}")
        _apply(row, changes)
        if "group_ids" in changes:
            await _set_groups(s, newsletter_id, list(changes["group_ids"] or []))
        await _refresh_due(s, row)
        await s.commit()
    result = await get(newsletter_id)
    assert result is not None
    return result


async def delete_newsletter(newsletter_id: int) -> bool:
    async with session() as s:
        row = await s.get(Newsletter, newsletter_id)
        if row is None:
            return False
        issue_ids = list(
            (
                await s.execute(
                    select(NewsletterIssue.id).where(
                        NewsletterIssue.newsletter_id == newsletter_id
                    )
                )
            ).scalars()
        )
        if issue_ids:
            await s.execute(
                delete(NewsletterIssueItem).where(NewsletterIssueItem.issue_id.in_(issue_ids))
            )
        await s.execute(
            delete(NewsletterIssue).where(NewsletterIssue.newsletter_id == newsletter_id)
        )
        await s.execute(
            delete(NewsletterGroup).where(NewsletterGroup.newsletter_id == newsletter_id)
        )
        await s.delete(row)
        await s.commit()
    return True


async def due_newsletters(*, now: datetime | None = None) -> list[int]:
    moment = now or utcnow()
    async with session() as s:
        return list(
            (
                await s.execute(
                    select(Newsletter.id).where(
                        Newsletter.enabled == true(),
                        Newsletter.next_due_at.is_not(None),
                        Newsletter.next_due_at <= moment,
                    )
                )
            ).scalars()
        )


async def claim_due(newsletter_id: int, *, now: datetime | None = None) -> bool:
    """Compare-and-swap on `next_due_at`, same as the system jobs.

    Two schedulers overlap on every deploy; this is what stops both queueing the
    same issue. Also refuses when the previous run is still going, so a slow
    generation cannot stack up behind itself.
    """
    from sqlalchemy import update as sql_update

    from .db import Run

    moment = now or utcnow()
    async with session() as s:
        row = await s.get(Newsletter, newsletter_id)
        if row is None or not row.enabled or row.next_due_at is None:
            return False
        due = as_utc(row.next_due_at)
        if due is None or due > moment:
            return False

        if row.last_enqueued_run_id:
            previous = await s.get(Run, row.last_enqueued_run_id)
            if previous is not None and previous.status in ("queued", "running"):
                logger.info("newsletter %s still generating — not queueing another", row.name)
                return False

        seen = row.next_due_at
        following = next_due(_dict(row, [], 0), after=moment)
        result = await s.execute(
            sql_update(Newsletter)
            .where(Newsletter.id == newsletter_id, Newsletter.next_due_at == seen)
            .values(next_due_at=following, last_run_at=moment)
        )
        await s.commit()
        return bool(result.rowcount == 1)


async def attach_run(newsletter_id: int, run_id: str) -> None:
    async with session() as s:
        row = await s.get(Newsletter, newsletter_id)
        if row is not None:
            row.last_enqueued_run_id = run_id
            await s.commit()


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


async def save_issue(
    newsletter_id: int,
    composed: dict[str, Any],
    rendered: dict[str, str],
    *,
    run_id: str = "",
    window_from: datetime | None = None,
    window_to: datetime | None = None,
    status: str = "draft",
) -> dict[str, Any]:
    """Persist a composed issue and the articles it used.

    Written before anything is sent, and before the run reports success — an
    issue that cost model calls must survive whatever happens next.
    """
    async with session() as s:
        number = int(
            await s.scalar(
                select(func.coalesce(func.max(NewsletterIssue.number), 0)).where(
                    NewsletterIssue.newsletter_id == newsletter_id
                )
            )
            or 0
        ) + 1

        issue = NewsletterIssue(
            newsletter_id=newsletter_id,
            run_id=run_id or None,
            number=number,
            status=status,
            subject=str(composed.get("subject", ""))[:300],
            preheader=str(composed.get("preheader", ""))[:300],
            intro=str(composed.get("intro", "")),
            markdown=rendered.get("markdown", ""),
            html=rendered.get("html", ""),
            text_body=rendered.get("text_body", ""),
            item_count=len(composed.get("article_ids", [])),
            window_from=window_from,
            window_to=window_to,
            error=str(composed.get("skipped_reason", "")),
            generated_on=str(composed.get("generated_on", "")),
        )
        s.add(issue)
        await s.commit()

        position = 0
        now = utcnow()
        for section in composed.get("sections", []):
            for item in section.get("items", []):
                s.add(
                    NewsletterIssueItem(
                        issue_id=issue.id,
                        newsletter_id=newsletter_id,
                        article_id=int(item["article_id"]),
                        section=str(section.get("id", ""))[:80],
                        position=position,
                        headline=str(item.get("headline", ""))[:400],
                        blurb=str(item.get("blurb", "")),
                    )
                )
                position += 1
        # Mark the articles so retention never prunes something an issue cites.
        ids = [int(i) for i in composed.get("article_ids", [])]
        if ids:
            from sqlalchemy import update as sql_update

            await s.execute(
                sql_update(Article).where(Article.id.in_(ids)).values(used_in_issue_at=now)
            )

        newsletter = await s.get(Newsletter, newsletter_id)
        if newsletter is not None:
            newsletter.last_issue_id = issue.id
        await s.commit()

    logger.info("issue #%d saved with %d item(s)", number, issue.item_count)
    result = await get_issue(issue.id)
    assert result is not None
    return result


async def list_issues(newsletter_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    async with session() as s:
        stmt = (
            select(NewsletterIssue, Newsletter.name)
            .join(Newsletter, Newsletter.id == NewsletterIssue.newsletter_id)
            .order_by(desc(NewsletterIssue.created_at))
            .limit(limit)
        )
        if newsletter_id is not None:
            stmt = stmt.where(NewsletterIssue.newsletter_id == newsletter_id)
        rows = (await s.execute(stmt)).all()
    return [_issue_dict(i, name) for i, name in rows]


async def get_issue(issue_id: int, *, full: bool = True) -> dict[str, Any] | None:
    async with session() as s:
        row = (
            await s.execute(
                select(NewsletterIssue, Newsletter.name)
                .join(Newsletter, Newsletter.id == NewsletterIssue.newsletter_id)
                .where(NewsletterIssue.id == issue_id)
            )
        ).first()
        if row is None:
            return None
        issue, name = row
        items = (
            await s.execute(
                select(NewsletterIssueItem, Article.url)
                .join(Article, Article.id == NewsletterIssueItem.article_id, isouter=True)
                .where(NewsletterIssueItem.issue_id == issue_id)
                .order_by(NewsletterIssueItem.position)
            )
        ).all()

    out = _issue_dict(issue, name, full=full)
    out["items"] = [
        {
            "article_id": item.article_id,
            "section": item.section,
            "position": item.position,
            "headline": item.headline,
            "blurb": item.blurb,
            "url": url or "",
        }
        for item, url in items
    ]
    return out


async def update_issue(issue_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    """Edit an issue before it goes anywhere. Raises ValueError once it has."""
    async with session() as s:
        row = await s.get(NewsletterIssue, issue_id)
        if row is None:
            raise KeyError(f"No issue {issue_id}")
        if row.status in ("sending", "sent"):
            raise ValueError(f"Issue {issue_id} is {row.status} and can no longer be edited.")
        for key in ("subject", "preheader", "intro", "markdown", "status"):
            if key in changes and changes[key] is not None:
                setattr(row, key, changes[key])
        await s.commit()
    result = await get_issue(issue_id)
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EDITABLE = {
    "name",
    "description",
    "enabled",
    "schedule_kind",
    "interval_minutes",
    "weekday",
    "day_of_month",
    "hour_local",
    "minute_local",
    "timezone",
    "lookback_hours",
    "max_items",
    "max_per_feed",
    "audience",
    "tone",
    "auto_send",
}


def _apply(row: Newsletter, changes: dict[str, Any]) -> None:
    """Validate the incoming value, not the row.

    A `default=` on a column is applied by the INSERT, not by the constructor, so
    a freshly built row still has `schedule_kind is None` here — checking the
    attribute would reject every create.
    """
    kind = changes.get("schedule_kind")
    if kind is not None and kind not in SCHEDULE_KINDS:
        raise ValueError(
            f"Unknown schedule kind {kind!r} — expected one of {', '.join(SCHEDULE_KINDS)}."
        )
    for key, value in changes.items():
        if key in EDITABLE and value is not None:
            setattr(row, key, value)


async def _refresh_due(s: Any, row: Newsletter) -> None:
    row.next_due_at = next_due(_dict(row, [], 0), after=utcnow())


async def _set_groups(s: Any, newsletter_id: int, group_ids: list[int]) -> None:
    await s.execute(delete(NewsletterGroup).where(NewsletterGroup.newsletter_id == newsletter_id))
    for group_id in dict.fromkeys(group_ids):
        s.add(NewsletterGroup(newsletter_id=newsletter_id, group_id=group_id))


async def _groups_for(s: Any, ids: list[int]) -> dict[int, list[int]]:
    if not ids:
        return {}
    rows = (
        await s.execute(
            select(NewsletterGroup.newsletter_id, NewsletterGroup.group_id).where(
                NewsletterGroup.newsletter_id.in_(ids)
            )
        )
    ).all()
    out: dict[int, list[int]] = {}
    for newsletter_id, group_id in rows:
        out.setdefault(newsletter_id, []).append(group_id)
    return out


def _iso(value: datetime | None) -> str | None:
    aware = as_utc(value)
    return aware.isoformat() if aware else None


def _dict(row: Newsletter, group_ids: list[int], issue_count: int) -> dict[str, Any]:
    out = {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "description": row.description,
        "enabled": row.enabled,
        "schedule_kind": row.schedule_kind,
        "interval_minutes": row.interval_minutes,
        "weekday": row.weekday,
        "day_of_month": row.day_of_month,
        "hour_local": row.hour_local,
        "minute_local": row.minute_local,
        "timezone": row.timezone,
        "lookback_hours": row.lookback_hours,
        "max_items": row.max_items,
        "max_per_feed": row.max_per_feed,
        "audience": row.audience,
        "tone": row.tone,
        "auto_send": row.auto_send,
        "group_ids": sorted(group_ids),
        "issue_count": issue_count,
        "next_due_at": _iso(row.next_due_at),
        "last_run_at": _iso(row.last_run_at),
        "last_issue_id": row.last_issue_id,
        "created_at": _iso(row.created_at),
    }
    out["upcoming"] = upcoming(out, count=3)
    return out


def _issue_dict(row: NewsletterIssue, newsletter_name: str, *, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "newsletter_id": row.newsletter_id,
        "newsletter_name": newsletter_name,
        "run_id": row.run_id,
        "number": row.number,
        "status": row.status,
        "subject": row.subject,
        "preheader": row.preheader,
        "item_count": row.item_count,
        "generated_on": row.generated_on,
        "error": row.error,
        "window_from": _iso(row.window_from),
        "window_to": _iso(row.window_to),
        "created_at": _iso(row.created_at),
        "sent_at": _iso(row.sent_at),
    }
    if full:
        out["intro"] = row.intro
        out["markdown"] = row.markdown
        out["text_body"] = row.text_body
    return out


def settings_defaults() -> dict[str, Any]:
    """Section ids the UI can offer, straight from the config document."""
    return {"sections": get_settings().newsletter_sections}


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------


def _normalise_address(channel_id: str, address: str) -> str:
    """One spelling per address, so the same person cannot be added twice.

    Email is case-insensitive in practice; a phone number is the same number
    with or without its plus and spaces; a Telegram chat id is already canonical.
    """
    raw = (address or "").strip()
    if channel_id == "email":
        return raw.lower()
    if channel_id == "whatsapp":
        return "+" + "".join(ch for ch in raw if ch.isdigit())
    return raw


async def list_recipients() -> list[dict[str, Any]]:
    async with session() as s:
        rows = list(
            (await s.execute(select(Recipient).order_by(Recipient.channel, Recipient.name))).scalars()
        )
    return [_recipient_dict(r) for r in rows]


async def create_recipient(
    channel_id: str, address: str, *, name: str = "", newsletter_ids: list[int] | None = None
) -> dict[str, Any]:
    from hashlib import sha256

    from .channels import channel as get_channel

    impl = get_channel(channel_id)
    if impl is None:
        raise ValueError(f"Unknown channel {channel_id!r}")

    normalised = _normalise_address(channel_id, address)
    if not normalised and not impl.broadcast:
        raise ValueError(f"{impl.label} needs an address.")

    digest = sha256(f"{channel_id}:{normalised}".encode()).hexdigest()
    async with session() as s:
        clash = (
            await s.execute(
                select(Recipient).where(
                    Recipient.channel == channel_id, Recipient.address_hash == digest
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise ValueError(f"That {impl.label} recipient is already on the list.")
        row = Recipient(
            channel=channel_id,
            address=normalised,
            address_hash=digest,
            name=name.strip()[:200] or normalised or impl.label,
            newsletter_ids=list(newsletter_ids or []),
        )
        s.add(row)
        await s.commit()
        return _recipient_dict(row)


async def update_recipient(recipient_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    async with session() as s:
        row = await s.get(Recipient, recipient_id)
        if row is None:
            raise KeyError(f"No recipient {recipient_id}")
        for key in ("name", "enabled", "notes", "newsletter_ids"):
            if key in changes and changes[key] is not None:
                setattr(row, key, changes[key])
        if changes.get("enabled"):
            # Re-enabling is "try again": clear the parked failure, or the very
            # next send skips it for a reason that is no longer true.
            row.failed_at = None
            row.last_error = ""
        await s.commit()
        return _recipient_dict(row)


async def delete_recipient(recipient_id: int) -> bool:
    """Remove a recipient, and the delivery rows that point at it.

    `deliveries.recipient_id` is a real foreign key, so deleting the row on its
    own is *547 The DELETE statement conflicted with the REFERENCE constraint* on
    SQL Server — a 500 from the Remove button, which is how this was found. The
    delivery rows go with it rather than being orphaned: nulling the column would
    make them read as broadcast rows, which is a lie about what was sent. What is
    lost is the per-recipient history of a recipient that no longer exists; the
    issue keeps its own status and counts.

    A parked recipient does not need removing at all — re-enabling it clears the
    failure (see `update_recipient`) — but Remove has to work when it is pressed.
    """
    async with session() as s:
        row = await s.get(Recipient, recipient_id)
        if row is None:
            return False
        await s.execute(delete(Delivery).where(Delivery.recipient_id == recipient_id))
        await s.delete(row)
        await s.commit()
    return True


def _recipient_dict(row: Recipient) -> dict[str, Any]:
    return {
        "id": row.id,
        "channel": row.channel,
        "address": row.address,
        "name": row.name,
        "enabled": row.enabled,
        "newsletter_ids": row.newsletter_ids or [],
        "notes": row.notes,
        "failed_at": _iso(row.failed_at),
        "last_error": row.last_error,
        "created_at": _iso(row.created_at),
    }
