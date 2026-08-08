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
    real_write_from_dossier = wf.write_post_from_dossier
    real_explore, real_shortlist = wf.explore_sources, wf.shortlist_from_sources

    async def stub_discover(instruction="", **kw):
        kw.setdefault("clients", stub_clients(exercise_loops=False))
        return await real_discover(instruction, **kw)

    async def stub_explore(instruction="", **kw):
        kw.setdefault("clients", stub_clients(exercise_loops=False))
        return await real_explore(instruction, **kw)

    async def stub_shortlist(reports, approved, **kw):
        kw.setdefault("clients", stub_clients(exercise_loops=False))
        return await real_shortlist(reports, approved, **kw)

    async def stub_write(topic, **kw):
        kw.setdefault("clients", stub_clients(exercise_loops=False))
        # The API passes these as None ("use the config default"), so setdefault
        # is not enough — force them off for tests.
        kw["push_to_wordpress"] = False
        kw["make_cover"] = False
        return await real_write(topic, **kw)

    async def stub_write_from_dossier(topic, dossier, **kw):
        kw.setdefault("clients", stub_clients(exercise_loops=False))
        kw["push_to_wordpress"] = False
        kw["make_cover"] = False
        return await real_write_from_dossier(topic, dossier, **kw)

    monkeypatch.setattr(wf, "discover_topics", stub_discover)
    monkeypatch.setattr(wf, "write_post", stub_write)
    monkeypatch.setattr(wf, "write_post_from_dossier", stub_write_from_dossier)
    monkeypatch.setattr(wf, "explore_sources", stub_explore)
    monkeypatch.setattr(wf, "shortlist_from_sources", stub_shortlist)

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
    assert set(kinds) == {"suggest", "explore", "shortlist", "write"}

    post = kinds["write"]["mermaid"]
    assert post.startswith("flowchart TD")
    # The loops the pipeline depends on must be visible in the canvas.
    assert "source_gate --> researcher" in post
    assert "review_gate --> writer" in post
    assert "finalizer --> translator" in post

    # An exploration sweep must stop at the harvester: no path to the editor,
    # because nothing may reach it before the operator has approved the sources.
    explore = kinds["explore"]["mermaid"]
    assert "--> source_harvester" in explore
    assert "topic_editor" not in explore
    assert "scout_replay --> topic_editor" in kinds["shortlist"]["mermaid"]


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


# ---------------------------------------------------------------------------
# Source exploration and approval
# ---------------------------------------------------------------------------

# The stub scouts report these three domains: one official, one already trusted,
# one nobody has classified. See testing._scout_report.
NEW_DOMAIN = "dataverse-notes.example"


async def _explore(api, instruction="wide sweep"):
    run_id = (
        await api.post("/api/runs/suggest", json={"instruction": instruction, "explore": True})
    ).json()["id"]
    finished = await _wait_for(api, run_id)
    assert finished["status"] == "succeeded", finished.get("error")
    return finished


@pytest.mark.asyncio
async def test_exploration_run_stops_at_a_source_review(api):
    finished = await _explore(api)
    assert finished["kind"] == "explore"
    assert finished["result"]["awaiting_source_approval"] is True
    # The sweep must not produce topics — that is the whole point of stopping.
    assert "suggestions" not in finished["result"]
    assert "topic_editor" not in finished["nodes"]

    review_id = finished["result"]["review_id"]
    pending = (await api.get("/api/source-reviews?status=pending")).json()
    assert [r["id"] for r in pending] == [review_id]

    review = (await api.get(f"/api/source-reviews/{review_id}")).json()
    assert review["instruction"] == "wide sweep"
    by_domain = {c["domain"]: c for c in review["candidates"]}
    assert set(by_domain) == {"learn.microsoft.com", "matthewdevaney.com", NEW_DOMAIN}
    # A site the config has never heard of is offered as new, at the cautious
    # default tier; sites already in sources.yaml keep the tier they have.
    assert by_domain[NEW_DOMAIN]["known"] is False
    assert by_domain[NEW_DOMAIN]["suggested_tier"] == "community_unverified"
    assert by_domain["learn.microsoft.com"]["known"] is True
    assert by_domain["learn.microsoft.com"]["suggested_tier"] == "official"
    # Every candidate carries what was found there, so the decision is informed.
    assert by_domain[NEW_DOMAIN]["items"][0]["url"].startswith(f"https://{NEW_DOMAIN}")


@pytest.mark.asyncio
async def test_approval_writes_sources_config_and_builds_the_shortlist(api):
    review_id = (await _explore(api))["result"]["review_id"]

    decided = await api.post(
        f"/api/source-reviews/{review_id}/decide",
        json={
            "decisions": [
                {"domain": NEW_DOMAIN, "approved": True, "tier": "community_trusted"},
                {"domain": "learn.microsoft.com", "approved": True, "tier": "official"},
                {"domain": "matthewdevaney.com", "approved": False},
            ]
        },
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["approved"] == [NEW_DOMAIN, "learn.microsoft.com"]

    finished = await _wait_for(api, body["run_id"])
    assert finished["status"] == "succeeded", finished.get("error")

    # The verdict lands in sources.yaml as a new version, so it applies to every
    # later topic run and to the Researcher and Source Checker on every draft.
    sources = (await api.get("/api/config/sources")).json()
    assert sources["version"] == body["config_version"]
    assert f"- {NEW_DOMAIN}" in sources["content"]
    assert "# Trust tiers used by the Source Checker" in sources["content"], "comments lost"
    from ppn_blogger.settings import get_settings

    assert NEW_DOMAIN in get_settings().trust_tiers["community_trusted"]["domains"]
    # Declining a site that already carries a tier only skips it for this run —
    # it must not silently disappear from the trust tiers.
    assert "matthewdevaney.com" not in get_settings().declined_domains
    assert "matthewdevaney.com" in get_settings().trust_tiers["community_trusted"]["domains"]
    assert finished["kind"] == "shortlist"
    assert finished["result"]["suggestions"]
    assert finished["result"]["approved_sources"] == [NEW_DOMAIN, "learn.microsoft.com"]
    assert {"scout_replay", "topic_editor", "topic_publisher"} <= set(finished["nodes"])

    # The shortlist is a topic run like any other: its ideas reach the catalog.
    ideas = (await api.get("/api/topic-ideas")).json()
    assert ideas and ideas[0]["slug"]

    review = (await api.get(f"/api/source-reviews/{review_id}")).json()
    assert review["status"] == "approved"
    assert review["shortlist_run_id"] == body["run_id"]


@pytest.mark.asyncio
async def test_declined_sites_are_never_offered_again(api):
    review_id = (await _explore(api))["result"]["review_id"]
    await api.post(
        f"/api/source-reviews/{review_id}/decide",
        json={
            "decisions": [
                {"domain": NEW_DOMAIN, "approved": False},
                {"domain": "learn.microsoft.com", "approved": True, "tier": "official"},
            ],
            "start_shortlist": False,
        },
    )
    from ppn_blogger.settings import get_settings

    assert get_settings().declined_domains == [NEW_DOMAIN]

    second = (await _explore(api))["result"]["review_id"]
    review = (await api.get(f"/api/source-reviews/{second}")).json()
    assert NEW_DOMAIN not in {c["domain"] for c in review["candidates"]}


@pytest.mark.asyncio
async def test_a_review_can_only_be_decided_once(api):
    review_id = (await _explore(api))["result"]["review_id"]
    payload = {
        "decisions": [{"domain": "learn.microsoft.com", "approved": True, "tier": "official"}],
        "start_shortlist": False,
    }
    assert (await api.post(f"/api/source-reviews/{review_id}/decide", json=payload)).status_code == 200
    again = await api.post(f"/api/source-reviews/{review_id}/decide", json=payload)
    assert again.status_code == 409

    unknown = await api.post(
        f"/api/source-reviews/{review_id + 99}/decide", json=payload
    )
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_decisions_must_name_sites_from_the_review(api):
    review_id = (await _explore(api))["result"]["review_id"]
    response = await api.post(
        f"/api/source-reviews/{review_id}/decide",
        json={"decisions": [{"domain": "somewhere-else.example", "approved": True}]},
    )
    assert response.status_code == 409
    assert "somewhere-else.example" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Catalog: topic ideas, posts, versions, regeneration
# ---------------------------------------------------------------------------


async def _suggest(api):
    suggest_id = (await api.post("/api/runs/suggest", json={})).json()["id"]
    return (await _wait_for(api, suggest_id))["result"]["suggestions"]


async def _write(api, topic):
    write_id = (
        await api.post("/api/runs/write", json={"topic": topic, "push": False})
    ).json()["id"]
    finished = await _wait_for(api, write_id, timeout=90)
    assert finished["status"] == "succeeded", finished.get("error")
    return finished


@pytest.mark.asyncio
async def test_suggest_run_populates_topic_ideas(api):
    topics = await _suggest(api)

    ideas = (await api.get("/api/topic-ideas")).json()
    idea_slugs = {i["slug"] for i in ideas}
    assert {t["slug"] for t in topics} <= idea_slugs
    assert all("score" in i and "watch_area" in i for i in ideas)

    # A second suggest run must not duplicate ideas — they upsert by slug.
    suggest_id2 = (await api.post("/api/runs/suggest", json={})).json()["id"]
    await _wait_for(api, suggest_id2)
    ideas2 = (await api.get("/api/topic-ideas")).json()
    slugs2 = [i["slug"] for i in ideas2]
    assert len(slugs2) == len(set(slugs2)), "topic ideas duplicated on re-run"
    assert len(ideas2) == len(ideas)


@pytest.mark.asyncio
async def test_write_run_creates_post_and_version(api):
    topics = await _suggest(api)
    finished = await _write(api, topics[0])

    posts = (await api.get("/api/posts")).json()
    assert len(posts) == 1
    post = posts[0]
    assert post["version_count"] == 1
    assert post["topic_idea_id"] is not None
    assert post["current_version"]["approved"] is True

    detail = (await api.get(f"/api/posts/{post['id']}")).json()
    assert detail["topic_idea"]["slug"] == topics[0]["slug"]
    versions = detail["versions"]
    assert len(versions) == 1 and versions[0]["version"] == 1

    # The version's markdown file resolves through the existing drafts endpoint.
    markdown_file = versions[0]["markdown_file"]
    assert markdown_file == finished["result"]["markdown_path"].split("/")[-1]
    body = (await api.get(f"/api/drafts/{markdown_file}")).json()
    assert body["markdown"].startswith("#")

    # The idea now links back to the post.
    idea = (await api.get(f"/api/topic-ideas/{post['topic_idea_id']}")).json()
    assert idea["has_draft"] is True
    assert idea["post_id"] == post["id"]


@pytest.mark.asyncio
async def test_topic_idea_has_draft_filter(api):
    topics = await _suggest(api)

    assert (await api.get("/api/topic-ideas?has_draft=true")).json() == []
    assert len((await api.get("/api/topic-ideas?has_draft=false")).json()) >= 1

    await _write(api, topics[0])

    drafted = (await api.get("/api/topic-ideas?has_draft=true")).json()
    assert [i["slug"] for i in drafted] == [topics[0]["slug"]]
    assert (await api.get("/api/topic-ideas?has_draft=false")).json() == []


@pytest.mark.asyncio
async def test_regenerate_reuse_bumps_version(api):
    topics = await _suggest(api)
    await _write(api, topics[0])
    post_id = (await api.get("/api/posts")).json()[0]["id"]

    resp = await api.post(
        f"/api/posts/{post_id}/regenerate",
        json={"instructions": "Make it shorter", "reuse_research": True},
    )
    assert resp.status_code == 202
    finished = await _wait_for(api, resp.json()["id"], timeout=90)
    assert finished["status"] == "succeeded", finished.get("error")
    assert finished["params"]["post_id"] == post_id
    assert finished["params"]["instructions"] == "Make it shorter"
    assert finished["params"]["reuse_research"] is True

    detail = (await api.get(f"/api/posts/{post_id}")).json()
    assert detail["version_count"] == 2
    newest = detail["versions"][0]
    assert newest["version"] == 2
    assert newest["reused_research"] is True
    assert newest["instructions"] == "Make it shorter"
    # Each version keeps its own markdown file — the history is not overwritten.
    files = {v["markdown_file"] for v in detail["versions"]}
    assert len(files) == 2, "regeneration overwrote the previous version's file"


def test_accumulate_guidance_folds_history_newest_last():
    from ppn_blogger.server.catalog import accumulate_guidance

    # No history: the new note passes through untouched.
    assert accumulate_guidance([], "Make it shorter") == "Make it shorter"
    assert accumulate_guidance([""], "  Make it shorter  ") == "Make it shorter"

    folded = accumulate_guidance(["Make it shorter", "Lead with the steps"], "Add a FAQ")
    # Every earlier note survives, in order, with the new one flagged last.
    assert "Make it shorter" in folded
    assert "Lead with the steps" in folded
    assert folded.rfind("Add a FAQ") > folded.rfind("Lead with the steps")
    assert "most recent one wins" in folded


@pytest.mark.asyncio
async def test_regeneration_carries_all_prior_guidance(api, monkeypatch):
    topics = await _suggest(api)
    await _write(api, topics[0])
    post_id = (await api.get("/api/posts")).json()[0]["id"]

    async def regenerate(instructions):
        resp = await api.post(
            f"/api/posts/{post_id}/regenerate",
            json={"instructions": instructions, "reuse_research": True},
        )
        finished = await _wait_for(api, resp.json()["id"], timeout=90)
        assert finished["status"] == "succeeded", finished.get("error")

    await regenerate("Make it shorter")  # v2

    # Capture what actually reaches the writer on the v3 regeneration.
    from ppn_blogger import workflows as wf

    seen: dict[str, str] = {}
    real = wf.write_post_from_dossier

    async def capturing(topic, dossier, **kw):
        seen["extra"] = kw.get("extra_instructions", "")
        return await real(topic, dossier, **kw)

    monkeypatch.setattr(wf, "write_post_from_dossier", capturing)

    await regenerate("Add a FAQ")  # v3

    # v3 honours both its own note and v2's, newest last.
    assert "Make it shorter" in seen["extra"]
    assert "Add a FAQ" in seen["extra"]
    assert seen["extra"].rfind("Add a FAQ") > seen["extra"].rfind("Make it shorter")

    # Storage still records only the per-version note, not the folded blob.
    detail = (await api.get(f"/api/posts/{post_id}")).json()
    assert detail["versions"][0]["instructions"] == "Add a FAQ"


@pytest.mark.asyncio
async def test_regenerate_fresh_research(api):
    topics = await _suggest(api)
    await _write(api, topics[0])
    post_id = (await api.get("/api/posts")).json()[0]["id"]

    resp = await api.post(
        f"/api/posts/{post_id}/regenerate",
        json={"instructions": "Rework the intro", "reuse_research": False},
    )
    assert resp.status_code == 202
    finished = await _wait_for(api, resp.json()["id"], timeout=90)
    assert finished["status"] == "succeeded", finished.get("error")

    detail = (await api.get(f"/api/posts/{post_id}")).json()
    assert detail["version_count"] == 2
    assert detail["versions"][0]["reused_research"] is False


@pytest.mark.asyncio
async def test_backfill_is_idempotent(api):
    topics = await _suggest(api)
    await _write(api, topics[0])

    ideas_before = (await api.get("/api/topic-ideas")).json()
    posts_before = (await api.get("/api/posts")).json()

    from ppn_blogger.server import catalog

    await catalog.backfill()
    await catalog.backfill()

    ideas_after = (await api.get("/api/topic-ideas")).json()
    posts_after = (await api.get("/api/posts")).json()
    assert len(ideas_after) == len(ideas_before)
    assert len(posts_after) == len(posts_before)
    assert posts_after[0]["version_count"] == posts_before[0]["version_count"]


@pytest.mark.asyncio
async def test_regenerate_unknown_post_is_404(api):
    resp = await api.post("/api/posts/9999/regenerate", json={"instructions": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_regenerate_cover_enqueues_a_cover_run(api):
    topics = await _suggest(api)
    await _write(api, topics[0])
    post = (await api.get("/api/posts")).json()[0]

    resp = await api.post(
        f"/api/posts/{post['id']}/cover", json={"instructions": "deep violet light shards"}
    )
    assert resp.status_code == 202
    finished = await _wait_for(api, resp.json()["id"], timeout=60)
    # The run carries the concept and the post it belongs to. (Cover generation
    # itself needs an image endpoint; disabled in tests, so it finishes without
    # writing a file — the point here is the wiring.)
    assert finished["params"]["post_id"] == post["id"]
    assert finished["params"]["concept"] == "deep violet light shards"
    assert finished["kind"] == "cover"


@pytest.mark.asyncio
async def test_regenerate_cover_unknown_post_is_404(api):
    resp = await api.post("/api/posts/9999/cover", json={"instructions": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_record_cover_result_marks_versions(api):
    """A successful cover run points the post's versions at the image."""
    topics = await _suggest(api)
    await _write(api, topics[0])
    post = (await api.get("/api/posts")).json()[0]
    assert post["current_version"]["has_cover"] is False

    from ppn_blogger.server import catalog

    await catalog.record_cover_result(
        {"post_id": post["id"]}, {"path": "/drafts/covers/slug.png", "error": ""}
    )
    refreshed = (await api.get(f"/api/posts/{post['id']}")).json()
    assert all(v["has_cover"] for v in refreshed["versions"])
    assert refreshed["current_version"]["cover_path"] == "/drafts/covers/slug.png"

    # A failed generation records nothing.
    await catalog.record_cover_result(
        {"post_id": post["id"]}, {"path": "", "error": "boom"}
    )
    still = (await api.get(f"/api/posts/{post['id']}")).json()
    assert all(v["has_cover"] for v in still["versions"]), "a failed cover wiped the good one"


# -- Static serving ----------------------------------------------------------
#
# These only make sense once `npm run build` has produced ui/dist; CI runs ruff
# and pytest without building the SPA, so they skip there rather than fail.

_ui_missing = pytest.mark.skipif(
    not (__import__("ppn_blogger.server.app", fromlist=["_ui_present"])._ui_present()),
    reason="ui/dist not built",
)


@_ui_missing
@pytest.mark.asyncio
async def test_hashed_assets_are_pinned_and_the_shell_is_not(api):
    """Content-hashed bundles may be cached for a year; the shell may not.

    Without this split a service worker's precache buys nothing (everything
    revalidates) or an installed app is stranded on an old build forever
    (nothing revalidates).
    """
    from ppn_blogger.server.app import UI_DIST

    asset = next((UI_DIST / "assets").glob("*.js")).name
    pinned = await api.get(f"/assets/{asset}")
    assert pinned.status_code == 200
    assert "immutable" in pinned.headers["cache-control"]

    shell = await api.get("/runs")
    assert shell.status_code == 200
    assert shell.headers["cache-control"] == "no-cache"


@_ui_missing
@pytest.mark.asyncio
async def test_missing_file_paths_404_rather_than_serving_the_shell(api):
    """A path that names a file but has none must not return index.html.

    The manifest and icons are excluded from Easy Auth by exact path. If one of
    those paths fell through to the SPA shell, the exclusion would hand the app
    to anyone who asked, unauthenticated.
    """
    assert (await api.get("/icons/does-not-exist.png")).status_code == 404
    assert (await api.get("/manifest.webmanifest")).status_code in (200, 404)
    # Extensionless paths are routes and still get the app.
    assert (await api.get("/drafts/17")).status_code == 200


@_ui_missing
@pytest.mark.asyncio
async def test_api_namespace_never_falls_through_to_the_shell(api):
    """Unchanged behaviour, asserted because the 404 branch above sits beside it."""
    assert (await api.get("/api/not-a-route")).status_code == 404
