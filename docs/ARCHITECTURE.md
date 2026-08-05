# Management UI — architecture

Stage 1 (the service core) is built and tested. Stage 2 (the React app in `ui/`)
is **built and verified** — it consumes the contract below. This document is the
API contract; the frontend's own build/run notes are in [ui/README.md](../ui/README.md).

## Shape

```
React SPA (ui/)                    ← stage 2
      │  REST + SSE
┌─────▼──────────────────────────────────────────────┐
│ FastAPI  (server/api.py, server/app.py)            │
│                                                    │
│  RunManager (server/runs.py)                       │
│    • asyncio queue + N workers  ← concurrency cap  │
│    • per-run event log + live fan-out              │
│    • per-run log capture via contextvar            │
│                                                    │
│  ConfigStore (server/config_store.py)              │
│    • versioned documents, seeded from config/*.yaml│
└─────┬──────────────────────────────────────────────┘
      │ same objects the CLI uses
      ▼
  agents · workflows · executors · covers · wordpress
```

The CLI and the server are two clients of the same core. Nothing in
`agents.py`, `prompts.py` or `workflows.py` knows the server exists.

## The three seams that make this work

**1. `on_event` already existed.** `discover_topics` and `write_post` take an
`on_event` callback (added for the CLI spinner). The server passes a callback
that writes to the run's event log. No workflow code changed.

**2. Config became swappable, not relocated.** `config_source.py` defines a
`ConfigSource` protocol. The CLI uses `YamlConfigSource`; the server installs a
database-backed one. `Settings` reads through whichever is active and
re-reads when its version token changes — so a config edit in the UI applies to
the *next run* with no restart, and no prompt code was touched.

**3. The graph draws itself.** `WorkflowViz(workflow).to_mermaid()` renders the
real workflow object. The canvas can never drift from the code, and
`GET /api/workflows` builds with the offline stub client so drawing a diagram
needs no Azure credentials and costs no tokens.

## Azure-readiness

| Concern | Local now | Azure later |
|---|---|---|
| Database | SQLite via `PPN_DATABASE_URL` | Point the same var at `postgresql+asyncpg://` |
| Queue | asyncio queue inside `RunManager` | Replace `RunManager._queue`/`_worker` with Storage Queue + a worker container |
| Artefacts | `drafts/` on disk | `server/drafts.py` is the only module that touches the filesystem |
| Auth | none | FastAPI dependency + Entra; the SPA is already a separate origin (CORS configured) |

Everything crossing a process boundary later is already behind one class.

## API contract

Base: `/api`

### Runs

| Method | Path | Notes |
|---|---|---|
| `POST` | `/runs/suggest` | `{instruction, label, explore}` → `202 {id}`. `explore: true` enqueues kind `explore` instead of `suggest`. |
| `POST` | `/runs/write` | `{topic, push, cover, translate, label}` → `202 {id}` |
| `POST` | `/runs/cover` | `{path, concept}` → `202 {id}` |
| `GET` | `/runs?status=&limit=` | newest first |
| `GET` | `/runs/{id}` | run + `nodes` (derived per-executor status) |
| `POST` | `/runs/{id}/cancel` | works on queued *and* running |
| `GET` | `/runs/{id}/events?after=N` | **SSE** |

Statuses: `queued · running · succeeded · failed · cancelled · interrupted`.
`interrupted` is applied at startup to runs a crash left mid-flight.

Kinds: `suggest · explore · shortlist · write · cover`. Each has its own graph in
`GET /workflows`, which is why exploration is a separate kind rather than a flag —
the canvas is keyed by kind.

### Source reviews

An `explore` run finishes `succeeded` with
`result = {awaiting_source_approval: true, review_id, candidate_count, new_count,
signal_count}` and produces **no suggestions**. The candidate sites wait in
`source_reviews` until a human answers; approving them files the verdict into
`sources.yaml` and (by default) enqueues the `shortlist` run that finishes the job.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/source-reviews?status=&limit=` | newest first; `status=pending` is the inbox |
| `GET` | `/source-reviews/{id}` | adds `candidates`, `decisions` and the `tiers` menu (read from `sources.yaml`, never hard-coded client-side) |
| `POST` | `/source-reviews/{id}/decide` | `{decisions: [{domain, approved, tier}], start_shortlist, instruction, label}` → `{review_id, approved, declined, config_version, run_id}` |
| `POST` | `/source-reviews/{id}/cancel` | drop a pending review without deciding |

`decide` is where the config write happens, before the review is marked decided: a
crash between the two leaves a review that can simply be approved again, whereas the
reverse order would leave approved sources in a "decided" review that never reached
the config. It returns `409` for a review that was already answered or a domain that
was not in it, and `404` for an unknown review. `run_id` is empty when
`start_shortlist` is false or nothing was approved.

### The event stream

`GET /runs/{id}/events?after=<seq>` **replays from `after`, then follows live.**
A browser opening mid-run sees the whole history; a dropped connection resumes
by passing the last `seq` it saw. Sequence numbers are per-run and strictly
increasing (asserted in the tests).

```json
{"seq": 12, "kind": "node", "executor_id": "researcher",
 "level": "info", "message": "researcher completed",
 "data": {"type": "executor_completed", "output": "..."}, "ts": "..."}
```

`kind` is one of:

- `status` — queued / running / terminal, with `data.status`
- `node` — an executor lifecycle event; drives the canvas. `data.type` is
  `executor_invoked` (lights the node) or `executor_completed` (carries the
  agent's finished output in `data.output`, when it can be extracted cleanly).
- `log` — a tool call (`ppn.tools`) or gate decision, **or** a chunk of an
  agent's streamed output (`data.type == "output"`), flushed in ~1 KB pieces so
  the UI fills in as the agent produces text. Concatenating an executor's output
  chunks reconstructs its full result; this is the reliable source for the
  streaming agents, while `data.output` covers structured ones (e.g. the
  publisher's `TopicSuggestionSet`).
- `eof` — terminal; the stream closes after this

Node status for the canvas is **derived from the same log** (`GET /runs/{id}`
returns `nodes`), so the graph and the transcript can never disagree, and a
finished run replays with the same animation as a live one.

### Config

| Method | Path | Notes |
|---|---|---|
| `GET` | `/config` | all documents with current versions |
| `GET` | `/config/{name}` | current content |
| `PUT` | `/config/{name}` | `{content, note}` → new version. **422 with the parser message on invalid YAML** |
| `GET` | `/config/{name}/history` | version list |
| `GET` | `/config/{name}/versions/{v}` | one version, for diffing |
| `POST` | `/config/{name}/rollback/{v}` | appends the old content as a new version |

Documents: `blog_profile`, `topics`, `sources`, `validation_rules` (YAML) and
`style_guide` (markdown).

**On the git trade-off.** Config used to live in git-tracked YAML, so rule
changes showed up in `git log`. In the database they do not. Every write is
therefore append-only: you keep full history, diffs and rollback in-app, and a
rollback is itself a new version rather than a rewrite. The YAML files are
imported once on first start and then stop being authoritative — keep them for
reference, but edit through the UI.

### Drafts

`GET /drafts` · `GET /drafts/{name}` (markdown + review report) ·
`PUT /drafts/{name}` · `GET /drafts/{name}/cover` · `POST /drafts/{name}/publish`

Paths are resolved inside `drafts/` and rejected otherwise.

### Catalog — topic ideas, posts, versions

The crew's artefacts stay as files; three DB tables (`topic_ideas`, `posts`,
`draft_versions` in `server/db.py`) index them so the UI can browse the backlog,
link ideas to the posts they became, and keep a version history.
`server/catalog.py` owns the tables: it writes rows when a run finishes
(`record_run_result`, called from `RunManager`) and reconciles existing runs and
on-disk drafts on first start (`backfill`, idempotent — see the lifespan).

| Method | Path | Notes |
|---|---|---|
| `GET` | `/topic-ideas?watch_area=&post_format=&has_draft=&min_score=&q=` | one row per suggestion, deduped by slug; `has_draft` says whether a post exists |
| `GET` | `/topic-ideas/{id}` | full idea + the posts written from it |
| `GET` | `/posts?status=&approved=&has_cover=&published=&q=` | one row per logical draft, with `version_count` + a `current_version` summary |
| `GET` | `/posts/{id}` | post + linked `topic_idea` + all `versions` |
| `GET` | `/posts/{id}/versions` · `GET /draft-versions/{id}` | version rows; `markdown_file` resolves through `/drafts/{name}` |
| `POST` | `/posts/{id}/regenerate` | `{instructions, reuse_research, push, cover}` → **202 {id}**; enqueues a `write` run that appends a new version |

A regeneration reuses the `write` run kind: with `reuse_research` it loads the
saved dossier and skips the source check (`write_post_from_dossier`), otherwise it
researches afresh; either way the `instructions` reach the writer's first-draft
prompt as an `<editor_instructions>` block. Each save claims its own filename
(`<date>-<slug>[-N].md`), so a version's markdown is never overwritten by the next.

## Running it

```bash
pip install -e ".[server]"
ppn serve                 # http://127.0.0.1:8000/api/health
PPN_MAX_CONCURRENT_RUNS=3 ppn serve
```

First start imports `config/*.yaml` into the database and logs that it did.

## Stage 2 — the React app (built)

`ui/`, Vite + React + TypeScript (Tailwind, TanStack Query, React Flow, CodeMirror).
Screens: **Runs** (queue, history, launch), **Run detail** (canvas + per-node
output), **Topic Ideas** (filterable backlog → idea detail with a "write a draft"
action), **Drafts** (filterable posts → post detail with version history, links to
the source idea and WordPress, publish, and a regenerate-with-instructions dialog),
**Config** (editor, history, rollback). Build/run notes: [ui/README.md](../ui/README.md).

The canvas parses the Mermaid from `/api/workflows` into React Flow nodes once
(`ui/src/lib/parseMermaid.ts` + dagre layout), then colours nodes from the folded
event log. The graph is static per workflow kind; only the status overlay is live.
Both the canvas and the per-node transcript read from one `deriveNodes()`
(`ui/src/lib/deriveNodes.ts`), a faithful port of `derive_nodes()` here in the
server — fold once, so they cannot drift apart.
