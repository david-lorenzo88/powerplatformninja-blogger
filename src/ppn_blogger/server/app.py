"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from ..settings import ROOT
from ..util import setup_logging
from . import config_store
from .api import router
from .api_news import router as news_router
from .db import engine, init_db
from .runs import manager

logger = logging.getLogger("ppn.server")

UI_DIST = ROOT / "ui" / "dist"

# Debian slim carries no /etc/mime.types, and whether the interpreter's built-in
# map knows .webmanifest varies by version. A manifest served as text/plain is
# silently ignored and the app is simply not installable, with no error anywhere
# — so state it rather than hope.
MEDIA_TYPES = {".webmanifest": "application/manifest+json"}

# A year, and immutable: every name under /assets/ carries a content hash, so the
# bytes behind one can never change.
IMMUTABLE = "public, max-age=31536000, immutable"


class HashedStatic(StaticFiles):
    """StaticFiles that pins its responses.

    Deliberately not an HTTP middleware. The obvious way to stamp Cache-Control
    is a `@app.middleware("http")`, but that wraps every route in a
    BaseHTTPMiddleware — including the SSE stream at /api/runs/{id}/events, which
    is a long-lived StreamingResponse and the one thing in this app that must not
    acquire a wrapper between it and the client. Narrow it to the handlers that
    actually serve files instead.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers.setdefault("Cache-Control", IMMUTABLE)
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    await init_db()
    seeded = await config_store.seed_from_yaml_if_empty()
    await config_store.refresh_active_source()
    if seeded:
        logger.info("configuration imported from config/ — the database is now authoritative")
    from . import catalog
    from .ingest import seed_feeds

    await catalog.backfill()
    # Idempotent by url_hash, so it is safe on every boot. Gives the News screen
    # something to show on a fresh database instead of an empty page; the crew's
    # own reading of sources.yaml is untouched.
    await seed_feeds()
    await manager().start()
    logger.info("ppn server ready")
    try:
        yield
    finally:
        await manager().stop()
        await engine().dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Power Platform Ninja — blogging crew",
        version="0.2.0",
        lifespan=lifespan,
        # A trailing slash must 404, never 307.
        #
        # `ui/src/api/client.ts` fetches with `redirect: 'manual'` and treats any
        # redirect as Easy Auth bouncing an expired session to Microsoft — so a
        # 307 does not surface as a 404, it logs the operator out.
        #
        # That client assumed this could never fire because every route is
        # declared without a trailing slash. It fired anyway: Starlette redirects
        # `/api/runs/` to `/api/runs` precisely *because* the latter exists. In
        # production the SPA catch-all happened to absorb it (it matches
        # `/{full_path:path}` and 404s anything under `api/`), so the bug was
        # invisible — but `ppn serve` in dev has no `ui/dist`, no catch-all, and
        # every API path there 307s on a stray slash.
        #
        # Turning the redirect off makes the behaviour identical with and without
        # a built UI, which is also what stops this from being a test that passes
        # locally and fails in CI.
        redirect_slashes=False,
    )

    # The Vite dev server runs on a different port during development.
    origins = [
        o.strip()
        for o in os.environ.get(
            "PPN_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)
    app.include_router(news_router)

    # Serve the built SPA when it exists, so production is a single process.
    if UI_DIST.exists():
        app.mount("/assets", HashedStatic(directory=UI_DIST / "assets"), name="assets")

        # HEAD as well as GET: the /assets mount answers HEAD (StaticFiles does),
        # so without this the shell and the manifest were the only things on the
        # origin that 405'd — which reads as a fault to anything that probes
        # before fetching, and made every `curl -I` against them misleading.
        @app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
        async def spa(full_path: str) -> FileResponse:
            # The API namespace must never fall through to the SPA shell. An
            # unmatched /api/* path is a 404, not a 200 with index.html — else a
            # malformed request like /api/drafts/..%2F..%2F.env reads as "route
            # not found, here is the app" instead of being rejected.
            if full_path.startswith("api/") or full_path == "api":
                raise HTTPException(404, "Not found")
            candidate = UI_DIST / full_path
            if full_path and candidate.is_file():
                # These names never change — the shell, the worker script, the
                # manifest, the icons — so without an explicit no-cache a deploy
                # stays invisible until some browser heuristic happens to expire.
                # That is the classic stale-PWA-shell failure, and it strands an
                # installed app on an old build with no way for the user to tell.
                return FileResponse(
                    candidate,
                    media_type=MEDIA_TYPES.get(candidate.suffix),
                    headers={"Cache-Control": "no-cache"},
                )
            # A path that names a file but has none is a 404, not the app. Routes
            # are extensionless so this costs nothing, and it matters because the
            # icons and manifest are excluded from Easy Auth by exact path: were
            # one to fall through, that exclusion would hand the app shell to
            # anyone who asked, unauthenticated.
            if Path(full_path).suffix:
                raise HTTPException(404, "Not found")
            return FileResponse(UI_DIST / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()


def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run(
        "ppn_blogger.server.app:app" if reload else app,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


def _ui_present() -> bool:
    return (UI_DIST / "index.html").exists()


__all__ = ["Path", "app", "create_app", "serve"]
