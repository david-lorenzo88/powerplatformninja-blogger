# Management UI — architecture

Stage 1 (the service core) is built and tested. Stage 2 is the React app, which
consumes the contract below.

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
| `POST` | `/runs/suggest` | `{instruction, label}` → `202 {id}` |
| `POST` | `/runs/write` | `{topic, push, cover, translate, label}` → `202 {id}` |
| `POST` | `/runs/cover` | `{path, concept}` → `202 {id}` |
| `GET` | `/runs?status=&limit=` | newest first |
| `GET` | `/runs/{id}` | run + `nodes` (derived per-executor status) |
| `POST` | `/runs/{id}/cancel` | works on queued *and* running |
| `GET` | `/runs/{id}/events?after=N` | **SSE** |

Statuses: `queued · running · succeeded · failed · cancelled · interrupted`.
`interrupted` is applied at startup to runs a crash left mid-flight.

### The event stream

`GET /runs/{id}/events?after=<seq>` **replays from `after`, then follows live.**
A browser opening mid-run sees the whole history; a dropped connection resumes
by passing the last `seq` it saw. Sequence numbers are per-run and strictly
increasing (asserted in the tests).

```json
{"seq": 12, "kind": "node", "executor_id": "researcher",
 "level": "info", "message": "researcher active", "data": {...}, "ts": "..."}
```

`kind` is one of:

- `status` — queued / running / terminal, with `data.status`
- `node` — a workflow executor produced output; drives the canvas
- `log` — one line per tool call (`ppn.tools`) and per gate decision
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

## Running it

```bash
pip install -e ".[server]"
ppn serve                 # http://127.0.0.1:8000/api/health
PPN_MAX_CONCURRENT_RUNS=3 ppn serve
```

First start imports `config/*.yaml` into the database and logs that it did.

## Stage 2 — the React app

`ui/`, Vite + React + TypeScript. Screens: **Runs** (queue, history, launch),
**Run detail** (canvas + per-node transcript), **Config** (editor, history,
rollback), **Drafts** (review, cover, publish).

For the canvas, render the Mermaid from `/api/workflows` into React Flow nodes
once, then colour nodes from the `node` events. The graph is static per workflow
kind; only the status overlay is live.
