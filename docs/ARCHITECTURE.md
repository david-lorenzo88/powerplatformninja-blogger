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
| `POST` | `/runs/write` | `{topic \| brief, sources, allow_unreachable, instructions, push, cover, translate, label}` → `202 {id}` |
| `POST` | `/runs/cover` | `{path, concept}` → `202 {id}` |
| `GET` | `/runs?status=&limit=` | newest first |
| `GET` | `/runs/{id}` | run + `nodes` (derived per-executor status) + `usage` |
| `GET` | `/runs/{id}/usage` | `{total, agents[]}` — the per-agent cost breakdown |
| `POST` | `/runs/{id}/cancel` | works on queued *and* running |
| `GET` | `/runs/{id}/events?after=N` | **SSE** |

`/runs/write` takes **either** a `topic` — one the crew proposed — **or** a
`brief` in the operator's own words, and 422s on both or neither. A brief is a
corpus run: the links in it (plus any in `sources`) are the only pages the post
may be built from. They are fetched before the run is queued, so a 422 listing
dead links is the normal answer to a typo; `allow_unreachable` starts it anyway
and keeps the dead link in the corpus, because the crew reporting what it could
not read beats a silently shorter list. Reachable links are replaced by where
they actually landed, and the interpreted topic is written back onto the run's
`params` so the UI can show what it decided.

Statuses: `queued · running · succeeded · failed · cancelled · interrupted`.
`interrupted` is applied at startup to runs a crash left mid-flight.

Kinds: `suggest · explore · shortlist · write · cover`. Each has its own graph in
`GET /workflows`, which is why exploration is a separate kind rather than a flag —
the canvas is keyed by kind.

### Cost

Every run carries a `usage` object — `null` when it called no model, which is
deliberately not the same as costing zero. Counts (`total_tokens`, `searches`,
`images`, `records`) are exact; `cost_micros` is those counts times a rate from the
`model_prices` document, so it is an **estimate at list price**. `priced: false`
means some model in the run had no configured rate: the tokens are still real and
the money is unknown, and the UI must not render that as zero.

Money is integer **micros** of `currency` end to end — never a float. These get
summed across thousands of rows to answer "what did last month cost".

| Method | Path | Notes |
|---|---|---|
| `GET` | `/usage?since=&until=&group_by=day\|kind&limit=` | rollup + `top_runs`. `since`/`until` take an ISO timestamp or a bare number of hours. An unreadable bound is a **422**, not a silently widened window — a spend figure over the wrong period looks like an answer. |
| `GET` | `/prices/candidates?model=` | the handful of retail meters that could price a model, for binding |
| `POST` | `/prices/refresh` | `{apply}` → the diff; with `apply: true`, saves a new `model_prices` version |

Prices are bound to exact meter names by a human once, then refreshed verbatim —
see [CONFIGURATION.md](CONFIGURATION.md) Part 6 for why matching them
automatically is not safe.

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

### Learning — what your edits taught the crew

Nothing here applies a change. `POST /learning-reviews/{id}/decide` is the only
write, and it takes a human's verdict.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/learning/metrics` | Share published unchanged, mean edit rate, the trend, edits by section, discard rate. All arithmetic over stored pairs — no model produces a figure. |
| GET | `/api/delta-pairs` | Every captured draft-and-published pair. `?status=` filters. |
| GET | `/api/delta-pairs/{id}` | One pair with its hunks, section diff, observations and the config versions the run read. The hunks are computed once on the server; the client renders and never diffs. |
| GET | `/api/learning/candidates` | Patterns accruing evidence, with their recurrence and gate status. |
| GET | `/api/learning/declined` | Patterns refused, which are never offered again. |
| POST | `/api/learning/sweep` | 202, `{id, run_id}` — enqueues a `learn` run. |
| GET | `/api/learning-reviews` | Pending and decided reviews. |
| GET | `/api/learning-reviews/{id}` | The proposals, each with its rendered document, the gate's report and the edits that motivated it. |
| POST | `/api/learning-reviews/{id}/decide` | `{decisions: [{fingerprint, approved, reason}]}`. 404 unknown, 409 already decided or naming something the review did not offer. Approving writes a new config version; declining is remembered. |
| POST | `/api/learning-reviews/{id}/cancel` | `{cancelled}`. |

`GET /api/news/pending` gains `learning_reviews`, so the nav badge is still one
poll for every queue.

A run now also carries `config_versions`: the decoded version of each config
document it read, or `null` when the stamp predates the encoding. See the
`Run.config_version` note in `docs/STATUS.md`.

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
`PUT /drafts/{name}` · `GET /drafts/{name}/cover` ·
`POST /drafts/{name}/publish?status=&cover=`

Paths are resolved inside `drafts/` and rejected otherwise. A publish sends the
cover as the featured image unless `cover=false`.

### Catalog — topic ideas, posts, versions

The crew's artefacts stay as files; three DB tables (`topic_ideas`, `posts`,
`draft_versions` in `server/db.py`) index them so the UI can browse the backlog,
link ideas to the posts they became, and keep a version history.
`server/catalog.py` owns the tables: it writes rows when a run finishes
(`record_run_result`, called from `RunManager`), when a draft is published
(`record_publish`) or when a cover push discovers where a post lives
(`record_cover_publish`), and reconciles existing runs and on-disk drafts on
first start (`backfill`, idempotent — see the lifespan).

`backfill` also fills in `wordpress_post_id` from `.ppn_state/wp_posts.json`
(`_backfill_wordpress_ids`), which repairs posts published before publishing
recorded anything. It only ever fills a blank, and needs no network.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/topic-ideas?watch_area=&post_format=&has_draft=&min_score=&q=` | one row per suggestion, deduped by slug; `has_draft` says whether a post exists |
| `GET` | `/topic-ideas/{id}` | full idea + the posts written from it |
| `GET` | `/posts?status=&approved=&has_cover=&published=&q=` | one row per logical draft, with `version_count` + a `current_version` summary |
| `GET` | `/posts/{id}` | post + linked `topic_idea` + all `versions` |
| `GET` | `/posts/{id}/versions` · `GET /draft-versions/{id}` | version rows; `markdown_file` resolves through `/drafts/{name}` |
| `POST` | `/posts/{id}/regenerate` | `{instructions, reuse_research, push, cover}` → **202 {id}**; enqueues a `write` run that appends a new version |
| `POST` | `/posts/{id}/cover` | `{instructions}` → **202 {id}**; enqueues a `cover` run that overwrites `covers/<slug>.png` |
| `POST` | `/posts/{id}/cover/wordpress` | uploads that image and sets it as the post's featured image → `PublishTarget`. Synchronous, and it **reports failures** (502) rather than swallowing them — nothing is at risk here and someone is waiting for the answer |

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
