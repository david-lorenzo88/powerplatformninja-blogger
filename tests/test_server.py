"""Service-layer tests: real HTTP against the app, real queue, real SSE.

Everything runs offline — the run manager is pointed at the stub client, so
these exercise the actual workflow graphs without touching Azure.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ppn_blogger.config_source import DOCUMENTS


@pytest.fixture
async def api(tmp_path, monkeypatch, database_url):
    """A live app with a clean database and its own drafts directory.

    The backend comes from the `database_url` fixture — a temp SQLite file
    locally, a real SQL Server in CI. Nothing in here knows which.
    """
    monkeypatch.setenv("PPN_MAX_CONCURRENT_RUNS", "2")

    from ppn_blogger.config_source import set_config_source
    from ppn_blogger.server import runs
    from ppn_blogger.settings import get_settings

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
    real_topic_from_brief = wf.topic_from_brief

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

    async def stub_topic_from_brief(brief, sources=None, **kw):
        kw.setdefault("clients", stub_clients(exercise_loops=False))
        return await real_topic_from_brief(brief, sources, **kw)

    monkeypatch.setattr(wf, "topic_from_brief", stub_topic_from_brief)
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
    assert names == set(DOCUMENTS)
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
    assert set(kinds) == {"suggest", "explore", "shortlist", "write", "newsletter"}

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
async def test_a_brief_run_is_built_only_from_the_links_it_names(api, monkeypatch, tmp_path):
    """The whole promise of the custom mode, end to end.

    The brief's link is offered as `http` and redirects; what the pipeline is
    given is where it actually landed, so the citation the crew brings back is
    recognised as part of the corpus rather than dropped as a stranger.
    """
    from ppn_blogger import tools

    async def fake_probe(url):
        return {"ok": True, "status": 200, "final_url": url.replace("http://", "https://")}

    monkeypatch.setattr(tools, "probe_url", fake_probe)

    body = {
        "brief": "Write up what changes for admins, from http://example.com/the-page",
        "push": False,
    }
    run_id = (await api.post("/api/runs/write", json=body)).json()["id"]
    finished = await _wait_for(api, run_id, timeout=90)
    assert finished["status"] == "succeeded", finished.get("error")

    # The corpus was resolved to where the link actually goes, and the topic the
    # interpreter produced was recorded on the run rather than left implicit.
    assert finished["params"]["sources"] == ["https://example.com/the-page"]
    assert finished["params"]["topic"]["seed_sources"] == ["https://example.com/the-page"]

    from ppn_blogger.models import ResearchDossier

    dossier = ResearchDossier.model_validate_json(
        Path(finished["result"]["dossier_path"]).read_text(encoding="utf-8")
    )
    assert [c.url for c in dossier.citations] == ["https://example.com/the-page"]


@pytest.mark.asyncio
async def test_a_brief_needs_links_and_a_topic_run_needs_a_topic(api):
    both = await api.post(
        "/api/runs/write", json={"topic": {"title": "t"}, "brief": "https://example.com/x"}
    )
    assert both.status_code == 422

    neither = await api.post("/api/runs/write", json={})
    assert neither.status_code == 422

    linkless = await api.post("/api/runs/write", json={"brief": "just write something good"})
    assert linkless.status_code == 422
    assert "at least one link" in linkless.json()["detail"]


@pytest.mark.asyncio
async def test_a_dead_link_stops_the_run_before_it_costs_anything(api, monkeypatch):
    from ppn_blogger import tools

    async def fake_probe(url):
        if "gone" in url:
            return {"ok": False, "status": 404, "final_url": url}
        return {"ok": True, "status": 200, "final_url": url}

    monkeypatch.setattr(tools, "probe_url", fake_probe)

    body = {
        "brief": "From https://example.com/live and https://example.com/gone",
        "push": False,
    }
    refused = await api.post("/api/runs/write", json=body)
    assert refused.status_code == 422
    assert "https://example.com/gone" in refused.json()["detail"]

    # Insisting keeps the dead link in the corpus: the crew reports what it could
    # not read, which is honest about the post resting on less than was asked.
    started = await api.post("/api/runs/write", json={**body, "allow_unreachable": True})
    assert started.status_code == 202
    finished = await _wait_for(api, started.json()["id"], timeout=90)
    assert finished["status"] == "succeeded", finished.get("error")
    assert finished["params"]["sources"] == [
        "https://example.com/live",
        "https://example.com/gone",
    ]


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


# -- Covers on WordPress -----------------------------------------------------
#
# A cover reaches the blog in two ways and both were broken: publishing sent no
# cover at all, and regenerating one only wrote a PNG. A post could show art in
# the app and `featured_media: 0` on the site.


def _write_cover_for(api_settings_dir, slug: str) -> None:
    """A 1x1 PNG where the covers directory expects this post's art."""
    directory = api_settings_dir / "covers"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{slug}.png").write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        )
    )


@pytest.mark.asyncio
async def test_publishing_a_draft_sends_its_cover(api, tmp_path, monkeypatch):
    topics = await _suggest(api)
    finished = await _write(api, topics[0])
    name = finished["result"]["markdown_path"].rsplit("/", 1)[-1]
    slug = finished["result"]["slug"]
    _write_cover_for(tmp_path, slug)

    from ppn_blogger import wordpress
    from ppn_blogger.models import PublishTarget

    seen = {}

    async def fake_push(draft, *, status=None, cover=None):
        seen["cover"] = cover
        return PublishTarget(post_id=1279, status=status or "draft", featured_media_id=99)

    monkeypatch.setattr(wordpress, "push_draft", fake_push)

    resp = await api.post(f"/api/drafts/{name}/publish?status=draft")
    assert resp.status_code == 200, resp.text
    assert seen["cover"] is not None, "publish went out without the cover again"
    assert seen["cover"].path.endswith(f"{slug}.png")
    assert seen["cover"].alt_text

    # The escape hatch for a featured image set by hand in WordPress.
    await api.post(f"/api/drafts/{name}/publish?cover=false")
    assert seen["cover"] is None


@pytest.mark.asyncio
async def test_cover_can_be_sent_to_an_existing_wordpress_post(api, tmp_path, monkeypatch):
    topics = await _suggest(api)
    finished = await _write(api, topics[0])
    post = (await api.get("/api/posts")).json()[0]

    # No image on disk yet: nothing to send, and it says so rather than 500ing.
    assert (await api.post(f"/api/posts/{post['id']}/cover/wordpress")).status_code == 422

    _write_cover_for(tmp_path, finished["result"]["slug"])

    from ppn_blogger import wordpress
    from ppn_blogger.models import PublishTarget

    seen = {}

    async def fake_push_cover(draft, cover, *, post_id=None):
        seen["slug"] = draft.slug
        seen["path"] = cover.path
        seen["post_id"] = post_id
        return PublishTarget(post_id=1279, status="publish", featured_media_id=4242)

    monkeypatch.setattr(wordpress, "push_cover", fake_push_cover)

    resp = await api.post(f"/api/posts/{post['id']}/cover/wordpress")
    assert resp.status_code == 200, resp.text
    assert resp.json()["featured_media_id"] == 4242
    assert seen["slug"] == finished["result"]["slug"]
    assert seen["path"].endswith(".png")


@pytest.mark.asyncio
async def test_sending_a_cover_reports_wordpress_failures(api, tmp_path, monkeypatch):
    topics = await _suggest(api)
    finished = await _write(api, topics[0])
    post = (await api.get("/api/posts")).json()[0]
    _write_cover_for(tmp_path, finished["result"]["slug"])

    from ppn_blogger import wordpress

    async def boom(draft, cover, *, post_id=None):
        raise wordpress.WordPressError("No WordPress post found for 'x'.")

    monkeypatch.setattr(wordpress, "push_cover", boom)

    resp = await api.post(f"/api/posts/{post['id']}/cover/wordpress")
    assert resp.status_code == 502
    assert "No WordPress post found" in resp.text


@pytest.mark.asyncio
async def test_sending_a_cover_for_an_unknown_post_is_404(api):
    assert (await api.post("/api/posts/9999/cover/wordpress")).status_code == 404


@pytest.mark.asyncio
async def test_publishing_records_the_post_on_the_catalog(api, monkeypatch):
    """A publish from the app must leave the post looking published here too.

    It did not: `record_write_result` was the only writer of
    `wordpress_post_id`, and it fires only when the write run itself pushed.
    """
    topics = await _suggest(api)
    finished = await _write(api, topics[0])
    name = finished["result"]["markdown_path"].rsplit("/", 1)[-1]
    post = (await api.get("/api/posts")).json()[0]
    assert post["wordpress_post_id"] is None

    from ppn_blogger import wordpress
    from ppn_blogger.models import PublishTarget

    async def fake_push(draft, *, status=None, cover=None):
        return PublishTarget(
            post_id=1279,
            status="publish",
            link="https://blog.test/p/1279",
            edit_link="https://blog.test/wp-admin/post.php?post=1279&action=edit",
        )

    monkeypatch.setattr(wordpress, "push_draft", fake_push)
    assert (await api.post(f"/api/drafts/{name}/publish")).status_code == 200

    refreshed = (await api.get(f"/api/posts/{post['id']}")).json()
    assert refreshed["wordpress_post_id"] == 1279
    assert refreshed["status"] == "published"
    assert refreshed["link"] == "https://blog.test/p/1279"
    assert refreshed["current_version"]["wordpress_post_id"] == 1279


@pytest.mark.asyncio
async def test_sending_a_cover_corrects_a_post_the_catalog_thinks_is_unpublished(
    api, tmp_path, monkeypatch
):
    """The button must work on the post it exists for — one already published.

    `push_cover` finds the post by slug when the catalog has no id, so the
    request is not refused; what it learns is then written back.
    """
    topics = await _suggest(api)
    finished = await _write(api, topics[0])
    post = (await api.get("/api/posts")).json()[0]
    assert post["wordpress_post_id"] is None
    _write_cover_for(tmp_path, finished["result"]["slug"])

    from ppn_blogger import wordpress
    from ppn_blogger.models import PublishTarget

    seen = {}

    async def fake_push_cover(draft, cover, *, post_id=None):
        seen["post_id"] = post_id
        return PublishTarget(
            post_id=1279,
            status="publish",
            link="https://blog.test/p/1279",
            edit_link="https://blog.test/wp-admin/post.php?post=1279&action=edit",
            featured_media_id=4242,
        )

    monkeypatch.setattr(wordpress, "push_cover", fake_push_cover)

    resp = await api.post(f"/api/posts/{post['id']}/cover/wordpress")
    assert resp.status_code == 200, resp.text
    assert seen["post_id"] is None, "a missing id must be resolved by slug, not refused"

    refreshed = (await api.get(f"/api/posts/{post['id']}")).json()
    assert refreshed["wordpress_post_id"] == 1279
    assert refreshed["status"] == "published"


@pytest.mark.asyncio
async def test_backfill_recovers_wordpress_ids_from_the_state_file(api, tmp_path, monkeypatch):
    """Posts published before any of this was recorded are repaired on boot.

    `.ppn_state/wp_posts.json` is on the same persistent mount as the drafts and
    has always been written by `upsert_draft`, so it knows the id even when the
    catalog does not.
    """
    topics = await _suggest(api)
    finished = await _write(api, topics[0])
    post = (await api.get("/api/posts")).json()[0]
    assert post["wordpress_post_id"] is None

    from ppn_blogger import wordpress
    from ppn_blogger.server import catalog
    from ppn_blogger.settings import get_settings

    state = tmp_path / "wp_posts.json"
    state.write_text(json.dumps({finished["result"]["slug"]: 1279}), encoding="utf-8")
    monkeypatch.setattr(wordpress, "STATE_FILE", state)
    monkeypatch.setattr(get_settings().wordpress, "url", "https://blog.test")

    await catalog.backfill()

    refreshed = (await api.get(f"/api/posts/{post['id']}")).json()
    assert refreshed["wordpress_post_id"] == 1279
    assert refreshed["status"] == "wordpress_draft"
    assert refreshed["edit_link"].endswith("post=1279&action=edit")

    # Idempotent, and it never overwrites an id that is already there.
    state.write_text(json.dumps({finished["result"]["slug"]: 999}), encoding="utf-8")
    await catalog.backfill()
    assert (await api.get(f"/api/posts/{post['id']}")).json()["wordpress_post_id"] == 1279


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


# -- Web Push ----------------------------------------------------------------


@pytest.fixture
def push_configured(monkeypatch):
    """Fake VAPID keys plus a recorder in place of the network.

    `_send_one` is the whole boundary to the outside world, so patching it is
    what makes these tests offline. It also lets a test assert on the payload,
    which is the part that actually matters — a notification that fires with the
    wrong words is as bad as one that does not fire.
    """
    from ppn_blogger import settings as settings_mod
    from ppn_blogger.server import push

    s = settings_mod.get_settings()
    monkeypatch.setattr(s.push, "public_key", "test-public")
    monkeypatch.setattr(s.push, "private_key", "test-private")
    monkeypatch.setattr(s.push, "subject", "mailto:test@example.com")

    sent: list[dict] = []

    def recorder(subscription, payload):
        sent.append({"endpoint": subscription["endpoint"], **json.loads(payload)})
        return 200

    monkeypatch.setattr(push, "_send_one", recorder)
    return sent


async def _subscribe(api, endpoint="https://push.example.com/abc"):
    return await api.post(
        "/api/push/subscribe",
        json={"endpoint": endpoint, "keys": {"p256dh": "k", "auth": "a"}},
    )


@pytest.mark.asyncio
async def test_push_is_disabled_until_vapid_keys_are_set(api):
    """No keys means 503, not a confusing success that never delivers."""
    assert (await _subscribe(api)).status_code == 503
    assert (await api.post("/api/push/test")).status_code == 503


@pytest.mark.asyncio
async def test_subscribing_twice_keeps_one_row(api, push_configured):
    """Browsers re-issue the same endpoint on every load.

    Without the unique index and the select-then-write upsert this would grow a
    row per page view, and one finished run would buzz the phone repeatedly.
    """
    first = await _subscribe(api)
    assert first.status_code == 201
    again = await _subscribe(api)
    assert again.status_code == 201
    assert again.json()["subscriptions"] == 1

    await _subscribe(api, "https://push.example.com/second")
    assert (await api.post("/api/push/test")).json()["subscriptions"] == 2

    removed = await api.post(
        "/api/push/unsubscribe", json={"endpoint": "https://push.example.com/second"}
    )
    assert removed.json() == {"removed": True, "subscriptions": 1}


@pytest.mark.asyncio
async def test_health_carries_the_public_key_but_never_the_private_one(api, push_configured):
    body = (await api.get("/api/health")).json()
    assert body["push"] == {"configured": True, "public_key": "test-public"}
    assert "test-private" not in json.dumps(body)


@pytest.mark.asyncio
async def test_a_finished_run_notifies_exactly_once(api, push_configured, controllable_dispatch):
    """One notification per run, and not before it is actually finished."""
    await _subscribe(api)
    started = await api.post("/api/runs/suggest", json={"instruction": "x", "label": "Weekly sweep"})
    run_id = started.json()["id"]

    # Held open by the fixture: nothing should have been sent yet.
    await asyncio.sleep(0.1)
    assert push_configured == []

    controllable_dispatch.release.set()
    await _wait_for(api, run_id)
    # The notification is spawned detached, so give the task a moment to land.
    for _ in range(50):
        if push_configured:
            break
        await asyncio.sleep(0.05)

    assert len(push_configured) == 1, "a run must notify exactly once"
    assert push_configured[0]["url"] == "/topic-ideas"


@pytest.mark.asyncio
async def test_a_sweep_awaiting_approval_says_so(api, push_configured):
    """The exploration case gets its own copy, and still only one notification.

    A run that stops for a verdict finishes `succeeded`, so a naive hook would
    announce "topics ready" for a run that produced none and is blocked on the
    operator.
    """
    from ppn_blogger.server import push as push_mod

    title, body, url = push_mod._describe(
        "explore",
        "succeeded",
        "Wide sweep",
        {"awaiting_source_approval": True, "review_id": 7, "candidate_count": 9, "new_count": 3},
    )
    assert "verdict" in title.lower()
    assert "9 sites" in body and "3 new" in body
    assert url == "/source-reviews/7"


@pytest.mark.asyncio
async def test_cancelling_your_own_run_does_not_notify(api, push_configured):
    """You were looking at it a second ago; a push would be noise."""
    from ppn_blogger.server import push as push_mod

    await _subscribe(api)
    await push_mod.notify_run_finished("write", "cancelled", "Some draft", None)
    assert push_configured == []


@pytest.mark.asyncio
async def test_an_expired_subscription_is_pruned(api, push_configured, monkeypatch):
    """410 Gone means that endpoint will never deliver again.

    Without pruning, a phone that was reset would be retried on every run for
    the life of the deployment.
    """
    from ppn_blogger.server import push as push_mod

    await _subscribe(api)
    monkeypatch.setattr(push_mod, "_send_one", lambda subscription, payload: 410)
    assert await push_mod.notify("t", "b", "/runs") == 0
    assert await push_mod.count() == 0


@pytest.mark.asyncio
async def test_a_failing_push_service_never_sinks_a_run(api, push_configured, monkeypatch):
    """Same doctrine as the cover and the WordPress push: the run matters more."""
    from ppn_blogger.server import push as push_mod

    await _subscribe(api)

    def explode(subscription, payload):
        raise RuntimeError("push service on fire")

    monkeypatch.setattr(push_mod, "_send_one", explode)
    # Must not raise.
    await push_mod.notify_run_finished("suggest", "succeeded", "x", {"suggestions": []})
    assert await push_mod.count() == 1, "a transient failure must not drop the subscription"


@pytest.mark.asyncio
async def test_a_pem_vapid_key_is_usable_by_pywebpush():
    """The key format actually reaches the signing code.

    Every other push test patches `_send_one`, which is the whole boundary to
    the outside world — and also the only place the key format matters. So the
    suite was fully green while every real send raised before a byte left the
    process: pywebpush hands a bare string to `Vapid.from_string`, which
    base64-decodes it as DER, and PEM (what every generator produces) dies with
    an ASN.1 parsing error. The only visible symptom was a test notification
    reporting that no device accepted the push.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from ppn_blogger.server.push import _vapid_key

    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )

    prepared = _vapid_key(pem)
    # pywebpush accepts a Vapid instance; it cannot accept PEM text.
    assert not isinstance(prepared, str), "PEM must be parsed, not passed through"
    assert prepared.sign({"aud": "https://example.com", "sub": "mailto:a@b.c"})["Authorization"]

    # A key already in the base64url DER form pywebpush understands is left alone.
    assert _vapid_key("abc123") == "abc123"


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_records_what_it_cost(api):
    """The whole chain, offline: meter → ledger → run_usage → API."""
    run_id = (await api.post("/api/runs/suggest", json={})).json()["id"]
    finished = await _wait_for(api, run_id)
    assert finished["status"] == "succeeded", finished.get("error")

    usage = finished["usage"]
    assert usage is not None, "a finished run must carry its usage"
    # Four agents in the discovery graph: three scouts and the editor.
    assert usage["records"] >= 4
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
    assert usage["searches"] > 0
    # Priced against the configured model — the meter records the deployment the
    # run would really have used, not what the stub answered as, so a dry run
    # gives a realistic estimate rather than an empty one.
    assert usage["priced"] is True
    assert usage["cost_micros"] > 0
    assert usage["currency"] == "USD"


@pytest.mark.asyncio
async def test_usage_is_broken_down_per_agent(api):
    run_id = (await api.post("/api/runs/suggest", json={})).json()["id"]
    await _wait_for(api, run_id)

    body = (await api.get(f"/api/runs/{run_id}/usage")).json()
    agents = {row["agent_id"] for row in body["agents"]}
    assert {"news_scout", "feed_scout", "docs_scout", "topic_editor"} <= agents
    assert body["total"]["total_tokens"] == sum(r["total_tokens"] for r in body["agents"])
    # The breakdown has to carry `priced` as well as the tokens, or the UI reads
    # every row as uncosted and renders a dash beside a real number. The totals
    # query got this right and the per-agent one did not, which is exactly the
    # asymmetry worth pinning down.
    assert body["total"]["cost_micros"] == sum(r["cost_micros"] for r in body["agents"])
    assert all(row["priced"] for row in body["agents"])


@pytest.mark.asyncio
async def test_usage_appears_on_the_runs_list(api):
    run_id = (await api.post("/api/runs/suggest", json={})).json()["id"]
    await _wait_for(api, run_id)

    listing = (await api.get("/api/runs")).json()
    row = next(r for r in listing if r["id"] == run_id)
    assert row["usage"]["total_tokens"] > 0


@pytest.mark.asyncio
async def test_a_never_metered_run_reports_no_usage_rather_than_zero(api, controllable_dispatch):
    """A run that called no model has no cost — which is not the same as £0.00."""
    run_id = (await api.post("/api/runs/suggest", json={})).json()["id"]
    controllable_dispatch.release.set()
    await _wait_for(api, run_id)

    assert (await api.get(f"/api/runs/{run_id}")).json()["usage"] is None


@pytest.mark.asyncio
async def test_rollup_groups_by_day_and_by_kind(api):
    run_id = (await api.post("/api/runs/suggest", json={})).json()["id"]
    await _wait_for(api, run_id)

    by_day = (await api.get("/api/usage")).json()
    assert len(by_day["buckets"]) == 1
    assert by_day["buckets"][0]["total_tokens"] > 0

    by_kind = (await api.get("/api/usage?group_by=kind")).json()
    assert [b["key"] for b in by_kind["buckets"]] == ["suggest"]
    assert by_kind["top_runs"][0]["run_id"] == run_id


@pytest.mark.asyncio
async def test_rollup_window_excludes_older_runs(api):
    run_id = (await api.post("/api/runs/suggest", json={})).json()["id"]
    await _wait_for(api, run_id)

    # A window that starts in the future can contain nothing. Passed through
    # `params` so the `+00:00` offset is encoded — inlined in the query string
    # the `+` arrives as a space and the bound is silently unreadable.
    from datetime import timedelta

    from ppn_blogger.server.db import utcnow

    future = (utcnow() + timedelta(hours=1)).isoformat()
    body = (await api.get("/api/usage", params={"since": future})).json()
    assert body["buckets"] == []
    assert body["top_runs"] == []


@pytest.mark.asyncio
async def test_an_unreadable_window_is_refused_rather_than_widened(api):
    """A cost figure over the wrong period looks like an answer. It must not."""
    response = await api.get("/api/usage?since=last+tuesday")
    assert response.status_code == 422
    assert "since" in response.json()["detail"]


@pytest.fixture
def recorded_prices(monkeypatch):
    """The retail API, served from the recorded payload used by test_prices."""
    import pathlib

    import httpx

    payload = json.loads(
        (pathlib.Path(__file__).parent / "fixtures" / "azure_retail_prices.json").read_text()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        expr = dict(request.url.params).get("$filter", "")
        rows = payload["Items"]
        if "contains(meterName, '" in expr:
            fragment = expr.split("contains(meterName, '")[1].split("')")[0]
            rows = [r for r in rows if fragment.lower() in r["meterName"].lower()]
        return httpx.Response(200, json={"Items": rows, "NextPageLink": None})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: original(*a, **{**kw, "transport": transport})
    )


@pytest.mark.asyncio
async def test_price_candidates_are_offered_for_binding(api, recorded_prices):
    body = (await api.get("/api/prices/candidates", params={"model": "gpt-5"})).json()
    assert body["region"] == "eastus"
    assert body["suggested"] == {
        "input": "5 pp inp Gl 1M Tokens",
        "cached_input": "5 pp cd inp Gl 1M Tokens",
        "output": "5 pp opt Gl 1M Tokens",
    }


@pytest.mark.asyncio
async def test_refresh_reports_before_it_writes(api, recorded_prices):
    """The manual path must never save without being asked."""
    before = (await api.get("/api/config/model_prices")).json()["version"]

    reported = (await api.post("/api/prices/refresh", json={"apply": False})).json()
    assert reported["checked"] > 0
    assert reported["applied"] is False
    assert (await api.get("/api/config/model_prices")).json()["version"] == before


@pytest.mark.asyncio
async def test_applying_a_refresh_saves_a_new_config_version(api, recorded_prices):
    # Make one price stale so there is something to move.
    document = (await api.get("/api/config/model_prices")).json()["content"]
    await api.put(
        "/api/config/model_prices",
        json={"content": document.replace("input: 2.50", "input: 1.11")},
    )
    version = (await api.get("/api/config/model_prices")).json()["version"]

    applied = (await api.post("/api/prices/refresh", json={"apply": True})).json()
    assert applied["applied"] is True
    assert applied["version"] == version + 1

    from ppn_blogger.settings import get_settings

    prices_now = get_settings().model_prices
    assert prices_now["models"]["gpt-5"]["input"] == 2.5
    # The hand-set figures Azure cannot price must survive the rewrite.
    assert prices_now["images"]["MAI-Image-2.5-Pro"]["per_image"] == 0.07
    assert prices_now["tools"]["web_search"]["per_call"] == 0.035
    assert prices_now["updated_from_azure"]


# ---------------------------------------------------------------------------
# Naming the configuration a run used
#
# `Run.config_version` is String(64) and used to hold `name:version|...` sliced
# to fit. That string is 112 characters with the documents this project ships,
# so the slice discarded four of them — `validation_rules` among them, which is
# the only thing the column is really worth asking about.
# ---------------------------------------------------------------------------


def test_a_stamp_fits_the_column_at_realistic_version_numbers():
    """The guard that has to fail in CI rather than in Azure. String(64) becomes
    NVARCHAR(64), and an over-long insert is rejected outright there."""
    from ppn_blogger.config_source import DOCUMENTS, STAMP_MAX_LENGTH, config_stamp

    at_one = config_stamp(dict.fromkeys(DOCUMENTS, 1))
    at_999 = config_stamp(dict.fromkeys(DOCUMENTS, 999))
    assert len(at_one) <= STAMP_MAX_LENGTH
    assert len(at_999) <= STAMP_MAX_LENGTH
    # The old encoding, for contrast: comfortably over, which is the bug.
    assert len("|".join(f"{name}:1" for name in sorted(DOCUMENTS))) > STAMP_MAX_LENGTH


def test_the_stamp_has_headroom_for_a_few_more_documents():
    """Four spare documents at three-digit versions. When this fails, someone has
    added enough config that the encoding needs revisiting — which is a decision,
    not something to discover from a truncated row in production."""
    from ppn_blogger.config_source import _STAMP_PREFIX, DOCUMENTS, STAMP_MAX_LENGTH, _names_digest

    names = sorted(DOCUMENTS) + [f"future_{n}" for n in range(4)]
    widest = f"{_STAMP_PREFIX}:{_names_digest(names)}:" + ".".join(["999"] * len(names))
    assert len(widest) <= STAMP_MAX_LENGTH


def test_a_stamp_round_trips_to_the_versions_it_recorded():
    from ppn_blogger.config_source import DOCUMENTS, config_stamp, read_config_stamp

    versions = {name: n for n, name in enumerate(sorted(DOCUMENTS), start=1)}
    assert read_config_stamp(config_stamp(versions)) == versions


def test_the_stamp_changes_when_any_single_document_does():
    """It is also the cache token Settings compares, so a state that produced the
    same string would leave the agents reading a stale ruleset."""
    from ppn_blogger.config_source import DOCUMENTS, config_stamp

    base = dict.fromkeys(DOCUMENTS, 1)
    seen = {config_stamp(base)}
    for name in DOCUMENTS:
        seen.add(config_stamp({**base, name: 2}))
    assert len(seen) == len(DOCUMENTS) + 1


def test_a_stamp_from_a_different_document_set_reads_as_unknown():
    """The reason the digest is there. Lining seven old versions up against eight
    current names would misreport every document after the one that was added —
    a confident reading of the wrong ruleset, which is worse than no reading."""
    from ppn_blogger.config_source import _STAMP_PREFIX, DOCUMENTS, _names_digest, read_config_stamp

    stale_names = sorted(DOCUMENTS)[:-1]
    stale = f"{_STAMP_PREFIX}:{_names_digest(stale_names)}:" + ".".join(["1"] * len(stale_names))
    assert read_config_stamp(stale) is None


def test_rows_written_before_this_encoding_read_as_unknown_not_as_data():
    """Existing rows hold a truncated `name:version|...`. They must not be
    reinterpreted — there is no honest way to recover what they meant."""
    from ppn_blogger.config_source import DOCUMENTS, read_config_stamp

    old = "|".join(f"{name}:1" for name in sorted(DOCUMENTS))[:64]
    assert read_config_stamp(old) is None
    assert read_config_stamp("") is None
    assert read_config_stamp("cfg1:deadbeef:not.numbers.here") is None


@pytest.mark.asyncio
async def test_a_run_records_a_configuration_it_can_be_traced_back_to(api):
    """The whole point of the column: which ruleset was this draft written under."""
    from ppn_blogger.config_source import read_config_stamp

    run_id = (await api.post("/api/runs/suggest", json={})).json()["id"]
    await _wait_for(api, run_id)

    run = (await api.get(f"/api/runs/{run_id}")).json()
    versions = read_config_stamp(run["config_version"])
    assert versions is not None
    assert versions["validation_rules"] == 1
    assert run["config_versions"] == versions


@pytest.mark.asyncio
async def test_editing_a_document_moves_the_stamp_of_the_next_run(api):
    from ppn_blogger.config_source import read_config_stamp

    first = (await api.post("/api/runs/suggest", json={})).json()["id"]
    await _wait_for(api, first)

    current = (await api.get("/api/config/topics")).json()["content"]
    await api.put("/api/config/topics", json={"content": current, "note": "no-op edit"})

    second = (await api.post("/api/runs/suggest", json={})).json()["id"]
    await _wait_for(api, second)

    before = read_config_stamp((await api.get(f"/api/runs/{first}")).json()["config_version"])
    after = read_config_stamp((await api.get(f"/api/runs/{second}")).json()["config_version"])
    assert before["topics"] == 1
    assert after["topics"] == 2
    assert after["validation_rules"] == 1
