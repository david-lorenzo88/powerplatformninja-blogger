# CLAUDE.md

Project instructions for Claude Code. Read this first; it is the map.

## What this is

A crew of ten LLM agents that drafts blog posts for **powerplatformninja.com**,
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
| `docs/CONFIGURATION.md` | Every env var; every key in `config/`; all 33 validation rules. |
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
  agents.py         the ten agent factories
  tools.py          search, fetch, feeds, Learn, blog search, trust checks
  executors.py      gates: parsing, routing, loop conditions, artefact writing
  workflows.py      the two Agent Framework graphs + the entry-point functions
  covers.py         neon cover art (MAI / OpenAI-compatible / OpenAI-direct)
  wordpress.py      REST client + Markdown → Gutenberg block conversion
  sources.py        harvest sites from a wide sweep; file the operator's verdict
  storage.py        drafts, dossiers, review reports, package JSON
  testing.py        offline stub chat client
  cli.py            typer commands
  server/           FastAPI: run queue, SSE, versioned config store, drafts API,
                    source reviews (server/reviews.py)
config/             editorial policy — the thing you actually tune
tests/              67 tests, fully offline
ui/                 React management UI (Vite + React + TS) — Stage 2; see ui/README.md
```

The React UI in `ui/` is the frontend for `server/`, built and verified and now on
`main`. Its one invariant:
`ui/src/lib/deriveNodes.ts` is a faithful port of `derive_nodes()` in
`server/api.py` — the canvas and the transcript are both folded from the event log
the same way, so they can never disagree. Keep them in lockstep.

## Commands

```bash
pytest                      # 67 tests, ~25s, no network, no credentials
ruff check src tests        # must pass; line-length 110, E/F/I/UP/B
ppn doctor                  # config + live WordPress check
ppn preflight               # 2 cheap real model calls; detects temperature support
ppn suggest --dry-run       # whole graph offline
ppn suggest --explore --dry-run --yes   # wide sweep + source approval, offline
ppn write --index 1 --dry-run
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

**The source review is code, never judgement.** In exploration mode the candidate
list is harvested from the scouts' own reported URLs by `sources.py`, so what the
operator approves is a faithful record of where the scouts went — never a model's
account of it. `ScoutReplay` is the single place that filters to approved sites;
keep it that way, or "the editor only ever sees approved sources" stops being an
invariant and becomes a hope.

**Nothing downstream of research may destroy research.** `DossierGate` writes the
dossier to `research/` before sending it on, which is what makes
`ppn write --dossier` possible. Preserve that property.

**Failures that must never raise:** cover generation (`build_cover` funnels
everything into `CoverImage.error`), the WordPress push in `Finalizer`, and
translation parsing in `TranslationGate`. Losing a finished draft to a transient
outage is the failure mode being designed out.

**Tools never raise either.** Every `@tool` in `tools.py` returns
`{"error": ..., "message": ...}` so the agent can reason about the failure.

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
