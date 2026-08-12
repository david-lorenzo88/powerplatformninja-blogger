"""Custom workflow executors — the deterministic glue between the agents.

The agents do the judgement; these executors do the bookkeeping: parsing typed
results, holding run state, enforcing the retry budgets, routing the loops and
writing artefacts. Keeping policy here (rather than in a prompt) is what makes
the pipeline reproducible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
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
    OutlineSection,
    PostOutline,
    PostPackage,
    ResearchDossier,
    ReviewOutcome,
    RuleFinding,
    ScoutReport,
    SourceReviewSet,
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


def _outline_brief(outline: PostOutline | None) -> str:
    """The outline as an agent needs to read it: compact, one line per section.

    Never ``as_json(outline)``. The point of handing the plan downstream is to keep
    the argument in front of the model on every round, and a prompt that grows by a
    full JSON object each round defeats that.
    """
    if outline is None:
        return "(no outline was produced for this run)"
    lines = [f"THESIS: {outline.thesis}", f"READER PROMISE: {outline.reader_promise}"]
    if outline.out_of_scope:
        lines.append("OUT OF SCOPE (never give these a heading): " + "; ".join(outline.out_of_scope))
    lines.append("SECTIONS, in order:")
    for i, section in enumerate(outline.content_sections, 1):
        claims = ", ".join(section.claim_ids) or "no claims"
        lines.append(
            f"  {i}. {section.title} [{section.target_words}w · {claims}]\n"
            f"     point: {section.makes_this_point}"
        )
    return "\n".join(lines)


def _scoped_dossier(dossier: ResearchDossier, outline: PostOutline | None) -> dict:
    """The dossier as the Writer needs to see it: the claims this post argues.

    Returns a **dict**, never a ``ResearchDossier``. A truncated dossier object
    would eventually be persisted by somebody and "nothing downstream of research
    may destroy research" would stop being checkable. Nothing is destroyed here:
    ``DossierGate`` already wrote the whole thing to ``research/`` and the whole
    thing rides in the ``PostPackage``. This narrows one agent's *view*, and only
    after the outline has said which claims the post rests on.

    What stays whole, and why, matters as much as what is cut. ``examples``,
    ``gotchas`` and ``limits`` are free strings with no claim id to filter on, and
    they are the material V12 and V13 (the specificity floor, a **blocker**) are
    satisfied from. Trading a focus win for a specificity blocker is a bad trade.
    ``suggested_outline`` is the one thing dropped outright: the Outliner has
    already read it and superseded it, and shipping both invites the Writer to
    follow the wrong plan.
    """
    if outline is None:
        return dossier.model_dump(mode="json")

    keep = set(outline.selected_claim_ids)
    claims = [c for c in dossier.claims if c.id in keep]
    cited = {cid for c in claims for cid in c.citation_ids}
    data = dossier.model_dump(mode="json")
    data["claims"] = [c.model_dump(mode="json") for c in claims]
    data["citations"] = [c.model_dump(mode="json") for c in dossier.citations if c.id in cited]
    data.pop("suggested_outline", None)
    return data


def _omitted_research(dossier: ResearchDossier, outline: PostOutline | None) -> str:
    """The claims the outline did not select, named rather than silently withheld.

    Telling the Writer what was cut works better than hiding it: an unexplained gap
    invites it to go and fill the gap from memory, which is the one thing it must
    never do.
    """
    if outline is None:
        return ""
    keep = set(outline.selected_claim_ids)
    dropped = [c for c in dossier.claims if c.id not in keep]
    if not dropped:
        return ""
    lines = "\n".join(f"- {c.id}: {c.statement}" for c in dropped[:25])
    return (
        "\n\n<omitted_research>\n"
        "This research exists and is verified. It is not this post. Do not reach for "
        f"it, and do not give any of it a heading.\n{lines}\n</omitted_research>"
    )


def _outline_brief_request(state: RunState, settings: Settings) -> str:
    """The message that asks the Outliner for a plan.

    Built in one place because two executors send it: the normal path through
    ``SourceGate`` and the resume path through ``DossierEntry``. The Outliner gets
    the dossier **whole** — it cannot choose what to leave out of research it was
    never shown.
    """
    topic = state.topic
    dossier = state.dossier
    assert topic is not None and dossier is not None
    fmt = topic.post_format or "analysis"
    lo, hi = settings.word_target(fmt, state.voice_mode)
    return f"""
Plan this post. Decide what it argues and what it leaves out.

<topic>
{as_json(topic)}
</topic>

<dossier>
{as_json(dossier)}
</dossier>{state.unresolved_source_issues}{_editor_instructions(state)}

<word_target>{lo} to {hi} words in total</word_target>

Return the complete PostOutline JSON.
""".strip()


def _first_draft_request(state: RunState, settings: Settings) -> str:
    """The message that asks the Writer for revision 1, against the approved plan."""
    topic = state.topic
    dossier = state.dossier
    outline = state.outline
    assert topic is not None and dossier is not None
    return f"""
Write the first draft of this post, following the approved outline exactly.

<thesis>
{outline.thesis if outline else topic.angle}
</thesis>

<approved_outline>
{_outline_brief(outline)}
</approved_outline>

<topic>
{as_json(topic)}
</topic>

<research>
{as_json(_scoped_dossier(dossier, outline))}
</research>{_omitted_research(dossier, outline)}{state.unresolved_source_issues}\
{_editor_instructions(state)}

{_author_context(state, settings)}

Return the complete Draft JSON. Set revision to 1, and copy the thesis above into
the `thesis` field verbatim.
""".strip()


def repair_outline(
    outline: PostOutline,
    dossier: ResearchDossier,
    settings: Settings,
    *,
    band: tuple[int, int],
    fallback_thesis: str = "",
) -> PostOutline:
    """Every check the outline gate makes, and every fix it applies.

    Pure, so it is testable without building a workflow. Returns a repaired copy
    with ``warnings`` recording each change.

    It never raises and never rejects, and that is the design, not a shortcut.
    Every problem below has one obviously correct deterministic repair, and a loop
    is for work a model must redo. Adding a third round counter would mean a new
    env var, a new bound, a new "what happens when the budget is spent" answer, and
    a third exception to an invariant that currently holds exactly twice. The one
    genuinely irreparable case (too few sections) is passed through with a warning
    and left to the revision loop, which is already bounded: a thin post that says
    NOT APPROVED beats no post at all.
    """
    warnings: list[str] = []
    known = {claim.id for claim in dossier.claims}
    lo = int(settings.structure.get("min_sections", 5))
    hi = int(settings.structure.get("max_sections", 7))

    sections: list[OutlineSection] = []
    for section in outline.sections:
        resolved = [cid for cid in section.claim_ids if cid in known]
        if invented := [cid for cid in section.claim_ids if cid not in known]:
            warnings.append(
                f"Section {section.title!r} cited claim ids the dossier does not have: "
                f"{', '.join(invented)}. Dropped."
            )
        sections.append(section.model_copy(update={"claim_ids": resolved}))

    # The closing section is the only one allowed to rest on no claims, because it
    # is an opinion. Anything else with nothing behind it would be written from
    # thin air, which is exactly what H01 exists to stop.
    if sections:
        kept: list[OutlineSection] = []
        for i, section in enumerate(sections):
            if section.claim_ids or i >= len(sections) - 1:
                kept.append(section)
            else:
                warnings.append(
                    f"Section {section.title!r} was left with no dossier claims. Dropped."
                )
        sections = kept

    if len(sections) > hi:
        # Keep the two mandatory tail sections and cut from the free middle, so a
        # truncation can never remove the critical-read or the closing section.
        cut = [s.title for s in sections[hi - 2:-2]]
        sections = sections[: hi - 2] + sections[-2:]
        if cut:
            warnings.append(f"{len(cut)} sections over the cap of {hi}; dropped: {', '.join(cut)}.")
    elif len(sections) < lo:
        warnings.append(
            f"Only {len(sections)} sections against a floor of {lo}. Passed through anyway; "
            "S02 and F04 will raise it on the draft."
        )

    out_of_scope = [s.strip() for s in outline.out_of_scope if s.strip()]
    if not out_of_scope:
        # Synthesised from the claims the outline did not select: those statements
        # are literally the material this post is not covering, so the field can
        # rarely come back empty and the focus rules have something to check.
        used = {cid for s in sections for cid in s.claim_ids}
        out_of_scope = [c.statement.strip() for c in dossier.claims if c.id not in used][:5]
        warnings.append(
            "The outline excluded nothing."
            + (
                f" out_of_scope was derived from the {len(out_of_scope)} unused dossier claims."
                # A thin dossier can legitimately leave nothing spare. Say so rather
                # than passing an empty list off as a decision: F02 and F03 have
                # nothing to check against it, and the human should know that.
                if out_of_scope
                else " Every claim was used, so there is nothing to derive it from and the "
                "focus rules have no scope boundary to check."
            )
        )

    thesis = outline.thesis.strip()
    if not thesis:
        thesis = fallback_thesis.strip() or dossier.summary.strip().split(". ")[0]
        warnings.append("The outline carried no thesis; fell back to the topic angle.")

    planned = sum(max(0, s.target_words) for s in sections)
    target = (band[0] + band[1]) // 2
    if sections and planned and not band[0] <= planned <= band[1]:
        scale = target / planned
        # Clamped to the per-section bounds afterwards, so rescaling to hit the
        # total can never hand the Writer a section budget that F04 will then
        # fail. The total may land slightly off the midpoint; the per-section
        # floor is the constraint that actually protects the argument.
        floor = int(settings.structure.get("min_section_words", 250))
        ceiling = int(settings.structure.get("max_section_words", 450))
        sections = [
            s.model_copy(
                update={"target_words": min(ceiling, max(floor, round(s.target_words * scale)))}
            )
            for s in sections
        ]
        warnings.append(
            f"Planned {planned} words against a band of {band[0]} to {band[1]}; "
            f"rescaled to about {target}, each section clamped to {floor}-{ceiling}."
        )

    return outline.model_copy(
        update={
            "thesis": thesis,
            "out_of_scope": out_of_scope,
            "sections": sections,
            "warnings": warnings,
        }
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
    outline: PostOutline | None = None
    draft: Draft | None = None
    source_verdict: SourceVerdict | None = None
    reports: list[ValidationReport] = field(default_factory=list)
    # The only two round counters in the system, each with exactly one bound. The
    # outline stage deliberately adds no third: every check its gate makes is
    # deterministically repairable, so there is nothing to send back and re-ask.
    source_round: int = 0
    revision_round: int = 0
    dossier_path: str = ""
    outline_path: str = ""
    # The source checker's unresolved findings, rendered once by SourceGate and
    # carried so OutlineGate can pass the same warning to the Writer. Rebuilding
    # it in two places is how the two gates start disagreeing.
    unresolved_source_issues: str = ""
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


def _parse_scout_reports(
    responses: list[AgentExecutorResponse],
) -> tuple[list[ScoutReport], list[str]]:
    """Parse the scout responses into reports, and into blocks for the editor.

    A scout that returns unparsable output still contributes its raw text to the
    editor's brief — losing one scout's findings to a schema slip would be a
    worse outcome than handing the editor slightly messier input. Such a report
    cannot be harvested for sources, so the two return values differ in length
    exactly when a scout failed to parse.
    """
    reports: list[ScoutReport] = []
    blocks: list[str] = []
    for response in responses:
        label = response.executor_id
        try:
            report = parse_model(response, ScoutReport)
            # The executor id is the scout's real identity; whatever the model
            # put in `scout` is a self-description and drifts between runs.
            report.scout = label or report.scout
            reports.append(report)
            blocks.append(f"<scout name=\"{label}\">\n{as_json(report)}\n</scout>")
            logger.info("scout %s returned %d items", label, len(report.items))
        except ValueError as exc:
            logger.warning("scout %s produced unparsable output: %s", label, exc)
            blocks.append(
                f"<scout name=\"{label}\" parse_error=\"true\">\n"
                f"{(response.agent_response.text if response.agent_response else '')[:4000]}\n</scout>"
            )
    return reports, blocks


def _editor_brief(blocks: list[str], preamble: str = "") -> str:
    lead = preamble or (
        "Here are the raw scout reports. Synthesise them into the ranked topic "
        "shortlist as instructed."
    )
    return lead + "\n\n" + "\n\n".join(blocks)


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
        _, blocks = _parse_scout_reports(responses)
        await ctx.send_message(
            AgentExecutorRequest(messages=[user_message(_editor_brief(blocks))], should_respond=True)
        )


class SourceHarvester(Executor):
    """Fan-in for an exploration sweep: ends the run with a list of sites to vet.

    This is where a wide sweep deliberately stops. The scouts have read the open
    web, but nothing they found may reach the topic editor until a human has said
    which sites are acceptable — so this executor yields the candidate list and
    the raw reports, and the run finishes. The shortlist is built later, by
    :class:`ScoutReplay`, once the verdict exists.
    """

    def __init__(
        self, settings: Settings, instruction: str = "", id: str = "source_harvester"
    ) -> None:
        super().__init__(id)
        self.settings = settings
        self.instruction = instruction
        self.result: SourceReviewSet | None = None

    @handler
    async def collect(
        self,
        responses: list[AgentExecutorResponse],
        ctx: WorkflowContext[Never, SourceReviewSet],
    ) -> None:
        from .sources import harvest_candidates

        reports, _ = _parse_scout_reports(responses)
        candidates = harvest_candidates(reports, declined=self.settings.declined_domains)
        review = SourceReviewSet(
            generated_on=date.today().isoformat(),
            instruction=self.instruction,
            candidates=candidates,
            reports=reports,
        )
        self.result = review
        logger.info(
            "harvested %d sites (%d new) from %d signals — awaiting approval",
            len(candidates),
            sum(1 for c in candidates if not c.known),
            sum(len(r.items) for r in reports),
        )
        await ctx.yield_output(review)


@dataclass
class ShortlistRequest:
    """Start message for the second half of an exploration run."""

    reports: list[ScoutReport]
    approved: list[str] = field(default_factory=list)
    instruction: str = ""


class ScoutReplay(Executor):
    """Entry point for the shortlist half: vetted reports in, editor brief out.

    The filtering happens here rather than at the caller so there is exactly one
    place where "the editor only ever sees approved sources" is enforced.
    """

    def __init__(self, settings: Settings, id: str = "scout_replay") -> None:
        super().__init__(id)
        self.settings = settings

    @handler
    async def start(
        self, request: ShortlistRequest, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        from .sources import filter_reports

        reports = filter_reports(request.reports, request.approved)
        kept = sum(len(r.items) for r in reports)
        logger.info(
            "replaying %d signals from %d approved sources", kept, len(request.approved)
        )
        preamble = (
            f"{request.instruction.strip()}\n\n" if request.instruction.strip() else ""
        ) + (
            "Here are the scout reports for this run. They have already been filtered "
            "to the sources the blog's editor approved:\n"
            f"{', '.join(sorted(request.approved)) or '(none)'}\n\n"
            "Everything from a source the editor turned down has been removed. Work "
            "only from what is here — do not reintroduce material from memory, and do "
            "not lower the bar because the volume is smaller. Synthesise the ranked "
            "topic shortlist as instructed."
        )
        blocks = [f"<scout name=\"{r.scout}\">\n{as_json(r)}\n</scout>" for r in reports]
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[user_message(_editor_brief(blocks, preamble))], should_respond=True
            )
        )


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
        # Still through the outliner. A regeneration that skipped straight to the
        # Writer would be the one path with no thesis, which is precisely how the
        # same research produced a differently-argued post last time.
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[user_message(_outline_brief_request(self.state, self.settings))],
                should_respond=True,
            ),
            target_id=A.OUTLINER,
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


class OutlineGate(Executor):
    """Checks the plan against the dossier in code, repairs it, and briefs the Writer.

    The routing decision this stage does *not* make is the interesting one: there
    is no branch back to the Outliner and no ``outline_round``. See
    ``repair_outline`` for why every failure here is a deterministic fix rather
    than a re-ask.

    Like ``DossierGate``, it writes its artefact before sending anything on, so a
    failure at the Writer costs you the Writer and not the decision about what the
    post argues.
    """

    def __init__(self, state: RunState, settings: Settings, id: str = "outline_gate") -> None:
        super().__init__(id)
        self.state = state
        self.settings = settings

    @handler
    async def route(
        self, response: AgentExecutorResponse, ctx: WorkflowContext[AgentExecutorRequest]
    ) -> None:
        topic = self.state.topic
        dossier = self.state.dossier
        assert topic is not None and dossier is not None

        outline = parse_model(response, PostOutline)
        offered = len(dossier.claims)
        outline = repair_outline(
            outline,
            dossier,
            self.settings,
            band=self.settings.word_target(topic.post_format or "analysis", self.state.voice_mode),
            fallback_thesis=topic.angle,
        )
        self.state.outline = outline
        self.state.outline_path = str(storage.save_outline(outline, topic.slug or topic.title))

        logger.info(
            "outline ready: %d sections, %d of %d claims selected, %d out of scope",
            len(outline.content_sections),
            len(outline.selected_claim_ids),
            offered,
            len(outline.out_of_scope),
        )
        for warning in outline.warnings:
            logger.warning("outline repaired: %s", warning)

        await ctx.send_message(
            AgentExecutorRequest(
                messages=[user_message(_first_draft_request(self.state, self.settings))],
                should_respond=True,
            ),
            target_id=A.WRITER,
        )


class SourceGate(Executor):
    """Routes on the source verdict: back to the researcher, or on to the outliner."""

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
        self.state.unresolved_source_issues = (
            ""
            if verdict.passed
            else (
                "\n\n<unresolved_source_issues>\n"
                "The source checker could not clear everything. Do NOT state the following as "
                "settled fact; either drop the claim or hedge it explicitly and flag it in your "
                f"changelog.\n{as_json(verdict.findings)}\n</unresolved_source_issues>"
            )
        )
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[user_message(_outline_brief_request(self.state, self.settings))],
                should_respond=True,
            ),
            target_id=A.OUTLINER,
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
        if not draft.thesis and self.state.outline:
            # Same shape as the word_count fallback above: a field the model was
            # told to fill, filled in code when it did not, so everything
            # downstream (the front matter, a later regeneration) can rely on it.
            draft.thesis = self.state.outline.thesis
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
            # C04's band is per format and shrinks in analysis mode, so the word
            # target the writer was given is the one it is judged against.
            post_format=draft.post_format,
            voice_mode=self.state.voice_mode,
            outline=self.state.outline,
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
            outline = self.state.outline
            # Only the Content Validator gets the outline. The Design Validator
            # judges shape and typography; whether the post argues one thing is an
            # editorial question, and giving both the same material is how one
            # validator ends up doing neither job well.
            extra = (
                f"\n\n<voice_mode>{self.state.voice_mode}</voice_mode>"
                f"\n\n<author_claims>\n{claims}\n</author_claims>"
                f"\n\n<thesis>\n{outline.thesis if outline else '(no outline)'}\n</thesis>"
                "\n\n<out_of_scope>\n"
                + ("\n".join(f"- {s}" for s in outline.out_of_scope) if outline else "(none)")
                + "\n</out_of_scope>"
                f"\n\n<approved_outline>\n{_outline_brief(outline)}\n</approved_outline>"
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
        outline = self.state.outline
        draft = self.state.draft
        # The thesis and the outline are restated every single round. Without
        # them, three rewrites happen steered only by style findings, with no
        # statement anywhere of what the post is supposed to be arguing — which is
        # how a draft ends up further from its brief the more it is revised.
        #
        # The draft is resent for the same reason. It used to arrive only via the
        # AgentSession history, which made the prompt unreadable in a log and put
        # the correctness of a rewrite at the mercy of how much of a growing thread
        # the model still attended to.
        prompt = f"""
The validators reviewed revision {self.state.revision_round} of your draft. Address every
blocker and major finding, then return the full corrected Draft JSON with
revision={self.state.revision_round + 1} and a changelog describing what you changed.
Change nothing factual while fixing style, and do not drift off the thesis to
satisfy a style finding.

<thesis>
{outline.thesis if outline else ''}
</thesis>

<approved_outline>
{_outline_brief(outline)}
</approved_outline>

<validator_findings>
{instructions}
</validator_findings>

<current_draft_markdown>
{draft.markdown if draft else ''}
</current_draft_markdown>

{_author_context(self.state, self.settings)}
{_editor_instructions(self.state)}

<research>
{as_json(_scoped_dossier(dossier, outline)) if dossier else '{}'}
</research>
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


#: Minors are worth sending but not worth a runaway prompt, and a long tail of
#: them buries the blockers above.
_MAX_MINORS_PER_VALIDATOR = 5


def _revision_text(reports: list[ValidationReport]) -> str:
    """Validator findings as an instruction to the Writer.

    Minors are included, subordinate to the graded findings. They used to be
    filtered out entirely, so a validator could write a precise fix, deduct points
    for it, and have the Writer never see it: one real run carried the same two
    minors unfixed through all three revision rounds while the config said "fix
    blockers first, then majors, then minors". ``info`` stays out — it never
    deducts and never blocks.
    """
    lines: list[str] = []
    for report in reports:
        graded = [f for f in report.findings if f.severity in {"blocker", "major"}]
        minors = [f for f in report.findings if f.severity == "minor"]
        if not graded and not minors:
            continue
        lines.append(f"From the {report.validator} validator (score {report.score}/100):")
        for f in sorted(graded, key=lambda x: 0 if x.severity == "blocker" else 1):
            where = f" [{f.location}]" if f.location else ""
            lines.append(f"  - {f.rule_id} ({f.severity}){where}: {f.problem}")
            lines.append(f"    FIX: {f.fix}")
        if minors:
            lines.append("  Also, where you can do it without disturbing anything above:")
            for f in minors[:_MAX_MINORS_PER_VALIDATOR]:
                where = f" [{f.location}]" if f.location else ""
                lines.append(f"  - {f.rule_id} (minor){where}: {f.problem}")
                lines.append(f"    FIX: {f.fix}")
            if len(minors) > _MAX_MINORS_PER_VALIDATOR:
                lines.append(
                    f"  ({len(minors) - _MAX_MINORS_PER_VALIDATOR} further minor findings "
                    "omitted from this round.)"
                )
        lines.append("")
    return "\n".join(lines).strip() or "No findings this round."


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
        report_path = storage.save_review_report(
            draft, outcome, markdown_path=markdown_path, outline=self.state.outline
        )
        package = PostPackage(
            draft=draft,
            dossier=dossier,
            outline=self.state.outline,
            outline_path=self.state.outline_path,
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


# ---------------------------------------------------------------------------
# Newsletter pipeline
#
# Two code gates around one agent. The agent decides what is worth including and
# what to say about it; everything else — which articles exist, how many, and
# whether an item survives — is plain Python here.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NewsletterCandidate:
    """One article offered to the editor, and the truth it is resolved back to."""

    id: int
    title: str
    url: str
    source: str
    published: str
    summary: str


@dataclass(slots=True)
class IssueRequest:
    """Everything the graph needs to compose one issue."""

    newsletter: dict[str, Any]
    candidates: list[NewsletterCandidate]
    window_from: str = ""
    window_to: str = ""
    instruction: str = ""


@dataclass(slots=True)
class ComposedIssue:
    """The finished article of the graph — rendered, but not yet stored."""

    newsletter_id: int
    subject: str
    preheader: str
    intro: str
    sections: list[dict[str, Any]]
    article_ids: list[int]
    dropped: list[int]
    omitted: list[int]
    skipped_reason: str = ""
    generated_on: str = ""

    @property
    def item_count(self) -> int:
        return len(self.article_ids)


class IssueBuilder(Executor):
    """Turns candidate articles into a brief. Makes the only branch in the graph.

    If there are fewer candidates than the newsletter's `min_items`, the run ends
    here and **no model is called at all**. That is what "no routing decision is
    made by a model" looks like in this pipeline: the sole decision point is an
    integer comparison, and a quiet week costs nothing rather than producing a
    padded issue.
    """

    def __init__(self, id: str = "issue_builder") -> None:
        super().__init__(id)
        self.skipped: ComposedIssue | None = None
        self.request: IssueRequest | None = None

    @handler
    async def start(
        self,
        request: IssueRequest,
        ctx: WorkflowContext[AgentExecutorRequest, ComposedIssue],
    ) -> None:
        self.request = request
        newsletter = request.newsletter
        minimum = int(newsletter.get("min_items", 3) or 0)

        if len(request.candidates) < minimum:
            reason = (
                f"only {len(request.candidates)} article(s) in the window, "
                f"below the minimum of {minimum}"
            )
            logger.info("skipping issue: %s", reason)
            self.skipped = ComposedIssue(
                newsletter_id=int(newsletter.get("id", 0)),
                subject="",
                preheader="",
                intro="",
                sections=[],
                article_ids=[],
                dropped=[],
                omitted=[],
                skipped_reason=reason,
                generated_on=date.today().isoformat(),
            )
            await ctx.yield_output(self.skipped)
            return

        logger.info(
            "composing '%s' from %d candidate(s)",
            newsletter.get("name", "newsletter"),
            len(request.candidates),
        )
        await ctx.send_message(
            AgentExecutorRequest(messages=[user_message(_issue_brief(request))], should_respond=True)
        )


def _issue_brief(request: IssueRequest) -> str:
    """The candidate list, numbered. The ids here are the only ones that resolve."""
    lines = []
    for c in request.candidates:
        lines.append(
            f"[{c.id}] {c.title}\n"
            f"     source: {c.source}   published: {c.published or 'unknown'}\n"
            f"     {c.summary[:400]}"
        )
    window = (
        f"Covering {request.window_from} to {request.window_to}."
        if request.window_from
        else "Covering the current window."
    )
    extra = f"\n\n{request.instruction.strip()}" if request.instruction.strip() else ""
    return (
        f"{window} There are {len(request.candidates)} candidate articles below.\n\n"
        "Refer to each by its bracketed id. Do not produce URLs.\n\n"
        + "\n\n".join(lines)
        + extra
    )


class IssuePublisher(Executor):
    """Resolves the editor's plan back to real articles, and drops what it cannot.

    This is the anti-fabrication gate, and the direct analogue of ``ScoutReplay``
    filtering to approved sources. The editor is given ids and returns ids; any
    id that was not in the candidate list is discarded here, along with any
    section that is not in the configured taxonomy. An email cannot be un-sent,
    so a link nobody verified must never reach one.

    It also enforces the caps rather than trusting the model to have counted.
    """

    def __init__(self, settings: Settings, id: str = "issue_publisher") -> None:
        super().__init__(id)
        self.settings = settings
        self.result: ComposedIssue | None = None
        self.request: IssueRequest | None = None

    @handler
    async def publish(
        self,
        response: AgentExecutorResponse,
        ctx: WorkflowContext[Never, ComposedIssue],
    ) -> None:
        from .models import NewsletterIssueDraft

        draft = parse_model(response, NewsletterIssueDraft)
        request = self.request
        assert request is not None, "IssuePublisher ran without a request"

        by_id = {c.id: c for c in request.candidates}
        allowed_sections = {s["id"]: s for s in self.settings.newsletter_sections}
        editorial = self.settings.newsletter_editorial
        headline_cap = int(editorial.get("headline_max_chars", 90))
        blurb_cap = int(editorial.get("blurb_max_words", 40))
        max_items = int(request.newsletter.get("max_items", 12) or 12)

        sections: list[dict[str, Any]] = []
        used: list[int] = []
        dropped: list[int] = []
        seen: set[int] = set()

        for section in draft.sections:
            if section.id not in allowed_sections:
                logger.info("dropping section %r — not in the configured taxonomy", section.id)
                dropped.extend(item.article_id for item in section.items)
                continue

            items: list[dict[str, Any]] = []
            for item in section.items:
                candidate = by_id.get(item.article_id)
                if candidate is None:
                    # The editor named something it was not given.
                    logger.warning("dropping item %s — not in the candidate list", item.article_id)
                    dropped.append(item.article_id)
                    continue
                if item.article_id in seen:
                    dropped.append(item.article_id)
                    continue
                if len(used) >= max_items:
                    dropped.append(item.article_id)
                    continue
                seen.add(item.article_id)
                used.append(item.article_id)
                items.append(
                    {
                        "article_id": candidate.id,
                        # URL and source come from the candidate, never the model.
                        "url": candidate.url,
                        "source": candidate.source,
                        "published": candidate.published,
                        "headline": (item.headline or candidate.title).strip()[:headline_cap],
                        "blurb": _cap_words(item.blurb, blurb_cap),
                    }
                )

            if items:
                sections.append(
                    {
                        "id": section.id,
                        "title": section.title or allowed_sections[section.id].get("title", section.id),
                        "items": items,
                    }
                )

        if dropped:
            logger.info("issue gate dropped %d item(s) the editor named", len(dropped))

        self.result = ComposedIssue(
            newsletter_id=int(request.newsletter.get("id", 0)),
            subject=draft.subject.strip()[:300],
            preheader=draft.preheader.strip()[:300],
            intro=_cap_words(draft.intro, int(editorial.get("intro_max_words", 80))),
            sections=sections,
            article_ids=used,
            dropped=dropped,
            omitted=list(draft.omitted),
            generated_on=date.today().isoformat(),
        )
        logger.info("issue composed: %d item(s) across %d section(s)", len(used), len(sections))
        await ctx.yield_output(self.result)


def _cap_words(text: str, limit: int) -> str:
    words = (text or "").strip().split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).rstrip(",;:") + "…"
