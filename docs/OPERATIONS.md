# Operations

Day-to-day use, the full CLI, and every failure that has actually happened here
with what to do about it.

- [The CLI](#the-cli)
- [A normal week](#a-normal-week)
- [Resuming a failed run](#resuming-a-failed-run)
- [Troubleshooting](#troubleshooting)
- [Cost](#cost)
- [Running the server](#running-the-server)
- [Development](#development)

---

## The CLI

Every command below takes `--help`.

### Checks

#### `ppn doctor`

Prints a configuration table and makes one live call to WordPress
(`/users/me?context=edit`). Run it after any `.env` change.

```bash
ppn doctor
```

Covers: Foundry endpoint and models, credential mode, search provider, WordPress
config plus a live connection test, cover provider, translation defaults, and the
counts of watch areas, feeds and validation rules loaded.

#### `ppn preflight`

Makes two small real model calls to establish what request shapes your deployment
accepts — structured output alone, and structured output plus `temperature`.

```bash
ppn preflight
```

If the result disagrees with what the code inferred from the model name, it tells
you to pin `FOUNDRY_TEMPERATURE_SUPPORT=true|false`. Costs a few hundred tokens and
saves an hour. Run it whenever you change `FOUNDRY_MODEL`.

#### `ppn models`

Lists model deployments visible on the configured image resource, flagging which
look like image models, plus provider-specific notes (MAI pixel caps, GPT-Image
verification requirements).

```bash
ppn models
```

#### `ppn rules` · `ppn show-config`

`rules` prints the 33 validation rules with severities, colour-coded. `show-config`
dumps the effective configuration as JSON with secrets reduced to booleans — safe
to paste into an issue.

### Producing posts

#### `ppn suggest`

```bash
ppn suggest
ppn suggest --instruction "focus on governance and DLP changes this month"
ppn suggest --explore
ppn suggest --dry-run
```

| Option | Default | Effect |
|---|---|---|
| `--instruction` / `-i` | *"Find what is worth writing about…"* | Steers the scouts. |
| `--explore` | off | Search the whole web, then approve the sites it read before any topic is proposed. |
| `--yes` / `-y` | off | With `--explore`, approve every site found at its suggested tier without prompting. |
| `--dry-run` | off | Offline stub. No network, no cost. |

Writes `topics/suggestions-<date>.json` and a readable `.md`. Takes 10–20 minutes.

**`--explore`** widens the news scout past the curated feeds and stops halfway to
show you every site it read, one row per domain, with what was found there:

```
                       Sites the scouts read — 2026-08-05
 #    ✓   Site                     Status    Tier                   Items
 1        powerplatform-diary.io   new       community_unverified       4
 2    ✓   learn.microsoft.com      trusted   official                  11
 3    ✓   matthewdevaney.com       trusted   community_trusted          3

Type numbers to toggle (e.g. 2 5), a for all, n for none, ?N to see what was
found on site N, or press Enter to accept.
```

Already-trusted sites start ticked; a site nobody has vetted has to be said yes to
explicitly. Each newly approved site is then asked for a trust tier — the choice
that decides whether a draft resting on it passes the Source Checker. Approved sites
are written into `config/sources.yaml`, so they apply to every later run and to the
Researcher on every later draft. Refused sites go to `declined_domains` and are
never offered again.

On the server the same approval happens under **Sources** in the UI, where the sweep
and the shortlist are two runs with your decision in between.

#### `ppn write`

```bash
ppn write --index 2
ppn write --index 1 --no-push --no-cover
ppn write --dossier research/2026-07-28-my-topic.json --index 1
ppn write --index 1 --notes input/notes/my-slug.md   # field-report mode
ppn write --index 1 --translate
```

| Option | Default | Effect |
|---|---|---|
| `--index` / `-n` | `1` | Which suggestion, 1-based. |
| `--topic` / `-t` | latest | A specific `suggestions-*.json`. |
| `--dossier` / `-d` | — | Reuse a saved dossier; skips the Researcher entirely. |
| `--skip-source-check` | off | Only with `--dossier`. Claims go unverified. |
| `--notes` | `input/notes/<slug>.md` | Author notes markdown. Present = field-report mode; absent = analysis mode. |
| `--push` / `--no-push` | `WP_AUTO_PUSH` | |
| `--cover` / `--no-cover` | `COVER_ENABLED` | |
| `--translate` / `--no-translate` | `TRANSLATE_ENABLED` | |
| `--dry-run` | off | Offline stub, exercising both loops. |
| `--verbose` / `-v` | off | Also shows the `agent_framework` trace. |

##### Author notes: field-report vs analysis

The single highest-value thing you can give a post is five minutes of raw notes on
what you actually did. Before a run:

```bash
cp config/author_notes.template.md input/notes/my-slug.md
$EDITOR input/notes/my-slug.md          # write badly; fragments are fine
ppn write --index 1                      # picks up input/notes/<slug>.md by default
```

With notes, the run is **field-report** mode: the normalizer turns them into typed
author claims (filed to `research/<date>-<slug>.notes.json`), and the Writer may use
first person, real numbers and real failures — but only where a claim backs them.
Without notes (or with just the unfilled template), the run is **analysis** mode:
neutral register, no first person, a lower word target, and V12 satisfied from the
dossier alone. `--notes <path>` points anywhere. The CLI prints the resulting voice
mode and claim count in the run summary.

#### `ppn write-topic`

Write something you already decided on, skipping discovery.

```bash
ppn write-topic \
  --title "Environment routing without a managed environment" \
  --area governance --format how-to \
  --problem "Makers land in the default environment and nobody notices" \
  --source https://learn.microsoft.com/power-platform/admin/... \
  --question "Does routing require Managed Environments in every case?" \
  --push
```

`--source` and `--question` are repeatable.

#### `ppn run`

`suggest` then `write` on the top-ranked suggestion, unattended. Takes an hour or
more. Useful on a schedule; less useful when you want a say in the topic.

```bash
ppn run --no-push
```

### After the draft

#### `ppn cover`

Regenerate cover art without re-running the pipeline.

```bash
ppn cover drafts/2026-07-28-my-post.md
ppn cover drafts/2026-07-28-my-post.md --concept "isometric neon lattice of connected environments" --push
```

`--push` uploads to the media library and prints the media id; attach it with
`ppn wp push`.

#### `ppn translate`

```bash
ppn translate drafts/2026-07-28-my-post.md --push
```

Writes `drafts/<date>-<slug>-es.md` with `language: es` and `translation_of` in the
front matter.

#### `ppn wp preview` · `ppn wp push` · `ppn wp check`

```bash
ppn wp check                                            # verify credentials
ppn wp preview drafts/2026-07-28-my-post.md             # print the block markup
ppn wp push    drafts/2026-07-28-my-post.md             # create or update
ppn wp push    drafts/2026-07-28-my-post.md --status publish
```

`push` **updates in place by slug** — it looks the slug up in
`.ppn_state/wp_posts.json`, then falls back to a REST query across all statuses. Re-pushing
never creates a duplicate. This is how you correct a post after changing the
Markdown or the converter.

### The server

```bash
ppn serve
ppn serve --host 0.0.0.0 --port 8000 --reload
```

### What it cost

Every run prints its own accounting when it finishes — model calls, tokens (with
the cached share broken out), searches, images, and the money those come to:

```
12 model call(s) · 604,331 tokens (487,002 in / 117,329 out, 121,750 cached) · 23 search(es)
→ ~3.39 USD (list price)
```

That figure is `counted units × configured rate`. The counts are exact; the rate is
list price, so treat the total as an estimate and Azure Cost Management as the bill.
A run that failed or timed out still prints one — the tokens were spent either way,
and that is the run you most want a number for.

#### `ppn cost`

```bash
ppn cost                        # spend by day, last 30 days
ppn cost --by kind --since 168  # by run kind, last week
```

Reads the rows the **server** stored, so only runs launched through `ppn serve` or
the UI appear. A CLI run prints its figure and keeps nothing — if a week looks
empty, that is usually why rather than a quiet week.

#### `ppn cost prices`

```bash
ppn cost prices --bind gpt-5    # list the meters that could price this model
ppn cost prices --refresh       # compare bound prices against Azure
ppn cost prices --refresh --apply
```

Binding is manual and happens once per model, because Azure's meter names cannot
be matched to a deployment without guessing and a wrong guess is undetectable —
see [CONFIGURATION.md](CONFIGURATION.md) Part 6. `--bind` prints a `meters:` block
to paste into `config/model_prices.yaml`; the Config screen's **Update from Azure**
button does the same thing with a diff you approve.

After that a weekly scheduler job re-reads those exact meter names and saves any
that moved as a new config version. Safe to leave unattended: it can only change a
number, never a binding, and a run's cost is stored when the run happens, so a new
price never rewrites history.

> **On a live server, the prices document needs one import.** `config/` is seeded
> into the database only on the very first start, so an existing deployment will
> not have `model_prices` until you run `ppn config reload` (or `POST
> /api/config/reload`). Until then runs report tokens and withhold the money.

---

## A normal week

```bash
ppn suggest                       # Monday, over coffee. 15 minutes.
                                  # read topics/suggestions-<date>.md
ppn write --index 3               # pick one. 20-60 minutes.
                                  # read drafts/<date>-<slug>.review.md
                                  # edit the draft in WordPress
                                  # press Publish yourself
ppn translate drafts/<file>.md --push   # if the post warrants it
ppn cost --since 168              # Friday. What did the week cost?
```

The review report is the part worth reading properly. It lists both validators' findings
rule by rule, with the exact location and a concrete fix for each, plus the Source
Checker's verdict — which URLs did not resolve, which claims lacked corroboration,
which excerpts did not appear on the page they were attributed to. A draft that
scored 88 with two majors is often better than one that scored 84 with none; the
report tells you which.

---

## Resuming a failed run

The dossier is written to `research/` **the moment the Researcher finishes**, before
anything downstream can fail. Research is the expensive stage — dozens of fetches,
several minutes — and it should never be paid for twice.

```bash
ls -t research/ | head -3
ppn write --dossier research/2026-07-28-my-topic.json --index 1
```

This enters the graph at `dossier_entry` instead of `brief_builder`. The Researcher
is not part of the graph at all in this mode.

If the source check already passed on the original run and you are only retrying a
downstream failure, skip it:

```bash
ppn write --dossier research/2026-07-28-my-topic.json --index 1 --skip-source-check
```

That goes straight to the Writer with a synthetic passing verdict. Use it when you
know the dossier was already verified — the log will warn that claims in the
resulting draft are unverified.

---

## Troubleshooting

### `Unsupported parameter: 'temperature' is not supported with this model`

**What happened.** A reasoning model (`gpt-5`, `o1`, `o3`, `o4`) rejected the
`temperature` the Writer sent. The run dies at the Writer — *after* research
succeeded.

**Fix.**

```bash
ppn preflight                       # confirms it in ~2 seconds
echo "FOUNDRY_TEMPERATURE_SUPPORT=false" >> .env
ppn write --dossier research/<the dossier that survived>.json --index 1
```

The name-based inference covers the known families; `preflight` is the check, and
the env var is the override.

### `401` or `403` from Foundry

Subscription Owner is not a data-plane role. In the **AI Foundry resource** → IAM,
assign yourself **Azure AI User** (models) and **Cognitive Services OpenAI User**
(images). Then:

```bash
az login
az account set --subscription "<name>"
```

Role assignments take a few minutes to propagate. If it fails immediately after
assigning, wait five minutes before debugging anything else.

### WordPress rejects the credentials (401)

- Is `WP_USERNAME` the login name, not the display name?
- Was the Application Password revoked? Regenerate it.
- Is a security plugin blocking the REST API or Basic auth headers? Some plugins
  strip `Authorization` — check the plugin's REST settings.

```bash
ppn wp check
```

### WordPress rejects the post (400)

Usually a term the account cannot create. **Author cannot create categories.** Use
Editor or Administrator.

### Gutenberg: "This block contains unexpected or invalid content"

Gutenberg validates a block by re-running its `save()` and diffing the result
against the stored markup. Any difference is reported as invalid content, even when
the post renders correctly on the front end.

If it is a **code block** and you are on a build from before the fix, the cause is
one of three serialisation mismatches — quotes escaped as `&quot;`, a `class` on
`<code>`, or an unescaped `[`. Update, then re-push:

```bash
ppn wp preview drafts/<file>.md | head -40    # eyeball the markup
ppn wp push    drafts/<file>.md               # updates in place by slug
```

For any other block, `ppn wp preview` prints exactly what would be sent — compare
it against what the editor produces for the same block by hand.

### An image or `IMAGE:` marker appears in the draft

It should not, and the Design Validator blocks it (rule S11) before publish — this
blog has no in-body images at all. There is no converter path that renders one, so
a marker that survived to WordPress means a blocker was overridden. Remove it: the
information in a screenshot belongs in a code block, a table or precise prose. The
only image is the cover, in front matter.

### Cover generation returns 400 on MAI

MAI caps images at 1,048,576 total pixels with a 768px minimum edge, and takes
integer `width`/`height` rather than a size string. `fit_to_mai_limits()` refits
automatically — `1536x1024` becomes `1248x832` — so a 400 here usually means
something else. Check the log line for the dimensions actually sent.

### `DeploymentNotFound` / 404 on cover generation

```bash
ppn models
```

The deployment name in `COVER_MODEL` must match exactly, including case.

### Cover fails with a 403 mentioning verification

`COVER_PROVIDER=openai` with GPT Image requires Organization Verification at
platform.openai.com → Settings → Organization → General. An OpenAI **API key** is
also not a ChatGPT Plus/Pro subscription — different product, separate billing.

Easiest fix: use `MAI-Image-2.5-Pro` on your existing Foundry resource.

### A run hangs

The timeouts (40 min for suggest, 90 for write) exist to break a genuine hang. Long
is not the same as hung — the scouts fetch every page they intend to cite.

```bash
ppn write --index 1 --verbose      # shows the agent_framework trace
```

`ppn.tools` logs one line per tool call, so the last line tells you which fetch is
sitting there.

### The validators never approve anything

Read the review report first — if the same rule fires every time, the crew is
telling you something. Then, in order of preference:

1. Fix the rule if it is wrong for your blog (`config/validation_rules.yaml`).
2. Lower `scoring.pass_threshold` from 85.
3. Raise `PPN_MAX_REVISION_ROUNDS`.

If the same **honesty** or **voice** blocker fires every round, the crew is usually
right and the material is missing. V12 (specificity floor) and H02/H03 (first
person and numbers must trace to something) fail when there are no author notes to
draw on — supply them rather than softening the rule (see below).

Do not disable `block_on_any_blocker`. The blockers include H01 (unsupported
claims), H03 (invented numbers) and H04 (dropped caveats) — the rules that keep the
blog honest. If a typography blocker (T01 dash, S11 image) keeps firing, that is a
code-side detector, not a matter of opinion: fix the draft, not the threshold.

### A run was `interrupted`

The server marks runs `interrupted` on startup if they were still `queued` or
`running` when the process died. Nothing is lost that was written to disk; check
`research/` for a dossier and resume with `--dossier`.

---

## Cost

Rough per-run token usage, dominated by the fetched page content in the research
stage rather than by the generation.

| Run | Model calls | Notes |
|---|---|---|
| `ppn suggest` | ~10–20 | Mostly fast tier. Setting `FOUNDRY_MODEL_FAST` cuts this substantially. |
| `ppn write` | ~10–25 | All reasoning tier. Each revision round adds three calls (writer + two validators). |
| Cover | 1 image | |
| Translation | 1 large call | |

Levers, in order of effect:

1. **Set `FOUNDRY_MODEL_FAST`.** The scouts are the highest-volume, lowest-value
   calls in the system.
2. **`SEARCH_CONTEXT_SIZE=low`.** Fewer tokens per search result.
3. **Lower `PPN_MAX_REVISION_ROUNDS`.** Each round is three reasoning calls.
4. **`COVER_ENABLED=false`** if you make covers yourself.

---

## Running the server

```bash
ppn serve
```

On first start, `config/*.yaml` is imported into `.ppn_state/ppn.db` and the
database becomes authoritative. **Later edits to the YAML files are not picked
up** — edit through the API, where changes are versioned and rollback-able.

To reset config back to the files, delete the database:

```bash
rm .ppn_state/ppn.db*
```

Runs and their event logs live in the same database, so that deletes history too.

**Concurrency.** `PPN_MAX_CONCURRENT_RUNS` (default 2) is the resource cap. Raise
it only if your Foundry quota can take it — two `write` runs in parallel is already
a lot of tokens per minute.

**Postgres.** Set `PPN_DATABASE_URL=postgresql+asyncpg://…`. No other change.

---

## Development

```bash
pytest                       # 31 tests, ~6 seconds, fully offline
ruff check src tests
```

Both test files run real workflow graphs. `tests/test_server.py` runs real HTTP
against the app with a real queue and real SSE.

**Everything is testable without Azure.** `stub_clients()` returns schema-valid
canned objects, and with `exercise_loops=True` it fails the first source check and
the first validation round — so a dry run walks both loops rather than taking the
happy path.

Two habits worth keeping:

**Introspect the installed package rather than trusting the docs.** Both of the
hardest integration problems here — Microsoft Agent Framework's real executor API,
and MAI's non-OpenAI image route — were solved by reading the installed code, not
by reading documentation about it.

**When a test hangs, get a stack dump rather than guessing.** A ~50% intermittent
hang in the server tests turned out to be five leaked aiosqlite worker threads
blocking teardown. Adding `-p no:cacheprovider --timeout` guesswork found nothing;
`pytest -o faulthandler_timeout=40` dumped every thread's stack and named the
culprit on the first run.
