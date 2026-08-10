"""Polling feeds and filing what comes back.

The one rule this module exists to enforce: **polling the same feed twice must
not create the same article twice.** Everything else here is bookkeeping around
that. Dedup is structural — a unique index on ``(feed_id, entry_key)`` — rather
than something this code has to remember, which is what makes "notify once"
true later without any extra care at the notify site.

The second thing it does that ``tools.read_feeds`` does not is *record failure*.
Today a feed that 403s returns ``[]`` and is indistinguishable from a feed with
nothing new, so a source can die and stay dead for months. Here every outcome
lands on the row: the HTTP status, the error text, and a strike count that
eventually disables the feed rather than retrying it forever.

Anything logged here reaches the run's SSE stream for free — ``RunLogHandler``
routes the ``ppn`` logger through whichever run is in the contextvar — so the
log lines are written to be read by a human watching a run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, true

from .. import news
from ..settings import get_settings
from .db import Article, Feed, as_utc, session, utcnow

logger = logging.getLogger("ppn.server.ingest")


async def ingest(
    *,
    feed_ids: list[int] | None = None,
    only_realtime: bool = False,
    only_due: bool = False,
) -> dict[str, Any]:
    """Poll feeds and file new articles.

    Returns a summary that becomes the run result, and therefore the text of the
    push notification when a scheduled sweep finishes.
    """
    settings = get_settings().news

    async with session() as s:
        stmt = select(Feed)
        if feed_ids is not None:
            stmt = stmt.where(Feed.id.in_(feed_ids))
        else:
            stmt = stmt.where(Feed.enabled == true())
            if only_realtime:
                stmt = stmt.where(Feed.realtime == true())
            if only_due:
                now = utcnow()
                stmt = stmt.where((Feed.next_poll_at.is_(None)) | (Feed.next_poll_at <= now))
        feeds = list((await s.execute(stmt)).scalars())

    if not feeds:
        logger.info("no feeds to poll")
        return {"feeds": 0, "not_modified": 0, "new_articles": 0, "errors": 0, "disabled": 0}

    logger.info("polling %d feed(s)", len(feeds))
    specs = [(f.url, f.etag, f.last_modified) for f in feeds]
    results = await news.fetch_many(
        specs,
        concurrency=settings.feed_concurrency,
        timeout=float(settings.feed_timeout_seconds),
    )

    summary = {
        "feeds": len(feeds),
        "not_modified": 0,
        "new_articles": 0,
        "errors": 0,
        "disabled": 0,
        "per_feed": [],
    }
    new_ids_by_feed: dict[int, list[int]] = {}

    for feed, result in zip(feeds, results, strict=True):
        outcome = await _apply(feed, result)
        summary["not_modified"] += 1 if outcome["not_modified"] else 0
        summary["new_articles"] += outcome["new"]
        summary["errors"] += 1 if outcome["error"] else 0
        summary["disabled"] += 1 if outcome["disabled"] else 0
        if outcome["new_ids"]:
            new_ids_by_feed[feed.id] = outcome["new_ids"]
        summary["per_feed"].append(
            {
                "feed_id": feed.id,
                "name": feed.name or feed.url,
                "new": outcome["new"],
                "not_modified": outcome["not_modified"],
                "error": outcome["error"],
            }
        )

    summary["new_by_feed"] = {str(k): len(v) for k, v in new_ids_by_feed.items()}
    logger.info(
        "%d new article(s) from %d feed(s) — %d unchanged, %d error(s)",
        summary["new_articles"],
        summary["feeds"],
        summary["not_modified"],
        summary["errors"],
    )
    return summary


async def _apply(feed: Feed, result: news.FeedFetch) -> dict[str, Any]:
    """Write one fetch outcome to the database."""
    settings = get_settings().news
    now = utcnow()
    out: dict[str, Any] = {
        "new": 0,
        "new_ids": [],
        "not_modified": result.not_modified,
        "error": "",
        "disabled": False,
    }

    async with session() as s:
        row = await s.get(Feed, feed.id)
        if row is None:
            return out

        row.last_checked_at = now
        row.last_status = result.status
        row.next_poll_at = _next_poll(row, now)

        if result.error:
            row.last_error = result.error[:400]
            row.consecutive_failures += 1
            out["error"] = result.error
            if row.consecutive_failures >= settings.max_failures and row.enabled:
                # Disabling beats retrying forever: a dead host otherwise fills
                # the log with the same line and hides live problems behind it.
                row.enabled = False
                row.next_poll_at = None
                out["disabled"] = True
                logger.warning(
                    "feed disabled after %d consecutive failures: %s (%s)",
                    row.consecutive_failures,
                    row.name or row.url,
                    result.error,
                )
            else:
                logger.info("feed failed: %s — %s", row.name or row.url, result.error)
            await s.commit()
            return out

        row.last_error = ""
        row.consecutive_failures = 0
        row.etag = (result.etag or "")[:200]
        row.last_modified = (result.last_modified or "")[:120]

        if result.not_modified:
            await s.commit()
            return out

        row.last_success_at = now
        if result.title and not row.title:
            row.title = result.title[:300]
        if result.site_url and not row.site_url:
            row.site_url = result.site_url
        if not row.name:
            row.name = (result.title or news.domain_of(row.url))[:200]

        entries = result.entries[: settings.max_items_per_feed]
        if entries:
            known = set(
                (
                    await s.execute(
                        select(Article.entry_key).where(
                            Article.feed_id == row.id,
                            Article.entry_key.in_([e.entry_key for e in entries]),
                        )
                    )
                ).scalars()
            )
            newest: datetime | None = as_utc(row.last_entry_at)
            seen_this_batch: set[str] = set()
            for entry in entries:
                # A feed listing the same entry twice in one document would
                # otherwise breach the unique index inside a single flush.
                if entry.entry_key in known or entry.entry_key in seen_this_batch:
                    continue
                seen_this_batch.add(entry.entry_key)
                article = Article(
                    feed_id=row.id,
                    entry_key=entry.entry_key,
                    url_hash=news.url_hash(entry.url),
                    url=entry.url,
                    title=entry.title[:400],
                    author=entry.author[:200],
                    summary=entry.summary,
                    tags=entry.tags,
                    domain=news.domain_of(entry.url)[:200],
                    tier=row.tier,
                    language=(entry.language or result.language or "")[:16],
                    published_at=entry.published,
                    fetched_at=now,
                )
                s.add(article)
                out["new"] += 1
                if entry.published and (newest is None or entry.published > newest):
                    newest = entry.published

            if out["new"]:
                row.entry_count += out["new"]
                row.last_entry_at = newest or now
                logger.info("%s: %d new", row.name or row.url, out["new"])

        await s.commit()

        if out["new"]:
            out["new_ids"] = list(
                (
                    await s.execute(
                        select(Article.id).where(
                            Article.feed_id == row.id, Article.fetched_at == now
                        )
                    )
                ).scalars()
            )

    return out


def _next_poll(row: Feed, now: datetime) -> datetime:
    settings = get_settings().news
    minutes = row.poll_interval_minutes or (
        settings.realtime_interval_minutes if row.realtime else settings.ingest_interval_minutes
    )
    return now + timedelta(minutes=max(1, minutes))


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def seed_feeds() -> int:
    """Copy the curated feeds from ``sources.yaml`` into the registry.

    A copy, not a move: ``tools.read_feeds`` keeps reading ``sources.yaml`` and
    the crew is untouched. This only gives the news subsystem something to show
    on first run instead of an empty page.

    Idempotent by ``url_hash``, so it is safe on every boot — same contract as
    ``catalog.backfill()``.
    """
    from .news_store import create_feed

    try:
        configured = get_settings().feeds
    except Exception as exc:  # noqa: BLE001 - never let seeding break startup
        logger.warning("could not read sources.yaml feeds: %s", exc)
        return 0

    added = 0
    for entry in configured:
        url = str(entry.get("url", "") or "").strip()
        if not url:
            continue
        try:
            await create_feed(
                url,
                name=str(entry.get("name", "") or ""),
                tier=str(entry.get("tier", "unknown") or "unknown"),
                origin="seed",
            )
            added += 1
        except ValueError:
            continue  # already registered
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not seed feed %s: %s", url, exc)

    if added:
        logger.info("seeded %d feed(s) from sources.yaml", added)
    return added
