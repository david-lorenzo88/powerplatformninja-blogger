"""The news subsystem's pure half: canonicalise, fetch, parse.

No database, no agents, no settings. Everything here is a function of its
arguments, which is what makes the dedup rules testable — and dedup is the whole
game. A feed polled every fifteen minutes returns the same forty entries every
time; the only thing standing between that and forty duplicate rows (and forty
duplicate push notifications) is ``entry_key``.

Why this exists next to ``tools.read_feeds`` rather than replacing it. That tool
serves the blog crew: its output shape is described in three scout prompts and
the agents are bound to it, so it is a prompt contract, not just a function
signature. It also has no memory — it re-downloads every feed on every agent call
and silently returns ``[]`` for anything that fails. This module is the opposite:
conditional GET so an unchanged feed costs one round trip and no parsing, and
every failure comes back as a value the caller can record and show.

Like ``tools.py``, nothing here raises. A fetch that fails returns a ``FeedFetch``
carrying the reason, because a feed that 403s must be distinguishable from a feed
with nothing new — that distinction is exactly what the current code throws away.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

USER_AGENT = "ppn-blogger/0.1 (+https://powerplatformninja.com)"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_CONCURRENCY = 8

# Parameters that identify a campaign, not a document. Two links differing only
# by these are the same article, and a newsletter that shows both looks broken.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_reader",
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "referrer",
        "source",
        "at_medium",
        "at_campaign",
        "spm",
    }
)

_FEED_LINK_TYPES = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/rdf+xml",
        "application/feed+json",
    }
)

# Tried in order when a page advertises no feed of its own.
COMMON_FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/atom.xml", "/index.xml", "/feed.xml")

_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"\s+")

SUMMARY_MAX_CHARS = 2000


# ---------------------------------------------------------------------------
# Canonicalisation — the reason dedup works
# ---------------------------------------------------------------------------


def canonical_url(url: str) -> str:
    """Normalise a URL so two spellings of the same resource compare equal.

    Deliberately aggressive about trailing slashes and tracking parameters:
    ``example.com/post/?utm_source=x`` and ``example.com/post`` are one article
    everywhere this project cares about. Over-merging two genuinely different
    pages is the theoretical risk; under-merging shows the operator the same
    story twice, which is the one that actually happens.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parts = urlparse(raw)
    except ValueError:
        return raw.lower()

    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return raw.lower()

    netloc = host
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = parts.path or ""
    if path.endswith("/"):
        path = path.rstrip("/")

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS]
    query = urlencode(sorted(kept))

    # Fragment dropped entirely: it never reaches the server.
    return urlunparse((scheme, netloc, path, "", query, ""))


def url_hash(url: str) -> str:
    """A 64-char index key for a URL of any length.

    Article URLs routinely exceed the 450 characters an indexable NVARCHAR can
    hold on Azure SQL, and Text/NVARCHAR(max) cannot be indexed at all — so the
    URL is stored unindexed and looked up through this.
    """
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def entry_key(entry: Any, resolved_url: str) -> str:
    """The stable identity of one feed entry, as a 64-char hash.

    Prefers the feed's own guid, but *only* when it is an opaque id rather than a
    permalink. Reddit and Blogger put the article URL in ``<guid>``/``<id>``, and
    a raw guid skips canonicalisation — so a feed that starts appending a
    campaign parameter would republish its whole back catalogue as new. When the
    guid is a link, the canonicalised URL is both more stable and more useful.
    """
    guid = ""
    for attr in ("id", "guid"):
        value = getattr(entry, attr, None) if not isinstance(entry, dict) else entry.get(attr)
        if isinstance(value, str) and value.strip():
            guid = value.strip()
            break

    if isinstance(entry, dict):
        guid_is_link = bool(entry.get("guidislink"))
    else:
        guid_is_link = bool(getattr(entry, "guidislink", False))

    if guid and not guid_is_link:
        return hashlib.sha256(guid.encode("utf-8")).hexdigest()
    return url_hash(resolved_url)


def strip_html(value: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", value or "")).strip()


def domain_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FetchedEntry:
    """One entry, carrying what a digest needs rather than what an agent needs.

    ``tools.read_feeds`` drops guid, author, tags and language and truncates the
    summary to 400 characters, all to keep an agent's context small. A newsletter
    editor wants the real text and the real identity.
    """

    title: str = ""
    url: str = ""
    guid: str = ""
    author: str = ""
    summary: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    published: datetime | None = None
    language: str = ""
    entry_key: str = ""


@dataclass(slots=True)
class FeedFetch:
    """The outcome of one fetch. Never an exception."""

    url: str = ""
    status: int = 0  # 0 means the request never completed — timeout, DNS, TLS
    not_modified: bool = False
    etag: str = ""
    last_modified: str = ""
    title: str = ""
    site_url: str = ""
    language: str = ""
    entries: list[FetchedEntry] = field(default_factory=list)
    error: str = ""
    took_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.error and (self.not_modified or 200 <= self.status < 300)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _published_of(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            try:
                return datetime(*value[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return None


def _content_of(entry: Any) -> str:
    blocks = getattr(entry, "content", None) or []
    for block in blocks:
        value = block.get("value") if isinstance(block, dict) else getattr(block, "value", "")
        if value:
            return str(value)
    return ""


def _tags_of(entry: Any) -> list[str]:
    out: list[str] = []
    for tag in getattr(entry, "tags", None) or []:
        term = tag.get("term") if isinstance(tag, dict) else getattr(tag, "term", "")
        if term and term not in out:
            out.append(str(term)[:80])
    return out[:20]


def parse_bytes(raw: bytes) -> tuple[str, str, str, list[FetchedEntry], str]:
    """Parse feed bytes into ``(title, site_url, language, entries, error)``.

    feedparser is synchronous and CPU-bound, so callers run this in a thread.
    A malformed document is not automatically a failure: feedparser recovers from
    most real-world breakage and still yields entries, so ``bozo`` is only fatal
    when nothing came out of it.
    """
    try:
        import feedparser
    except Exception as exc:  # noqa: BLE001 - an import failure is still a value
        return "", "", "", [], f"feedparser unavailable: {exc}"

    try:
        parsed = feedparser.parse(raw)
    except Exception as exc:  # noqa: BLE001
        return "", "", "", [], f"{type(exc).__name__}: {exc}"[:400]

    meta = getattr(parsed, "feed", None) or {}
    title = str(meta.get("title", "") or "")[:300]
    site_url = str(meta.get("link", "") or "")
    language = str(meta.get("language", "") or "")[:16]

    entries: list[FetchedEntry] = []
    for entry in getattr(parsed, "entries", None) or []:
        link = str(getattr(entry, "link", "") or "").strip()
        guid = str(getattr(entry, "id", "") or "").strip()
        resolved = link or guid
        if not resolved:
            continue
        summary = strip_html(str(getattr(entry, "summary", "") or ""))
        entries.append(
            FetchedEntry(
                title=strip_html(str(getattr(entry, "title", "") or ""))[:400],
                url=resolved,
                guid=guid,
                author=str(getattr(entry, "author", "") or "")[:200],
                summary=summary[:SUMMARY_MAX_CHARS],
                content=_content_of(entry),
                tags=_tags_of(entry),
                published=_published_of(entry),
                language=language,
                entry_key=entry_key(entry, resolved),
            )
        )

    if not entries and getattr(parsed, "bozo", 0):
        exc = getattr(parsed, "bozo_exception", None)
        return title, site_url, language, [], f"unparseable feed: {exc}"[:400]

    return title, site_url, language, entries, ""


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def build_client(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml,"
                 " application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8"},
        timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        follow_redirects=True,
    )


async def fetch_feed(
    url: str,
    *,
    etag: str = "",
    last_modified: str = "",
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> FeedFetch:
    """Fetch and parse one feed, conditionally when we have a validator.

    A 304 is the point of the whole exercise: an unchanged feed costs one round
    trip, no body, and no parsing. It is what makes a fifteen-minute cadence
    reasonable in bandwidth — it stays expensive in database wakeups, which is a
    separate problem the scheduler owns.
    """
    started = datetime.now(UTC)
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    owned = client is None
    http = client or build_client(timeout)
    try:
        try:
            resp = await asyncio.wait_for(http.get(url, headers=headers), timeout=timeout + 5)
        except TimeoutError:
            return FeedFetch(url=url, error="timed out", took_ms=_ms_since(started))
        except Exception as exc:  # noqa: BLE001 - a dead host is a value, not a crash
            return FeedFetch(
                url=url, error=f"{type(exc).__name__}: {exc}"[:400], took_ms=_ms_since(started)
            )

        new_etag = resp.headers.get("ETag", "") or etag
        new_last_modified = resp.headers.get("Last-Modified", "") or last_modified

        if resp.status_code == 304:
            return FeedFetch(
                url=url,
                status=304,
                not_modified=True,
                etag=new_etag,
                last_modified=new_last_modified,
                took_ms=_ms_since(started),
            )
        if resp.status_code >= 400:
            return FeedFetch(
                url=url,
                status=resp.status_code,
                etag=etag,
                last_modified=last_modified,
                error=f"HTTP {resp.status_code}",
                took_ms=_ms_since(started),
            )

        title, site_url, language, entries, error = await asyncio.to_thread(
            parse_bytes, resp.content
        )
        return FeedFetch(
            url=url,
            status=resp.status_code,
            etag=new_etag,
            last_modified=new_last_modified,
            title=title,
            site_url=site_url or str(resp.url),
            language=language,
            entries=entries,
            error=error,
            took_ms=_ms_since(started),
        )
    finally:
        if owned:
            await http.aclose()


async def fetch_many(
    specs: list[tuple[str, str, str]],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[FeedFetch]:
    """Fetch ``[(url, etag, last_modified), ...]`` over one shared connection pool.

    The timeout is applied per feed rather than to the sweep, so one hung host
    delays its own result and nothing else.
    """
    if not specs:
        return []
    limit = asyncio.Semaphore(max(1, concurrency))
    async with build_client(timeout) as http:

        async def one(spec: tuple[str, str, str]) -> FeedFetch:
            url, etag, last_modified = spec
            async with limit:
                return await fetch_feed(
                    url, etag=etag, last_modified=last_modified, client=http, timeout=timeout
                )

        return list(await asyncio.gather(*(one(s) for s in specs)))


async def probe(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> FeedFetch:
    """Fetch a feed unconditionally — what the 'validate this URL' button calls."""
    return await fetch_feed(url, timeout=timeout)


def describe_failure(result: FeedFetch) -> str:
    """Why a probe did not produce a feed, in a sentence worth showing someone.

    Pasting an ordinary web page is the single most likely mistake, and
    feedparser's own account of it is ``<unknown>:2:0: syntax error`` — accurate,
    and no help at all in deciding what to do next. The technical detail is kept,
    in brackets, after the part that tells the operator where to look.
    """
    if result.status == 0:
        return f"Could not reach that address ({result.error or 'no response'})."
    if result.status >= 400:
        return f"That address answered HTTP {result.status}."
    if not result.entries:
        detail = f" ({result.error})" if result.error else ""
        return (
            "That address loaded, but there is no feed there. "
            f"Try the site's /feed or /rss address instead.{detail}"
        )
    return result.error


# ---------------------------------------------------------------------------
# Discovery from a page
# ---------------------------------------------------------------------------


def discover_feeds_in_html(html: str, base_url: str) -> list[str]:
    """Feed URLs advertised by a page, in document order, deduped.

    This is the everyday 'add a feed' path and it involves no model at all: paste
    a site URL, read its own ``<link rel="alternate">``, show a preview. A model
    can suggest a feed but it cannot know whether one exists.
    """
    try:
        from bs4 import BeautifulSoup
    except Exception:  # noqa: BLE001
        return []

    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001
        return []

    out: list[str] = []
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        if isinstance(rel, str):
            rel = [rel]
        if not any(str(r).lower() == "alternate" for r in rel):
            continue
        if str(link.get("type", "")).lower().strip() not in _FEED_LINK_TYPES:
            continue
        href = str(link.get("href", "") or "").strip()
        if not href:
            continue
        resolved = urljoin(base_url, href)
        if resolved not in out:
            out.append(resolved)
    return out


async def discover_feeds(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[str]:
    """Candidate feed URLs for a site: what it advertises, then the usual paths.

    Returns URLs only — every candidate still has to be fetched and parsed before
    anyone is offered it, which is what keeps a suggested feed from being a guess.
    """
    async with build_client(timeout) as http:
        try:
            resp = await http.get(url)
            html = resp.text if resp.status_code < 400 else ""
            base = str(resp.url)
        except Exception:  # noqa: BLE001
            html, base = "", url

    found = discover_feeds_in_html(html, base) if html else []
    for path in COMMON_FEED_PATHS:
        candidate = urljoin(base, path)
        if candidate not in found:
            found.append(candidate)
    return found


def _ms_since(started: datetime) -> int:
    return int((datetime.now(UTC) - started).total_seconds() * 1000)
