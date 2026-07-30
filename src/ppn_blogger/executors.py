"""Custom workflow executors — the deterministic glue between the agents.

The agents do the judgement; these executors do the bookkeeping: parsing typed
results, holding run state, enforcing the retry budgets, routing the loops and
writing artefacts. Keeping policy here (rather than in a prompt) is what makes
the pipeline reproducible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Never

from agent_framework import (
    AgentExecutorRequest,
    AgentExecutorResponse,
    Executor,
    WorkflowContext,
    handler,
)

from . import agents as A
from . import detectors, storage
from .models import (
    AuthorClaim,
    AuthorClaimSet,
    Draft,
    PostPackage,
    ResearchDossier,
    ReviewOutcome,
    RuleFinding,
    ScoutReport,
    SourceVerdict,
    TopicSuggestion,
    TopicSuggestionSet,
    ValidationReport,
    ValidationReportDraft,
)
from .settings import Settings, get_settings
from .util import as_json, parse_model, user_message, word_count

logger = logging.getLogger("ppn.workflow")


# ---------------------------------------------------------------------------
# Author notes helpers
# ---------------------------------------------------------------------------


def notes_are_filled(text: str) -> bool:
    """True when the notes file has real content, not just the template.

    Missing file or the unfilled template (headings plus ``<...>`` prompts and
    empty fences) yields an empty claim list and analysis mode, so this decides
    whether the normalizer model is worth calling at all.
    """
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("#", ">", "|", "```", "---")):
            continue
        if line.startswith("<") and line.endswith(">"):
            continue  # an angle-bracket prompt from the template
        if line in {"-", "*"}:
            continue
        if len(line) >= 3:
            return True
    return False


def _research_brief(topic: TopicSuggestion, notes_text: str = "") -> str:
    questions = "\n".join(f"- {q}" for q in topic.key_questions) or "- (derive them from the angle)"
    seeds = "\n".join(f"- {s}" for s in topic.seed_sources) or "- (none supplied; find your own)"
    notes_block = ""
    if notes_text.strip():
        # The error strings and version numbers in the raw notes are the best
        # search seeds the researcher will get, so hand them the notes as-is.
        notes_block = (
            "\n\n<author_notes_raw>\nThe author wrote these notes. Use the error strings, "
            "version numbers and product names in them as search seeds. Do NOT treat them as "
            f"verified facts — verify against sources like anything else.\n{notes_text.strip()}\n"
            "</author_notes_raw>"
        )
    return f"""
<research_brief>
Title (working): {topic.title}
Watch area: {topic.watch_area}
Post format: {topic.post_format}
Primary keyword: {topic.primary_keyword}

Problem to solve for the reader:
{topic.problem_statement}

Angle we are taking:
{topic.angle}

Why this is timely:
{topic.why_now}

Novelty we are promising:
{topic.novelty}

Questions the dossier MUST answer:
{questions}

Seed sources to start from (verify each before citing):
{seeds}
</research_brief>{notes_block}

Research this now and return the complete ResearchDossier JSON.
""".strip()


def _author_context(state: RunState, settings: Settings) -> str:
    """The voice-mode, word-target and author-claim block for a writer message."""
    fmt = state.topic.post_format if state.topic else "analysis"
    lo, hi = settings.word_target(fmt, state.voice_mode)
    if state.author_claims:
        claims_block = as_json([c.model_dump() for c in state.author_claims])
    else:
        claims_block = (
            "(none — analysis post: no first person anywhere, no invented numbers, "
            "no anecdotes. Use placeholders if a concrete detail is missing.)"
        )
    return (
        f"<voice_mode>{state.voice_mode}</voice_mode>\n"
        f"<word_target>{lo} to {hi} words</word_target>\n"
        f"<author_claims>\n{claims_block}\n</author_claims>"
    )


def _testimony(state: RunState) -> str:
    """Author claims for the Source Checker: testimony, never to be verified."""
    if not state.author_claims:
        return ""
    return (
        "\n\n<author_testimony>\nThese are the author's own claims. Do NOT verify them, "
        "search for them, or fail the dossier because of them. Pass over them.\n"
        f"{as_json([c.model_dump() for c in state.author_claims])}\n</author_testimony>"
    )


def _editor_instructions(state: RunState) -> str:
    """Editor guidance for a regeneration, injected into the writer's first draft.

    A new version is written for a reason — "shorter", "lead with the migration
    steps", "drop the FAQ". That instruction outranks the stylistic defaults, so
    it goes to the writer verbatim alongside the topic and dossier.
    """
    if not state.extra_instructions.strip():
        return ""
    return (
        "\n\n<editor_instructions>\n"
        "The editor requested this version specifically. Honour these instructions "
        "above your stylistic defaults, without inventing facts or contradicting the "
        f"dossier:\n{state.extra_instructions.strip()}\n</editor_instructions>"
    )


# ---------------------------------------------------------------------------
# Shared run state
# ---------------------------------------------------------------------------


@dataclass
class ResumePayload:
    """Start message for a run that reuses research already on disk."""

    topic: TopicSuggestion
    dossier: ResearchDossier


@dataclass
class RunState:
    """Mutable state for one pipeline run. One instance per built workflow."""

    topic: TopicSuggestion | None = None
    dossier: ResearchDossier | None = None
    draft: Draft | None = None
    source_verdict: SourceVerdict | None = None
    reports: list[ValidationReport] = field(default_factory=list)
    source_round: int = 0
    revision_round: int = 0
    dossier_path: str = ""
    package: PostPackage | None = None
    # Editor guidance for a regeneration, injected into the writer's first-draft
    # prompt. Empty for an ordinary write.
    extra_instructions: str = ""
    # Author notes: raw text in, typed claims and a voice mode out.
    notes_text: str = ""
    author_claims: list[AuthorClaim] = field(default_factory=list)
    voice_mode: str = "analysis"
    notes_path: str = ""
    # Code-side detector output for the current draft, keyed by validator name.
    code_findings: dict[str, list[RuleFinding]] = field(default_factory=dict)
    measurements: dict = field(default_factory=dict)
    prev_finding_ids: set[str] = field(default_factory=set)

    def snapshot_outcome(self, approved: bool, instructions: str = "") -> ReviewOutcome:
        scores = [r.score for r in self.reports] or [0]
        blockers = sum(
            1 for r in self.reports for f in r.findings if f.severity == "blocker"
        )
        return ReviewOutcome(
            approved=approved,
            revision=self.revision_round,
            overall_score=sum(scores) / len(scores),
            blockers=blockers,
            reports=list(self.reports),
            source_verdict=self.source_verdict,
            revision_instructions=instructions,
        )


# ---------------------------------------------------------------------------
# Topic discovery graph
# ---------------------------------------------------------------------------


class ScoutDispatcher(Executor):
    """Entry point: turns a free-text instruction into a request for every scout."""

    def __init__(self, settings: Settings, id: str = "scout_dispatcher") -> None:
        super().__init__(id)
        self.settings = settings

    @handler
    async def dispatch(
        self, instruction: str, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        areas = ", ".join(f"{a['id']}" for a in self.settings.watch_areas)
        prompt = (
            f"{instruction.strip()}\n\n"
            f"Watch areas to cover: {areas}.\n"
            "Work now. Use your tools, then return your ScoutReport JSON."
        )
        await ctx.send_message(AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True))


class ScoutAggregator(Executor):
    """Fan-in: collects the three scout reports and briefs the topic editor."""

    def __init__(self, settings: Settings, id: str = "scout_aggregator") -> None:
        super().__init__(id)
        self.settings = settings

    @handler
    async def aggregate(
        self,
        responses: list[AgentExecutorResponse],
        ctx: WorkflowContext[AgentExecutorRequest],
    ) -> None:
        blocks: list[str] = []
        for response in responses:
            label = response.executor_id
            try:
                report = parse_model(response, ScoutReport)
                blocks.append(f"<scout name=\"{label}\">\n{as_json(report)}\n</scout>")
                logger.info("scout %s returned %d items", label, len(report.items))
            except ValueError as exc:
                logger.warning("scout %s produced unparsable output: %s", label, exc)
                blocks.append(
                    f"<scout name=\"{label}\" parse_error=\"true\">\n"
                    f"{(response.agent_response.text if response.agent_response else '')[:4000]}\n</scout>"
                )

        prompt = (
            "Here are the raw scout reports. Synthesise them into the ranked topic "
            "shortlist as instructed.\n\n" + "\n\n".join(blocks)
        )
        await ctx.send_message(AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True))


class TopicPublisher(Executor):
    """Parses the editor's shortlist, writes it to disk and ends the workflow."""

    def __init__(self, id: str = "topic_publisher") -> None:
        super().__init__(id)
        self.result: TopicSuggestionSet | None = None
        self.markdown_path: str = ""
        self.json_path: str = ""

    @handler
    async def publish(
        self,
        response: AgentExecutorResponse,
        ctx: WorkflowContext[Never, TopicSuggestionSet],
    ) -> None:
        suggestions = parse_model(response, TopicSuggestionSet)
        md_path, json_path = storage.save_topic_suggestions(suggestions)
        self.result = suggestions
        self.markdown_path, self.json_path = str(md_path), str(json_path)
        logger.info("wrote %d topic suggestions to %s", len(suggestions.suggestions), md_path)
        await ctx.yield_output(suggestions)


# ---------------------------------------------------------------------------
# Post pipeline graph
# ---------------------------------------------------------------------------


class BriefBuilder(Executor):
    """Entry point: routes through the notes normalizer, then to the researcher.

    When there are real author notes, they go to the normalizer first, which
    turns them into typed claims and puts the run in ``field_report`` mode. With
    no notes (or just the unfilled template) there is nothing to normalise: the
    run is ``analysis`` and the brief goes straight to the researcher.
    """

    def __init__(self, state: RunState, settings: Settings, id: str = "brief_builder") -> None:
        super().__init__(id)
        self.state = state
        self.settings = settings

    @handler
    async def build(
        self, topic: TopicSuggestion, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        self.state.topic = topic

        if notes_are_filled(self.state.notes_text):
            logger.info("author notes present — normalising into claims (field_report)")
            prompt = (
                "Normalise these author notes into typed author claims. Extract only what is "
                "written; invent nothing.\n\n<author_notes>\n"
                f"{self.state.notes_text.strip()}\n</author_notes>"
            )
            await ctx.send_message(
                AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True),
                target_id=A.NOTES_NORMALIZER,
            )
            return

        # No notes: analysis mode, empty claim set, straight to research.
        self.state.author_claims = []
        self.state.voice_mode = "analysis"
        self.state.notes_path = str(storage.save_notes([], topic.slug or topic.title))
        logger.info("no author notes — analysis mode")
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[user_message(_research_brief(topic, self.state.notes_text))],
                should_respond=True,
            ),
            target_id=A.RESEARCHER,
        )


class NotesGate(Executor):
    """Parses the normalizer's claims, files them, and briefs the researcher."""

    def __init__(self, state: RunState, id: str = "notes_gate") -> None:
        super().__init__(id)
        self.state = state

    @handler
    async def receive(
        self, response: AgentExecutorResponse, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        try:
            result = parse_model(response, AuthorClaimSet)
            claims = result.claims
        except ValueError as exc:
            # A broken normalization must not sink the run — fall back to analysis.
            logger.warning("author notes unparsable, continuing in analysis mode: %s", exc)
            claims = []

        self.state.author_claims = claims
        self.state.voice_mode = "field_report" if claims else "analysis"
        topic = self.state.topic
        assert topic is not None
        self.state.notes_path = str(storage.save_notes(claims, topic.slug or topic.title))
        logger.info(
            "author claims: %d (%s)", len(claims), self.state.voice_mode
        )
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[user_message(_research_brief(topic, self.state.notes_text))],
                should_respond=True,
            ),
            target_id=A.RESEARCHER,
        )


class DossierEntry(Executor):
    """Entry point for a run that resumes from research already on disk.

    Research is the expensive half of a post — several minutes and a lot of
    tokens. When a later stage fails, the dossier is still saved, so a retry
    should never pay for it twice.
    """

    def __init__(
        self,
        state: RunState,
        settings: Settings,
        *,
        skip_source_check: bool = False,
        id: str = "dossier_entry",
    ) -> None:
        super().__init__(id)
        self.state = state
        self.settings = settings
        self.skip_source_check = skip_source_check

    @handler
    async def start(
        self,
        payload: ResumePayload,
        ctx: WorkflowContext[AgentExecutorRequest],
    ) -> None:
        self.state.topic = payload.topic
        self.state.dossier = payload.dossier
        logger.info(
            "resuming from saved dossier: %d claims, %d citations",
            len(payload.dossier.claims),
            len(payload.dossier.citations),
        )

        if not self.skip_source_check:
            prompt = (
                "Verify this dossier against the source policy. Be adversarial.\n\n"
                f"<dossier>\n{as_json(payload.dossier)}\n</dossier>{_testimony(self.state)}"
            )
            await ctx.send_message(
                AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True),
                target_id=A.SOURCE_CHECKER,
            )
            return

        logger.warning("source check skipped — claims in this draft are unverified")
        self.state.source_verdict = SourceVerdict(
            passed=True, summary="Skipped at the operator's request (--skip-source-check)."
        )
        prompt = f"""
Write the first draft of this post.

<topic>
{as_json(payload.topic)}
</topic>

<dossier>
{as_json(payload.dossier)}
</dossier>{_editor_instructions(self.state)}

{_author_context(self.state, self.settings)}

Return the complete Draft JSON. Set revision to 1.
""".strip()
        await ctx.send_message(
            AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True),
            target_id=A.WRITER,
        )


class DossierGate(Executor):
    """Parses the dossier and hands it to the Source Checker."""

    def __init__(self, state: RunState, id: str = "dossier_gate") -> None:
        super().__init__(id)
        self.state = state

    @handler
    async def receive(
        self, response: AgentExecutorResponse, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        dossier = parse_model(response, ResearchDossier)
        self.state.dossier = dossier
        self.state.dossier_path = str(storage.save_dossier(dossier))
        logger.info(
            "dossier ready: %d claims, %d citations (round %d)",
            len(dossier.claims),
            len(dossier.citations),
            self.state.source_round,
        )
        prompt = (
            "Verify this dossier against the source policy. Be adversarial.\n\n"
            f"<dossier>\n{as_json(dossier)}\n</dossier>{_testimony(self.state)}"
        )
        await ctx.send_message(
            AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True),
            target_id=A.SOURCE_CHECKER,
        )


class SourceGate(Executor):
    """Routes on the source verdict: back to the researcher, or on to the writer."""

    def __init__(self, state: RunState, settings: Settings, id: str = "source_gate") -> None:
        super().__init__(id)
        self.state = state
        self.settings = settings

    @handler
    async def route(
        self, response: AgentExecutorResponse, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        from .prompts import researcher_revision_instructions

        verdict = parse_model(response, SourceVerdict)
        self.state.source_verdict = verdict
        max_rounds = self.settings.run.max_source_rounds

        if not verdict.passed and self.state.source_round < max_rounds:
            self.state.source_round += 1
            logger.warning(
                "source check failed (round %d/%d): %s",
                self.state.source_round,
                max_rounds,
                verdict.summary[:160],
            )
            prompt = (
                f"{researcher_revision_instructions()}\n\n"
                f"<source_verdict>\n{as_json(verdict)}\n</source_verdict>"
            )
            await ctx.send_message(
                AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True),
                target_id=A.RESEARCHER,
            )
            return

        if not verdict.passed:
            logger.warning(
                "source check still failing after %d rounds — continuing with warnings recorded",
                max_rounds,
            )

        dossier = self.state.dossier
        topic = self.state.topic
        assert dossier is not None and topic is not None
        caveat = (
            ""
            if verdict.passed
            else (
                "\n\n<unresolved_source_issues>\n"
                "The source checker could not clear everything. Do NOT state the following as "
                "settled fact; either drop the claim or hedge it explicitly and flag it in your "
                f"changelog.\n{as_json(verdict.findings)}\n</unresolved_source_issues>"
            )
        )
        prompt = f"""
Write the first draft of this post.

<topic>
{as_json(topic)}
</topic>

<dossier>
{as_json(dossier)}
</dossier>{caveat}{_editor_instructions(self.state)}

{_author_context(self.state, self.settings)}

Return the complete Draft JSON. Set revision to 1.
""".strip()
        await ctx.send_message(
            AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True),
            target_id=A.WRITER,
        )


class DraftGate(Executor):
    """Parses the draft, runs the code-side detectors, briefs both validators.

    The two validators judge different families and so get different payloads:
    the Content Validator (honesty, voice, content) gets the dossier and the
    author claims; the Design Validator (typography, structure, SEO) gets
    neither. Both get the pre-computed detector findings and measurements for
    their own families, so the model never re-counts what a regex already knows.
    """

    def __init__(self, state: RunState, settings: Settings, id: str = "draft_gate") -> None:
        super().__init__(id)
        self.state = state
        self.settings = settings

    @handler
    async def receive(
        self, response: AgentExecutorResponse, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        draft = parse_model(response, Draft)
        if not draft.word_count:
            draft.word_count = word_count(draft.markdown)
        if not draft.read_minutes:
            wpm = int(get_settings().structure.get("reading_speed_wpm", 200)) or 200
            draft.read_minutes = max(1, round(draft.word_count / wpm))
        draft.revision = draft.revision or (self.state.revision_round + 1)
        self.state.draft = draft

        dossier = self.state.dossier
        dossier_blob = as_json(dossier) if dossier else ""

        content_run = detectors.run_detectors(
            draft.markdown,
            groups=self.settings.CONTENT_GROUPS,
            settings=self.settings,
            dossier_blob=dossier_blob,
            author_claims=self.state.author_claims,
            slug=draft.slug,
        )
        design_run = detectors.run_detectors(
            draft.markdown,
            groups=self.settings.DESIGN_GROUPS,
            settings=self.settings,
            slug=draft.slug,
        )
        self.state.code_findings = {
            "content": content_run.findings,
            "design": design_run.findings,
        }
        self.state.measurements = content_run.measurements  # same for both
        logger.info(
            "draft r%d ready: %d words, %d code findings (%d blockers)",
            draft.revision,
            draft.word_count,
            len(content_run.findings) + len(design_run.findings),
            sum(
                1
                for f in content_run.findings + design_run.findings
                if f.severity == "blocker"
            ),
        )

        await ctx.send_message(
            AgentExecutorRequest(
                messages=[user_message(self._payload(draft, "content", content_run, dossier))],
                should_respond=True,
            ),
            target_id=A.CONTENT_VALIDATOR,
        )
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[user_message(self._payload(draft, "design", design_run, None))],
                should_respond=True,
            ),
            target_id=A.DESIGN_VALIDATOR,
        )

    def _payload(
        self,
        draft: Draft,
        validator: str,
        run: detectors.DetectorRun,
        dossier: ResearchDossier | None,
    ) -> str:
        precomputed = (
            as_json([f.model_dump() for f in run.findings]) if run.findings else "(none)"
        )
        hints = f"\n\n<detector_hints>\n{run.hints}\n</detector_hints>" if run.hints else ""
        extra = ""
        if validator == "content":
            claims = (
                as_json([c.model_dump() for c in self.state.author_claims])
                if self.state.author_claims
                else "(none — analysis post)"
            )
            extra = (
                f"\n\n<voice_mode>{self.state.voice_mode}</voice_mode>"
                f"\n\n<author_claims>\n{claims}\n</author_claims>"
                f"\n\n<dossier>\n{as_json(dossier) if dossier else '{}'}\n</dossier>"
            )
        return f"""
Validate this draft. You own the {validator} families.

<draft_metadata>
{as_json(draft.model_dump(exclude={'markdown'}))}
</draft_metadata>

<draft_markdown>
{draft.markdown}
</draft_markdown>

<precomputed_findings>
These were found by the code-side detectors. Include them; do not re-derive them.
{precomputed}
</precomputed_findings>

<measurements>
{as_json(run.measurements)}
</measurements>{hints}{extra}

Return your ValidationReport JSON with validator="{validator}".
""".strip()


class ReviewGate(Executor):
    """Fan-in: merges validator reports and decides revise vs finalise."""

    def __init__(self, state: RunState, settings: Settings, id: str = "review_gate") -> None:
        super().__init__(id)
        self.state = state
        self.settings = settings

    @handler
    async def decide(
        self,
        responses: list[AgentExecutorResponse],
        ctx: WorkflowContext[AgentExecutorRequest | ReviewOutcome],
    ) -> None:
        scoring = self.settings.validation.get("scoring", {})
        threshold = float(scoring.get("pass_threshold", 82))
        block_on_blocker = bool(scoring.get("block_on_any_blocker", True))

        reports: list[ValidationReport] = []
        for response in responses:
            try:
                # The agent is bound to ValidationReportDraft (no measurements);
                # upcast so the code-side detectors can attach measurements below.
                draft = parse_model(response, ValidationReportDraft)
                reports.append(ValidationReport.model_validate(draft.model_dump()))
            except ValueError as exc:
                logger.warning("validator %s unparsable: %s", response.executor_id, exc)
                reports.append(
                    ValidationReport(
                        validator=response.executor_id,
                        score=0,
                        passed=False,
                        summary=f"Validator output could not be parsed: {exc}"[:500],
                    )
                )

        # Merge the code-side detector findings into the matching report so a
        # blocker a regex raised gates the run exactly like a model blocker.
        self._merge_code_findings(reports)

        blockers = [f for r in reports for f in r.findings if f.severity == "blocker"]
        majors = [f for r in reports for f in r.findings if f.severity == "major"]
        avg = sum(r.score for r in reports) / max(len(reports), 1)
        approved = avg >= threshold and not (block_on_blocker and blockers)

        max_rounds = self.settings.run.max_revision_rounds
        logger.info(
            "review r%d: avg %.1f, %d blockers, %d majors -> %s",
            self.state.revision_round + 1,
            avg,
            len(blockers),
            len(majors),
            "approved" if approved else "revise",
        )

        if approved or self.state.revision_round >= max_rounds:
            outcome = self.state.snapshot_outcome(
                approved=approved,
                instructions="" if approved else _revision_text(reports),
            )
            await ctx.send_message(outcome, target_id="finalizer")
            return

        self.state.revision_round += 1
        instructions = _revision_text(reports)
        dossier = self.state.dossier
        prompt = f"""
The validators reviewed revision {self.state.revision_round} of your draft. Address every
blocker and major finding, then return the full corrected Draft JSON with
revision={self.state.revision_round + 1} and a changelog describing what you changed.
Change nothing factual while fixing style.

<validator_findings>
{instructions}
</validator_findings>

{_author_context(self.state, self.settings)}

<dossier>
{as_json(dossier) if dossier else '{}'}
</dossier>
""".strip()
        await ctx.send_message(
            AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True),
            target_id=A.WRITER,
        )

    def _merge_code_findings(self, reports: list[ValidationReport]) -> None:
        """Fold the pre-computed detector findings into the matching reports.

        Also attaches the measured values and computes which rule ids were
        resolved since the last round, then records this round's findings so the
        next round can diff against them.
        """
        by_name = {r.validator: r for r in reports}
        for validator, code in self.state.code_findings.items():
            report = by_name.get(validator)
            if report is None:
                # Model mislabelled the validator; fall back to any report that
                # is not already claimed, else the first one.
                report = next((r for r in reports if r.validator not in self.state.code_findings), None)
                report = report or (reports[0] if reports else None)
            if report is None:
                continue
            seen = {(f.rule_id, f.location) for f in report.findings}
            for finding in code:
                if (finding.rule_id, finding.location) not in seen:
                    report.findings.append(finding)

        current_ids = {f.rule_id for r in reports for f in r.findings}
        resolved = sorted(self.state.prev_finding_ids - current_ids)
        for report in reports:
            report.measurements = report.measurements or self.state.measurements
            report.resolved_since_last_iteration = resolved
        self.state.prev_finding_ids = current_ids
        self.state.reports = reports


def _revision_text(reports: list[ValidationReport]) -> str:
    lines: list[str] = []
    for report in reports:
        graded = [f for f in report.findings if f.severity in {"blocker", "major"}]
        if not graded:
            continue
        lines.append(f"From the {report.validator} validator (score {report.score}/100):")
        for f in sorted(graded, key=lambda x: 0 if x.severity == "blocker" else 1):
            where = f" [{f.location}]" if f.location else ""
            lines.append(f"  - {f.rule_id} ({f.severity}){where}: {f.problem}")
            lines.append(f"    FIX: {f.fix}")
        lines.append("")
    return "\n".join(lines).strip() or "No blocking findings; polish only."


class Finalizer(Executor):
    """Writes the artefacts, optionally pushes to WordPress, ends the workflow."""

    def __init__(
        self,
        state: RunState,
        settings: Settings,
        *,
        push_to_wordpress: bool | None = None,
        make_cover: bool | None = None,
        translate: bool | None = None,
        id: str = "finalizer",
    ) -> None:
        super().__init__(id)
        self.state = state
        self.settings = settings
        self.push = settings.wordpress.auto_push if push_to_wordpress is None else push_to_wordpress
        self.make_cover = settings.cover.enabled if make_cover is None else make_cover
        self.translate = settings.translation.enabled if translate is None else translate
        self.package: PostPackage | None = None

    @handler
    async def finalize(
        self,
        outcome: ReviewOutcome,
        ctx: WorkflowContext[AgentExecutorRequest, PostPackage],
    ) -> None:
        draft = self.state.draft
        dossier = self.state.dossier
        assert draft is not None and dossier is not None

        markdown_path = storage.save_draft(draft, outcome)
        report_path = storage.save_review_report(draft, outcome, markdown_path=markdown_path)
        package = PostPackage(
            draft=draft,
            dossier=dossier,
            outcome=outcome,
            voice_mode=self.state.voice_mode,
            author_claims=list(self.state.author_claims),
            notes_path=self.state.notes_path,
            markdown_path=str(markdown_path),
            report_path=str(report_path),
            dossier_path=self.state.dossier_path,
        )

        if self.make_cover:
            from .covers import build_cover

            package.cover = await build_cover(draft, self.settings)
            if package.cover.error:
                logger.warning("no cover for this post: %s", package.cover.error)

        if self.push:
            try:
                from .wordpress import push_draft

                package.published = await push_draft(draft, cover=package.cover)
                logger.info("pushed to WordPress: %s", package.published.edit_link)
            except Exception as exc:  # noqa: BLE001 - never lose the draft over a publish failure
                logger.error("WordPress push failed (%s). Draft is safe at %s",
                             exc, markdown_path)

        self.package = package
        self.state.package = package

        if self._should_translate(outcome):
            profile = self.settings.translation_profile
            prompt = f"""
Translate this approved post into {profile.get('target_language', 'Spanish')}.

<english_draft_metadata>
{as_json(draft.model_dump(exclude={'markdown'}))}
</english_draft_metadata>

<english_markdown>
{draft.markdown}
</english_markdown>

Return the complete translated Draft JSON.
""".strip()
            logger.info("sending approved draft to the translator")
            await ctx.send_message(
                AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True),
                target_id=A.TRANSLATOR,
            )
            return

        storage.save_package(package)
        await ctx.yield_output(package)

    def _should_translate(self, outcome: ReviewOutcome) -> bool:
        # ``self.translate`` already resolves the per-run flag against
        # TRANSLATE_ENABLED, so do not re-check the config here — that would make
        # an explicit --translate unable to override the default.
        if not self.translate:
            return False
        if self.settings.translation.only_when_approved and not outcome.approved:
            logger.info("draft not approved — skipping translation")
            return False
        return True


class TranslationGate(Executor):
    """Parses the localised draft, saves it and publishes it as its own post."""

    def __init__(
        self,
        state: RunState,
        settings: Settings,
        *,
        push_to_wordpress: bool | None = None,
        id: str = "translation_gate",
    ) -> None:
        super().__init__(id)
        self.state = state
        self.settings = settings
        self.push = settings.translation.push if push_to_wordpress is None else push_to_wordpress

    @handler
    async def receive(
        self, response: AgentExecutorResponse, ctx: WorkflowContext[Never, PostPackage]
    ) -> None:
        package = self.state.package
        assert package is not None
        profile = self.settings.translation_profile

        try:
            translated = parse_model(response, Draft)
        except ValueError as exc:
            # A broken translation must not cost us the English post.
            logger.error("translation unparsable, keeping the English post only: %s", exc)
            storage.save_package(package)
            await ctx.yield_output(package)
            return

        suffix = profile.get("slug_suffix", "-es")
        base_slug = translated.slug or package.draft.slug
        if not base_slug.endswith(suffix):
            base_slug = f"{base_slug}{suffix}"
        translated.slug = base_slug
        translated.category = package.draft.category  # taxonomy stays English
        if not translated.word_count:
            translated.word_count = word_count(translated.markdown)
        if not translated.read_minutes:
            wpm = int(get_settings().structure.get("reading_speed_wpm", 200)) or 200
            translated.read_minutes = max(1, round(translated.word_count / wpm))

        # Spanish prose reaches for the raya by default, so run the typography
        # detectors that matter most (T01 dash ban, T02 spaced-hyphen, T04
        # straight quotes) against the translated output. These never block a
        # finished English post; they are logged so a bad translation is visible.
        typo = detectors.run_detectors(
            translated.markdown, groups=("typography",), settings=self.settings
        )
        offenders = [f for f in typo.findings if f.rule_id in {"T01", "T02", "T04"}]
        if offenders:
            logger.warning(
                "translation typography issues: %s",
                ", ".join(f"{f.rule_id} {f.location!r}" for f in offenders[:5]),
            )

        package.translation = translated
        package.translation_language = profile.get("target_code", "es")
        package.translation_path = str(
            storage.save_draft(
                translated,
                package.outcome,
                language=package.translation_language,
                translation_of=package.draft.slug,
            )
        )
        logger.info("translated draft saved to %s", package.translation_path)

        if self.push:
            try:
                from .wordpress import push_draft

                # Reuse the English artwork rather than paying for a second image.
                cover = package.cover if profile.get("reuse_cover", True) else None
                package.translation_published = await push_draft(translated, cover=cover)
                logger.info(
                    "pushed translation to WordPress: %s", package.translation_published.edit_link
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("translation push failed (%s); file is safe at %s",
                             exc, package.translation_path)

        storage.save_package(package)
        await ctx.yield_output(package)


def any_value(obj: Any) -> Any:  # pragma: no cover - typing convenience for handlers
    return obj
