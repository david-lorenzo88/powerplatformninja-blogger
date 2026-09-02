# Status

Last updated: 2026-09-02. The React management UI (Stage 2) is **built, verified
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
- **UI**: a *Sources* tab on the start-run dialog, a **Sources**
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
  - The **only branch in the graph is whether there is anything to send**: with
    an empty window the run finishes `skipped` and **no model is called at all**.
    (There was a `min_items` floor here until the relay work below removed it.)
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
- **Schedules** are pure and previewable: `manual | interval | daily | weekly |
  monthly`, computed in the newsletter's own zone, with the next three fire times
  shown in the UI. Monthly is capped at day 28 — "the 31st" silently meaning "the
  28th" in February is a schedule that lies. `daily` is a wall-clock time rather
  than `interval` set to 1440: an interval is measured from the last generation,
  so a daily letter would land a few minutes later every day. Claimed by the
  scheduler with the same compare-and-swap as the system jobs.
- **Auto-send** (`auto_send`, off by default) queues the same `deliver` run the
  Send button queues, so an unattended issue fans out to every enabled recipient
  on every configured channel. The toggle on the newsletter screen names them all
  before it is flipped — turning it on is the one setting that lets the app reach
  an audience with nobody reading first.
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

## News phases 4 and 5 — delivery and feed discovery (new)

**Phase 4 — delivery.** Issues can reach people. Two of the three vendor channels
are shaped by platform facts, not by choice:

- **WhatsApp has no group API.** Meta's Cloud API messages individual numbers,
  and a newsletter is business-initiated outside the 24-hour window, so it can
  only be a **pre-approved template**, billed per conversation.
- **Telegram covers the group case** — a group or channel is a chat id, and the
  bot must be added first. Nothing here drives WhatsApp Web.
- **Email is ACS, not SMTP**: Container Apps blocks outbound port 25, and mail
  from its egress IPs without SPF/DKIM lands in spam whatever port it uses. The
  Bicep provisions ACS with a *managed* domain, which sends with no DNS.
- **Web push and "copy out by hand" need no configuration**, which is what makes
  the feature usable before any vendor decision.
- Rows are written `pending` before the first send; permanent failures are tried
  once and park the recipient; retry touches only what failed; an unconfigured
  channel is `skipped`, not `failed`.

**Phase 5 — feed discovery.** A sweep asks a model where to look, then
**fetches and parses every URL it names before the operator sees it**. Anything
that is not a real feed with entries is discarded, so approving cannot mean
adding a URL nobody checked. Refusals are remembered so a later sweep never
re-offers them. The approval mirrors `reviews.py`, including the crash-safe
ordering: create the feeds first, then close the review.

The nav badges now come from a single `GET /api/news/pending` rather than one
poll per badge — three polls would wake the serverless database three times as
often for no more information.

- **Tests**: 27 new (15 delivery, 12 discovery); suite is **234 passing**.

## Feed discovery, aimed by a brief (new)

Phase 5 shipped the whole mechanism — sweep, verify, review, approve, remember
the refusal — but the only way to start one was a **Find new** button that sent
no instruction. `sweep(instruction)` and `FeedDiscoveryReview.instruction`
existed and were never populated: dead wiring, which from the outside looks
exactly like working wiring.

- **The brief now exists in the UI.** *Find new* opens a dialog with a free-text
  box, four example briefs that fill it, and a plain statement of what a sweep
  costs. Empty is still valid and means a general sweep.
- **A brief governs the prompt.** `feed_scout_discovery_instructions(settings,
  brief)` puts it in the scout's *instructions* — a sweep is one long
  tool-calling loop, and an aim in a user turn is competing with everything nine
  searches returned since. Where the brief disagrees with the configured
  sections, the brief wins and the sections drop to context.
- **The scout is told how to research**, not just what to find: several distinct
  angles in the vocabulary each community uses, open a promising site rather than
  guessing its feed path, breadth over certainty — which is safe precisely
  because `_verify` fetches every URL before the operator sees it.
- **The review shows what was asked.** "You asked for: …" on the review screen
  and under each row in the list; the brief also names the run. The `instruction`
  column now holds the operator's words *only* — it used to fall back to the
  scout's own notes, which would have made "you asked for" a guess. (Notes are
  logged instead; giving them a column would need hand-run DDL, since
  `create_all` never alters.)
- **`ppn news discover "<brief>"`** — the CLI half, with per-feed approval in the
  terminal, mirroring `suggest --explore`.

**Two real bugs this turned up**, both invisible while the sweep was unreachable:

- **`discovery._ask` imported `ChatMessage` from `agent_framework`**, which does
  not exist in 1.12.1 (it is `Message`; `util.user_message` already wraps it). A
  real sweep would have died with an `ImportError` on its first call. Every
  existing discovery test drove `_verify` and `decide` directly, so the suite was
  green.
- **`StubChatClient` had no canned `FeedSuggestionSet`**, so a sweep could not be
  run offline at all — which is *why* the above survived. It now returns two
  suggestions, one of them a plausible address with nothing behind it, so every
  dry run exercises the discard rather than the happy path.

**Verified end to end offline**: `ppn news discover "…" --dry-run --yes` swept,
discarded the dead URL, filed the review, and created the surviving feed;
verification fetched a real feed (10 entries) as designed. Both dialogs and the
review screen checked in the browser at desktop and 375×812.

- **Tests**: 6 new (5 discovery, 1 API); suite is **240 passing**.
- **Not yet run against real Azure** — the sweep has still never called a live
  model, so how many usable feeds a real brief returns is unknown.

## Run cost accounting (new)

Every run now reports what it consumed and what that cost, per run and per agent,
with a spend view over time and prices kept in step with Azure.

- **One seam: `usage.UsageMeter`, an `AgentMiddleware`** attached by every factory
  in `agents.py`. Chat middleware was the obvious choice and is the wrong one —
  `StubChatClient` extends the raw `BaseChatClient`, which carries no
  `ChatMiddlewareLayer`, so a chat-level meter fires against Foundry and *never*
  offline. Proved with a probe before a line of the feature was written; the two
  paths are now pinned by `test_meter_records_on_the_*_path`.
- **Counted, not estimated**: tokens (including the cached and reasoning splits),
  cover images, and hosted web searches — the last of those turned out to be
  countable after all, since `web_search_call` items surface as `search_tool_call`
  contents with a `call_id` on both response paths.
- **Priced from `config/model_prices.yaml`**, a new versioned config document, so
  a rate change is an edit with history and a rollback. Unknown model → tokens
  reported, money withheld, never a confident zero.
- **`run_usage` table**, one row per agent invocation, written as the run proceeds
  so a cancelled or failed run still accounts for what it spent. A new table
  rather than columns on `runs`, because `create_all` never alters.
- **Prices track Azure.** `ppn cost prices --bind <model>` runs a targeted retail
  query (six rows for gpt-5, not the four hundred a broad one returns) and a human
  picks; refreshes then read those exact meter names back. A weekly scheduler job
  applies moves unattended — safe because it can only change a number, and because
  costs are stored at run time so history is never rewritten.
- **UI**: cost on the Runs list, a per-agent breakdown on run detail, a new
  **Spend** screen (day/kind, priciest runs), and **Update from Azure** on the
  prices document.

**Verified**: 292 tests passing, ruff clean; `ppn suggest --dry-run` reports a
real tally; the bind and refresh flows exercised against the **live** retail API
from both the CLI and the browser; the Runs list, breakdown and Spend screen
checked in the browser against seeded data.

**It broke `main` first, on the dialect seam.** The day rollup used
`func.date()` — SQLite's spelling, and not a function at all on SQL Server, so
CI went red immediately after the merge. Worth knowing *why* the usual guards
missed it: `func.date` compiles cleanly against the mssql dialect and fails only
on execution, so the compile-time checks in `test_sql_portability.py` had nothing
to catch. Nor is there a portable spelling to switch to — `CAST(x AS DATE)` is
accepted by SQLite and returns the **year**, silently bucketing a whole year
together. Fixed with `usage_store.day_bucket()`, a dialect branch, plus a source
grep for SQLite-only date functions so the next one fails in CI. Re-verified
against a real SQL Server 2022 container, not just SQLite.

**One bug caught in the browser that the tests had missed**: `by_agent` returned
no `priced` field, so every row in the breakdown rendered "—" beside a real
number while the run total was correct. `test_usage_is_broken_down_per_agent` now
asserts the per-agent costs sum to the total.

**Not yet run against real Azure** — no metered run has called a live model, so
the figures have never been cross-checked against an actual bill. That is the
next thing worth doing, and it needs nothing but one ordinary run.

> **Deploy step**: `config/` is seeded into the database only on first start, so
> the live server needs one `ppn config reload` (or `POST /api/config/reload`)
> before `model_prices` exists there. Until then runs report tokens and withhold
> the money.

## Draft focus: the diagnosis and the fix (new, 2026-08-13)

**The complaint:** drafts mix topics and wander off the subject they were
commissioned to write.

**The evidence.** Three real drafts pulled from WordPress (post ids `1258`, `1259`,
`1261`). Draft 1261, *"Copilot Studio orchestration: generative vs classic"*, has
11 content sections of 96 to 229 words, of which **four (705 words, 40% of the
body) are about Business Central** — not the subject, and not in the blog's own
category list. Its second opening paragraph is a five-item enumeration of what the
post "covers": a table of contents in prose, not a thesis. Drafts 1258 and 1259
were generated a day apart on the same subject with near-identical section plans.

**Why nothing caught it.** Six causes, all verified in code:

1. **No stage decided what the post was about.** `source_gate -> writer` directly.
   `ResearchDossier.suggested_outline` existed, was produced by the Researcher, and
   was read by nobody.
2. **The thesis was never carried.** `TopicSuggestion.angle` had nowhere to live
   downstream, and the revision prompt sent findings plus dossier and **no topic
   block at all**, so three rewrites happened with no statement of the argument.
   `catalog._topic_from_post` rebuilt topics with `angle=""`.
3. **The config specified thin sections.** 12 sections against a 0.8-scaled 2000
   word band is 133 words each.
4. **No rule asked whether the post was about one thing.** The content family had
   four rules, none of them coherence.
5. **Twelve rules were enforced by nobody** — `auto: true` with no detector meant
   the code skipped them and the validator was told to skip them. S02 (section
   count) and C04 (word count) were among them.
6. **The crew was blind to its own output.** `search_existing_posts` queried
   `status: "publish"`, and everything this crew produces is a *draft*. That is the
   mechanical reason 1258 and 1259 both exist.

**What changed.** A new **Outliner** agent and **`OutlineGate`** between the source
gate and the Writer, producing a `PostOutline` (thesis, reader promise,
`out_of_scope`, sections with dossier claim ids) that is checked in code, repaired
deterministically and persisted to `research/<date>-<slug>.outline.json`. The
thesis now rides the whole run, including every revision turn. Sections went 8-12
→ 5-7 with a 250-450 word floor per section. A new **F (focus)** rule family, three
of whose rules are code-decided against the outline. `COMPUTED_RULES` closes the
`auto` hole, with a test that fails the build if it reopens. The search tool now
sees drafts, minors reach the Writer, and the write launch dialog takes free-text
instructions that steer the *scope* via the Outliner.

Verified offline: 313 tests, ruff clean, `npm run build` and `tsc --noEmit` clean.
Verified against the real artefacts: S02, F03, F04 and F05 all fire on draft 1261,
and the search tool now returns the two duplicate drafts it used to miss.

**Not yet done** (deliberately held back until a live run shows the effect of the
above): scoring derived from `scoring.deductions` instead of the validator's
self-reported integer, cross-run duplicate detection wired to
`duplicate_similarity_threshold`, and deleting the dead `loop.*` config.

---

## Writing from your own sources (new)

A second way to start a post, for when the author already knows what it is and
which pages it rests on: `ppn write-brief`, and the **Custom** mode of the Write
dialog. The brief's links are the whole corpus — the crew reads them and nothing
else. Full reasoning in
[HOW-IT-WORKS](HOW-IT-WORKS.md#workflow-2c--writing-from-your-own-sources).

- **One cheap call turns the brief into a topic**, and code has the last word:
  `seed_sources` is overwritten with the links read out of the brief in Python, and
  the taxonomy is clamped to the configured ids. The interpreter is never asked for
  a URL and cannot contribute one.
- **The Researcher loses every route to the open web** — the tool list bypasses
  `_searchable()` rather than filtering it, so the hosted search tool is not
  attached either. A test asserts the whole tool set, and it is the guard the mode
  rests on.
- **`repair_corpus_citations()`** drops any citation from outside the corpus and any
  claim left unsupported, before the dossier is saved. Deterministic, no new loop,
  no new counter.
- **Three source-policy rules are suspended and only those three** — the trust
  average, the source count for a critical claim, and the official-source
  requirement. Reachability, excerpt accuracy and "no claim beyond its page" are
  unchanged and matter more.
- **Links are proved before the run is queued**: a 422 lists the dead ones,
  `allow_unreachable` overrides, and reachable links are stored as where they
  actually landed.

Verified offline: 332 tests, ruff clean, `npm run build` and oxlint clean, and a
full `ppn write-brief --dry-run` walks the whole pipeline.

**Not yet done:** never run against a real model or real pages. The first live run
is the interesting one — what a corpus-only dossier looks like when the pages are
thin is exactly what cannot be learned from the stub.

---

## Supervised delta learning (new)

Every post is finished by hand before it ships, and until now that difference was
thrown away — `server/drafts.py:write_draft` rewrites the draft body in place, so
the crew's original was destroyed by the first save. The loop now captures it,
scores it, and turns what recurs into reviewable configuration changes.
**Built and tested offline; never run against a real model.**

- **Two pure modules.** `delta.py` normalises both sides through one function,
  diffs H2 structure and blocks, and reports a **word-level edit rate** — the
  metric machine translation settled on decades ago. `config_edit.py` renders a
  typed proposal into a document edit under an allowlist. No LLM score appears
  anywhere: a rubric number would be the thing to optimise, and optimising it is
  how a loop like this games itself. A model classifies *what kind* of edit
  happened; arithmetic decides *how much*.
- **Capture rides two call sites that already existed.** `record_write_result`
  snapshots the pristine draft; `record_publish` records what was published.
  Posts published untouched are kept too — they are the positive class, and the
  golden set every proposal is tested against.
- **Five tables** (`server/db.py`): `delta_pairs`, `delta_observations`,
  `learning_candidates`, `learning_reviews`, `declined_learnings`. A refusal has
  its own table rather than a status, for the same reason `declined_feeds` does:
  the cluster keeps accruing evidence afterwards and a status would be overwritten.
- **The gate is the point.** A proposed rule runs against every draft the crew
  wrote *and* every version published. **A rule that fires on something the author
  published is a false positive by construction** and is discarded before a human
  sees it. Structure numbers get the same treatment through S02/C04/F03-F05.
  Detectors run in a **separate process under a wall-clock timeout** — Python's
  `re` has no timeout and a thread running a catastrophic pattern cannot be killed.
- **Nothing auto-applies.** `learning_reviews.decide` is the only path to
  `config_store.save_document`; a test parses `learning.py` to keep it that way.
  Honesty rules and `sources` are closed to the learner in the *type*, a learned
  rule is capped at `minor`, existing rules are immutable, and the ceiling on
  learned rules is configurable.
- **A new versioned document**, `config/agent_guidance.yaml`, so learned guidance
  for the Writer and Outliner is an ordinary config version with history and
  rollback — prompts stay Python, because the learner must never write Python.
  The validators deliberately get none: a learner that can coach the checker can
  teach it to accept the writer's mistakes.
- **UI**: a *Learning* screen under Blog with the metrics the spec asks for
  (share published unchanged, mean edit rate, edits by section), a per-post diff
  viewer, and a review screen that puts the gate's four counts **above** the diff.
- **CLI**: `ppn learn status | pairs | show <id> | run [--dry-run]`.
- **Tests**: 106 new; suite is **438 passing**, ruff clean, `tsc` and the UI build
  clean.

**Verified end to end offline and in the browser** (2026-09-02): four seeded posts,
three edited the same way and one published untouched; the sweep analysed 4 pairs,
found 2 clusters, proposed 2 and **1 survived** — the stub's deliberately bad
proposal (a detector matching ordinary prose) was discarded by the gate. Approving
in the browser wrote `validation_rules v2` with rule `V17`, allocated by code, and
**all 72 comment lines survived**.

**Three real bugs this turned up:**

- **`detectors._PROSE_SCOPED` is a hardcoded id set.** Any rule id allocated after
  that module was written — by hand or by the learner — was in neither list, so its
  detector ran against raw markdown and fired inside code fences, inline spans and
  URLs: exactly the T01/T02 false positives the masking layer exists to prevent.
  Rules can now declare `prose_only`; the shipped 61 are unaffected.
- **`config/style_guide.md` §8 holds `## Contents` and `## Sources` inside a fenced
  skeleton** the Writer copies literally. Matching an anchor without masking fences
  would have spliced editorial policy into that template, and into every post.
- **`sources.merge_into_yaml_text` does not generalise** to a mapping in a sequence,
  which is what a validation rule is, and its `safe_dump` fallback would have
  stripped 26 KB of explanation from the ruleset. The path-generic helpers were
  promoted to `config_edit.py`; the fallback is a refusal there.

**Not yet done:** the model replay (tier 2 validation) is designed and deliberately
unbuilt — see the plan. `gate_status` already carries `skipped` so it lands without
a schema change. It belongs in a **separate process**, as a CLI command, because
`config_store._source` is a module singleton that `save_document` mutates in place.

**The honest caveat:** the loop's clock is the publishing cadence. At three distinct
posts per pattern, roughly ten published posts are needed before the first proposal
exists. The capture half costs nothing and should run long before the rest is
switched on — `PPN_LEARN_ENABLED` is off by default.

## `Run.config_version` was truncated, and is now decodable (new)

The column is `String(64)` and held `"|".join(f"{name}:{version}")` sliced to fit.
That token is **112 characters** with the documents this project ships, so the
slice threw away four of them — including `validation_rules`, the one thing the
column is really worth asking about — and the cut point moved as version numbers
gained digits, so the value was not even stable in shape.

`config_source.config_stamp()` now encodes it as
`cfg1:<digest of the document names>:<version per document>` — 30 characters today,
45 at three-digit versions, and decodable with `read_config_stamp()`. The digest is
load-bearing: a stamp written before a document was added reads as **unknown**
rather than being silently misaligned by one position into a confident wrong
answer, and rows written before the encoding read as unknown too. The column stays
`String(64)`, so no hand-run DDL against Azure SQL. Run detail now shows which
version of each document a run read.

---

## Every article straight to Telegram (new)

Two changes, one for each half of "I want every article in my Telegram, and no
minimum before it sends".

**The newsletter minimum is gone.** `IssueBuilder`'s branch was
`len(candidates) < min_items`; it is now `not candidates`. A floor is a second,
silent way for an issue never to happen and it fails in the direction that costs
the operator news — one article that landed today is worth sending today, and
holding it for company only makes it stale. The `min_items` column stays mapped
and unread: `create_all` never ALTERs, so removing it would leave a NOT NULL
column with no server default on every existing database and break every INSERT
there while passing on a fresh one. It is off the API, the CLI line and the UI.

**A watched feed can now be relayed to Telegram**, one message per article, the
moment the poll finds it — the un-composed counterpart to a newsletter: no model
call, no issue stored, headline and link exactly as the feed gave them.

- **It rides on the watch set**, not on a column of its own. `notified_at` is
  already stamped before anything is announced, so the same stamp covers both
  the browser push and the relay, and "watched" keeps its single meaning. Which
  feeds are relayed is which feeds are `realtime`. A new column would not have
  reached an existing database anyway.
- **The relay chats are named in the environment** (`PPN_TELEGRAM_RELAY_CHAT_ID`),
  never taken from the recipient list. Recipients receive composed issues;
  somebody who subscribed to a weekly letter has not asked for forty raw
  headlines a day. Empty means off.
- **Plain text, never Markdown.** Telegram answers a 400 — which this code maps
  to *permanent* — when `*`, `_` or `[` do not balance under a parse mode, and a
  headline is arbitrary text from somebody else's feed. `channels.send_telegram`
  now takes `markdown=`, defaulting to off; only `render_short`, which is ours,
  passes `True`.
- **A flood becomes a digest, not a drop.** Telegram throttles a group at about
  twenty messages a minute and a feed's first poll can carry a hundred articles,
  so past `PPN_TELEGRAM_RELAY_MAX_PER_TICK` the tail travels as one message that
  says how many it is carrying.
- **It cannot raise and cannot block the push**, which runs after it: the
  articles are stamped either way, so a failure is a logged line rather than a
  duplicate the next tick.
- Quiet hours still apply to both announcements — during the window nothing is
  stamped, so the backlog rolls up at 07:00 rather than being lost. Set
  `PPN_REALTIME_QUIET_HOURS=` to switch that off.

**Watching is now settable per group.** A group screen with no way to say "watch
everything in here" meant flagging forty feeds one at a time. It is a **bulk
write over the group's feeds**, not a flag on the group, and the reasoning is
the same one that kept the relay off a column of its own: `Feed.realtime` is
what the poller, `watch.pending_articles` and the cadence cost calculation all
read, and a group-level column would have to be ORed into every one of them —
and would never reach the live database, because `create_all` never ALTERs.
`feeds_realtime` on the group is therefore derived: all, some or none. The cost
is that it does not persist as an intention — a feed added tomorrow is not
watched until the button is pressed again — and that is the safer direction,
because a feed silently joining the fifteen-minute cadence is a bill nobody
chose. The UI says so, and uses buttons rather than a checkbox for exactly that
reason.

Switching a feed to watched now also pulls `next_poll_at` forward, on both
paths. Without it "watch this closely" changed nothing until the six-hourly
sweep next came round, which can be five hours of silence from a feed the
operator just asked to hear from quickly.

- **Tests**: 8 new (4 relay, 1 for a one-article issue, 2 for group watching,
  1 for the poll-forward); the quiet-week test now asserts the empty-window
  skip, and the group HTTP round-trip covers the new endpoint.

---

## Still open / not yet exercised

- **A corpus run against real pages** — `write-brief` and the Custom mode are
  exercised offline only.
- **`write` run through the server/UI** — only the CLI has done a real write. Drive one
  from the Runs screen to prove the writer/validator streaming and the Drafts publish.
- **The Translator** — wired, unit-tested against the stub, never run for real.
- **`ppn write --dossier` (resume)** — unit-tested, never used to rescue a real run.
- **`ppn preflight`** — never run against the deployment.
- **The Telegram relay against the real API** — covered offline only; the send
  path is shared with the delivery channel, but no live chat has received one.
- **Cost figures against a real bill** — the accounting has never metered a live
  model call, so it has not been reconciled with Azure Cost Management.
- **The Outliner against a real model** — the whole outline stage is exercised
  offline only. The natural regression case is re-running draft 1261's topic: we
  know exactly what it did wrong, so the before/after is unambiguous.

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
