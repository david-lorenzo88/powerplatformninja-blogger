# Status

Last updated: 2026-08-05. The React management UI (Stage 2) is **built, verified
and merged into `main`** (PR #1). Read this before starting anything.

---

## Where things stand

- **Stage 1** (crew + CLI + FastAPI service): built, tested, documented.
- **Stage 2** (React management UI in `ui/`): built, verified against real Azure, and
  **merged into `main`** via
  **[PR #1](https://github.com/david-lorenzo88/powerplatformninja-blogger/pull/1)**.
- **GitHub**: the repo exists and is **public**. `main` now carries the crew, the docs,
  the ignore rules for generated output, the React UI and the run-event enrichment —
  everything below is on `main`.

---

## Verified against real Azure

One complete **production run** (CLI), 2026-07-28 — *"Advanced connector policies:
migrate your DLP safely"*, the entire pipeline end to end:

| Stage | Result |
|---|---|
| Topic discovery | 6 suggestions |
| Researcher | dossier written to `research/` |
| Source Checker | **passed** |
| Writer + validators | **approved**, score **93.5**, 3 revision rounds, 0 blockers |
| MAI cover | generated, uploaded as media **1244** |
| WordPress push | post **1245**, status `draft`, featured image set |

Then, during the UI build (2026-07-28), several **`suggest` runs driven through the
server and the UI**, end to end: the queue, the SSE stream, the live canvas, per-node
output and replay-then-follow all confirmed against live Foundry — not just the stub.

**The `write` pipeline has not yet been driven through the server/UI** — only through
the CLI (the production run above). That is the most valuable next real test.

---

## The management UI (Stage 2) — built

Location: **`ui/`**. Stack: Vite + React + TypeScript, Tailwind v4, TanStack Query,
React Flow (`@xyflow/react`) + dagre, CodeMirror 6, react-markdown. Full dev notes in
**[ui/README.md](../ui/README.md)**.

Four screens:

1. **Runs** — history, live status, visible queue depth, launch (suggest/write),
   cancel. A `write` is launched by picking a suggestion from a completed `suggest`
   run (a valid `TopicSuggestion`), with a raw-JSON escape hatch.
2. **Run detail** — the workflow drawn as a canvas that lights up live from the SSE
   stream; clicking a node shows that agent's **output and log lines**. Topology from
   `GET /api/workflows`; node state folded client-side by `ui/src/lib/deriveNodes.ts`,
   a faithful port of `derive_nodes()` in `server/api.py`.
3. **Config** — edit the five documents in CodeMirror with history and rollback;
   invalid YAML shows the server's **422 parser message inline**.
4. **Drafts** — read/edit the Markdown, view the review report and cover, publish to
   WordPress behind a confirm dialog.

**Server enrichment shipped with the UI.** `server/runs.py` `_on_event` used to
collapse every workflow event into a `"{agent} active"` ping (thousands per run, no
content). It now emits, per executor: `executor_invoked` (lights the node), the
streamed output flushed in ~1 KB chunks, and `executor_completed` with the finished
result. Observability only — no crew behaviour, no routing change, both loops
untouched, `derive_nodes` folds it exactly as before. See the event stream section in
[ARCHITECTURE.md](ARCHITECTURE.md).

*Known nuance:* on real Foundry responses the completed event's `data.output` comes
back empty for text-streaming agents (their `AgentResponse.text` is blank), so the UI
reconstructs the full output from the streamed chunks; structured agents (e.g. the
publisher's `TopicSuggestionSet`) populate `data.output` directly. Both paths covered.

### Running it in dev

Two processes (see ui/README.md):

```bash
ppn serve                 # API on http://127.0.0.1:8000   (from the repo root)
cd ui && npm install && npm run dev   # SPA on http://localhost:5173
```

Vite proxies `/api` (REST + SSE) to the API server, so both are same-origin in dev.
Override the proxy target if 8000 is taken: `PPN_API_TARGET=http://127.0.0.1:8008 npm run dev`.
Production is a single process: `npm run build` → `ui/dist`, served by `ppn serve`.

---

## Catalog: topic ideas & draft management (new)

A DB-backed catalog now indexes the crew's artefacts so the UI can manage the
backlog end to end. **Built and tested offline; not yet driven against real Azure.**

- **Three tables** (`server/db.py`): `topic_ideas` (deduped by slug), `posts` (the
  logical draft, one per subject), `draft_versions` (the version chain). Content
  (markdown/review/cover) stays as files; the DB holds the index, links and versions.
- **`server/catalog.py`** populates rows when a run finishes and **backfills** from
  existing runs + on-disk drafts on first start (idempotent, runs in the lifespan).
- **New endpoints** (`server/api.py`): `/topic-ideas`, `/posts`, `/posts/{id}`,
  `/posts/{id}/versions`, `/draft-versions/{id}`, and `POST /posts/{id}/regenerate`.
  See the Catalog table in [ARCHITECTURE.md](ARCHITECTURE.md).
- **Regeneration** reuses the `write` kind with `{instructions, reuse_research}`;
  instructions reach the writer as an `<editor_instructions>` block (both the fresh
  and resume-from-dossier prompt sites). `save_draft` is now non-clobbering
  (`<date>-<slug>[-N].md`) so version markdown is never overwritten.
- **UI** (`ui/`): new **Topic Ideas** section (filterable list + detail with a
  "write a draft" action) and an evolved **Drafts** section (filterable posts list +
  post detail with version history, idea/WordPress links, publish, and a regenerate
  dialog with a reuse-research toggle).
- **Tests**: 10 new (7 server + 3 pipeline), all offline; suite is **48 passing**.
- **Next real test:** drive a suggest → write → regenerate through the deployed
  server/UI to confirm population, links and the reuse-research path against Azure.

## Exploration mode & source review (new)

Topic discovery could only draw on the nine curated feeds and Microsoft Learn, which
kept the research defensible and the shortlist repetitive. Exploration mode lets the
scouts range across the open web and then **stops for a human verdict on every site
they read** before a single topic is proposed. **Built and tested offline; not yet
driven against real Azure.**

- **Two graphs, one decision between them** (`workflows.py`):
  `build_source_exploration_workflow()` ends at `source_harvester`; once sites are
  approved, `build_shortlist_workflow()` (`scout_replay → topic_editor →
  topic_publisher`) finishes the job. The approval sits between two *runs*, so no
  worker is held open for a human and a restart mid-review costs nothing.
- **`sources.py`** — pure: harvest candidates from the scouts' own URLs, filter
  reports to approved sites, and merge the verdict into `sources.yaml` **line by
  line** so the file's comments survive (verified against `apply_decisions`, with a
  full-dump fallback).
- **`source_reviews` table + `server/reviews.py`** with
  `/api/source-reviews{,/{id},/{id}/decide,/{id}/cancel}`. Run kinds are now
  `suggest · explore · shortlist · write · cover`.
- **Approved sites are permanent**: each lands in `sources.yaml` at the chosen trust
  tier as a new config version, so it is trusted by later topic runs *and* by the
  Researcher and Source Checker on later drafts. Refused new sites go to
  `declined_domains` and are never offered again.
- **UI**: a *Search the whole web* toggle on the suggest dialog, a **Sources**
  section (pending count badged in the nav) with per-site checkboxes, trust-tier
  dropdowns and the findings behind each site, and a banner on a finished sweep
  linking straight to its review.
- **`ppn suggest --explore`** does the same in one process with a terminal prompt
  (`--yes` to accept everything at its suggested tier).
- **Also fixed here:** `config_store` used to re-serialise `config/*.yaml` on import,
  which stripped every comment from the documents shown in the Config screen. It now
  stores the file text verbatim.
- **Tests**: 14 new (8 pure, 5 server, 1 pipeline); suite is **67 passing**.
- **Next real test:** an `--explore` sweep against live Foundry — the candidate list
  is only as good as what the scouts actually report, and the stub cannot tell you
  whether a real wide sweep returns 8 useful new domains or 40 useless ones.

## News aggregation — phase 1 of 5 (new)

The first half of the news/newsletter subsystem: a managed feed registry with
persistent, deduplicated harvesting. **Built, tested offline, and verified
against real feeds** (see below). No models, no third-party accounts, no
scheduler yet.

- **`src/ppn_blogger/news.py`** — the pure layer: `canonical_url`, `url_hash`,
  `entry_key`, `fetch_feed` with **conditional GET**, `fetch_many` over one
  shared connection pool, `probe`, `discover_feeds_in_html`. Never raises;
  a failure is a `FeedFetch` carrying its reason.
- **Four tables** (`server/db.py`): `feeds`, `articles`, `feed_groups`,
  `feed_group_members`. URLs are stored as `Text` and indexed through a
  `String(64)` sha256 of the canonical form — article URLs blow past the ~450
  characters an indexable NVARCHAR holds on Azure SQL. The unique
  `(feed_id, entry_key)` index **is** the dedup guarantee.
- **`server/ingest.py`** and **`server/news_store.py`** — polling, select-then-write
  upserts, per-feed health, auto-disable after `PPN_FEED_MAX_FAILURES`, and
  `seed_feeds()` copying the nine `sources.yaml` feeds in on first boot
  (idempotent, like `catalog.backfill()`).
- **New run kind `ingest`**, so a fetch appears in the Runs list with the same
  log, cancellation and history as everything else. It is the first kind to
  apply its own `asyncio.wait_for` — the manager applies none.
- **`server/api_news.py`** — a second `APIRouter`, prefix `/api/news`.
  No route carries a trailing slash (see the note in `ui/src/api/client.ts`).
- **UI**: the bottom tab bar was regrouped from five destinations to four —
  Ideas, Drafts and Sources merged behind **Blog** with a `SubNav`, which is what
  pays for **News** (Stream · Feeds · Groups) and leaves a slot for Letters.
- **Tests**: 70 new (30 pure, 27 store/ingest, 13 API); suite is **137 passing**.

**Verified against real feeds** (2026-08-10, three live sources — Simon Willison,
the .NET blog, arXiv cs.AI): discovery found the feed behind a bare site URL;
140 articles harvested on the first poll; **all three returned 304 Not Modified
on the second**, exercising all three validator strategies (Last-Modified only,
both, ETag only); a duplicate feed differing only by a trailing slash and a
`utm_source` was rejected; a dead host recorded `status=0` and its reason rather
than raising.

**One bug caught by this work worth knowing about:** SQLite returns *naive*
datetimes from a `DateTime(timezone=True)` column while Azure SQL returns aware
ones, so comparing a stored timestamp to `utcnow()` raises on one backend and
silently works on the other — the worst possible shape, since the tests run on
the forgiving one. `db.as_utc()` now exists for exactly this, and there is a
regression test.

**Next:** phase 2 is the scheduler (sleep-until-due, with a compare-and-swap
guard — one replica is *not* one process during a Container Apps revision swap)
and real-time push. Phases 3-5 are newsletter generation, delivery
(ACS email / Telegram / WhatsApp) and feed auto-discovery.

## The SQLite/Azure SQL seam — closed (new)

Two production incidents came from the same place: every test ran on SQLite and
production runs on Azure SQL, and SQLite is the more forgiving dialect in exactly
the ways that matter.

| Bug | SQLite | Azure SQL |
|---|---|---|
| `.is_(True)` → `WHERE enabled IS 1` | accepted | `Incorrect syntax near '1'` — every ingest died |
| `DateTime(timezone=True)` read back | naive | aware — comparing to `utcnow()` raised |

Both were green through the whole suite. Three guards now, deliberately layered:

- **`PPN_DATABASE_URL` has no default.** It used to fall back to SQLite silently,
  so nothing ever announced which dialect it was on. An unconfigured environment
  now raises with the line to paste into `.env`.
- **CI runs the suite against real SQL Server** — a service container in
  `deploy.yml`, with the same ODBC Driver 18 install the Dockerfile uses.
  Locally `pytest` still uses a temp SQLite file and needs no services;
  `tests/conftest.py` chooses from `PPN_TEST_DATABASE_URL`.
- **`tests/test_sql_portability.py`** compiles statements against the SQL Server
  dialect *and* greps the source for the pattern, so a `.is_(True)` written later
  fails in CI rather than in Azure. Verified it fails when the bug is reintroduced.

**Verified:** the full suite, 145 tests, run against a real SQL Server engine in
a container (Azure SQL Edge locally; CI uses SQL Server 2022). That is the first
time the schema and queries have been exercised on the production dialect at all.

## News phase 2 — scheduler and real-time watch (new)

The feeds now keep themselves fresh, and a watched source buzzes the phone.
**Built, tested on both backends, and driven end to end against a live feed.**

- **`server/scheduler.py`** — three jobs (`fetch`, `watch`, `prune`) on durable
  due-times in a `scheduler_jobs` table. It **sleeps until the next due time
  rather than ticking**: a one-minute loop would query the database 1,440 times a
  day and end Azure SQL's auto-pause by itself. An `asyncio.Event` makes an edit
  take effect at once, which is what makes a long sleep acceptable.
- **Ticks are claimed with a compare-and-swap.** `minReplicas: 1` does not mean
  one process — Container Apps starts the new revision before draining the old,
  so every deploy briefly runs two schedulers. `UPDATE ... WHERE next_due_at =
  <what we read>` lets exactly one win, on any dialect.
- **Missed ticks collapse.** A job due six times over fires once; the next due
  time is computed from *now*, not from the one that was missed.
- **Only the full sweep is a visible run.** The watch job runs inline: at fifteen
  minutes it would file ~96 run rows a day and bury the Runs screen.
- **`server/watch.py`** — notify once (structurally, via the unique
  `(feed_id, entry_key)` index), stamp `notified_at` *before* sending so a crash
  costs a missed notification rather than a duplicate, one notification per feed
  rather than per article, a summary above the per-feed cap, a single rolled-up
  line above the per-tick cap, and quiet hours that suppress **without** stamping
  so the backlog survives the window.
- **`GET /api/news/schedule`** reports `db_can_autopause`, and the Feeds screen
  shows it: watching even one feed closely means the database never idles, which
  is the difference between near-zero and roughly $150-200/month. That line sits
  next to the toggle that causes it.
- **Tests**: 23 new; suite is **168 passing**, green on SQLite *and* on a real
  SQL Server engine in a container.

**Verified end to end** (2026-08-10): with no watched feeds `db_can_autopause` is
true; adding one flips it false and the cadence to 15 minutes; a tick fired all
three jobs, harvested 30 real articles from a live feed and sent exactly **one**
coalesced notification; the following tick correctly found nothing due.

**Still off by default.** `PPN_SCHEDULER_ENABLED=false` everywhere except Bicep,
so nothing polls until the deployment says so.

## News phase 3 — newsletter generation (new)

Issues are generated from feed groups on a schedule and read in the app.
**Deliberately no sending yet** — delivery is phase 4, so nothing can reach a
recipient before the mechanism for it has been reviewed on its own.

- **Four tables** (`server/db.py`): `newsletters`, `newsletter_groups`,
  `newsletter_issues`, `newsletter_issue_items`. An issue's markdown/HTML live in
  columns rather than files — a deliberate exception to the crew's "content is
  files" rule, because an issue is the *payload* of a delivery and storing the
  rendered HTML makes a re-send byte-identical.
- **`config/newsletters.yaml`** — a new versioned config document holding
  editorial policy only: the section taxonomy, blurb and headline caps,
  include/exclude rules, banned phrases, brand colour. Tunable in the Config
  screen with history and rollback, no deploy.
- **One agent between two code gates** (`build_newsletter_workflow`):
  `IssueBuilder → newsletter_editor → IssuePublisher`.
  - The **only branch in the graph is an integer comparison**: below
    `min_items` the run finishes `skipped` and **no model is called at all**.
  - **`IssuePublisher` is the anti-fabrication gate.** The editor refers to
    articles by id and is never given a URL; anything it names that was not in
    the candidate list is dropped, as is any section outside the taxonomy. An
    email cannot be un-sent.
- **`GET /newsletters/{id}/preview`** returns exactly what the next issue would
  draw from, **with no model call** — the cheapest way to tune a newsletter.
- **`newsletter_render.py`** — markdown, email HTML (every style inlined, single
  column, absolute URLs only, `javascript:` dropped) and plain text. Built from
  the composed issue rather than by converting the markdown, so there is nothing
  to sanitise.
- **Schedules** are pure and previewable: `manual | interval | weekly | monthly`,
  computed in the newsletter's own zone, with the next three fire times shown in
  the UI. Monthly is capped at day 28 — "the 31st" silently meaning "the 28th" in
  February is a schedule that lies. Claimed by the scheduler with the same
  compare-and-swap as the system jobs.
- **UI**: a fifth `Letters` tab (the ceiling), newsletter list/detail with a live
  candidate preview and schedule editor, and an issue screen whose email preview
  is a **sandboxed iframe** — email HTML carries its own inlined styles and would
  wreck the app shell.
- **Tests**: 21 new; suite is **190 passing**.

**Verified end to end offline** against a live feed's articles: 10 candidates,
the stub's fabricated id and invented section both dropped, one real item
surviving with its URL taken from the candidate row, and a weekly schedule
resolving correctly across the Madrid offset.

**Next:** phase 4 is delivery (ACS email, Telegram, WhatsApp) and phase 5 is feed
auto-discovery.

## Still open / not yet exercised

- **`write` run through the server/UI** — only the CLI has done a real write. Drive one
  from the Runs screen to prove the writer/validator streaming and the Drafts publish.
- **The Translator** — wired, unit-tested against the stub, never run for real.
- **`ppn write --dossier` (resume)** — unit-tested, never used to rescue a real run.
- **`ppn preflight`** — never run against the deployment.

---

## Done this session (2026-07-28)

- Confirmed the GitHub repo already existed and both base commits were pushed; committed
  and pushed the project docs (CLAUDE.md, this file, README link) to `main` as `ef1da27`.
- **Generated-output decision made:** `drafts/`, `research/`, `topics/` are gitignored
  (each keeps a tracked `.gitkeep`); nothing generated reaches the public repo.
- **Re-pushed post 1245** — the `core/code` blocks now serialise cleanly (verified via
  `wp preview`; David to confirm in Gutenberg).
- **Built the React UI** (four screens) and the run-event enrichment; opened PR #1.
- Fixed a naive-UTC timestamp bug in the UI (`ui/src/lib/format.ts`): the server emits
  naive UTC ISO strings, so durations were off by the local offset until parsed as UTC.

---

## Recent fixes worth knowing about

**Gutenberg code blocks.** Three serialisation mismatches against `core/code`:
`html.escape` turning quotes into `&quot;`, a `class` attribute on `<code>`, and an
unescaped `[`. Fixed in `escape_code()`; regression tests assert the serialisation.

**No in-body images (editorial v2).** The blog carries no in-body images at all;
rule S11 blocks any, and the Markdown-to-Gutenberg converter has no image path. The
old `[SCREENSHOT: ...]` / `![alt](IMAGE:slug)` handling and the empty `core/image`
slot are gone.

**`temperature` on `gpt-5`.** Killed a production run six minutes in. `supports_temperature`
infers from the model name; `ppn preflight` verifies it. This is why `--dossier` exists.

**Intermittent test hang (~50%).** Leaked aiosqlite worker threads. Fixed with
`NullPool`, a single event-writer task, and `engine().dispose()` in the lifespan.

---

## Environment notes

Two Azure roles on the **AI Foundry resource**, not implied by subscription Owner:
**Azure AI User** (models) and **Cognitive Services OpenAI User** (images). A 401/403 on
first run is almost always this. The WordPress account needs **Editor** or
**Administrator** — Author cannot create categories. `.env` is gitignored; `.env.example`
documents every variable.

**Local machine gotchas** (this dev box, 2026-07-28):

- `ruff` and `ppn` are **not on PATH**. Use `python3 -m ruff check src tests` and
  `python3 -m ppn_blogger.cli <cmd>` (the `ppn serve` command works via the module too).
- `npm install` fails with "cache folder contains root-owned files". Work around with
  `npm install --cache <writable-dir> …`, or fix permanently with
  `sudo chown -R 501:20 ~/.npm` (needs David's password).
- **Port collisions with another project** (`carwash-os`): its API and Vite grab 8000
  and 5173. During this session the PPN API ran on **8008** and the PPN Vite on **5280**
  (`PPN_API_TARGET=http://127.0.0.1:8008 npm run dev -- --port 5280 --strictPort`). A
  fresh session can use the defaults (8000/5173) if those ports are free.
- The `ppn serve` editable install resolves to the **main working tree**, so `ROOT`
  (and therefore `ui/dist`, the SQLite DB at `.ppn_state/ppn.db`, and `drafts/`) is the
  main tree, not a worktree. Point `PYTHONPATH` at a worktree's `src` to make `ROOT` that
  worktree if you need it to serve a worktree's built `ui/dist`.
- **The same applies to `pytest`, and there it is dangerous** — from inside a worktree,
  `python3 -m pytest` imports `ppn_blogger` from the *main* tree and happily reports
  everything green while testing none of your changes. There is no warning; the only
  symptom is that a test for code you just wrote passes before you write it. Always run
  `PYTHONPATH=$PWD/src python3 -m pytest -q` from a worktree, and check with
  `PYTHONPATH=$PWD/src python3 -c "import ppn_blogger; print(ppn_blogger.__file__)"` if
  a result looks too good.
- Vite's dev server binds **IPv6 only**: `http://127.0.0.1:5280` returns nothing,
  `http://localhost:5280` works.
