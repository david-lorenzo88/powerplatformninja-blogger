"""Reads and writes for feeds, groups and articles.

Shaped after ``catalog.py``: module-level async functions over ``async with
session()``, hand-built ``_x_dict`` serialisers rather than response models, and
filters applied as ``stmt.where(...)``. Upserts are select-then-write throughout —
never ``ON CONFLICT`` — because the same code has to run on SQLite locally and
Azure SQL in production.

Duplicate detection lives here rather than in the API layer so that both the
HTTP route and the CLI get it: a feed is identified by
``news.canonical_url``, so pasting the same feed twice — once from the address
bar, once from a share link with a campaign parameter — is one row and a 409.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, desc, func, or_, select

from .. import news
from ..settings import get_settings
from .db import Article, Feed, FeedGroup, FeedGroupMember, as_utc, session, utcnow

logger = logging.getLogger("ppn.server.news")


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


async def list_feeds(
    *,
    enabled: bool | None = None,
    realtime: bool | None = None,
    group_id: int | None = None,
    q: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    async with session() as s:
        stmt = select(Feed).order_by(Feed.name, Feed.title, Feed.id).limit(limit)
        if enabled is not None:
            stmt = stmt.where(Feed.enabled == enabled)
        if realtime is not None:
            stmt = stmt.where(Feed.realtime == realtime)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Feed.name).like(like),
                    func.lower(Feed.title).like(like),
                    func.lower(Feed.home_domain).like(like),
                )
            )
        if group_id is not None:
            stmt = stmt.join(FeedGroupMember, FeedGroupMember.feed_id == Feed.id).where(
                FeedGroupMember.group_id == group_id
            )
        rows = list((await s.execute(stmt)).scalars())
        memberships = await _group_ids_for(s, [r.id for r in rows])
    return [_feed_dict(r, memberships.get(r.id, [])) for r in rows]


async def get_feed(feed_id: int) -> dict[str, Any] | None:
    async with session() as s:
        row = await s.get(Feed, feed_id)
        if row is None:
            return None
        memberships = await _group_ids_for(s, [row.id])
    return _feed_dict(row, memberships.get(row.id, []))


async def create_feed(
    url: str,
    *,
    name: str = "",
    title: str = "",
    site_url: str = "",
    tier: str = "unknown",
    topics: list[str] | None = None,
    realtime: bool = False,
    origin: str = "manual",
    group_ids: list[int] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Register a feed. Raises ``ValueError`` if it is already known."""
    canonical = news.canonical_url(url)
    if not canonical:
        raise ValueError("That does not look like a URL.")
    digest = news.url_hash(url)

    async with session() as s:
        existing = (
            await s.execute(select(Feed).where(Feed.url_hash == digest))
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError(f"Already following that feed: {existing.name or existing.url}")

        row = Feed(
            url=url.strip(),
            url_hash=digest,
            name=name.strip()[:200] or title.strip()[:200] or news.domain_of(canonical),
            title=title.strip()[:300],
            site_url=site_url.strip(),
            home_domain=news.domain_of(canonical)[:200],
            tier=tier[:40],
            topics=list(topics or []),
            realtime=bool(realtime),
            origin=origin[:32],
            notes=notes[:500],
            # Due immediately: a feed the operator just added should fill in
            # without waiting for the next sweep.
            next_poll_at=utcnow(),
        )
        s.add(row)
        await s.commit()
        if group_ids:
            await _set_feed_groups(s, row.id, group_ids)
            await s.commit()
        memberships = await _group_ids_for(s, [row.id])
        logger.info("feed added: %s (%s)", row.name, row.url)
        return _feed_dict(row, memberships.get(row.id, []))


async def update_feed(feed_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    """Patch a feed. Raises ``KeyError`` when it does not exist."""
    editable = {
        "name",
        "title",
        "site_url",
        "tier",
        "topics",
        "enabled",
        "realtime",
        "notes",
        "poll_interval_minutes",
        "used_by_crew",
    }
    async with session() as s:
        row = await s.get(Feed, feed_id)
        if row is None:
            raise KeyError(f"No feed {feed_id}")
        for key, value in changes.items():
            if key in editable:
                setattr(row, key, value)
        if changes.get("enabled"):
            # Re-enabling is the operator saying "try again" — clear the strike
            # count or it is disabled again on the next failure.
            row.consecutive_failures = 0
            row.last_error = ""
            row.next_poll_at = utcnow()
        if "group_ids" in changes:
            await _set_feed_groups(s, feed_id, list(changes["group_ids"] or []))
        await s.commit()
        memberships = await _group_ids_for(s, [feed_id])
        return _feed_dict(row, memberships.get(feed_id, []))


async def delete_feed(feed_id: int, *, purge: bool = False) -> bool:
    """Remove a feed. Without ``purge`` its articles are kept.

    Articles outlive their feed by default because a digest that already cited
    one must keep resolving — deleting a noisy source should not rewrite history.
    """
    async with session() as s:
        row = await s.get(Feed, feed_id)
        if row is None:
            return False
        await s.execute(delete(FeedGroupMember).where(FeedGroupMember.feed_id == feed_id))
        if purge:
            await s.execute(delete(Article).where(Article.feed_id == feed_id))
            await s.delete(row)
        else:
            row.enabled = False
            row.next_poll_at = None
        await s.commit()
    return True


async def due_feeds(*, only_realtime: bool = False, now: datetime | None = None) -> list[int]:
    """Ids of enabled feeds whose next poll is due."""
    moment = now or utcnow()
    async with session() as s:
        stmt = select(Feed.id).where(Feed.enabled.is_(True))
        if only_realtime:
            stmt = stmt.where(Feed.realtime.is_(True))
        stmt = stmt.where(or_(Feed.next_poll_at.is_(None), Feed.next_poll_at <= moment))
        return list((await s.execute(stmt)).scalars())


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


async def list_groups() -> list[dict[str, Any]]:
    async with session() as s:
        rows = list((await s.execute(select(FeedGroup).order_by(FeedGroup.name))).scalars())
        counts = dict(
            (
                await s.execute(
                    select(FeedGroupMember.group_id, func.count()).group_by(
                        FeedGroupMember.group_id
                    )
                )
            ).all()
        )
    return [_group_dict(r, int(counts.get(r.id, 0))) for r in rows]


async def get_group(group_id: int) -> dict[str, Any] | None:
    async with session() as s:
        row = await s.get(FeedGroup, group_id)
        if row is None:
            return None
        count = await s.scalar(
            select(func.count()).select_from(FeedGroupMember).where(
                FeedGroupMember.group_id == group_id
            )
        )
        feed_ids = list(
            (
                await s.execute(
                    select(FeedGroupMember.feed_id)
                    .where(FeedGroupMember.group_id == group_id)
                    .order_by(FeedGroupMember.position, FeedGroupMember.id)
                )
            ).scalars()
        )
    out = _group_dict(row, int(count or 0))
    out["feed_ids"] = feed_ids
    return out


async def create_group(name: str, *, description: str = "") -> dict[str, Any]:
    from slugify import slugify

    label = name.strip()
    if not label:
        raise ValueError("A group needs a name.")
    slug = slugify(label)[:120] or "group"
    async with session() as s:
        clash = (
            await s.execute(select(FeedGroup).where(FeedGroup.slug == slug))
        ).scalar_one_or_none()
        if clash is not None:
            raise ValueError(f"A group called {clash.name!r} already exists.")
        row = FeedGroup(slug=slug, name=label[:200], description=description)
        s.add(row)
        await s.commit()
        return _group_dict(row, 0)


async def update_group(group_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    async with session() as s:
        row = await s.get(FeedGroup, group_id)
        if row is None:
            raise KeyError(f"No feed group {group_id}")
        if "name" in changes and changes["name"]:
            row.name = str(changes["name"]).strip()[:200]
        if "description" in changes:
            row.description = str(changes["description"] or "")
        await s.commit()
        count = await s.scalar(
            select(func.count()).select_from(FeedGroupMember).where(
                FeedGroupMember.group_id == group_id
            )
        )
        return _group_dict(row, int(count or 0))


async def delete_group(group_id: int) -> bool:
    async with session() as s:
        row = await s.get(FeedGroup, group_id)
        if row is None:
            return False
        await s.execute(delete(FeedGroupMember).where(FeedGroupMember.group_id == group_id))
        await s.delete(row)
        await s.commit()
    return True


async def set_group_feeds(group_id: int, feed_ids: list[int]) -> dict[str, Any]:
    """Replace a group's membership wholesale — one call, no diffing in the UI."""
    async with session() as s:
        row = await s.get(FeedGroup, group_id)
        if row is None:
            raise KeyError(f"No feed group {group_id}")
        await s.execute(delete(FeedGroupMember).where(FeedGroupMember.group_id == group_id))
        for position, feed_id in enumerate(dict.fromkeys(feed_ids)):
            s.add(FeedGroupMember(group_id=group_id, feed_id=feed_id, position=position))
        await s.commit()
    result = await get_group(group_id)
    assert result is not None
    return result


async def _set_feed_groups(s: Any, feed_id: int, group_ids: list[int]) -> None:
    await s.execute(delete(FeedGroupMember).where(FeedGroupMember.feed_id == feed_id))
    for position, group_id in enumerate(dict.fromkeys(group_ids)):
        s.add(FeedGroupMember(group_id=group_id, feed_id=feed_id, position=position))


async def _group_ids_for(s: Any, feed_ids: list[int]) -> dict[int, list[int]]:
    if not feed_ids:
        return {}
    rows = (
        await s.execute(
            select(FeedGroupMember.feed_id, FeedGroupMember.group_id).where(
                FeedGroupMember.feed_id.in_(feed_ids)
            )
        )
    ).all()
    out: dict[int, list[int]] = {}
    for feed_id, group_id in rows:
        out.setdefault(feed_id, []).append(group_id)
    return out


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------


async def list_articles(
    *,
    group_id: int | None = None,
    feed_id: int | None = None,
    since: datetime | None = None,
    q: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    async with session() as s:
        stmt = (
            select(Article, Feed.name, Feed.tier)
            .join(Feed, Feed.id == Article.feed_id)
            .order_by(desc(func.coalesce(Article.published_at, Article.fetched_at)))
            .limit(max(1, min(limit, 500)))
        )
        if feed_id is not None:
            stmt = stmt.where(Article.feed_id == feed_id)
        if group_id is not None:
            stmt = stmt.join(FeedGroupMember, FeedGroupMember.feed_id == Article.feed_id).where(
                FeedGroupMember.group_id == group_id
            )
        if since is not None:
            stmt = stmt.where(func.coalesce(Article.published_at, Article.fetched_at) >= since)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(
                or_(func.lower(Article.title).like(like), func.lower(Article.summary).like(like))
            )
        rows = (await s.execute(stmt)).all()
    return [_article_dict(a, feed_name, feed_tier) for a, feed_name, feed_tier in rows]


async def get_article(article_id: int) -> dict[str, Any] | None:
    async with session() as s:
        row = (
            await s.execute(
                select(Article, Feed.name, Feed.tier)
                .join(Feed, Feed.id == Article.feed_id)
                .where(Article.id == article_id)
            )
        ).first()
    if row is None:
        return None
    article, feed_name, feed_tier = row
    return _article_dict(article, feed_name, feed_tier, full=True)


async def counts() -> dict[str, Any]:
    """Totals for the News screen header."""
    day_ago = utcnow() - timedelta(hours=24)
    async with session() as s:
        feeds = await s.scalar(select(func.count()).select_from(Feed))
        enabled = await s.scalar(
            select(func.count()).select_from(Feed).where(Feed.enabled.is_(True))
        )
        failing = await s.scalar(
            select(func.count()).select_from(Feed).where(Feed.consecutive_failures > 0)
        )
        realtime = await s.scalar(
            select(func.count()).select_from(Feed).where(
                Feed.enabled.is_(True), Feed.realtime.is_(True)
            )
        )
        groups = await s.scalar(select(func.count()).select_from(FeedGroup))
        articles = await s.scalar(select(func.count()).select_from(Article))
        recent = await s.scalar(
            select(func.count()).select_from(Article).where(Article.fetched_at >= day_ago)
        )
    return {
        "feeds": int(feeds or 0),
        "feeds_enabled": int(enabled or 0),
        "feeds_failing": int(failing or 0),
        "feeds_realtime": int(realtime or 0),
        "groups": int(groups or 0),
        "articles": int(articles or 0),
        "articles_last_24h": int(recent or 0),
    }


async def prune_articles(*, older_than_days: int | None = None) -> int:
    """Delete articles past the retention window.

    Anything an issue has cited is kept regardless — ``used_in_issue_at`` is set
    when a newsletter uses an article, and deleting it would break both the
    archive and the foreign key from ``newsletter_issue_items``.
    """
    days = older_than_days or get_settings().news.article_retention_days
    cutoff = utcnow() - timedelta(days=max(1, days))
    async with session() as s:
        result = await s.execute(
            delete(Article).where(
                Article.fetched_at < cutoff, Article.used_in_issue_at.is_(None)
            )
        )
        await s.commit()
    removed = int(result.rowcount or 0)
    if removed:
        logger.info("pruned %d article(s) older than %d days", removed, days)
    return removed


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _feed_dict(row: Feed, group_ids: list[int]) -> dict[str, Any]:
    return {
        "id": row.id,
        "url": row.url,
        "name": row.name,
        "title": row.title,
        "site_url": row.site_url,
        "domain": row.home_domain,
        "tier": row.tier,
        "topics": row.topics or [],
        "enabled": row.enabled,
        "realtime": row.realtime,
        "origin": row.origin,
        "group_ids": sorted(group_ids),
        "entry_count": row.entry_count,
        "poll_interval_minutes": row.poll_interval_minutes,
        "next_poll_at": _iso(row.next_poll_at),
        "last_checked_at": _iso(row.last_checked_at),
        "last_success_at": _iso(row.last_success_at),
        "last_entry_at": _iso(row.last_entry_at),
        "last_status": row.last_status,
        "last_error": row.last_error,
        "consecutive_failures": row.consecutive_failures,
        "notes": row.notes,
        "health": _health_of(row),
        "created_at": _iso(row.created_at),
    }


def _health_of(row: Feed) -> str:
    """One word for the UI: ok | stale | failing | disabled.

    'stale' is deliberately separate from 'failing': a feed that fetches
    perfectly but has published nothing in months is a different problem from one
    that is 403ing, and only the first is the operator's to judge.
    """
    if not row.enabled:
        return "disabled"
    if row.consecutive_failures > 0:
        return "failing"
    last_entry = as_utc(row.last_entry_at)
    if last_entry and last_entry < utcnow() - timedelta(days=90):
        return "stale"
    return "ok"


def _group_dict(row: FeedGroup, feed_count: int) -> dict[str, Any]:
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "description": row.description,
        "feed_count": feed_count,
        "created_at": _iso(row.created_at),
    }


def _article_dict(
    row: Article, feed_name: str, feed_tier: str, *, full: bool = False
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "feed_id": row.feed_id,
        "feed_name": feed_name,
        "url": row.url,
        "title": row.title,
        "author": row.author,
        "summary": row.summary,
        "domain": row.domain,
        "tier": row.tier or feed_tier,
        "tags": row.tags or [],
        "language": row.language,
        "published_at": _iso(row.published_at),
        "fetched_at": _iso(row.fetched_at),
    }
    if full:
        out["content"] = row.content
        out["notified_at"] = _iso(row.notified_at)
        out["used_in_issue_at"] = _iso(row.used_in_issue_at)
    return out


def parse_since(value: str) -> datetime | None:
    """Accept an ISO timestamp or a bare number of hours ('24')."""
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return utcnow() - timedelta(hours=int(raw))
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
