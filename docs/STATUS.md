# Status

Last updated: 2026-07-28. Written at the handoff from a Cowork session to Claude
Code.

---

## Verified against real Azure

One complete production run, on 2026-07-28 — *"Advanced connector policies:
migrate your DLP safely"*. This exercised the entire pipeline end to end:

| Stage | Result |
|---|---|
| Topic discovery | 6 suggestions, `topics/suggestions-2026-07-27.json` |
| Researcher | dossier written, `research/2026-07-28-advanced-connector-policies-acp-migrate-your-dlp-safely.json` |
| Source Checker | **passed** |
| Writer + validators | **approved**, score **93.5**, 3 revision rounds, 0 blockers |
| MAI cover | generated, uploaded as media **1244** |
| WordPress push | post **1245**, status `draft`, featured image set |

So the revision loop, the source loop, the MAI image route, media upload and the
Gutenberg push are all proven against live services — not just against the stub.

## Not yet exercised against real Azure

- **The Translator.** Wired, unit-tested against the stub, never run for real.
  `ppn translate drafts/2026-07-28-advanced-connector-policies-migrate-dlp-safely.md`
  is the cheapest way to find out — one model call.
- **The server** (`ppn serve`). All 31 tests pass, including real HTTP against the
  app, but no real Foundry-backed run has been driven through the API.
- **`ppn write --dossier` (resume).** Built after the failure that motivated it;
  the graph is unit-tested but has not been used to rescue a real run.
- **`ppn preflight`.** Written after the incident it prevents. Should be run once
  to confirm it agrees with the name-based inference for `gpt-5`.

---

## Open work

### 1. Push the repository to GitHub — blocked, needs David

`origin` is configured, both commits exist locally, but the remote repository has
not been created. `git push` returns `Repository not found`, and an unauthenticated
check confirms `david-lorenzo88/powerplatformninja-blogger` does not exist
publicly.

```bash
cd ~/Documents/repos/powerplatformninja-blogger
git remote remove origin
gh repo create powerplatformninja-blogger --public --source=. --push
```

Or create an **empty** repo at <https://github.com/new> — no README, no
`.gitignore`, no licence — then `git push -u origin main`.

### 2. Re-push the live post to fix its code blocks

Post 1245 on the blog still carries the broken `core/code` markup that prompted
the fix. The converter is corrected and covered by regression tests, but the
already-published draft has not been regenerated.

```bash
ppn wp preview drafts/2026-07-28-advanced-connector-policies-migrate-dlp-safely.md | head -40
ppn wp push    drafts/2026-07-28-advanced-connector-policies-migrate-dlp-safely.md
```

`push` updates in place by slug, so this corrects post 1245 rather than creating a
duplicate. Then open it in Gutenberg and confirm the block warnings are gone —
that is the only remaining unverified part of the fix.

The same draft contains five `[SCREENSHOT: …]` markers, which now become empty
`core/image` blocks — clickable upload slots. They are meant to be filled in by
hand. **Do not generate these images.** A fabricated Microsoft admin-centre
screenshot would be convincing and wrong on a post whose entire positioning is
reproducible steps; cover art is decoration, a screenshot is evidence.

### 3. The React management UI — the main remaining feature

Stage 1 (the service core) is built, tested and documented. Stage 2 is the
frontend, and it does not exist yet: there is no `ui/` directory.

The API contract is in [ARCHITECTURE.md](ARCHITECTURE.md). The server already
serves `ui/dist` as static files and falls back to `index.html` for client-side
routing, and `PPN_CORS_ORIGINS` defaults to the Vite dev server, so the backend
side of the integration is done.

Intended stack: **Vite + React + TypeScript**.

Four screens, in the order David described them:

1. **Runs** — list with status, kind, label, timing. Start a `suggest` or `write`
   run. Cancel a queued or running one. Queue depth should be visible: with
   `PPN_MAX_CONCURRENT_RUNS=2`, runs 3 and 4 sit in `queued` and that must be
   legible rather than looking stuck.
2. **Run detail** — the one with real requirements. A **canvas showing the agents
   as a graph**, from `GET /api/workflows` (mermaid + node list, built with stub
   clients so it needs no credentials). Nodes light up live as events arrive on
   `GET /api/runs/{id}/events`. **Clicking a node shows that agent's output and
   log lines in real time.** `derive_nodes()` in `server/api.py` is the reference
   implementation for folding events into node state — the client should fold the
   same way so the canvas and the log can never disagree.
3. **Config** — edit the five documents with version history and rollback. YAML is
   validated server-side; a 422 carries the parse error and must be surfaced
   inline rather than swallowed.
4. **Drafts** — list, read, edit the markdown, view the review report and the
   cover, publish to WordPress.

Notes for whoever builds it:

- **SSE reconnection is already solved server-side.** Reconnect with
  `?after=<last seen seq>` and you get exactly what you missed. Sequence numbers
  are strictly increasing per run; use them, do not de-duplicate by content.
- The stream sends a `: keep-alive` comment every 15 seconds of silence. Do not
  treat silence as a dropped connection before that.
- A synthetic `eof` event carrying the terminal status ends the stream. It is not
  persisted; it exists so the client knows to stop listening.
- Replay-then-follow means a browser arriving late at a finished run animates
  identically to one that watched it live. Build for that case first — it is the
  easiest to develop against.

### 4. Smaller things

- **Decide whether generated output belongs in a public repo.** `drafts/`,
  `research/` and `topics/` are currently untracked but not ignored, so a
  `git add -A` would publish unpublished drafts and full research dossiers.
  Either add them to `.gitignore` (keeping `.gitkeep`) or commit them
  deliberately. **This decision is unmade — ask David rather than guessing.**
- `ppn preflight` has not been run against the deployment.
- The `.gitkeep` files in `drafts/`, `research/` and `topics/` exist on David's
  machine but were not in the initial commit, which is why those directories show
  as untracked.

---

## Recent fixes worth knowing about

**Gutenberg code blocks.** Three serialisation mismatches against `core/code`
made every code block show *"This block contains unexpected or invalid content"*:
`html.escape` turning quotes into `&quot;`, a `class` attribute on `<code>`, and
an unescaped `[`. Fixed in `escape_code()`; regression tests assert the exact
serialisation. See `CLAUDE.md` → Gotchas.

**Screenshot markers.** The Writer drifts into `[SCREENSHOT: slug] caption`
despite being told to emit `![alt](IMAGE:slug)`. The converter now normalises both
into an empty `core/image` block plus a capture note. The Writer's prompt was also
tightened to forbid the drifting form.

**`temperature` on `gpt-5`.** Killed a production run six minutes in, after
research had succeeded. `supports_temperature` now infers from the model name and
`ppn preflight` verifies it. This is why `--dossier` resume exists.

**Intermittent test hang (~50%).** Five leaked aiosqlite worker threads blocking
teardown. Fixed with `NullPool`, a single event-writer task, and
`engine().dispose()` in the lifespan. Found with
`pytest -o faulthandler_timeout=40`, not by guessing.

---

## Environment notes

Two Azure roles on the **AI Foundry resource** are required and are not implied by
subscription Owner: **Azure AI User** (models) and **Cognitive Services OpenAI
User** (images). A 401/403 on first run is almost always this.

The WordPress account needs **Editor** or **Administrator** — Author cannot create
categories, and the pipeline creates them.

`.env` is gitignored and is not in the repository. `.env.example` documents every
variable.
