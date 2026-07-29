# powerplatformninja-blogger

A crew of agents that drafts blog posts for **powerplatformninja.com**, orchestrated with
**Microsoft Agent Framework** (`agent_framework` 1.12+) on **Azure AI Foundry**, and
publishing straight into WordPress as unpublished drafts.

Nothing here auto-publishes. The crew's job ends at a WordPress draft plus a review report;
you press Publish.

## Documentation

| Document | Read it when |
|---|---|
| **[Getting started](docs/GETTING-STARTED.md)** | Setting this up on a fresh machine. Azure resources, model deployments, WordPress credentials, first run. |
| **[How it works](docs/HOW-IT-WORKS.md)** | Before changing anything. Every stage, every gate, every loop, and why each one is built the way it is. |
| **[Configuration](docs/CONFIGURATION.md)** | Tuning the crew. Every environment variable and every key in `config/`. |
| **[Operations](docs/OPERATIONS.md)** | Daily use and when something breaks. Full CLI reference plus the failures that have actually happened here. |
| **[Architecture](docs/ARCHITECTURE.md)** | Building against the server API. |
| **[Status](docs/STATUS.md)** | What is done, what is verified against real Azure, what is left. |

The short version is below.

---

## The crew

| Agent | Model role | Tools | Produces |
|---|---|---|---|
| **News Scout** | fast | web search, fetch, blog search | `ScoutReport` |
| **Feed Scout** | fast | curated RSS, fetch, blog search | `ScoutReport` |
| **Docs Scout** | fast | Microsoft Learn search, fetch | `ScoutReport` |
| **Topic Editor** | reasoning | blog search | `TopicSuggestionSet` |
| **Notes Normalizer** | fast | — | `AuthorClaimSet` |
| **Researcher** | reasoning | Learn, web search, fetch, feeds, trust check | `ResearchDossier` |
| **Source Checker** | reasoning | URL reachability, fetch, trust check, Learn | `SourceVerdict` |
| **Writer** | reasoning | blog search | `Draft` |
| **Content Validator** | reasoning | — | `ValidationReport` |
| **Design Validator** | reasoning | — | `ValidationReport` |
| **Translator** | reasoning | — | `Draft` (localised) |

Plus a non-agent stage: neon cover art (see below).

Every agent is bound to a Pydantic `response_format`, so the workflow moves typed objects
between nodes rather than prose it has to re-parse.

## The two workflows

**Topic discovery** — three scouts run concurrently, a fan-in aggregator merges their
signals, the Topic Editor de-duplicates against posts already on the blog and returns a
ranked shortlist.

```
scout_dispatcher ─┬─▶ news_scout ─┐
                  ├─▶ feed_scout ─┼─▶ scout_aggregator ─▶ topic_editor ─▶ topic_publisher
                  └─▶ docs_scout ─┘
```

**Post pipeline** — research is source-checked *before* a word is written, and the draft
loops through both validators until it clears the bar or the revision budget runs out.

```
brief_builder ─▶ researcher ─▶ dossier_gate ─▶ source_checker ─▶ source_gate
                     ▲                                               │
                     └────────── source loop (max 2) ────────────────┤
                                                                     ▼
                                            writer ─▶ draft_gate ─┬─▶ content_validator ─┐
                                              ▲                   └─▶ design_validator  ─┤
                                              │                                          ▼
                                              └──── revision loop (max 3) ──── review_gate ─▶ finalizer
                                                                                                   │
                                                            (only when approved)  translator ◀─────┘
                                                                    │
                                                            translation_gate ─▶ Spanish draft
```

The loops are enforced in code (`executors.py`), not in a prompt — an agent cannot talk its
way past the source check or the blocker count.

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then fill it in
```

### Azure AI Foundry

```env
FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
FOUNDRY_MODEL=gpt-5
FOUNDRY_MODEL_FAST=gpt-5-mini     # optional, used by the scouts
AZURE_CREDENTIAL_MODE=cli         # `az login`, or "default" for managed identity
```

```bash
az login
```

### WordPress

Create an Application Password: **WP Admin → Users → Profile → Application Passwords**.
Give it a name like `ppn-blogger`, copy the generated password.

```env
WP_URL=https://powerplatformninja.com
WP_USERNAME=your-wp-username
WP_APP_PASSWORD=xxxx xxxx xxxx xxxx xxxx xxxx
WP_DEFAULT_STATUS=draft
WP_AUTO_PUSH=true
```

Verify it:

```bash
ppn wp check
```

The account needs the **Editor** or **Administrator** role to create posts and terms via the
REST API. If your host blocks `Authorization` headers (some Apache setups do), add this to
`.htaccess`:

```apache
RewriteCond %{HTTP:Authorization} ^(.*)
RewriteRule ^(.*) - [E=HTTP_AUTHORIZATION:%1]
```

### Web search

Default is Foundry's own web search tool — **no API key and no extra Azure resource**.
Microsoft manages the Bing resource behind it and the search executes inside the service,
so results never round-trip through this process.

```env
SEARCH_PROVIDER=foundry
SEARCH_CONTEXT_SIZE=medium    # low | medium | high
SEARCH_USER_COUNTRY=ES
```

It is GA and works with Azure OpenAI model deployments. Note that search data leaves the
Azure compliance boundary — the same is true of every option here.

Alternatives, if you ever want them:

| `SEARCH_PROVIDER` | Needs | When |
|---|---|---|
| `foundry` | nothing | Default. Zero setup, billed with your Foundry usage. |
| `tavily` | `TAVILY_API_KEY` | You want search results in your own logs, or a non-Bing index. |
| `brave` | `BRAVE_API_KEY` | Same, different index. |
| `none` | — | Feeds + Microsoft Learn only. Degraded but functional. |

Switching the value swaps the tool the agents get — `foundry` attaches the hosted tool,
`tavily`/`brave` attach the local `web_search` function, `none` attaches neither. Nothing
else changes.

If you later need `freshness`, `count` or `market` parameters, or want to restrict grounding
to a curated domain list, Foundry also exposes `get_bing_grounding_tool` and
`get_bing_custom_search_tool` (both preview, both require you to create and connect a
Grounding with Bing Search resource yourself). Swap them in inside
`clients.hosted_web_search_tools`.

---

## Usage

```bash
ppn serve                       # management UI + API on http://127.0.0.1:8000
ppn doctor                      # config + connectivity check
ppn preflight                   # one cheap real call per request shape
ppn suggest                     # scan news/feeds/docs → topics/suggestions-<date>.md
ppn write --index 1             # full pipeline for suggestion #1
ppn write --index 1 -d research/2026-07-28-x.json   # reuse saved research
ppn write --index 1 --notes input/notes/my-slug.md  # field-report mode from your notes
ppn write --index 1 --translate # ...and produce the Spanish version too
ppn run                         # suggest, then write the top-ranked topic
ppn rules                       # print the rulebook the validators enforce
ppn config reload               # re-import config/ into a running server's database

ppn write-topic --title "Elastic tables: what you give up" \
                --area dataverse --format deep-dive \
                --source https://learn.microsoft.com/... \
                --question "Which column types are unsupported?"

ppn models                      # list image models deployed on your resource
ppn cover drafts/x.md           # regenerate the cover image for a draft
ppn translate drafts/x.md       # translate an approved draft to Spanish (opt-in)
ppn wp check                    # verify WordPress credentials
ppn wp preview drafts/x.md      # show the Gutenberg markup without publishing
ppn wp push drafts/x.md         # push an edited local draft
```

Add `--dry-run` to any pipeline command to run the **entire graph offline** against a stub
client — no Azure, no API keys, no network. Useful for checking config changes:

```bash
ppn suggest --dry-run
ppn write --index 1 --dry-run --no-push
```

### House style

Posts are written in **English** and follow one fixed shape, modelled on the structure
used at [blog.azurebrains.com](https://blog.azurebrains.com):

- Two framing paragraphs instead of a TL;DR block.
- `## Contents` — a linked table of contents matching the H2s exactly.
- 8 to 12 sections, **all H2, no H3**. Descriptive headings ("What…", "Why…", "How…");
  generic ones are rejected.
- `## What to watch carefully` as the penultimate section — the critical read is
  mandatory, not decorative.
- Closing section: `## My take` (preferred) or `## Conclusion` for a pure news post,
  an opinion rather than a recap.
- `## Sources` — markdown links, no dates, no numbering.
- **No inline citations.** The body reads clean.
- **No in-body images of any kind.** No screenshots, no diagrams, no placeholders. A
  screenshot's information belongs in a code block, a table or three precise sentences.
  The only image is the cover, in front matter. Any image in the body is a blocker
  (rule S11).
- **No dashes and no curly quotes.** The typography rules (T family) are enforced by
  code-side regex detectors before either validator runs.

That does not weaken verification. The Source Checker validates the *dossier*, before a
word is written, so claim-to-source traceability lives there. The Content Validator then
checks the draft sentence by sentence against the dossier (rule H01), traces every number
to the dossier or the author notes (H03), and treats a dropped caveat as a blocker (H04).
You lose the visual clutter, not the guard rail.

### Author notes and voice mode

The one thing the crew cannot invent is what *you* did. Drop raw notes at
`input/notes/<slug>.md` (copy `config/author_notes.template.md`) — five minutes of what
you built, measured and broke. A normalizer turns them into typed, id'd author claims,
and the run switches to **field_report** mode: the Writer may use first person, real
numbers and real failures, but only where a claim backs them. No notes means **analysis**
mode: neutral register, no first person, a lower word target, and the specificity floor
satisfied from the dossier alone. Point elsewhere with `--notes <path>`.

Change any of this in `config/blog_profile.yaml` under `structure:` and `voice_mode:` —
the writer prompt and both validators read it.

### Translation

Translation is **opt-in, per draft**. Nothing is localised unless you ask for it.

```bash
ppn write --index 1 --translate        # English + Spanish in one run
ppn translate drafts/2026-07-27-x.md   # translate an existing draft later
```

Set `TRANSLATE_ENABLED=true` only if you want every approved draft translated
automatically. The `--translate` / `--no-translate` flag overrides that per run.

When it runs, the Translator produces the Spanish version and publishes it as a
separate WordPress draft with an `-es` slug suffix. It is a translator, not a second
editor: same sections, same order, same content. Code blocks, formulas, CLI commands
and column names are copied verbatim; product and UI terms stay in English
(`elastic tables`, not "tablas elásticas"). Structural headings are localised —
`Contents` → `Contenido`, `Sources` → `Fuentes` — and the table-of-contents anchors are
rebuilt from the translated headings. The English cover art is reused rather than
regenerated.

A rejected draft is never translated, and a translation that fails to parse never costs
you the English post.

**No multilingual plugin needed.** The two posts are independent WordPress drafts. The
Spanish file carries `translation_of: <english-slug>` in its front matter, so if you
later add Polylang or WPML you have the pairing already recorded.

Target language, localised headings and the terms to keep in English live in
`config/blog_profile.yaml` under `translation:`.

### Cover images

Every post gets a **pure generated graphic** — neon-lit artwork built from the post's
own subject matter. Nothing is composited on top: no title, no logo, no text of any
kind. The writer produces a `cover_concept` describing the scene in shapes and light,
and the shared `art_direction` in `blog_profile.yaml` supplies the neon aesthetic and a
per-category colour scheme.

The prompt explicitly excludes text, letters, logos, watermarks and UI, because image
models render words badly and a cover with mangled typography is worse than no cover.

**Which model, and where.** Set `COVER_MODEL` — the request shape is worked out from it.

```env
COVER_PROVIDER=foundry            # your Azure resource (default)
COVER_MODEL=MAI-Image-2.5-Pro     # Microsoft's own image model
```

The three request shapes are not interchangeable, which is why this is automatic:

| Model | Route | Notes |
|---|---|---|
| `MAI-Image-2.5-Pro`, `MAI-Image-2.5-Flash`, … | `/mai/v1/images/generations` | Microsoft's own. Integer `width`/`height`, **no `quality`, no `n`**, and a hard cap of **1,048,576 total pixels** with a 768px minimum edge. |
| `gpt-image-2` (GA), `gpt-image-1`/`-1.5`/`-1-mini` (limited access), `FLUX-1.1-pro` | `/openai/v1/images/generations` | OpenAI-compatible, takes a `size` string. `dall-e-3` was retired 4 March 2026. |
| any of the above on `COVER_PROVIDER=openai` | api.openai.com | A ChatGPT Plus/Pro subscription is **not** API access, and GPT Image needs Organization Verification. |

MAI's pixel cap is the one that bites: the natural blog cover size `1536x1024` is
1,572,864 pixels — over the limit. Rather than let the service reject it, `COVER_SIZE`
is **fitted down automatically** with the aspect ratio preserved (`1536x1024` →
`1248x832`, still 3:2), rounded to multiples of 16 and floored at 768px. The run logs
the substitution so it is never silent. Set `COVER_SIZE=1248x832` if you prefer to be
explicit.

`COVER_PROVIDER=mai` forces the MAI route for a deployment whose name does not start
with `MAI-`. `ppn models` lists what your resource actually serves.

```bash
ppn models                                      # what is actually deployed
ppn cover drafts/2026-07-27-my-post.md          # regenerate the art
ppn cover drafts/2026-07-27-my-post.md -c "..." # override the concept
ppn write --index 1 --no-cover                  # skip cover for this run
```

Rerun `ppn cover` until you like the art — it costs one image call, not a whole pipeline.
If generation fails the run still completes; the reason lands in the package JSON and the
draft is untouched.

### How long it takes

These are slow commands and that is expected. `ppn suggest` runs **10–20 minutes**: three
scouts each searching and fetching pages, then the Topic Editor cross-checking every
candidate against your published posts before ranking. The last step — one model call
producing the whole shortlist — is the longest and produces no output while it runs.
A full `ppn write` can take **30–60 minutes** when both loops fire.

Both commands show a spinner with the current phase and an elapsed counter, so you can
always tell the difference between working and wedged. Per-tool activity is logged one
line at a time (`ppn.tools  fetch_page learn.microsoft.com/...`).

If a run genuinely hangs, `PPN_SUGGEST_TIMEOUT_MINUTES` / `PPN_WRITE_TIMEOUT_MINUTES` break
it with a message naming the phase it died in. To make discovery faster, lower
`suggestions_per_run` or trim `watch_areas` in `config/topics.yaml` — the final generation
scales with both. `--verbose` adds the Agent Framework trace; `PPN_LOG_LEVEL=DEBUG` adds
everything.

### Output

```
topics/suggestions-2026-07-27.md      human-readable shortlist
topics/suggestions-2026-07-27.json    machine-readable, input to `ppn write`
research/2026-07-27-<slug>.json       the dossier: claims, citations, excerpts
drafts/2026-07-27-<slug>.md           the draft with YAML front matter
drafts/2026-07-27-<slug>.review.md    every finding, per rule, per validator
drafts/covers/<slug>.png              the generated cover artwork
drafts/2026-07-27-<slug>-es.md        the Spanish translation
drafts/2026-07-27-<slug>.package.json everything, including the WordPress post id
```

Markdown is converted to real **Gutenberg blocks** on push — headings, lists, code blocks
with language, tables, quotes and separators, so the post opens as editable blocks. The
body carries no images at all (rule S11 blocks them), so the converter has no image path;
the only image is the cover, uploaded separately and set as the featured image.

Re-running the same slug **updates** the existing WordPress draft instead of creating a
duplicate (tracked in `.ppn_state/wp_posts.json`, with a slug lookup as fallback).

---

## Tuning it

All behaviour lives in `config/` — no code changes needed.

| File | Controls |
|---|---|
| `blog_profile.yaml` | Audience, positioning, categories, post formats and word targets |
| `topics.yaml` | Watch areas, keywords, weights, freshness windows, exclusions |
| `sources.yaml` | RSS feeds, trust tiers per domain, blocked domains, source policy |
| `validation_rules.yaml` | The rulebook: six rule families, severities, detectors, the pass threshold |
| `style_guide.md` | Voice, structure template, banned phrases, typography, citation style |
| `author_notes.template.md` | Copy to `input/notes/<slug>.md` and fill in before a run |

Two things are worth knowing:

* **Trust tiers drive the Source Checker.** Add an MVP blog you trust to
  `community_trusted` and its citations stop being flagged. Add a domain to
  `blocked_domains` and any draft citing it fails outright.
* **Rule severity drives the loop.** A `blocker` finding forces a revision; `major` must be
  addressed or justified; `minor` is reported only. Change
  `scoring.pass_threshold` to make the validators harsher or softer.

Runtime budgets live in `.env`:

```env
PPN_MAX_SOURCE_ROUNDS=2      # researcher re-tries after a failed source check
PPN_MAX_REVISION_ROUNDS=3    # writer re-tries after validator findings
```

When a budget is exhausted the pipeline still finishes and still produces a draft — it just
marks it **NOT APPROVED**, records the outstanding findings in the review report, and (if
the source check never cleared) instructs the writer to hedge rather than assert.

---

## Running it on a schedule

Weekly topic scan, Monday 07:00:

```cron
0 7 * * 1 cd /path/to/powerplatformninja-blogger && .venv/bin/ppn suggest >> logs/suggest.log 2>&1
```

Or as a GitHub Action with `AZURE_CREDENTIAL_MODE=default` and a federated identity, so the
shortlist lands in the repo as a PR. Writing a post is best kept manual — you want to choose
the topic.

---

## Management UI

`ppn serve` starts a FastAPI service that wraps the same agents the CLI uses:
a run queue with a concurrency cap, a per-run event log you can replay or follow
live, database-backed config with version history, and the workflow graph
rendered from the code itself.

```bash
pip install -e ".[server]"
ppn serve                          # API at /api/health
PPN_MAX_CONCURRENT_RUNS=3 ppn serve
```

On first start the YAML under `config/` is imported into the database, which then
becomes authoritative — edits are versioned there rather than in git, with
history and rollback in-app.

The **React management UI** (Stage 2) lives in `ui/` — four screens over this API:
Runs, a live Run-detail canvas of the agent graph, Config, and Drafts. Dev and
build notes are in [ui/README.md](ui/README.md).

```bash
cd ui && npm install && npm run dev   # SPA on :5173, proxies /api to `ppn serve`
```

The API contract and the Azure migration seams are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Development

```bash
pytest            # runs the whole pipeline offline, both loops included
ruff check .
```

`src/ppn_blogger/testing.py` holds the stub chat client. It deliberately fails the first
source check and the first validation round, so the tests fail if either feedback loop
stops working.

### Layout

```
src/ppn_blogger/
  settings.py    .env + config/ loading
  models.py      typed contracts between agents (Pydantic)
  clients.py     Foundry chat clients (reasoning + fast)
  prompts.py     agent instructions, built from config
  agents.py      the ten agent factories
  covers.py      neon cover art (MAI / OpenAI-compatible routes)
  config_source.py  swappable config backend (YAML files or the server's DB)
  server/        FastAPI: run queue, SSE events, versioned config, drafts API
  tools.py       search, fetch, feeds, Learn, blog search, trust checks
  executors.py   gates, loops, state, artefact writing
  workflows.py   the two Agent Framework graphs
  wordpress.py   REST client + Markdown → Gutenberg blocks
  storage.py     drafts, dossiers, review reports
  cli.py         typer commands
```

---

## Cost note

A full post run is roughly: 3 scout calls + 1 editor + 1–3 researcher + 1–3 source-checker +
1–4 writer + 2–8 validator calls, each with tool round-trips. Use `FOUNDRY_MODEL_FAST` for
the scouts and keep `PPN_MAX_REVISION_ROUNDS` at 2–3 unless you are debugging.
