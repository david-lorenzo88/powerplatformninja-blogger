"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..settings import ROOT
from ..util import setup_logging
from . import config_store
from .api import router
from .db import engine, init_db
from .runs import manager

logger = logging.getLogger("ppn.server")

UI_DIST = ROOT / "ui" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    await init_db()
    seeded = await config_store.seed_from_yaml_if_empty()
    await config_store.refresh_active_source()
    if seeded:
        logger.info("configuration imported from config/ — the database is now authoritative")
    from . import catalog

    await catalog.backfill()
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

    # Serve the built SPA when it exists, so production is a single process.
    if UI_DIST.exists():
        app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

        @app.get("/{full_path:path}")
        async def spa(full_path: str) -> FileResponse:
            # The API namespace must never fall through to the SPA shell. An
            # unmatched /api/* path is a 404, not a 200 with index.html — else a
            # malformed request like /api/drafts/..%2F..%2F.env reads as "route
            # not found, here is the app" instead of being rejected.
            if full_path.startswith("api/") or full_path == "api":
                raise HTTPException(404, "Not found")
            candidate = UI_DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(UI_DIST / "index.html")

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
