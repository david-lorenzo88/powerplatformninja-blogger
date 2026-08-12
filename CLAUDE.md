# CLAUDE.md

Project instructions for Claude Code. Read this first; it is the map.

## What this is

A crew of eleven LLM agents that drafts blog posts for **powerplatformninja.com**,
orchestrated with **Microsoft Agent Framework** (`agent_framework` 1.12+) on
**Azure AI Foundry**, publishing into WordPress as unpublished drafts.

Nothing auto-publishes. The crew's job ends at a WordPress draft plus a review
report; David presses Publish.

**Current status, open work and known issues: [docs/STATUS.md](docs/STATUS.md).**
Read it before starting anything.

## Documentation

Written and verified against the code. Do not restate these in chat — link them.

| File | Contents |
|---|---|
| `docs/HOW-IT-WORKS.md` | Every stage, gate and loop, with the reasoning behind each design decision. The deep one. |
| `docs/GETTING-STARTED.md` | Fresh-environment setup: Azure resources, roles, WordPress, first run. |
| `docs/CONFIGURATION.md` | Every env var; every key in `config/`; all 61 validation rules. |
| `docs/OPERATIONS.md` | CLI reference and troubleshooting for failures that actually occurred. |
| `docs/DEPLOYMENT.md` | Deploy to Azure Container Apps (Entra Easy Auth, Postgres, Azure Files, Bicep) — the online-hosting runbook. |
| `docs/ARCHITECTURE.md` | Server API contract — the input for the React UI. |
| `docs/STATUS.md` | What is done, what is verified, what is left. |

## Layout

```
src/ppn_blogger/
  settings.py       .env + config/ loading, all dataclasses, get_settings() singleton
  config_source.py  swappable config backend: YAML files (CLI) or DB (server)
  models.py         Pydantic contracts moved between agents
  clients.py        Foundry chat clients (reasoning + fast tiers)
  prompts.py        agent instructions, built from config at call time
  agents.py         the agent factories
  tools.py          search, fetch, feeds, Learn, blog search, trust checks
  executors.py      gates: parsing, routing, loop conditions, artefact writing
  workflows.py      the Agent Framework graphs + the entry-point functions
  covers.py         neon cover art (MAI / OpenAI-compatible / OpenAI-direct)
  usage.py          token metering (agent middleware) + pure pricing arithmetic
  wordpress.py      REST client + Markdown → Gutenberg block conversion
  sources.py        harvest sites from a wide sweep; file the operator's verdict
  news.py           news feeds, pure: canonicalise, conditional GET, parse, discover
  newsletter_render.py  one composed issue -> markdown, email HTML, plain text
  storage.py        drafts, dossiers, review reports, package JSON
  testing.py        offline stub chat client
  cli.py            typer commands
  server/           FastAPI: run queue, SSE, versioned config store, drafts API,
                    source reviews (server/reviews.py), news feeds
                    (server/api_news.py, news_store.py, ingest.py), the
                    scheduler (scheduler.py), watch notifications (watch.py) and
                    newsletters (newsletters.py, newsletter_runs.py), delivery
                    (channels.py, delivery.py), feed discovery (discovery.py)
                    and cost (usage_store.py, prices.py)
config/             editorial policy — the thing you actually tune
tests/              313 tests, offline; conftest.py picks the DB backend
ui/                 React management UI (Vite + React + TS) — Stage 2; see ui/README.md
```

The React UI in `ui/` is the frontend for `server/`, built and verified and now on
`main`. Its one invariant:
`ui/src/lib/deriveNodes.ts` is a faithful port of `derive_nodes()` in
`server/api.py` — the canvas and the transcript are both folded from the event log
the same way, so they can never disagree. Keep them in lockstep.

## Commands

```bash
pytest                      # 313 tests, ~45s, no network, no credentials
                            # (SQLite locally; CI runs the same suite on SQL Server)
ruff check src tests        # must pass; line-length 110, E/F/I/UP/B
ppn doctor                  # config + live WordPress check
ppn preflight               # 2 cheap real model calls; detects temperature support
ppn suggest --dry-run       # whole graph offline
ppn suggest --explore --dry-run --yes   # wide sweep + source approval, offline
ppn write --index 1 --dry-run
ppn news validate <url>     # is there a feed there? (no model, no writes)
ppn news add <url>          # register a feed, after confirming it is one
ppn news discover "<brief>" # search the web for feeds matching a brief, then approve
ppn news poll               # fetch every enabled feed; run twice to see the 304s
ppn news list | ppn news read
ppn newsletter list|preview|generate   # preview calls no model
ppn cost                    # what the server-side runs have spent
ppn cost prices --bind gpt-5   # find the retail meters that price a model
ppn cost prices --refresh      # compare bound prices against Azure
```

`pip install -e ".[dev]"` gives you the CLI, the server extras and pytest.

For the UI (Stage 2), from `ui/`:

```bash
npm install && npm run dev   # SPA on :5173, proxies /api to `ppn serve` on :8000
npm run build                # → ui/dist, served by `ppn serve` in production
npm run lint                 # oxlint
```

On this dev box `ruff` and `ppn` are not on PATH — use `python3 -m ruff` and
`python3 -m ppn_blogger.cli`. `npm install` needs `--cache <writable-dir>` (a
root-owned `~/.npm`); full environment notes are in `docs/STATUS.md`.

## How to work on this

**Config before code.** Behaviour lives in `config/*.yaml` and
`config/style_guide.md`. "Be stricter about licensing claims" is a line in
`validation_rules.yaml`, not a code change. Reach for Python only when the
config layer genuinely cannot express it.

**No routing decision is made by a model.** Agents produce judgements; the gates
in `executors.py` act on them. Every loop condition is plain Python you can read
and test. Keep it that way — do not move control flow into a prompt.

**Every agent is bound to a Pydantic `response_format`.** The graph moves typed
objects, not prose. If you add an agent, add its model to `models.py` first.

**Both loops must stay bounded.** `source_round` and `revision_round` are the only
counters in the system, each with exactly one bound
(`PPN_MAX_SOURCE_ROUNDS`, `PPN_MAX_REVISION_ROUNDS`). Exhausting a budget
finalises the run anyway — producing nothing after 40 minutes is worse than
producing a draft marked NOT APPROVED.

The outline gate deliberately adds **no third counter**. Every check it makes has
one obviously correct deterministic repair (drop an invented claim id, truncate to
the section cap, derive `out_of_scope` from the claims the outline left unused), and
a loop is for work a model must *redo*. The one irreparable case, too few sections,
passes through with a warning and is caught by S02 and F04 in the revision loop,
which is already bounded. A test asserts `RunState` grows no further `_round` field.

**The post's thesis is decided once, in code-checked form, before a word is
written.** The Outliner returns claim *ids* and `OutlineGate` resolves them against
the real dossier, so a section cannot rest on research that does not exist. The
thesis is then restated in every writer turn including revisions — without that,
three rewrites happen steered only by style findings, with no statement anywhere of
what the post is meant to argue. `out_of_scope` is the load-bearing field: naming
what a post excludes is the decision, and it is what the focus rules check a
wandering heading against.

**A discovered feed is proved before it is offered.** A discovery sweep asks a
model where to look; `discovery._verify` then fetches and parses **every** URL it
named, and anything that does not resolve to a real feed with entries is
discarded before the review row is written. Approving therefore cannot mean
adding a URL nobody checked — which matters more than the source review did,
because an approved feed goes into a poller that calls it forever. A refusal is
remembered in `declined_feeds` so a later sweep never re-offers it.

**The sweep's brief is the operator's, and it governs.** `sweep(instruction)`
puts the brief in the scout's *instructions*, not in a user turn — a sweep is one
long tool-calling loop, and an aim stated in the system prompt still holds at
search number nine. Where the brief disagrees with the configured newsletter
sections, the brief wins and the sections drop to context; without one the
sections are the aim. `FeedDiscoveryReview.instruction` stores the brief
**verbatim and only the brief**, because the review screen shows it back as "you
asked for this" and a verdict is only meaningful against the actual question.

**Delivery must never raise, and never destroy an issue.** Every `Channel.send`
returns a `DeliveryResult` rather than throwing; every `deliveries` row is
written `pending` before the first send; a *permanent* failure (bad address,
unapproved template) is tried once and parks the recipient rather than being
retried. Same doctrine as `build_cover` and the WordPress push.

**A newsletter's links are code, never judgement.** The editor is handed a
numbered candidate list and returns *ids*; it is never given a URL and cannot
produce one. `IssuePublisher` resolves each id back to the article it came from
and drops anything that was not offered, along with any section outside the
configured taxonomy. An email cannot be un-sent, which is why this is stricter
than the blog side. The offline stub deliberately returns one fabricated id and
one invented section, so every dry run exercises the gate.

**The source review is code, never judgement.** In exploration mode the candidate
list is harvested from the scouts' own reported URLs by `sources.py`, so what the
operator approves is a faithful record of where the scouts went — never a model's
account of it. `ScoutReplay` is the single place that filters to approved sites;
keep it that way, or "the editor only ever sees approved sources" stops being an
invariant and becomes a hope.

**Nothing downstream of research may destroy research.** `DossierGate` writes the
dossier to `research/` before sending it on, which is what makes
`ppn write --dossier` possible. Preserve that property.

**The cost meter is agent middleware, and that is not an accident.** Agent
Framework offers chat middleware too, which looks like the better seam — it sees
the model name and fires per round trip. It is the wrong one: `StubChatClient`
extends `BaseChatClient`, the *raw* base, and `ChatMiddlewareLayer` is a mixin
only concrete clients carry. A meter written there works against Foundry and
fires **never** against the stub — invisible to `pytest` and to every dry run.
`AgentMiddleware` is installed by the `Agent` itself, so it fires either way; the
price is that `AgentResponse` carries no model name, which is why `_meter()` in
`agents.py` is told its tier. Counting once per invocation loses nothing, because
`FunctionInvocationLayer` already aggregates the whole tool loop.

**A price is bound by a human once, then refreshed verbatim.** Azure's retail feed
carries every token meter and no usable name — 400+ GPT rows in one region, called
things like `5.4 Batch cd inp Dz 1M Tokens`, and gpt-5 is `5 pp` while gpt-5.4 is
plain `5.4`. Matching that automatically is guesswork, and a wrong guess prices you
against the wrong meter, which reads exactly like a right answer. So
`prices.candidates()` offers a shortlist, a person picks, and the meter *names* go
in the config; `prices.refresh()` then reads those names back. An unattended weekly
refresh can move a number and can never repoint a binding. Cover images and hosted
search have no matchable meter and stay hand-set — `prices.apply()` must keep
leaving them alone.

**Counting is exact; costing is an estimate.** Everything above `usage.price` is
what the service reported. Below it, a number from config is applied, so every
figure the UI shows says "list price" and `priced: false` means the money is
*unknown*, never zero. A run that called no model reports `usage: null` rather than
a confident 0.00. Money is integer micros end to end — never a float across the
SQLite/SQL Server seam.

**Failures that must never raise:** cover generation (`build_cover` funnels
everything into `CoverImage.error`), the WordPress push in `Finalizer`, and
translation parsing in `TranslationGate`. Losing a finished draft to a transient
outage is the failure mode being designed out.

**Tools never raise either.** Every `@tool` in `tools.py` returns
`{"error": ..., "message": ...}` so the agent can reason about the failure.

Metering is on that list: `UsageMeter` swallows and logs, `Ledger.add` swallows a
failing sink, and `prices` returns `[]` rather than raising. Losing a forty-minute
run to a bookkeeping bug is the failure being designed out.

## Testing

`stub_clients()` returns schema-valid canned objects with no network. With
`exercise_loops=True` (the default) it **fails the first source check and the
first validation round**, so a dry run walks both loops instead of gliding down
the happy path. Keep that property when you touch the stub — a dry run that only
tests the happy path is not worth running.

`tests/test_server.py` runs real HTTP against the app with a real queue and real
SSE. Queue and cancellation tests use the `controllable_dispatch` fixture, which
holds a job open on an `asyncio.Event`; stub runs finish in milliseconds, so
timing-based assertions there are races.

## Gotchas that have already cost real runs

**`temperature` on reasoning models.** `gpt-5`, `o1`, `o3`, `o4` reject it with an
HTTP 400. Only the Writer, Validators and Translator send one — so the failure
lands *after* research succeeded. `_opts()` drops it when
`settings.foundry.supports_temperature` is false; `ppn preflight` verifies against
the live service. Never add a `temperature` without going through `_opts()`.

**Gutenberg block validation.** Gutenberg re-runs a block's `save()` and diffs the
result against the stored markup; any difference shows in the editor as *"This
block contains unexpected or invalid content"*. `escape_code()` therefore escapes
`&`, `<`, `>` and `[` and **deliberately leaves quotes alone** — `html.escape`
would produce `&quot;` and invalidate every line of a JSON snippet. `<code>` must
carry no attributes. Verify any converter change with `ppn wp preview`.

**MAI is not OpenAI-compatible.** Route `/mai/v1/images/generations`, integer
`width`/`height` rather than a `size` string, no `quality`, no `n`, and a hard cap
of 1,048,576 pixels with a 768px minimum edge. `fit_to_mai_limits()` handles the
cap.

**Introspect the installed package rather than trusting documentation.** Both of
the hardest integrations here — Agent Framework's real executor API and MAI's
route — were solved by reading installed code, not docs about it.

**SQLite is the forgiving dialect, and it has cost two production incidents.**
`.is_(True)` compiles to `IS 1`, which SQL Server rejects outright; and a
`DateTime(timezone=True)` column reads back *naive* on SQLite and aware on Azure
SQL, so comparing one to `utcnow()` raises only in production. Both were green
through the entire suite. Three things guard this seam now, and all three matter:

- **`PPN_DATABASE_URL` has no default.** An unconfigured environment raises
  rather than quietly picking the dialect that hides bugs. Put
  `PPN_DATABASE_URL=sqlite+aiosqlite:///.ppn_state/ppn.db` in `.env` for local work.
- **CI runs the whole suite against real SQL Server** (a service container in
  `deploy.yml`). Locally `pytest` still uses a temp SQLite file and needs no
  services — `tests/conftest.py` picks the backend from `PPN_TEST_DATABASE_URL`,
  so pointing the suite at a real server is one environment variable.
- **`tests/test_sql_portability.py`** compiles statements against the SQL Server
  dialect and greps the source for the pattern, so a `.is_(True)` written next
  month fails in CI rather than in Azure.

Use `== true()` / `== false()` for boolean predicates, and `db.as_utc()` before
comparing any stored timestamp in Python.

**Date truncation has no portable spelling, and both wrong answers differ.** This
one reached `main`: `func.date(x)` is SQLite's, and SQL Server has no such
function — *'date' is not a recognized built-in function name*, statement dead.
The obvious fix is worse. `CAST(x AS DATE)` does not fail on SQLite: "DATE"
carries no affinity keyword, so it gets NUMERIC affinity and returns the **year
as an integer**, collapsing every day of a year into one bucket with nothing
raised anywhere. Use `usage_store.day_bucket()`, which branches on the dialect.
Note that a compile check cannot catch this — `func.date` renders happily
against the mssql dialect and only fails on execution — which is why the guard
is a source grep for `func.date|time|datetime|julianday|strftime`, with an
explicit `# sqlite-only:` marker for a deliberate use.

**A polling cadence is a bill.** Azure SQL is serverless with
`autoPauseDelay: 60`, so any server-side loop touching the database more often
than hourly stops it ever pausing — on the order of $150-200/month at list price
instead of near-zero. This is why `PPN_INGEST_INTERVAL_MINUTES` defaults to 360,
why the realtime cadence only exists once a feed opts in, and why
**`/api/health` must never grow a database count**: it is on the container's
readiness probe every 15 seconds and currently touches no database at all.

**The scheduler sleeps until due; it does not tick.** `server/scheduler.py` is
the only periodic work in the codebase. A fixed interval would query the database
1,440 times a day and end auto-pause on its own — the sleep-to-horizon design is
what keeps the cost a consequence of the operator's cadence choices rather than
of the scheduler existing. An `asyncio.Event` (`scheduler().wake()`) makes an
edit take effect immediately, which is what makes a long sleep safe.

**One replica is not one process.** `minReplicas: 1` looks like a guarantee and
is not: Container Apps starts the new revision before draining the old, so every
deploy briefly runs two schedulers. Ticks are claimed with a compare-and-swap on
`next_due_at` — no `SELECT FOR UPDATE`, no dialect-specific locking — and exactly
one wins. Do not replace that with a plain read-then-write.

**New run kinds must set their own timeout.** `RunManager._dispatch` wraps
nothing; `suggest_timeout_minutes`/`write_timeout_minutes` are read only by
`cli.py`. A server-side run without its own `asyncio.wait_for` can hold a worker
forever.

**The app sets `redirect_slashes=False`; leave it off.** `ui/src/api/client.ts`
fetches with `redirect: 'manual'` and reads any redirect as Easy Auth bouncing an
expired session, so a 307 does not surface as an error — it signs the operator
out. Declaring routes without a trailing slash does not prevent this; it *causes*
it, because Starlette redirects `/api/runs/` to `/api/runs` precisely because the
latter exists.

This was masked in production: the SPA catch-all matches `/{full_path:path}` and
404s anything under `api/`, so the redirect only ever fired where `ui/dist` does
not exist — which is `ppn serve` in dev, and CI. If a test involving route
resolution passes locally and fails in CI, a built `ui/dist` is the first thing
to suspect.

## Git

The repository exists and is **public** at
`https://github.com/david-lorenzo88/powerplatformninja-blogger`. `main` carries the
crew, the docs and the ignore rules:

```
ef1da27  docs: add project map, status doc, and README status link
7f68e1e  docs: full reference — how it works, setup, configuration, operations
c18e7fb  feat: agent crew that drafts blog posts for powerplatformninja.com
```

The React UI (Stage 2) — `ui/` plus a run-event enrichment in `server/runs.py` —
was merged into `main` via
[PR #1](https://github.com/david-lorenzo88/powerplatformninja-blogger/pull/1).

Generated output — `drafts/`, `research/`, `topics/` — is **gitignored** (each keeps a
tracked `.gitkeep`); never `git add` a draft or dossier into the public repo.

Do not commit `.env`. Do not commit anything containing the WordPress Application
Password or an Azure endpoint with credentials.

## Style

Match the surrounding code. Notable existing conventions:

- Module docstrings explain *why* the module is shaped the way it is, not what it
  does. Keep that.
- Comments earn their place by recording a decision or a trap, not by narrating
  the next line.
- `from __future__ import annotations` at the top of every module.
- Dataclasses use `slots=True`.
- Prose in docs and comments is British English; the blog itself is written in
  English by the crew and translated to Spanish on request.
