# Management UI

The React SPA for the blogging crew — Stage 2 of the service described in
[../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md). It consumes the FastAPI
contract; there is no backend code here.

## Stack

Vite + React + TypeScript · Tailwind CSS v4 · TanStack Query · React Router ·
React Flow (`@xyflow/react`) + dagre for the agent canvas · CodeMirror 6 for the
YAML/Markdown editors · react-markdown for rendered drafts.

## Develop

Two processes. The API server:

```bash
# from the repo root
ppn serve            # http://127.0.0.1:8000
```

The dev server:

```bash
cd ui
npm install
npm run dev          # http://localhost:5173
```

Vite proxies `/api` (REST **and** the SSE event stream) to the API server, so
both are same-origin in dev — no CORS to negotiate. The proxy target defaults to
`http://127.0.0.1:8000`; point it elsewhere (e.g. if that port is taken) with:

```bash
PPN_API_TARGET=http://127.0.0.1:8008 npm run dev
```

## Build

```bash
npm run build        # → ui/dist
```

`ppn serve` serves `ui/dist` as a same-origin SPA when it exists, so production is
a single process. `ui/dist` and `ui/node_modules` are git-ignored.

## Navigation

**Five tabs, and five is the ceiling.** `NAV` in `components/AppShell.tsx` drives
both the desktop sidebar and a mobile bottom bar of `flex-1` columns — nine
destinations would be ~43px each, below the 44px floor the whole UI is built to.
So related screens share a tab and separate through `<SubNav>` *inside* the
section, rendered by the screen rather than the shell so a section with one child
shows nothing.

| Tab | Lands on | Sub-nav |
|---|---|---|
| **Runs** | `/runs` | — |
| **News** | `/articles` | Stream · Feeds · Groups |
| **Letters** | `/newsletters` | Newsletters · Issues · Recipients |
| **Blog** | `/topic-ideas` | Ideas · Drafts · Sources |
| **Config** | `/config` | — |

`paths` on each entry exists because NavLink's own `isActive` only matches its
`to`: the Blog tab has to look active on `/drafts`, which is not a route it links
to. Badges come from one `GET /api/news/pending` — three separate polls would
wake the serverless database three times as often for no more information.

## Screens

- **Runs** (`/runs`) — history, live status, queue depth, launch, cancel.
- **Run detail** (`/runs/:id`) — the agents drawn as a graph that lights up live
  from the SSE stream; click a node for its output and log lines. The topology
  comes from `GET /api/workflows`; node state is folded from the event log.
- **Articles** (`/articles`) — everything harvested, grouped by day, filterable
  by group/window/search. Filters run **server-side** here, unlike the topic and
  draft lists: this one grows without limit.
- **Feeds** (`/feeds`, `/feeds/:id`) — health, add-by-URL with a live preview
  before saving, a watch-closely toggle that states its cost, and *Find new* to
  start a discovery sweep.
- **Feed reviews** (`/feed-reviews/:id`) — approve discovered feeds. Every
  candidate was fetched and parsed before it got here.
- **Newsletters** (`/newsletters`, `/newsletters/:id`) — groups, schedule editor
  showing the next three fire times, and a **free** candidate preview (no model
  call) so tuning costs nothing.
- **Issues** (`/newsletters/issues/:id`) — preview, edit, per-recipient delivery,
  and Send behind a confirmation that names every recipient.
- **Recipients** (`/newsletters/recipients`) — channels with their real
  constraints stated where you choose one.
- **Config** (`/config`) — edit the six documents with history and rollback;
  invalid YAML surfaces the server's 422 parser message inline.
- **Drafts** (`/drafts`) — read/edit the Markdown, view the review report and
  cover, publish to WordPress (behind a confirm dialog — it writes to the live
  blog).

## Two rules that are not style

**The email preview is a sandboxed iframe**, never `dangerouslySetInnerHTML`. An
issue's HTML carries its own inlined styles and a `<table>` layout, and dropping
that into the document would leak straight into the app shell.

**No API path may carry a trailing slash.** `api/client.ts` fetches with
`redirect: 'manual'` and reads any redirect as an expired Easy Auth session, so a
307 signs the operator out instead of returning data. The server sets
`redirect_slashes=False` to make that impossible; keep it set.

## What is persisted offline

`lib/persist.ts` is an allow-list of query-key roots, and it is an allow-list on
purpose: what is worth reading on a train is *content* — drafts, articles,
issues, the backlog. Run state, delivery status, `pending` and `schedule` are
deliberately excluded. A cached "running" or "next due in 3 minutes" shown an
hour later is not stale data, it is a lie.

## The one invariant worth knowing

The canvas and the transcript must never disagree. Both are derived from the same
event stream by [`src/lib/deriveNodes.ts`](src/lib/deriveNodes.ts), a faithful
port of `derive_nodes()` in `server/api.py`. Keep the two in lockstep: fold the
log, never store node status separately. Because status is derived, a browser
opening a finished run replays it with the same animation as one that watched it
live.
