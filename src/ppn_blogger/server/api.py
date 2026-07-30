"""HTTP API.

Contract notes for the frontend:

* Every run is observed through ``GET /api/runs/{id}/events`` — an SSE stream
  that **replays from ``?after=<seq>`` first, then follows live**. A browser
  opened mid-run therefore sees the full history, and reconnecting after a drop
  costs nothing but the last sequence number.
* Node status for the canvas is derived from the same event log
  (``GET /api/runs/{id}`` returns ``nodes``), so the graph and the log can never
  disagree.
* The graph itself comes from ``WorkflowViz`` on the real workflow object, so it
  cannot drift from the code.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from ..settings import get_settings
from . import catalog, config_store, drafts
from .db import Run, session
from .runs import TERMINAL, manager

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Health and settings
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "ok": True,
        "language": settings.language,
        "concurrency": manager().concurrency,
        "cover": {
            "enabled": settings.cover.enabled,
            "model": settings.cover.model,
            "route": settings.cover.route,
            "configured": settings.cover.is_configured,
        },
        "wordpress": {"configured": settings.wordpress.is_configured, "url": settings.wordpress.url},
        "search": {"provider": settings.search.provider, "configured": settings.search.is_configured},
        "translation": {"enabled": settings.translation.enabled},
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class ConfigWrite(BaseModel):
    content: str
    note: str = ""


@router.get("/config")
async def list_config() -> list[dict[str, Any]]:
    newest = await config_store.latest_versions()
    return [
        {
            "name": name,
            "format": row.format,
            "version": row.version,
            "note": row.note,
            "updated_at": row.created_at.isoformat(),
        }
        for name, row in sorted(newest.items())
    ]


@router.get("/config/{name}")
async def get_config(name: str) -> dict[str, Any]:
    newest = (await config_store.latest_versions()).get(name)
    if newest is None:
        raise HTTPException(404, f"No config document named {name!r}")
    return {
        "name": name,
        "format": newest.format,
        "version": newest.version,
        "content": newest.content,
        "updated_at": newest.created_at.isoformat(),
    }


@router.put("/config/{name}")
async def put_config(name: str, body: ConfigWrite) -> dict[str, Any]:
    try:
        row = await config_store.save_document(name, body.content, body.note)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        # Invalid YAML is a user error, not a server error — say so precisely.
        raise HTTPException(422, str(exc)) from exc
    return {"name": name, "version": row.version, "updated_at": row.created_at.isoformat()}


@router.get("/config/{name}/history")
async def config_history(name: str) -> list[dict[str, Any]]:
    return [
        {
            "version": row.version,
            "note": row.note,
            "created_at": row.created_at.isoformat(),
            "size": len(row.content),
        }
        for row in await config_store.history(name)
    ]


@router.get("/config/{name}/versions/{version}")
async def config_version(name: str, version: int) -> dict[str, Any]:
    row = await config_store.get_version(name, version)
    if row is None:
        raise HTTPException(404, f"{name} has no version {version}")
    return {"name": name, "version": row.version, "content": row.content, "note": row.note}


@router.post("/config/{name}/rollback/{version}")
async def config_rollback(name: str, version: int) -> dict[str, Any]:
    try:
        row = await config_store.rollback(name, version)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"name": name, "version": row.version}


@router.post("/config/reload")
async def reload_config(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """Re-import the image's ``config/`` files into the database as new versions.

    The database is authoritative once the server has booted, so a code deploy that
    ships new ``config/`` (a new editorial ruleset, say) does not reach a running
    instance on its own. This is the ``ppn config reload`` path as an endpoint, so a
    CI job can trigger it after rolling the image.

    Machine-to-machine, not a user action: it is guarded by a bearer token
    (``PPN_ADMIN_TOKEN``) rather than the interactive Entra login, and is **disabled**
    unless that variable is set. Exclude *only* this path from Easy Auth so CI can
    reach it; the token is what keeps it safe.
    """
    expected = os.environ.get("PPN_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Config reload is disabled. Set PPN_ADMIN_TOKEN to enable it.")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(401, "Invalid or missing admin token.")

    # First boot after a fresh database still needs the initial seed; after that,
    # append the current files as a new version of each document.
    if await config_store.seed_from_yaml_if_empty():
        await config_store.refresh_active_source()
        results = [(name, 1) for name in config_store.DOCUMENTS]
    else:
        results = await config_store.reimport_from_yaml(
            note="Reloaded via POST /api/config/reload"
        )
    return {"reloaded": [{"name": n, "version": v} for n, v in results]}


# ---------------------------------------------------------------------------
# Workflow graphs (the canvas)
# ---------------------------------------------------------------------------


@router.get("/workflows")
async def workflows() -> list[dict[str, Any]]:
    """The workflow graphs, rendered from the real workflow objects.

    Built with the offline stub client: the graph topology is identical either
    way, and drawing a diagram should never need Azure credentials or cost a
    token. This endpoint therefore works before anything is configured.
    """
    from agent_framework import WorkflowViz

    from ..testing import stub_clients
    from ..workflows import build_post_workflow, build_topic_discovery_workflow

    clients = stub_clients()
    built = [
        ("suggest", "Topic discovery", build_topic_discovery_workflow(clients=clients).workflow),
        ("write", "Post pipeline", build_post_workflow(clients=clients).workflow),
    ]
    out = []
    for kind, title, workflow in built:
        viz = WorkflowViz(workflow)
        out.append(
            {
                "kind": kind,
                "title": title,
                "mermaid": viz.to_mermaid(),
                "nodes": sorted(workflow.executors) if hasattr(workflow, "executors") else [],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class SuggestRequest(BaseModel):
    instruction: str = ""
    label: str = ""


class WriteRequest(BaseModel):
    topic: dict[str, Any]
    push: bool | None = None
    cover: bool | None = None
    translate: bool | None = None
    label: str = ""


class CoverRequest(BaseModel):
    path: str
    concept: str = ""


def _run_dict(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "kind": run.kind,
        "status": run.status,
        "label": run.label,
        "params": run.params,
        "result": run.result,
        "error": run.error,
        "config_version": run.config_version,
        "queued_at": run.queued_at.isoformat() if run.queued_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.post("/runs/suggest", status_code=202)
async def start_suggest(body: SuggestRequest) -> dict[str, str]:
    run_id = await manager().enqueue(
        "suggest", {"instruction": body.instruction}, body.label or "Topic discovery"
    )
    return {"id": run_id}


@router.post("/runs/write", status_code=202)
async def start_write(body: WriteRequest) -> dict[str, str]:
    label = body.label or body.topic.get("title", "Post")
    run_id = await manager().enqueue(
        "write",
        {"topic": body.topic, "push": body.push, "cover": body.cover, "translate": body.translate},
        label,
    )
    return {"id": run_id}


@router.post("/runs/cover", status_code=202)
async def start_cover(body: CoverRequest) -> dict[str, str]:
    run_id = await manager().enqueue(
        "cover", {"path": body.path, "concept": body.concept}, f"Cover · {body.path}"
    )
    return {"id": run_id}


@router.get("/runs")
async def list_runs(
    status: str | None = None, limit: int = Query(50, le=200)
) -> list[dict[str, Any]]:
    async with session() as s:
        stmt = select(Run).order_by(desc(Run.queued_at)).limit(limit)
        if status:
            stmt = stmt.where(Run.status == status)
        return [_run_dict(r) for r in (await s.execute(stmt)).scalars()]


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    async with session() as s:
        run = await s.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "No such run")
    events = await manager().replay(run_id)
    return {**_run_dict(run), "nodes": derive_nodes(events), "event_count": len(events)}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, bool]:
    return {"cancelled": await manager().cancel(run_id)}


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, after: int = 0) -> StreamingResponse:
    """SSE: replay everything after ``after``, then follow live."""
    async with session() as s:
        run = await s.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "No such run")

    mgr = manager()
    queue = mgr.subscribe(run_id)

    async def stream():
        try:
            last = after
            for event in await mgr.replay(run_id, after=after):
                last = event["seq"]
                yield _sse(event)

            async with session() as s:
                current = await s.get(Run, run_id)
            if current is not None and current.status in TERMINAL:
                yield _sse({"kind": "eof", "status": current.status})
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"  # stops proxies closing an idle stream
                    continue
                if event.seq <= last:
                    continue
                last = event.seq
                yield _sse(event.to_dict())
                if event.kind == "status" and (event.data or {}).get("status") in TERMINAL:
                    yield _sse({"kind": "eof", "status": (event.data or {}).get("status")})
                    return
        finally:
            mgr.unsubscribe(run_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def derive_nodes(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-executor status for the canvas, derived from the event log.

    Deriving rather than storing means the graph can never disagree with the
    log, and a replayed run animates exactly like a live one.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for event in events:
        executor_id = event.get("executor_id")
        if not executor_id:
            continue
        node = nodes.setdefault(
            executor_id,
            {"id": executor_id, "events": 0, "logs": 0, "first_seen": event["ts"],
             "last_seen": event["ts"], "status": "pending"},
        )
        node["events"] += 1
        node["last_seen"] = event["ts"]
        node["status"] = "active"
        if event.get("kind") == "log":
            node["logs"] += 1
    return nodes


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


class DraftWrite(BaseModel):
    markdown: str = Field(..., min_length=1)


@router.get("/drafts")
async def list_drafts() -> list[dict[str, Any]]:
    return drafts.list_drafts()


@router.get("/drafts/{name}")
async def get_draft(name: str) -> dict[str, Any]:
    try:
        return drafts.read_draft(name)
    except FileNotFoundError as exc:
        raise HTTPException(404, "No such draft") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/drafts/{name}")
async def put_draft(name: str, body: DraftWrite) -> dict[str, bool]:
    try:
        drafts.write_draft(name, body.markdown)
    except FileNotFoundError as exc:
        raise HTTPException(404, "No such draft") from exc
    return {"saved": True}


@router.get("/drafts/{name}/cover")
async def get_cover(name: str) -> FileResponse:
    try:
        path = drafts.cover_path(name)
    except FileNotFoundError as exc:
        raise HTTPException(404, "No such draft") from exc
    if path is None:
        raise HTTPException(404, "No cover for this draft")
    return FileResponse(path, media_type="image/png")


@router.post("/drafts/{name}/publish")
async def publish_draft(name: str, status: str = "draft") -> dict[str, Any]:
    from ..wordpress import push_draft

    try:
        draft = drafts.draft_to_model(drafts._safe_path(name))
    except FileNotFoundError as exc:
        raise HTTPException(404, "No such draft") from exc
    target = await push_draft(draft, status=status)
    return target.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Catalog: topic ideas, posts and draft versions
#
# The DB indexes the crew's artefacts so the UI can browse the backlog of ideas,
# see which became posts, filter drafts, and keep a version history. Content
# (markdown, review, cover) is still served by the /drafts endpoints above; a
# version points at its markdown file by name.
# ---------------------------------------------------------------------------


@router.get("/topic-ideas")
async def list_topic_ideas(
    watch_area: str | None = None,
    post_format: str | None = None,
    has_draft: bool | None = None,
    min_score: float | None = None,
    q: str | None = None,
    limit: int = Query(200, le=500),
) -> list[dict[str, Any]]:
    return await catalog.list_topic_ideas(
        watch_area=watch_area,
        post_format=post_format,
        has_draft=has_draft,
        min_score=min_score,
        q=q,
        limit=limit,
    )


@router.get("/topic-ideas/{idea_id}")
async def get_topic_idea(idea_id: int) -> dict[str, Any]:
    idea = await catalog.get_topic_idea(idea_id)
    if idea is None:
        raise HTTPException(404, "No such topic idea")
    return idea


@router.get("/posts")
async def list_posts(
    status: str | None = None,
    approved: bool | None = None,
    has_cover: bool | None = None,
    published: bool | None = None,
    q: str | None = None,
    limit: int = Query(200, le=500),
) -> list[dict[str, Any]]:
    return await catalog.list_posts(
        status=status,
        approved=approved,
        has_cover=has_cover,
        published=published,
        q=q,
        limit=limit,
    )


@router.get("/posts/{post_id}")
async def get_post(post_id: int) -> dict[str, Any]:
    post = await catalog.get_post(post_id)
    if post is None:
        raise HTTPException(404, "No such post")
    return post


@router.get("/posts/{post_id}/versions")
async def get_post_versions(post_id: int) -> list[dict[str, Any]]:
    if await catalog.get_post(post_id) is None:
        raise HTTPException(404, "No such post")
    return await catalog.list_versions(post_id)


@router.get("/draft-versions/{version_id}")
async def get_draft_version(version_id: int) -> dict[str, Any]:
    version = await catalog.get_version(version_id)
    if version is None:
        raise HTTPException(404, "No such draft version")
    return version


class RegenerateRequest(BaseModel):
    instructions: str = ""
    reuse_research: bool = True
    push: bool | None = None
    cover: bool | None = None


@router.post("/posts/{post_id}/regenerate", status_code=202)
async def regenerate_post(post_id: int, body: RegenerateRequest) -> dict[str, str]:
    """Launch a write run that produces a new version of an existing post."""
    post = await catalog.get_post(post_id)
    if post is None:
        raise HTTPException(404, "No such post")
    try:
        topic, dossier_path = await catalog.post_topic_and_dossier(post_id)
    except KeyError as exc:
        raise HTTPException(404, "No such post") from exc
    if body.reuse_research and dossier_path is None:
        raise HTTPException(
            422, "No saved research for this post — regenerate with fresh research instead."
        )
    run_id = await manager().enqueue(
        "write",
        {
            "topic": topic.model_dump(mode="json"),
            "post_id": post_id,
            "instructions": body.instructions,
            "reuse_research": body.reuse_research,
            "push": body.push,
            "cover": body.cover,
        },
        f"Regenerate · {post.get('title') or topic.title}"[:300],
    )
    return {"id": run_id, "run_id": run_id}
