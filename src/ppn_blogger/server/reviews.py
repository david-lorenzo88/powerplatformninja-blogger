"""The human decision in the middle of an exploration run.

A wide-web sweep produces a list of sites; this module stores that list, takes
the operator's verdict, and files it back into ``sources.yaml`` as a new config
version. Filing it into the config — rather than into a per-run field — is the
whole point of the feature: a source approved once is trusted by every later
topic run *and* by the Researcher and Source Checker on every future draft.

The verdict is applied in one transaction-shaped order: write the config first,
then mark the review decided. A crash between the two leaves a review that can
be approved again idempotently; the reverse order would leave sources the
operator approved sitting in a "decided" review that never reached the config.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import desc, func, select

from ..models import ScoutReport, SourceDecision, SourceReviewSet
from ..sources import merge_into_yaml_text
from .db import SourceReview, session, utcnow

logger = logging.getLogger("ppn.server.reviews")

PENDING, APPROVED, CANCELLED = "pending", "approved", "cancelled"


async def create(run_id: str, review: SourceReviewSet) -> int:
    """Store a finished sweep and return the id the operator will act on."""
    async with session() as s:
        row = SourceReview(
            run_id=run_id,
            status=PENDING,
            instruction=review.instruction,
            generated_on=review.generated_on,
            candidates=[c.model_dump(mode="json") for c in review.candidates],
            reports=[r.model_dump(mode="json") for r in review.reports],
            created_at=utcnow(),
        )
        s.add(row)
        await s.commit()
        logger.info("source review %d created with %d candidates", row.id, len(review.candidates))
        return row.id


async def get(review_id: int) -> dict[str, Any] | None:
    async with session() as s:
        row = await s.get(SourceReview, review_id)
    return _to_dict(row, full=True) if row is not None else None


async def list_reviews(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    async with session() as s:
        stmt = select(SourceReview).order_by(desc(SourceReview.created_at)).limit(limit)
        if status:
            stmt = stmt.where(SourceReview.status == status)
        return [_to_dict(r) for r in (await s.execute(stmt)).scalars()]


async def pending_count() -> int:
    async with session() as s:
        result = await s.execute(
            select(func.count()).select_from(SourceReview).where(SourceReview.status == PENDING)
        )
        return int(result.scalar() or 0)


async def decide(review_id: int, decisions: list[SourceDecision]) -> dict[str, Any]:
    """Apply the operator's verdict: update ``sources.yaml``, close the review.

    Raises ``KeyError`` for an unknown review, ``ValueError`` for a review that
    is no longer pending, an unknown domain or an unknown trust tier.
    """
    from . import config_store

    async with session() as s:
        row = await s.get(SourceReview, review_id)
        if row is None:
            raise KeyError(f"No source review {review_id}")
        if row.status != PENDING:
            raise ValueError(f"Source review {review_id} was already {row.status}.")
        known = {str(c.get("domain", "")).lower() for c in (row.candidates or [])}
        instruction = row.instruction

    unknown = sorted({d.domain.lower() for d in decisions} - known)
    if unknown:
        raise ValueError(f"Not in this review: {', '.join(unknown)}")

    current = (await config_store.latest_versions()).get("sources")
    if current is None:
        raise ValueError("No sources config document to write to. Seed the config first.")

    merged = merge_into_yaml_text(current.content, decisions)
    version: int | None = None
    if merged.strip() != current.content.strip():
        approved = [d.domain for d in decisions if d.approved]
        saved = await config_store.save_document(
            "sources",
            merged,
            note=f"Source review {review_id}: approved {len(approved)} site(s)",
        )
        version = saved.version

    async with session() as s:
        row = await s.get(SourceReview, review_id)
        row.status = APPROVED
        row.decisions = [d.model_dump(mode="json") for d in decisions]
        row.config_version = version
        row.decided_at = utcnow()
        await s.commit()

    approved_domains = sorted({d.domain.lower() for d in decisions if d.approved})
    logger.info(
        "source review %d approved: %d of %d sites, sources.yaml v%s",
        review_id,
        len(approved_domains),
        len(decisions),
        version if version is not None else "unchanged",
    )
    return {
        "review_id": review_id,
        "approved": approved_domains,
        "declined": sorted({d.domain.lower() for d in decisions if not d.approved}),
        "config_version": version,
        "instruction": instruction,
    }


async def material(review_id: int) -> tuple[list[ScoutReport], list[str], str]:
    """The reports and approved sites the shortlist run works from."""
    async with session() as s:
        row = await s.get(SourceReview, review_id)
    if row is None:
        raise KeyError(f"No source review {review_id}")
    if row.status != APPROVED:
        raise ValueError(f"Source review {review_id} is {row.status}, not approved.")
    reports = [ScoutReport.model_validate(r) for r in (row.reports or [])]
    approved = [
        str(d.get("domain", "")).lower() for d in (row.decisions or []) if d.get("approved")
    ]
    return reports, [d for d in approved if d], row.instruction


async def attach_shortlist_run(review_id: int, run_id: str) -> None:
    async with session() as s:
        row = await s.get(SourceReview, review_id)
        if row is not None:
            row.shortlist_run_id = run_id
            await s.commit()


async def cancel(review_id: int) -> bool:
    async with session() as s:
        row = await s.get(SourceReview, review_id)
        if row is None or row.status != PENDING:
            return False
        row.status = CANCELLED
        row.decided_at = utcnow()
        await s.commit()
    return True


def _to_dict(row: SourceReview, full: bool = False) -> dict[str, Any]:
    candidates = row.candidates or []
    out: dict[str, Any] = {
        "id": row.id,
        "run_id": row.run_id,
        "status": row.status,
        "instruction": row.instruction,
        "generated_on": row.generated_on,
        "candidate_count": len(candidates),
        "new_count": sum(1 for c in candidates if not c.get("known")),
        # Distinct pages, not raw scout hits: three scouts finding the same URL is
        # one thing to read, and the per-site counts must add up to this number.
        "signal_count": sum(int(c.get("item_count") or 0) for c in candidates),
        "config_version": row.config_version,
        "shortlist_run_id": row.shortlist_run_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }
    if full:
        # The raw reports are deliberately not returned: they are several
        # thousand tokens of scout output the operator has no use for, and the
        # candidates already carry every item, grouped by the site it came from.
        out["candidates"] = candidates
        out["decisions"] = row.decisions or []
        # The tier menu travels with the review so the UI never hard-codes a
        # list that lives in sources.yaml.
        from ..settings import get_settings

        out["tiers"] = [
            {"id": tier_id, "label": tier.get("label", tier_id), "score": tier.get("score", 3)}
            for tier_id, tier in get_settings().trust_tiers.items()
        ]
    return out
