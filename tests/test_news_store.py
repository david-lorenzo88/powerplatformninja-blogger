"""Feed registry and ingestion against a real database.

The assertion that matters most here is that polling a feed twice files its
articles once. A feed returns the same forty entries on every poll; without the
unique (feed_id, entry_key) index and the select-then-write upsert above it, a
fifteen-minute cadence would produce ninety-six copies of every article a day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ppn_blogger import news


@pytest.fixture
async def store(database_url):
    """A migrated, empty database — SQLite locally, SQL Server in CI."""
    from ppn_blogger.server import db

    await db.init_db()
    yield


def _entry(url: str, *, key: str = "", title: str = "A post", published=None) -> news.FetchedEntry:
    return news.FetchedEntry(
        title=title,
        url=url,
        summary="why it matters",
        published=published or datetime(2026, 8, 3, tzinfo=UTC),
        entry_key=key or news.url_hash(url),
    )


def _fetch(entries, **kw) -> news.FeedFetch:
    return news.FeedFetch(
        status=kw.pop("status", 200),
        title=kw.pop("title", "Example Blog"),
        site_url="https://example.com/",
        entries=entries,
        **kw,
    )


def _canned(monkeypatch, results, captured=None):
    """Replace the network with a fixed list of outcomes, one per feed."""

    async def fake_fetch_many(specs, **kw):
        if captured is not None:
            captured.append(list(specs))
        return list(results)

    monkeypatch.setattr(news, "fetch_many", fake_fetch_many)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


async def test_create_feed_rejects_the_same_feed_spelled_differently(store) -> None:
    from ppn_blogger.server import news_store

    await news_store.create_feed("https://example.com/feed/", name="Example")
    with pytest.raises(ValueError, match="Already following"):
        await news_store.create_feed("https://www.example.com/feed?utm_source=twitter")

    assert len(await news_store.list_feeds()) == 1


async def test_feed_groups_membership_round_trips(store) -> None:
    from ppn_blogger.server import news_store

    a = await news_store.create_feed("https://a.example/feed", name="A")
    b = await news_store.create_feed("https://b.example/feed", name="B")
    group = await news_store.create_group("AI research", description="papers")

    await news_store.set_group_feeds(group["id"], [a["id"], b["id"]])
    detail = await news_store.get_group(group["id"])
    assert detail is not None and detail["feed_count"] == 2
    assert detail["feed_ids"] == [a["id"], b["id"]]

    in_group = await news_store.list_feeds(group_id=group["id"])
    assert {f["id"] for f in in_group} == {a["id"], b["id"]}

    # Replacing membership wholesale is one call, not a diff.
    await news_store.set_group_feeds(group["id"], [b["id"]])
    detail = await news_store.get_group(group["id"])
    assert detail is not None and detail["feed_ids"] == [b["id"]]


async def test_deleting_a_feed_keeps_its_articles_by_default(store, monkeypatch) -> None:
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example")
    _canned(monkeypatch, [_fetch([_entry("https://example.com/one")])])
    await ingest.ingest(feed_ids=[feed["id"]])

    await news_store.delete_feed(feed["id"])
    assert len(await news_store.list_articles()) == 1  # a cited article must still resolve

    await news_store.delete_feed(feed["id"], purge=True)
    assert await news_store.list_articles() == []


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


async def test_polling_twice_files_articles_once(store, monkeypatch) -> None:
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example")
    entries = [_entry("https://example.com/one"), _entry("https://example.com/two")]

    _canned(monkeypatch, [_fetch(entries)])
    first = await ingest.ingest(feed_ids=[feed["id"]])
    assert first["new_articles"] == 2

    # The same feed, unchanged, returning the same entries.
    _canned(monkeypatch, [_fetch(entries)])
    second = await ingest.ingest(feed_ids=[feed["id"]])
    assert second["new_articles"] == 0
    assert len(await news_store.list_articles()) == 2


async def test_a_later_poll_can_add_to_a_feed_that_already_has_entries(store, monkeypatch) -> None:
    """Regression: comparing a stored timestamp to utcnow() used to raise.

    SQLite returns naive datetimes from a DateTime(timezone=True) column, so
    once `last_entry_at` was set, the next poll that brought a newer entry blew
    up on an offset-naive/offset-aware comparison. Azure SQL would not have —
    which is exactly why this needs a test.
    """
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example")
    _canned(monkeypatch, [_fetch([_entry("https://example.com/one")])])
    await ingest.ingest(feed_ids=[feed["id"]])

    _canned(
        monkeypatch,
        [
            _fetch(
                [
                    _entry("https://example.com/one"),
                    _entry("https://example.com/two", published=datetime(2026, 8, 9, tzinfo=UTC)),
                ]
            )
        ],
    )
    result = await ingest.ingest(feed_ids=[feed["id"]])

    assert result["new_articles"] == 1
    row = await news_store.get_feed(feed["id"])
    assert row is not None and row["entry_count"] == 2


async def test_an_entry_repeated_inside_one_document_is_filed_once(store, monkeypatch) -> None:
    """Some feeds list the same item twice; the unique index must not blow up."""
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example")
    duplicate = _entry("https://example.com/one")
    _canned(monkeypatch, [_fetch([duplicate, _entry("https://example.com/one?utm_source=x")])])

    result = await ingest.ingest(feed_ids=[feed["id"]])
    assert result["new_articles"] == 1
    assert len(await news_store.list_articles()) == 1


async def test_the_stored_validator_is_sent_on_the_next_poll(store, monkeypatch) -> None:
    """This is what makes a short cadence affordable — verify it actually happens."""
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example")

    calls: list[list[tuple[str, str, str]]] = []
    _canned(
        monkeypatch,
        [_fetch([_entry("https://example.com/one")], etag='"v1"', last_modified="Mon, 03 Aug 2026")],
        captured=calls,
    )
    await ingest.ingest(feed_ids=[feed["id"]])
    assert calls[0] == [("https://example.com/feed", "", "")]

    _canned(monkeypatch, [news.FeedFetch(status=304, not_modified=True, etag='"v1"')], calls)
    result = await ingest.ingest(feed_ids=[feed["id"]])

    assert calls[1] == [("https://example.com/feed", '"v1"', "Mon, 03 Aug 2026")]
    assert result["not_modified"] == 1
    assert result["new_articles"] == 0


async def test_a_failing_feed_records_why_instead_of_looking_empty(store, monkeypatch) -> None:
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example")
    _canned(monkeypatch, [news.FeedFetch(status=403, error="HTTP 403")])

    result = await ingest.ingest(feed_ids=[feed["id"]])
    assert result["errors"] == 1

    row = await news_store.get_feed(feed["id"])
    assert row is not None
    assert row["last_status"] == 403
    assert row["last_error"] == "HTTP 403"
    assert row["consecutive_failures"] == 1
    assert row["health"] == "failing"


async def test_a_feed_that_keeps_failing_is_disabled(store, monkeypatch) -> None:
    from ppn_blogger.server import ingest, news_store
    from ppn_blogger.settings import get_settings

    monkeypatch.setattr(get_settings().news, "max_failures", 3)
    feed = await news_store.create_feed("https://example.com/feed", name="Example")

    for _ in range(3):
        _canned(monkeypatch, [news.FeedFetch(status=0, error="ConnectError: no route")])
        await ingest.ingest(feed_ids=[feed["id"]])

    row = await news_store.get_feed(feed["id"])
    assert row is not None
    assert row["enabled"] is False
    assert row["health"] == "disabled"

    # Re-enabling clears the strike count, or it dies again on the next failure.
    row = await news_store.update_feed(feed["id"], {"enabled": True})
    assert row["consecutive_failures"] == 0 and row["last_error"] == ""


async def test_a_successful_poll_clears_an_earlier_failure(store, monkeypatch) -> None:
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example")
    _canned(monkeypatch, [news.FeedFetch(status=500, error="HTTP 500")])
    await ingest.ingest(feed_ids=[feed["id"]])

    _canned(monkeypatch, [_fetch([_entry("https://example.com/one")])])
    await ingest.ingest(feed_ids=[feed["id"]])

    row = await news_store.get_feed(feed["id"])
    assert row is not None
    assert row["consecutive_failures"] == 0
    assert row["health"] == "ok"


async def test_due_feeds_respects_the_next_poll_time(store, monkeypatch) -> None:
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example")
    assert await news_store.due_feeds() == [feed["id"]]  # new feeds are due at once

    _canned(monkeypatch, [_fetch([])])
    await ingest.ingest(feed_ids=[feed["id"]])
    assert await news_store.due_feeds() == []


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------


async def test_article_filters(store, monkeypatch) -> None:
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example")
    other = await news_store.create_feed("https://other.example/feed", name="Other")
    group = await news_store.create_group("AI")
    await news_store.set_group_feeds(group["id"], [feed["id"]])

    _canned(
        monkeypatch,
        [
            _fetch([_entry("https://example.com/one", title="Transformers explained")]),
            _fetch([_entry("https://other.example/two", title="Something else")]),
        ],
    )
    await ingest.ingest(feed_ids=[feed["id"], other["id"]])

    assert len(await news_store.list_articles()) == 2
    assert len(await news_store.list_articles(feed_id=feed["id"])) == 1
    assert len(await news_store.list_articles(group_id=group["id"])) == 1

    hits = await news_store.list_articles(q="transformers")
    assert len(hits) == 1 and hits[0]["title"] == "Transformers explained"

    assert await news_store.list_articles(since=datetime(2026, 9, 1, tzinfo=UTC)) == []


async def test_prune_keeps_anything_an_issue_used(store, monkeypatch) -> None:
    from sqlalchemy import select

    from ppn_blogger.server import ingest, news_store
    from ppn_blogger.server.db import Article, session, utcnow

    feed = await news_store.create_feed("https://example.com/feed", name="Example")
    _canned(
        monkeypatch,
        [_fetch([_entry("https://example.com/one"), _entry("https://example.com/two")])],
    )
    await ingest.ingest(feed_ids=[feed["id"]])

    async with session() as s:
        rows = list((await s.execute(select(Article))).scalars())
        for row in rows:
            row.fetched_at = utcnow() - timedelta(days=400)
        rows[0].used_in_issue_at = utcnow()  # cited by a newsletter
        await s.commit()

    assert await news_store.prune_articles() == 1
    remaining = await news_store.list_articles()
    assert len(remaining) == 1


def test_parse_since_accepts_hours_or_iso() -> None:
    from ppn_blogger.server.news_store import parse_since

    assert parse_since("") is None
    assert parse_since("nonsense") is None
    recent = parse_since("24")
    assert recent is not None and recent.tzinfo is not None
    exact = parse_since("2026-08-03T00:00:00Z")
    assert exact is not None and exact.year == 2026
