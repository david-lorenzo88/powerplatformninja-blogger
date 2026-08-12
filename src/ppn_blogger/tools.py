"""Tools the agents can call.

Everything here is a plain async function wrapped with ``@tool``; Agent
Framework builds the JSON schema from the signature and the ``Annotated``
descriptions, and invokes them automatically during a run.

Design notes
------------
* No tool ever raises. Failures come back as a readable string so the agent can
  reason about them instead of killing the workflow.
* Network results are cached in-process for the lifetime of a run so the same
  URL is not fetched five times by five agents.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from agent_framework import tool

from . import news
from .settings import get_settings
from .util import log_tool

# Shared with the news subsystem so there is one answer to "who are we". The
# contact URL that used to be here is what Cloudflare was blocking — see the
# note on news.USER_AGENT. This path is worse hit than the news one: read_feeds
# turns a 403 into an empty list, so the Feed Scout has been quietly seeing
# nothing from any Cloudflare-fronted MVP blog.
_USER_AGENT = news.USER_AGENT
_TIMEOUT = httpx.Timeout(25.0, connect=10.0)
_page_cache: dict[str, str] = {}
_head_cache: dict[str, dict[str, Any]] = {}


def _client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT,
        follow_redirects=True,
        **kwargs,
    )


def domain_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def classify_domain(url: str) -> tuple[str, int]:
    """Return (tier_id, score) for a URL based on config/sources.yaml."""
    settings = get_settings()
    host = domain_of(url)
    path = url.lower()
    if not host:
        return "unknown", 1
    if any(host == b or host.endswith("." + b) for b in settings.blocked_domains):
        return "blocked", 0
    best: tuple[str, int] = ("unknown", 1)
    for tier_id, tier in settings.trust_tiers.items():
        for pattern in tier.get("domains", []):
            pattern = pattern.lower()
            if "/" in pattern:  # e.g. github.com/microsoft
                if pattern in path:
                    return tier_id, int(tier.get("score", 3))
                continue
            if host == pattern or host.endswith("." + pattern):
                score = int(tier.get("score", 3))
                if score > best[1]:
                    best = (tier_id, score)
    return best


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------


async def _tavily(query: str, max_results: int, days: int | None) -> list[dict[str, Any]]:
    settings = get_settings()
    payload: dict[str, Any] = {
        "api_key": settings.search.tavily_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": False,
    }
    if days:
        payload["topic"] = "news"
        payload["days"] = days
    async with _client() as http:
        resp = await http.post("https://api.tavily.com/search", json=payload)
        resp.raise_for_status()
        data = resp.json()
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("content") or "")[:600],
            "published": r.get("published_date"),
        }
        for r in data.get("results", [])
    ]


async def _brave(query: str, max_results: int, days: int | None) -> list[dict[str, Any]]:
    settings = get_settings()
    params: dict[str, Any] = {"q": query, "count": min(max_results, 20)}
    if days:
        params["freshness"] = f"pd{days}" if days <= 31 else "pm"
    async with _client(headers={"X-Subscription-Token": settings.search.brave_key,
                                "Accept": "application/json",
                                "User-Agent": _USER_AGENT}) as http:
        resp = await http.get("https://api.search.brave.com/res/v1/web/search", params=params)
        resp.raise_for_status()
        data = resp.json()
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("description") or "")[:600],
            "published": (r.get("page_age") or None),
        }
        for r in data.get("web", {}).get("results", [])
    ]


@tool(name="web_search")
async def web_search(
    query: Annotated[str, "Search query. Be specific; include product names and version words."],
    max_results: Annotated[int, "How many results to return (1-15)."] = 8,
    recent_days: Annotated[
        int, "Restrict to items published in the last N days. Use 0 for no restriction."
    ] = 0,
) -> str:
    """Search the public web for pages relevant to a query.

    Returns a JSON array of {title, url, snippet, published, domain, trust_tier}.
    Use this to discover material; use fetch_page to actually read a result.
    """
    log_tool("web_search", query)
    settings = get_settings()
    if not settings.search.is_configured:
        return json.dumps(
            {
                "error": "no_search_provider",
                "message": (
                    f"SEARCH_PROVIDER={settings.search.provider} has no API key configured. "
                    "Fall back to read_feeds and search_microsoft_learn."
                ),
            }
        )
    days = recent_days or None
    try:
        if settings.search.provider == "brave":
            results = await _brave(query, max_results, days)
        else:
            results = await _tavily(query, max_results, days)
    except Exception as exc:  # noqa: BLE001 - surfaced to the agent, never fatal
        return json.dumps({"error": type(exc).__name__, "message": str(exc)[:400]})

    for item in results:
        tier, score = classify_domain(item["url"])
        item["domain"] = domain_of(item["url"])
        item["trust_tier"] = tier
        item["trust_score"] = score
    results = [r for r in results if r["trust_tier"] != "blocked"]
    return json.dumps(results[:max_results], ensure_ascii=False)


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------


@tool(name="fetch_page")
async def fetch_page(
    url: Annotated[str, "Absolute http(s) URL to read."],
    max_chars: Annotated[int, "Truncate the extracted text to this many characters."] = 12000,
) -> str:
    """Fetch a web page and return its main content as plain text/markdown.

    Always read a page before citing it. If this returns an error, the URL is
    not usable as a citation.
    """
    log_tool("fetch_page", url)
    if not url.lower().startswith(("http://", "https://")):
        return f"ERROR: not an absolute URL: {url}"
    tier, _ = classify_domain(url)
    if tier == "blocked":
        return f"ERROR: {domain_of(url)} is on the blocked_domains list in config/sources.yaml."
    cache_key = f"{url}:{max_chars}"
    if cache_key in _page_cache:
        return _page_cache[cache_key]
    try:
        async with _client() as http:
            resp = await http.get(url)
        if resp.status_code >= 400:
            return f"ERROR: HTTP {resp.status_code} for {url}"
        html = resp.text
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__} fetching {url}: {str(exc)[:200]}"

    text = _extract_text(html)
    published = _guess_published(html)
    header = f"# Source: {url}\ndomain: {domain_of(url)}\ntrust_tier: {tier}\n"
    if published:
        header += f"published: {published}\n"
    out = header + "\n" + text[:max_chars]
    _page_cache[cache_key] = out
    return out


def _extract_text(html: str) -> str:
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html, include_links=True, include_tables=True, favor_recall=True
        )
        if extracted:
            return extracted
    except Exception:  # noqa: BLE001
        pass
    # Fallback: crude tag strip
    text = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_DATE_META = re.compile(
    r'(?is)<meta[^>]+(?:property|name)=["\'](?:article:published_time|date|pubdate|'
    r'og:published_time|citation_publication_date)["\'][^>]+content=["\']([^"\']+)'
)


def _guess_published(html: str) -> str | None:
    match = _DATE_META.search(html)
    if match:
        return match.group(1)[:10]
    match = re.search(r'(?is)<time[^>]+datetime=["\']([^"\']+)', html)
    return match.group(1)[:10] if match else None


@tool(name="check_url_reachable")
async def check_url_reachable(
    urls: Annotated[list[str], "List of absolute URLs to verify."],
) -> str:
    """Verify that URLs actually resolve. Use this to catch fabricated citations.

    Returns JSON: {url: {ok, status, final_url, trust_tier, trust_score}}.
    A url with ok=false must never appear in a published post.
    """
    log_tool("check_url_reachable", f"{len(urls)} urls")

    async def probe(url: str) -> tuple[str, dict[str, Any]]:
        if url in _head_cache:
            return url, _head_cache[url]
        tier, score = classify_domain(url)
        record: dict[str, Any] = {"ok": False, "status": None, "final_url": url,
                                  "trust_tier": tier, "trust_score": score}
        try:
            async with _client() as http:
                resp = await http.get(url)
            record["status"] = resp.status_code
            record["final_url"] = str(resp.url)
            record["ok"] = resp.status_code < 400
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        _head_cache[url] = record
        return url, record

    pairs = await asyncio.gather(*(probe(u) for u in urls[:30]))
    return json.dumps(dict(pairs), ensure_ascii=False)


# ---------------------------------------------------------------------------
# RSS / Atom feeds
# ---------------------------------------------------------------------------


@tool(name="read_feeds")
async def read_feeds(
    max_age_days: Annotated[int, "Only return entries published within this many days."] = 30,
    max_items_per_feed: Annotated[int, "Cap entries taken from each feed."] = 8,
    only_tier: Annotated[
        str, "Filter to one trust tier ('official', 'community_trusted', ...) or '' for all."
    ] = "",
) -> str:
    """Read the curated RSS/Atom feeds from config/sources.yaml.

    This is the highest-signal source for 'what shipped recently'. Returns JSON:
    [{feed, tier, title, url, published, summary}].
    """
    log_tool("read_feeds", f"tier={only_tier or 'all'} max_age={max_age_days}d")
    settings = get_settings()
    feeds = [f for f in settings.feeds if not only_tier or f.get("tier") == only_tier]
    if not feeds:
        return json.dumps({"error": "no_feeds", "message": "No feeds match that tier."})

    cutoff = datetime.now(UTC) - timedelta(days=max(max_age_days, 1))

    async def pull(feed: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            async with _client() as http:
                resp = await http.get(feed["url"])
            if resp.status_code >= 400:
                return []
            raw = resp.content
        except Exception:  # noqa: BLE001
            return []
        try:
            import feedparser

            parsed = await asyncio.to_thread(feedparser.parse, raw)
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        for entry in parsed.entries[: max_items_per_feed * 3]:
            published = None
            for key in ("published_parsed", "updated_parsed"):
                if getattr(entry, key, None):
                    published = datetime(*getattr(entry, key)[:6], tzinfo=UTC)
                    break
            if published and published < cutoff:
                continue
            summary = re.sub(r"(?s)<[^>]+>", " ", getattr(entry, "summary", "") or "")
            out.append(
                {
                    "feed": feed.get("name", feed["url"]),
                    "tier": feed.get("tier", "unknown"),
                    "title": getattr(entry, "title", "").strip(),
                    "url": getattr(entry, "link", ""),
                    "published": published.date().isoformat() if published else None,
                    "summary": re.sub(r"\s+", " ", summary).strip()[:400],
                }
            )
            if len(out) >= max_items_per_feed:
                break
        return out

    batches = await asyncio.gather(*(pull(f) for f in feeds))
    items = [item for batch in batches for item in batch]
    items.sort(key=lambda i: i.get("published") or "", reverse=True)
    return json.dumps(items, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Microsoft Learn
# ---------------------------------------------------------------------------


@tool(name="search_microsoft_learn")
async def search_microsoft_learn(
    query: Annotated[str, "What to look for in official Microsoft documentation."],
    max_results: Annotated[int, "How many docs to return (1-15)."] = 8,
) -> str:
    """Search learn.microsoft.com — the authoritative source for behaviour and limits.

    Prefer this over web_search for anything about documented behaviour,
    licensing, service limits, API contracts or GA/preview status.
    Returns JSON: [{title, url, description, last_updated}].
    """
    log_tool("search_microsoft_learn", query)
    params = {
        "search": query,
        "locale": "en-us",
        "$top": str(max(1, min(max_results, 15))),
        "facet": "category",
        "expandScope": "true",
    }
    try:
        async with _client() as http:
            resp = await http.get("https://learn.microsoft.com/api/search", params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": type(exc).__name__, "message": str(exc)[:300]})

    results = []
    for item in data.get("results", [])[:max_results]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": (item.get("description") or "")[:400],
                "last_updated": (item.get("lastUpdatedDate") or "")[:10] or None,
                "trust_tier": "official",
            }
        )
    return json.dumps(results, ensure_ascii=False)


# ---------------------------------------------------------------------------
# The blog itself (WordPress REST, read-only)
# ---------------------------------------------------------------------------


@tool(name="search_existing_posts")
async def search_existing_posts(
    query: Annotated[str, "Keywords to look for among posts already on the blog, draft or live."],
    max_results: Annotated[int, "How many posts to return."] = 10,
) -> str:
    """Search posts on powerplatformninja.com, including unpublished drafts.

    Use this to avoid proposing a duplicate topic and to find internal linking
    opportunities. Returns JSON: [{id, title, url, date, excerpt}].

    Drafts are included deliberately. Every post this crew produces lands as a
    WordPress *draft* and stays one until a human presses Publish, so a
    publish-only search made the crew blind to its own output — it once proposed
    and wrote the same subject twice in two days. A draft on the blog is a topic
    already taken.
    """
    log_tool("search_existing_posts", query)
    settings = get_settings()
    if not settings.wordpress.url:
        return json.dumps({"error": "no_wp_url", "message": "WP_URL is not configured."})
    # Listing drafts needs authentication. Without credentials WordPress rejects
    # the whole request rather than quietly returning the published subset, so
    # fall back to publish-only instead of failing the tool call.
    headers = {}
    if settings.wordpress.is_configured:
        token = base64.b64encode(
            f"{settings.wordpress.username}:{settings.wordpress.app_password.replace(' ', '')}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {token}"
    params = {
        "search": query,
        "per_page": str(max(1, min(max_results, 20))),
        "status": "publish,draft" if headers else "publish",
        "_fields": "id,link,date,title,excerpt,status",
        "orderby": "relevance",
    }
    try:
        async with _client(verify=settings.wordpress.verify_tls) as http:
            resp = await http.get(
                f"{settings.wordpress.api_base}/posts", params=params, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": type(exc).__name__, "message": str(exc)[:300]})

    out = []
    for post in data:
        out.append(
            {
                "id": post.get("id"),
                "title": re.sub(r"(?s)<[^>]+>", "", (post.get("title") or {}).get("rendered", "")),
                "url": post.get("link", ""),
                # So the agent can tell "already published" from "already drafted,
                # not yet live". Both mean the topic is taken.
                "status": post.get("status", ""),
                "date": (post.get("date") or "")[:10],
                "excerpt": re.sub(
                    r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", (post.get("excerpt") or {}).get("rendered", ""))
                ).strip()[:300],
            }
        )
    return json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


@tool(name="assess_source_trust")
async def assess_source_trust(
    urls: Annotated[list[str], "URLs to classify against the blog's trust policy."],
) -> str:
    """Classify URLs against the trust tiers in config/sources.yaml.

    Returns JSON: [{url, domain, tier, score, label}]. Tier 'blocked' means the
    source must be removed. Tier 'unknown' means it needs corroboration.
    """
    log_tool("assess_source_trust", f"{len(urls)} urls")
    settings = get_settings()
    tiers = settings.trust_tiers
    out = []
    for url in urls[:50]:
        tier, score = classify_domain(url)
        out.append(
            {
                "url": url,
                "domain": domain_of(url),
                "tier": tier,
                "score": score,
                "label": tiers.get(tier, {}).get("label", "Unrecognised source — corroborate before citing"),
            }
        )
    return json.dumps(out, ensure_ascii=False)


@tool(name="today")
async def today_tool() -> str:
    """Return today's date in ISO format. Use it whenever you date a claim."""
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Tool bundles per agent
# ---------------------------------------------------------------------------

SCOUT_NEWS_TOOLS = [web_search, fetch_page, search_existing_posts, today_tool]
SCOUT_FEED_TOOLS = [read_feeds, fetch_page, search_existing_posts, today_tool]
SCOUT_DOCS_TOOLS = [search_microsoft_learn, fetch_page, search_existing_posts, today_tool]
RESEARCHER_TOOLS = [
    search_microsoft_learn,
    web_search,
    fetch_page,
    read_feeds,
    search_existing_posts,
    assess_source_trust,
    today_tool,
]
SOURCE_CHECKER_TOOLS = [check_url_reachable, fetch_page, assess_source_trust, search_microsoft_learn, today_tool]
WRITER_TOOLS = [search_existing_posts, today_tool]
