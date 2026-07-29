# Configuration reference

Two layers, with different jobs:

- **`.env`** — credentials, endpoints and runtime knobs. Machine-specific. Never
  committed.
- **`config/*.yaml` + `config/style_guide.md`** — editorial policy. What the blog
  is, what it will and will not publish, what the validators enforce. Committed,
  reviewed, and the thing you actually tune.

Changing the crew's behaviour almost always means editing the second layer. There
is no code change behind "be stricter about licensing claims" — it is a line in
`validation_rules.yaml`.

---

## Part 1 — environment variables

Everything below has a working default unless marked **required**. `.env` is loaded
at import time with `override=false`, so a real environment variable always wins.

### Azure AI Foundry

| Variable | Default | What it does |
|---|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | — | **Required.** `https://<resource>.services.ai.azure.com/api/projects/<project>`. The project endpoint, not the resource endpoint. |
| `FOUNDRY_MODEL` | `gpt-5` | Deployment name for the reasoning tier: Researcher, Source Checker, Writer, both Validators, Translator. |
| `FOUNDRY_MODEL_FAST` | *(empty)* | Deployment name for the scout tier. Empty means "use `FOUNDRY_MODEL`". Setting it to a cheap model is the single biggest cost lever here. |
| `AZURE_CREDENTIAL_MODE` | `cli` | `cli` → `AzureCliCredential` (`az login`). `default` → `DefaultAzureCredential` (managed identity, env vars, VS Code). Use `default` on a server. |
| `FOUNDRY_TEMPERATURE_SUPPORT` | `auto` | `auto` infers from the model name — unsupported for anything starting `gpt-5`, `gpt5`, `o1`, `o3`, `o4`. `true`/`false` pins it. See below. |

> **Why `FOUNDRY_TEMPERATURE_SUPPORT` exists.** Reasoning models reject a
> `temperature` parameter with an HTTP 400. Only the Writer, Validators and
> Translator send one, so the failure lands *after* research has completed — the
> expensive part — and takes the whole run with it. `ppn preflight` checks the real
> service in about two seconds and tells you which value to pin.

### WordPress

| Variable | Default | What it does |
|---|---|---|
| `WP_URL` | — | **Required.** Site root, no trailing slash. The REST API is derived as `<WP_URL>/wp-json/wp/v2`. |
| `WP_USERNAME` | — | **Required.** The login name, not the display name. |
| `WP_APP_PASSWORD` | — | **Required.** WP Admin → Users → Profile → Application Passwords. Spaces are stripped, so paste it either way. |
| `WP_DEFAULT_STATUS` | `draft` | Status for posts the crew creates. `draft`, `pending` or `publish`. Leave it on `draft`. |
| `WP_AUTO_PUSH` | `true` | Push at the end of a successful run. `--push` / `--no-push` overrides per run. |
| `WP_VERIFY_TLS` | `true` | Set false only for a local dev site with a self-signed certificate. |
| `WP_CODE_LANGUAGE_ATTR` | `true` | Emit `<!-- wp:code {"language":"json"} -->`. Core ignores it; the Syntax-highlighting Code Block plugin reads it. Harmless either way. |

The WordPress account needs to create posts, create terms and upload media.
**Author is not enough** — it cannot create categories. Use Editor or Administrator.

### Web search

| Variable | Default | What it does |
|---|---|---|
| `SEARCH_PROVIDER` | `foundry` | `foundry` (hosted, no key), `tavily`, `brave`, or `none`. |
| `SEARCH_CONTEXT_SIZE` | `medium` | `low` / `medium` / `high`. More context, more tokens. Foundry only. |
| `SEARCH_USER_COUNTRY` | `ES` | Biases results. Blank to disable. Foundry only. |
| `TAVILY_API_KEY` | *(empty)* | Only for `SEARCH_PROVIDER=tavily`. |
| `BRAVE_API_KEY` | *(empty)* | Only for `SEARCH_PROVIDER=brave`. |

`foundry` is the default because it needs nothing: no API key, no third-party
account, no extra Azure resource. Microsoft manages the Bing resource behind it and
the search executes inside the service.

`none` is a legitimate mode, not a degraded one — the crew falls back to the
curated RSS feeds and Microsoft Learn, which for a docs-heavy post is often the
better evidence base anyway.

### Cover images

| Variable | Default | What it does |
|---|---|---|
| `COVER_ENABLED` | `true` | Master switch. |
| `COVER_PROVIDER` | `foundry` | `foundry` (auto-detects MAI vs OpenAI-compatible from the model name), `mai` (force), `openai` (api.openai.com). |
| `COVER_MODEL` | `MAI-Image-2.5-Pro` | Deployment name. `ppn models` lists what you have. |
| `COVER_SIZE` | `1536x1024` | Requested size. On MAI this is refitted automatically — see below. |
| `COVER_QUALITY` | `high` | Used by the `gpt-image` models. Ignored by MAI and FLUX. |
| `COVER_API_VERSION` | `preview` | API version query parameter. |
| `COVER_ENDPOINT` | *(empty)* | Blank derives the resource root from `FOUNDRY_PROJECT_ENDPOINT`. Set it only if images live on a different resource. |
| `COVER_API_KEY` | *(empty)* | Blank authenticates with Entra (`az login` / managed identity). |
| `COVER_UPLOAD_TO_WP` | `true` | Upload the cover to the media library and set it as the featured image. |
| `OPENAI_API_KEY` | *(empty)* | Only for `COVER_PROVIDER=openai`. |
| `OPENAI_ORG_ID` | *(empty)* | Optional OpenAI organisation id. |

**Model availability, as of mid-2026:**

| Model | Status |
|---|---|
| `MAI-Image-2.5-Pro` | Microsoft's own. No registration. The default here. |
| `gpt-image-2` | GA, no registration. |
| `gpt-image-1`, `-1.5`, `-1-mini` | Limited access — must be approved before they appear in the deploy list. |
| `FLUX-1.1-pro` | Black Forest Labs, serverless, no registration. |
| `dall-e-3` | **Retired March 2026.** Existing deployments are dead. |

**MAI's pixel cap.** MAI accepts at most **1,048,576 total pixels** with a 768px
minimum edge, and takes integer `width`/`height` rather than a size string.
`COVER_SIZE=1536x1024` (1.57M pixels) is silently refitted to `1248x832`, keeping
the aspect ratio. You do not need to compute this yourself; the log line tells you
what it used.

> An OpenAI API key is **not** a ChatGPT Plus or Pro subscription. The API is a
> separate product with separate billing, and the GPT Image models additionally
> require Organization Verification at platform.openai.com → Settings →
> Organization → General.

### Translation

| Variable | Default | What it does |
|---|---|---|
| `TRANSLATE_ENABLED` | `false` | Translate every approved draft automatically. Leave false and decide per draft with `--translate` or `ppn translate`. |
| `TRANSLATE_PUSH` | `true` | Push the translation to WordPress as its own draft post. |
| `TRANSLATE_ONLY_WHEN_APPROVED` | `true` | Never translate a draft the validators rejected. |

### Runtime

| Variable | Default | What it does |
|---|---|---|
| `PPN_MAX_REVISION_ROUNDS` | `3` | Writer ↔ validator loops before the run finalises regardless. |
| `PPN_MAX_SOURCE_ROUNDS` | `2` | Researcher ↔ source-checker loops before the run continues regardless. |
| `PPN_SUGGEST_TIMEOUT_MINUTES` | `40` | Wall-clock ceiling for `ppn suggest`. |
| `PPN_WRITE_TIMEOUT_MINUTES` | `90` | Wall-clock ceiling for `ppn write`. |
| `PPN_OUTPUT_DIR` | `drafts` | Relative to the repo root. |
| `PPN_RESEARCH_DIR` | `research` | Relative to the repo root. |
| `PPN_TOPICS_DIR` | `topics` | Relative to the repo root. |
| `PPN_LOG_LEVEL` | `INFO` | `DEBUG` also un-silences the `agent_framework` logger. |

> The timeouts are deliberately generous. They exist to break a genuine hang, not
> to cut short honest work — topic discovery really does take 10–20 minutes, and a
> post that walks both loops can take an hour. An earlier 15/45 default would have
> killed a successful run.

Raising a loop budget also raises the workflow's `max_iterations`, which is
computed as `40 + 10 × (max_source_rounds + max_revision_rounds)`.

### Server only

| Variable | Default | What it does |
|---|---|---|
| `PPN_MAX_CONCURRENT_RUNS` | `2` | Worker count. This is the resource cap — raise it only if your Foundry quota can take it. |
| `PPN_DATABASE_URL` | `sqlite+aiosqlite:///.ppn_state/ppn.db` | Any SQLAlchemy async URL. `postgresql+asyncpg://…` works with no other change. |
| `PPN_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated. The Vite dev server defaults. |

---

## Part 2 — `config/blog_profile.yaml`

Who the blog is for and what shape a post takes.

### `blog`

Identity: `name`, `url`, `author`, `tagline`, `language` (`en`), `language_label`.
`language` is the **drafting** language — Spanish is produced afterwards by the
Translator, not written natively.

### `audience`

`primary` describes the reader in prose. `seniority: intermediate-to-advanced`.
`assumes_knowledge` and `does_not_assume` are lists, and they are load-bearing:
they are why a post explains a Dataverse plugin registration step but not what a
Dataverse table is.

### `positioning`

`must_have` — five things every post needs, including *"at least one thing that is
not obvious from the official docs"* and *"reproducible steps"*. `avoid` — three
things that get a topic killed, including *"rewriting a Learn page"*.

These reach the Topic Editor and the Content Validator. If the crew keeps proposing
things you would never write, this is the block to edit.

### `categories`

The ten WordPress categories. The Writer must pick exactly one, and it is created
in WordPress if it does not exist — so a typo here becomes a real category on the
blog.

```
Power Apps · Power Automate · Dataverse · Power Pages · Copilot Studio
ALM & DevOps · Governance & CoE · Licensing · Integration & Azure · AI
```

### `structure`

The house shape. Both the Writer and the Design Validator read it, which is what
keeps them in agreement.

| Key | Value | Effect |
|---|---|---|
| `table_of_contents` | `true` | A `## Contents` list of anchor links after the opening. |
| `toc_heading` | `Contents` | |
| `min_sections` / `max_sections` | `8` / `12` | H2 count, excluding Contents and Sources. Outside the range is a finding. |
| `heading_depth` | `2` | H2 only. **Any H3 is a blocker.** |
| `opening_paragraphs` | `2` | Problem, then payoff. |
| `tldr_block` | `false` | No TL;DR box. |
| `callout_label` | `Important:` | `> **Important:** …` |
| `max_callouts` | `3` | Per post. |
| `critical_section_heading` | `What to watch carefully` | Mandatory penultimate section. |
| `closing_headings` | `[My take, Conclusion]` | The Writer picks one; `My take` preferred, `Conclusion` only for a pure news post. |
| `banned_headings` | list | Generic titles the Design Validator rejects (rule S04): Introduction, Background, Overview, Summary, Getting started, Conclusion (as a title), Final thoughts, Wrapping up, Key takeaways. |
| `sources_heading` | `Sources` | |
| `sources_style` | `plain` | Markdown links, no dates, no numbering. |
| `inline_citations` | `false` | None in the body, anywhere. |
| `tags_per_post` | `[4, 6]` | |
| `reading_speed_wpm` | `200` | Drives `read_minutes`. |

Changing `min_sections`/`max_sections` changes both what the Writer targets and
what the Design Validator enforces, because both read this key. That is the point
of putting it here rather than in a prompt.

### `voice_mode`

Not a post format (`--format` still carries deep-dive and friends). A separate axis,
set automatically by the notes normalizer from whether real author notes were
supplied.

| Mode | When | `first_person` | `v12` | `word_target_factor` |
|---|---|---|---|---|
| `field_report` | author notes present | `true` | `blocker` | `1.0` |
| `analysis` | no notes | `false` | `relaxed` | `0.8` |

In `field_report` the Writer may use first person, but only where an author claim
backs it, and the specificity floor (V12) is a hard blocker. In `analysis` there is
no first person anywhere, the word target drops to 80% of the format band, and V12
relaxes to "a named limitation the docs understate", satisfiable from the dossier
alone. `register` is a prose sentence fed to the Writer for each mode.

### `post_formats`

Five formats, each with a word-count band the Content Validator checks (rule C04),
scaled by the voice mode's `word_target_factor`:

| id | Target words |
|---|---|
| `analysis` | 2000–2800 |
| `deep-dive` | 2400–3200 |
| `how-to` | 1600–2400 |
| `comparison` | 1800–2600 |
| `troubleshooting` | 1400–2200 |

### `cover`

- `art_direction` — the house look, in prose. Neon-lit digital graphic, electric
  cyan/magenta/violet on near-black, luminous wireframes and circuitry, high
  contrast, cinematic.
- `negative` — what must never appear: text, letters, numbers, words, captions,
  typography, logos, watermarks, signatures, UI, labelled charts, recognisable
  people, company branding.
- `palette` — a neon scheme per category, plus a `default`. Dataverse gets emerald
  and cyan, Copilot Studio purple and pink, Licensing cool steel blue and white.

The negative list is the important one. Image models want to render text, and
generated text is always subtly wrong. A purely graphic cover cannot fail that way.

### `translation`

| Key | Value |
|---|---|
| `target_language` | `Spanish (Spain)` |
| `target_code` | `es` |
| `slug_suffix` | `-es` |
| `headings.toc` | `Contenido` |
| `headings.critical` | `Lo que conviene observar con cautela` |
| `headings.closing` | `[Conclusión, Mi lectura]` |
| `headings.sources` | `Fuentes` |
| `callout_label` | `Importante:` |
| `keep_in_english` | Product and feature names; UI labels as seen in the portal; API, column, code and CLI identifiers |
| `reuse_cover` | `true` |

---

## Part 3 — `config/topics.yaml`

What the scouts go looking for.

### `watch_areas`

| id | Label | Weight | Freshness |
|---|---|---|---|
| `copilot-studio` | Copilot Studio & agents | 5 | 30 days |
| `dataverse` | Dataverse platform | 4 | 45 |
| `power-apps` | Power Apps | 4 | 45 |
| `alm-devops` | ALM & DevOps | 4 | 60 |
| `power-automate` | Power Automate | 3 | 45 |
| `governance` | Governance, CoE & security | 3 | 60 |
| `licensing` | Licensing & cost | 3 | 90 |

Each area also carries `keywords` (fed to web search verbatim) and `angle` (the
editorial lens — it steers what the Topic Editor proposes within that area).

`weight` (1–5) multiplies the final score, so raising Copilot Studio to 5 is how
that area came to dominate the shortlist. `freshness_days` is how recent an item
must be to count as news for that area — licensing changes slowly, agent tooling
does not.

### The rest

| Key | Value | Effect |
|---|---|---|
| `exclude_keywords` | `crypto`, `"AI will replace developers"` | Hard exclusion. Any matching suggestion is dropped. |
| `suggestions_per_run` | `6` | Exactly this many, best first. |
| `duplicate_similarity_threshold` | `0.72` | Above this against an existing post, reject. |

---

## Part 4 — `config/sources.yaml`

What counts as evidence.

### `feeds`

Nine feeds, each `{name, url, tier}` — six official Microsoft blogs and release
notes, two named MVP blogs, one Reddit community. The Feed Scout reads them once
per tier group.

### `trust_tiers`

| Tier | Score | Meaning |
|---|---|---|
| `official` | 5 | First-party Microsoft docs or announcement |
| `standards` | 5 | Standards bodies and RFCs |
| `community_trusted` | 4 | Recognised MVP or long-standing community authority |
| `vendor` | 3 | Vendor or partner content — useful, commercially motivated |
| `community_unverified` | 2 | Forum, Reddit or Q&A — must be corroborated |

Each tier lists `domains`. A pattern containing `/` matches as a path-scoped
substring, so `github.com/microsoft` is official and the rest of GitHub is not.
Adding a blog you trust to `community_trusted` is how you let the Researcher lean
on it.

### `blocked_domains`

Never citable. A citation from one of these is a hard fail, not a deduction.

### `policy`

| Key | Value | Effect |
|---|---|---|
| `min_average_trust` | `3.5` | Below this and the source verdict fails even with no individual blocker. |
| `min_sources_per_critical_claim` | `2` | Independent sources per critical claim. |
| `require_official_for_critical` | `true` | At least one must be `official` or `standards`. |
| `max_source_age_days_for_news` | `180` | Older-only support fails for news-like claims. |
| `hard_fail_on` | 4 conditions | `blocked_domain_cited`, `fabricated_url`, `claim_contradicted_by_official_source`, `version_or_pricing_claim_without_official_source` |

`min_average_trust: 3.5` is the quiet quality lever. It means a dossier built
mostly from Reddit fails even if every individual URL resolves.

---

## Part 5 — `config/validation_rules.yaml`

The v2 ruleset: **six families**, split across the two validators. The Content
Validator owns **honesty (H)**, **voice (V)** and **content (C)**; the Design
Validator owns **typography (T)**, **structure (S)** and **SEO (E)**. Severity:
**blocker** stops finalisation, **major** must be fixed unless justified, **minor**
is reported and never blocks, **info** is a guard rail that never fires against the
Writer.

### Detectors run in code

21 rules carry a `detector` regex. Those regexes run in **Python**, in
`detectors.py`, before either validator model is called (`run_detectors`). Rules
marked `auto: true` are decided by the detector and passed to the validator as
pre-computed findings; the model only judges the `auto: false` rules. The
measurements a `ValidationReport` carries (average sentence length, per-section word
counts, H2 count, dash hits, banned-word hits, placeholder count) are counted the
same way, never estimated by a model.

The T01/T02 detectors skip fenced code blocks, inline code spans, URLs and list
bullets, so a hyphen in `low-code`, a slug, a CLI flag or a URL is never mistaken
for punctuation.

### Families and the rules that carry the blog

| Family | Owner | The rules that matter |
|---|---|---|
| **H** honesty | Content | H01 every statement traces to the dossier; **H02** first person traces to author notes; **H03** every number/version traces to dossier or notes; H04 caveats survive; H05 placeholder discipline. Blocker-heavy. |
| **T** typography | Design | **T01** no dash characters (blocker); **T02** no spaced-hyphen punctuation (blocker); T04 straight quotes only; T06 UI paths use `>`. All regex-decided. |
| **V** voice | Content | The anti-LLM family: V01 banned vocabulary, V02 antithesis reflex, V05 signposting, V07 audience sandwich, V08 empty comparatives, V09 closing opinion, V10 sentence-length variance, **V12** specificity floor, V13 something not in the docs, V15 no first-person plural. |
| **C** content | Content | C01 two framing paragraphs; C02 reproducible steps; C03 cost of a position stated; C04 word count in band. |
| **S** structure | Design | S01 one H1/no H3; S02 8 to 12 sections; S03 Contents matches; S06 critical-read penultimate; S07 closing = My take; S08 Sources + no inline citations; S09 code fences carry a language; **S11 no images anywhere (blocker)**; S12 descriptive links. |
| **E** SEO | Design | E01 title length + keyword; E02 meta description; E04 slug format; E07 `cover_concept` is a concrete neon scene, not a restated title. |

The old `C01–C12` and `S11 (alt text)` IDs from v1 are gone. The anti-hallucination
pair is now **H01** (unsupported statement) and **H04** (dropped caveat), with
**H03** added for numbers; **S11** is now a hard blocker on *any* in-body image.

### `scoring`, `loop` and `output_schema`

```yaml
scoring:
  dimensions: { honesty: 0.30, voice: 0.30, structure: 0.20, content: 0.15, seo: 0.05 }
  deductions: { blocker: 25, major: 8, minor: 2, info: 0 }
  pass_threshold: 85
  block_on_any_blocker: true
```

Approval needs the **mean** of the two validators' scores at or above
`pass_threshold` **and** zero blockers — a blocker a detector raised in code gates
the run exactly like a model blocker. The `loop` block documents the order of
checks and the escalation conditions; the `output_schema` block is the validator
output contract, which `ValidationReport` implements (including `measurements` and
`resolved_since_last_iteration`).

### Adding a rule

Append it to the right family with an `id`, a `rule` (written as an instruction), a
`severity`, and either `auto: true` with a `detector` regex or `auto: false` with a
`check_hint`. No code change — `rules_text()` renders every rule into the right
validator's prompt, and `run_detectors()` compiles any new detector, on the next
run.

---

## Part 6 — `config/style_guide.md`

Injected verbatim into the Writer and both Validators. Editing it changes the
crew's voice with no code change at all.

**Voice.** "A consultant who just finished the implementation explaining it to a
peer over coffee." Confident, specific, occasionally dry. First person singular for
what you did — *"I hit the 2,000-row limit"*. Second person for instructing the
reader. Never a press release. Never a beginner tutorial.

**Rules of thumb.** Lead with the problem. Show the thing — a code block or a table
over prose (no in-body images). Say the number: *"3.4 s for 500 rows, 11 s for
2,000."* Admit the limits alongside the recommendation, not in a footnote. Date your
claims. One idea per section. Kill the adverbs — *simply*, *just*, *easily*.

**Banned phrases.** `game changer`, `dive deep`, `in today's fast-paced world`,
`unlock the power of`, `revolutionise`, `seamlessly`, `leverage` (as a verb),
`it's important to note that`, `in conclusion`, `the possibilities are endless`,
`supercharge`, `in this article we will see`. Enforced by rule V01 (a code-side
detector).

**Typography.** No dash characters anywhere in the prose (em dash, en dash, minus,
unicode hyphen) and no hyphen used as spaced punctuation. Straight quotes only, no
curly quotes, no ellipsis character, no emoji. Ordinary compound hyphens, product
names, slugs, CLI flags and anything inside code are correct and stay. Enforced by
the T family, which runs as code-side regex.

**Formatting.** Code fences always carry a language, from the set `powerfx`, `json`,
`csharp`, `yaml`, `bash`, `xml`, `sql`, `text`. Power Fx one statement per line.
UI paths in bold with arrows: **Settings > Environments > Managed Environments**.
**No in-body images:** no screenshots, no diagrams, no placeholders. If a screenshot
feels necessary, the information belongs in a code block, a table or precise prose.

**Citations.** None inline. Everything under `## Sources` as markdown links with
the document title — no dates, no publisher, no numbering. The guide is explicit
that this does not relax rigour: every factual statement must still be backed by a
dossier claim that already passed the Source Checker, or by an author claim.

**What a finished draft is NOT.** Not a summary of a Microsoft Learn page. Not a
feature list with no opinion. Not padded to hit a word count.

---

## Part 7 — author notes

The one input the crew cannot research is what the author personally did. Notes live
at `input/notes/<slug>.md` (copy `config/author_notes.template.md`), or anywhere you
point `--notes <path>`. A missing file, or the unfilled template, is not an error —
the post simply runs in `analysis` mode.

The **notes normalizer** (fast tier) turns the raw notes into a list of typed
`AuthorClaim`s — `measurement`, `failure`, `limit`, `environment`, `exact_string`,
`opinion`, `context` — each with a stable id. It invents nothing: an empty or
templated file yields an empty list. The claims are written to
`research/<date>-<slug>.notes.json` beside the dossier, handed to the Writer as the
only licensed source of first person, numbers and failures, and to the Content
Validator to enforce H02 and H03. The Source Checker receives them as testimony and
never verifies them; the Translator receives neither and preserves first person.

### Reloading config into a running server

`ppn serve` imports `config/` into its database once, on first start, and the
database is authoritative from then on. After a git-only swap of the YAML (like this
editorial ruleset), run **`ppn config reload`** to append the current files as a new
version of each document — the change goes live for the UI and any queued run, and
the edit history is kept. The CLI path (no server) reads `config/` directly and
needs no reload.

---

## How configuration reaches the agents

`Settings` reads the five documents through a swappable `ConfigSource`:

- **CLI** → `YamlConfigSource(config/)`. Version token is the five files' mtimes.
- **Server** → a database-backed source. Version token is `name:version|…`.

`Settings` caches parsed documents and drops the whole cache when the token
changes. So a YAML edit is live on the next run, and a config edit through the API
is live with no restart — and `Run.config_version` records exactly which versions
produced a given output.

`ppn show-config` prints the effective configuration with secrets redacted to
booleans.
