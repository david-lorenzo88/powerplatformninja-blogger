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
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import news
from ..settings import get_settings
from . import news_store, newsletters
from .runs import manager
from .scheduler import scheduler

router = APIRouter(prefix="/api/news")


async def _reschedule() -> None:
    """Re-derive the schedule after a change that could alter it.

    Whether the watch job exists at all depends on whether any feed is watched,
    and the scheduler sleeps until its next due time rather than polling — so a
    change that nothing announces would not take effect until that sleep ended.
    """
    await scheduler().sync_jobs()
    scheduler().wake()


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
        feed = await news_store.create_feed(
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
    await _reschedule()
    return feed


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
        feed = await news_store.update_feed(feed_id, changes)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    await _reschedule()
    return feed


@router.delete("/feeds/{feed_id}")
async def delete_feed(feed_id: int, purge: bool = False) -> dict[str, Any]:
    removed = await news_store.delete_feed(feed_id, purge=purge)
    if not removed:
        raise HTTPException(404, f"No feed {feed_id}")
    await _reschedule()
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
    watched = counts["feeds_realtime"]
    scheduled = len([n for n in await newsletters.list_newsletters() if n["next_due_at"]])
    return {
        **counts,
        "ingest_interval_minutes": settings.ingest_interval_minutes,
        "realtime_interval_minutes": settings.realtime_interval_minutes,
        "effective_min_cadence_minutes": settings.effective_min_cadence(
            watched_feeds=watched, scheduled_newsletters=scheduled
        ),
        "db_can_autopause": settings.db_can_autopause(
            watched_feeds=watched, scheduled_newsletters=scheduled
        ),
    }


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


@router.get("/schedule")
async def get_schedule() -> dict[str, Any]:
    """Job due-times, plus what the current cadence costs.

    `db_can_autopause` is the number worth watching: false means the polling
    cadence is holding the serverless database awake around the clock.
    """
    return await scheduler().describe()


@router.post("/schedule/{key}/run")
async def run_job(key: str) -> dict[str, Any]:
    """Force one job now, ignoring its due time."""
    try:
        return await scheduler().run_now(key)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------------------
# Newsletters
# ---------------------------------------------------------------------------


class NewsletterBody(BaseModel):
    name: str = ""
    description: str | None = None
    enabled: bool | None = None
    group_ids: list[int] | None = None
    schedule_kind: str | None = None
    interval_minutes: int | None = None
    weekday: int | None = None
    day_of_month: int | None = None
    hour_local: int | None = None
    minute_local: int | None = None
    timezone: str | None = None
    lookback_hours: int | None = None
    max_items: int | None = None
    min_items: int | None = None
    max_per_feed: int | None = None
    audience: str | None = None
    tone: str | None = None
    auto_send: bool | None = None


class IssuePatch(BaseModel):
    subject: str | None = None
    preheader: str | None = None
    intro: str | None = None
    markdown: str | None = None
    status: str | None = None


@router.get("/newsletters")
async def list_newsletters() -> list[dict[str, Any]]:
    return await newsletters.list_newsletters()


@router.post("/newsletters", status_code=201)
async def create_newsletter(body: NewsletterBody) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    # `name` is the positional argument, so it must not also arrive in the
    # splat — that is a TypeError, and it 500s rather than 422s because it is
    # raised before any validation the caller could act on.
    name = str(fields.pop("name", "")).strip()
    if not name:
        raise HTTPException(422, "A newsletter needs a name.")
    try:
        row = await newsletters.create(name, **fields)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _reschedule()
    return row


@router.get("/newsletters/{newsletter_id}")
async def get_newsletter(newsletter_id: int) -> dict[str, Any]:
    row = await newsletters.get(newsletter_id)
    if row is None:
        raise HTTPException(404, f"No newsletter {newsletter_id}")
    return row


@router.patch("/newsletters/{newsletter_id}")
async def patch_newsletter(newsletter_id: int, body: NewsletterBody) -> dict[str, Any]:
    changes = body.model_dump(exclude_none=True)
    # An empty name would blank the newsletter; the model defaults it to "" so
    # a PATCH that does not mean to rename has to be told apart from one that
    # does.
    if not str(changes.get("name", "")).strip():
        changes.pop("name", None)
    try:
        row = await newsletters.update(newsletter_id, changes)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await _reschedule()
    return row


@router.delete("/newsletters/{newsletter_id}")
async def delete_newsletter(newsletter_id: int) -> dict[str, Any]:
    if not await newsletters.delete_newsletter(newsletter_id):
        raise HTTPException(404, f"No newsletter {newsletter_id}")
    return {"deleted": True}


@router.get("/newsletters/{newsletter_id}/preview")
async def preview_newsletter(newsletter_id: int) -> dict[str, Any]:
    """Exactly what the next issue would draw from — and no model is called.

    The cheapest way to tune a newsletter: change its groups or its window and
    see precisely what changes, for free.
    """
    try:
        return await newsletters.candidates(newsletter_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/newsletters/{newsletter_id}/generate", status_code=202)
async def generate_issue(newsletter_id: int, instruction: str = "") -> dict[str, str]:
    row = await newsletters.get(newsletter_id)
    if row is None:
        raise HTTPException(404, f"No newsletter {newsletter_id}")
    run_id = await manager().enqueue(
        "newsletter",
        {"newsletter_id": newsletter_id, "instruction": instruction},
        f"Newsletter · {row['name']}",
    )
    await newsletters.attach_run(newsletter_id, run_id)
    return {"id": run_id, "run_id": run_id}


@router.get("/newsletters/{newsletter_id}/issues")
async def list_newsletter_issues(newsletter_id: int) -> list[dict[str, Any]]:
    return await newsletters.list_issues(newsletter_id)


@router.get("/issues")
async def list_issues(limit: int = Query(50, le=200)) -> list[dict[str, Any]]:
    return await newsletters.list_issues(limit=limit)


@router.get("/issues/{issue_id}")
async def get_issue(issue_id: int) -> dict[str, Any]:
    row = await newsletters.get_issue(issue_id)
    if row is None:
        raise HTTPException(404, f"No issue {issue_id}")
    return row


@router.patch("/issues/{issue_id}")
async def patch_issue(issue_id: int, body: IssuePatch) -> dict[str, Any]:
    try:
        return await newsletters.update_issue(issue_id, body.model_dump(exclude_none=True))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/issues/{issue_id}/html", response_class=HTMLResponse)
async def get_issue_html(issue_id: int) -> str:
    """The rendered email, for the preview iframe."""
    from .db import NewsletterIssue, session

    async with session() as s:
        row = await s.get(NewsletterIssue, issue_id)
    if row is None:
        raise HTTPException(404, f"No issue {issue_id}")
    return row.html or "<p>This issue has no rendered body.</p>"


# ---------------------------------------------------------------------------
# Recipients and delivery
# ---------------------------------------------------------------------------


class RecipientCreate(BaseModel):
    channel: str = Field(..., min_length=1)
    address: str = ""
    name: str = ""
    newsletter_ids: list[int] = Field(default_factory=list)


class RecipientPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    notes: str | None = None
    newsletter_ids: list[int] | None = None


@router.get("/channels")
async def list_channels() -> list[dict[str, Any]]:
    """Which channels exist and whether each is configured.

    Shaped like the dots on /api/health, and settings-only — no database, so it
    stays cheap enough for the UI to poll.
    """
    from .channels import describe_channels

    return describe_channels()


@router.get("/recipients")
async def list_recipients() -> list[dict[str, Any]]:
    return await newsletters.list_recipients()


@router.post("/recipients", status_code=201)
async def create_recipient(body: RecipientCreate) -> dict[str, Any]:
    try:
        return await newsletters.create_recipient(
            body.channel, body.address, name=body.name, newsletter_ids=body.newsletter_ids
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/recipients/{recipient_id}")
async def patch_recipient(recipient_id: int, body: RecipientPatch) -> dict[str, Any]:
    try:
        return await newsletters.update_recipient(
            recipient_id, body.model_dump(exclude_none=True)
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/recipients/{recipient_id}")
async def delete_recipient(recipient_id: int) -> dict[str, Any]:
    if not await newsletters.delete_recipient(recipient_id):
        raise HTTPException(404, f"No recipient {recipient_id}")
    return {"deleted": True}


@router.post("/issues/{issue_id}/send", status_code=202)
async def send_issue(issue_id: int) -> dict[str, str]:
    """Queue delivery. Refuses an issue that has already gone out."""
    issue = await newsletters.get_issue(issue_id)
    if issue is None:
        raise HTTPException(404, f"No issue {issue_id}")
    if issue["status"] in ("sending", "sent"):
        raise HTTPException(409, f"Issue {issue_id} is already {issue['status']}.")
    if issue["status"] == "skipped":
        raise HTTPException(409, "This issue was skipped — there is nothing to send.")

    run_id = await manager().enqueue(
        "deliver",
        {"issue_id": issue_id},
        f"Send · {issue['newsletter_name']} #{issue['number']}",
    )
    return {"id": run_id, "run_id": run_id}


@router.post("/issues/{issue_id}/retry", status_code=202)
async def retry_issue(issue_id: int) -> dict[str, str]:
    """Re-attempt only the failed deliveries. Anything already sent is left alone."""
    if await newsletters.get_issue(issue_id) is None:
        raise HTTPException(404, f"No issue {issue_id}")
    run_id = await manager().enqueue(
        "deliver", {"issue_id": issue_id, "retry": True}, f"Retry · issue {issue_id}"
    )
    return {"id": run_id, "run_id": run_id}


@router.get("/issues/{issue_id}/deliveries")
async def issue_deliveries(issue_id: int) -> dict[str, Any]:
    from .delivery import summary

    return await summary(issue_id)


@router.post("/recipients/{recipient_id}/test")
async def test_recipient(recipient_id: int, issue_id: int) -> dict[str, Any]:
    """Send one issue to one recipient, leaving the issue's own state untouched.

    What you use before trusting a channel — it proves the credentials and the
    address, and records nothing that would confuse the delivery history.
    """
    from .delivery import test_send

    try:
        return await test_send(recipient_id, issue_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------------------
# Feed discovery
# ---------------------------------------------------------------------------


class FeedDecision(BaseModel):
    url: str
    approved: bool = False
    name: str = ""
    topics: list[str] = Field(default_factory=list)
    group_ids: list[int] = Field(default_factory=list)
    realtime: bool = False


class DecideFeeds(BaseModel):
    decisions: list[FeedDecision] = Field(default_factory=list)


@router.post("/discover", status_code=202)
async def start_discovery(instruction: str = "") -> dict[str, str]:
    run_id = await manager().enqueue(
        "discover", {"instruction": instruction}, "Feed discovery sweep"
    )
    return {"id": run_id, "run_id": run_id}


@router.get("/feed-reviews")
async def list_feed_reviews(status: str = "") -> list[dict[str, Any]]:
    from . import discovery

    return await discovery.list_reviews(status or None)


@router.get("/feed-reviews/{review_id}")
async def get_feed_review(review_id: int) -> dict[str, Any]:
    from . import discovery

    row = await discovery.get(review_id)
    if row is None:
        raise HTTPException(404, f"No feed review {review_id}")
    return row


@router.post("/feed-reviews/{review_id}/decide")
async def decide_feed_review(review_id: int, body: DecideFeeds) -> dict[str, Any]:
    from . import discovery

    try:
        result = await discovery.decide(
            review_id, [d.model_dump() for d in body.decisions]
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _reschedule()
    return result


@router.post("/feed-reviews/{review_id}/cancel")
async def cancel_feed_review(review_id: int) -> dict[str, Any]:
    from . import discovery

    return {"cancelled": await discovery.cancel(review_id)}


@router.get("/pending")
async def pending_counts() -> dict[str, int]:
    """Every nav badge in one request.

    One endpoint rather than one poll per badge — the shell already polls this
    every 15 seconds, and three separate polls would wake the serverless
    database three times as often for no more information.
    """
    from . import discovery, reviews

    return {
        "source_reviews": await reviews.pending_count(),
        "feed_reviews": await discovery.pending_count(),
    }
