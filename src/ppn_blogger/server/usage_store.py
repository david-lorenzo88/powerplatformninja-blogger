"""Persisting and aggregating what runs cost.

The write side is deliberately fire-and-forget. ``usage.Ledger`` calls a *sync*
sink from inside agent middleware, which may be running anywhere in the workflow
graph, so the sink hands the row to a background task rather than blocking an
agent on a database round trip. Losing a usage row is an accounting gap; blocking
the writer on a slow database, or raising inside middleware, would cost the run
itself. The trade is made in that direction on purpose.

The read side is plain SQL aggregation so a month of spend is one query rather
than a month of rows pulled into Python.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import Date, case, cast, false, func, select, true

from ..usage import Ledger, UsageRecord, price_record
from .db import Run, RunUsage, as_utc, engine, session, utcnow

logger = logging.getLogger("ppn.server.usage")

# Background writes in flight. Held so they are not garbage collected mid-flight,
# and so shutdown can wait for them.
_pending: set[asyncio.Task[None]] = set()


def ledger_for(run_id: str, prices: dict[str, Any] | None) -> Ledger:
    """A ledger that costs each record as it arrives and files it against a run.

    Pricing happens at write time, not at read time, so the figure on a run is
    what it cost *then*. A later price edit moves future runs and leaves history
    alone — which is what makes the unattended price refresh safe.
    """

    def sink(item: UsageRecord) -> None:
        micros, priced = price_record(item, prices or {})
        row = RunUsage(
            run_id=run_id,
            agent_id=item.agent_id,
            model=item.model,
            kind=item.kind,
            input_tokens=item.input_tokens,
            output_tokens=item.output_tokens,
            cached_input_tokens=item.cached_input_tokens,
            reasoning_tokens=item.reasoning_tokens,
            total_tokens=item.total_tokens,
            searches=item.searches,
            images=item.images,
            cost_micros=micros,
            currency=str((prices or {}).get("currency") or ""),
            priced=priced,
            created_at=utcnow(),
        )
        _schedule(row)

    return Ledger(sink=sink)


def _schedule(row: RunUsage) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - middleware always runs in a loop
        return
    task = loop.create_task(_write(row))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _write(row: RunUsage) -> None:
    try:
        async with session() as s:
            s.add(row)
            await s.commit()
    except Exception:  # noqa: BLE001 - an accounting gap, never a failed run
        logger.exception("could not record usage for run %s", row.run_id)


async def drain() -> None:
    """Wait for in-flight writes. Called before a run is marked terminal.

    Without this a fast run can finish and be read back before its last rows
    land, and the UI would show a completed run with a cost still climbing.
    """
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

_TOTALS = (
    func.coalesce(func.sum(RunUsage.input_tokens), 0),
    func.coalesce(func.sum(RunUsage.output_tokens), 0),
    func.coalesce(func.sum(RunUsage.cached_input_tokens), 0),
    func.coalesce(func.sum(RunUsage.reasoning_tokens), 0),
    func.coalesce(func.sum(RunUsage.total_tokens), 0),
    func.coalesce(func.sum(RunUsage.searches), 0),
    func.coalesce(func.sum(RunUsage.images), 0),
    func.coalesce(func.sum(RunUsage.cost_micros), 0),
    func.count(),
    # Whether every row in the group carried a price. Aggregated as an integer
    # rather than with MIN over the boolean column: SQL Server refuses MIN on a
    # `bit`, and a group that is partly unpriced must read as unpriced, since
    # its total is then a floor rather than a sum.
    func.min(case((RunUsage.priced == true(), 1), else_=0)),
)

_TOTALS_WIDTH = len(_TOTALS)


def _totals_dict(row: Any) -> dict[str, Any]:
    return {
        "input_tokens": int(row[0] or 0),
        "output_tokens": int(row[1] or 0),
        "cached_input_tokens": int(row[2] or 0),
        "reasoning_tokens": int(row[3] or 0),
        "total_tokens": int(row[4] or 0),
        "searches": int(row[5] or 0),
        "images": int(row[6] or 0),
        "cost_micros": int(row[7] or 0),
        "records": int(row[8] or 0),
        "priced": bool(row[9]),
    }


async def for_run(run_id: str) -> dict[str, Any] | None:
    """Totals for one run, or None when nothing was metered."""
    async with session() as s:
        row = (
            await s.execute(
                select(*_TOTALS, func.min(RunUsage.currency)).where(RunUsage.run_id == run_id)
            )
        ).one()
        if not row[8]:
            return None
        # Any single unpriced row makes the run's total an undercount, so the
        # whole figure is flagged rather than quietly reported as complete.
        unpriced = (
            await s.execute(
                select(RunUsage.model)
                .where(RunUsage.run_id == run_id, RunUsage.priced == false())
                .distinct()
            )
        ).scalars().all()

    out = _totals_dict(row)
    out["currency"] = row[_TOTALS_WIDTH] or ""
    out["priced"] = not unpriced
    out["unpriced_models"] = sorted(m for m in unpriced if m)
    return out


async def for_runs(run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Totals for many runs in one query.

    The runs list renders fifty rows; fifty round trips to show a number beside
    each would be a worse bargain than the number is worth.
    """
    if not run_ids:
        return {}
    async with session() as s:
        rows = (
            await s.execute(
                select(RunUsage.run_id, *_TOTALS, func.min(RunUsage.currency))
                .where(RunUsage.run_id.in_(run_ids))
                .group_by(RunUsage.run_id)
            )
        ).all()
        unpriced = set(
            (
                await s.execute(
                    select(RunUsage.run_id)
                    .where(RunUsage.run_id.in_(run_ids), RunUsage.priced == false())
                    .distinct()
                )
            )
            .scalars()
            .all()
        )

    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = _totals_dict(row[1:])
        entry["currency"] = row[1 + _TOTALS_WIDTH] or ""
        entry["priced"] = row[0] not in unpriced
        out[row[0]] = entry
    return out


async def by_agent(run_id: str) -> list[dict[str, Any]]:
    """Per-agent breakdown — which stage of the run was expensive."""
    async with session() as s:
        rows = (
            await s.execute(
                select(RunUsage.agent_id, RunUsage.model, RunUsage.kind, *_TOTALS)
                .where(RunUsage.run_id == run_id)
                .group_by(RunUsage.agent_id, RunUsage.model, RunUsage.kind)
                .order_by(func.sum(RunUsage.cost_micros).desc())
            )
        ).all()
    return [
        {"agent_id": r[0], "model": r[1], "kind": r[2], **_totals_dict(r[3:])} for r in rows
    ]


def day_bucket(column: Any, dialect: str = "") -> Any:
    """Truncate a timestamp to a calendar day, in each dialect's own spelling.

    There is no portable spelling, and getting it wrong fails differently on
    each side — which is why this is a branch rather than one clever expression:

    * SQL Server has **no `DATE()` function** and rejects the whole statement:
      *'date' is not a recognized built-in function name*. Loud, and it took a
      merge to `main` to find, because the local suite runs on SQLite.
    * SQLite is worse. `CAST(x AS DATE)` is not an error there — "DATE" carries
      no type-affinity keyword, so it gets NUMERIC affinity and the cast yields
      the **year as an integer**. Every day of a year would collapse into one
      bucket, silently, with no failure anywhere.

    ``dialect`` is injectable so both branches can be compiled in a test without
    a live database.
    """
    name = dialect or engine().dialect.name
    if name == "sqlite":
        return func.date(column)  # sqlite-only: the whole point of this branch
    return cast(column, Date)


async def rollup(
    since: datetime | None = None,
    until: datetime | None = None,
    group_by: str = "day",
) -> list[dict[str, Any]]:
    """Spend over time, grouped by calendar day or by run kind.

    Grouped in the database rather than in Python: a year of runs is tens of
    thousands of rows and the answer is a few dozen.
    """
    async with session() as s:
        if group_by == "kind":
            key: Any = Run.kind
            stmt = select(key, *_TOTALS).join(Run, Run.id == RunUsage.run_id)
        else:
            key = day_bucket(RunUsage.created_at)
            stmt = select(key, *_TOTALS)

        if since is not None:
            stmt = stmt.where(RunUsage.created_at >= since)
        if until is not None:
            stmt = stmt.where(RunUsage.created_at <= until)

        rows = (await s.execute(stmt.group_by(key).order_by(key))).all()

    return [{"key": str(r[0]), **_totals_dict(r[1:])} for r in rows]


async def most_expensive(limit: int = 10, since: datetime | None = None) -> list[dict[str, Any]]:
    async with session() as s:
        stmt = (
            select(
                RunUsage.run_id,
                Run.kind,
                Run.label,
                Run.finished_at,
                func.coalesce(func.sum(RunUsage.cost_micros), 0).label("cost"),
                func.coalesce(func.sum(RunUsage.total_tokens), 0),
            )
            .join(Run, Run.id == RunUsage.run_id)
            .group_by(RunUsage.run_id, Run.kind, Run.label, Run.finished_at)
            .order_by(func.coalesce(func.sum(RunUsage.cost_micros), 0).desc())
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(RunUsage.created_at >= since)
        rows = (await s.execute(stmt)).all()

    return [
        {
            "run_id": r[0],
            "kind": r[1],
            "label": r[2],
            "finished_at": (as_utc(r[3]).isoformat() if r[3] else None),
            "cost_micros": int(r[4] or 0),
            "total_tokens": int(r[5] or 0),
        }
        for r in rows
    ]
