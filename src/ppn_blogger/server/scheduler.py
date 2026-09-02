"""The first periodic work in this codebase.

Three things here are decisions rather than mechanics, and each one is load-bearing.

**It sleeps until the next due time; it does not tick.** A one-minute loop would
query the database 1,440 times a day and guarantee Azure SQL never auto-pauses —
roughly $150-200/month at list price, for a feature that fires a handful of times
a day. Sleeping to the horizon means the process holds no connection and issues
no queries between jobs, so the database is genuinely idle and genuinely pauses.
The cost then follows the operator's own cadence choices, which is the honest
place for it to sit. An ``asyncio.Event`` makes an edit take effect immediately
rather than at the next wake, which is what makes a long sleep acceptable.

**A claim is a compare-and-swap, because one replica is not one process.**
`minReplicas: 1` looks like a guarantee and is not: Container Apps starts the new
revision before draining the old, so every deploy has two schedulers alive for a
minute or so. ``UPDATE ... WHERE next_due_at = <what we read>`` lets exactly one
of them win, with no ``SELECT FOR UPDATE`` and no dialect-specific locking.

**Missed ticks collapse to one.** After a restart or a long outage, a job due six
times over fires once. Six backlogged fetches produce nothing one fetch does not,
and the alternative is a thundering herd every time the app comes back.

Only the full sweep becomes a visible run. The watch job runs inline: at fifteen
minutes it would file ninety-six run rows a day and bury the Runs screen, which
is the main window onto the crew. Its outcome lands on the job row instead, and
its real signal is the notification it sends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from ..settings import get_settings
from .db import SchedulerJob, as_utc, session, utcnow

logger = logging.getLogger("ppn.server.scheduler")

FETCH, WATCH, PRUNE, LETTERS, RETRIES, PRICES, LEARN = (
    "fetch",
    "watch",
    "prune",
    "newsletters",
    "retry_deliveries",
    "prices",
    "learn",
)

# Azure list prices move a few times a year. Weekly is already far more often
# than the data changes; anything shorter would be a database wake-up bought for
# nothing.
PRICE_REFRESH_MINUTES = 7 * 24 * 60


@dataclass(slots=True)
class Job:
    key: str
    label: str
    interval_minutes: Callable[[], int]
    run: Callable[[], Awaitable[str]]
    # False for jobs that should not exist yet — the watch job has nothing to do
    # until a feed opts in, and creating it anyway would hold the database awake
    # for no reason.
    applies: Callable[[], Awaitable[bool]]


# ---------------------------------------------------------------------------
# What the jobs actually do
# ---------------------------------------------------------------------------


async def _run_fetch() -> str:
    """Enqueue the full sweep as an ordinary run.

    Through the queue rather than inline so a scheduled fetch is identical to one
    the operator started: same list, same log, same cancellation, same history.
    """
    from .runs import manager

    run_id = await manager().enqueue("ingest", {"only_due": True}, "Scheduled fetch")
    return f"queued run {run_id[:8]}"


async def _run_watch() -> str:
    """Poll the closely-watched feeds and notify about anything new.

    Inline, and deliberately not a run: at this cadence it would file a run row
    every fifteen minutes and bury the Runs screen.
    """
    from .ingest import ingest
    from .watch import notify_new_articles

    result = await ingest(only_realtime=True, only_due=True)
    sent = await notify_new_articles()
    detail = f"{result['new_articles']} new, {sent} notification(s)"
    if result["errors"]:
        detail += f", {result['errors']} error(s)"
    return detail


async def _run_newsletters() -> str:
    """Queue an issue for every newsletter whose turn it is.

    Each is claimed with the same compare-and-swap as a system job, so two
    schedulers overlapping on a deploy cannot both queue the same issue — and a
    newsletter whose previous run is still going is skipped rather than stacked.
    """
    from . import newsletters
    from .runs import manager

    queued = 0
    for newsletter_id in await newsletters.due_newsletters():
        if not await newsletters.claim_due(newsletter_id):
            continue
        row = await newsletters.get(newsletter_id)
        if row is None:
            continue
        run_id = await manager().enqueue(
            "newsletter", {"newsletter_id": newsletter_id}, f"Newsletter · {row['name']}"
        )
        await newsletters.attach_run(newsletter_id, run_id)
        queued += 1
    return f"{queued} issue(s) queued"


async def _any_scheduled_newsletters() -> bool:
    from sqlalchemy import func, true

    from .db import Newsletter

    async with session() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(Newsletter)
            .where(Newsletter.enabled == true(), Newsletter.next_due_at.is_not(None))
        )
    return bool(count)


async def _run_retries() -> str:
    """Re-attempt deliveries whose backoff has expired.

    Folded into the same due-time horizon as everything else rather than given
    its own poll — and it only exists while something is actually waiting, so a
    quiet system holds nothing awake.
    """
    from .delivery import deliver_issue, due_retries

    retried = 0
    for issue_id in await due_retries():
        await deliver_issue(issue_id, only_pending=True)
        retried += 1
    return f"{retried} issue(s) retried"


async def _any_pending_deliveries() -> bool:
    from sqlalchemy import func

    from .db import Delivery

    async with session() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(Delivery)
            .where(Delivery.status == "pending", Delivery.next_retry_at.is_not(None))
        )
    return bool(count)


async def _run_prune() -> str:
    from .news_store import prune_articles

    removed = await prune_articles()
    return f"{removed} article(s) pruned"


async def _any_watched_feeds() -> bool:
    from sqlalchemy import func, true

    from .db import Feed

    async with session() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(Feed)
            .where(Feed.enabled == true(), Feed.realtime == true())
        )
    return bool(count)


async def _run_prices() -> str:
    """Re-read every bound price meter, and save any that moved.

    Applied without asking, which is safe for one specific reason: `run_usage`
    stores `cost_micros` at the time of the run, so a price change moves future
    figures and never rewrites history. The write is an ordinary config version,
    so it is visible in the document's history and can be rolled back like any
    other edit.

    Only ever moves numbers. Meter *names* are bound by a human and this cannot
    change them, so the worst an unattended run can do is track Azure.
    """
    import yaml

    from ..settings import get_settings as _settings
    from . import config_store, prices

    document = _settings().model_prices
    changes = await prices.refresh(document)
    moved = [c for c in changes if c["changed"]]
    missing = [c["meter"] for c in changes if not c["found"]]

    if not moved:
        detail = f"{len(changes)} meter(s) checked, all current"
        return detail + (f"; {len(missing)} not found" if missing else "")

    updated = prices.apply(document, changes)
    updated["updated_from_azure"] = utcnow().isoformat()
    row = await config_store.save_document(
        "model_prices",
        yaml.safe_dump(updated, sort_keys=False, allow_unicode=True),
        note=f"Weekly Azure price refresh: {len(moved)} change(s)",
    )

    summary = ", ".join(
        f"{c['model']} {c['direction']} {c['old']}→{c['new']}" for c in moved[:4]
    )
    await _notify_price_change(moved, row.version)
    return f"saved v{row.version}: {summary}" + (" …" if len(moved) > 4 else "")


async def _notify_price_change(moved: list[dict], version: int) -> None:
    """Tell the operator their cost baseline moved. Never raises."""
    try:
        from . import push

        await push.notify(
            title="Model prices changed",
            body=f"{len(moved)} price(s) updated from Azure (model_prices v{version}).",
            url="/config",
        )
    except Exception:  # noqa: BLE001 - a missed notification is not a failed job
        logger.exception("could not notify about the price refresh")


async def _any_bound_meters() -> bool:
    """False until a human has bound at least one meter.

    The refresh has nothing to read before that, and a job row that exists only
    to wake the database and find nothing to do is precisely the cost this
    scheduler is shaped to avoid.
    """
    from ..settings import get_settings as _settings

    models = _settings().model_prices.get("models") or {}
    return any(
        isinstance(entry, dict) and entry.get("meters") for entry in models.values()
    )


async def _run_learn() -> str:
    """Enqueue a learning sweep as an ordinary run, like the feed fetch."""
    from .runs import manager

    run_id = await manager().enqueue("learn", {}, "Learning sweep")
    return f"run {run_id}"


async def _any_captured_pairs() -> bool:
    """False until the author has actually published something that was edited.

    Two conditions, both required. The feature has to be switched on, and there
    has to be at least one captured pair nobody has read yet — a weekly job that
    wakes a serverless database to discover there is nothing to learn is exactly
    the cost this scheduler exists to avoid.
    """
    from ..settings import get_settings as _settings

    if not _settings().learning.enabled:
        return False
    from . import delta_store

    return await delta_store.unanalysed_count() > 0


async def _always() -> bool:
    return True


def _jobs() -> list[Job]:
    news = get_settings().news
    return [
        Job(FETCH, "Fetch all feeds", lambda: news.ingest_interval_minutes, _run_fetch, _always),
        Job(
            WATCH,
            "Check watched feeds",
            lambda: news.realtime_interval_minutes,
            _run_watch,
            _any_watched_feeds,
        ),
        Job(
            LETTERS,
            "Generate due newsletters",
            # Checked often enough that a 07:00 weekly lands close to 07:00.
            # This is the cadence db_can_autopause costs the job at, so the two
            # must not drift — hence the shared setting.
            lambda: news.newsletter_check_minutes,
            _run_newsletters,
            _any_scheduled_newsletters,
        ),
        Job(
            RETRIES,
            "Retry failed deliveries",
            lambda: 5,
            _run_retries,
            _any_pending_deliveries,
        ),
        Job(PRUNE, "Prune old articles", lambda: news.prune_interval_hours * 60, _run_prune, _always),
        Job(
            PRICES,
            "Refresh model prices from Azure",
            lambda: PRICE_REFRESH_MINUTES,
            _run_prices,
            _any_bound_meters,
        ),
        Job(
            LEARN,
            "Learn from the author's edits",
            lambda: get_settings().learning.interval_minutes,
            _run_learn,
            _any_captured_pairs,
        ),
    ]


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._owner = uuid.uuid4().hex[:16]
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        settings = get_settings().scheduler
        if self._started:
            return
        await self.sync_jobs()
        if not settings.enabled:
            logger.info("scheduler is off (PPN_SCHEDULER_ENABLED)")
            return
        self._started = True
        self._task = asyncio.create_task(self._loop())
        logger.info("scheduler started (owner %s)", self._owner)

    async def stop(self) -> None:
        self._started = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def wake(self) -> None:
        """Recompute the horizon now rather than at the next scheduled wake.

        Called by every write that changes a schedule — toggling a feed's watch
        flag, enabling or disabling one — so an edit takes effect immediately and
        the sleep between jobs can safely be long.
        """
        self._wake.set()

    # -- job rows ----------------------------------------------------------

    async def sync_jobs(self) -> None:
        """Create the job rows, and keep their intervals in step with settings.

        Idempotent, so it is safe on every boot. A job whose ``applies`` is false
        is parked with no due time rather than deleted, which keeps its history.
        """
        now = utcnow()
        async with session() as s:
            existing = {
                row.key: row for row in (await s.execute(select(SchedulerJob))).scalars()
            }
            for job in _jobs():
                applies = await job.applies()
                interval = max(1, job.interval_minutes())
                row = existing.get(job.key)
                if row is None:
                    s.add(
                        SchedulerJob(
                            key=job.key,
                            interval_minutes=interval,
                            enabled=applies,
                            # Due at once on a fresh database: the operator should
                            # not wait six hours to see the feature work.
                            next_due_at=now if applies else None,
                        )
                    )
                    continue
                row.interval_minutes = interval
                row.enabled = applies
                if applies and row.next_due_at is None:
                    row.next_due_at = now
                elif not applies:
                    row.next_due_at = None
            await s.commit()

    # -- the loop ----------------------------------------------------------

    async def _loop(self) -> None:
        settings = get_settings().scheduler
        ceiling = max(1, settings.max_sleep_minutes) * 60
        while True:
            try:
                delay = await self._seconds_until_due(ceiling)
                self._wake.clear()
                if delay > 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=delay)
                        continue  # woken by an edit: recompute rather than fire
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken tick must not kill the loop
                logger.exception("scheduler tick failed")
                await asyncio.sleep(60)

    async def _seconds_until_due(self, ceiling: float) -> float:
        async with session() as s:
            soonest = await s.scalar(
                select(SchedulerJob.next_due_at)
                .where(SchedulerJob.next_due_at.is_not(None))
                .order_by(SchedulerJob.next_due_at)
                .limit(1)
            )
        if soonest is None:
            return ceiling
        due = as_utc(soonest)
        assert due is not None
        return max(0.0, min(ceiling, (due - utcnow()).total_seconds()))

    async def tick_once(self) -> dict[str, Any]:
        """Run every job that is due. The unit the tests drive directly."""
        fired: list[str] = []
        for job in _jobs():
            claimed = await self._claim(job)
            if not claimed:
                continue
            fired.append(job.key)
            try:
                detail = await job.run()
                await self._finish(job.key, "ok", detail=detail)
                logger.info("scheduler: %s — %s", job.label, detail)
            except Exception as exc:  # noqa: BLE001
                await self._finish(job.key, "error", error=f"{type(exc).__name__}: {exc}")
                logger.exception("scheduler job %s failed", job.key)
        return {"fired": fired}

    async def _claim(self, job: Job) -> bool:
        """Take the tick, or find that someone else already did.

        The compare-and-swap is the whole point: two processes read the same
        ``next_due_at``, both try to move it forward, and exactly one UPDATE
        matches. Deliberately no ``SELECT FOR UPDATE`` — it would need different
        SQL per dialect, and this needs none.

        The new due time is computed from *now*, not from the old one, which is
        what makes a missed tick fire once instead of once per interval missed.
        """
        now = utcnow()
        lease = timedelta(minutes=max(1, get_settings().scheduler.lease_minutes))

        async with session() as s:
            row = (
                await s.execute(select(SchedulerJob).where(SchedulerJob.key == job.key))
            ).scalar_one_or_none()
            if row is None or not row.enabled or row.next_due_at is None:
                return False

            due = as_utc(row.next_due_at)
            if due is None or due > now:
                return False

            held = as_utc(row.lease_expires_at)
            if held is not None and held > now and row.lease_owner != self._owner:
                return False

            # The raw value as stored, not the as_utc() form — the WHERE has to
            # compare what the database actually holds.
            seen: datetime = row.next_due_at

            result = await s.execute(
                update(SchedulerJob)
                .where(SchedulerJob.key == job.key, SchedulerJob.next_due_at == seen)
                .values(
                    next_due_at=now + timedelta(minutes=max(1, job.interval_minutes())),
                    lease_owner=self._owner,
                    lease_expires_at=now + lease,
                    last_started_at=now,
                    runs=SchedulerJob.runs + 1,
                )
            )
            await s.commit()
            return bool(result.rowcount == 1)

    async def _finish(self, key: str, status: str, *, detail: str = "", error: str = "") -> None:
        async with session() as s:
            await s.execute(
                update(SchedulerJob)
                .where(SchedulerJob.key == key)
                .values(
                    last_finished_at=utcnow(),
                    last_status=status,
                    last_detail=detail[:300],
                    last_error=error[:500],
                    lease_owner="",
                    lease_expires_at=None,
                )
            )
            await s.commit()

    # -- read side ---------------------------------------------------------

    async def describe(self) -> dict[str, Any]:
        news = get_settings().news
        watched = await _watched_count()
        scheduled = await _scheduled_newsletter_count()
        labels = {job.key: job.label for job in _jobs()}
        async with session() as s:
            rows = list((await s.execute(select(SchedulerJob).order_by(SchedulerJob.key))).scalars())
        return {
            "enabled": get_settings().scheduler.enabled,
            "jobs": [
                {
                    "key": r.key,
                    "label": labels.get(r.key, r.key),
                    "enabled": r.enabled,
                    "interval_minutes": r.interval_minutes,
                    "next_due_at": _iso(as_utc(r.next_due_at)),
                    "last_finished_at": _iso(as_utc(r.last_finished_at)),
                    "last_status": r.last_status,
                    "last_detail": r.last_detail,
                    "last_error": r.last_error,
                    "runs": r.runs,
                }
                for r in rows
            ],
            "watched_feeds": watched,
            "scheduled_newsletters": scheduled,
            "effective_min_cadence_minutes": news.effective_min_cadence(
                watched_feeds=watched, scheduled_newsletters=scheduled
            ),
            # False means the polling cadence is holding Azure SQL awake around
            # the clock. Reported so the trade is visible where it is chosen —
            # and a scheduled newsletter counts, because the 15-minute *check*
            # for whether one is due is what touches the database.
            "db_can_autopause": news.db_can_autopause(
                watched_feeds=watched, scheduled_newsletters=scheduled
            ),
        }

    async def run_now(self, key: str) -> dict[str, Any]:
        """Force one job, ignoring its due time. Raises KeyError for a bad key."""
        job = next((j for j in _jobs() if j.key == key), None)
        if job is None:
            raise KeyError(f"No scheduler job {key!r}")
        detail = await job.run()
        await self._finish(job.key, "ok", detail=detail)
        return {"key": key, "detail": detail}


async def _scheduled_newsletter_count() -> int:
    from sqlalchemy import func, true

    from .db import Newsletter

    async with session() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(Newsletter)
            .where(Newsletter.enabled == true(), Newsletter.next_due_at.is_not(None))
        )
    return int(count or 0)


async def _watched_count() -> int:
    from sqlalchemy import func, true

    from .db import Feed

    async with session() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(Feed)
            .where(Feed.enabled == true(), Feed.realtime == true())
        )
    return int(count or 0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


_scheduler: Scheduler | None = None


def scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler


async def reset_scheduler() -> None:
    """Drop the singleton — used by tests between apps."""
    global _scheduler
    if _scheduler is not None:
        await _scheduler.stop()
    _scheduler = None
