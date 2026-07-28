# Getting started

From an empty machine to a WordPress draft written by the crew. Roughly 30 minutes,
most of it waiting for Azure to finish deploying things.

Everything below is a real command. Where a step needs a value from a portal, the
step says where in the portal it is.

---

## 0. What you are building

Ten agents, two workflows, one output: an unpublished WordPress draft plus a review
report. Nothing here publishes anything. The crew's job ends at a draft.

You need three external things:

| Thing | Why | Cost |
|---|---|---|
| An Azure AI Foundry project | Runs the models and the hosted web search | Pay-per-token |
| A WordPress site with REST API access | Where drafts land | You already have it |
| An image model deployment | Cover art | Pay-per-image, optional |

---

## 1. Local prerequisites

```bash
python3 --version     # must be 3.11 or newer
git --version
az version            # Azure CLI — install from https://aka.ms/azcli
```

Python 3.11 is a hard floor: the code uses `X | None` annotations in dataclass
fields with `slots=True`, and `tomllib`.

---

## 2. Get the code

```bash
git clone https://github.com/david-lorenzo88/powerplatformninja-blogger.git
cd powerplatformninja-blogger
```

A virtual environment is strongly recommended — this installs a `ppn` console
script and about 40 dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

`[dev]` pulls in the server extras and the test tooling. If you only ever want the
CLI, `pip install -e .` is enough; if you want the CLI plus the web UI but not the
tests, use `pip install -e ".[server]"`.

Check it landed:

```bash
ppn --help
```

---

## 3. Azure AI Foundry

### 3.1 Create the project

1. Go to <https://ai.azure.com>.
2. **Create new** → **AI Foundry resource**. Pick a region that has the models you
   want — `swedencentral`, `eastus2` and `westus3` have the widest coverage.
3. Inside the resource, create a **project**. The project is what you point the
   code at, not the resource.

### 3.2 Deploy the models

In the project, go to **Models + endpoints** → **Deploy model**.

Deploy at minimum:

| Deployment | Used for | Notes |
|---|---|---|
| `gpt-5` | Researcher, Source Checker, Writer, both Validators, Translator | Any strong reasoning model works |
| `gpt-5-mini` | The three scouts | Optional — falls back to the main model |
| `MAI-Image-2.5-Pro` | Cover art | Optional — set `COVER_ENABLED=false` to skip |

The scout tier exists because the scouts make many cheap calls and the quality bar
is low: find URLs, don't reason about them. Splitting the tier cut roughly 60% of
the token spend on a discovery run.

> **On image models.** `gpt-image-1` and its variants need limited-access approval
> before they appear in the deploy list. `MAI-Image-2.5-Pro` and `gpt-image-2` do
> not. `dall-e-3` was retired in March 2026 — existing deployments are dead.

### 3.3 Copy the project endpoint

**Overview** → **Project details** → **Endpoints and keys**. You want the one that
looks like:

```
https://<resource>.services.ai.azure.com/api/projects/<project>
```

Not the resource-level endpoint. The `/api/projects/<project>` suffix matters.

### 3.4 Grant yourself a role

Being the subscription owner is not enough — the data plane has its own roles. In
the **AI Foundry resource** (not the project), go to **Access control (IAM)** →
**Add role assignment** and give your own user:

- **Azure AI User** — to call models
- **Cognitive Services OpenAI User** — to call the image endpoint

Role assignments take a couple of minutes to propagate. If your first run fails
with a 401 or 403, this is usually why; wait five minutes and retry before
debugging anything else.

### 3.5 Sign in

```bash
az login
az account set --subscription "<your subscription name>"
```

The default credential mode is `cli`, which uses this session. On a server, set
`AZURE_CREDENTIAL_MODE=default` and use a managed identity instead.

---

## 4. WordPress

### 4.1 Create an Application Password

1. WP Admin → **Users** → **Profile**.
2. Scroll to **Application Passwords**.
3. Name it `ppn-blogger`, click **Add New Application Password**.
4. Copy the generated value. It is shown once.

You can paste it with or without the spaces WordPress displays — the client strips
them.

If the section is missing, your site is not served over HTTPS, or a security plugin
has disabled the feature. Both are fixable in the plugin's settings; Application
Passwords are core WordPress, not a plugin.

### 4.2 Check the user's role

The account needs to create posts, create categories and tags, and upload media.
**Author** is not enough (it cannot create terms). Use **Editor** or
**Administrator**.

---

## 5. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in the five values that have no sensible default:

```bash
FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
FOUNDRY_MODEL=gpt-5
FOUNDRY_MODEL_FAST=gpt-5-mini

WP_URL=https://powerplatformninja.com
WP_USERNAME=<your wp login, not the display name>
WP_APP_PASSWORD=<the application password from 4.1>
```

Everything else in `.env.example` has a working default and is documented inline.
[CONFIGURATION.md](CONFIGURATION.md) is the full reference.

`.env` is gitignored. It should stay that way.

---

## 6. Verify before you spend anything

Two commands, in this order. They exist because both of their failure modes have
already cost a real run.

```bash
ppn doctor
```

Checks configuration and makes one live call to WordPress. Everything should read
green; a red WordPress line means the credentials or the URL are wrong, and no
amount of Azure being correct will save the run.

```bash
ppn preflight
```

Makes two small real calls to your Foundry model to establish what request shapes
it accepts — specifically whether it tolerates a `temperature` parameter.

This matters more than it sounds. Reasoning models (`gpt-5`, `o1`, `o3`, `o4`)
reject `temperature` with an HTTP 400. The code infers support from the model name,
but the inference is a heuristic and a wrong guess kills the run *at the writer
stage* — six minutes and a completed research dossier in. `preflight` finds out in
two seconds. If it disagrees with the inference, it tells you to pin
`FOUNDRY_TEMPERATURE_SUPPORT=true` or `=false`.

Optionally, check what image models you actually have:

```bash
ppn models
```

---

## 7. A dry run first

```bash
ppn suggest --dry-run
```

No network, no credentials, no cost. An offline stub client returns schema-valid
canned objects, so this exercises the real workflow graph — the fan-out, the
fan-in, the parsing, the file writing — and proves the plumbing works before you
pay for tokens.

```bash
ppn write --dry-run
```

Same for the writing pipeline. The stub deliberately fails the first source check
and the first validation round, so a dry run walks both loops rather than taking
the happy path.

---

## 8. The real thing

### 8.1 Find something to write about

```bash
ppn suggest
```

Three scouts run concurrently — one on open web search, one on the curated RSS
feeds in `config/sources.yaml`, one on Microsoft Learn. The Topic Editor merges
their findings, checks each idea against posts already on your blog, and returns a
ranked shortlist.

**This takes 10–20 minutes.** That is normal. The scouts fetch every page they
intend to cite, and fetching is slow. The timeout is 40 minutes and exists to break
a genuine hang, not to cut short honest work.

Output lands in `topics/suggestions-<date>.json` plus a readable `.md` alongside it.

### 8.2 Write one

```bash
ppn write --index 1
```

`--index` is 1-based against the shortlist you just generated. The full pipeline
runs: research → adversarial source check → draft → two validators in parallel →
cover art → WordPress push.

**Budget 20–60 minutes.** The revision loop is the variable: a draft that clears
the validators first time finishes fast, one that needs three rounds does not.

You get:

```
drafts/2026-07-28-<slug>.md              the draft, with front matter
drafts/2026-07-28-<slug>.review.md       what the validators said, rule by rule
drafts/2026-07-28-<slug>.package.json    the complete run, including the dossier
drafts/covers/<slug>.png                 the cover art
research/2026-07-28-<slug>.json          the research dossier
```

…and a draft post in WordPress, with the cover set as the featured image, the
category and tags created if they did not exist, and the body as real Gutenberg
blocks rather than a lump of HTML.

The CLI prints the edit link. Open it, read it, fix what you disagree with, press
Publish yourself.

### 8.3 Optionally, translate it

```bash
ppn translate drafts/2026-07-28-<slug>.md --push
```

Opt-in per draft. The Translator is instructed to translate, not to edit — same
structure, same section count, same URLs, code blocks byte-identical. Maker-facing
terms stay in English, because that is how people actually speak about the
platform. The Spanish post gets its own slug with a `-es` suffix and reuses the
English cover.

---

## 9. The web UI (optional)

```bash
ppn serve
```

Starts a FastAPI service on <http://127.0.0.1:8000>. On first start it imports
`config/*.yaml` into a SQLite database at `.ppn_state/ppn.db`, and from that point
the database is authoritative — edits made through the API are versioned and
never touch the YAML files again.

The API gives you a run queue with a concurrency cap, live SSE event streams per
run, a versioned config store with rollback, and draft read/edit/publish endpoints.
[ARCHITECTURE.md](ARCHITECTURE.md) has the contract.

---

## 10. When something goes wrong

[OPERATIONS.md](OPERATIONS.md) covers the failures that have actually happened
here — the `temperature` 400, a run that dies after research succeeds, Gutenberg
rejecting code blocks, cover generation returning 400 — and what to do about each.

The single most useful habit: **a failed run after the research stage does not have
to pay for research twice.** The dossier is written to `research/` the moment the
Researcher finishes, before anything downstream can fail. Resume with:

```bash
ppn write --dossier research/2026-07-28-<slug>.json --index 1
```
