# Status

Last updated: 2026-07-28. The React management UI (Stage 2) is **built, verified
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

**Screenshot markers.** The Writer drifts into `[SCREENSHOT: slug] caption`; the
converter normalises both forms into an empty `core/image` block plus a capture note.

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
