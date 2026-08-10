"""HTTP for the news subsystem.

A second router rather than more of ``api.py``, which is already 715 lines of a
different domain. The service layer has been split this way from the start
(``catalog``, ``reviews``, ``config_store``, ``push``); only the router never was.

Conventions are api.py's, deliberately: no ``response_model=``, hand-built dicts,
``KeyError`` -> 404, ``ValueError`` -> 409, 422 for content the operator can fix,
202 for anything that becomes a run.

**No route here may be declared with a trailing slash.** ``ui/src/api/client.ts``
fetches with ``redirect: "manual"`` and treats any redirect as an expired Easy
Auth session, so a route that triggers FastAPI's ``redirect_slashes`` bounces the
user to the Entra login instead of returning data — silent, and baffling to debug.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import news
from ..settings import get_settings
from . import news_store
from .runs import manager

router = APIRouter(prefix="/api/news")


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


class FeedCreate(BaseModel):
    url: str = Field(..., min_length=3)
    name: str = ""
    tier: str = "unknown"
    topics: list[str] = Field(default_factory=list)
    realtime: bool = False
    group_ids: list[int] = Field(default_factory=list)
    notes: str = ""
    # Off only for tests and bulk imports; the UI always validates.
    validate_feed: bool = True


class FeedPatch(BaseModel):
    name: str | None = None
    tier: str | None = None
    topics: list[str] | None = None
    enabled: bool | None = None
    realtime: bool | None = None
    notes: str | None = None
    poll_interval_minutes: int | None = None
    group_ids: list[int] | None = None


class UrlBody(BaseModel):
    url: str = Field(..., min_length=3)


@router.get("/feeds")
async def list_feeds(
    enabled: bool | None = None,
    realtime: bool | None = None,
    group_id: int | None = None,
    q: str = "",
    limit: int = Query(500, le=1000),
) -> list[dict[str, Any]]:
    return await news_store.list_feeds(
        enabled=enabled, realtime=realtime, group_id=group_id, q=q, limit=limit
    )


@router.post("/feeds", status_code=201)
async def create_feed(body: FeedCreate) -> dict[str, Any]:
    """Register a feed, having first confirmed it is one.

    Validating before saving is the whole reason the registry can be trusted
    later: a discovery run may *suggest* a URL, but nothing is ever stored that
    has not been fetched and parsed.
    """
    title, site_url = "", ""
    if body.validate_feed:
        probe = await news.probe(body.url, timeout=float(get_settings().news.feed_timeout_seconds))
        if not probe.entries:
            raise HTTPException(422, news.describe_failure(probe))
        title, site_url = probe.title, probe.site_url

    try:
        return await news_store.create_feed(
            body.url,
            name=body.name,
            title=title,
            site_url=site_url,
            tier=body.tier,
            topics=body.topics,
            realtime=body.realtime,
            group_ids=body.group_ids,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/feeds/validate")
async def validate_feed(body: UrlBody) -> dict[str, Any]:
    """Probe a URL and describe what is there, without saving anything.

    Accepts a site URL as readily as a feed URL: if the address is not itself a
    feed, its ``<link rel="alternate">`` tags and the usual paths are tried, so
    the operator can paste what is in their address bar.
    """
    timeout = float(get_settings().news.feed_timeout_seconds)
    result = await news.probe(body.url, timeout=timeout)
    if result.entries:
        return _probe_dict(body.url, result)

    for candidate in await news.discover_feeds(body.url, timeout=timeout):
        found = await news.probe(candidate, timeout=timeout)
        if found.entries:
            return _probe_dict(candidate, found, discovered_from=body.url)

    return {
        "ok": False,
        "url": body.url,
        "error": news.describe_failure(result),
        "entries": [],
    }


def _probe_dict(url: str, result: news.FeedFetch, discovered_from: str = "") -> dict[str, Any]:
    newest = max((e.published for e in result.entries if e.published), default=None)
    return {
        "ok": True,
        "url": url,
        "discovered_from": discovered_from,
        "title": result.title,
        "site_url": result.site_url,
        "language": result.language,
        "entry_count": len(result.entries),
        "newest": newest.isoformat() if newest else None,
        # Five is enough to recognise a feed and not enough to be a page of text.
        "entries": [
            {
                "title": e.title,
                "url": e.url,
                "published": e.published.isoformat() if e.published else None,
                "summary": e.summary[:280],
            }
            for e in result.entries[:5]
        ],
        "error": "",
    }


@router.get("/feeds/{feed_id}")
async def get_feed(feed_id: int) -> dict[str, Any]:
    row = await news_store.get_feed(feed_id)
    if row is None:
        raise HTTPException(404, f"No feed {feed_id}")
    return row


@router.patch("/feeds/{feed_id}")
async def patch_feed(feed_id: int, body: FeedPatch) -> dict[str, Any]:
    changes = body.model_dump(exclude_none=True)
    try:
        return await news_store.update_feed(feed_id, changes)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/feeds/{feed_id}")
async def delete_feed(feed_id: int, purge: bool = False) -> dict[str, Any]:
    removed = await news_store.delete_feed(feed_id, purge=purge)
    if not removed:
        raise HTTPException(404, f"No feed {feed_id}")
    return {"deleted": True, "purged": purge}


@router.post("/feeds/{feed_id}/refresh", status_code=202)
async def refresh_feed(feed_id: int) -> dict[str, str]:
    row = await news_store.get_feed(feed_id)
    if row is None:
        raise HTTPException(404, f"No feed {feed_id}")
    run_id = await manager().enqueue(
        "ingest", {"feed_ids": [feed_id]}, f"Refresh · {row['name'] or row['url']}"
    )
    return {"id": run_id, "run_id": run_id}


@router.post("/refresh", status_code=202)
async def refresh_all(only_due: bool = False, only_realtime: bool = False) -> dict[str, str]:
    run_id = await manager().enqueue(
        "ingest",
        {"only_due": only_due, "only_realtime": only_realtime},
        "Refresh feeds",
    )
    return {"id": run_id, "run_id": run_id}


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


class GroupBody(BaseModel):
    name: str = ""
    description: str = ""


class GroupFeeds(BaseModel):
    feed_ids: list[int] = Field(default_factory=list)


@router.get("/feed-groups")
async def list_groups() -> list[dict[str, Any]]:
    return await news_store.list_groups()


@router.post("/feed-groups", status_code=201)
async def create_group(body: GroupBody) -> dict[str, Any]:
    try:
        return await news_store.create_group(body.name, description=body.description)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/feed-groups/{group_id}")
async def get_group(group_id: int) -> dict[str, Any]:
    row = await news_store.get_group(group_id)
    if row is None:
        raise HTTPException(404, f"No feed group {group_id}")
    return row


@router.patch("/feed-groups/{group_id}")
async def patch_group(group_id: int, body: GroupBody) -> dict[str, Any]:
    try:
        return await news_store.update_group(group_id, body.model_dump(exclude_defaults=True))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/feed-groups/{group_id}")
async def delete_group(group_id: int) -> dict[str, Any]:
    if not await news_store.delete_group(group_id):
        raise HTTPException(404, f"No feed group {group_id}")
    return {"deleted": True}


@router.put("/feed-groups/{group_id}/feeds")
async def set_group_feeds(group_id: int, body: GroupFeeds) -> dict[str, Any]:
    try:
        return await news_store.set_group_feeds(group_id, body.feed_ids)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------


@router.get("/articles")
async def list_articles(
    group_id: int | None = None,
    feed_id: int | None = None,
    since: str = "",
    q: str = "",
    limit: int = Query(100, le=500),
) -> list[dict[str, Any]]:
    return await news_store.list_articles(
        group_id=group_id,
        feed_id=feed_id,
        since=news_store.parse_since(since),
        q=q,
        limit=limit,
    )


@router.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict[str, Any]:
    row = await news_store.get_article(article_id)
    if row is None:
        raise HTTPException(404, f"No article {article_id}")
    return row


@router.get("/summary")
async def summary() -> dict[str, Any]:
    """Counts for the News screen.

    Deliberately its own endpoint and *not* part of /api/health: health is on the
    container's readiness probe every 15 seconds and currently touches no
    database at all. Putting a count there would wake the serverless SQL database
    around the clock and quietly end its auto-pause.
    """
    settings = get_settings().news
    counts = await news_store.counts()
    return {
        **counts,
        "ingest_interval_minutes": settings.ingest_interval_minutes,
        "realtime_interval_minutes": settings.realtime_interval_minutes,
        "db_can_autopause": settings.db_can_autopause,
    }
