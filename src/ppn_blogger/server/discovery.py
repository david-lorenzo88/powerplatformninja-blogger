"""Finding new feeds, and proving they exist before anyone is asked about them.

The important property here is not the sweep — it is what happens to its output.
A model suggests places to look; **every URL it names is fetched and parsed
before the operator sees it**, and anything that does not resolve to a real feed
with entries is discarded without comment. So the review screen shows feeds that
demonstrably exist, never a list of plausible guesses.

That is ``sources.py``'s rule carried over intact: *the review is code, never
judgement*. It matters more here than it did there, because approving a source
review only affects what the crew is allowed to cite, while approving a feed
puts a URL into a poller that will call it every six hours forever.

The approval flow mirrors ``reviews.py`` deliberately — same shape, same
crash-safe ordering: **create the feeds first, then mark the review decided.** A
crash between the two leaves a review that can be approved again idempotently,
where the reverse would strand approved feeds in a review that was already
closed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import desc, func, select

from .. import news
from ..settings import get_settings
from .db import DeclinedFeed, Feed, FeedDiscoveryReview, as_utc, session, utcnow

logger = logging.getLogger("ppn.server.discovery")

PENDING, APPROVED, CANCELLED = "pending", "approved", "cancelled"

# A feed with nothing recent is a feed that will look broken the moment it is
# added, so it is not offered at all.
MIN_ENTRIES = 1


async def sweep(instruction: str = "", *, on_event: Any = None) -> dict[str, Any]:
    """Ask for candidate sources, verify every one, and file a review.

    Returns a thin result — counts and a review id. The candidate list lives in
    the row, exactly as an exploration sweep does, because that is what the
    operator acts on and a restart must not throw it away.
    """
    from ..models import FeedSuggestionSet

    settings = get_settings()
    suggested = await _ask(instruction, settings, on_event=on_event)
    logger.info("scout suggested %d source(s)", len(suggested.suggestions))

    known, declined = await _already_seen()
    candidates = await _verify(suggested.suggestions, known=known, declined=declined)
    logger.info(
        "%d of %d suggestion(s) resolved to a real feed", len(candidates), len(suggested.suggestions)
    )

    if not isinstance(suggested, FeedSuggestionSet):  # pragma: no cover - defensive
        raise TypeError("discovery returned an unexpected shape")

    review_id = await create(None, instruction, candidates, notes=suggested.notes)
    return {
        "awaiting_feed_approval": True,
        "review_id": review_id,
        "suggested": len(suggested.suggestions),
        "candidate_count": len(candidates),
    }


async def _ask(instruction: str, settings: Any, *, on_event: Any = None) -> Any:
    from agent_framework import ChatMessage, Role

    from .. import agents as A
    from ..clients import default_clients

    clients = default_clients()
    agent = A.build_feed_discovery_scout(settings, clients)
    prompt = (
        instruction.strip()
        or "Find sources worth following for the topics above. Return feed URLs where you can."
    )
    response = await agent.run([ChatMessage(role=Role.USER, text=prompt)])

    from ..models import FeedSuggestionSet
    from ..util import parse_model

    return parse_model(response, FeedSuggestionSet)


async def _already_seen() -> tuple[set[str], set[str]]:
    """Hashes of feeds we follow, and of ones the operator has turned down."""
    async with session() as s:
        known = set((await s.execute(select(Feed.url_hash))).scalars())
        declined = set((await s.execute(select(DeclinedFeed.url_hash))).scalars())
    return known, declined


async def _verify(
    suggestions: list[Any], *, known: set[str], declined: set[str]
) -> list[dict[str, Any]]:
    """Fetch every suggestion and keep only what is genuinely a feed.

    This is the gate. A suggestion that 404s, that is an HTML page with no feed
    behind it, or that parses to nothing, never reaches the operator — so
    "approve" cannot mean "add a URL nobody checked".

    Where a suggestion is a site rather than a feed, its own
    ``<link rel="alternate">`` is followed, which is the same code path the
    paste-a-URL button uses.
    """
    settings = get_settings().news
    timeout = float(settings.feed_timeout_seconds)
    limit = asyncio.Semaphore(settings.feed_concurrency)

    async def one(suggestion: Any) -> dict[str, Any] | None:
        async with limit:
            found, url = await _resolve(str(getattr(suggestion, "url", "")), timeout)
        if found is None:
            logger.info("discarding %s — no feed there", getattr(suggestion, "url", "?"))
            return None

        digest = news.url_hash(url)
        if digest in known:
            return None  # already followed
        if digest in declined:
            return None  # turned down before; never offered twice

        newest = max((e.published for e in found.entries if e.published), default=None)
        return {
            "url": url,
            "name": (getattr(suggestion, "name", "") or found.title or news.domain_of(url))[:200],
            "title": found.title,
            "site_url": found.site_url,
            "domain": news.domain_of(url),
            "entry_count": len(found.entries),
            "newest": newest.isoformat() if newest else None,
            "topics": list(getattr(suggestion, "topics", []) or []),
            "reason": getattr(suggestion, "reason", ""),
            "sample_titles": [e.title for e in found.entries[:3] if e.title],
            "suggested_from": str(getattr(suggestion, "url", "")),
        }

    results = await asyncio.gather(*(one(s) for s in suggestions), return_exceptions=True)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        if isinstance(result, BaseException) or result is None:
            continue
        digest = news.url_hash(result["url"])
        if digest in seen:
            continue
        seen.add(digest)
        out.append(result)
    return out


async def _resolve(raw: str, timeout: float) -> tuple[news.FeedFetch | None, str]:
    """A feed at this URL, or behind it. Returns (fetch, resolved_url)."""
    if not raw.strip():
        return None, ""
    probe = await news.probe(raw, timeout=timeout)
    if len(probe.entries) >= MIN_ENTRIES:
        return probe, raw
    for candidate in await news.discover_feeds(raw, timeout=timeout):
        found = await news.probe(candidate, timeout=timeout)
        if len(found.entries) >= MIN_ENTRIES:
            return found, candidate
    return None, raw


# ---------------------------------------------------------------------------
# The review
# ---------------------------------------------------------------------------


async def create(
    run_id: str | None, instruction: str, candidates: list[dict[str, Any]], *, notes: str = ""
) -> int:
    from datetime import date

    async with session() as s:
        row = FeedDiscoveryReview(
            run_id=run_id,
            status=PENDING,
            instruction=(instruction or notes)[:4000],
            generated_on=date.today().isoformat(),
            candidates=candidates,
        )
        s.add(row)
        await s.commit()
        logger.info("feed review %d created with %d candidate(s)", row.id, len(candidates))
        return row.id


async def get(review_id: int) -> dict[str, Any] | None:
    async with session() as s:
        row = await s.get(FeedDiscoveryReview, review_id)
    return _to_dict(row, full=True) if row is not None else None


async def list_reviews(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    async with session() as s:
        stmt = (
            select(FeedDiscoveryReview)
            .order_by(desc(FeedDiscoveryReview.created_at))
            .limit(limit)
        )
        if status:
            stmt = stmt.where(FeedDiscoveryReview.status == status)
        return [_to_dict(r) for r in (await s.execute(stmt)).scalars()]


async def pending_count() -> int:
    async with session() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(FeedDiscoveryReview)
            .where(FeedDiscoveryReview.status == PENDING)
        )
    return int(count or 0)


async def decide(review_id: int, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the verdict: create the approved feeds, remember the refused ones.

    Ordering follows ``reviews.decide`` exactly — **feeds first, then the row.**
    A crash in between leaves a review that can be approved again idempotently
    (creating a feed is already deduped by url_hash), where the reverse would
    strand approved feeds in a review that had already been closed.

    Raises ``KeyError`` for an unknown review and ``ValueError`` for one that has
    already been decided, or for a URL that was not in it.
    """
    from .news_store import create_feed

    async with session() as s:
        row = await s.get(FeedDiscoveryReview, review_id)
        if row is None:
            raise KeyError(f"No feed review {review_id}")
        if row.status != PENDING:
            raise ValueError(f"Feed review {review_id} was already {row.status}.")
        offered = {str(c.get("url", "")) for c in (row.candidates or [])}
        by_url = {str(c.get("url", "")): c for c in (row.candidates or [])}

    unknown = sorted({str(d.get("url", "")) for d in decisions} - offered)
    if unknown:
        raise ValueError(f"Not in this review: {', '.join(unknown)}")

    created: list[int] = []
    refused: list[str] = []
    for decision in decisions:
        url = str(decision.get("url", ""))
        candidate = by_url.get(url, {})
        if decision.get("approved"):
            try:
                feed = await create_feed(
                    url,
                    name=str(decision.get("name") or candidate.get("name", "")),
                    title=str(candidate.get("title", "")),
                    site_url=str(candidate.get("site_url", "")),
                    topics=list(decision.get("topics") or candidate.get("topics") or []),
                    realtime=bool(decision.get("realtime")),
                    group_ids=list(decision.get("group_ids") or []),
                    origin="discovered",
                )
                created.append(int(feed["id"]))
            except ValueError:
                # Added between the sweep and the verdict. Not an error.
                logger.info("already following %s — skipping", url)
        else:
            refused.append(url)

    if refused:
        await _decline(refused)

    async with session() as s:
        row = await s.get(FeedDiscoveryReview, review_id)
        row.status = APPROVED
        row.decisions = decisions
        row.created_feed_ids = created
        row.approved_count = len(created)
        row.declined_urls = refused
        row.decided_at = utcnow()
        await s.commit()

    logger.info(
        "feed review %d: %d added, %d declined", review_id, len(created), len(refused)
    )
    return {
        "review_id": review_id,
        "added": len(created),
        "declined": len(refused),
        "feed_ids": created,
    }


async def _decline(urls: list[str]) -> None:
    """Remember a refusal so a later sweep never offers the same URL again."""
    async with session() as s:
        for url in urls:
            digest = news.url_hash(url)
            existing = (
                await s.execute(select(DeclinedFeed).where(DeclinedFeed.url_hash == digest))
            ).scalar_one_or_none()
            if existing is None:
                s.add(DeclinedFeed(url_hash=digest, url=url))
        await s.commit()


async def cancel(review_id: int) -> bool:
    async with session() as s:
        row = await s.get(FeedDiscoveryReview, review_id)
        if row is None or row.status != PENDING:
            return False
        row.status = CANCELLED
        row.decided_at = utcnow()
        await s.commit()
    return True


async def list_declined() -> list[dict[str, Any]]:
    async with session() as s:
        rows = list((await s.execute(select(DeclinedFeed))).scalars())
    return [
        {"id": r.url_hash[:12], "url": r.url, "declined_at": _iso(r.created_at)} for r in rows
    ]


def _iso(value: Any) -> str | None:
    aware = as_utc(value)
    return aware.isoformat() if aware else None


def _to_dict(row: FeedDiscoveryReview, *, full: bool = False) -> dict[str, Any]:
    candidates = row.candidates or []
    out: dict[str, Any] = {
        "id": row.id,
        "run_id": row.run_id,
        "status": row.status,
        "instruction": row.instruction,
        "generated_on": row.generated_on,
        "candidate_count": len(candidates),
        "approved_count": row.approved_count,
        "created_feed_ids": row.created_feed_ids or [],
        "created_at": _iso(row.created_at),
        "decided_at": _iso(row.decided_at),
    }
    if full:
        out["candidates"] = candidates
        out["decisions"] = row.decisions or []
        out["declined_urls"] = row.declined_urls or []
    return out
