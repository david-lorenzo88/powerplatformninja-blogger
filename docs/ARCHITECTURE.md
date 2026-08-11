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

### News — feeds, articles, groups

A second router, `server/api_news.py`, prefix **`/api/news`**. It is separate
because `api.py` is a different domain and was already 700 lines; the service
layer had been split that way from the start.

The registry is deliberately *not* the crew's nine feeds in `config/sources.yaml`
— that list is a prompt contract three scouts depend on. These are copied in on
first boot (`ingest.seed_feeds`, idempotent by `url_hash`) and diverge from there.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/news/feeds?enabled=&realtime=&group_id=&q=` | health is derived: `ok · stale · failing · disabled` |
| `POST` | `/news/feeds` | **probes before saving** — a URL that is not a feed is a 422, never a row |
| `GET·PATCH·DELETE` | `/news/feeds/{id}` | delete is soft; `?purge=true` also removes its articles |
| `POST` | `/news/feeds/validate` | `{url}` → the feed behind it plus a five-entry preview. No write. |
| `POST` | `/news/feeds/{id}/refresh` · `/news/refresh` | **202** — an `ingest` run |
| `GET·POST` | `/news/feed-groups` · `GET·PATCH·DELETE /news/feed-groups/{id}` | |
| `PUT` | `/news/feed-groups/{id}/feeds` | `{feed_ids}` replaces membership in one call |
| `GET` | `/news/articles?group_id=&feed_id=&since=&q=&limit=` | `since` takes ISO or a bare hour count |
| `GET` | `/news/articles/{id}` | one article, with its stored content |
| `GET` | `/news/summary` | counts **plus `db_can_autopause`** |

Ingestion (`server/ingest.py`) is conditional-GET: a 304 costs one round trip and
no parsing. Dedup is a unique index on `(feed_id, entry_key)` — structural, so
"notify once" needs no bookkeeping. Failures are *recorded*, unlike
`tools.read_feeds`, which turns any error into an empty list.

### Scheduler and watch

`server/scheduler.py` is the only periodic work in the codebase. It **sleeps
until the next due time** rather than ticking: a one-minute loop would query the
database 1,440 times a day and stop Azure SQL ever auto-pausing. Ticks are
claimed with a compare-and-swap on `next_due_at`, because a Container Apps
revision swap briefly runs two schedulers.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/news/schedule` | jobs, next due, and **`db_can_autopause`** |
| `POST` | `/news/schedule/{key}/run` | force one job — `fetch · watch · newsletters · retry_deliveries · prune` |
| `GET` | `/news/pending` | every nav badge in one request, so the shell polls once not three times |

### Newsletters and issues

One agent between two code gates: `IssueBuilder → newsletter_editor →
IssuePublisher`. The editor is handed a numbered candidate list and returns
**ids**; it is never given a URL and cannot produce one. `IssuePublisher`
resolves each id back to the article it came from and drops anything that was not
offered, along with any section outside `config/newsletters.yaml`. Below
`min_items` the run ends at the builder and **no model is called**.

| Method | Path | Notes |
|---|---|---|
| `GET·POST` | `/news/newsletters` · `GET·PATCH·DELETE /news/newsletters/{id}` | `manual · interval · weekly · monthly`; response carries `upcoming` fire times |
| `GET` | `/news/newsletters/{id}/preview` | exactly what the next issue would draw from — **no model call** |
| `POST` | `/news/newsletters/{id}/generate` | **202** — a `newsletter` run |
| `GET` | `/news/newsletters/{id}/issues` · `/news/issues` | |
| `GET·PATCH` | `/news/issues/{id}` | 409 once `sending`/`sent` |
| `GET` | `/news/issues/{id}/html` | the rendered email, for the sandboxed preview frame |

### Delivery

| Method | Path | Notes |
|---|---|---|
| `GET` | `/news/channels` | `webpush · manual · email · telegram · whatsapp`, each with `configured` |
| `GET·POST` | `/news/recipients` · `PATCH·DELETE /news/recipients/{id}` | addresses normalised, so the same person cannot be added twice |
| `POST` | `/news/recipients/{id}/test?issue_id=` | one send, no delivery row — what you use before trusting a channel |
| `POST` | `/news/issues/{id}/send` · `/news/issues/{id}/retry` | **202** — a `deliver` run; retry touches only failed rows |
| `GET` | `/news/issues/{id}/deliveries` | per-recipient outcome with the provider's own error text |

Every `deliveries` row is written `pending` **before** the first send — intent
durable before side effect. No `Channel.send` may raise. A *permanent* failure
(bad address, unapproved template) is tried once and parks the recipient; only
transient ones are retried, with a 2/10/60-minute backoff. An unconfigured
channel is `skipped`, not `failed`.

### Feed discovery

| Method | Path | Notes |
|---|---|---|
| `POST` | `/news/discover?instruction=` | **202** — a `discover` run |
| `GET` | `/news/feed-reviews?status=` · `/news/feed-reviews/{id}` | |
| `POST` | `/news/feed-reviews/{id}/decide` · `/news/feed-reviews/{id}/cancel` | decide once; a URL not in the review is a 409 |

A sweep asks a model where to look, then **fetches and parses every URL it names
before the review row is written**. Anything that does not resolve to a real feed
with entries is discarded, so approving cannot mean adding a URL nobody checked.
Refusals are remembered in `declined_feeds` and never offered again. The verdict
follows `reviews.decide`'s ordering: create the feeds, *then* close the review.

## Run kinds

`suggest · explore · shortlist · write · cover · ingest · newsletter · deliver · discover`

Adding one touches six places: `runs.py:_dispatch`, a 202 endpoint,
`catalog.record_run_result`, `push._describe`, `api.workflows()`, and the
`Run.kind` comment. `_dispatch` wraps nothing, so **a new kind must apply its own
`asyncio.wait_for`** — the existing `suggest`/`write` ceilings are read only by
`cli.py`.

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
**Config** (editor, history, rollback), plus the news subsystem: **News**
(article stream, feeds, groups, feed reviews) and **Letters** (newsletters,
issues, recipients). Build/run notes: [ui/README.md](../ui/README.md).

The canvas parses the Mermaid from `/api/workflows` into React Flow nodes once
(`ui/src/lib/parseMermaid.ts` + dagre layout), then colours nodes from the folded
event log. The graph is static per workflow kind; only the status overlay is live.
Both the canvas and the per-node transcript read from one `deriveNodes()`
(`ui/src/lib/deriveNodes.ts`), a faithful port of `derive_nodes()` here in the
server — fold once, so they cannot drift apart.
