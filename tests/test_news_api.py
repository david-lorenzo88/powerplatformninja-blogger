"""Real HTTP against the news router, with the network stubbed at news.probe.

The behaviour worth pinning here is that nothing reaches the registry without
being fetched and parsed first. A feed URL that 404s, or a page that is HTML
rather than a feed, must be refused at the door — otherwise "the registry only
contains real feeds" stops being true and every later phase inherits the doubt.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ppn_blogger import news


@pytest.fixture
async def api(monkeypatch, database_url):
    monkeypatch.setenv("PPN_MAX_CONCURRENT_RUNS", "2")

    from ppn_blogger.config_source import set_config_source
    from ppn_blogger.server import runs

    await runs.reset_manager()

    import httpx

    from ppn_blogger.server.app import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            yield client

    await runs.reset_manager()
    set_config_source(None)


def _good_probe(entries: int = 3):
    async def probe(url, **kw):
        return news.FeedFetch(
            status=200,
            title="Example Blog",
            site_url="https://example.com/",
            entries=[
                news.FetchedEntry(
                    title=f"Post {i}",
                    url=f"https://example.com/{i}",
                    summary="something happened",
                    published=datetime(2026, 8, 3, tzinfo=UTC),
                    entry_key=news.url_hash(f"https://example.com/{i}"),
                )
                for i in range(entries)
            ],
        )

    return probe


async def _wait_for(client, run_id, timeout=20):
    from ppn_blogger.server.runs import TERMINAL

    for _ in range(timeout * 10):
        body = (await client.get(f"/api/runs/{run_id}")).json()
        if body["status"] in TERMINAL:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"run {run_id} never finished")


# ---------------------------------------------------------------------------
# Seeding and listing
# ---------------------------------------------------------------------------


async def test_the_curated_feeds_are_seeded_on_first_boot(api) -> None:
    feeds = (await api.get("/api/news/feeds")).json()
    assert len(feeds) >= 9
    assert all(f["origin"] == "seed" for f in feeds)
    assert any("power-platform" in f["url"] for f in feeds)


async def test_summary_reports_the_cost_of_the_current_cadence(api) -> None:
    body = (await api.get("/api/news/summary")).json()
    assert body["feeds"] >= 9
    assert body["articles"] == 0
    # The six-hourly default has to leave the serverless database able to pause.
    assert body["db_can_autopause"] is True


# ---------------------------------------------------------------------------
# Adding feeds
# ---------------------------------------------------------------------------


async def test_a_feed_is_fetched_before_it_is_stored(api, monkeypatch) -> None:
    monkeypatch.setattr(news, "probe", _good_probe())

    response = await api.post(
        "/api/news/feeds", json={"url": "https://example.com/feed", "name": "Example"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Example"
    assert body["title"] == "Example Blog"
    assert body["health"] == "ok"


async def test_a_url_that_is_not_a_feed_is_refused(api, monkeypatch) -> None:
    async def empty(url, **kw):
        return news.FeedFetch(status=200, entries=[])

    async def no_candidates(url, **kw):
        return []

    monkeypatch.setattr(news, "probe", empty)
    monkeypatch.setattr(news, "discover_feeds", no_candidates)

    response = await api.post("/api/news/feeds", json={"url": "https://example.com/about"})
    assert response.status_code == 422
    assert "no feed there" in response.json()["detail"].lower()


async def test_adding_the_same_feed_twice_is_a_conflict(api, monkeypatch) -> None:
    monkeypatch.setattr(news, "probe", _good_probe())

    assert (await api.post("/api/news/feeds", json={"url": "https://example.com/feed"})).status_code == 201
    duplicate = await api.post(
        "/api/news/feeds", json={"url": "https://www.example.com/feed/?utm_source=x"}
    )
    assert duplicate.status_code == 409
    assert "already following" in duplicate.json()["detail"].lower()


async def test_validate_finds_the_feed_behind_a_site_url(api, monkeypatch) -> None:
    """Paste what is in the address bar; we find the feed and show a preview."""
    calls: list[str] = []

    async def probe(url, **kw):
        calls.append(url)
        if url.endswith("/feed.xml"):
            return await _good_probe()(url)
        return news.FeedFetch(status=200, entries=[])

    async def discover(url, **kw):
        return ["https://example.com/feed.xml"]

    monkeypatch.setattr(news, "probe", probe)
    monkeypatch.setattr(news, "discover_feeds", discover)

    body = (await api.post("/api/news/feeds/validate", json={"url": "https://example.com"})).json()
    assert body["ok"] is True
    assert body["url"] == "https://example.com/feed.xml"
    assert body["discovered_from"] == "https://example.com"
    assert body["entry_count"] == 3
    assert len(body["entries"]) == 3  # a preview, capped at five


async def test_validate_says_so_when_there_is_nothing_there(api, monkeypatch) -> None:
    async def probe(url, **kw):
        return news.FeedFetch(status=404, error="HTTP 404")

    async def discover(url, **kw):
        return []

    monkeypatch.setattr(news, "probe", probe)
    monkeypatch.setattr(news, "discover_feeds", discover)

    body = (await api.post("/api/news/feeds/validate", json={"url": "https://nope.example"})).json()
    assert body["ok"] is False
    assert body["error"]


# ---------------------------------------------------------------------------
# Editing, groups, articles
# ---------------------------------------------------------------------------


async def test_feed_patch_and_delete(api, monkeypatch) -> None:
    monkeypatch.setattr(news, "probe", _good_probe())
    feed = (await api.post("/api/news/feeds", json={"url": "https://example.com/feed"})).json()

    patched = (
        await api.patch(f"/api/news/feeds/{feed['id']}", json={"realtime": True, "tier": "official"})
    ).json()
    assert patched["realtime"] is True and patched["tier"] == "official"

    assert (await api.delete(f"/api/news/feeds/{feed['id']}")).status_code == 200
    assert (await api.get(f"/api/news/feeds/{feed['id']}")).json()["enabled"] is False

    assert (await api.get("/api/news/feeds/999999")).status_code == 404
    assert (await api.patch("/api/news/feeds/999999", json={"name": "x"})).status_code == 404


async def test_groups_round_trip_over_http(api) -> None:
    feeds = (await api.get("/api/news/feeds")).json()
    group = (await api.post("/api/news/feed-groups", json={"name": "Microsoft"})).json()
    assert group["slug"] == "microsoft"

    assert (await api.post("/api/news/feed-groups", json={"name": "Microsoft"})).status_code == 409

    ids = [feeds[0]["id"], feeds[1]["id"]]
    updated = (
        await api.put(f"/api/news/feed-groups/{group['id']}/feeds", json={"feed_ids": ids})
    ).json()
    assert updated["feed_ids"] == ids

    in_group = (await api.get(f"/api/news/feeds?group_id={group['id']}")).json()
    assert len(in_group) == 2

    # Watching is set for the whole group in one call.
    watched = await api.post(
        f"/api/news/feed-groups/{group['id']}/realtime", json={"realtime": True}
    )
    assert watched.status_code == 200, watched.text
    assert watched.json()["feeds_realtime"] == watched.json()["feed_count"]
    members = (await api.get(f"/api/news/feeds?group_id={group['id']}")).json()
    assert all(f["realtime"] for f in members)
    # Feeds outside the group are left alone.
    others = [f for f in (await api.get("/api/news/feeds")).json() if f["id"] not in ids]
    assert not any(f["realtime"] for f in others)

    off = await api.post(
        f"/api/news/feed-groups/{group['id']}/realtime", json={"realtime": False}
    )
    assert off.json()["feeds_realtime"] == 0

    missing = await api.post("/api/news/feed-groups/9999/realtime", json={"realtime": True})
    assert missing.status_code == 404

    assert (await api.delete(f"/api/news/feed-groups/{group['id']}")).status_code == 200
    assert (await api.get(f"/api/news/feed-groups/{group['id']}")).status_code == 404


# ---------------------------------------------------------------------------
# The ingest run
# ---------------------------------------------------------------------------


async def test_a_refresh_is_a_run_and_it_fills_the_stream(api, monkeypatch) -> None:
    """An ingest is an ordinary run: same list, same log, same history."""
    monkeypatch.setattr(news, "probe", _good_probe())
    feed = (await api.post("/api/news/feeds", json={"url": "https://example.com/feed"})).json()

    async def fetch_many(specs, **kw):
        return [await _good_probe()(url) for url, _, _ in specs]

    monkeypatch.setattr(news, "fetch_many", fetch_many)

    response = await api.post(f"/api/news/feeds/{feed['id']}/refresh")
    assert response.status_code == 202
    finished = await _wait_for(api, response.json()["id"])

    assert finished["status"] == "succeeded"
    assert finished["result"]["new_articles"] == 3
    assert finished["kind"] == "ingest"

    articles = (await api.get("/api/news/articles")).json()
    assert len(articles) == 3
    assert articles[0]["feed_name"]

    # Polling again files nothing new — the property the whole subsystem rests on.
    again = await api.post(f"/api/news/feeds/{feed['id']}/refresh")
    finished = await _wait_for(api, again.json()["id"])
    assert finished["result"]["new_articles"] == 0
    assert len((await api.get("/api/news/articles")).json()) == 3


async def test_refresh_of_an_unknown_feed_is_404(api) -> None:
    assert (await api.post("/api/news/feeds/999999/refresh")).status_code == 404


async def test_article_query_filters_over_http(api, monkeypatch) -> None:
    monkeypatch.setattr(news, "probe", _good_probe())
    feed = (await api.post("/api/news/feeds", json={"url": "https://example.com/feed"})).json()

    async def fetch_many(specs, **kw):
        return [await _good_probe()(url) for url, _, _ in specs]

    monkeypatch.setattr(news, "fetch_many", fetch_many)
    await _wait_for(api, (await api.post(f"/api/news/feeds/{feed['id']}/refresh")).json()["id"])

    assert len((await api.get("/api/news/articles?q=post")).json()) == 3
    assert len((await api.get("/api/news/articles?q=nothingmatches")).json()) == 0
    assert len((await api.get(f"/api/news/articles?feed_id={feed['id']}&limit=2")).json()) == 2
    assert (await api.get("/api/news/articles/999999")).status_code == 404


async def test_a_trailing_slash_404s_and_never_redirects(api) -> None:
    """A 307 reads as an expired session in the UI client, not as a 404.

    ui/src/api/client.ts fetches with redirect: 'manual' and bounces to the Entra
    login on any redirect, so a redirecting route logs the operator out instead
    of returning an error they can see.

    Declaring routes without a trailing slash is not enough on its own — that is
    exactly what makes Starlette redirect *to* them. The app sets
    redirect_slashes=False; this is the assertion that keeps it set.

    The pre-existing routes are checked too, because they had the same problem
    and it was masked in production by the SPA catch-all absorbing the path.
    """
    for path in (
        "/api/news/feeds",
        "/api/news/feed-groups",
        "/api/news/articles",
        "/api/runs",
        "/api/config",
    ):
        assert (await api.get(path)).status_code == 200
        assert (await api.get(path + "/")).status_code == 404


async def test_a_discovery_brief_travels_in_the_body(api, monkeypatch) -> None:
    """The brief is prose the operator typed, so it goes in the body.

    The query parameter is kept because it was the original signature and a
    cached SPA still sends it — an operator on yesterday's bundle should get a
    general sweep, not a 422.
    """
    from ppn_blogger.server import discovery

    # The queue dispatches for real, and a real sweep calls a model. What is
    # under test is the parameter's journey to the run row, not the sweep.
    # Every run started here is then waited on: leaving one in flight hangs the
    # teardown rather than failing, which reads as a broken test file.
    swept: list[str] = []

    async def no_sweep(instruction="", **kw):
        swept.append(instruction)
        return {"review_id": 0, "candidate_count": 0, "suggested": 0}

    monkeypatch.setattr(discovery, "sweep", no_sweep)

    brief = "Power Platform ALM — release notes, and the practitioners worth reading"

    response = await api.post("/api/news/discover", json={"instruction": brief})
    assert response.status_code == 202

    run = (await api.get(f"/api/runs/{response.json()['id']}")).json()
    assert run["params"]["instruction"] == brief
    # The brief names the run, so the Runs list distinguishes one sweep from another.
    assert brief[:40] in run["label"]
    await _wait_for(api, response.json()["id"])

    legacy = await api.post("/api/news/discover?instruction=older+client")
    assert legacy.status_code == 202
    assert (await api.get(f"/api/runs/{legacy.json()['id']}")).json()["params"][
        "instruction"
    ] == "older client"
    await _wait_for(api, legacy.json()["id"])

    # And with nothing at all: a general sweep, not a validation error.
    bare = await api.post("/api/news/discover")
    assert bare.status_code == 202
    await _wait_for(api, bare.json()["id"])

    # The brief reached the sweep itself, not just the run row.
    assert swept == [brief, "older client", ""]
