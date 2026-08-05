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

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
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
    # suggest | explore | shortlist | write | cover
    kind: Mapped[str] = mapped_column(String(32), index=True)
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


class SourceReview(Base):
    """One wide-web sweep, paused for the operator's verdict on its sources.

    The scout reports are stored verbatim rather than re-derived: the shortlist
    is built from exactly the material the operator was shown, and a sweep costs
    real model calls that a server restart must not throw away. This row is what
    lets the approval sit between two runs instead of holding a worker open.
    """

    __tablename__ = "source_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    instruction: Mapped[str] = mapped_column(Text, default="")
    generated_on: Mapped[str] = mapped_column(String(32), default="")
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reports: Mapped[list[Any]] = mapped_column(JSON, default=list)
    decisions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # The config version the approval produced, so a review is traceable to the
    # sources.yaml edit it caused.
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shortlist_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Catalog: topic ideas, posts, draft versions
#
# The crew's artefacts (shortlists, drafts, dossiers) live as files on disk; the
# database used to index only runs and config. These three tables index the
# artefacts so the UI can browse and filter the backlog of ideas, see which
# became posts, and keep a version history — while the markdown, review and cover
# stay as files. Rows are written when a run finishes (see server/catalog.py) and
# backfilled from existing runs and files on first start.
# ---------------------------------------------------------------------------


class TopicIdea(Base):
    """One topic proposal from a suggest run. Deduped by slug (upsert)."""

    __tablename__ = "topic_ideas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    watch_area: Mapped[str] = mapped_column(String(80), default="", index=True)
    post_format: Mapped[str] = mapped_column(String(80), default="", index=True)
    primary_keyword: Mapped[str] = mapped_column(String(200), default="")
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    audience_fit: Mapped[int] = mapped_column(Integer, default=0)
    timeliness: Mapped[int] = mapped_column(Integer, default=0)
    effort: Mapped[int] = mapped_column(Integer, default=0)
    angle: Mapped[str] = mapped_column(Text, default="")
    problem_statement: Mapped[str] = mapped_column(Text, default="")
    why_now: Mapped[str] = mapped_column(Text, default="")
    novelty: Mapped[str] = mapped_column(Text, default="")
    duplicate_risk: Mapped[str] = mapped_column(Text, default="")
    key_questions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    seed_sources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # The full TopicSuggestion, kept verbatim so a write can be launched from the
    # idea without reconstructing it from columns.
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    suggest_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id"), nullable=True, index=True
    )
    generated_on: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Post(Base):
    """The logical draft/post grouping — one subject, many draft versions."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_idea_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("topic_ideas.id"), nullable=True, index=True
    )
    slug: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    wordpress_post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    edit_link: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    # Plain Integer, not a ForeignKey: posts <-> draft_versions is a cycle, and a
    # hard FK would force insert ordering / use_alter at create_all time. The app
    # resolves this pointer in code.
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DraftVersion(Base):
    """One generated draft attempt for a Post. Version bumps per post."""

    __tablename__ = "draft_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(Integer, ForeignKey("posts.id"), index=True)
    write_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    # The editor guidance that produced this version, empty for the first draft.
    instructions: Mapped[str] = mapped_column(Text, default="")
    reused_research: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(300), default="")
    slug: Mapped[str] = mapped_column(String(200), default="")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    blockers: Mapped[int] = mapped_column(Integer, default=0)
    markdown_path: Mapped[str] = mapped_column(Text, default="")
    report_path: Mapped[str] = mapped_column(Text, default="")
    cover_path: Mapped[str] = mapped_column(Text, default="")
    dossier_path: Mapped[str] = mapped_column(Text, default="")
    wordpress_post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    edit_link: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_draft_versions_post_version", DraftVersion.post_id, DraftVersion.version.desc())


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
    "DraftVersion",
    "Post",
    "Run",
    "RunEvent",
    "SourceReview",
    "TopicIdea",
    "engine",
    "func",
    "init_db",
    "reset_engine",
    "session",
    "session_factory",
    "utcnow",
]
