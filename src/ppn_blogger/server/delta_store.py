"""Capturing what the crew wrote and what the author published.

Two moments, both of which already exist as call sites, so nothing new has to be
instrumented and nothing can be missed by forgetting to call it:

* when a write run finishes, ``catalog.record_write_result`` has just written the
  draft file and knows where it is — the body is pristine at that instant;
* when the author publishes from the Drafts screen, ``catalog.record_publish``
  resolves the same draft version, and the file now holds the edited text.

The baseline has to be *copied* rather than read later, because
``server/drafts.py:write_draft`` rewrites the body in place. The crew's original
is destroyed by the author's first save, which is exactly the evidence this whole
feature exists to keep.

Everything here swallows and logs. A pair is bookkeeping about a draft; losing
one costs a data point, and letting it raise would cost the publish it was
attached to. Same doctrine as the cost ledger and the WordPress push.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .. import delta
from ..models import DeltaAnalysis
from ..storage import split_front_matter
from .db import DeltaObservation, DeltaPair, DraftVersion, Post, as_utc, session, utcnow

logger = logging.getLogger("ppn.delta")

AWAITING, CAPTURED, ANALYSED, DISCARDED = "awaiting_final", "captured", "analysed", "discarded"


async def _config_versions() -> dict[str, int | None]:
    """Which version of each document the run was built from.

    Read here rather than taken from ``Run.config_version``: that column is a
    String(64) holding a 95-character token, so it is truncated mid-name and
    cannot answer which ruleset a draft was written under.
    """
    from . import config_store

    try:
        latest = await config_store.latest_versions()
    except Exception:  # noqa: BLE001 - provenance is worth less than the pair
        logger.debug("could not read config versions for a delta pair", exc_info=True)
        return {}
    return {
        "validation_version": getattr(latest.get("validation_rules"), "version", None),
        "style_guide_version": getattr(latest.get("style_guide"), "version", None),
        "profile_version": getattr(latest.get("blog_profile"), "version", None),
    }


def _body(path_text: str) -> str:
    path = Path(path_text)
    if not path_text or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


async def capture_baseline(version_id: int, result: dict[str, Any]) -> int | None:
    """Snapshot the draft as the crew wrote it. Returns the pair id, or None.

    Idempotent on ``draft_version_id``: a regenerate produces a new version and
    therefore a new pair, but re-recording the same version does not file a
    second opinion about the same draft.
    """
    markdown_path = str(result.get("markdown_path") or "")
    text = _body(markdown_path)
    if not text:
        logger.info("no draft file at %s; no baseline captured", markdown_path or "(none)")
        return None

    async with session() as s:
        existing = (
            await s.execute(select(DeltaPair.id).where(DeltaPair.draft_version_id == version_id))
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)
        version = await s.get(DraftVersion, version_id)
        if version is None:
            return None
        post_id, run_id = version.post_id, version.write_run_id
        slug, title = version.slug, version.title
        wp_id = version.wordpress_post_id

    front, _ = split_front_matter(text)
    versions = await _config_versions()

    async with session() as s:
        pair = DeltaPair(
            post_id=post_id,
            draft_version_id=version_id,
            run_id=run_id,
            wordpress_post_id=wp_id,
            slug=slug or str(front.get("slug", "")),
            title=(title or str(front.get("title", "")))[:300],
            language=str(front.get("language", "") or _blog_language()),
            status=AWAITING,
            agent_text=text,
            created_at=utcnow(),
            **versions,
        )
        s.add(pair)
        await s.commit()
        logger.info("delta baseline captured for draft version %s", version_id)
        return pair.id


def _blog_language() -> str:
    from ..settings import get_settings

    return get_settings().language


async def capture_final(markdown_file: str, *, source: str = "in_app") -> int | None:
    """Record the published text against its baseline. Returns the pair id.

    Called on publish. A second publish after further editing updates the final
    text and re-opens the pair, because the newest published version is the one
    that says what the author actually wanted.
    """
    if not markdown_file:
        return None
    async with session() as s:
        rows = (
            await s.execute(select(DraftVersion).where(DraftVersion.markdown_path != ""))
        ).scalars().all()
        version = next((v for v in rows if Path(v.markdown_path).name == markdown_file), None)
        if version is None:
            return None
        pair = (
            await s.execute(
                select(DeltaPair).where(DeltaPair.draft_version_id == version.id)
            )
        ).scalar_one_or_none()
        if pair is None:
            logger.info("no baseline for %s; nothing to compare against", markdown_file)
            return None
        path_text = version.markdown_path
        agent_text = pair.agent_text
        pair_id = pair.id

    final_text = _body(path_text)
    if not final_text:
        return None

    scored = delta.score(agent_text, final_text)

    async with session() as s:
        pair = await s.get(DeltaPair, pair_id)
        if pair is None:
            return None
        pair.final_text = final_text
        pair.capture_source = source
        pair.edit_rate = scored.edit_rate_permille
        pair.overlap = int(round(scored.overlap * 1000))
        pair.changed_blocks = scored.changed_blocks
        pair.total_blocks = scored.total_blocks
        pair.identical = scored.identical
        pair.diff = scored.as_dict()
        pair.status = CAPTURED
        pair.captured_at = utcnow()
        # A re-publish is new evidence about the same draft, so any earlier
        # reading of it is stale. Dropping the observations rather than adding to
        # them keeps a pair's contribution to a cluster at exactly one opinion.
        pair.analysis = None
        pair.analysed_at = None
        await s.execute(
            DeltaObservation.__table__.delete().where(DeltaObservation.pair_id == pair_id)
        )
        await s.commit()

    logger.info(
        "delta captured for %s: %s%% of words changed across %s of %s blocks",
        markdown_file,
        round(scored.edit_rate * 100, 1),
        scored.changed_blocks,
        scored.total_blocks,
    )
    return pair_id


async def record_analysis(pair_id: int, analysis: DeltaAnalysis, language: str) -> int:
    """Store the analyst's observations. Returns how many were kept.

    Two things are dropped here rather than downstream, because both are about
    this pair and nothing later has the context to judge them: a pair the analyst
    marked as a rushed pass, and any observation aimed at ``none`` — a fact the
    post got wrong is a research failure, not a habit worth a rule.
    """
    async with session() as s:
        pair = await s.get(DeltaPair, pair_id)
        if pair is None:
            return 0
        post_id = pair.post_id
        pair.analysis = analysis.model_dump(mode="json")
        pair.status = ANALYSED
        pair.analysed_at = utcnow()

        kept = 0
        if not analysis.one_off:
            for observation in analysis.observations:
                if observation.target == "none":
                    continue
                s.add(
                    DeltaObservation(
                        pair_id=pair_id,
                        post_id=post_id,
                        edit_kind=observation.edit_kind,
                        target=observation.target,
                        language=language,
                        signature=observation.signature[:200],
                        fingerprint=delta.fingerprint(
                            observation.edit_kind, observation.target, language, observation.signature
                        ),
                        before_text=observation.before,
                        after_text=observation.after,
                        rationale=observation.rationale,
                        confidence=observation.confidence,
                    )
                )
                kept += 1
        await s.commit()
    return kept


async def discard(pair_id: int, reason: str) -> None:
    """Park a pair the loop must not learn from, with the reason kept."""
    async with session() as s:
        pair = await s.get(DeltaPair, pair_id)
        if pair is None:
            return
        pair.status = DISCARDED
        pair.discard_reason = reason[:200]
        await s.commit()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def pending_analysis(limit: int) -> list[dict[str, Any]]:
    """Captured pairs not yet analysed, newest first."""
    async with session() as s:
        rows = (
            await s.execute(
                select(DeltaPair)
                .where(DeltaPair.status == CAPTURED)
                .order_by(DeltaPair.captured_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "id": r.id,
                "post_id": r.post_id,
                "slug": r.slug,
                "title": r.title,
                "language": r.language,
                "identical": bool(r.identical),
                "edit_rate": r.edit_rate / 1000,
                "agent_text": r.agent_text,
                "final_text": r.final_text,
                "diff": r.diff or {},
            }
            for r in rows
        ]


async def unanalysed_count() -> int:
    """Drives the scheduler's ``applies`` predicate.

    A system with nothing to learn from must not wake the database on a cadence:
    Azure SQL is serverless with a sixty-second auto-pause, and a job that exists
    only to find nothing to do is precisely the cost the scheduler is shaped to
    avoid.
    """
    async with session() as s:
        return int(
            (
                await s.execute(
                    select(func.count()).select_from(DeltaPair).where(DeltaPair.status == CAPTURED)
                )
            ).scalar()
            or 0
        )


async def corpus(limit: int = 400) -> list[dict[str, str]]:
    """Every pair the gate tests a proposal against.

    ``final_text`` is the golden set — what the author actually shipped. A
    proposed rule that fires on any of it is a false positive by definition, and
    that check is the whole reason this feature is self-learning rather than
    self-suggesting.
    """
    async with session() as s:
        rows = (
            await s.execute(
                select(DeltaPair)
                .where(DeltaPair.status.in_([CAPTURED, ANALYSED]))
                .order_by(DeltaPair.captured_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "id": r.id,
                "slug": r.slug,
                "agent": delta.normalise(r.agent_text),
                "final": delta.normalise(r.final_text),
            }
            for r in rows
        ]


async def observations_for(fingerprints: list[str] | None = None) -> list[dict[str, Any]]:
    async with session() as s:
        query = select(DeltaObservation)
        if fingerprints:
            query = query.where(DeltaObservation.fingerprint.in_(fingerprints))
        rows = (await s.execute(query)).scalars().all()
        return [
            {
                "id": r.id,
                "pair_id": r.pair_id,
                "post_id": r.post_id,
                "edit_kind": r.edit_kind,
                "target": r.target,
                "language": r.language,
                "signature": r.signature,
                "fingerprint": r.fingerprint,
                "before": r.before_text,
                "after": r.after_text,
                "rationale": r.rationale,
                "confidence": r.confidence,
            }
            for r in rows
        ]


async def list_pairs(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    async with session() as s:
        query = select(DeltaPair).order_by(DeltaPair.created_at.desc()).limit(limit)
        if status:
            query = query.where(DeltaPair.status == status)
        rows = (await s.execute(query)).scalars().all()
        return [_summary(r) for r in rows]


async def get_pair(pair_id: int) -> dict[str, Any] | None:
    async with session() as s:
        pair = await s.get(DeltaPair, pair_id)
        if pair is None:
            return None
        post = await s.get(Post, pair.post_id) if pair.post_id else None
        observations = (
            await s.execute(
                select(DeltaObservation)
                .where(DeltaObservation.pair_id == pair_id)
                .order_by(DeltaObservation.id)
            )
        ).scalars().all()
        return {
            **_summary(pair),
            "agent_text": pair.agent_text,
            "final_text": pair.final_text,
            "diff": pair.diff or {},
            "wordpress_post_id": pair.wordpress_post_id,
            "edit_link": getattr(post, "edit_link", ""),
            "link": getattr(post, "link", ""),
            "config": {
                "validation_rules": pair.validation_version,
                "style_guide": pair.style_guide_version,
                "blog_profile": pair.profile_version,
            },
            "observations": [
                {
                    "id": o.id,
                    "edit_kind": o.edit_kind,
                    "target": o.target,
                    "signature": o.signature,
                    "before": o.before_text,
                    "after": o.after_text,
                    "rationale": o.rationale,
                    "confidence": o.confidence,
                    "fingerprint": o.fingerprint,
                }
                for o in observations
            ],
        }


def _summary(pair: DeltaPair) -> dict[str, Any]:
    return {
        "id": pair.id,
        "post_id": pair.post_id,
        "draft_version_id": pair.draft_version_id,
        "run_id": pair.run_id,
        "slug": pair.slug,
        "title": pair.title,
        "language": pair.language,
        "status": pair.status,
        "capture_source": pair.capture_source,
        # Reported as a fraction; stored per mille so no float crosses the seam.
        "edit_rate": pair.edit_rate / 1000,
        "overlap": pair.overlap / 1000,
        "changed_blocks": pair.changed_blocks,
        "total_blocks": pair.total_blocks,
        "identical": bool(pair.identical),
        "discard_reason": pair.discard_reason,
        "created_at": _iso(pair.created_at),
        "captured_at": _iso(pair.captured_at),
        "analysed_at": _iso(pair.analysed_at),
    }


def _iso(value: Any) -> str | None:
    moment = as_utc(value)
    return moment.isoformat() if moment else None


async def metrics() -> dict[str, Any]:
    """The numbers the spec asks for, all of them arithmetic over stored pairs.

    ``clean_rate`` — the share of posts published untouched — leads, because it
    is more sensitive than the mean and it is the one an author recognises. No
    model is involved in any figure here, which is the point: an LLM quality
    score would be the thing to optimise, and optimising it is how a loop like
    this learns to game itself.
    """
    async with session() as s:
        rows = (
            await s.execute(
                select(DeltaPair)
                .where(DeltaPair.status.in_([CAPTURED, ANALYSED]))
                .order_by(DeltaPair.captured_at.desc())
            )
        ).scalars().all()
        awaiting = int(
            (
                await s.execute(
                    select(func.count()).select_from(DeltaPair).where(DeltaPair.status == AWAITING)
                )
            ).scalar()
            or 0
        )
        discarded = int(
            (
                await s.execute(
                    select(func.count()).select_from(DeltaPair).where(DeltaPair.status == DISCARDED)
                )
            ).scalar()
            or 0
        )

    rates = [r.edit_rate / 1000 for r in rows]
    recent = rows[:20]
    return {
        "pairs": len(rows),
        "awaiting_final": awaiting,
        "discarded": discarded,
        "clean_rate": (sum(1 for r in rows if r.identical) / len(rows)) if rows else 0.0,
        "mean_edit_rate": (sum(rates) / len(rates)) if rates else 0.0,
        "discard_rate": (discarded / (len(rows) + discarded)) if (rows or discarded) else 0.0,
        "trend": [
            {"slug": r.slug, "at": _iso(r.captured_at), "edit_rate": r.edit_rate / 1000}
            for r in reversed(recent)
        ],
        "by_section": delta.by_section(
            [
                delta.DeltaScore(
                    hunks=[delta.Hunk(**h) for h in (r.diff or {}).get("hunks", [])]
                )
                for r in rows
            ]
        ),
    }
