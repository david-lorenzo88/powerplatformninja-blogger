"""Database-backed catalog of topic ideas, posts and draft versions.

The crew writes its artefacts — shortlists, dossiers, drafts, review reports — as
files on disk. That is the record of *content*; this module is the record of
*relationships*: which idea became which post, how many versions a post has, and
which one is live on WordPress. Rows are written when a run finishes (see
``record_run_result``, called from :class:`RunManager`) and reconciled from
existing runs and files on first start (see ``backfill``).

Nothing here owns content: the markdown, review and cover stay as files and are
still served by ``server/drafts.py``. A draft version simply points at them.

The style mirrors ``config_store.py``: module-level ``async`` functions over
``async with session()``, and a caller that treats a failure as non-fatal — a
finished draft matters more than its catalog row.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from ..models import TopicSuggestion
from ..settings import get_settings
from .db import DraftVersion, Post, Run, TopicIdea, session, utcnow

logger = logging.getLogger("ppn.server.catalog")


# ---------------------------------------------------------------------------
# Population — called when a run finishes
# ---------------------------------------------------------------------------


async def record_run_result(
    run_id: str, kind: str, params: dict[str, Any], result: dict[str, Any] | None
) -> None:
    """Fold a finished run's output into the catalog. The switchboard."""
    if not result:
        return
    if kind in ("suggest", "shortlist"):
        # An `explore` run has no suggestions to record — it stops at a review.
        await upsert_topic_ideas(run_id, result.get("suggestions", []), result.get("generated_on", ""))
    elif kind == "write":
        version_id = await record_write_result(run_id, params, result)
        # Snapshot the draft exactly as the crew wrote it, while the file still
        # holds it: the Drafts editor rewrites the body in place, so the author's
        # first save destroys the only copy of what the crew produced.
        from . import delta_store

        await delta_store.capture_baseline(version_id, result)
    elif kind == "cover":
        await record_cover_result(params, result)


async def upsert_topic_ideas(
    run_id: str | None, suggestions: list[dict[str, Any]], generated_on: str
) -> int:
    """Upsert one row per suggestion, deduped by slug. Returns rows touched.

    A portable select-then-write upsert rather than a dialect-specific
    ``ON CONFLICT``: the same idea recurring across suggest runs updates the
    existing row (keeping its ``created_at``) instead of duplicating.
    """
    touched = 0
    async with session() as s:
        for raw in suggestions:
            slug = (raw.get("slug") or "").strip()
            if not slug:
                continue
            row = (
                await s.execute(select(TopicIdea).where(TopicIdea.slug == slug))
            ).scalar_one_or_none()
            if row is None:
                row = TopicIdea(slug=slug)
                s.add(row)
            _apply_suggestion(row, raw, run_id, generated_on)
            touched += 1
        await s.commit()
    return touched


def _apply_suggestion(
    row: TopicIdea, raw: dict[str, Any], run_id: str | None, generated_on: str
) -> None:
    row.title = raw.get("title", "") or ""
    row.watch_area = raw.get("watch_area", "") or ""
    row.post_format = raw.get("post_format", "") or ""
    row.primary_keyword = raw.get("primary_keyword", "") or ""
    row.score = float(raw.get("score") or 0.0)
    row.audience_fit = int(raw.get("audience_fit") or 0)
    row.timeliness = int(raw.get("timeliness") or 0)
    row.effort = int(raw.get("effort") or 0)
    row.angle = raw.get("angle", "") or ""
    row.problem_statement = raw.get("problem_statement", "") or ""
    row.why_now = raw.get("why_now", "") or ""
    row.novelty = raw.get("novelty", "") or ""
    row.duplicate_risk = raw.get("duplicate_risk", "") or ""
    row.key_questions = raw.get("key_questions", []) or []
    row.seed_sources = raw.get("seed_sources", []) or []
    row.data = raw
    if run_id and not row.suggest_run_id:
        row.suggest_run_id = run_id
    if generated_on:
        row.generated_on = generated_on


async def record_write_result(
    run_id: str | None, params: dict[str, Any], result: dict[str, Any]
) -> int:
    """Create/find the parent post and append a draft version. Returns version id."""
    async with session() as s:
        post = await _resolve_post(s, params, result)
        next_version = (
            await s.execute(
                select(func.max(DraftVersion.version)).where(DraftVersion.post_id == post.id)
            )
        ).scalar() or 0
        version = DraftVersion(
            post_id=post.id,
            write_run_id=run_id,
            version=next_version + 1,
            instructions=params.get("instructions", "") or "",
            reused_research=bool(params.get("reuse_research")),
            title=result.get("title", "") or "",
            slug=result.get("slug", "") or "",
            approved=bool(result.get("approved")),
            score=float(result.get("score") or 0.0),
            blockers=int(result.get("blockers") or 0),
            markdown_path=result.get("markdown_path", "") or "",
            report_path=result.get("report_path", "") or "",
            cover_path=result.get("cover_path", "") or "",
            dossier_path=result.get("dossier_path", "") or "",
            wordpress_post_id=result.get("post_id"),
            edit_link=result.get("edit_link", "") or "",
        )
        s.add(version)
        await s.flush()  # assign version.id so the post can point at it

        post.current_version_id = version.id
        post.title = version.title or post.title
        if version.wordpress_post_id is not None:
            post.wordpress_post_id = version.wordpress_post_id
            post.edit_link = result.get("edit_link", "") or post.edit_link
            post.link = result.get("link", "") or post.link
            post.status = "published" if result.get("link") else "wordpress_draft"
        post.updated_at = utcnow()
        await s.commit()
        return version.id


async def record_cover_result(params: dict[str, Any], result: dict[str, Any]) -> None:
    """Point a post's versions at a freshly generated cover.

    There is one cover per post (``covers/<slug>.png``, overwritten in place), so
    every version shares it — recording the path on all of them keeps ``has_cover``
    honest whichever version the UI is showing. A failed generation carries an
    ``error`` and no path; nothing is recorded then.
    """
    post_id = params.get("post_id")
    path = (result or {}).get("path") or ""
    if post_id is None or not path or (result or {}).get("error"):
        return
    async with session() as s:
        versions = (
            await s.execute(select(DraftVersion).where(DraftVersion.post_id == int(post_id)))
        ).scalars().all()
        for v in versions:
            v.cover_path = path
        post = await s.get(Post, int(post_id))
        if post is not None:
            post.updated_at = utcnow()
        await s.commit()


async def record_publish(markdown_file: str, target: dict[str, Any]) -> int | None:
    """Remember that a draft file reached WordPress. Returns the post row's id.

    Publishing from the app used to tell only the browser: the endpoint returned
    its ``PublishTarget`` and wrote nothing down, leaving ``record_write_result``
    the only thing that could set ``wordpress_post_id`` — and that fires only
    when the *write run itself* pushed. A post published by hand afterwards was
    live on the blog and "not on WordPress" here, which is exactly the state that
    disabled the cover's Send-to-WordPress button on the post that needed it.

    Matching is by markdown filename, the same handle the publish endpoint takes,
    compared in Python rather than with a LIKE — the row count is one per draft
    version and ``_backfill_draft_files`` already reconciles paths this way.
    """
    post_id = target.get("post_id")
    if not markdown_file or not post_id:
        return None
    async with session() as s:
        rows = (
            await s.execute(select(DraftVersion).where(DraftVersion.markdown_path != ""))
        ).scalars().all()
        version = next(
            (v for v in rows if Path(v.markdown_path).name == markdown_file), None
        )
        if version is None:
            return None
        version.wordpress_post_id = int(post_id)
        version.edit_link = target.get("edit_link", "") or version.edit_link

        post = await s.get(Post, version.post_id)
        if post is not None:
            post.wordpress_post_id = int(post_id)
            post.edit_link = target.get("edit_link", "") or post.edit_link
            post.link = target.get("link", "") or post.link
            # The status WordPress itself reported, not one inferred from a link
            # being present — a draft has a link too (`/?p=1279`).
            if target.get("status"):
                post.status = "published" if target["status"] == "publish" else "wordpress_draft"
            post.updated_at = utcnow()
        await s.commit()
        post_id = version.post_id

    # The file now holds what the author actually decided to publish, which is
    # the other half of the pair. Bookkeeping, so it must never sink a publish.
    try:
        from . import delta_store

        await delta_store.capture_final(markdown_file)
    except Exception:  # noqa: BLE001 - a lost data point beats a lost publish
        logger.exception("could not capture the delta for %s", markdown_file)
    return post_id


async def record_cover_publish(post_id: int, target: dict[str, Any]) -> None:
    """Record what sending a cover discovered about where the post lives.

    ``push_cover`` resolves the WordPress post by remembered id and then by slug,
    so it succeeds on posts this catalog believes are unpublished. When it does,
    it has just proved the row wrong — write the answer back rather than making
    the operator publish again to correct it.
    """
    wp_id = target.get("post_id")
    if not wp_id:
        return
    async with session() as s:
        post = await s.get(Post, int(post_id))
        if post is None:
            return
        post.wordpress_post_id = int(wp_id)
        post.edit_link = target.get("edit_link", "") or post.edit_link
        post.link = target.get("link", "") or post.link
        if target.get("status"):
            post.status = "published" if target["status"] == "publish" else "wordpress_draft"
        post.updated_at = utcnow()
        await s.commit()


async def _resolve_post(s: Any, params: dict[str, Any], result: dict[str, Any]) -> Post:
    """Find or create the parent post for a write result.

    Grouping is stable by *idea*, not by draft slug, so a regeneration that
    changes the slug still lands under the same post.
    """
    # 1. Explicit post id (a regeneration targets an existing post).
    post_id = params.get("post_id")
    if post_id is not None:
        post = await s.get(Post, int(post_id))
        if post is not None:
            return post

    topic = params.get("topic") or {}
    topic_slug = (topic.get("slug") or "").strip()

    # 2/3. An explicit idea id, or the topic's slug, ties the post to an idea.
    idea = None
    idea_id = params.get("topic_idea_id")
    if idea_id is not None:
        idea = await s.get(TopicIdea, int(idea_id))
    if idea is None and topic_slug:
        idea = (
            await s.execute(select(TopicIdea).where(TopicIdea.slug == topic_slug))
        ).scalar_one_or_none()
    if idea is not None:
        existing = (
            await s.execute(select(Post).where(Post.topic_idea_id == idea.id))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        post = Post(topic_idea_id=idea.id, slug=idea.slug or topic_slug, title=idea.title)
        s.add(post)
        await s.flush()
        return post

    # 4. No idea: group by the draft slug.
    draft_slug = (result.get("slug") or topic_slug or "").strip()
    if draft_slug:
        existing = (
            await s.execute(
                select(Post).where(Post.topic_idea_id.is_(None), Post.slug == draft_slug)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    post = Post(slug=draft_slug, title=result.get("title", "") or "")
    s.add(post)
    await s.flush()
    return post


# ---------------------------------------------------------------------------
# Regeneration support
# ---------------------------------------------------------------------------


async def post_topic_and_dossier(post_id: int) -> tuple[TopicSuggestion, Path | None]:
    """Rebuild a post's TopicSuggestion and locate its newest dossier on disk."""
    async with session() as s:
        post = await s.get(Post, post_id)
        if post is None:
            raise KeyError(f"No post with id {post_id}")
        idea = (
            await s.get(TopicIdea, post.topic_idea_id) if post.topic_idea_id is not None else None
        )
        versions = (
            await s.execute(
                select(DraftVersion)
                .where(DraftVersion.post_id == post_id)
                .order_by(DraftVersion.version.desc())
            )
        ).scalars().all()

    topic = _topic_from_idea(idea) if idea is not None else _topic_from_post(post, versions)

    dossier_path: Path | None = None
    for v in versions:  # newest first
        if v.dossier_path and Path(v.dossier_path).exists():
            dossier_path = Path(v.dossier_path)
            break
    if dossier_path is None:
        dossier_path = _newest_dossier_for(topic.slug)
    return topic, dossier_path


async def prior_guidance(post_id: int) -> list[str]:
    """Every non-empty editor guidance already recorded for a post, oldest first.

    Each regeneration stores only the guidance that produced *that* version, so
    the running history has to be reassembled here. A later revision should keep
    honouring what earlier ones asked for — "shorter" plus "lead with the steps"
    plus the newest note — not silently drop the accumulated intent.
    """
    async with session() as s:
        versions = (
            await s.execute(
                select(DraftVersion)
                .where(DraftVersion.post_id == post_id)
                .order_by(DraftVersion.version.asc())
            )
        ).scalars().all()
    return [v.instructions.strip() for v in versions if v.instructions.strip()]


def accumulate_guidance(history: list[str], new_instructions: str) -> str:
    """Fold prior guidance and the new note into one instruction for the writer.

    The newest note is repeated last and flagged highest-priority so that, where
    two revisions conflict ("make it longer" then "make it shorter"), the writer
    follows the most recent. When there is no history this is just the new note.
    """
    new = new_instructions.strip()
    history = [h for h in history if h]
    if not history:
        return new
    lines = [
        "This post has been revised before. Apply the full history of editor "
        "guidance below. Where two items conflict, the most recent one wins.",
        "",
        "Guidance from previous revisions (oldest first):",
    ]
    lines += [f"- {h}" for h in history]
    if new:
        lines += ["", "New guidance for this revision (highest priority):", new]
    return "\n".join(lines)


def _topic_from_idea(idea: TopicIdea) -> TopicSuggestion:
    if idea.data:
        try:
            return TopicSuggestion.model_validate(idea.data)
        except Exception:  # noqa: BLE001 - fall back to the columns
            logger.warning("topic idea %s has an unparsable data blob; rebuilding from columns", idea.id)
    return TopicSuggestion(
        title=idea.title or idea.slug,
        slug=idea.slug,
        watch_area=idea.watch_area,
        post_format=idea.post_format or "analysis",
        primary_keyword=idea.primary_keyword,
        angle=idea.angle,
        problem_statement=idea.problem_statement,
        why_now=idea.why_now,
        key_questions=list(idea.key_questions or []),
        seed_sources=list(idea.seed_sources or []),
        novelty=idea.novelty,
        audience_fit=idea.audience_fit or 3,
        timeliness=idea.timeliness or 3,
        effort=idea.effort or 3,
        score=idea.score,
        duplicate_risk=idea.duplicate_risk,
    )


def _topic_from_post(post: Post, versions: list[DraftVersion]) -> TopicSuggestion:
    """A best-effort topic for a post with no linked idea (backfilled from files)."""
    from . import drafts as drafts_mod

    title = post.title or post.slug
    slug = post.slug
    keyword = ""
    post_format = "analysis"
    thesis = ""
    latest = versions[0] if versions else None
    if latest and latest.markdown_path:
        try:
            draft = drafts_mod.draft_to_model(Path(latest.markdown_path))
            title = draft.title or title
            slug = draft.slug or slug
            keyword = draft.primary_keyword
            post_format = draft.post_format or post_format
            # The argument the earlier version made, read back off the front
            # matter. Without it a regeneration hands the outliner a topic with an
            # empty angle, so it re-derives a thesis from the dossier and the same
            # research argues something different the second time round.
            thesis = draft.thesis
        except Exception:  # noqa: BLE001 - the file may be gone; use the columns
            pass
    return TopicSuggestion(
        title=title,
        slug=slug,
        watch_area="",
        post_format=post_format,
        primary_keyword=keyword,
        angle=thesis,
        problem_statement="",
        why_now="",
        novelty="",
        audience_fit=3,
        timeliness=3,
        effort=3,
        score=0.0,
    )


def _newest_dossier_for(slug: str) -> Path | None:
    if not slug:
        return None
    research_dir = get_settings().run.research_dir
    if not research_dir.exists():
        return None
    matches = sorted(research_dir.glob(f"*-{slug}.json"))
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# Read side — the API's filtered lists
# ---------------------------------------------------------------------------


async def list_topic_ideas(
    *,
    watch_area: str | None = None,
    post_format: str | None = None,
    has_draft: bool | None = None,
    min_score: float | None = None,
    q: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    async with session() as s:
        stmt = select(TopicIdea).order_by(TopicIdea.score.desc(), TopicIdea.created_at.desc())
        if watch_area:
            stmt = stmt.where(TopicIdea.watch_area == watch_area)
        if post_format:
            stmt = stmt.where(TopicIdea.post_format == post_format)
        if min_score is not None:
            stmt = stmt.where(TopicIdea.score >= min_score)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(
                func.lower(TopicIdea.title).like(like)
                | func.lower(TopicIdea.slug).like(like)
                | func.lower(TopicIdea.primary_keyword).like(like)
            )
        ideas = (await s.execute(stmt)).scalars().all()
        posts_by_idea = await _posts_by_idea(s, [i.id for i in ideas])

    out = []
    for idea in ideas:
        posts = posts_by_idea.get(idea.id, [])
        drafted = len(posts) > 0
        if has_draft is not None and has_draft != drafted:
            continue
        out.append(_idea_summary(idea, posts))
        if len(out) >= limit:
            break
    return out


async def get_topic_idea(idea_id: int) -> dict[str, Any] | None:
    async with session() as s:
        idea = await s.get(TopicIdea, idea_id)
        if idea is None:
            return None
        posts = await _posts_by_idea(s, [idea.id])
    data = _idea_summary(idea, posts.get(idea.id, []))
    data.update(
        {
            "angle": idea.angle,
            "problem_statement": idea.problem_statement,
            "why_now": idea.why_now,
            "novelty": idea.novelty,
            "key_questions": idea.key_questions or [],
            "seed_sources": idea.seed_sources or [],
            "data": idea.data or {},
            "suggest_run_id": idea.suggest_run_id,
        }
    )
    return data


async def list_posts(
    *,
    status: str | None = None,
    approved: bool | None = None,
    has_cover: bool | None = None,
    published: bool | None = None,
    q: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    async with session() as s:
        stmt = select(Post).order_by(Post.updated_at.desc())
        if status:
            stmt = stmt.where(Post.status == status)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(
                func.lower(Post.title).like(like) | func.lower(Post.slug).like(like)
            )
        posts = (await s.execute(stmt)).scalars().all()
        versions_by_post = await _versions_by_post(s, [p.id for p in posts])

    out = []
    for post in posts:
        versions = versions_by_post.get(post.id, [])
        current = _current_version(post, versions)
        if approved is not None and bool(current and current.approved) != approved:
            continue
        if has_cover is not None and bool(current and current.cover_path) != has_cover:
            continue
        if published is not None and (post.wordpress_post_id is not None) != published:
            continue
        out.append(_post_summary(post, versions, current))
        if len(out) >= limit:
            break
    return out


async def get_post(post_id: int) -> dict[str, Any] | None:
    async with session() as s:
        post = await s.get(Post, post_id)
        if post is None:
            return None
        versions = (
            await s.execute(
                select(DraftVersion)
                .where(DraftVersion.post_id == post_id)
                .order_by(DraftVersion.version.desc())
            )
        ).scalars().all()
        idea = (
            await s.get(TopicIdea, post.topic_idea_id) if post.topic_idea_id is not None else None
        )
    current = _current_version(post, versions)
    data = _post_summary(post, versions, current)
    data["topic_idea"] = _idea_summary(idea, []) if idea is not None else None
    data["versions"] = [_version_dict(v) for v in versions]
    return data


async def list_versions(post_id: int) -> list[dict[str, Any]]:
    async with session() as s:
        versions = (
            await s.execute(
                select(DraftVersion)
                .where(DraftVersion.post_id == post_id)
                .order_by(DraftVersion.version.desc())
            )
        ).scalars().all()
    return [_version_dict(v) for v in versions]


async def get_version(version_id: int) -> dict[str, Any] | None:
    async with session() as s:
        version = await s.get(DraftVersion, version_id)
    return _version_dict(version) if version is not None else None


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _idea_summary(idea: TopicIdea, posts: list[Post]) -> dict[str, Any]:
    return {
        "id": idea.id,
        "slug": idea.slug,
        "title": idea.title,
        "watch_area": idea.watch_area,
        "post_format": idea.post_format,
        "primary_keyword": idea.primary_keyword,
        "score": idea.score,
        "audience_fit": idea.audience_fit,
        "timeliness": idea.timeliness,
        "effort": idea.effort,
        "duplicate_risk": idea.duplicate_risk,
        "generated_on": idea.generated_on,
        "created_at": idea.created_at.isoformat() if idea.created_at else None,
        "has_draft": len(posts) > 0,
        "post_id": posts[0].id if posts else None,
        "posts": [{"id": p.id, "slug": p.slug, "status": p.status} for p in posts],
    }


def _post_summary(
    post: Post, versions: list[DraftVersion], current: DraftVersion | None
) -> dict[str, Any]:
    return {
        "id": post.id,
        "slug": post.slug,
        "title": post.title,
        "status": post.status,
        "topic_idea_id": post.topic_idea_id,
        "wordpress_post_id": post.wordpress_post_id,
        "edit_link": post.edit_link,
        "link": post.link,
        "version_count": len(versions),
        "current_version": _version_dict(current) if current is not None else None,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "updated_at": post.updated_at.isoformat() if post.updated_at else None,
    }


def _version_dict(v: DraftVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "post_id": v.post_id,
        "version": v.version,
        "write_run_id": v.write_run_id,
        "instructions": v.instructions,
        "reused_research": v.reused_research,
        "title": v.title,
        "slug": v.slug,
        "approved": v.approved,
        "score": v.score,
        "blockers": v.blockers,
        "markdown_path": v.markdown_path,
        "markdown_file": Path(v.markdown_path).name if v.markdown_path else "",
        "report_path": v.report_path,
        "cover_path": v.cover_path,
        "has_cover": bool(v.cover_path),
        "dossier_path": v.dossier_path,
        "wordpress_post_id": v.wordpress_post_id,
        "edit_link": v.edit_link,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


def _current_version(post: Post, versions: list[DraftVersion]) -> DraftVersion | None:
    if post.current_version_id is not None:
        for v in versions:
            if v.id == post.current_version_id:
                return v
    return versions[0] if versions else None  # versions are newest-first


async def _posts_by_idea(s: Any, idea_ids: list[int]) -> dict[int, list[Post]]:
    if not idea_ids:
        return {}
    rows = (
        await s.execute(select(Post).where(Post.topic_idea_id.in_(idea_ids)))
    ).scalars().all()
    grouped: dict[int, list[Post]] = {}
    for post in rows:
        grouped.setdefault(post.topic_idea_id, []).append(post)
    return grouped


async def _versions_by_post(s: Any, post_ids: list[int]) -> dict[int, list[DraftVersion]]:
    if not post_ids:
        return {}
    rows = (
        await s.execute(
            select(DraftVersion)
            .where(DraftVersion.post_id.in_(post_ids))
            .order_by(DraftVersion.version.desc())
        )
    ).scalars().all()
    grouped: dict[int, list[DraftVersion]] = {}
    for v in rows:
        grouped.setdefault(v.post_id, []).append(v)
    return grouped


# ---------------------------------------------------------------------------
# Backfill — reconcile existing runs and files on first start
# ---------------------------------------------------------------------------


async def backfill() -> None:
    """Catch the catalog up from existing runs and on-disk drafts. Idempotent.

    Safe to run on every boot: topic ideas dedupe by slug, write runs skip if a
    version already references their run id, and file drafts skip if a version
    already references their markdown path.
    """
    ideas = await _backfill_suggest_runs()
    versions = await _backfill_write_runs()
    files = await _backfill_draft_files()
    published = await _backfill_wordpress_ids()
    if ideas or versions or files or published:
        logger.info(
            "catalog backfill: %d topic idea(s), %d write run(s), %d file draft(s), "
            "%d WordPress id(s)",
            ideas,
            versions,
            files,
            published,
        )


async def _backfill_wordpress_ids() -> int:
    """Fill in post ids for drafts pushed before publishing recorded them.

    ``.ppn_state/wp_posts.json`` is the slug → post id map ``upsert_draft`` has
    written all along, on the same persistent mount as the drafts. Reading it
    back repairs every post published from the app before ``record_publish``
    existed — no network call, and it can only fill a blank, never overwrite an
    id that is already there.
    """
    from ..wordpress import remembered_posts

    known = remembered_posts()
    if not known:
        return 0

    settings = get_settings()
    site = settings.wordpress.url
    touched = 0
    async with session() as s:
        posts = (
            await s.execute(select(Post).where(Post.wordpress_post_id.is_(None)))
        ).scalars().all()
        for post in posts:
            versions = (
                await s.execute(select(DraftVersion).where(DraftVersion.post_id == post.id))
            ).scalars().all()
            # A post's own slug is the *idea's*; a regeneration can change the
            # draft's. Both are worth asking about, newest version first.
            candidates = [post.slug] + [v.slug for v in sorted(versions, key=lambda v: -v.version)]
            wp_id = next((known[c] for c in candidates if c and c in known), None)
            if wp_id is None:
                continue
            post.wordpress_post_id = wp_id
            if post.status == "draft":
                # Which of draft-on-WordPress or published it is, we cannot tell
                # from a local file. The next publish or cover push says.
                post.status = "wordpress_draft"
            if site and not post.edit_link:
                post.edit_link = f"{site}/wp-admin/post.php?post={wp_id}&action=edit"
            for v in versions:
                if v.wordpress_post_id is None and v.slug in known:
                    v.wordpress_post_id = known[v.slug]
            touched += 1
        await s.commit()
    return touched


async def _backfill_suggest_runs() -> int:
    async with session() as s:
        runs = (
            await s.execute(
                select(Run).where(Run.kind.in_(["suggest", "shortlist"]), Run.status == "succeeded")
            )
        ).scalars().all()
    touched = 0
    for run in runs:
        result = run.result or {}
        touched += await upsert_topic_ideas(
            run.id, result.get("suggestions", []), result.get("generated_on", "")
        )
    return touched


async def _backfill_write_runs() -> int:
    async with session() as s:
        runs = (
            await s.execute(
                select(Run)
                .where(Run.kind == "write", Run.status == "succeeded")
                .order_by(Run.queued_at)
            )
        ).scalars().all()
        seen = set(
            (
                await s.execute(
                    select(DraftVersion.write_run_id).where(DraftVersion.write_run_id.is_not(None))
                )
            ).scalars()
        )
    touched = 0
    for run in runs:
        if run.id in seen:
            continue
        if not run.result:
            continue
        await record_write_result(run.id, dict(run.params or {}), dict(run.result))
        touched += 1
    return touched


async def _backfill_draft_files() -> int:
    from . import drafts as drafts_mod

    items = drafts_mod.list_drafts()
    if not items:
        return 0

    async with session() as s:
        known_paths = set(
            (
                await s.execute(
                    select(DraftVersion.markdown_path).where(DraftVersion.markdown_path != "")
                )
            ).scalars()
        )
        known_files = {Path(p).name for p in known_paths if p}

    touched = 0
    output_dir = get_settings().run.output_dir
    for item in items:
        file_name = item.get("file", "")
        if not file_name or file_name in known_files:
            continue
        slug = item.get("slug") or Path(file_name).stem
        review = item.get("review") or {}
        cover = output_dir / "covers" / f"{slug}.png"
        markdown_path = str(output_dir / file_name)
        async with session() as s:
            idea = (
                await s.execute(select(TopicIdea).where(TopicIdea.slug == slug))
            ).scalar_one_or_none()
            post = await _find_or_create_post_by_slug(s, slug, item.get("title", ""), idea)
            next_version = (
                await s.execute(
                    select(func.max(DraftVersion.version)).where(DraftVersion.post_id == post.id)
                )
            ).scalar() or 0
            version = DraftVersion(
                post_id=post.id,
                write_run_id=None,
                version=next_version + 1,
                reused_research=False,
                title=item.get("title", "") or "",
                slug=slug,
                approved=bool(review.get("approved")),
                score=float(review.get("score") or 0.0),
                blockers=int(review.get("blockers") or 0),
                markdown_path=markdown_path,
                cover_path=str(cover) if cover.exists() else "",
            )
            s.add(version)
            await s.flush()
            post.current_version_id = version.id
            post.title = version.title or post.title
            post.updated_at = utcnow()
            await s.commit()
        touched += 1
    return touched


async def _find_or_create_post_by_slug(
    s: Any, slug: str, title: str, idea: TopicIdea | None
) -> Post:
    if idea is not None:
        existing = (
            await s.execute(select(Post).where(Post.topic_idea_id == idea.id))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        post = Post(topic_idea_id=idea.id, slug=slug, title=title or idea.title)
        s.add(post)
        await s.flush()
        return post
    existing = (
        await s.execute(
            select(Post).where(Post.topic_idea_id.is_(None), Post.slug == slug)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    post = Post(slug=slug, title=title)
    s.add(post)
    await s.flush()
    return post
