"""The news subsystem's pure half.

Dedup is the whole game: a feed polled every fifteen minutes returns the same
entries every time, so a bug in ``canonical_url`` or ``entry_key`` is not a
cosmetic duplicate — it is a duplicate push notification at 3am and the same
story twice in a digest. These tests exist to pin exactly that.
"""

from __future__ import annotations

import httpx
import pytest

from ppn_blogger import news
from ppn_blogger.news import (
    FetchedEntry,
    canonical_url,
    discover_feeds_in_html,
    entry_key,
    fetch_feed,
    parse_bytes,
    url_hash,
)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example Blog</title>
  <link>https://example.com/</link>
  <language>en</language>
  <item>
    <title>First &amp; best post</title>
    <link>https://example.com/first/?utm_source=rss</link>
    <guid isPermaLink="false">tag:example.com,2026:post-1</guid>
    <author>ada@example.com</author>
    <pubDate>Mon, 03 Aug 2026 09:00:00 GMT</pubDate>
    <description>&lt;p&gt;Some  &lt;b&gt;marked up&lt;/b&gt; summary.&lt;/p&gt;</description>
    <category>ai</category>
  </item>
  <item>
    <title>Second post</title>
    <link>https://example.com/second</link>
    <guid isPermaLink="true">https://example.com/second</guid>
    <pubDate>Tue, 04 Aug 2026 09:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

HTML = """
<html><head>
  <link rel="stylesheet" href="/style.css">
  <link rel="alternate" type="application/rss+xml" title="RSS" href="/feed.xml">
  <link rel="alternate" type="application/atom+xml" href="https://cdn.example.com/atom">
  <link rel="alternate" type="text/html" href="/amp">
</head><body>hi</body></html>
"""


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://Example.com/Post/", "https://example.com/Post"),
        ("https://www.example.com/post", "https://example.com/post"),
        ("https://example.com/post#section", "https://example.com/post"),
        ("https://example.com/post?utm_source=rss&utm_campaign=x", "https://example.com/post"),
        ("https://example.com/post?b=2&a=1", "https://example.com/post?a=1&b=2"),
        ("https://example.com:443/post", "https://example.com/post"),
        ("http://example.com:80/post", "http://example.com/post"),
        ("https://example.com:8443/post", "https://example.com:8443/post"),
        ("https://example.com/", "https://example.com"),
        ("example.com/post", "https://example.com/post"),
        ("  https://example.com/post  ", "https://example.com/post"),
        ("https://example.com/p?id=7&fbclid=abc", "https://example.com/p?id=7"),
        ("", ""),
    ],
)
def test_canonical_url(raw: str, expected: str) -> None:
    assert canonical_url(raw) == expected


def test_url_hash_ignores_the_spellings_that_do_not_matter() -> None:
    # The case the operator actually hits: pasting a feed twice, once from the
    # address bar and once from a share link.
    assert url_hash("https://example.com/feed/") == url_hash(
        "https://www.example.com/feed?utm_source=twitter"
    )
    assert url_hash("https://example.com/a") != url_hash("https://example.com/b")


def test_canonical_url_survives_rubbish() -> None:
    for bad in ("http://", "not a url", "https://[oops"):
        assert isinstance(canonical_url(bad), str)


# ---------------------------------------------------------------------------
# entry_key
# ---------------------------------------------------------------------------


def test_entry_key_prefers_an_opaque_guid() -> None:
    entry = {"id": "tag:example.com,2026:post-1", "guidislink": False}
    key = entry_key(entry, "https://example.com/first")
    # Stable across a URL change — the point of preferring the guid.
    assert key == entry_key(entry, "https://example.com/first-renamed")


def test_entry_key_falls_back_to_the_url_when_the_guid_is_a_link() -> None:
    """Reddit and Blogger put the article URL in <guid>/<id>.

    Using it raw would skip canonicalisation, so a feed that started appending a
    campaign parameter would republish its entire back catalogue as new.
    """
    a = {"id": "https://example.com/post?utm_source=rss", "guidislink": True}
    b = {"id": "https://www.example.com/post/", "guidislink": True}
    assert entry_key(a, "https://example.com/post?utm_source=rss") == entry_key(
        b, "https://www.example.com/post/"
    )


def test_entry_key_is_stable_with_no_guid_at_all() -> None:
    entry: dict[str, str] = {}
    assert entry_key(entry, "https://example.com/x") == entry_key(entry, "https://example.com/x/")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_bytes_extracts_what_a_digest_needs() -> None:
    title, site_url, language, entries, error = parse_bytes(RSS)
    assert not error
    assert title == "Example Blog"
    assert site_url == "https://example.com/"
    assert language == "en"
    assert len(entries) == 2

    first = entries[0]
    assert first.title == "First & best post"
    assert first.url == "https://example.com/first/?utm_source=rss"
    assert first.author == "ada@example.com"
    assert first.tags == ["ai"]
    assert first.published is not None and first.published.year == 2026
    # HTML stripped and whitespace collapsed, but not truncated at 400 chars.
    assert first.summary == "Some marked up summary."
    assert first.entry_key


def test_parse_bytes_never_raises_on_rubbish() -> None:
    for raw in (b"", b"not xml at all", b"<rss><channel><item>"):
        _, _, _, entries, _ = parse_bytes(raw)
        assert entries == []


def test_parse_bytes_keeps_entries_from_a_slightly_broken_feed() -> None:
    """feedparser recovers from most real-world breakage; bozo alone is not fatal."""
    broken = RSS.replace(b"</channel></rss>", b"")
    _, _, _, entries, _ = parse_bytes(broken)
    assert len(entries) >= 1


# ---------------------------------------------------------------------------
# fetch_feed
# ---------------------------------------------------------------------------


async def test_fetch_feed_parses_a_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "If-None-Match" not in request.headers
        return httpx.Response(200, content=RSS, headers={"ETag": '"v1"'})

    async with _client(handler) as http:
        result = await fetch_feed("https://example.com/feed", client=http)

    assert result.ok and result.status == 200
    assert result.etag == '"v1"'
    assert len(result.entries) == 2
    assert result.title == "Example Blog"


async def test_fetch_feed_sends_validators_and_honours_a_304() -> None:
    """The whole point: an unchanged feed costs one round trip and no parsing."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(304)

    async with _client(handler) as http:
        result = await fetch_feed(
            "https://example.com/feed",
            etag='"v1"',
            last_modified="Mon, 03 Aug 2026 09:00:00 GMT",
            client=http,
        )

    assert seen["if-none-match"] == '"v1"'
    assert seen["if-modified-since"] == "Mon, 03 Aug 2026 09:00:00 GMT"
    assert result.not_modified and result.ok
    assert result.entries == []
    # The validator must survive a 304 or the next poll refetches the body.
    assert result.etag == '"v1"'
    assert result.last_modified == "Mon, 03 Aug 2026 09:00:00 GMT"


async def test_fetch_feed_reports_an_http_error_rather_than_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="go away")

    async with _client(handler) as http:
        result = await fetch_feed("https://example.com/feed", etag='"keep"', client=http)

    assert not result.ok
    assert result.status == 403
    assert "403" in result.error
    # A rejected request tells us nothing new, so the old validator stands.
    assert result.etag == '"keep"'


async def test_fetch_feed_reports_a_transport_failure_rather_than_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with _client(handler) as http:
        result = await fetch_feed("https://example.com/feed", client=http)

    assert not result.ok
    assert result.status == 0  # never left the process
    assert "ConnectError" in result.error


async def test_fetch_feed_reports_a_page_that_is_not_a_feed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html><body>not a feed</body></html>")

    async with _client(handler) as http:
        result = await fetch_feed("https://example.com/", client=http)

    assert result.status == 200
    assert result.entries == []


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_feeds_in_html_resolves_relative_hrefs() -> None:
    found = discover_feeds_in_html(HTML, "https://example.com/blog/")
    assert found == ["https://example.com/feed.xml", "https://cdn.example.com/atom"]


def test_discover_feeds_in_html_ignores_non_feed_alternates() -> None:
    assert "/amp" not in "".join(discover_feeds_in_html(HTML, "https://example.com/"))


def test_discover_feeds_in_html_survives_rubbish() -> None:
    assert discover_feeds_in_html("", "https://example.com") == []
    assert discover_feeds_in_html("<<<>>>", "https://example.com") == []


def test_the_user_agent_carries_no_url() -> None:
    """A contact URL in the User-Agent is what gets us blocked.

    `name/version (+https://site)` is the polite convention and it is exactly
    what Cloudflare's managed rules refuse: measured against a real host, the
    same request was 403 with the URL and 200 without it, while a browser string
    and a missing header both passed. Reddit's rate limiter relaxed too.

    This test exists because the polite form is the one a future reader will want
    to restore.
    """
    from ppn_blogger import tools

    assert "http" not in news.USER_AGENT.lower()
    assert news.USER_AGENT.startswith("ppn-blogger")
    # One answer to "who are we" — the crew's fetching is behind the same rules.
    assert tools._USER_AGENT == news.USER_AGENT


def test_fetched_entry_defaults_are_not_shared() -> None:
    a, b = FetchedEntry(), FetchedEntry()
    a.tags.append("x")
    assert b.tags == []
