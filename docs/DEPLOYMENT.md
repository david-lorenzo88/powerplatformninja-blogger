# Deployment — hosting the crew on Azure

This runbook takes the app from *runs on my laptop* to *runs on Azure and I
manage it from a browser*. It targets **Azure Container Apps**, protects the
management UI with **Entra ID (Easy Auth)**, keeps state in **PostgreSQL** plus
an **Azure Files** share, and provisions everything with **Bicep**.

Nothing here changes application code. The image is built from the repo as-is;
every Azure-specific choice is an environment variable or an infrastructure
setting. Three small optional code changes are listed at the [end](#optional-code-follow-ups).

> **Live instance (2026-07-28).** This was deployed and is running — see
> [Appendix A](#appendix-a--the-live-deployment). Because this subscription is
> **offer-restricted** from PostgreSQL in every region, the database is **Azure
> SQL Database** (serverless, in `centralus`) reached via `mssql+aioodbc`, and the
> image carries the **Microsoft ODBC Driver 18**. `infra/main.bicep` provisions
> exactly that (Azure SQL); prose below that still says "Postgres" describes the
> original template intent — the appendix and the Bicep are the source of truth.

> **Why these choices.** The service is a single stateful process by design — an
> in-process `asyncio` run queue, in-memory SSE fan-out, and a startup sweep that
> marks orphaned runs `interrupted` (`server/runs.py`). It cannot be scaled out
> without the Storage-Queue refactor the code anticipates. So the deployment pins
> **one replica**, gives it a **real database** and **persistent disk** so a
> restart loses nothing, and puts a **login** in front so the blog and your Azure
> spend are not exposed. See [Known limitations](#known-limitations).

---

## 0. Architecture

```
Browser ──HTTPS──▶ Container Apps ingress
                     │  Easy Auth (Entra) — login required, restricted to you
                     ▼
              Container App  (exactly 1 replica, listens on :8000)
              image built in ACR: node stage builds ui/dist → python stage
              user-assigned managed identity ─┬─▶ Azure AI Foundry  (existing)
                     │                         │     Azure AI User
                     │   Azure Files volume    │     Cognitive Services OpenAI User
                     │   /data  +  /app/.ppn_state
                     │                         ├─▶ ACR              (AcrPull)
                     ▼                         └─▶ Key Vault (opt.) (Secrets User)
              Azure Database for PostgreSQL Flexible Server
                     └────────▶ WordPress REST (public)  via WP_APP_PASSWORD
```

Resources created: a resource group, **Azure Container Registry**, **Log
Analytics**, a **Container Apps environment** with an **Azure Files** storage
link, a **user-assigned managed identity**, **PostgreSQL Flexible Server** +
database, and the **Container App** with its **auth config**. The **Foundry
resource already exists** — the deployment only adds role assignments to it.

---

## 1. Prerequisites

- **Azure CLI** ≥ 2.60 with extensions:
  ```bash
  az extension add --name containerapp --upgrade
  az extension add --name rdbms-connect --upgrade
  az provider register --namespace Microsoft.App
  az provider register --namespace Microsoft.OperationalInsights
  az provider register --namespace Microsoft.DBforPostgreSQL
  ```
- An existing **Azure AI Foundry** resource + project, with the model
  deployments you use: `gpt-5` (required), and optionally `gpt-5-mini` (scouts)
  and `MAI-Image-2.5-Pro` (covers). You need its **project endpoint** —
  `https://<resource>.services.ai.azure.com/api/projects/<project>` — and the
  **resource id** of the underlying AI Services account.
- A **WordPress** account on the target site with the **Editor** or
  **Administrator** role (Author cannot create categories), and an **Application
  Password** (WP Admin → Users → Profile → Application Passwords).
- Permission to create resources and **assign roles** (Owner or User Access
  Administrator on the subscription/resource group, plus rights on the Foundry
  resource).

> The local-box quirks noted in `docs/STATUS.md` — `ppn`/`ruff` not on `PATH`,
> the root-owned `~/.npm` cache — **do not apply**: the image is built in a clean
> container by `az acr build`, so no local Docker, Node, or Python is needed.

---

## 2. The container image

Save these two files at the repo root.

### `Dockerfile`

```dockerfile
# ---- stage 1: build the React SPA into ui/dist -----------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm ci
COPY ui/ ./
RUN npm run build            # → /ui/dist

# ---- stage 2: python runtime ----------------------------------------------
FROM python:3.11-slim AS app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
# Copy the whole repo. settings.py resolves ROOT = parents[2] of the package,
# so the app MUST run from the source tree — hence the editable install below.
COPY . /app
# Bring in the built SPA where app.py expects it (ROOT/ui/dist).
COPY --from=ui /ui/dist /app/ui/dist

# ".[server]" pulls fastapi/uvicorn/sqlalchemy/aiosqlite; asyncpg is the Postgres
# driver and is deliberately NOT in pyproject (see the optional follow-up).
RUN pip install -e ".[server]" asyncpg

# Persisted at runtime via an Azure Files mount; create so first boot succeeds.
RUN mkdir -p /data/drafts /data/research /data/topics /app/.ppn_state \
 && useradd -m app && chown -R app:app /app /data
USER app

EXPOSE 8000
CMD ["ppn", "serve", "--host", "0.0.0.0", "--port", "8000"]
```

### `.dockerignore`

```gitignore
.git
.venv
venv
**/__pycache__
.pytest_cache
.ruff_cache
ui/node_modules
ui/dist
.ppn_state
drafts/*
research/*
topics/*
.env
*.db
```

Why it is shaped this way:

- **Multi-stage.** `ui/dist` is gitignored, so the SPA is built in the image and
  copied to `ROOT/ui/dist`, exactly where `server/app.py` mounts it for
  single-process serving.
- **Editable install + full tree.** `ROOT = Path(__file__).resolve().parents[2]`
  anchors `config/`, `ui/dist`, and the data dirs. A normal site-packages install
  would move `ROOT` into site-packages and break all of them.
- **`--host 0.0.0.0`.** `ppn serve` defaults to `127.0.0.1`, which a container
  ingress cannot reach.
- **`asyncpg`.** Required by `postgresql+asyncpg://`; not currently a dependency.

---

## 3. Provision — variables and registry

Set shell variables (adjust names/region/password):

```bash
# --- identifiers ---
export LOC=westeurope
export RG=ppn-blogger-rg
export ACR=ppnblogger$RANDOM          # must be globally unique, lowercase
export APP=ppn-blogger
export ENVNAME=ppn-env
export IMAGE_TAG=v1

# --- existing Foundry ---
export FOUNDRY_PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"
export FOUNDRY_RESOURCE_ID="/subscriptions/<sub>/resourceGroups/<fdy-rg>/providers/Microsoft.CognitiveServices/accounts/<account>"

# --- WordPress ---
export WP_URL="https://powerplatformninja.com"
export WP_USERNAME="<wp-user>"
export WP_APP_PASSWORD="<application password>"

# --- Postgres admin ---
export PG_ADMIN=ppnadmin
export PG_PASSWORD="$(openssl rand -base64 24)"   # save this somewhere safe
```

Create the resource group and the registry, then **build the image in the cloud**
(no local Docker):

```bash
az group create -n "$RG" -l "$LOC"

az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled false

az acr build -r "$ACR" -t "ppn-blogger:$IMAGE_TAG" .
```

`az acr build` uploads the build context and runs the Dockerfile on ACR Tasks;
the resulting image is `"$ACR".azurecr.io/ppn-blogger:$IMAGE_TAG`.

---

## 4. Provision — the Bicep template

Save as `infra/main.bicep`. It creates everything except the pre-existing Foundry
resource and the registry (created above so the image can exist before the app
that pulls it).

```bicep
@description('Location for all resources')
param location string = resourceGroup().location
param appName string = 'ppn-blogger'
param envName string = 'ppn-env'

@description('Existing ACR name and the image tag built into it')
param acrName string
param imageTag string = 'v1'

@description('Foundry project endpoint (…/api/projects/<project>)')
param foundryProjectEndpoint string

param wpUrl string
param wpUsername string
@secure()
param wpAppPassword string

param pgAdmin string
@secure()
param pgPassword string

var pgServerName = '${appName}-pg-${uniqueString(resourceGroup().id)}'
var storageName  = toLower('ppnfiles${uniqueString(resourceGroup().id)}')
var shareName    = 'ppn-data'

// ---- identity the app runs as -------------------------------------------
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-id'
  location: location
}

// ---- observability -------------------------------------------------------
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

// ---- existing registry (for AcrPull) ------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

// ---- persistent files ----------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
}
resource fileSvc 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}
resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileSvc
  name: shareName
  properties: { shareQuota: 16 }
}

// ---- PostgreSQL ----------------------------------------------------------
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: pgServerName
  location: location
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: pgAdmin
    administratorLoginPassword: pgPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7 }
    highAvailability: { mode: 'Disabled' }
  }
}
resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = {
  parent: pg
  name: 'ppn'
}
// Let Container Apps (and other Azure services) reach the server.
resource pgFw 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-06-01-preview' = {
  parent: pg
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

// ---- Container Apps environment + files link ----------------------------
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}
resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'ppndata'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: shareName
      accessMode: 'ReadWrite'
    }
  }
}

// ---- AcrPull for the identity -------------------------------------------
var acrPullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, 'AcrPull')
  properties: {
    roleDefinitionId: acrPullRole
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---- the app ------------------------------------------------------------
resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        // one replica → session affinity is unnecessary, but harmless
        stickySessions: { affinity: 'sticky' }
      }
      registries: [
        { server: '${acrName}.azurecr.io', identity: uami.id }
      ]
      secrets: [
        { name: 'wp-app-password', value: wpAppPassword }
        { name: 'pg-url', value: 'postgresql+asyncpg://${pgAdmin}:${pgPassword}@${pg.properties.fullyQualifiedDomainName}:5432/ppn?ssl=require' }
      ]
    }
    template: {
      scale: { minReplicas: 1, maxReplicas: 1 }   // single-instance: DO NOT raise
      volumes: [
        { name: 'data', storageType: 'AzureFile', storageName: 'ppndata' }
      ]
      containers: [
        {
          name: appName
          image: '${acrName}.azurecr.io/ppn-blogger:${imageTag}'
          resources: { cpu: json('1.0'), memory: '2Gi' }
          volumeMounts: [
            { volumeName: 'data', mountPath: '/data' }
            { volumeName: 'data', mountPath: '/app/.ppn_state', subPath: 'ppn_state' }
          ]
          env: [
            { name: 'AZURE_CREDENTIAL_MODE', value: 'default' }
            { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
            { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
            { name: 'FOUNDRY_MODEL', value: 'gpt-5' }
            { name: 'FOUNDRY_MODEL_FAST', value: 'gpt-5-mini' }
            { name: 'WP_URL', value: wpUrl }
            { name: 'WP_USERNAME', value: wpUsername }
            { name: 'WP_APP_PASSWORD', secretRef: 'wp-app-password' }
            { name: 'PPN_DATABASE_URL', secretRef: 'pg-url' }
            { name: 'PPN_OUTPUT_DIR', value: '/data/drafts' }
            { name: 'PPN_RESEARCH_DIR', value: '/data/research' }
            { name: 'PPN_TOPICS_DIR', value: '/data/topics' }
            { name: 'PPN_MAX_CONCURRENT_RUNS', value: '2' }
            { name: 'PPN_LOG_LEVEL', value: 'INFO' }
          ]
          probes: [
            { type: 'Liveness',  httpGet: { path: '/api/health', port: 8000 }, initialDelaySeconds: 20, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/api/health', port: 8000 }, initialDelaySeconds: 10, periodSeconds: 15 }
          ]
        }
      ]
    }
  }
}

output appFqdn string = app.properties.configuration.ingress.fqdn
output principalId string = uami.properties.principalId
output clientId string = uami.properties.clientId
```

> **`PPN_DATABASE_URL` note.** Azure PostgreSQL Flexible Server enforces TLS, so
> the URL carries `?ssl=require` — the app builds the engine straight from this
> string (no custom `connect_args` for non-SQLite in `db.py`), so SSL must be set
> here. If your asyncpg/SQLAlchemy versions reject the literal `require`, `ssl=true`
> also forces TLS. `.ppn_state` is mounted from the same share under a `ppn_state`
> sub-path so `wp_posts.json` (the WordPress dedupe cache, whose path is not
> env-configurable) survives restarts. `PPN_CORS_ORIGINS` is omitted on purpose —
> the SPA is served same-origin, so no CORS entry is needed.

Deploy it:

```bash
az deployment group create -g "$RG" -f infra/main.bicep \
  --parameters \
    acrName="$ACR" imageTag="$IMAGE_TAG" \
    foundryProjectEndpoint="$FOUNDRY_PROJECT_ENDPOINT" \
    wpUrl="$WP_URL" wpUsername="$WP_USERNAME" wpAppPassword="$WP_APP_PASSWORD" \
    pgAdmin="$PG_ADMIN" pgPassword="$PG_PASSWORD"

export APP_FQDN=$(az deployment group show -g "$RG" -n main --query properties.outputs.appFqdn.value -o tsv)
export PRINCIPAL_ID=$(az deployment group show -g "$RG" -n main --query properties.outputs.principalId.value -o tsv)
echo "https://$APP_FQDN"
```

---

## 5. Grant the identity access to Foundry

These two **data-plane** roles are what let the app call models and the image
endpoint with no API key. They are on the *existing* Foundry resource, so they
are assigned by CLI rather than in the template:

```bash
az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Azure AI User" --scope "$FOUNDRY_RESOURCE_ID"

az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" --scope "$FOUNDRY_RESOURCE_ID"
```

> A 401/403 on the first run is almost always one of these two roles missing —
> `Azure AI User` covers chat models, `Cognitive Services OpenAI User` covers the
> cover-image endpoint. Because `AZURE_CREDENTIAL_MODE=default` and `AZURE_CLIENT_ID`
> point `DefaultAzureCredential` at the user-assigned identity, and `COVER_API_KEY`
> is left unset, both chat and images authenticate with the managed identity.
> Role propagation can take a few minutes; restart the revision if the first run
> 403s right after assignment.

---

## 6. Lock the front door — Entra Easy Auth

Until this step the URL is open to anyone. Add the Microsoft identity provider,
require authentication, then restrict it to your own account.

**Portal path (recommended — it creates the app registration for you):**

1. Portal → your Container App → **Settings → Authentication → Add identity
   provider**.
2. Provider **Microsoft**; **Create new app registration**; *Supported account
   types* **Current tenant — Single tenant**.
3. *Restrict access* = **Require authentication**; *Unauthenticated requests* =
   **HTTP 302 Found redirect** (so a browser is sent to login). **Add**.

**CLI equivalent** (if you prefer scripting — register the app, then wire it up):

```bash
export TENANT=$(az account show --query tenantId -o tsv)
export APPREG=$(az ad app create --display-name "$APP-auth" \
  --web-redirect-uris "https://$APP_FQDN/.auth/login/aad/callback" \
  --query appId -o tsv)

az containerapp auth microsoft update -g "$RG" -n "$APP" \
  --client-id "$APPREG" \
  --issuer "https://login.microsoftonline.com/$TENANT/v2.0"

az containerapp auth update -g "$RG" -n "$APP" \
  --unauthenticated-client-action RedirectToLoginPage \
  --enabled true
```

**Then restrict to only you** (otherwise anyone in the tenant can sign in):

- **Entra ID → Enterprise applications →** the app just created → **Properties**:
  set **Assignment required = Yes**.
- Under **Users and groups**, assign only your own account.

After this, hitting `https://$APP_FQDN` bounces to a Microsoft login and only your
account gets through. Easy Auth is entirely separate from the managed identity in
§5 — one is who *you* are, the other is who the *app* is.

---

## 7. Verify the deployment

Work top to bottom; each step proves one layer.

1. **Auth gate** — open `https://$APP_FQDN` in a private window: it must redirect
   to Microsoft login; a non-assigned account is refused; your account reaches the
   UI.
2. **Health & boot** — logs show a clean start, no credential errors:
   ```bash
   az containerapp logs show -g "$RG" -n "$APP" --follow
   ```
   Look for `ppn server ready`.
3. **Identity → Foundry** — from the **Runs** screen launch a **suggest** run. The
   canvas should light up over SSE and produce topic suggestions. (A 403 here =
   the §5 roles.)
4. **Persistence** — after a run produces a draft, confirm it on the **Drafts**
   screen, then restart the revision and confirm the draft, its review report, and
   the run history are still present:
   ```bash
   az containerapp revision restart -g "$RG" -n "$APP" \
     --revision $(az containerapp revision list -g "$RG" -n "$APP" --query "[0].name" -o tsv)
   ```
   Survival proves Postgres + Azure Files (not ephemeral disk).
5. **WordPress** — publish a draft from the Drafts screen (behind the confirm
   dialog) and check the unpublished draft appears in WordPress. Proves the
   `WP_APP_PASSWORD` secret and outbound reach.

---

## 8. Updating the app

Rebuild the image and roll a new revision:

```bash
export IMAGE_TAG=v2
az acr build -r "$ACR" -t "ppn-blogger:$IMAGE_TAG" .
az containerapp update -g "$RG" -n "$APP" \
  --image "$ACR.azurecr.io/ppn-blogger:$IMAGE_TAG"
```

Config-only changes (env/secret) can go through `az containerapp update
--set-env-vars …` / `--secrets …` without a rebuild.

---

## 8a. Continuous deployment with GitHub Actions

`.github/workflows/deploy.yml` automates §8: on every merge to `main` it runs the
offline test suite, then `az acr build` + `az containerapp update`, exactly the
manual path above. It authenticates to Azure with **OIDC** (a federated credential,
no stored secret) and **never runs the Bicep** — infrastructure stays a deliberate,
reviewed `az deployment` (re-running the template would mint a second SQL server, as
Appendix A warns).

### One-time Azure setup (you run this once)

Create an app registration for the workflow, trust this repo's `main` branch, and
grant it rights on the app's resource group:

```bash
export SUB=$(az account show --query id -o tsv)
export TENANT=$(az account show --query tenantId -o tsv)
export GH_REPO="david-lorenzo88/powerplatformninja-blogger"   # owner/repo

# App registration + service principal
export CI_APP_ID=$(az ad app create --display-name "ppn-blogger-github-actions" --query appId -o tsv)
az ad sp create --id "$CI_APP_ID"

# Federated credential: GitHub OIDC tokens from main are accepted with no secret.
az ad app federated-credential create --id "$CI_APP_ID" --parameters "{
  \"name\": \"ppn-main\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:${GH_REPO}:ref:refs/heads/main\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"

# Contributor on the app resource group covers both `az acr build` (the ACR lives
# in this RG) and `az containerapp update`.
az role assignment create --assignee "$CI_APP_ID" --role Contributor \
  --scope "/subscriptions/${SUB}/resourceGroups/ppn-blogger-rg"

echo "AZURE_CLIENT_ID       = $CI_APP_ID"
echo "AZURE_TENANT_ID       = $TENANT"
echo "AZURE_SUBSCRIPTION_ID = $SUB"
```

### GitHub configuration

Under **Settings → Secrets and variables → Actions**:

| Kind | Name | Value |
|---|---|---|
| **Secret** | `AZURE_CLIENT_ID` | the `CI_APP_ID` printed above |
| **Secret** | `AZURE_TENANT_ID` | your tenant id |
| **Secret** | `AZURE_SUBSCRIPTION_ID` | your subscription id |
| Secret *(optional)* | `PPN_ADMIN_TOKEN` | enables the post-deploy config reload — see below |
| Variable *(optional)* | `AZURE_RESOURCE_GROUP` | overrides the default `ppn-blogger-rg` |
| Variable *(optional)* | `AZURE_ACR_NAME` | overrides the default `ppnblogger286957664` |
| Variable *(optional)* | `AZURE_CONTAINER_APP` | overrides the default `ppn-blogger` |

The identifiers default to the live deployment, so the three secrets are all that is
strictly required. Nothing secret is committed — none of `WP_APP_PASSWORD`, the DB
URL or a registry password is needed, because the build runs in ACR and the app
authenticates to everything with its managed identity.

### What it does, and what it deliberately does not

- **Tags** each image with the commit SHA (immutable, so a rollback is
  `az containerapp update --image …:<old-sha>`) and also moves `latest`.
- **Health gate:** it polls the new revision's `runningState` rather than curling
  the URL, because Easy Auth 302-redirects every anonymous request whether the app
  is up or not.
- **Config reload:** after the new revision is running, it calls
  `POST /api/config/reload`, which re-imports the image's `config/` into the
  database as a new version of each document. The database is authoritative after
  first boot, so without this a new editorial ruleset would ship in the image but
  never reach the app. The step is opt-in (see below) and non-fatal — a failed
  reload never fails a deploy that already rolled the image.
- **`main` only, image only.** It does not touch Bicep, roles, or Easy Auth.

### Enabling the post-deploy config reload

The reload endpoint is machine-to-machine: it is guarded by a bearer token, not the
interactive Entra login, and is **disabled unless `PPN_ADMIN_TOKEN` is set**. Three
one-time steps:

```bash
# 1. Generate a strong token and set it on the running app (secret + env var).
export ADMIN_TOKEN=$(openssl rand -base64 32)
az containerapp secret set -g ppn-blogger-rg -n ppn-blogger \
  --secrets admin-token="$ADMIN_TOKEN"
az containerapp update -g ppn-blogger-rg -n ppn-blogger \
  --set-env-vars PPN_ADMIN_TOKEN=secretref:admin-token

# 2. Exclude ONLY this path from Easy Auth, so CI can reach it. The token is what
#    keeps it safe; nothing else under /api is exposed.
SUB=$(az account show --query id -o tsv)
az rest --method patch \
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/ppn-blogger-rg/providers/Microsoft.App/containerApps/ppn-blogger/authConfigs/current?api-version=2024-03-01" \
  --body '{"properties":{"globalValidation":{"excludedPaths":["/api/config/reload"]}}}'
```

```
# 3. Add PPN_ADMIN_TOKEN as a GitHub *secret* (same value as step 1).
```

With all three in place, every merge to `main` re-imports the new `config/` after
the image rolls. Leave any of them out and the step logs a warning and skips — the
image still deploys, and you can always run `ppn config reload` by hand.

**On demand:** run the workflow manually (Actions → Deploy to Azure → *Run
workflow*) with **`reload_only` = true** to re-import `config/` from the
already-deployed image without rebuilding or rolling it — handy if a reload was
skipped, or to re-apply config after editing it in place.

(For a *fresh* provision, `infra/main.bicep` takes an optional `adminToken`
parameter that wires the same secret and env var, so step 1 is handled by the
template. The Easy Auth exclusion in step 2 is still applied separately, as auth is
configured outside the template.)

> **If you add a manual-approval gate** (a GitHub *Environment* named e.g.
> `production` on the `deploy` job), the OIDC token's subject changes to
> `repo:${GH_REPO}:environment:production`. Add a second federated credential with
> that subject, or the login step will fail.

### Making the app installable behind Easy Auth

The UI ships as a PWA. Easy Auth is configured to answer every unauthenticated
request with a 302 to Microsoft, and that breaks installation in a way that
surfaces no error at all: the browser fetches `manifest.webmanifest`, follows the
redirect cross-origin, fails to parse HTML as JSON, and simply never offers to
install. Two fixes, and you want both.

**1. The manifest link already carries `crossorigin="use-credentials"`** (in
`ui/index.html`), which makes the browser send the `AppServiceAuthSession` cookie
with the manifest request. That is enough for the manifest itself.

**2. Exclude the manifest and the icons from Easy Auth.** Icon fetches do not
reliably inherit the manifest's credentials mode across engines, so without this
the app can install with a generic globe for a logo. Nothing here is secret — an
app name, two hex colours and a lightning bolt.

> **The PATCH replaces the array, it does not merge.** Read the current value
> first. Dropping `/api/config/reload` breaks CI's post-deploy config reload —
> and it fails *non-fatally*, so the deploy still goes green while the new
> editorial config silently never lands.

```bash
SUB=$(az account show --query id -o tsv)
AUTH="https://management.azure.com/subscriptions/$SUB/resourceGroups/ppn-blogger-rg/providers/Microsoft.App/containerApps/ppn-blogger/authConfigs/current?api-version=2024-03-01"

# Confirm what is there now — it should be exactly ["/api/config/reload"].
az rest --method get --url "$AUTH" --query properties.globalValidation

az rest --method patch --url "$AUTH" --body '{
  "properties": {
    "globalValidation": {
      "excludedPaths": [
        "/api/config/reload",
        "/manifest.webmanifest",
        "/favicon.svg",
        "/apple-touch-icon.png",
        "/icons/icon-192.png",
        "/icons/icon-512.png",
        "/icons/maskable-512.png"
      ]
    }
  }
}'
```

`excludedPaths` matches **exact paths — assume no globbing**, which is why the
icon set is deliberately small and lives at fixed, unhashed paths under
`/icons/`. Verify from a browser with no session:

```bash
FQDN=ppn-blogger.yellowdune-067a04a7.eastus.azurecontainerapps.io
for p in / /runs /api/health /manifest.webmanifest /favicon.svg /apple-touch-icon.png \
         /icons/icon-192.png /icons/icon-512.png /icons/maskable-512.png; do
  printf '%-28s %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' "https://$FQDN$p")"
done
```

Expect **302** for `/`, `/runs` and `/api/health`; **200** for the manifest and
every icon. Anything else and installation will fail silently.

Deliberately **not** excluded:

- **`/` and `/runs`** — that would serve the app shell anonymously. Chrome does
  not require an anonymous `start_url`; it requires the worker to serve it
  offline, which the precache does.
- **`/sw.js`** — fetched during registration from an already-authenticated page.
  A background update check against an expired session gets HTML, the update
  fails, and the existing worker stays registered, which is correct.
- **`/assets/*`** — hashed, so un-enumerable, and never needed anonymously.

A path in that list that does not exist on disk would previously have fallen
through to `index.html` with a 200 — handing the app shell to anyone who asked.
`server/app.py` now 404s any missing path that carries a file extension, so the
exclusion cannot leak the shell even if a filename is mistyped here.

> **Sessions still expire.** The Easy Auth cookie has a finite lifetime, so an
> installed app will periodically bounce to a Microsoft login. That is inherent
> to Easy Auth, not something the PWA work removes. `client.ts` detects it and
> redirects deliberately rather than failing as "Failed to fetch"; if the
> interruption becomes annoying, enable the Easy Auth token store and raise
> `login.cookieExpiration.timeToExpiration` — a config change on the live app,
> no redeploy.

---

## Known limitations

Consequences of the single-instance design — accept them or address the code
first:

- **No horizontal scale / no HA.** `minReplicas = maxReplicas = 1` is mandatory.
  Raising `maxReplicas` breaks the run queue (per-process), SSE (per-process), and
  triggers the orphan sweep to cancel another instance's runs. True scale-out
  needs the Azure Storage Queue + worker refactor the code TODO anticipates.
- **In-flight runs don't survive a restart.** A redeploy, revision restart, or
  platform recycle marks any `queued`/`running` row `interrupted` on boot. A
  90-minute `write` interrupted this way must be relaunched (its research dossier
  is preserved on the share, so `ppn write --dossier` can resume it).
- **Cost floor.** The standing cost is dominated by PostgreSQL Flexible Server
  (`Standard_B1ms` ≈ a low double-digit monthly figure) plus a small Azure Files
  and Log Analytics charge; the Container App itself can idle cheaply but must not
  scale to zero.

### Cheaper alternative: skip Postgres, keep SQLite on the share

For a personal, low-traffic deployment you can drop the PostgreSQL server and let
the DB live on the mounted Azure Files share:

- Remove the `pg*` resources and the `pg-url` secret from the Bicep; **do not set
  `PPN_DATABASE_URL`** (the app falls back to SQLite at `.ppn_state/ppn.db`), and
  keep the `/app/.ppn_state` mount so the DB persists.
- **Caveat:** SQLite in WAL mode over SMB (Azure Files) is officially discouraged
  because of file-locking behaviour. At a single replica with the app's single
  event-writer task it works in practice, but it is less robust than Postgres.
  This trades reliability for roughly the Postgres monthly cost.

---

## Optional code follow-ups

Small changes that would simplify the above (not required, not done here):

- **Add `asyncpg` to `pyproject` `[server]`** so `postgresql+asyncpg://` works
  without the Dockerfile's extra `pip install`.
- **Make `wp_posts.json`'s directory configurable** — it is hard-coded to
  `ROOT/.ppn_state` (`wordpress.py`), which is why §4 mounts a second sub-path.
- **Deepen `GET /api/health`** with a lightweight DB touch if you want the
  Container Apps probes to catch a broken database, not just a live process.

---

## Appendix A — the live deployment

Deployed 2026-07-28 into subscription `Azure subscription 1`, resource group
**`ppn-blogger-rg`** (app resources in **eastus**). URL:

**https://ppn-blogger.yellowdune-067a04a7.eastus.azurecontainerapps.io** —
protected by Entra login; sign in with the Microsoft account on this tenant.

### What was built

| Component | Value |
|---|---|
| Container Registry | `ppnblogger286957664.azurecr.io`, images `ppn-blogger:v1` (SQLite/pg attempt) and **`:v2`** (running — ODBC driver) |
| Container App | `ppn-blogger`, 1 replica, ingress :8000, user-assigned identity |
| Managed identity (app) | client id `9cf1e2ea-707a-4ff4-8784-7a02cff402b9`; roles on the Foundry resource: **Azure AI Developer**, **Cognitive Services User**, **Cognitive Services OpenAI User** |
| Database | **Azure SQL Database** — server `ppnsql-centralus-17995` (**centralus**), db `ppn`, GeneralPurpose **Serverless** Gen5, min 0.5 vCore, auto-pause 60 min |
| File storage | Azure Files share `ppn-data` on `ppnfilesbvic5hr5oh4pm`, mounted at `/data` (+ `/app/.ppn_state`) |
| Auth | Entra Easy Auth, single-tenant, app registration client id `3deb3d1a-944d-4de0-81c2-dc9236b655be`, `RedirectToLoginPage` |
| Foundry | existing `foundry-powerplatformninja-blog` (eastus), reached key-less via the managed identity |

Verified at deploy time: clean boot (`ppn server ready`), `create_all` + config
seeding succeeded against Azure SQL (5 documents written — proves runtime
read/write), `/api/health` 200, SPA served, and unauthenticated requests redirect
to Microsoft login.

### Why it diverged from the template

1. **PostgreSQL is offer-restricted on this subscription.** Provisioning was
   rejected in eastus, eastus2, westus2 (`LocationIsOfferRestricted` / "location is
   restricted") and errored in westus3. This is subscription-wide, not one region.
2. **SQLite on Azure Files does not work.** SMB does not support SQLite's locking;
   the app failed to start with `sqlite3.OperationalError: database is locked`
   (WAL over SMB). Documented caveat, confirmed the hard way.
3. **→ Azure SQL Database.** Provisions in `centralus` on this subscription.
   Requires the app image to carry the **Microsoft ODBC Driver 18** plus
   `aioodbc`, and `PPN_DATABASE_URL=mssql+aioodbc://…?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes`.
   No application code changed — only the Dockerfile and the URL.

### Operating it

- **Logs:** `az containerapp logs show -g ppn-blogger-rg -n ppn-blogger --follow`
- **Update the app:** rebuild (`az acr build -r ppnblogger286957664 -t ppn-blogger:vN .`)
  then `az containerapp update -g ppn-blogger-rg -n ppn-blogger --image ppnblogger286957664.azurecr.io/ppn-blogger:vN`.
- **Serverless auto-pause:** the SQL database pauses after 60 min idle; the first
  request after a pause pays a one-off ~20–30 s resume. `/api/health` does not
  touch the DB, so probes neither keep it awake nor fail while it is paused.
- **Restarting** (or any redeploy) marks in-flight runs `interrupted` — expected,
  single-instance.

### Optional hardening (not yet applied)

- **Restrict sign-in to only you.** Single-tenant already limits login to this
  tenant. To lock it to your account: Entra ID → Enterprise applications →
  `ppn-blogger-auth` → Properties → **Assignment required = Yes**, then assign
  only yourself under Users and groups.
- **Adopt vs. recreate the DB.** `infra/main.bicep` now provisions the Azure SQL
  server + serverless `ppn` database directly (a fresh `az deployment` creates
  them with a `uniqueString` name). The **live** server was created by CLI with a
  different name, so re-running the Bicep against this resource group would create
  a *second* SQL server and repoint the app to the empty one — migrate the data
  first, or point the Bicep at the existing server, before doing that.
