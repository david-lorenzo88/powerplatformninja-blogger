# How it works

Every stage, in order, with the decisions each one makes and the reasoning behind
how it was built. This is the document to read before changing anything.

- [The shape of the thing](#the-shape-of-the-thing)
- [Why typed objects, not prose](#why-typed-objects-not-prose)
- [Workflow 1 — topic discovery](#workflow-1--topic-discovery)
- [Workflow 2 — writing a post](#workflow-2--writing-a-post)
- [The source loop](#the-source-loop)
- [The outline stage](#the-outline-stage)
- [The revision loop](#the-revision-loop)
- [Cover art](#cover-art)
- [Publishing to WordPress](#publishing-to-wordpress)
- [Translation](#translation)
- [The tools agents can call](#the-tools-agents-can-call)
- [Where state lives](#where-state-lives)
- [The server](#the-server)
- [Testing without Azure](#testing-without-azure)

---

## The shape of the thing

Eleven agents, two workflow graphs, one non-agent stage (cover art), and a publisher.

```
                        ┌──────────────────┐
   ppn suggest ────────▶│ topic discovery  │────▶ topics/suggestions-<date>.json
                        └──────────────────┘
                                 │  (you pick one)
                                 ▼
                        ┌──────────────────┐
   ppn write   ────────▶│  post pipeline   │────▶ drafts/  +  WordPress draft
                        └──────────────────┘
```

Orchestration is **Microsoft Agent Framework** (`agent_framework` 1.12+). The unit
of composition is a `Workflow` built from `Executor` nodes and edges. Two kinds of
node appear in these graphs:

- **`AgentExecutor`** — wraps an LLM agent. Receives an `AgentExecutorRequest`,
  returns an `AgentExecutorResponse`.
- **Custom `Executor` subclasses** — plain Python. These are the *gates*: they
  parse the agent's output into a typed object, decide where the run goes next, and
  own every loop condition in the system.

That split is deliberate. **No routing decision is made by a model.** The agents
produce judgements; the gates act on them. A gate is a `@handler` method you can
read, test and set a breakpoint in.

---

## Why typed objects, not prose

Every agent is bound to a Pydantic model through `response_format`:

```python
Agent(
    name="researcher",
    chat_client=clients.reasoning,
    instructions=researcher_instructions(...),
    tools=_searchable(tools.RESEARCHER_TOOLS, clients.reasoning),
    default_options=_opts(ResearchDossier),   # ← response_format
)
```

The service is then responsible for producing JSON that validates against the
schema. The gate calls `parse_model(response, ResearchDossier)` and gets an object
with `.claims`, `.citations`, `.open_questions`. Nothing downstream does regex
archaeology on a wall of markdown.

The models live in `models.py`. The important ones:

| Model | Produced by | Carries |
|---|---|---|
| `ScoutReport` | each scout | `items: list[SignalItem]`, `notes` |
| `TopicSuggestionSet` | Topic Editor | `suggestions`, `discarded` |
| `AuthorClaimSet` | Notes Normalizer | `claims: list[AuthorClaim]` — the author's testimony, typed and id'd |
| `ResearchDossier` | Researcher | `claims`, `citations`, `examples`, `gotchas`, `limits`, `open_questions`, `suggested_outline` |
| `PostOutline` | Outliner | `thesis`, `reader_promise`, `out_of_scope`, `sections` (each with `claim_ids`), `warnings` |
| `SourceVerdict` | Source Checker | `passed`, `average_trust`, `fabricated_urls`, `contradictions`, `findings`, `instructions_for_researcher` |
| `Draft` | Writer / Translator | `markdown`, `title`, `slug`, `meta_description`, `thesis`, `tags`, `cover_concept`, `revision`, `changelog` |
| `ValidationReport` | each Validator | `score`, `passed`, `findings: list[RuleFinding]`, `strengths` |
| `ReviewOutcome` | `ReviewGate` | aggregate of the above plus the source verdict |
| `PostPackage` | `Finalizer` | everything, serialised to `.package.json` |

A `Claim` carries `criticality` (`critical` / `supporting` / `colour`),
`citation_ids` and a `caveat`. That structure is what makes the Source Checker
possible at all — it can walk the critical claims and demand corroboration for each
one specifically, rather than judging a blob of text.

### Temperature, and a production failure

`_opts()` builds the options dict:

```python
def _opts(response_format, temperature=None):
    options = {"response_format": response_format}
    if temperature is not None and get_settings().foundry.supports_temperature:
        options["temperature"] = temperature
    return options
```

Only five agents ask for a temperature at all: Writer `0.7`, both Validators `0.2`,
Translator `0.3`, Outliner `0.3`. The scouts, Topic Editor, Researcher and Source Checker do not —
their job is retrieval and judgement, not variety.

The guard exists because reasoning models reject the parameter outright:

```
BadRequestError: Unsupported parameter: 'temperature' is not supported with this model
```

That error killed a real run six minutes in, *after* research had completed
successfully, because the Writer was the first agent to request a temperature.
`supports_temperature` now infers from the model name (`gpt-5`, `gpt5`, `o1`, `o3`,
`o4` prefixes → unsupported) and `ppn preflight` verifies the inference against the
live service in about two seconds. `FOUNDRY_TEMPERATURE_SUPPORT=true|false` pins it
if the heuristic is ever wrong.

---

## Workflow 1 — topic discovery

`build_topic_discovery_workflow()` in `workflows.py`. Entry point:
`scout_dispatcher`.

```mermaid
flowchart LR
    scout_dispatcher --> news_scout
    scout_dispatcher --> feed_scout
    scout_dispatcher --> docs_scout
    news_scout --> scout_aggregator
    feed_scout --> scout_aggregator
    docs_scout --> scout_aggregator
    scout_aggregator --> topic_editor
    topic_editor --> topic_publisher
```

### `scout_dispatcher` → the three scouts

`ScoutDispatcher` builds one prompt listing the watch-area ids from
`config/topics.yaml` and fans it out to all three scouts, which run **concurrently**.
Each is a different lens on the same question, and each is blind to what the others
find:

- **News Scout** (`web_search`, `fetch_page`, `search_existing_posts`) — one search
  per watch area, restricted to that area's `freshness_days` window. Fetches
  anything substantive before reporting it. Capped at 4 items per area, 15 total.
- **Feed Scout** (`read_feeds`, `fetch_page`, `search_existing_posts`) — reads the
  curated feeds in `config/sources.yaml`, once for the official tier and once for
  the community tiers, then fetches the 5–8 most substantive entries. Prefers
  release-plan and product-blog entries that describe a concrete change.
- **Docs Scout** (`search_microsoft_learn`, `fetch_page`, `search_existing_posts`) —
  looks for docs updated in the last 60 days, pages about limits, quotas, throttling
  and licensing, and preview-vs-GA flags. Its prompt calls it "the counterweight to
  hype": its job is documented reality, including the quiet limitations, not
  announcements.

All three run on the **fast** model tier (`FOUNDRY_MODEL_FAST`). They make many
cheap calls; the reasoning happens later.

Every scout is forbidden from inventing URLs, from reporting anything already
covered on the blog (checked live via `search_existing_posts`), and from writing
anything resembling a post. Each item needs a one-sentence *why it matters* and a
`watch_area` id.

### `scout_aggregator`

`ScoutAggregator` is a fan-in node. It parses each `ScoutReport` and wraps it as
`<scout name="...">…json…</scout>`. A report that fails to parse is passed through
as raw text with `parse_error="true"` rather than being dropped — a scout that
produced good findings in a bad envelope should not silently vanish.

### `topic_editor`

`TopicSuggestionSet`, on the reasoning tier, with only `search_existing_posts` and
`today`. No web search: everything it needs is in front of it. Its method, as
instructed:

1. Cluster overlapping signals into one topic.
2. Kill anything that is just news unless it solves a reader problem.
3. Cross-check each idea against the blog, recording overlap in `duplicate_risk`.
4. Assign a `post_format` and an effort rating.
5. Score `0.4 × timeliness + 0.35 × audience_fit + 0.25 × novelty`, adjusted down
   for high effort and duplicate risk, and weighted by the watch area's `weight`.
6. Write `key_questions` that are specific and answerable — they become the
   research brief.

Returns exactly `topics.suggestions_per_run` suggestions (default 6), best first,
plus a `discarded` list with half-sentence reasons. The discard list matters: it is
how you tune the watch areas without re-running discovery.

### `topic_publisher`

Writes `topics/suggestions-<date>.json` and a human-readable `.md` beside it, then
yields the `TopicSuggestionSet` as the workflow output. The markdown version prints
the exact `ppn write --topic … --index N` command for each suggestion.

---

## Workflow 1b — exploration mode, and the source review

The crew above is deliberately narrow: nine curated feeds, Microsoft Learn, and a
news scout whose trust tiers pull it back toward the same domains. That is what
makes the research defensible, and it is also why a tenth topic run keeps finding
the ninth run's material.

**Exploration mode** lifts the ceiling without lowering the bar. The scouts range
across the open web, and the run then *stops* — with a list of every site they
read — until a human says which of those sites the blog is allowed to use.

```mermaid
flowchart LR
    scout_dispatcher --> news_scout
    scout_dispatcher --> feed_scout
    scout_dispatcher --> docs_scout
    news_scout --> source_harvester
    feed_scout --> source_harvester
    docs_scout --> source_harvester
    source_harvester --> review[/"source review — you"/]
    review --> scout_replay
    scout_replay --> topic_editor
    topic_editor --> topic_publisher
```

Two graphs — `build_source_exploration_workflow()` and `build_shortlist_workflow()`
— rather than one paused graph. The approval sits *between two runs*, so no worker
(and no model connection) is held open for however long you take, and a server
restart mid-review costs nothing: the sweep is already banked in the database.

### The wide sweep

Only the **News Scout** changes: `news_scout_instructions(settings, explore=True)`
appends an exploration block that tells it to look past the usual suspects —
independent consultants, engineering blogs, conference write-ups, GitHub
discussions, regional communities, non-English sources with real substance — and
that an unfamiliar domain is *not* a problem, because every new site is shown to a
human before it can influence anything. Its cap rises from 4/15 items to 8/25, and
it is asked for at least 8 distinct domains. The feed and docs scouts are unchanged:
they are the counterweight the sweep is measured against.

### `source_harvester`

A fan-in node that ends the run. It parses the scout reports, then calls
`harvest_candidates()` in `sources.py` to group every reported item **by the site it
came from**:

- domain (`www.` stripped), the most common `source_name` as a label
- `known` — whether `sources.yaml` already gives it a tier
- `current_tier` / `suggested_tier` — an already-trusted site is offered at the tier
  it has; a site nobody has classified is offered at `community_unverified`
- which scouts found it, and every item they found there

New sites sort first, then by contribution, so the list opens on the decisions that
actually need making. Sites already on `declined_domains` are never offered again.

This grouping is **code, not judgement** — no agent is asked what it searched. What
you approve is a faithful record of where the scouts actually went.

### The review

The run finishes `succeeded` with `awaiting_source_approval: true` and a review id.
`source_reviews` in the database holds the candidates *and the raw scout reports*,
so the shortlist is later built from exactly the material you were shown.

You answer it in the UI (**Sources**) or, on the CLI, in the prompt that
`ppn suggest --explore` puts in front of you. Each site gets a checkbox and a trust
tier. Approving one:

1. adds its domain to that tier's `domains` list in `sources.yaml`, as a **new
   config version** — so it is trusted by every future topic run *and* by the
   Researcher and Source Checker on every future draft;
2. lets its findings through to the topic editor for this run.

Turning down a site the config has never seen records it in `declined_domains`:
never proposed again, but **not** blocked — a declined site does not fail a draft
that cites it, it is simply never suggested. Turning down a site that already has a
tier only skips it for this shortlist; silently demoting a trusted feed would be a
much larger decision than unticking a box.

The tier choice is the consequential part, and the UI says so: `policy.min_average_trust`
is 3.5, so a draft resting mainly on `community_unverified` (score 2) sources will
fail the source gate. Promote a genuinely good MVP blog to `community_trusted` (4)
and it will not.

`sources.yaml` is edited **line by line**, not re-serialised: half the file is
explanation, and a round-trip through `yaml.safe_dump` would throw all of it away.
`merge_into_yaml_text()` verifies its own edit against the mapping `apply_decisions()`
computes, and falls back to a full dump if the two ever disagree — losing the
comments is bad, writing config nobody predicted is worse.

### `scout_replay`

Entry point of the second run. It calls `filter_reports()` to drop every item that
did not come from an approved site, then briefs the same `topic_editor` with the
same envelope, plus a preamble naming the approved sources and telling it not to
reintroduce anything from memory. The filtering lives here rather than at the caller
so there is exactly one place enforcing *the editor only ever sees approved
sources*. From `topic_editor` on, the graph is identical to ordinary discovery.

---

## Workflow 2 — writing a post

`build_post_workflow()`. Entry point: `brief_builder` (or `dossier_entry` when
resuming).

```mermaid
flowchart TD
    brief_builder -->|notes present| notes_normalizer
    notes_normalizer --> notes_gate
    notes_gate --> researcher
    brief_builder -->|no notes| researcher
    researcher --> dossier_gate
    dossier_gate --> source_checker
    source_checker --> source_gate
    source_gate -->|failed, budget left| researcher
    source_gate -->|passed, or budget spent| outliner
    outliner --> outline_gate
    outline_gate --> writer
    writer --> draft_gate
    draft_gate --> content_validator
    draft_gate --> design_validator
    content_validator --> review_gate
    design_validator --> review_gate
    review_gate -->|not approved, budget left| writer
    review_gate -->|approved, or budget spent| finalizer
    finalizer -->|only if translating| translator
    translator --> translation_gate
```

`max_iterations` for the graph is computed, not guessed:
`40 + 10 × (max_source_rounds + max_revision_rounds)`. Raising a loop budget in
`.env` raises the graph budget with it.

### `brief_builder`

Turns the chosen `TopicSuggestion` into a `<research_brief>` — the angle, the
problem statement, the `key_questions`, the seed sources. It also stores the topic
on the shared `RunState` and decides the route: if real author notes were supplied
it sends them to the normalizer first; if not, the brief goes straight to the
Researcher and the run is `analysis` mode.

### `notes_normalizer` and `notes_gate` — the author's testimony

New per-post input: `input/notes/<slug>.md` (or `--notes <path>`). Raw, badly
written notes of what the author actually built, measured and broke — the only
place the Writer is allowed to get first person, real numbers and real failures.

`BriefBuilder` decides whether the file has real content. The unfilled template, or
a missing file, produces an **empty claim list** and puts the run in `analysis`
mode — no model call, and nothing is inferred. When there are notes, the
**Notes Normalizer** (fast tier, `AuthorClaimSet`) turns them into typed
`AuthorClaim`s (`measurement`, `failure`, `limit`, `environment`, `exact_string`,
`opinion`, `context`), each with a stable id. Its one hard rule: extract only what
is written, invent nothing. `NotesGate` files the claims to
`research/<date>-<slug>.notes.json` beside the dossier, sets the run to
`field_report`, and briefs the Researcher.

The claims travel through the run with a strict contract:

- The **Researcher** gets the raw notes text as search seeds (the error strings and
  version numbers in them are exactly what to search for), but treats them as
  unverified like anything else.
- The **Source Checker** gets the claims as *testimony*. It must not verify them,
  and they cannot fail `source_gate`.
- The **Writer** gets the claims as the only permitted source of first person,
  numbers and failures, with the placeholder mechanism for anything missing.
- The **Content Validator** gets the claims to enforce H02 (first person traces to a
  claim) and H03 (every number traces to the dossier or a claim).
- The **Translator** gets neither, and preserves first person as first person.

### `researcher`

The most tool-heavy agent: Microsoft Learn search, web search, page fetching, feed
reading, blog search, and a trust-classification tool.

Its instructions are specific about method, and the specificity is what makes the
Source Checker's job possible:

- Microsoft Learn **first**, for documented behaviour. Official docs outrank
  everything.
- Web search for practitioner reality — what actually happens in a tenant.
- **Fetch every source before citing it.** "If the fetch fails, the source does not
  exist for your purposes."
- Run `assess_source_trust` on the URL list; replace anything `blocked` or
  `unknown` unless it is corroborated.
- Capture the **exact supporting sentence** in each citation's `excerpt`.

Hard rules: every claim gets an id (`C1`, `C2`…) and cites citation ids. Any claim
about limits, quotas, pricing, licensing or GA/preview status is `critical`, needs
at least `min_sources_per_critical_claim` independent sources (default 2), and at
least one of them must be official Microsoft. Anything unverifiable goes to
`open_questions` — never guessed. Version, region, preview and tenant caveats are
recorded explicitly.

It never writes the post.

### `dossier_gate`

Parses the `ResearchDossier` and **immediately writes it to `research/`**, before
sending anything downstream.

This is the single most valuable line in the file. Research is the expensive stage —
dozens of fetches, minutes of wall clock — and everything after it can fail. Saving
here means a failure at the Writer costs you the Writer, not the whole run. It is
what makes `ppn write --dossier …` possible.

---

## The source loop

The Source Checker is **adversarial by construction**. Its prompt opens by telling
it to assume the research contains at least one fabricated URL, one overstated
claim, and one source that does not say what it is quoted as saying.

Its procedure, in order:

1. `check_url_reachable` on **every** dossier URL, in one call. Any URL that does
   not resolve is a blocker.
2. `assess_source_trust` on the same list. Any `blocked`-tier domain is a hard
   fail. Compute the average trust score.
3. For every `critical` claim, `fetch_page` its cited sources and **confirm the
   excerpt actually appears**. A mismatch is a blocker, with the evidence recorded.
4. For limits, pricing, licensing and GA/preview claims, require at least one
   official Microsoft source. Missing one is
   `version_or_pricing_claim_without_official_source` — a hard fail.
5. **Actively search for contradictions** via Microsoft Learn on the critical
   claims. Being contradicted by official docs is a hard fail.
6. Check source recency for news-like claims against
   `max_source_age_days_for_news` (default 180).

`passed` is true only with zero blockers, no hard-fail condition, **and**
`average_trust >= min_average_trust` (default 3.5).

### The gate

`SourceGate.route()` owns the loop:

```python
if not verdict.passed and self.state.source_round < max_rounds:
    self.state.source_round += 1
    → back to the researcher, with researcher_revision_instructions() + the verdict
else:
    → on to the writer
```

`max_rounds` is `PPN_MAX_SOURCE_ROUNDS`, default 2.

Two design choices worth noting:

**A failing verdict does not block the pipeline.** Once the budget is spent, the
run continues — but the Writer receives an `<unresolved_source_issues>` block
naming exactly what could not be verified, and the caveats propagate into the
prose. A post that says "as of July 2026, in my tenant" is more useful than no post.

**The revision is surgical.** `researcher_revision_instructions()` tells the
Researcher to fix only what was flagged: replace fabricated or unreachable URLs,
add corroborating official sources for the named claims, remove or downgrade
unsupportable claims — and *not* to expand scope. Then return the complete
corrected dossier.

---

## The outline stage

The stage that decides what the post argues, before any of it exists.

It was added because the crew could pass every rule it had and still produce a
survey. One real draft scored **93.5, APPROVED, zero blockers** with eleven
sections averaging 170 words, four of them (40% of the body) about a product that
was not its subject. Honesty passed, voice passed, structure passed, and nothing
in the system was asking whether the post was about one thing. The Writer had been
handed the full dossier and a mandate to fill eight to twelve sections, which is
a recipe for finding eight to twelve subjects.

### `outliner`

Reasoning tier, bound to `PostOutline`, and **no tools at all**. Every fact it may
use is already in the dossier and has already been through the Source Checker; a
search tool here would invite it to widen scope at exactly the stage whose purpose
is to narrow it.

It produces a `thesis` (one sentence, stated so a reader could disagree), a
`reader_promise`, a list of `sections` each making one point, and — the field that
does the real work — `out_of_scope`: the subjects the research supports that this
post deliberately leaves out.

It returns **claim ids, never claim text**. This is the newsletter editor's
contract, for the same reason: the model is asked *which* research to build on,
and code resolves the answer, so a section cannot rest on research that does not
exist.

Where the operator supplied instructions on launch, those outrank the topic's own
angle. A brief that says "focus on the licensing impact" is a *scope* instruction,
and scope is decided here.

### `outline_gate` — and why it does not loop

`OutlineGate` checks the plan against the dossier in Python: unknown claim ids,
sections with nothing behind them, a section count over the cap, an empty
out-of-scope list, a word budget outside the band. Then it writes the outline to
`research/<date>-<slug>.outline.json` **before** sending anything on, the same
doctrine as `DossierGate`.

**It has no round counter and no edge back to the outliner.** That is deliberate.
`source_round` and `revision_round` are the only two counters in the system, and a
third would need an env var, a bound, a documented exhaustion behaviour and a
third exception to an invariant that currently holds exactly twice. It earns none
of that, because a loop is for work a model must *redo*, and every failure here
has one obviously correct deterministic repair:

| Problem | Repair |
|---|---|
| a claim id the dossier does not have | drop the id; drop the section if it is left with nothing |
| more sections than the cap | truncate the free middle, never the critical or closing section |
| `out_of_scope` empty | derive it from the claims the outline did not select — that *is* the material the post is not covering |
| word budget off the band | rescale proportionally, then clamp each section to the per-section bounds |
| no thesis | fall back to the topic's angle, then to the dossier summary |
| **too few sections** | the one irreparable case. Pass through with a warning and let S02 and F04 raise it in the revision loop, which is already bounded |

Every repair is recorded in `outline.warnings`, which lands in the review report
under **Thesis and scope**, so the human sees what was wrong with the plan.

The repair logic lives in a pure `repair_outline()` at module level rather than
inside the executor, so it is testable without building a workflow.

### What the Writer now sees

`_scoped_dossier()` narrows the Writer's view to the claims the outline selected,
plus the citations those claims reference. `suggested_outline` is dropped outright
— the Outliner has already read it and superseded it, and shipping both invites
the Writer to follow the wrong plan. That field had been dead config until now:
produced by the Researcher and read by nobody.

`examples`, `gotchas`, `limits` and `licensing_notes` stay whole. They are free
strings with no claim id to filter on, and they are what V12 and V13 (the
specificity floor, a **blocker**) are satisfied from. Trading a focus win for a
specificity blocker is a bad trade.

This does not violate *nothing downstream of research may destroy research*.
`DossierGate` already wrote the full dossier to disk, the full dossier rides in
the `PostPackage`, and `_scoped_dossier` returns a plain `dict` rather than a
`ResearchDossier` precisely so a truncated one can never be persisted by mistake.
What narrows is one agent's view, not the record.

The Writer is also handed an `<omitted_research>` block naming the claims that
were cut. Telling it what was left out works better than silently withholding it:
an unexplained gap invites it to fill the gap from memory.

---

## The revision loop

### `writer`

The Writer has almost no tools — `search_existing_posts` and `today`. It writes
from the approved outline, the claims that outline selected, and the author
claims, and from nothing else. Everything it might have looked up has already been
fetched, verified, structured and scoped.

Its message carries the `thesis` and the `<approved_outline>`, the run's
`voice_mode`, a word-target band and the author claims. The outline's sections are
the post's sections, with those titles, in that order: no additions, no
reordering. Where it thinks a section is wrong it writes the post anyway and says
so in `changelog`. Nothing on the out-of-scope list gets a heading. In `field_report` mode it may use first person, numbers and failures, but
only where a claim backs them. In `analysis` mode there is no first person at all,
and the word target drops to the lower end of the band.

It enforces one fixed post shape, driven entirely by
`blog_profile.yaml → structure`:

1. One `# H1`, 45–65 characters, containing the primary keyword.
2. Exactly `opening_paragraphs` (2) opening paragraphs — problem, then payoff. No
   TL;DR block.
3. `## Contents` — bullet list of anchor links, one per following H2, in order.
4. Between `min_sections` and `max_sections` (5 to 7) `##` sections, each 250 to
   450 words. **Never H3.** Generic headings (`banned_headings`) are rejected.
5. A mandatory penultimate section — `critical_section_heading`, default *"What to
   watch carefully"* — covering real risks, maturity, availability, and what can
   break.
6. A closing section from `closing_headings` — *My take* (preferred) or
   *Conclusion* for a pure news post — an opinion and a recommendation, not a recap.
7. `## Sources` — markdown links, document title only, no dates, no numbering.

**No in-body images and no dashes.** The body carries no images of any kind (rule
S11) and no dash characters (rule T01). **No inline citations anywhere** either —
that puts the whole burden of factual integrity on the dossier, the author claims
and the validators. Every factual statement must trace to a dossier claim or a
claim; dossier caveats must survive into prose.

On revision it must address every blocker and major finding *by id*, bump
`revision`, and summarise what changed in `changelog`. Where it disagrees with a
validator, it records the disagreement in the changelog rather than arguing in the
body. Minor findings come through too, in a subordinate block capped at five per
validator — they used to be filtered out entirely, so a validator could write a
precise fix, deduct points for it, and have the Writer never see it.

**The revision message restates the thesis and the outline every round, and
resends the draft.** Without that, three rewrites happen steered only by style
findings with no statement anywhere of what the post is meant to argue, which is
how a draft ends up further from its brief the more it is revised. Resending the
draft also makes the prompt self-contained: it previously arrived only through the
`AgentSession` history, which made the instruction unreadable in a log and put the
rewrite at the mercy of how much of a growing thread the model still attended to.

### `draft_gate`

Parses the `Draft`, fills `word_count` and `read_minutes` if the model left them
blank (`reading_speed_wpm`, default 200), then **runs the code-side detectors** and
fans out to both validators — with a *different* payload for each.

`run_detectors()` in `detectors.py` compiles the 22 detector regexes and runs them
over the draft before any model call. `auto: true` rules are decided in code; their
hits become pre-computed `RuleFinding`s the validator is told to include rather than
re-derive. It also computes the `measurements` a model must never estimate: average
sentence length, per-section word counts, longest paragraph, H2 count, body word
count, placeholder count, dash hits, banned-word hits. The T01/T02 detectors mask
fenced code, inline code, URLs and list bullets first, so a hyphen in `low-code` or
a URL is never flagged. The code findings are merged back into the reports at
`review_gate`, so a blocker a regex raised gates the run exactly like a model
blocker.

#### `auto: true` means the code decides it, and that is now checked

Some rules need a configured number rather than a pattern: the section count
against `min_sections`/`max_sections`, the word count against the format's band,
the focus rules against the outline. Those live in `detectors.COMPUTED_RULES` and
are decided in `_computed_findings()`.

This exists because the alternative failed silently for two rulesets. `rules_text`
stamps `[auto]` on any rule with `auto: true`, the validator prompt says to add
findings only for rules *without* that tag, and `run_detectors` skips any rule
carrying no `detector`. **A rule marked `auto` with nothing behind it was therefore
checked by nobody** — no error, no warning, just a rule that read like policy and
executed as nothing. Twelve rules were in that state, including S02 (section count)
and C04 (word count): the two that would have caught the eleven-section draft that
scored 93.5.

Ten of those rules are now `auto: false` and judged by the validator that always
should have owned them. S02 and C04 keep `auto: true` and have code behind them. A
test asserts every `auto` rule carries a detector or sits in `COMPUTED_RULES`, so
the hole cannot reopen.

### The two validators

They run in parallel and judge different families, on purpose. One validator asked
to check both facts and formatting does neither well.

**Content Validator** (`rules_text(groups=("honesty", "voice", "content", "focus"))`,
temperature 0.2) is the blog's hard-to-please editor. It receives the draft, the
dossier, the author claims *and the approved outline* — the anti-hallucination backstop, since the
published post carries no inline citations. Its hardest rules:

- **H01** — any statement not traceable to a dossier claim is a **blocker**, quoted
  verbatim in `location`. "It is generally known" is never acceptable support.
- **H02 / H03** — every first-person sentence traces to an author claim, and every
  number, version and error string traces to the dossier or a claim.
- **H04** — any dropped dossier caveat is a **blocker**.
- The **V** family is where drafts read as machine-written: the specificity floor,
  the closing opinion, sentence-length variance, banned vocabulary.
- The **F** family is the one that exists because a draft can pass everything else
  and still be four posts stapled together. F01 (one thesis, not an enumeration of
  what the post "covers") and F02 (every section advances it) are blockers and are
  judged here; F03, F04 and F05 are decided in code against the outline. That last
  point is the whole argument for having an outline stage: it turns three
  judgements into three comparisons.

**Design Validator** (`groups=("typography", "structure", "seo")`) judges
typography, structure and readability. It gets neither the dossier nor the
outline: whether the post argues one thing is an editorial question, and one
validator handed everything does neither job well. Most of its rules are `auto` and already
found by the detectors (dashes, curly quotes, images, missing code languages,
generic headings, inline citations); it spends its judgement on the rest: TOC
entries compared one-by-one against the actual H2s; the critical-read section
penultimate and the closing section from `closing_headings`; **any in-body image a
blocker (S11)**; walls of text; a table where a comparison deserves one;
`cover_concept` a concrete visual scene rather than a restated title.

Every finding, from either validator, must carry a `fix` that is an executable
rewrite instruction. "Tighten this section" is not a fix; "delete the second
sentence and merge the third into the first" is.

### `review_gate`

```python
approved = avg_score >= pass_threshold and not (block_on_any_blocker and blockers)
```

`pass_threshold` (default 82) and `block_on_any_blocker` (default true) come from
`validation_rules.yaml → scoring`.

```python
if approved or self.state.revision_round >= max_rounds:
    → finalizer
else:
    self.state.revision_round += 1
    → back to the writer with every blocker and major, by id
```

`max_rounds` is `PPN_MAX_REVISION_ROUNDS`, default 3.

Again: exhausting the budget **finalises anyway**. You get the draft, the report
tells you exactly what is still wrong, and you decide. The alternative — a pipeline
that produces nothing after 40 minutes because it could not reach 82 — is worse.

A report that fails to parse is replaced by a synthetic `ValidationReport(score=0,
passed=False)` rather than crashing the run.

### `finalizer`

1. Writes `drafts/<date>-<slug>.md` (front matter + body, including the `thesis`)
   and `drafts/<date>-<slug>.review.md`, whose **Thesis and scope** block sits
   directly under the verdict: the thesis, the out-of-scope list, the planned
   sections and any repair the outline gate had to make. "Did this post stay on
   the argument it was commissioned to make" should be answerable in ten seconds
   without opening the draft.
2. Generates the cover, if enabled.
3. Pushes to WordPress, if enabled. **Push failures are caught and logged, never
   raised** — a WordPress outage must not destroy a finished draft.
4. Decides on translation: skipped if disabled, and skipped if
   `only_when_approved` is set and the draft was not approved.
5. Either yields the `PostPackage` (done) or hands off to the Translator.

---

## Cover art

Not an agent — a single API call in `covers.py`, driven by the `cover_concept` the
Writer produced.

### The prompt

`build_prompt()` composes four parts from `blog_profile.yaml → cover`:

```
{art_direction}

Subject of the graphic: {draft.cover_concept}

Colour scheme: {palette[draft.category] or palette["default"]}.

Topic area: {draft.category}.

Absolutely do not include: {negative}
```

`art_direction` describes the house look: bold neon-lit digital graphic, electric
cyan/magenta/violet on near-black, luminous wireframes and circuitry, high contrast,
cinematic. `negative` forbids text, letters, numbers, captions, typography, logos,
watermarks, signatures, UI, labelled charts, recognisable people and company
branding.

That negative list is doing real work. Image models are eager to render text, and
generated text is always subtly wrong — misspelt, mis-kerned, meaningless. A
purely graphic cover has no such failure mode. The palette is per-category, so
Dataverse posts read emerald-and-cyan and Copilot Studio posts read purple-and-pink,
which gives the blog's index page a visual rhythm without anyone designing one.

### Three request shapes

The route is chosen automatically:

| `route` | When | Endpoint | Body |
|---|---|---|---|
| `mai` | `COVER_PROVIDER=mai`, or `foundry` + model starts `MAI-` | `{root}/mai/v1/images/generations` | `model`, `prompt`, `width`, `height` (ints) |
| `azure-openai` | `foundry` + any other model | `{root}/openai/v1/images/generations` | `model`, `prompt`, `size` (string), `n`, maybe `quality` |
| `openai` | `COVER_PROVIDER=openai` | `api.openai.com` | as above, with `OPENAI_API_KEY` |

MAI is **not** OpenAI-compatible, which is worth knowing before you debug a 400 for
an hour: it wants integer `width`/`height` rather than a `"1536x1024"` string, it
rejects `quality` and `n`, and it caps images at **1,048,576 total pixels** with a
768px minimum edge.

`fit_to_mai_limits()` handles the cap rather than making you compute it:

```python
if width * height > 1_048_576:
    scale = sqrt(1_048_576 / (width * height))
    width, height = int(width * scale), int(height * scale)
width  = max(768, width  // 16 * 16)     # round down to a multiple of 16
height = max(768, height // 16 * 16)
while width * height > 1_048_576 and max(width, height) > 768:
    # shave 16px off the longer side until it fits
```

So `COVER_SIZE=1536x1024` silently becomes `1248x832`, keeping the aspect ratio.

Auth: `COVER_API_KEY` if set, otherwise an Entra bearer token from the same
credential chain as the chat client — so `az login` covers it.

### The error contract

`build_cover()` **never raises**. Every failure lands in `CoverImage.error` and the
pipeline continues without a cover. It also pattern-matches the error and logs
something actionable: a 403 mentioning "verif" gets the Organization Verification
explanation; a 404 or `DeploymentNotFound` tells you to run `ppn models`; a 400 on
the MAI route explains the pixel cap.

Output: `drafts/covers/<slug>.png`.

---

## Publishing to WordPress

`WordPressClient` in `wordpress.py`, using REST API v2 with an Application
Password over HTTP Basic. No plugin required.

### Markdown → Gutenberg blocks

The body is converted into real Gutenberg block markup, so the post opens in the
block editor as headings, lists, tables, code blocks and quotes — not one lump of
classic HTML in a "Classic" block.

This is finicky, because **Gutenberg validates a block by re-running its `save()`
function and diffing the result against the stored markup.** Anything serialised
differently shows up in the editor as *"This block contains unexpected or invalid
content."*

The `core/code` block cost a real post to get right. Three separate mismatches:

| Mistake | Why it fails |
|---|---|
| `html.escape()` on the code | It turns `"` into `&quot;`; core leaves quotes alone. A JSON snippet was flagged on every line. |
| `<code class="language-json">` | Core emits a bare `<code>`. Any attribute is a diff. |
| Leaving `[` unescaped | Core writes `&#91;` so a snippet can never be parsed as a shortcode. |

Hence `escape_code()`, which escapes `&`, `<`, `>` and `[` and nothing else. The
language rides in the block delimiter instead —
`<!-- wp:code {"language":"json"} -->` — where core ignores it and the
Syntax-highlighting Code Block plugin reads it. `WP_CODE_LANGUAGE_ATTR=false`
turns that off.

There is **no image path** in the converter. This blog carries no in-body images
(rule S11 is a blocker on any), so there is no `![alt](IMAGE:slug)` handling, no
`[SCREENSHOT: ...]` normalisation and no empty `core/image` slot. The only image is
the cover, uploaded separately and set as `featured_media`.

`ppn wp preview <draft>` prints the block markup without publishing, which is the
fastest way to check a conversion change.

### Upsert by slug

`upsert_draft()` never creates duplicates:

1. Look up the slug in `.ppn_state/wp_posts.json` (a local `slug → post_id` map).
2. Failing that, `GET /posts?slug=…&status=draft,pending,publish,future,private`.
3. Found → `POST /posts/{id}` (update). Not found → `POST /posts` (create).
4. Remember the id.

So re-running `ppn wp push` on the same draft updates the post in place. That is
how you fix a published post after changing the converter.

Categories and tags are resolved to term ids by search-then-create: `GET
/categories?search=…`, exact case-insensitive name match, else `POST /categories`.
A 400 from a create race is recovered by reading `data.term_id` out of the error
body. Tags are capped at the first 8.

The cover is uploaded to `/media` with the alt text from `CoverImage`, then set as
`featured_media` on the post.

Default status is `draft` (`WP_DEFAULT_STATUS`). Nothing in this system publishes
anything.

---

## Translation

Opt-in per draft — `--translate` on `ppn write`, or `ppn translate <draft>` after
the fact. `TRANSLATE_ENABLED=true` makes it automatic for every approved draft.

The Translator's prompt is emphatic that it is **a translator, not an editor**:
nothing added, nothing removed, nothing improved. Specifically:

- Same section count, same order, headings translated — with the four fixed
  headings mapped to their configured Spanish labels (`Contenido`, `Lo que conviene
  observar con cautela`, `Conclusión`/`Mi lectura`, `Fuentes`).
- **Source URLs untouched.** Link titles are translated only if a localised version
  of that document actually exists.
- TOC anchors rebuilt from the translated headings, so they still resolve.
- Code, formulas, CLI and JSON copied **verbatim**. Comments inside code may be
  translated; executable content may not.
- Maker-facing terms stay in English, per `translation.keep_in_english`. The
  intended register is *"las elastic tables no soportan rollup columns"* — which is
  how people actually talk about the platform, and translating those terms makes
  the post harder to read, not easier.
- `tú`, never `usted`.
- **The dash ban applies to Spanish too**, and it matters more here because Spanish
  prose reaches for the raya (—) by default. `translation_gate` runs the T01, T02
  and T04 detectors over the Spanish output and logs any hit. First person is kept
  as first person, never rewritten to an impersonal form.
- `meta_description` rewritten to fit 140–158 characters in Spanish, not stretched.

The Spanish post gets the English slug plus `-es`, keeps the English category (the
taxonomy is not translated), and reuses the English cover artwork
(`translation.reuse_cover`).

`TranslationGate` degrades safely: if the translation fails to parse, it logs the
error and yields the English-only package. A translation failure never costs you
the English post.

---

## The tools agents can call

All in `tools.py`. Every one is `async`, and **none of them ever raise** — failures
come back as JSON `{"error": …, "message": …}` so the agent can reason about them
rather than the run dying.

| Tool | Calls | Notes |
|---|---|---|
| `web_search` | Tavily or Brave | Only when `SEARCH_PROVIDER` is one of those. Classifies each result's trust tier and drops blocked domains. |
| `fetch_page` | any URL | Extracts main content with `trafilatura`; guesses a publish date; prepends domain and trust tier. Cached in-process. |
| `check_url_reachable` | any URLs | Up to 30 concurrently. The anti-fabrication tool. |
| `read_feeds` | the feeds in `sources.yaml` | Filters by age and tier; a broken feed is skipped silently rather than failing the batch. |
| `search_microsoft_learn` | `learn.microsoft.com/api/search` | The authoritative source. Returns `last_updated`, which the Docs Scout uses. |
| `search_existing_posts` | your own WordPress | Duplicate detection and internal-link candidates. |
| `assess_source_trust` | nothing — local | Classifies URLs against `sources.yaml`. |
| `today` | nothing | Models are bad at knowing the date, and every freshness rule depends on it. |

### Trust tiers

`classify_domain()` scores a URL against `config/sources.yaml`:

| Tier | Score | Examples |
|---|---|---|
| `official` | 5 | learn.microsoft.com, devblogs.microsoft.com, github.com/microsoft |
| `standards` | 5 | ietf.org, w3.org, oauth.net |
| `community_trusted` | 4 | named MVP blogs |
| `vendor` | 3 | xrmtoolbox.com, kingswaysoft.com |
| `community_unverified` | 2 | reddit.com, stackoverflow.com |
| `blocked` | 0 | `blocked_domains` — hard fail if cited |
| `unknown` | 1 | anything unlisted |

A pattern containing `/` matches as a path-scoped substring, so `github.com/microsoft`
is official while the rest of GitHub is not.

### Where web search comes from

`SEARCH_PROVIDER=foundry` (the default) uses Azure AI Foundry's **hosted** web
search: no API key, no third-party account, no extra Azure resource. Microsoft
manages the Bing resource behind it and the search runs inside the service.
`_searchable()` strips the local `web_search` function from the tool list and
appends the hosted tool instead.

`tavily` and `brave` call those APIs from this process. `none` disables open-web
search entirely, leaving feeds and Microsoft Learn — which is a legitimate mode,
not a broken one.

---

## Where state lives

### During a run

One `RunState` dataclass, constructed once in `build_post_workflow()` and passed by
reference into every gate. It is *not* sent through the graph edges — the edges
carry messages, the state is shared:

```python
topic, dossier, draft, source_verdict, reports,
source_round, revision_round, dossier_path, package,
notes_text, author_claims, voice_mode, notes_path,   # author notes
code_findings, measurements, prev_finding_ids         # code-side detectors
```

Only two lines in the codebase increment a round counter, and each has exactly one
bound. That is the whole loop-safety story.

### On disk

```
topics/suggestions-<date>.json        the shortlist, machine-readable
topics/suggestions-<date>.md          the same, with the command to run each one
research/<date>-<slug>.json           the dossier — written before anything downstream can fail
drafts/<date>-<slug>.md               front matter + body
drafts/<date>-<slug>.review.md        both validators, rule by rule, plus the source verdict
drafts/<date>-<slug>.package.json     the entire run, serialised
drafts/covers/<slug>.png              cover art
.ppn_state/wp_posts.json              slug → WordPress post id
.ppn_state/ppn.db                     the server's SQLite database
```

Draft front matter carries `title`, `slug`, `description`, `primary_keyword`,
`category`, `tags`, `post_format`, `word_count`, `read_minutes`, `revision`,
`generated`, `status`, plus `review` (approved / score / blockers) and, for a
translation, `language` and `translation_of` — a breadcrumb that lets you pair the
two posts later with Polylang, WPML or hand-written hreflang.

### Configuration

Five documents — `blog_profile`, `topics`, `sources`, `validation_rules`,
`style_guide` — read through a swappable `ConfigSource`. The CLI reads them from
`config/*.yaml`; the server reads them from its database. `Settings` does not know
or care which, because both return the same shapes.

`Settings` caches parsed documents and invalidates the cache when the source's
`version_token()` changes — file mtimes for YAML, `name:version|…` for the
database. Edit a rule and the next run sees it with no restart.

---

## The server

`ppn serve` starts a FastAPI service. It exists so the pipeline can be driven from
a UI: queue several runs, watch each agent's output live, edit the config and the
editorial rules without touching a YAML file, review and publish drafts.

**Run queue.** One `asyncio.Queue` and N persistent workers, N =
`PPN_MAX_CONCURRENT_RUNS` (default 2). A run is enqueued with a UUID and the
current config version token, so you can always tell which rules produced a given
output. Cancelling a queued run marks it cancelled; cancelling a running one
actually cancels the task and frees its worker.

**Crash recovery.** On startup, any run still marked `queued` or `running` from a
previous process is marked `interrupted` — a hard crash otherwise leaves rows
claiming to be in flight forever.

**Events.** Every run has an append-only event log with per-run sequence numbers.
`emit()` assigns the seq, fans the event out to live SSE subscribers immediately,
and queues a durable write. A **single** background writer task owns all database
writes — an earlier design used one connection per event and leaked aiosqlite
worker threads when a task was cancelled mid-write, which wedged shutdown about
half the time. `flush()` drains that queue before a run is marked terminal, so a
run that reports `succeeded` always has a complete log on disk.

**SSE contract.** `GET /api/runs/{id}/events?after=N` replays everything with
`seq > N`, then follows live, then sends a synthetic `eof` frame when the run
reaches a terminal state. A browser that reconnects with its last seen seq gets
exactly what it missed — no gaps, no duplicates. Event kinds: `status`, `node`,
`log`, `eof`.

**Canvas.** `derive_nodes()` folds the same event log into per-executor state, so
the graph view and the log view can never disagree, and replaying a finished run
animates identically to watching it live. `GET /api/workflows` returns the mermaid
topology, built with stub clients so drawing a graph needs no Azure credentials.

**Config store.** Append-only versioning: every save inserts a new row rather than
updating one. YAML is validated before it is stored, so a bad edit is rejected at
the API (422) instead of breaking the next run. Rollback re-saves an old version as
a *new* version, keeping the rollback itself auditable. This is deliberately the
git history you gave up by moving config out of files.

Full endpoint reference in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Testing without Azure

`stub_clients()` in `testing.py` returns a `ClientBundle` wrapping a
`StubChatClient` that inspects the requested `response_format` and returns a
schema-valid canned instance of that model. No network, no credentials.

The important detail: `exercise_loops=True` (the default) makes the stub **fail the
first round of every gated check** — the first source verdict does not pass, the
first validation round carries a blocker. A dry run therefore walks the source loop
and the revision loop rather than gliding down the happy path. A dry run that only
tests the happy path is not worth running.

The stub implements both streaming and non-streaming paths, because the CLI streams
for progress display and an earlier version only stubbed the non-streaming path —
which meant `--dry-run` exercised different code than a real run.

```bash
pytest                     # 31 tests, ~6 seconds, fully offline
ruff check src tests
```

`tests/test_pipeline.py` runs both real workflow graphs end to end against the
stub. `tests/test_server.py` runs real HTTP against the app with a real queue and
real SSE — including deterministic queue and cancellation tests, which use a
`controllable_dispatch` fixture that holds a job open on an `asyncio.Event` rather
than hoping to observe a millisecond-long stub run at the right moment.
