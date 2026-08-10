"""Run registry, work queue and the live event stream.

Turning the CLI into a service needs three things the CLI never had:

* **A queue with a ceiling.** Every run costs real model calls, so runs are
  admitted by a fixed pool of workers (``PPN_MAX_CONCURRENT_RUNS``) rather than
  all starting at once.
* **A durable event log.** Everything the UI shows — node status, agent output,
  tool calls — is replayed from ``run_events`` and then followed live, so a
  browser opened halfway through a run still sees the whole history.
* **Per-run log capture.** The agents already log one line per tool call; a
  context-bound handler routes those lines to the run that produced them
  instead of a shared console.

The queue is deliberately behind :class:`RunManager` rather than spread through
the API, so replacing the in-process pool with an Azure Storage Queue later is
one class, not a rewrite.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update

from ..settings import get_settings
from .db import Run, RunEvent, session, utcnow

logger = logging.getLogger("ppn.server.runs")

QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
)
TERMINAL = {SUCCEEDED, FAILED, CANCELLED, INTERRUPTED}

# Set while a run executes, so log records can be attributed to it.
current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ppn_current_run_id", default=None
)


@dataclass
class LiveEvent:
    seq: int
    kind: str
    executor_id: str
    level: str
    message: str
    data: dict[str, Any] | None
    ts: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "executor_id": self.executor_id,
            "level": self.level,
            "message": self.message,
            "data": self.data,
            "ts": self.ts,
        }


@dataclass
class _RunChannel:
    """Per-run sequence counter and set of live subscribers."""

    seq: int = 0
    subscribers: set[asyncio.Queue] = field(default_factory=set)


class RunLogHandler(logging.Handler):
    """Routes ``ppn.*`` log records to whichever run is executing."""

    def __init__(self, manager: RunManager) -> None:
        super().__init__(level=logging.INFO)
        self.manager = manager

    def emit(self, record: logging.LogRecord) -> None:
        run_id = current_run_id.get()
        if not run_id:
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging break a run
            return
        self.manager.emit_threadsafe(
            run_id,
            kind="log",
            level=record.levelname.lower(),
            message=message[:4000],
            executor_id=getattr(record, "executor_id", "") or "",
            data={"logger": record.name},
        )


def _stream_text(data: Any) -> str:
    """Text delta from a streaming ``output`` event (an AgentResponseUpdate)."""
    text = getattr(data, "text", None)
    return text if isinstance(text, str) else ""


def _final_output_text(data: Any) -> str:
    """The agent's finished output from an ``executor_completed`` event.

    Completed events carry a list. An ``AgentExecutor`` yields a single
    ``AgentExecutorResponse`` whose ``agent_response.text`` is the full result;
    the publisher yields a structured Pydantic object; a routing gate yields an
    ``AgentExecutorRequest`` envelope we deliberately surface nothing for. Duck
    typed on purpose — the point of this module is to not import Agent Framework
    internals just to read a string off them.
    """
    item = data[-1] if isinstance(data, (list, tuple)) and data else data
    agent_response = getattr(item, "agent_response", None)
    if agent_response is not None:
        text = getattr(agent_response, "text", None)
        if isinstance(text, str):
            return text
    dump = getattr(item, "model_dump_json", None)
    if callable(dump):
        try:
            return dump(indent=2)
        except Exception:  # noqa: BLE001 - never let observability break a run
            return ""
    if isinstance(item, str):
        return item
    return ""


class RunManager:
    """Owns the queue, the workers and the event fan-out."""

    # Streamed agent output is flushed in chunks this size, so the UI shows text
    # as it is produced without emitting one event per token.
    _OUTPUT_CHUNK = 1200

    def __init__(self, concurrency: int | None = None) -> None:
        self.concurrency = concurrency or int(os.environ.get("PPN_MAX_CONCURRENT_RUNS", "2"))
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._channels: dict[str, _RunChannel] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handler: RunLogHandler | None = None
        self._started = False
        # Every run_events row goes through this queue and one writer task.
        self._writes: asyncio.Queue | None = None
        self._writer: asyncio.Task | None = None
        # In-flight push notifications, held so shutdown can drain them.
        self._notifications: set[asyncio.Task] = set()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        await self._recover_orphans()
        self._writes = asyncio.Queue()
        self._writer = asyncio.create_task(self._event_writer(), name="ppn-event-writer")
        self._handler = RunLogHandler(self)
        logging.getLogger("ppn").addHandler(self._handler)
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"ppn-worker-{i}")
            for i in range(self.concurrency)
        ]
        self._started = True
        logger.info("run manager started with %d workers", self.concurrency)

    async def stop(self) -> None:
        # Detach the handler first so nothing new is queued while we drain.
        if self._handler is not None:
            logging.getLogger("ppn").removeHandler(self._handler)
            self._handler = None
        self._started = False

        for task in self._workers:
            task.cancel()
        for task in list(self._tasks.values()):
            task.cancel()

        outstanding = [*self._workers, *self._tasks.values()]
        if outstanding:
            await asyncio.gather(*outstanding, return_exceptions=True)

        # Give notifications a moment to land rather than cancelling them: this
        # runs on the way to a deploy, and "your draft is ready" arriving is the
        # whole point. Bounded, because a wedged push service must not hold the
        # container open.
        if self._notifications:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.gather(*self._notifications, return_exceptions=True), timeout=10
                )
            for task in list(self._notifications):
                task.cancel()
            self._notifications.clear()

        # Drain queued writes before the engine goes away, then stop the writer.
        if self._writes is not None and self._writer is not None:
            self._writes.put_nowait(None)
            try:
                await asyncio.wait_for(self._writer, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._writer.cancel()

        self._workers.clear()
        self._tasks.clear()
        self._writes = None
        self._writer = None

    async def _recover_orphans(self) -> None:
        """A crash leaves rows claiming to be running. Mark them honestly."""
        async with session() as s:
            result = await s.execute(
                update(Run)
                .where(Run.status.in_([QUEUED, RUNNING]))
                .values(status=INTERRUPTED, finished_at=utcnow(),
                        error="Server restarted while this run was in flight.")
            )
            if result.rowcount:
                logger.warning("marked %d interrupted run(s) from a previous process",
                               result.rowcount)
            await s.commit()

    # -- enqueue / cancel --------------------------------------------------

    async def enqueue(self, kind: str, params: dict[str, Any], label: str = "") -> str:
        run_id = str(uuid.uuid4())
        from .config_store import active_source

        async with session() as s:
            s.add(
                Run(
                    id=run_id,
                    kind=kind,
                    status=QUEUED,
                    label=label[:300],
                    params=params,
                    config_version=active_source().version_token()[:64],
                    queued_at=utcnow(),
                )
            )
            await s.commit()
        self.emit(run_id, kind="status", message=f"Queued ({kind})", data={"status": QUEUED})
        await self._queue.put(run_id)
        return run_id

    async def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            return True
        async with session() as s:
            run = await s.get(Run, run_id)
            if run is None or run.status in TERMINAL:
                return False
            run.status = CANCELLED
            run.finished_at = utcnow()
            await s.commit()
        self.emit(run_id, kind="status", message="Cancelled before it started",
                  data={"status": CANCELLED})
        await self.flush()
        return True

    # -- events ------------------------------------------------------------

    def _channel(self, run_id: str) -> _RunChannel:
        return self._channels.setdefault(run_id, _RunChannel())

    def emit(
        self,
        run_id: str,
        *,
        kind: str,
        message: str = "",
        executor_id: str = "",
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Record an event: assign a sequence number, fan out live, queue the write.

        Synchronous on purpose. Persisting inline meant one SQLite connection per
        event, opened from whichever task happened to be running — and a task
        cancelled mid-write leaked its connection, which then outlived engine
        disposal and blocked shutdown. Live subscribers still see the event
        immediately; only the disk write is deferred to a single writer.
        """
        channel = self._channel(run_id)
        channel.seq += 1
        event = LiveEvent(
            seq=channel.seq,
            kind=kind,
            executor_id=executor_id,
            level=level,
            message=message,
            data=data,
            ts=utcnow().isoformat(),
        )
        for queue in list(channel.subscribers):
            queue.put_nowait(event)
        if self._writes is not None:
            self._writes.put_nowait((run_id, event))

    def emit_threadsafe(self, run_id: str, **kwargs: Any) -> None:
        """Emit from a logging handler, which may run off the event loop."""
        if not self._started:
            return
        try:
            asyncio.get_running_loop()
            self.emit(run_id, **kwargs)
        except RuntimeError:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(lambda: self.emit(run_id, **kwargs))

    async def flush(self, timeout: float = 10.0) -> None:
        """Wait until every queued event has been written.

        Called before a run is marked terminal, so that anything reading a
        finished run — `GET /runs/{id}`, an SSE replay — is guaranteed a
        complete event log rather than whatever happened to be flushed.
        """
        if self._writes is None:
            return
        try:
            await asyncio.wait_for(self._writes.join(), timeout=timeout)
        except TimeoutError:
            logger.warning("event log flush timed out; some events may be missing")

    async def _event_writer(self) -> None:
        """The only thing that writes run_events. One connection, ordered writes."""
        import sys

        while True:
            item = await self._writes.get()
            try:
                if item is None:
                    return
                run_id, event = item
                async with session() as s:
                    s.add(
                        RunEvent(
                            run_id=run_id,
                            seq=event.seq,
                            kind=event.kind,
                            executor_id=event.executor_id,
                            level=event.level,
                            message=event.message,
                            data=event.data,
                            ts=utcnow(),
                        )
                    )
                    await s.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the run matters more than its log
                # Never logged through `ppn.*`: that re-enters the handler that
                # produced this event and recurses.
                print(f"[ppn] could not persist run event: {exc}", file=sys.stderr)
            finally:
                self._writes.task_done()

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._channel(run_id).subscribers.add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        self._channel(run_id).subscribers.discard(queue)

    async def replay(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        async with session() as s:
            rows = await s.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > after)
                .order_by(RunEvent.seq)
            )
            return [
                LiveEvent(
                    seq=r.seq, kind=r.kind, executor_id=r.executor_id, level=r.level,
                    message=r.message, data=r.data, ts=r.ts.isoformat() if r.ts else "",
                ).to_dict()
                for r in rows.scalars()
            ]

    # -- execution ---------------------------------------------------------

    async def _worker(self, index: int) -> None:
        while True:
            run_id = await self._queue.get()
            try:
                async with session() as s:
                    run = await s.get(Run, run_id)
                    if run is None or run.status != QUEUED:
                        continue  # cancelled while waiting
                task = asyncio.create_task(self._execute(run_id))
                self._tasks[run_id] = task
                await task
            except asyncio.CancelledError:
                self.emit(run_id, kind="status", message="Cancelled",
                          data={"status": CANCELLED})
                await self.flush()
                await self._finish(run_id, CANCELLED, error="Cancelled")
            except Exception as exc:  # noqa: BLE001 - one bad run must not kill the worker
                logger.exception("run %s failed", run_id)
                self.emit(run_id, kind="status", level="error",
                          message=f"Failed: {exc}"[:500], data={"status": FAILED})
                await self.flush()
                await self._finish(run_id, FAILED, error=f"{type(exc).__name__}: {exc}"[:2000])
            finally:
                self._tasks.pop(run_id, None)
                self._queue.task_done()

    async def _execute(self, run_id: str) -> None:
        async with session() as s:
            run = await s.get(Run, run_id)
            if run is None:
                return
            run.status = RUNNING
            run.started_at = utcnow()
            kind, params = run.kind, dict(run.params or {})
            await s.commit()

        self.emit(run_id, kind="status", message="Running", data={"status": RUNNING})
        token = current_run_id.set(run_id)
        try:
            result = await self._dispatch(run_id, kind, params)
            # Index the artefacts (topic ideas / posts / versions). Bookkeeping
            # must never sink a finished run — the run matters more than its row.
            try:
                from . import catalog

                await catalog.record_run_result(run_id, kind, params, result)
            except Exception:  # noqa: BLE001
                logger.exception("catalog population failed for run %s", run_id)
            self.emit(run_id, kind="status", message="Finished",
                      data={"status": SUCCEEDED, "result": result})
            # Flush before the row goes terminal: readers that see "succeeded"
            # must also see every event that produced it.
            await self.flush()
            await self._finish(run_id, SUCCEEDED, result=result)
        finally:
            current_run_id.reset(token)

    def _on_event(self, run_id: str) -> Callable[[Any], None]:
        """Bridge workflow events into the run's event stream.

        The workflow emits, per executor, one ``executor_invoked``, a run of
        streamed ``output`` deltas, and one ``executor_completed`` carrying the
        finished result. We surface all three so a node not only lights up but
        shows what the agent actually researched, wrote or decided:

        * ``executor_invoked`` → a ``node`` event that lights the node.
        * ``output`` → the streamed text, accumulated and flushed in ~1 KB
          ``log`` chunks so the panel fills in as the agent produces it, rather
          than one event per token (the old behaviour emitted thousands).
        * ``executor_completed`` → a ``node`` event whose ``data.output`` is the
          agent's full, clean result.

        Everything is keyed by ``executor_id`` so it folds onto the right node,
        and ``derive_nodes`` still counts these exactly as before.
        """
        pending: dict[str, str] = {}

        def flush(executor_id: str) -> None:
            text = pending.pop(executor_id, "")
            if text:
                self.emit_threadsafe(
                    run_id, kind="log", executor_id=executor_id,
                    message=text[:4000], data={"type": "output"},
                )

        def handler(event: Any) -> None:
            executor_id = getattr(event, "executor_id", "") or ""
            if not executor_id:
                return
            etype = str(getattr(event, "type", "") or "")
            data = getattr(event, "data", None)

            if etype == "executor_invoked":
                self.emit_threadsafe(
                    run_id, kind="node", executor_id=executor_id,
                    message=f"{executor_id} started", data={"type": etype},
                )
            elif etype == "output":
                text = _stream_text(data)
                if text:
                    pending[executor_id] = pending.get(executor_id, "") + text
                    if len(pending[executor_id]) >= self._OUTPUT_CHUNK:
                        flush(executor_id)
            elif etype == "executor_completed":
                flush(executor_id)
                output = _final_output_text(data)
                self.emit_threadsafe(
                    run_id, kind="node", executor_id=executor_id,
                    message=f"{executor_id} completed",
                    data={"type": etype, "output": output[:12000]},
                )

        return handler

    async def _dispatch(self, run_id: str, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        from ..models import TopicSuggestion
        from ..workflows import discover_topics, write_post

        settings = get_settings()

        default_instruction = (
            "Find what is worth writing about on the Power Platform Ninja blog right now."
        )

        if kind == "suggest":
            instruction = params.get("instruction") or default_instruction
            result = await discover_topics(instruction, on_event=self._on_event(run_id))
            return {
                "suggestions": [s.model_dump(mode="json") for s in result.suggestions],
                "generated_on": result.generated_on,
            }

        if kind == "explore":
            # Half a topic run: the scouts sweep wide and stop. The result is
            # deliberately thin — counts and a review id — because the candidate
            # list itself lives in source_reviews, which is what the operator
            # acts on and what the follow-up run reads.
            from ..workflows import explore_sources
            from . import reviews

            instruction = params.get("instruction") or default_instruction
            review = await explore_sources(instruction, on_event=self._on_event(run_id))
            review_id = await reviews.create(run_id, review)
            return {
                "awaiting_source_approval": True,
                "review_id": review_id,
                "generated_on": review.generated_on,
                "candidate_count": len(review.candidates),
                "new_count": sum(1 for c in review.candidates if not c.known),
                "signal_count": sum(c.item_count for c in review.candidates),
            }

        if kind == "shortlist":
            from ..workflows import shortlist_from_sources
            from . import reviews

            review_id = int(params["review_id"])
            reports, approved, instruction = await reviews.material(review_id)
            if not approved:
                raise RuntimeError(
                    f"Source review {review_id} approved no sites — nothing to build a shortlist from."
                )
            result = await shortlist_from_sources(
                reports,
                approved,
                instruction=params.get("instruction") or instruction,
                on_event=self._on_event(run_id),
            )
            return {
                "suggestions": [s.model_dump(mode="json") for s in result.suggestions],
                "generated_on": result.generated_on,
                "review_id": review_id,
                "approved_sources": approved,
            }

        if kind == "write":
            topic = TopicSuggestion.model_validate(params["topic"])
            instructions = params.get("instructions") or ""
            # A regeneration carries the whole guidance history, not just the
            # newest note, so earlier revision requests keep applying. Only the
            # new note is stored per version (record_write_result); the fold of
            # all prior guidance happens here, for both reuse- and fresh-research.
            if params.get("post_id") is not None:
                from . import catalog

                instructions = catalog.accumulate_guidance(
                    await catalog.prior_guidance(params["post_id"]), instructions
                )
            common = dict(
                push_to_wordpress=params.get("push"),
                make_cover=params.get("cover"),
                translate=params.get("translate"),
                on_event=self._on_event(run_id),
            )
            # A regeneration reusing prior research: load the saved dossier and
            # skip the (already-passed) source check, so it costs only the write.
            if params.get("reuse_research") and params.get("post_id") is not None:
                from ..storage import load_dossier
                from ..workflows import write_post_from_dossier
                from . import catalog

                _topic, dossier_path = await catalog.post_topic_and_dossier(params["post_id"])
                if dossier_path is None:
                    raise RuntimeError(
                        "No saved research for this post — regenerate with fresh research instead."
                    )
                dossier = load_dossier(dossier_path)
                package = await write_post_from_dossier(
                    topic,
                    dossier,
                    skip_source_check=True,
                    extra_instructions=instructions,
                    dossier_path=str(dossier_path),
                    **common,
                )
            else:
                package = await write_post(topic, extra_instructions=instructions, **common)
            return {
                "title": package.draft.title,
                "slug": package.draft.slug,
                "approved": package.outcome.approved,
                "score": package.outcome.overall_score,
                "blockers": package.outcome.blockers,
                "markdown_path": package.markdown_path,
                "report_path": package.report_path,
                "cover_path": package.cover.path if package.cover else "",
                "cover_error": package.cover.error if package.cover else "",
                "dossier_path": package.dossier_path,
                "translation_path": package.translation_path,
                "post_id": package.published.post_id if package.published else None,
                "edit_link": package.published.edit_link if package.published else "",
                "link": package.published.link if package.published else "",
            }

        if kind == "cover":
            from pathlib import Path

            from ..covers import build_cover
            from .drafts import draft_to_model

            draft = draft_to_model(Path(params["path"]), concept=params.get("concept", ""))
            cover = await build_cover(draft, settings)
            return cover.model_dump(mode="json")

        if kind == "newsletter":
            # Costs model calls and takes minutes, so it belongs in the queue
            # like any other run — same log, same cancellation, same history.
            from .newsletter_runs import compose_and_store

            return await asyncio.wait_for(
                compose_and_store(
                    int(params["newsletter_id"]),
                    run_id=run_id,
                    instruction=params.get("instruction", ""),
                    on_event=self._on_event(run_id),
                ),
                timeout=settings.news.newsletter_timeout_minutes * 60,
            )

        if kind == "ingest":
            # No model calls at all — this is HTTP and SQL. It runs as a run
            # anyway so a scheduled sweep is visible in the same list as
            # everything else, with the same log, cancellation and history.
            #
            # The timeout is applied here rather than left to the manager
            # because the manager applies none: suggest/write set theirs in
            # cli.py, so a server-side run has no wall-clock ceiling at all and
            # one unresponsive host could hold a worker forever.
            from .ingest import ingest

            return await asyncio.wait_for(
                ingest(
                    feed_ids=params.get("feed_ids"),
                    only_realtime=bool(params.get("only_realtime")),
                    only_due=bool(params.get("only_due")),
                ),
                timeout=settings.news.ingest_timeout_minutes * 60,
            )

        raise ValueError(f"Unknown run kind: {kind}")

    async def _finish(
        self, run_id: str, status: str, result: dict[str, Any] | None = None, error: str = ""
    ) -> None:
        async with session() as s:
            run = await s.get(Run, run_id)
            if run is None:
                return
            run.status = status
            run.result = result
            run.error = error
            run.finished_at = utcnow()
            label = run.label or ""
            kind = run.kind
            await s.commit()

        # The single place a run goes terminal, which is why the notification
        # hangs here rather than at the four call sites: succeeded, failed,
        # cancelled and interrupted all pass through, and only here is the whole
        # row loaded — `_execute` drops the label before it gets this far.
        #
        # Detached and after the commit, deliberately. The serverless SQL tier
        # auto-pauses after an hour idle and takes 20-30s to wake, and a push
        # service is network I/O to a third party; neither may hold up the row
        # that says a forty-minute run succeeded.
        self._spawn_notify(kind, status, label, result)

    def _spawn_notify(
        self, kind: str, status: str, label: str, result: dict[str, Any] | None
    ) -> None:
        from . import push

        task = asyncio.create_task(
            push.notify_run_finished(kind, status, label, result), name=f"ppn-notify-{kind}"
        )
        # Held so stop() can drain it and so the loop keeps a strong reference —
        # an untracked task can be garbage-collected mid-flight.
        self._notifications.add(task)
        task.add_done_callback(self._notifications.discard)


_manager: RunManager | None = None


def manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager()
    return _manager


async def reset_manager() -> None:
    global _manager
    if _manager is not None:
        await _manager.stop()
    _manager = None
