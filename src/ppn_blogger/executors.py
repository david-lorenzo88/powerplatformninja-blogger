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
from . import storage
from .models import (
    Draft,
    PostPackage,
    ResearchDossier,
    ReviewOutcome,
    ScoutReport,
    SourceVerdict,
    TopicSuggestion,
    TopicSuggestionSet,
    ValidationReport,
)
from .settings import Settings, get_settings
from .util import as_json, parse_model, user_message, word_count

logger = logging.getLogger("ppn.workflow")


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
    """Entry point: turns a chosen topic into the researcher's brief."""

    def __init__(self, state: RunState, settings: Settings, id: str = "brief_builder") -> None:
        super().__init__(id)
        self.state = state
        self.settings = settings

    @handler
    async def build(
        self, topic: TopicSuggestion, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        self.state.topic = topic
        questions = "\n".join(f"- {q}" for q in topic.key_questions) or "- (derive them from the angle)"
        seeds = "\n".join(f"- {s}" for s in topic.seed_sources) or "- (none supplied; find your own)"
        prompt = f"""
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
</research_brief>

Research this now and return the complete ResearchDossier JSON.
""".strip()
        await ctx.send_message(AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True))


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
                f"<dossier>\n{as_json(payload.dossier)}\n</dossier>"
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
</dossier>

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
            f"<dossier>\n{as_json(dossier)}\n</dossier>"
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
</dossier>{caveat}

Return the complete Draft JSON. Set revision to 1.
""".strip()
        await ctx.send_message(
            AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True),
            target_id=A.WRITER,
        )


class DraftGate(Executor):
    """Parses the draft and fans it out to both validators."""

    def __init__(self, state: RunState, id: str = "draft_gate") -> None:
        super().__init__(id)
        self.state = state

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
        logger.info("draft r%d ready: %d words", draft.revision, draft.word_count)

        dossier = self.state.dossier
        payload = f"""
Validate this draft.

<draft_metadata>
{as_json(draft.model_dump(exclude={'markdown'}))}
</draft_metadata>

<draft_markdown>
{draft.markdown}
</draft_markdown>

<dossier>
{as_json(dossier) if dossier else '{}'}
</dossier>

Return your ValidationReport JSON.
""".strip()
        await ctx.send_message(
            AgentExecutorRequest(messages=[user_message(payload)], should_respond=True)
        )


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
                reports.append(parse_model(response, ValidationReport))
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
        self.state.reports = reports

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

<validator_findings>
{instructions}
</validator_findings>

<dossier>
{as_json(dossier) if dossier else '{}'}
</dossier>
""".strip()
        await ctx.send_message(
            AgentExecutorRequest(messages=[user_message(prompt)], should_respond=True),
            target_id=A.WRITER,
        )


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
        report_path = storage.save_review_report(draft, outcome)
        package = PostPackage(
            draft=draft,
            dossier=dossier,
            outcome=outcome,
            markdown_path=str(markdown_path),
            report_path=str(report_path),
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
