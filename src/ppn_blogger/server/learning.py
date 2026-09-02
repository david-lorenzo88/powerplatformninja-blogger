"""Turning captured edits into reviewable configuration changes.

Shaped like ``discovery.py``, and for the same reason: a model is asked where to
look, and code decides what the operator is allowed to see. There the check is
"does this URL actually resolve to a feed"; here it is stronger, because the
evidence needed to test a proposal is already in hand.

The gate is the point of the module. A proposed rule is run against every draft
this crew has written **and** against every version the author published. What
the author published is the golden set by definition — they shipped it — so a
rule that fires on any of it would flag the finished article, and is discarded
before a human ever spends attention on it. That single check is what makes the
loop self-learning rather than self-suggesting.

Three things stay in code, never in a prompt: which shape of proposal a pattern
gets, which rule id is allocated, and whether anything passes. The model is asked
only to fill in fields.

Nothing here can write configuration. ``config_store.save_document`` is reachable
only from ``learning_reviews.decide``, after a human has said yes, and a test
greps this module to keep it that way.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select

from .. import config_edit, delta, detectors
from ..config_edit import Refusal
from ..models import DeltaAnalysis, LearningProposal
from ..settings import Settings, get_settings
from ..util import parse_model, user_message
from . import delta_store
from .db import DeclinedLearning, LearningCandidate, session, utcnow

logger = logging.getLogger("ppn.learning")

# Which shape of proposal a pattern earns. Code's decision, from the closed
# target vocabulary the analyst chose from — the same doctrine as the newsletter
# editor being handed article ids rather than asked for URLs.
_SHAPE_FOR: dict[str, str] = {
    "voice_rule": "rule",
    "typography_rule": "rule",
    "content_rule": "rule",
    "structure_rule": "rule",
    "seo_rule": "rule",
    "focus_rule": "rule",
    "style_guide": "style_note",
    "blog_profile_structure": "profile_scalar",
    "writer_guidance": "guidance",
}

_GROUP_FOR: dict[str, str] = {
    "voice_rule": "voice_rules",
    "typography_rule": "typography_rules",
    "content_rule": "content_rules",
    "structure_rule": "structure_rules",
    "seo_rule": "seo_rules",
    "focus_rule": "focus_rules",
}

# How many published posts the regression check reads. Compiling the whole
# ruleset is not cached inside `compute_measurements`, so this is the difference
# between a second and a minute; the finals are near-identical in the ways that
# matter to other rules, so a sample answers the question.
_REGRESSION_SAMPLE = 12


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GateReport:
    """What the deterministic check measured. Shown above the diff, not below."""

    status: str = "skipped"  # passed | failed | skipped
    reason: str = ""
    evidence_hits: int = 0
    draft_hits: int = 0
    final_hits: int = 0
    drafts: int = 0
    finals: int = 0
    regressions: int = 0
    replay: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _scan(payload: tuple[str, list[str]]) -> list[int]:
    """Indices of the texts a pattern matches. Runs in a *separate process*.

    Separate because Python's ``re`` has no timeout and a catastrophic pattern
    cannot be interrupted: a thread running one cannot be killed, and a worker
    stuck on it takes the container's readiness probe down with it. A process
    can be killed.
    """
    pattern, texts = payload
    compiled = re.compile(pattern)
    return [i for i, text in enumerate(texts) if compiled.search(text)]


async def _scan_safely(pattern: str, texts: list[str], timeout: float) -> list[int] | None:
    """Run ``_scan`` under a wall-clock ceiling. None means it ran too long."""
    if not texts:
        return []
    loop = asyncio.get_running_loop()
    # "spawn" explicitly, rather than the platform default. Linux defaults to
    # fork, and forking a process that is already running an event loop and a
    # thread pool copies locks in whatever state they happened to be in — a
    # deadlock that would appear only in the container, never on a Mac, where
    # spawn is already the default. A fresh interpreter costs a second or two on
    # a weekly job, which is nothing next to that.
    pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(pool, _scan, (pattern, texts)), timeout=timeout
        )
    except TimeoutError:
        logger.warning("a proposed detector exceeded %ss and was discarded", timeout)
        return None
    except Exception:  # noqa: BLE001 - a broken pattern is a discard, not a crash
        logger.warning("a proposed detector failed to run", exc_info=True)
        return None
    finally:
        # shutdown(wait=False) leaves a spinning child alive, which is the whole
        # failure being designed out, so the processes are killed explicitly.
        for process in list(getattr(pool, "_processes", {}).values()):
            if process.is_alive():
                process.kill()
        pool.shutdown(wait=False, cancel_futures=True)


def _settings_with(documents: dict[str, Any]) -> Settings:
    """A Settings reading the proposed documents, and nothing global.

    An instance field rather than ``set_config_source``. ``config_store._source``
    is a module singleton that ``save_document`` mutates in place, so swapping it
    would let an operator's ordinary config edit change the configuration this
    check is running under — and every concurrent run's along with it.
    """
    from ..config_source import DOCUMENTS, MappingConfigSource, get_config_source

    active = get_config_source()
    merged: dict[str, Any] = {}
    for name, fmt in DOCUMENTS.items():
        if name in documents:
            merged[name] = documents[name]
        else:
            merged[name] = active.get_text(name) if fmt == "markdown" else active.get_mapping(name)
    source = MappingConfigSource(merged, version=f"proposed-{id(documents)}")
    return Settings(source=source)


def _rule_ids(text: str, settings: Settings) -> set[str]:
    run = detectors.run_detectors(
        text,
        groups=Settings.CONTENT_GROUPS + Settings.DESIGN_GROUPS,
        settings=settings,
    )
    return {finding.rule_id for finding in run.findings}


async def gate(
    proposal: LearningProposal,
    *,
    rule_id: str,
    documents: dict[str, Any],
    evidence_slugs: set[str],
    corpus: list[dict[str, str]],
    settings: Settings,
) -> GateReport:
    """Test a proposal against history. It can only reject.

    Four floors, and the second is the one that matters:

    1. it fires on the drafts that motivated it — otherwise it does not describe
       the pattern it claims to;
    2. **it fires on nothing the author published** — those are finished articles,
       so a hit there is a false positive by construction;
    3. there is enough published work to have tested it against at all;
    4. no *other* rule's verdict changes on any published post, which catches a
       structure number quietly rewriting how every past draft would be scored.
    """
    report = GateReport(drafts=len(corpus), finals=len(corpus))
    minimum = settings.learning.min_distinct_posts

    if len(corpus) < minimum:
        report.reason = (
            f"only {len(corpus)} published post(s) to test against; "
            f"{minimum} are needed before anything is proposed"
        )
        return report

    if proposal.kind == "rule" and proposal.detector:
        prose_scoped = bool(proposal.prose_scoped)
        drafts = [detectors.prose_only(p["agent"]) if prose_scoped else p["agent"] for p in corpus]
        finals = [detectors.prose_only(p["final"]) if prose_scoped else p["final"] for p in corpus]
        timeout = settings.learning.regex_timeout_seconds

        draft_hits = await _scan_safely(proposal.detector, drafts, timeout)
        if draft_hits is None:
            report.status = "failed"
            report.reason = "the detector took too long to run and was discarded"
            return report
        final_hits = await _scan_safely(proposal.detector, finals, timeout)
        if final_hits is None:
            report.status = "failed"
            report.reason = "the detector took too long to run and was discarded"
            return report

        report.draft_hits = len(draft_hits)
        report.final_hits = len(final_hits)
        report.evidence_hits = sum(1 for i in draft_hits if corpus[i]["slug"] in evidence_slugs)

        if report.final_hits:
            report.status = "failed"
            report.reason = (
                f"fires on {report.final_hits} post(s) the author published — "
                "it would flag finished work"
            )
            return report
        if not report.draft_hits:
            report.status = "failed"
            report.reason = "fires on none of the drafts it was meant to describe"
            return report

    # Floor 4. Cheap for a style note (no detector reads it) and the whole point
    # for a structure number, which the computed rules S02/C04/F03-F05 read.
    if proposal.kind in ("rule", "profile_scalar"):
        proposed = _settings_with(documents)
        regressions = 0
        for pair in corpus[:_REGRESSION_SAMPLE]:
            before = _rule_ids(pair["final"], settings)
            after = _rule_ids(pair["final"], proposed)
            regressions += len(after - before - {rule_id})
        report.regressions = regressions
        if regressions:
            report.status = "failed"
            report.reason = (
                f"changes {regressions} other rule verdict(s) on posts already published"
            )
            return report

    if proposal.kind == "rule" and not proposal.detector:
        report.status = "skipped"
        report.reason = (
            "no detector, so there is nothing to measure. The evidence is the "
            "recurring edits below."
        )
        return report
    if proposal.kind in ("style_note", "guidance"):
        report.status = "skipped"
        report.reason = (
            "prose guidance cannot be tested mechanically. The evidence is the "
            "recurring edits below."
        )
        return report

    report.status = "passed"
    report.reason = (
        f"fires on {report.draft_hits} draft(s) and on none of the "
        f"{report.finals} posts you published"
    )
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Rendered:
    document: str
    content: str
    rule_id: str = ""
    documents: dict[str, Any] = field(default_factory=dict)


def render(proposal: LearningProposal, cluster: delta.Cluster, settings: Settings) -> Rendered | Refusal:
    """Turn a typed proposal into the actual document text. Code's job entirely.

    Every refusal here is a discard with a reason, never a fallback: writing
    configuration nobody predicted is worse than writing none.
    """
    import yaml


    denied = config_edit.check_content(
        proposal.summary, proposal.rule_text, proposal.note_markdown, proposal.guidance_text
    )
    if denied is not None:
        return denied

    if proposal.kind == "rule":
        group = _GROUP_FOR.get(cluster.target, proposal.rule_group)
        if proposal.detector:
            unsafe = config_edit.check_detector(proposal.detector)
            if unsafe is not None:
                return unsafe
        text = _document_text("validation_rules")
        if text is None:
            return Refusal("validation_rules is not in the config store yet")
        rule_id = config_edit.next_rule_id(settings.all_rules(), group)
        if rule_id is None:
            return Refusal(f"no free rule id left in {group}")
        rule = {
            "id": rule_id,
            "rule": proposal.rule_text,
            "severity": proposal.severity,
            # `auto` is set by code, never by the model: a rule marked auto with
            # no detector behind it is checked by nobody, which is the exact hole
            # the ruleset's own header documents.
            "auto": bool(proposal.detector),
            "detector": proposal.detector,
            "prose_only": bool(proposal.prose_scoped),
            "check_hint": proposal.check_hint,
            "fix_hint": proposal.fix_hint,
        }
        result = config_edit.append_rule(text, group, rule)
        if isinstance(result, Refusal):
            return result
        return Rendered(
            "validation_rules", result, rule_id, {"validation_rules": yaml.safe_load(result) or {}}
        )

    if proposal.kind == "style_note":
        text = _document_text("style_guide")
        if text is None:
            return Refusal("style_guide is not in the config store yet")
        result = config_edit.insert_under_heading(text, proposal.anchor, proposal.note_markdown)
        if isinstance(result, Refusal):
            return result
        return Rendered("style_guide", result, "", {"style_guide": result})

    if proposal.kind == "profile_scalar":
        text = _document_text("blog_profile")
        if text is None:
            return Refusal("blog_profile is not in the config store yet")
        result = config_edit.set_profile_scalar(text, proposal.profile_key, proposal.profile_value)
        if isinstance(result, Refusal):
            return result
        return Rendered("blog_profile", result, "", {"blog_profile": yaml.safe_load(result) or {}})

    if proposal.kind == "guidance":
        text = _document_text("agent_guidance")
        if text is None:
            return Refusal("agent_guidance is not in the config store yet")
        entry = {
            "text": proposal.guidance_text,
            "language": cluster.language,
            "added": date.today().isoformat(),
            "learned_from": sorted({o.signature for o in cluster.observations})[:3],
        }
        result = config_edit.append_guidance(text, proposal.guidance_agent, entry)
        if isinstance(result, Refusal):
            return result
        return Rendered("agent_guidance", result, "", {"agent_guidance": yaml.safe_load(result) or {}})

    return Refusal("the diagnostician proposed nothing")


_document_cache: dict[str, str] = {}


def _document_text(name: str) -> str | None:
    return _document_cache.get(name)


async def _load_documents() -> None:
    """Read the current documents once per run, as raw text."""
    from . import config_store

    _document_cache.clear()
    for name, row in (await config_store.latest_versions()).items():
        _document_cache[name] = row.content


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def sweep(*, clients: Any = None, run_id: str | None = None) -> dict[str, Any]:
    """Analyse what has been captured, and file a review if anything earned one."""
    from .. import agents as agent_factories
    from . import learning_reviews

    settings = get_settings()
    # Only built when the caller did not supply one: a dry run passes the stub,
    # and constructing a real Foundry client would need credentials it has not
    # got.
    if clients is None:
        from ..clients import default_clients

        clients = default_clients()
    await _load_documents()

    analysed = await _analyse(settings, clients)
    corpus = await delta_store.corpus()
    rates = [delta.edit_rate(p["agent"], p["final"]) for p in corpus]

    # Order matters: an empty corpus also reads as "clean", and reporting it that
    # way tells the author their drafts are excellent when in fact nothing has
    # been captured at all.
    if not corpus:
        return _nothing(
            analysed,
            "Nothing captured yet. A pair is recorded when a write run finishes, "
            "and completed when you publish the draft.",
        )

    if delta.already_clean(rates, threshold=settings.learning.clean_rate):
        return _nothing(
            analysed,
            "Drafts are already published close to unchanged. A new rule would be "
            "more likely to flag work that was already right than to catch a fault.",
        )

    figures = await delta_store.metrics()
    if figures["discard_rate"] > settings.learning.max_discard_rate:
        return _nothing(
            analysed,
            f"{round(figures['discard_rate'] * 100)}% of recent pairs were discarded. "
            "Something about capture is wrong, and a rule learned from what is left "
            "would be learned from noise.",
        )

    clusters = await _clusters(settings)
    eligible = delta.recurring(clusters, min_distinct_posts=settings.learning.min_distinct_posts)
    if not eligible:
        return _nothing(analysed, "No correction has recurred across enough separate posts yet.")

    proposals: list[dict[str, Any]] = []
    candidate_ids: list[int] = []
    for cluster in eligible[: settings.learning.max_pairs_per_run]:
        outcome = await _propose(cluster, settings, clients, corpus, agent_factories)
        if outcome is None:
            continue
        proposals.append(outcome)
        candidate_ids.append(outcome["candidate_id"])

    survivors = [p for p in proposals if p["gate"]["status"] != "failed"]
    if not survivors:
        return _nothing(
            analysed,
            f"{len(proposals)} proposal(s) were tested against your published posts "
            "and none survived.",
        )

    review_id = await learning_reviews.create(run_id, survivors, candidate_ids)
    return {
        "awaiting_learning_approval": True,
        "review_id": review_id,
        "analysed": analysed,
        "clusters": len(clusters),
        "eligible": len(eligible),
        "proposed": len(proposals),
        "survived": len(survivors),
    }


def _nothing(analysed: int, reason: str) -> dict[str, Any]:
    logger.info("nothing proposed: %s", reason)
    return {
        "awaiting_learning_approval": False,
        "analysed": analysed,
        "proposed": 0,
        "survived": 0,
        "reason": reason,
    }


async def _analyse(settings: Settings, clients: Any) -> int:
    """Classify every captured pair that has not been read yet."""
    from .. import agents as agent_factories

    pairs = await delta_store.pending_analysis(settings.learning.max_pairs_per_run)
    if not pairs:
        return 0

    analyst = agent_factories.build_delta_analyst(settings, clients)
    done = 0
    for pair in pairs:
        if pair["identical"]:
            # Nothing to classify, and it is not a discard: an untouched post is
            # the positive class and belongs in the corpus the gate tests against.
            await delta_store.record_analysis(
                pair["id"], DeltaAnalysis(post_slug=pair["slug"]), pair["language"]
            )
            done += 1
            continue
        try:
            response = await analyst.run(user_message(_analysis_prompt(pair)))
            analysis = parse_model(response, DeltaAnalysis)
        except Exception as exc:  # noqa: BLE001 - one unreadable pair is not a failed run
            logger.warning("could not analyse pair %s: %s", pair["id"], exc)
            await delta_store.discard(pair["id"], f"analysis failed: {exc}"[:200])
            continue
        await delta_store.record_analysis(pair["id"], analysis, pair["language"])
        done += 1
    return done


def _analysis_prompt(pair: dict[str, Any]) -> str:
    hunks = [h for h in (pair["diff"] or {}).get("hunks", []) if h.get("op") != "equal"]
    sections = [s for s in (pair["diff"] or {}).get("sections", []) if s.get("op") != "equal"]
    lines = [
        f"Post: {pair['title'] or pair['slug']}",
        f"Words changed: {round(pair['edit_rate'] * 100)}%",
        "",
        "<structural_changes>",
    ]
    for change in sections:
        lines.append(f"{change['op']}: {change.get('before') or '—'} -> {change.get('after') or '—'}")
    lines.append("</structural_changes>")
    lines.append("")
    lines.append("<differences>")
    for n, hunk in enumerate(hunks[:40], 1):
        lines.append(f"[{n}] {hunk['op']} in section: {hunk.get('section') or '(none)'}")
        lines.append(f"  crew:   {hunk.get('before', '')[:400]}")
        lines.append(f"  author: {hunk.get('after', '')[:400]}")
    lines.append("</differences>")
    return "\n".join(lines)


async def _clusters(settings: Settings) -> list[delta.Cluster]:
    """Group every stored observation, minus anything already refused."""
    observations = await delta_store.observations_for()
    refused = await declined_fingerprints()

    from ..models import EditObservation

    pairs: list[tuple[int, EditObservation]] = []
    for row in observations:
        if row["fingerprint"] in refused:
            continue
        pairs.append(
            (
                int(row["post_id"] or row["pair_id"]),
                EditObservation(
                    edit_kind=row["edit_kind"],
                    target=row["target"],
                    signature=row["signature"],
                    before=row["before"],
                    after=row["after"],
                    rationale=row["rationale"],
                    confidence=row["confidence"] or 1,
                ),
            )
        )
    return delta.cluster(pairs, language=settings.language)


async def _propose(
    cluster: delta.Cluster,
    settings: Settings,
    clients: Any,
    corpus: list[dict[str, str]],
    agent_factories: Any,
) -> dict[str, Any] | None:
    """Diagnose one cluster, render it, gate it, and record what happened."""
    shape = _SHAPE_FOR.get(cluster.target)
    if shape is None:
        return None

    candidate_id = await _upsert_candidate(cluster)

    agent = agent_factories.build_learning_diagnostician(
        settings, clients, shape=shape, context_block=_context_for(shape, settings)
    )
    try:
        response = await agent.run(user_message(_diagnosis_prompt(cluster)))
        proposal = parse_model(response, LearningProposal)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not diagnose %s: %s", cluster.label, exc)
        await _mark(candidate_id, status="accruing")
        return None

    if proposal.kind == "none":
        await _mark(candidate_id, status="accruing")
        return None

    rendered = render(proposal, cluster, settings)
    if isinstance(rendered, Refusal):
        await _mark(
            candidate_id,
            status="rejected_by_gate",
            gate_status="failed",
            gate_report={"status": "failed", "reason": rendered.reason},
            proposal=proposal.model_dump(mode="json"),
        )
        logger.info("discarded a proposal for %s: %s", cluster.label, rendered.reason)
        return None

    evidence_slugs = {o.signature for o in cluster.observations}
    report = await gate(
        proposal,
        rule_id=rendered.rule_id,
        documents=rendered.documents,
        evidence_slugs=evidence_slugs,
        corpus=corpus,
        settings=settings,
    )
    await _mark(
        candidate_id,
        status="rejected_by_gate" if report.status == "failed" else "pending_review",
        gate_status=report.status,
        gate_report=report.as_dict(),
        proposal=proposal.model_dump(mode="json"),
        proposal_kind=proposal.kind,
        allocated_rule_id=rendered.rule_id,
    )

    return {
        "candidate_id": candidate_id,
        "fingerprint": cluster.fingerprint,
        "label": cluster.label,
        "edit_kind": cluster.edit_kind,
        "target": cluster.target,
        "distinct_posts": cluster.distinct_posts,
        "occurrences": cluster.occurrences,
        "kind": proposal.kind,
        "summary": proposal.summary,
        "evidence_note": proposal.evidence_note,
        "rule_id": rendered.rule_id,
        "document": rendered.document,
        "content": rendered.content,
        "proposal": proposal.model_dump(mode="json"),
        "gate": report.as_dict(),
        "examples": [
            {"before": o.before, "after": o.after, "rationale": o.rationale}
            for o in cluster.observations[:3]
        ],
    }


def _context_for(shape: str, settings: Settings) -> str:
    if shape == "style_note":
        anchors = [
            line.strip()
            for line in detectors.prose_only(settings.style_guide).splitlines()
            if line.strip().startswith("## ") or line.strip().startswith("### ")
        ]
        listing = "\n".join(anchors)
        return f"\n<available_anchors>\nCopy one of these verbatim:\n{listing}\n</available_anchors>\n"
    if shape == "profile_scalar":
        rows = "\n".join(
            f"{key}: currently {_current_scalar(settings, key)}, permitted {lo}-{hi}"
            for key, (lo, hi) in config_edit.PROFILE_SCALARS.items()
        )
        return f"\n<permitted_keys>\n{rows}\n</permitted_keys>\n"
    return ""


def _current_scalar(settings: Settings, dotted: str) -> Any:
    cursor: Any = settings.blog_profile
    for part in dotted.split("."):
        cursor = (cursor or {}).get(part) if isinstance(cursor, dict) else None
    return cursor


def _diagnosis_prompt(cluster: delta.Cluster) -> str:
    lines = [
        f"Recurring correction: {cluster.label}",
        f"Kind: {cluster.edit_kind}. Seen in {cluster.distinct_posts} separate posts, "
        f"{cluster.occurrences} times in total.",
        "",
        "<examples>",
    ]
    for n, observation in enumerate(cluster.observations[:6], 1):
        lines.append(f"[{n}] {observation.rationale}")
        lines.append(f"  crew:   {observation.before[:300]}")
        lines.append(f"  author: {observation.after[:300]}")
    lines.append("</examples>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


async def _upsert_candidate(cluster: delta.Cluster) -> int:
    async with session() as s:
        row = (
            await s.execute(
                select(LearningCandidate).where(
                    LearningCandidate.fingerprint == cluster.fingerprint
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = LearningCandidate(
                fingerprint=cluster.fingerprint,
                edit_kind=cluster.edit_kind,
                target=cluster.target,
                language=cluster.language,
                label=cluster.label[:300],
                first_seen_at=utcnow(),
            )
            s.add(row)
        row.occurrences = cluster.occurrences
        row.distinct_posts = cluster.distinct_posts
        row.post_ids = sorted(set(cluster.post_ids))
        row.examples = [
            {"before": o.before, "after": o.after, "rationale": o.rationale}
            for o in cluster.observations[:3]
        ]
        row.last_seen_at = utcnow()
        await s.commit()
        return row.id


async def _mark(candidate_id: int, **fields: Any) -> None:
    async with session() as s:
        row = await s.get(LearningCandidate, candidate_id)
        if row is None:
            return
        for key, value in fields.items():
            setattr(row, key, value)
        await s.commit()


async def declined_fingerprints() -> set[str]:
    async with session() as s:
        rows = (await s.execute(select(DeclinedLearning.fingerprint))).scalars().all()
        return set(rows)


async def list_candidates(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    async with session() as s:
        query = select(LearningCandidate).order_by(LearningCandidate.last_seen_at.desc()).limit(limit)
        if status:
            query = query.where(LearningCandidate.status == status)
        rows = (await s.execute(query)).scalars().all()
        return [_candidate_dict(r) for r in rows]


async def get_candidate(candidate_id: int) -> dict[str, Any] | None:
    async with session() as s:
        row = await s.get(LearningCandidate, candidate_id)
        return _candidate_dict(row) if row else None


def _candidate_dict(row: LearningCandidate) -> dict[str, Any]:
    from .db import as_utc

    return {
        "id": row.id,
        "fingerprint": row.fingerprint,
        "edit_kind": row.edit_kind,
        "target": row.target,
        "language": row.language,
        "label": row.label,
        "status": row.status,
        "occurrences": row.occurrences,
        "distinct_posts": row.distinct_posts,
        "post_ids": row.post_ids or [],
        "examples": row.examples or [],
        "proposal": row.proposal,
        "proposal_kind": row.proposal_kind,
        "allocated_rule_id": row.allocated_rule_id,
        "gate_status": row.gate_status,
        "gate_report": row.gate_report,
        "review_id": row.review_id,
        "config_version": row.config_version,
        "first_seen_at": (as_utc(row.first_seen_at) or row.first_seen_at).isoformat(),
        "last_seen_at": (as_utc(row.last_seen_at) or row.last_seen_at).isoformat(),
    }
