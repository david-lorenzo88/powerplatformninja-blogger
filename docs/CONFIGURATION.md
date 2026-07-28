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
| `WP_SCREENSHOT_PLACEHOLDER` | `image` | `image` → an empty `core/image` block (a clickable upload slot in the editor) plus an instruction note. `note` → the note only. |

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
| `min_sections` / `max_sections` | `8` / `11` | H2 count, excluding Contents and Sources. Outside the range is a finding. |
| `heading_depth` | `2` | H2 only. **Any H3 is a blocker.** |
| `opening_paragraphs` | `2` | Problem, then payoff. |
| `tldr_block` | `false` | No TL;DR box. |
| `callout_label` | `Important:` | `> **Important:** …` |
| `max_callouts` | `3` | Per post. |
| `critical_section_heading` | `What to watch carefully` | Mandatory penultimate section. |
| `closing_headings` | `[Conclusion, My take]` | The Writer picks one. |
| `sources_heading` | `Sources` | |
| `sources_style` | `plain` | Markdown links, no dates, no numbering. |
| `inline_citations` | `false` | None in the body, anywhere. |
| `tags_per_post` | `[4, 6]` | |
| `reading_speed_wpm` | `200` | Drives `read_minutes`. |

Changing `min_sections`/`max_sections` changes both what the Writer targets and
what the Design Validator enforces, because both read this key. That is the point
of putting it here rather than in a prompt.

### `post_formats`

Five formats, each with a word-count band the Content Validator checks (rule C11):

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

33 rules in three groups. The Content Validator sees `content`; the Design
Validator sees `structure` and `seo`. Severity: **blocker** stops finalisation,
**major** must be fixed unless justified, **minor** is reported and never blocks.

### `content_rules` — Content Validator

| id | Severity | Rule |
|---|---|---|
| C01 | blocker | Opens with two framing paragraphs naming the problem. No throat-clearing. |
| C02 | blocker | Contains something not in the official docs — a gotcha, a limit, a measured number, a non-obvious combination. |
| C03 | blocker | **Every factual statement traces to a dossier claim.** |
| C04 | major | Preview/GA status stated, with the date it was checked. |
| C05 | blocker | Steps are reproducible. |
| C06 | major | Licensing and capacity implications called out. |
| C07 | blocker | **Dossier caveats survive into the prose.** |
| C08 | major | The closing section gives an opinion, not a recap. |
| C09 | major | Claims honestly hedged — "in my tenant", "as of July 2026". |
| C10 | major | No filler phrases (the style guide's banned list). |
| C11 | minor | Word count within the chosen format's band. |
| C12 | major | Clear idiomatic English; exact Microsoft product and UI naming. |

C02 is the rule that defines the blog. C03 and C07 are the anti-hallucination pair
— they are the entire reason the validator receives the dossier alongside the
draft.

### `structure_rules` — Design Validator

| id | Severity | Rule |
|---|---|---|
| S01 | blocker | Exactly one H1, everything else H2, no H3. |
| S02 | major | 8–11 H2 sections, excluding Contents and Sources. |
| S03 | blocker | `## Contents` right after the opening, matching H2 order. |
| S04 | major | Descriptive section titles; Introduction/Background/Overview/Summary banned. |
| S05 | blocker | Penultimate section is the critical read, with real risks. |
| S06 | major | Last content section is Conclusion or My take. |
| S07 | blocker | `## Sources` at the end; no inline citations in the body. |
| S08 | blocker | Every fenced code block declares a language. |
| S09 | major | No section runs ~350 words without a list, table, code block or callout. |
| S10 | minor | Callouts as labelled blockquotes, max 3. |
| S11 | major | Images carry real alt text; placeholders use `![alt](IMAGE:slug)`. |
| S12 | minor | A table for any comparison of 3+ items across 2+ dimensions. |
| S13 | major | Descriptive link anchors — never "here", never a bare URL. |
| S14 | minor | No emoji. |

### `seo_rules` — Design Validator

| id | Severity | Rule |
|---|---|---|
| E01 | major | Title 45–65 chars, contains the primary keyword. |
| E02 | major | Meta description 140–158 chars, reads as a sentence. |
| E03 | minor | Primary keyword in the first 100 words and in at least one H2. |
| E04 | minor | Slug lowercase, hyphenated, ≤60 chars, no stop words. |
| E05 | minor | 2–5 internal links to existing posts. |
| E06 | minor | One category from the taxonomy plus 4–6 tags. |
| E07 | minor | `cover_concept` is a concrete neon-lit scene from the post's subject, with no text instruction and not a restated title. |

### `scoring`

```yaml
scoring:
  dimensions: [content, structure, seo]
  pass_threshold: 82
  block_on_any_blocker: true
```

Approval needs the **mean** of the two validators' scores at or above
`pass_threshold` **and** zero blockers. Both conditions, not either.

### Adding a rule

Append it to the right group with an `id`, a `rule` (written as an instruction), a
`severity`, and optionally a `check_hint` telling the validator how to verify it.
No code change — `rules_text()` renders every rule into the validator's prompt on
the next run.

---

## Part 6 — `config/style_guide.md`

Injected verbatim into the Writer and both Validators. Editing it changes the
crew's voice with no code change at all.

**Voice.** "A consultant who just finished the implementation explaining it to a
peer over coffee." Confident, specific, occasionally dry. First person singular for
what you did — *"I hit the 2,000-row limit"*. Second person for instructing the
reader. Never a press release. Never a beginner tutorial.

**Rules of thumb.** Lead with the problem. Show the thing — code, table or
screenshot over prose. Say the number: *"3.4 s for 500 rows, 11 s for 2,000."*
Admit the limits alongside the recommendation, not in a footnote. Date your claims.
One idea per section. Kill the adverbs — *simply*, *just*, *easily*.

**Banned phrases.** `game changer`, `dive deep`, `in today's fast-paced world`,
`unlock the power of`, `revolutionise`, `seamlessly`, `leverage` (as a verb),
`it's important to note that`, `in conclusion`, `the possibilities are endless`,
`supercharge`, `in this article we will see`. Enforced by rule C10.

**Formatting.** Code fences always carry a language, from the set `powerfx`, `json`,
`csharp`, `yaml`, `bash`, `xml`, `sql`, `text`. Power Fx one statement per line.
UI paths in bold with arrows: **Settings → Environments → Managed Environments**.
Image placeholders `![Descriptive alt text](IMAGE:short-slug)`.

**Citations.** None inline. Everything under `## Sources` as markdown links with
the document title — no dates, no publisher, no numbering. The guide is explicit
that this does not relax rigour: every factual statement must still be backed by a
dossier claim that already passed the Source Checker.

**What a finished draft is NOT.** Not a summary of a Microsoft Learn page. Not a
feature list with no opinion. Not padded to hit a word count.

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
