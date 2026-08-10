"""The scheduler and the watch notifications.

Two assertions here are the reason this file exists.

**Two schedulers over one database fire exactly once.** `minReplicas: 1` is not
the guarantee it looks like — Container Apps starts the new revision before
draining the old, so every deploy briefly runs two. Without the compare-and-swap
claim, every deploy would double-fetch and double-notify.

**A missed tick fires once, not once per interval missed.** After an outage the
alternative is a thundering herd the moment the app comes back.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import select

from ppn_blogger import news
from ppn_blogger.server.watch import in_quiet_hours, parse_quiet_hours


@pytest.fixture
async def sched(database_url, monkeypatch):
    """A migrated database and a fresh scheduler, with the jobs seeded."""
    from ppn_blogger.server import db
    from ppn_blogger.server import scheduler as sched_mod

    await db.init_db()
    await sched_mod.reset_scheduler()

    # Nothing in these tests should reach the network or the run queue unless the
    # test says so.
    async def no_fetch(specs, **kw):
        return [news.FeedFetch(status=304, not_modified=True) for _ in specs]

    monkeypatch.setattr(news, "fetch_many", no_fetch)

    yield sched_mod.scheduler()
    await sched_mod.reset_scheduler()


async def _job(key: str):
    from ppn_blogger.server.db import SchedulerJob, session

    async with session() as s:
        return (
            await s.execute(select(SchedulerJob).where(SchedulerJob.key == key))
        ).scalar_one_or_none()


async def _set_due(key: str, when: datetime) -> None:
    from ppn_blogger.server.db import SchedulerJob, session

    async with session() as s:
        row = (
            await s.execute(select(SchedulerJob).where(SchedulerJob.key == key))
        ).scalar_one_or_none()
        row.next_due_at = when
        await s.commit()


# ---------------------------------------------------------------------------
# Job rows
# ---------------------------------------------------------------------------


async def test_jobs_are_seeded_and_due_immediately(sched) -> None:
    from ppn_blogger.server.scheduler import FETCH, PRUNE, WATCH

    await sched.sync_jobs()
    fetch = await _job(FETCH)
    assert fetch is not None and fetch.enabled and fetch.next_due_at is not None

    # The watch job does not exist as work until a feed opts in — creating it
    # anyway would hold the database awake every 15 minutes for nothing.
    watch = await _job(WATCH)
    assert watch is not None and watch.enabled is False and watch.next_due_at is None

    assert (await _job(PRUNE)) is not None


async def test_watching_a_feed_brings_the_watch_job_into_being(sched) -> None:
    from ppn_blogger.server import news_store
    from ppn_blogger.server.scheduler import WATCH

    await news_store.create_feed("https://example.com/feed", name="Example", realtime=True)
    await sched.sync_jobs()

    watch = await _job(WATCH)
    assert watch.enabled is True and watch.next_due_at is not None


def sched_mod_jobs():
    from ppn_blogger.server import scheduler as sched_mod

    return sched_mod._jobs()


async def test_sync_jobs_is_idempotent(sched) -> None:
    from ppn_blogger.server.db import SchedulerJob, session

    for _ in range(3):
        await sched.sync_jobs()
    async with session() as s:
        rows = list((await s.execute(select(SchedulerJob))).scalars())
    # One row per job key, however many jobs there are.
    assert len(rows) == len({r.key for r in rows}) == len(sched_mod_jobs())


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


async def test_a_job_that_is_not_due_does_not_fire(sched) -> None:
    await sched.sync_jobs()
    await _set_due("fetch", datetime.now(UTC) + timedelta(hours=1))
    # Other jobs are seeded due-now on a fresh database and legitimately fire;
    # this is only about the one that has been pushed into the future.
    assert "fetch" not in (await sched.tick_once())["fired"]


async def test_two_schedulers_fire_a_due_job_exactly_once(sched, monkeypatch) -> None:
    """The deploy-overlap case: two replicas alive, one tick between them."""
    from ppn_blogger.server.scheduler import Scheduler

    calls: list[str] = []

    async def counted() -> str:
        calls.append("fetch")
        return "ok"

    monkeypatch.setattr("ppn_blogger.server.scheduler._run_fetch", counted)

    await sched.sync_jobs()
    await _set_due("fetch", datetime.now(UTC) - timedelta(minutes=1))

    a, b = Scheduler(), Scheduler()
    fired_a = await a.tick_once()
    fired_b = await b.tick_once()

    assert calls == ["fetch"]
    assert ("fetch" in fired_a["fired"]) != ("fetch" in fired_b["fired"])


async def test_a_missed_tick_fires_once_not_once_per_interval(sched, monkeypatch) -> None:
    """Six hours late is still one fetch. The next due time is now + interval."""
    calls: list[str] = []

    async def counted() -> str:
        calls.append("fetch")
        return "ok"

    monkeypatch.setattr("ppn_blogger.server.scheduler._run_fetch", counted)

    await sched.sync_jobs()
    await _set_due("fetch", datetime.now(UTC) - timedelta(hours=30))

    await sched.tick_once()
    await sched.tick_once()  # immediately again: nothing is due now

    assert calls == ["fetch"]
    row = await _job("fetch")
    from ppn_blogger.server.db import as_utc

    assert as_utc(row.next_due_at) > datetime.now(UTC)


async def test_a_failing_job_records_why_and_does_not_stop_the_others(sched, monkeypatch) -> None:
    async def boom() -> str:
        raise RuntimeError("feed host exploded")

    monkeypatch.setattr("ppn_blogger.server.scheduler._run_fetch", boom)

    await sched.sync_jobs()
    await _set_due("fetch", datetime.now(UTC) - timedelta(minutes=1))
    await _set_due("prune", datetime.now(UTC) - timedelta(minutes=1))

    result = await sched.tick_once()
    assert set(result["fired"]) == {"fetch", "prune"}

    failed = await _job("fetch")
    assert failed.last_status == "error" and "exploded" in failed.last_error
    assert (await _job("prune")).last_status == "ok"


async def test_describe_reports_what_the_cadence_costs(sched) -> None:
    from ppn_blogger.server import news_store

    await sched.sync_jobs()
    quiet = await sched.describe()
    assert quiet["watched_feeds"] == 0
    assert quiet["effective_min_cadence_minutes"] >= 60
    assert quiet["db_can_autopause"] is True

    await news_store.create_feed("https://example.com/feed", name="Example", realtime=True)
    await sched.sync_jobs()
    watched = await sched.describe()

    # One watched feed is what turns the short cadence on, and with it the bill.
    assert watched["watched_feeds"] == 1
    assert watched["effective_min_cadence_minutes"] < 60
    assert watched["db_can_autopause"] is False


async def test_run_now_ignores_the_due_time(sched) -> None:
    await sched.sync_jobs()
    result = await sched.run_now("prune")
    assert "pruned" in result["detail"]

    with pytest.raises(KeyError):
        await sched.run_now("nonsense")


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "moment,expected",
    [
        (time(23, 0), True),   # inside, before midnight
        (time(3, 0), True),    # inside, after midnight
        (time(6, 59), True),
        (time(7, 0), False),   # the window is half-open at the end
        (time(21, 59), False),
        (time(22, 0), True),
    ],
)
def test_quiet_hours_across_midnight(moment: time, expected: bool) -> None:
    window = parse_quiet_hours("22:00-07:00")
    now = datetime(2026, 8, 10, moment.hour, moment.minute, tzinfo=UTC)
    assert in_quiet_hours(now, window) is expected


def test_quiet_hours_within_one_day() -> None:
    window = parse_quiet_hours("01:00-06:00")
    assert in_quiet_hours(datetime(2026, 8, 10, 3, tzinfo=UTC), window) is True
    assert in_quiet_hours(datetime(2026, 8, 10, 12, tzinfo=UTC), window) is False


def test_unparseable_quiet_hours_means_no_quiet_hours() -> None:
    for bad in ("", "nonsense", "25:00-99:00", "22:00"):
        assert parse_quiet_hours(bad) is None
    assert in_quiet_hours(datetime.now(UTC), None) is False


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def _entry(url: str, title: str = "A post") -> news.FetchedEntry:
    return news.FetchedEntry(
        title=title,
        url=url,
        summary="s",
        published=datetime(2026, 8, 3, tzinfo=UTC),
        entry_key=news.url_hash(url),
    )


async def _watched_feed_with(monkeypatch, entries) -> int:
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example", realtime=True)

    async def fetch_many(specs, **kw):
        return [news.FeedFetch(status=200, title="Example", entries=entries) for _ in specs]

    monkeypatch.setattr(news, "fetch_many", fetch_many)
    await ingest.ingest(feed_ids=[feed["id"]])
    return feed["id"]


@pytest.fixture
def sent(monkeypatch):
    """Capture pushes instead of sending them, with quiet hours out of the way.

    Quiet hours default to 22:00-07:00 in the operator's timezone, so without
    this every test below asserts a notification that the code is right to
    suppress — and the suite passes in the afternoon and fails at night, on
    either backend. Found exactly that way: a SQL Server run at 22:50 Madrid
    time failed three tests that had nothing to do with the dialect.

    The quiet-hours behaviour itself is covered separately, by patching
    `in_quiet_hours` directly.
    """
    from ppn_blogger.server import push
    from ppn_blogger.settings import get_settings

    monkeypatch.setattr(get_settings().news, "quiet_hours", "")

    calls: list[tuple[str, str, str, str]] = []

    async def fake_notify(title, body, url, tag=""):
        calls.append((title, body, url, tag))
        return 1

    monkeypatch.setattr(push, "notify", fake_notify)
    return calls


async def test_an_article_notifies_exactly_once(sched, monkeypatch, sent) -> None:
    from ppn_blogger.server.watch import notify_new_articles

    await _watched_feed_with(monkeypatch, [_entry("https://example.com/one", "Something shipped")])

    assert await notify_new_articles() == 1
    assert len(sent) == 1
    assert "Something shipped" in sent[0][1]

    # Nothing new, and nothing re-announced.
    assert await notify_new_articles() == 0
    assert len(sent) == 1


async def test_one_notification_per_feed_not_per_article(sched, monkeypatch, sent) -> None:
    from ppn_blogger.server.watch import notify_new_articles

    await _watched_feed_with(
        monkeypatch,
        [_entry(f"https://example.com/{i}", f"Post {i}") for i in range(3)],
    )

    assert await notify_new_articles() == 1
    assert len(sent) == 1
    assert "Post 0" in sent[0][1] and "Post 2" in sent[0][1]


async def test_a_firehose_is_summarised_rather_than_dropped(sched, monkeypatch, sent) -> None:
    """40 new items is one line, not 40 buzzes and not 3 with 37 lost."""
    from ppn_blogger.server.watch import notify_new_articles

    await _watched_feed_with(
        monkeypatch,
        [_entry(f"https://example.com/{i}", f"Post {i}") for i in range(40)],
    )

    assert await notify_new_articles() == 1
    assert len(sent) == 1
    assert "40 new items" in sent[0][1]
    # Every one of them is marked, so the next tick is silent.
    assert await notify_new_articles() == 0


async def test_quiet_hours_hold_notifications_without_losing_them(
    sched, monkeypatch, sent
) -> None:
    from ppn_blogger.server import watch

    await _watched_feed_with(monkeypatch, [_entry("https://example.com/one", "Late news")])

    monkeypatch.setattr(watch, "in_quiet_hours", lambda *a, **kw: True)
    assert await watch.notify_new_articles() == 0
    assert sent == []
    # Not stamped: the backlog survives the window rather than being burned.
    assert await watch.unnotified_count() == 1

    monkeypatch.setattr(watch, "in_quiet_hours", lambda *a, **kw: False)
    assert await watch.notify_new_articles() == 1
    assert len(sent) == 1


async def test_an_unwatched_feed_never_notifies(sched, monkeypatch, sent) -> None:
    from ppn_blogger.server import ingest, news_store
    from ppn_blogger.server.watch import notify_new_articles

    feed = await news_store.create_feed("https://quiet.example/feed", name="Quiet")

    async def fetch_many(specs, **kw):
        return [news.FeedFetch(status=200, entries=[_entry("https://quiet.example/one")])]

    monkeypatch.setattr(news, "fetch_many", fetch_many)
    await ingest.ingest(feed_ids=[feed["id"]])

    assert await notify_new_articles() == 0
    assert sent == []


async def test_a_broken_push_service_never_stops_the_tick(sched, monkeypatch) -> None:
    from ppn_blogger.server import push
    from ppn_blogger.server.watch import notify_new_articles

    await _watched_feed_with(monkeypatch, [_entry("https://example.com/one")])

    async def boom(*a, **kw):
        raise RuntimeError("push service down")

    monkeypatch.setattr(push, "notify", boom)
    assert await notify_new_articles() == 0  # swallowed, not raised
