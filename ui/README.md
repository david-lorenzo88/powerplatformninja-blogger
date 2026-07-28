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

## Screens

- **Runs** (`/runs`) — history, live status, queue depth, launch (suggest/write),
  cancel.
- **Run detail** (`/runs/:id`) — the agents drawn as a graph that lights up live
  from the SSE stream; click a node for its output and log lines. The topology
  comes from `GET /api/workflows`; node state is folded from the event log.
- **Config** (`/config`) — edit the five documents with history and rollback;
  invalid YAML surfaces the server's 422 parser message inline.
- **Drafts** (`/drafts`) — read/edit the Markdown, view the review report and
  cover, publish to WordPress (behind a confirm dialog — it writes to the live
  blog).

## The one invariant worth knowing

The canvas and the transcript must never disagree. Both are derived from the same
event stream by [`src/lib/deriveNodes.ts`](src/lib/deriveNodes.ts), a faithful
port of `derive_nodes()` in `server/api.py`. Keep the two in lockstep: fold the
log, never store node status separately. Because status is derived, a browser
opening a finished run replays it with the same animation as one that watched it
live.
