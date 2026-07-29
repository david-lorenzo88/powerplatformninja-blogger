"""Service-layer tests: real HTTP against the app, real queue, real SSE.

Everything runs offline — the run manager is pointed at the stub client, so
these exercise the actual workflow graphs without touching Azure.
"""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
async def api(tmp_path, monkeypatch):
    """A live app with its own SQLite file and drafts directory."""
    monkeypatch.setenv("PPN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("PPN_MAX_CONCURRENT_RUNS", "2")

    from ppn_blogger.config_source import set_config_source
    from ppn_blogger.server import db, runs
    from ppn_blogger.settings import get_settings

    await db.reset_engine()
    await runs.reset_manager()

    settings = get_settings()
    for attr in ("output_dir", "research_dir", "topics_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    # Every run in these tests uses the offline stub.
    from ppn_blogger import workflows as wf
    from ppn_blogger.testing import stub_clients

    real_discover, real_write = wf.discover_topics, wf.write_post

    async def stub_discover(instruction="", **kw):
        kw.setdefault("clients", stub_clients(exercise_loops=False))
        return await real_discover(instruction, **kw)

    async def stub_write(topic, **kw):
        kw.setdefault("clients", stub_clients(exercise_loops=False))
        # The API passes these as None ("use the config default"), so setdefault
        # is not enough — force them off for tests.
        kw["push_to_wordpress"] = False
        kw["make_cover"] = False
        return await real_write(topic, **kw)

    monkeypatch.setattr(wf, "discover_topics", stub_discover)
    monkeypatch.setattr(wf, "write_post", stub_write)

    import httpx

    from ppn_blogger.server.app import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            yield client

    await runs.reset_manager()
    await db.reset_engine()
    set_config_source(None)


async def _wait_for(client, run_id, timeout=30):
    """Poll a run until it reaches a terminal state."""
    from ppn_blogger.server.runs import TERMINAL

    for _ in range(timeout * 10):
        response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        if body["status"] in TERMINAL:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"run {run_id} never finished; last status {body['status']}")


@pytest.mark.asyncio
async def test_health_and_config_seeded_from_yaml(api):
    health = (await api.get("/api/health")).json()
    assert health["ok"] is True

    documents = (await api.get("/api/config")).json()
    names = {d["name"] for d in documents}
    assert names == {"blog_profile", "topics", "sources", "validation_rules", "style_guide"}
    # The move to the database must not lose anything — v1 is the YAML import.
    assert all(d["version"] == 1 for d in documents)

    sources = (await api.get("/api/config/sources")).json()
    assert "learn.microsoft.com" in sources["content"]


@pytest.mark.asyncio
async def test_config_edit_versions_and_reaches_the_agents(api):
    from ppn_blogger.settings import get_settings

    original = (await api.get("/api/config/topics")).json()
    assert get_settings().topics["suggestions_per_run"] == 6

    edited = original["content"].replace("suggestions_per_run: 6", "suggestions_per_run: 3")
    saved = await api.put(
        "/api/config/topics", json={"content": edited, "note": "fewer per run"}
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == 2

    # The whole point of DB config: the next run sees the change with no restart.
    assert get_settings().topics["suggestions_per_run"] == 3

    history = (await api.get("/api/config/topics/history")).json()
    assert [h["version"] for h in history] == [2, 1]

    # Rollback appends a new version rather than rewriting history.
    rolled = await api.post("/api/config/topics/rollback/1")
    assert rolled.json()["version"] == 3
    assert get_settings().topics["suggestions_per_run"] == 6


@pytest.mark.asyncio
async def test_invalid_yaml_is_rejected_before_it_can_break_a_run(api):
    response = await api.put(
        "/api/config/topics", json={"content": "watch_areas:\n  - id: x\n   bad: indent\n"}
    )
    assert response.status_code == 422
    assert "Invalid YAML" in response.json()["detail"]
    # The bad edit must not have been stored.
    assert (await api.get("/api/config/topics")).json()["version"] == 1


@pytest.mark.asyncio
async def test_workflow_graphs_come_from_the_code(api):
    graphs = (await api.get("/api/workflows")).json()
    kinds = {g["kind"]: g for g in graphs}
    assert set(kinds) == {"suggest", "write"}

    post = kinds["write"]["mermaid"]
    assert post.startswith("flowchart TD")
    # The loops the pipeline depends on must be visible in the canvas.
    assert "source_gate --> researcher" in post
    assert "review_gate --> writer" in post
    assert "finalizer --> translator" in post


@pytest.mark.asyncio
async def test_run_executes_and_streams_events(api):
    started = await api.post("/api/runs/suggest", json={"instruction": "test"})
    assert started.status_code == 202
    run_id = started.json()["id"]

    finished = await _wait_for(api, run_id)
    assert finished["status"] == "succeeded", finished.get("error")
    assert finished["result"]["suggestions"]

    # Node status for the canvas is derived from the event log — and a run that
    # reports "succeeded" must already have a COMPLETE log on disk. Events are
    # written by a single background writer, so without a flush before the
    # terminal status the UI would paint a run that is missing its last nodes.
    nodes = finished["nodes"]
    assert "topic_editor" in nodes, f"event log incomplete at terminal: {sorted(nodes)}"
    assert nodes["topic_editor"]["events"] > 0
    assert {"scout_dispatcher", "news_scout", "feed_scout", "docs_scout"} <= set(nodes)

    # SSE replays the whole run for a browser that arrives late.
    async with api.stream("GET", f"/api/runs/{run_id}/events") as response:
        assert response.status_code == 200
        payloads = []
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                payloads.append(json.loads(line[6:]))
                if payloads[-1].get("kind") == "eof":
                    break

    kinds = {p["kind"] for p in payloads}
    assert {"status", "node", "eof"} <= kinds
    assert payloads[-1]["status"] == "succeeded"
    # Sequence numbers must be strictly increasing — the UI relies on them to
    # resume a dropped connection without duplicating or losing events.
    seqs = [p["seq"] for p in payloads if "seq" in p]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


@pytest.fixture
def controllable_dispatch(monkeypatch):
    """Replace the real work with a job we can hold open and release.

    Stub runs finish in milliseconds, which makes any assertion about queueing
    or cancellation a race. This gives the tests a job that runs until told to
    stop, so the queue behaviour is observed deterministically rather than hoped
    for.
    """
    from ppn_blogger.server.runs import RunManager

    release = asyncio.Event()
    started: list[str] = []

    async def fake_dispatch(self, run_id, kind, params):
        started.append(run_id)
        await release.wait()
        return {"ok": True}

    monkeypatch.setattr(RunManager, "_dispatch", fake_dispatch)
    return type("Control", (), {"release": release, "started": started})()


@pytest.mark.asyncio
async def test_queue_admits_only_the_concurrency_limit(api, controllable_dispatch):
    """Four runs, two workers — exactly two run, the rest wait."""
    ids = [(await api.post("/api/runs/suggest", json={})).json()["id"] for _ in range(4)]

    for _ in range(100):
        if len(controllable_dispatch.started) >= 2:
            break
        await asyncio.sleep(0.02)

    await asyncio.sleep(0.1)  # give any over-admission a chance to show itself
    assert len(controllable_dispatch.started) == 2, (
        f"concurrency cap breached: {len(controllable_dispatch.started)} runs started"
    )

    listing = (await api.get("/api/runs")).json()
    statuses = sorted(r["status"] for r in listing)
    assert statuses == ["queued", "queued", "running", "running"], statuses

    controllable_dispatch.release.set()
    for run_id in ids:
        assert (await _wait_for(api, run_id))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_cancel_a_queued_run(api, controllable_dispatch):
    """A run still behind the cap is cancelled without ever starting."""
    ids = [(await api.post("/api/runs/suggest", json={})).json()["id"] for _ in range(4)]
    for _ in range(100):
        if len(controllable_dispatch.started) >= 2:
            break
        await asyncio.sleep(0.02)

    last = ids[-1]
    assert last not in controllable_dispatch.started
    assert (await api.post(f"/api/runs/{last}/cancel")).json()["cancelled"] is True
    assert (await api.get(f"/api/runs/{last}")).json()["status"] == "cancelled"

    controllable_dispatch.release.set()
    # Cancelling a queued run must not wedge the worker that later dequeues it.
    assert (await _wait_for(api, ids[0]))["status"] == "succeeded"
    assert (await _wait_for(api, ids[2]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_cancel_a_running_run(api, controllable_dispatch):
    """Cancelling in-flight work stops it and frees the worker for the queue."""
    ids = [(await api.post("/api/runs/suggest", json={})).json()["id"] for _ in range(3)]
    for _ in range(100):
        if len(controllable_dispatch.started) >= 2:
            break
        await asyncio.sleep(0.02)

    running = controllable_dispatch.started[0]
    assert (await api.post(f"/api/runs/{running}/cancel")).json()["cancelled"] is True
    assert (await _wait_for(api, running, timeout=15))["status"] == "cancelled"

    # The freed worker must pick up the run that was waiting behind it.
    for _ in range(150):
        if len(controllable_dispatch.started) >= 3:
            break
        await asyncio.sleep(0.02)
    assert len(controllable_dispatch.started) == 3, "worker did not recover after a cancellation"

    controllable_dispatch.release.set()
    assert (await _wait_for(api, ids[-1]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_write_run_produces_a_reviewable_draft(api):
    suggest_id = (await api.post("/api/runs/suggest", json={})).json()["id"]
    topics = (await _wait_for(api, suggest_id))["result"]["suggestions"]

    write_id = (
        await api.post("/api/runs/write", json={"topic": topics[0], "push": False})
    ).json()["id"]
    finished = await _wait_for(api, write_id, timeout=90)
    assert finished["status"] == "succeeded", finished.get("error")
    assert finished["result"]["approved"] is True

    listing = (await api.get("/api/drafts")).json()
    assert listing, "the write run produced no draft file"
    # Only real drafts, never the topics shortlist that shares this directory.
    assert all(item["slug"] for item in listing), listing
    name = next(i["file"] for i in listing if i["slug"] == finished["result"]["slug"])

    detail = (await api.get(f"/api/drafts/{name}")).json()
    assert detail["markdown"].startswith("#")
    assert detail["review"], "review report should be readable in the UI"

    edited = detail["markdown"] + "\n\nEdited in the UI.\n"
    assert (await api.put(f"/api/drafts/{name}", json={"markdown": edited})).status_code == 200
    assert "Edited in the UI." in (await api.get(f"/api/drafts/{name}")).json()["markdown"]


@pytest.mark.asyncio
async def test_draft_paths_cannot_escape_the_drafts_directory(api):
    response = await api.get("/api/drafts/..%2F..%2F.env")
    assert response.status_code in (400, 404)


@pytest.mark.asyncio
async def test_config_reload_endpoint_is_token_guarded(api, monkeypatch):
    """POST /api/config/reload: disabled without a token, guarded with one."""
    # Disabled unless PPN_ADMIN_TOKEN is set.
    monkeypatch.delenv("PPN_ADMIN_TOKEN", raising=False)
    assert (await api.post("/api/config/reload")).status_code == 503

    monkeypatch.setenv("PPN_ADMIN_TOKEN", "s3cret-token")
    # Missing or wrong token is rejected.
    assert (await api.post("/api/config/reload")).status_code == 401
    assert (
        await api.post("/api/config/reload", headers={"X-Admin-Token": "wrong"})
    ).status_code == 401

    # The right token re-imports config as a new version of each document.
    before = {d["name"]: d["version"] for d in (await api.get("/api/config")).json()}
    resp = await api.post("/api/config/reload", headers={"X-Admin-Token": "s3cret-token"})
    assert resp.status_code == 200
    reloaded = {r["name"]: r["version"] for r in resp.json()["reloaded"]}
    assert "validation_rules" in reloaded
    # Seeded at v1 by the lifespan, so a reload bumps every document to v2.
    for name, version in before.items():
        assert reloaded[name] == version + 1
