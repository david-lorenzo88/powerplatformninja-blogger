"""Database layer: SQLAlchemy models and session handling.

The backend is whatever ``PPN_DATABASE_URL`` names, and there is deliberately no
default. Production is Azure SQL over ``mssql+aioodbc``; SQLite is still a fine
choice for a laptop, but it has to be asked for.

The default used to be SQLite, and it cost real money twice. SQLite is the more
forgiving dialect in exactly the places that matter — it accepts ``IS 1`` where
SQL Server rejects the statement outright, and it returns naive datetimes from a
timezone-aware column where Azure SQL returns aware ones. Both differences
produced code that was green through the whole test suite and failed on the
first request in Azure. A silent fallback meant nothing ever announced which
dialect it was on; now an unconfigured environment says so instead of quietly
picking the one that hides bugs.

``tests/test_sql_portability.py`` guards the same seam from the other side.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
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

# Importing settings is also what loads .env, which is where PPN_DATABASE_URL
# lives on a laptop — so this import is load-bearing, not just for ROOT.
from ..settings import ROOT

LOCAL_SQLITE_URL = "sqlite+aiosqlite:///.ppn_state/ppn.db"


def _database_url() -> str:
    configured = os.environ.get("PPN_DATABASE_URL", "").strip()
    if not configured:
        raise RuntimeError(
            "PPN_DATABASE_URL is not set, and there is no default.\n\n"
            "Production is Azure SQL. For a laptop, add this line to "
            f"{ROOT / '.env'}:\n"
            f"    PPN_DATABASE_URL={LOCAL_SQLITE_URL}\n\n"
            "It has to be spelled out rather than assumed: SQLite accepts SQL "
            "that Azure SQL rejects, so silently defaulting to it hid two bugs "
            "until they reached production."
        )
    return configured


def _ensure_sqlite_dir(url: str) -> None:
    path = url.split("///", 1)[1].split("?", 1)[0] if "///" in url else ""
    if path and path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Make a datetime read back from the database safe to compare.

    SQLite has no timezone type, so a ``DateTime(timezone=True)`` column returns
    a *naive* datetime locally and in tests, while Azure SQL's DATETIMEOFFSET
    returns an aware one. Comparing either against ``utcnow()`` without this
    raises TypeError on one backend and silently works on the other — which is
    the worst shape a bug can have here, since the tests run on the forgiving
    one. Everything stored is UTC, so attaching the timezone is sound.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


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


class PushSubscription(Base):
    """One browser's Web Push grant.

    Per-browser, not per-user: the app has no user model at all — Easy Auth sits
    in front of it and nothing downstream reads the identity header — so the push
    endpoint the browser hands us *is* the identity.

    It has to be a row rather than a dict in memory for two reasons. CI replaces
    the container on every merge to main, and an in-memory store would silently
    unsubscribe every device on each deploy. And push exists precisely for the
    case where no SSE client is connected, so the delivery list cannot be derived
    from live subscribers.

    Two Azure SQL constraints shape the columns, and both are the kind that only
    bite in production:

    - `endpoint` is String(500), not Text. Text becomes NVARCHAR(max), which
      cannot carry an index at all, and this column needs a unique one to make
      re-subscribing idempotent.
    - That index is explicitly non-clustered. SQL Server makes a primary key
      clustered by default, and the clustered key limit is 900 bytes —
      NVARCHAR(500) is 1000 and would be rejected outright. Hence the ordinary
      autoincrement id, like every other table here.

    `Base.metadata.create_all` is the whole migration story (checkfirst=True), so
    this table appears on the next boot and the existing seven are untouched. It
    never *alters*, though — a column added later needs hand-run DDL against
    Azure SQL, so this wants to be right first time.
    """

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(String(500))
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    failures: Mapped[int] = mapped_column(Integer, default=0)


Index("ix_push_endpoint", PushSubscription.endpoint, unique=True)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # suggest | explore | shortlist | write | cover | ingest | newsletter | deliver
    #   | discover
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


class RunUsage(Base):
    """What one agent invocation, or one generated image, consumed.

    A **table** rather than columns on ``runs``, for two reasons. The mechanical
    one: ``create_all`` is checkfirst-only and never ALTERs, so a column added
    here would need hand-run DDL against Azure SQL while a new table simply
    appears on the next boot. The useful one: per-agent rows are what answer
    "which stage is expensive", and a run total is a ``SUM`` over them — the
    other way round, the breakdown is gone for good.

    Rows are written as the run proceeds rather than at the end, so a run that
    fails or is cancelled still accounts for what it had already spent. Those are
    the runs most worth costing.
    """

    __tablename__ = "run_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), default="")
    # As reported by the service, which is not always what was configured: a
    # deployment called gpt-5 answers as gpt-5-2025-08-07.
    model: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(16), default="model")  # model | image
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    searches: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    # Integer micros of `currency`, never a Float. These get summed across
    # thousands of rows to answer "what did last month cost", and binary
    # floating point is not the thing to accumulate money in — the repo has
    # already been bitten once by trusting the forgiving dialect.
    cost_micros: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="")
    # False when no rate was configured for this model. The tokens above are
    # still real; only the money is unknown, and the UI must say so rather than
    # render a confident zero.
    priced: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_run_usage_created", RunUsage.created_at)


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


# ---------------------------------------------------------------------------
# News: feeds, groups, articles
#
# The crew reads the nine feeds in sources.yaml fresh on every agent call and
# keeps nothing. That is fine for research and useless for a digest, which has to
# answer "what is new since last time" — so the news subsystem keeps its own
# state: conditional-GET validators, per-feed health, and one row per entry ever
# seen. sources.yaml is left exactly as it is; these feeds are a second, larger
# registry seeded from it (see server/ingest.py).
#
# Two shapes here are load-bearing and both come from Azure SQL. URLs are stored
# as Text and indexed through a String(64) hash of their canonical form, because
# article URLs routinely exceed the ~450 characters an indexable NVARCHAR holds
# and NVARCHAR(max) cannot be indexed at all. And every column a later phase
# needs is declared now — create_all is checkfirst-only and never ALTERs, so a
# column added later means hand-run DDL against production.
# ---------------------------------------------------------------------------


class Feed(Base):
    """One RSS/Atom source in the managed registry."""

    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text)
    # sha256(news.canonical_url(url)) — the dedup key. Unique and non-clustered:
    # the same trap as push_subscriptions.endpoint, for the same reason.
    url_hash: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200), default="")  # what the operator calls it
    title: Mapped[str] = mapped_column(String(300), default="")  # what the feed calls itself
    site_url: Mapped[str] = mapped_column(Text, default="")
    home_domain: Mapped[str] = mapped_column(String(200), default="", index=True)
    tier: Mapped[str] = mapped_column(String(40), default="unknown")  # trust_tiers ids
    topics: Mapped[list[Any]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Phase 2: a realtime feed is polled on the short cadence and notifies.
    realtime: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Reserved: exposes this feed to tools.read_feeds once the crew reads the
    # registry rather than sources.yaml. Not wired yet, deliberately.
    used_by_crew: Mapped[bool] = mapped_column(Boolean, default=False)
    origin: Mapped[str] = mapped_column(String(32), default="manual")  # manual|seed|discovered
    # Conditional GET state — the thing that makes a short cadence affordable.
    etag: Mapped[str] = mapped_column(String(200), default="")
    last_modified: Mapped[str] = mapped_column(String(120), default="")
    # Health. `last_status` is the HTTP code, or 0 when the request never
    # completed at all — a distinction read_feeds throws away, which is why a
    # feed can currently 403 for months without anyone noticing.
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(400), default="")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, index=True)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, default=0)  # 0 = use the default
    notes: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


Index("ix_feeds_url_hash", Feed.url_hash, unique=True)
Index("ix_feeds_enabled_realtime", Feed.enabled, Feed.realtime)


class FeedGroup(Base):
    """A named bundle of feeds — the unit a newsletter draws from."""

    __tablename__ = "feed_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


Index("ix_feed_groups_slug", FeedGroup.slug, unique=True)


class FeedGroupMember(Base):
    """Feed-to-group membership.

    A mapped class rather than a bare association Table: there is no
    relationship() anywhere in this codebase — every read is an explicit
    select() — and a plain autoincrement PK sidesteps the clustered-key question
    a composite primary key would raise on Azure SQL.
    """

    __tablename__ = "feed_group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("feed_groups.id"), index=True)
    feed_id: Mapped[int] = mapped_column(Integer, ForeignKey("feeds.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_feed_group_member_pair", FeedGroupMember.group_id, FeedGroupMember.feed_id, unique=True)


class Article(Base):
    """One entry harvested from a feed.

    The unique (feed_id, entry_key) index is not an optimisation — it is the
    dedup guarantee the whole subsystem rests on. A feed republishing an entry
    after an edit cannot create a second row, which is what makes "notify once"
    structural rather than something the notify code has to remember.
    """

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feed_id: Mapped[int] = mapped_column(Integer, ForeignKey("feeds.id"), index=True)
    entry_key: Mapped[str] = mapped_column(String(64))
    # Separate from entry_key: the same story syndicated by two feeds is two rows
    # but one URL, and a digest must not show it twice.
    url_hash: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(400), default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    # Reserved and deliberately left empty: storing content:encoded for hundreds
    # of feeds reaches gigabytes within a year on a tier billed by storage. The
    # column exists now only because it could never be added later.
    content: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    domain: Mapped[str] = mapped_column(String(200), default="", index=True)
    tier: Mapped[str] = mapped_column(String(40), default="unknown")
    language: Mapped[str] = mapped_column(String(16), default="")
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    # Phase 2: set before the push is sent, so a crash mid-send costs a missed
    # notification rather than a duplicate one.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Phase 3: cheap "has any issue used this?" without joining issue items.
    used_in_issue_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


Index("ix_articles_feed_entry", Article.feed_id, Article.entry_key, unique=True)
Index("ix_articles_published", Article.published_at.desc())
Index("ix_articles_fetched", Article.fetched_at.desc())


class SchedulerJob(Base):
    """Durable due-time for one periodic job.

    The due time lives here rather than in memory so a restart resumes the same
    schedule, and — more importantly — so two processes cannot both fire a tick.
    `minReplicas: 1` is not the guarantee it looks like: Container Apps starts the
    new revision before draining the old on every deploy, so there are routinely
    two schedulers alive for a minute or so. The claim is a compare-and-swap on
    `next_due_at` (see server/scheduler.py), which needs no dialect-specific
    locking and works identically on SQLite and Azure SQL.

    `lease_owner`/`lease_expires_at` cover the other half: a process that claims a
    tick and then dies would otherwise leave the job looking taken forever.

    Newsletters will carry their own `next_due_at` on the newsletter row when they
    arrive — a fire time belongs with the definition the operator edits, and two
    sources of truth for one schedule is how a digest gets sent twice.
    """

    __tablename__ = "scheduler_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # fetch | watch | prune
    key: Mapped[str] = mapped_column(String(64))
    next_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status: Mapped[str] = mapped_column(String(16), default="")  # ok | error | skipped
    last_error: Mapped[str] = mapped_column(String(500), default="")
    last_detail: Mapped[str] = mapped_column(String(300), default="")
    runs: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str] = mapped_column(String(64), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


Index("ix_scheduler_jobs_key", SchedulerJob.key, unique=True)


# ---------------------------------------------------------------------------
# Newsletters: definitions, issues, and the items in them
#
# An issue's markdown and HTML live in columns rather than files — a deliberate
# exception to the crew's "content is files, the database is an index" rule. A
# blog draft is a file because `ppn write` works with no server and the operator
# edits the markdown by hand. An issue has no CLI-first story, is the *payload*
# of a delivery, and is read by the sender: one query beats an Azure Files round
# trip, and storing the rendered HTML makes a re-send byte-identical to the
# first attempt.
# ---------------------------------------------------------------------------


class Newsletter(Base):
    """One newsletter definition: what it draws from, and how often."""

    __tablename__ = "newsletters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Four schedule kinds rather than cron: cron needs a dependency for
    # expressiveness nobody has asked for, and these cover "every six hours",
    # "Wednesdays at 07:00" and "the 1st". `cron` is reserved so adding it later
    # is a code change rather than hand-run DDL against production.
    schedule_kind: Mapped[str] = mapped_column(String(16), default="manual")
    interval_minutes: Mapped[int] = mapped_column(Integer, default=0)
    weekday: Mapped[int] = mapped_column(Integer, default=0)  # 0=Mon .. 6=Sun
    day_of_month: Mapped[int] = mapped_column(Integer, default=1)
    hour_local: Mapped[int] = mapped_column(Integer, default=7)
    minute_local: Mapped[int] = mapped_column(Integer, default=0)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Madrid")
    cron: Mapped[str] = mapped_column(String(120), default="")

    lookback_hours: Mapped[int] = mapped_column(Integer, default=168)
    max_items: Mapped[int] = mapped_column(Integer, default=12)
    min_items: Mapped[int] = mapped_column(Integer, default=3)
    max_per_feed: Mapped[int] = mapped_column(Integer, default=3)
    audience: Mapped[str] = mapped_column(Text, default="")
    tone: Mapped[str] = mapped_column(String(120), default="")
    # Phase 4, and default false on purpose: the crew's standing rule is that
    # nothing reaches an audience unattended.
    auto_send: Mapped[bool] = mapped_column(Boolean, default=False)

    next_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stops a second issue being queued while the first is still generating. A
    # column beats filtering a JSON params field, which is awkward on SQL Server
    # and slow everywhere.
    last_enqueued_run_id: Mapped[str] = mapped_column(String(36), default="")
    # Plain Integer, not a ForeignKey — newsletters <-> issues is a cycle, the
    # same shape as Post.current_version_id, and a hard FK would force use_alter
    # at create_all time.
    last_issue_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


Index("ix_newsletters_slug", Newsletter.slug, unique=True)
Index("ix_newsletters_due", Newsletter.enabled, Newsletter.next_due_at)


class NewsletterGroup(Base):
    """Which feed groups a newsletter draws from."""

    __tablename__ = "newsletter_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    newsletter_id: Mapped[int] = mapped_column(Integer, ForeignKey("newsletters.id"), index=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("feed_groups.id"), index=True)


Index(
    "ix_newsletter_group_pair",
    NewsletterGroup.newsletter_id,
    NewsletterGroup.group_id,
    unique=True,
)


class NewsletterIssue(Base):
    """One generated issue."""

    __tablename__ = "newsletter_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    newsletter_id: Mapped[int] = mapped_column(Integer, ForeignKey("newsletters.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    number: Mapped[int] = mapped_column(Integer, default=1)
    # draft | ready | sending | sent | failed | skipped
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    subject: Mapped[str] = mapped_column(String(300), default="")
    preheader: Mapped[str] = mapped_column(String(300), default="")
    intro: Mapped[str] = mapped_column(Text, default="")
    markdown: Mapped[str] = mapped_column(Text, default="")
    html: Mapped[str] = mapped_column(Text, default="")
    text_body: Mapped[str] = mapped_column(Text, default="")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    window_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    generated_on: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_issues_newsletter_number", NewsletterIssue.newsletter_id, NewsletterIssue.number.desc())


class NewsletterIssueItem(Base):
    """One article as it appeared in one issue."""

    __tablename__ = "newsletter_issue_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(Integer, ForeignKey("newsletter_issues.id"), index=True)
    # Denormalised so "has this newsletter already used this article?" is one
    # lookup rather than a join back through issues on every composition.
    newsletter_id: Mapped[int] = mapped_column(Integer, index=True)
    article_id: Mapped[int] = mapped_column(Integer, ForeignKey("articles.id"), index=True)
    section: Mapped[str] = mapped_column(String(80), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    headline: Mapped[str] = mapped_column(String(400), default="")
    blurb: Mapped[str] = mapped_column(Text, default="")


# Deliberately NOT unique. "Never repeat an article" is a filter over items
# belonging to issues that were actually sent or approved — a discarded or
# failed issue must not permanently burn the article it happened to mention.
Index(
    "ix_issue_items_newsletter_article",
    NewsletterIssueItem.newsletter_id,
    NewsletterIssueItem.article_id,
)


# ---------------------------------------------------------------------------
# Delivery: who receives an issue, and what happened when it was sent
# ---------------------------------------------------------------------------


class Recipient(Base):
    """One address on one channel.

    The list is private and managed by the operator, so there is no signup flow,
    no double opt-in and no consent record. ``unsubscribe_token`` and
    ``consent_source`` are nonetheless declared, unused: `create_all` never
    ALTERs, so they are the difference between "add a public signup later" and
    "hand-run DDL against production later". They cost two nullable columns.
    """

    __tablename__ = "recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # email | telegram | whatsapp | webpush | manual
    channel: Mapped[str] = mapped_column(String(24), index=True)
    # An address, a Telegram chat id, or an E.164 number. Empty for the
    # broadcast channels, which have no per-recipient target.
    address: Mapped[str] = mapped_column(Text, default="")
    # sha256 of the normalised address. Indexed instead of `address` for the same
    # reason article URLs are: an indexable key has to be bounded.
    address_hash: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Empty means every newsletter.
    newsletter_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(String(500), default="")
    # Set when a channel reports the address is permanently bad, so a dead
    # address stops being retried without being deleted.
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(400), default="")
    unsubscribe_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


Index("ix_recipients_addr", Recipient.channel, Recipient.address_hash, unique=True)


class Delivery(Base):
    """One attempt to get one issue to one recipient.

    Written as ``pending`` *before* anything is sent, so the intent is durable
    before the side effect — the same ordering discipline as ``reviews.decide``.
    A process that dies mid-send leaves rows saying exactly how far it got.
    """

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(Integer, ForeignKey("newsletter_issues.id"), index=True)
    # Null for a broadcast channel (web push goes to every subscribed browser,
    # which is not a recipient row).
    recipient_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("recipients.id"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(24), index=True)
    # pending | sent | failed | skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider_message_id: Mapped[str] = mapped_column(String(200), default="")
    error: Mapped[str] = mapped_column(String(500), default="")
    # Null once the row is terminal. A retry job selects on this rather than
    # scanning every pending row.
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_deliveries_issue_recipient", Delivery.issue_id, Delivery.recipient_id)
Index("ix_deliveries_retry", Delivery.status, Delivery.next_retry_at)


class FeedDiscoveryReview(Base):
    """Feeds a discovery run proposes, paused for the operator's verdict.

    Shaped like ``SourceReview`` on purpose: the same UI and the same mental
    model, because it is the same decision — a model has suggested where to look,
    and a human decides whether it may.

    The candidates are stored **already validated**. A discovery run fetches and
    parses every URL the scout named before writing this row, so what the
    operator sees is a list of feeds that demonstrably exist, not a list of
    guesses. That is the ``sources.py`` rule — the review is code, never
    judgement — carried over intact.
    """

    __tablename__ = "feed_discovery_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    instruction: Mapped[str] = mapped_column(Text, default="")
    generated_on: Mapped[str] = mapped_column(String(32), default="")
    # Verbatim, and already probed: {url, title, site_url, entry_count, newest,
    # sample_titles, suggested_topics, reason, known}
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    decisions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # Feeds actually created, so the review is traceable to what it caused.
    created_feed_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    approved_count: Mapped[int] = mapped_column(Integer, default=0)
    # URLs the operator turned down, so a later sweep never offers them again.
    declined_urls: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_feed_reviews_status", FeedDiscoveryReview.status, FeedDiscoveryReview.created_at.desc())


class DeclinedFeed(Base):
    """A feed the operator said no to. Never offered again.

    A separate table rather than a flag on ``feeds``: a declined URL was never
    registered as a feed, and inventing a disabled row for it would put things
    in the Feeds screen that the operator explicitly rejected.
    """

    __tablename__ = "declined_feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_declined_feeds_hash", DeclinedFeed.url_hash, unique=True)


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def engine():
    global _engine
    if _engine is None:
        url = _database_url()
        kwargs: dict[str, Any] = {"echo": False, "future": True}
        if url.startswith("sqlite"):
            # The URL is now given rather than derived, so the directory it names
            # is not guaranteed to exist; without this SQLite fails with a bare
            # "unable to open database file".
            _ensure_sqlite_dir(url)
            # NullPool for aiosqlite: each connection runs on its own worker
            # thread, and a pooled connection checked out by a task that gets
            # cancelled is never returned — the thread then outlives dispose()
            # and blocks shutdown. Opening per session costs microseconds on
            # SQLite and makes teardown deterministic.
            kwargs["poolclass"] = NullPool
            kwargs["connect_args"] = {"timeout": 30}
        _engine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            _enforce_sqlite_foreign_keys(_engine)
    return _engine


def _enforce_sqlite_foreign_keys(async_engine: Any) -> None:
    """Make SQLite check foreign keys, as every other backend already does.

    SQLite ships with ``PRAGMA foreign_keys`` **off**: it stores the constraints
    and enforces none of them, so an orphan row is accepted in silence. Azure SQL
    rejects the same INSERT outright. That is the third bug of this exact shape —
    after ``IS 1`` and naive-vs-aware datetimes — where the tests run on the
    forgiving backend and production runs on the strict one.

    The pragma is **per connection**, not per database, so setting it once at
    startup would cover exactly one connection and none of the ones real work
    uses. Hence the connect listener.

    This makes the local suite fail on the same data SQL Server would reject,
    which is the whole point: a broken foreign key should be a red test on a
    laptop, not a red deploy.
    """
    from sqlalchemy import event

    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


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
    "Article",
    "Base",
    "ConfigDocument",
    "DeclinedFeed",
    "Delivery",
    "DraftVersion",
    "Feed",
    "FeedGroup",
    "FeedDiscoveryReview",
    "FeedGroupMember",
    "Newsletter",
    "NewsletterGroup",
    "NewsletterIssue",
    "NewsletterIssueItem",
    "Post",
    "PushSubscription",
    "Recipient",
    "Run",
    "RunEvent",
    "SchedulerJob",
    "SourceReview",
    "TopicIdea",
    "as_utc",
    "engine",
    "func",
    "init_db",
    "reset_engine",
    "session",
    "session_factory",
    "utcnow",
]
