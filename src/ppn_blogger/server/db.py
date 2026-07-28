"""Database layer: SQLAlchemy models and session handling.

SQLite by default so the whole thing runs locally with no services. The engine
URL is the only Azure-facing seam — point ``PPN_DATABASE_URL`` at
``postgresql+asyncpg://...`` and nothing else changes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from ..settings import ROOT


def _database_url() -> str:
    configured = os.environ.get("PPN_DATABASE_URL", "").strip()
    if configured:
        return configured
    path = ROOT / ".ppn_state" / "ppn.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{path}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ConfigDocument(Base):
    """One version of one config document. Append-only, so edits keep history.

    Config used to live in git-tracked YAML; moving it here means `git log` no
    longer shows rule changes, so every write lands as a new row rather than an
    update. That is what makes rollback and diffing possible in the UI.
    """

    __tablename__ = "config_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    format: Mapped[str] = mapped_column(String(16))  # yaml | markdown
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(400), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_config_name_version", ConfigDocument.name, ConfigDocument.version.desc())


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # suggest | write | translate | cover
    status: Mapped[str] = mapped_column(String(16), index=True)
    label: Mapped[str] = mapped_column(String(300), default="")
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    config_version: Mapped[str] = mapped_column(String(64), default="")
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEvent(Base):
    """Append-only event log per run — the thing the UI replays and streams."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))  # node | log | status | output
    executor_id: Mapped[str] = mapped_column(String(64), default="")
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


Index("ix_run_events_run_seq", RunEvent.run_id, RunEvent.seq)


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def engine():
    global _engine
    if _engine is None:
        url = _database_url()
        kwargs: dict[str, Any] = {"echo": False, "future": True}
        if url.startswith("sqlite"):
            # NullPool for aiosqlite: each connection runs on its own worker
            # thread, and a pooled connection checked out by a task that gets
            # cancelled is never returned — the thread then outlives dispose()
            # and blocks shutdown. Opening per session costs microseconds on
            # SQLite and makes teardown deterministic.
            kwargs["poolclass"] = NullPool
            kwargs["connect_args"] = {"timeout": 30}
        _engine = create_async_engine(url, **kwargs)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    async with session_factory()() as s:
        yield s


async def init_db() -> None:
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if _database_url().startswith("sqlite"):
            # WAL lets the API read while a worker writes.
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")


async def reset_engine() -> None:
    """Dispose the engine — used by tests switching database URLs."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


__all__ = [
    "Base",
    "ConfigDocument",
    "Run",
    "RunEvent",
    "engine",
    "func",
    "init_db",
    "reset_engine",
    "session",
    "session_factory",
    "utcnow",
]
