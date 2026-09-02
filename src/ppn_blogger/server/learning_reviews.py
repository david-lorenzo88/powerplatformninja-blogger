"""The author's verdict on what the crew should learn.

Mirrors ``discovery.py`` — the same three states, the same error contract
(``KeyError`` for an unknown review, ``ValueError`` for one already decided or a
decision naming something that was not offered), and the same crash-safe
ordering: **write the configuration first, then close the review**. A crash
between the two leaves a review that can be decided again, which costs a second
click; the other order leaves a decided review that changed nothing, which is
silent and unrecoverable.

This is the only module in the feature that may call
``config_store.save_document``. ``learning.py`` cannot reach it, and a test
enforces that, because "nothing auto-applies" has to be a property of the code
rather than a promise about how it is called.

A refusal is remembered by fingerprint in ``declined_learnings``, a table of its
own rather than a status on the candidate: the author goes on making the same
edit — they still prefer it that way — so the cluster keeps accruing evidence and
a status field would be overwritten by the next aggregation pass. The refusal has
to outlive the row it refused.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import select

from .db import DeclinedLearning, LearningCandidate, LearningReview, as_utc, session, utcnow

logger = logging.getLogger("ppn.learning.reviews")

PENDING, APPROVED, CANCELLED = "pending", "approved", "cancelled"


async def create(run_id: str | None, proposals: list[dict[str, Any]], candidate_ids: list[int]) -> int:
    async with session() as s:
        row = LearningReview(
            run_id=run_id,
            status=PENDING,
            generated_on=date.today().isoformat(),
            candidate_ids=list(candidate_ids),
            proposals=list(proposals),
            created_at=utcnow(),
        )
        s.add(row)
        await s.commit()
        review_id = row.id

    async with session() as s:
        for candidate_id in candidate_ids:
            candidate = await s.get(LearningCandidate, candidate_id)
            if candidate is not None:
                candidate.review_id = review_id
        await s.commit()
    return review_id


async def get(review_id: int) -> dict[str, Any] | None:
    async with session() as s:
        row = await s.get(LearningReview, review_id)
        return _to_dict(row, full=True) if row else None


async def list_reviews(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    async with session() as s:
        query = select(LearningReview).order_by(LearningReview.created_at.desc()).limit(limit)
        if status:
            query = query.where(LearningReview.status == status)
        rows = (await s.execute(query)).scalars().all()
        return [_to_dict(r) for r in rows]


async def pending_count() -> int:
    from sqlalchemy import func

    async with session() as s:
        return int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(LearningReview)
                    .where(LearningReview.status == PENDING)
                )
            ).scalar()
            or 0
        )


async def decide(review_id: int, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply what the author approved, then close the review.

    Each decision is ``{"fingerprint": ..., "approved": bool, "reason": ...}``.
    A fingerprint that was not offered in this review is refused outright — the
    same guard the source and feed reviews carry, so a stale screen or a replayed
    request cannot apply something the author never saw.
    """
    from .. import config_edit
    from . import config_store

    async with session() as s:
        row = await s.get(LearningReview, review_id)
        if row is None:
            raise KeyError(f"No learning review {review_id}")
        if row.status != PENDING:
            raise ValueError(f"Learning review {review_id} was already {row.status}.")
        proposals = list(row.proposals or [])

    offered = {str(p.get("fingerprint", "")) for p in proposals}
    named = {str(d.get("fingerprint", "")) for d in decisions}
    unknown = sorted(named - offered)
    if unknown:
        raise ValueError(f"Not in this review: {', '.join(unknown)}")

    by_fingerprint = {str(p.get("fingerprint", "")): p for p in proposals}
    applied: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []

    for decision in decisions:
        fingerprint = str(decision.get("fingerprint", ""))
        proposal = by_fingerprint.get(fingerprint)
        if proposal is None:
            continue
        if not decision.get("approved"):
            refused.append({**proposal, "reason": str(decision.get("reason", ""))})
            continue

        document = str(proposal.get("document", ""))
        # Re-checked here and not only at render time: the review row outlives a
        # restart, and a redeploy between filing and approving could have changed
        # what is writable.
        if document not in config_edit.WRITABLE_DOCUMENTS:
            logger.warning("refusing to write %s from a learning review", document)
            refused.append({**proposal, "reason": f"{document} is not writable by the learner"})
            continue

        saved = await config_store.save_document(
            document,
            str(proposal.get("content", "")),
            note=f"Learning review {review_id}: {str(proposal.get('summary', ''))[:200]}",
        )
        applied.append(
            {
                "document": document,
                "version": saved.version,
                "fingerprint": fingerprint,
                "rule_id": proposal.get("rule_id", ""),
                "summary": proposal.get("summary", ""),
            }
        )

    if refused:
        await _decline(refused)

    async with session() as s:
        row = await s.get(LearningReview, review_id)
        row.status = APPROVED
        row.decisions = list(decisions)
        row.applied = applied
        row.applied_count = len(applied)
        row.decided_at = utcnow()

        for entry in applied:
            candidate = await _candidate(s, entry["fingerprint"])
            if candidate is not None:
                candidate.status = "applied"
                candidate.config_version = entry["version"]
        for entry in refused:
            candidate = await _candidate(s, str(entry.get("fingerprint", "")))
            if candidate is not None:
                candidate.status = "declined"
        await s.commit()

    logger.info(
        "learning review %s: applied %s change(s), declined %s", review_id, len(applied), len(refused)
    )
    return {
        "review_id": review_id,
        "applied": applied,
        "declined": len(refused),
    }


async def cancel(review_id: int) -> bool:
    async with session() as s:
        row = await s.get(LearningReview, review_id)
        if row is None or row.status != PENDING:
            return False
        row.status = CANCELLED
        row.decided_at = utcnow()
        await s.commit()
        return True


async def _candidate(s: Any, fingerprint: str) -> LearningCandidate | None:
    if not fingerprint:
        return None
    return (
        await s.execute(
            select(LearningCandidate).where(LearningCandidate.fingerprint == fingerprint)
        )
    ).scalar_one_or_none()


async def _decline(proposals: list[dict[str, Any]]) -> None:
    """Remember a refusal, so a later run never offers the same pattern again."""
    async with session() as s:
        known = set((await s.execute(select(DeclinedLearning.fingerprint))).scalars().all())
        for proposal in proposals:
            fingerprint = str(proposal.get("fingerprint", ""))
            if not fingerprint or fingerprint in known:
                continue
            known.add(fingerprint)
            s.add(
                DeclinedLearning(
                    fingerprint=fingerprint,
                    edit_kind=str(proposal.get("edit_kind", ""))[:32],
                    target=str(proposal.get("target", ""))[:32],
                    label=str(proposal.get("label", ""))[:300],
                    reason=str(proposal.get("reason", ""))[:200],
                    created_at=utcnow(),
                )
            )
        await s.commit()


async def list_declined(limit: int = 100) -> list[dict[str, Any]]:
    async with session() as s:
        rows = (
            await s.execute(
                select(DeclinedLearning).order_by(DeclinedLearning.created_at.desc()).limit(limit)
            )
        ).scalars().all()
        return [
            {
                "fingerprint": r.fingerprint,
                "edit_kind": r.edit_kind,
                "target": r.target,
                "label": r.label,
                "reason": r.reason,
                "created_at": _iso(r.created_at),
            }
            for r in rows
        ]


def _to_dict(row: LearningReview, *, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "run_id": row.run_id,
        "status": row.status,
        "generated_on": row.generated_on,
        "proposal_count": len(row.proposals or []),
        "applied_count": row.applied_count,
        "created_at": _iso(row.created_at),
        "decided_at": _iso(row.decided_at),
    }
    if full:
        out["proposals"] = row.proposals or []
        out["decisions"] = row.decisions or []
        out["applied"] = row.applied or []
        out["candidate_ids"] = row.candidate_ids or []
    return out


def _iso(value: Any) -> str | None:
    moment = as_utc(value)
    return moment.isoformat() if moment else None
