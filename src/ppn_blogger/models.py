"""Typed contracts passed between agents and workflow executors.

Every agent that must produce machine-readable output is bound to one of these
models via ``response_format``, so the workflow never has to guess at prose.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

# "info" was added with the v2 ruleset (rule T03 is info): a finding worth
# surfacing that never deducts and never blocks.
Severity = Literal["blocker", "major", "minor", "info"]
TrustTier = Literal[
    "official", "standards", "community_trusted", "vendor", "community_unverified", "unknown"
]


# ---------------------------------------------------------------------------
# Topic discovery
# ---------------------------------------------------------------------------


class SignalItem(BaseModel):
    """One raw thing a scout found worth paying attention to."""

    title: str
    url: str
    published: str | None = Field(None, description="ISO date if known")
    source_name: str | None = None
    why_it_matters: str = Field(..., description="One sentence: why a Power Platform pro should care")
    watch_area: str = Field(..., description="id of the watch area from topics.yaml")


class ScoutReport(BaseModel):
    scout: str
    items: list[SignalItem] = Field(default_factory=list)
    notes: str = ""


class TopicSuggestion(BaseModel):
    title: str = Field(..., description="Working title, 45-65 chars, contains the primary keyword")
    slug: str
    watch_area: str
    post_format: str = Field(..., description="one of the post_formats ids in blog_profile.yaml")
    primary_keyword: str
    angle: str = Field(..., description="The specific take that makes this post worth writing")
    problem_statement: str = Field(..., description="The reader pain this post resolves")
    why_now: str = Field(..., description="What makes this timely — the news hook or the recurring pain")
    key_questions: list[str] = Field(default_factory=list, description="What the research must answer")
    seed_sources: list[str] = Field(default_factory=list, description="URLs the researcher should start from")
    novelty: str = Field(..., description="What this adds beyond the official docs")
    audience_fit: int = Field(..., ge=1, le=5)
    timeliness: int = Field(..., ge=1, le=5)
    effort: int = Field(..., ge=1, le=5, description="1 = quick win, 5 = major research project")
    score: float = Field(..., ge=0, le=100)
    duplicate_risk: str = Field("", description="Existing PPN post this may overlap with, or empty")


class TopicSuggestionSet(BaseModel):
    generated_on: str
    suggestions: list[TopicSuggestion] = Field(default_factory=list)
    discarded: list[str] = Field(default_factory=list, description="Ideas rejected and why")


# ---------------------------------------------------------------------------
# Source exploration
#
# A wide-web sweep is only useful if the sites it turns up can be vetted before
# they influence anything. These three models carry that review: what the
# scouts drew on, what the operator decided, and the raw reports the shortlist
# is later built from. None of them is bound to an agent — the harvest is code,
# not judgement, which is what keeps the candidate list honest about where the
# scouts actually went.
# ---------------------------------------------------------------------------


class SourceCandidate(BaseModel):
    """One site the scouts drew on, offered to the operator for approval."""

    domain: str
    name: str = Field("", description="Human label, taken from the scout's source_name")
    known: bool = Field(False, description="True when sources.yaml already gives it a tier")
    current_tier: str = Field("unknown", description="Tier it classifies as today")
    suggested_tier: str = Field(
        "community_unverified", description="Tier to apply on approval unless the operator changes it"
    )
    item_count: int = 0
    scouts: list[str] = Field(default_factory=list, description="Which scouts reported it")
    items: list[SignalItem] = Field(default_factory=list, description="What was found there")


class SourceDecision(BaseModel):
    """The operator's verdict on one candidate."""

    domain: str
    approved: bool = False
    tier: str = Field(
        "community_unverified", description="Trust tier to file an approved domain under"
    )


class SourceReviewSet(BaseModel):
    """Everything the shortlist stage needs once the review is settled."""

    generated_on: str
    instruction: str = ""
    candidates: list[SourceCandidate] = Field(default_factory=list)
    reports: list[ScoutReport] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Author notes
#
# The one place the Writer is allowed to draw first person, real numbers and
# real failures from. Raw notes are normalised into these typed claims by the
# notes normalizer, so downstream agents move a list of testimony rather than
# free prose. Author claims are testimony: the Source Checker passes them
# through untouched and never fails a run on them.
# ---------------------------------------------------------------------------


AuthorClaimType = Literal[
    "measurement", "failure", "limit", "environment", "exact_string", "opinion", "context"
]


class AuthorClaim(BaseModel):
    id: str = Field(..., description="Short stable id, e.g. A1 — referenced by the Writer")
    type: AuthorClaimType
    text: str = Field(..., description="The claim, taken from the notes, never invented")
    scope: str = Field("", description="Where it holds: tenant, region, date, environment type")
    verbatim: bool = Field(
        False, description="True when `text` must be reproduced exactly (error strings, names)"
    )
    author_attested: bool = Field(
        True, description="Always true — this is the author's own testimony, not researched"
    )


class AuthorClaimSet(BaseModel):
    """What the normalizer produces from the raw notes file."""

    claims: list[AuthorClaim] = Field(default_factory=list)
    # Set in code, not by the model: field_report when there are real notes,
    # analysis when the file is missing or still the unfilled template.
    voice_mode: Literal["field_report", "analysis"] = "analysis"
    summary: str = ""


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    id: str = Field(..., description="Short stable id, e.g. S1, S2 — referenced by claims")
    title: str
    url: str
    publisher: str = ""
    published: str | None = None
    accessed: str = Field(default_factory=lambda: date.today().isoformat())
    tier: TrustTier = "unknown"
    excerpt: str = Field("", description="The exact sentence(s) that support the claim")


class Claim(BaseModel):
    id: str = Field(..., description="Short stable id, e.g. C1")
    statement: str
    criticality: Literal["critical", "supporting", "colour"] = "supporting"
    citation_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    caveat: str = Field("", description="Preview status, version, tenant-specific behaviour, ...")


class ResearchDossier(BaseModel):
    topic_title: str
    primary_keyword: str
    post_format: str
    summary: str = Field(..., description="What the research concluded, 3-6 sentences")
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list, description="Code, formulas, configs the writer can use")
    gotchas: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)
    licensing_notes: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    suggested_outline: list[str] = Field(default_factory=list)
    internal_link_candidates: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Source checking
# ---------------------------------------------------------------------------


class SourceFinding(BaseModel):
    citation_id: str = ""
    claim_id: str = ""
    url: str = ""
    issue: str
    severity: Severity
    evidence: str = ""
    remediation: str


class SourceVerdict(BaseModel):
    passed: bool
    average_trust: float = Field(0.0, ge=0, le=5)
    unreachable_urls: list[str] = Field(default_factory=list)
    fabricated_urls: list[str] = Field(default_factory=list)
    blocked_domains_cited: list[str] = Field(default_factory=list)
    uncorroborated_critical_claims: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    findings: list[SourceFinding] = Field(default_factory=list)
    instructions_for_researcher: str = Field(
        "", description="Exactly what to go and fix. Empty when passed."
    )
    summary: str = ""


# ---------------------------------------------------------------------------
# Outlining
#
# The stage that turns a dossier into an argument. A dossier answers "what is
# true"; an outline answers "what is this post arguing", and those are different
# questions. Without one, the Writer was handed every verified claim at once and
# a mandate to fill eight to twelve sections, which is a recipe for a survey: one
# real draft spent forty percent of its body on a product that was not its topic.
#
# The outliner returns *claim ids*, never claim text — the same contract as the
# newsletter editor, and for the same reason. `OutlineGate` resolves each id back
# to the claim it came from and drops what it cannot account for, so a section
# cannot be built on research that does not exist.
#
# `out_of_scope` is the field that does the work. Anyone can list what a post
# covers; naming what it deliberately leaves out is the decision, and it is what
# the focus rules later check a wandering heading against.
# ---------------------------------------------------------------------------


class OutlineSection(BaseModel):
    title: str = Field(..., description="The H2 as it will appear. Descriptive and specific.")
    makes_this_point: str = Field(
        ..., description="ONE sentence: the single point this section establishes. Not a summary."
    )
    claim_ids: list[str] = Field(
        default_factory=list,
        description="Dossier claim ids this section rests on. Never invent one.",
    )
    evidence_kind: Literal["prose", "list", "table", "code", "callout"] = "prose"
    target_words: int = Field(300, ge=0)


class PostOutline(BaseModel):
    thesis: str = Field(
        ...,
        description=(
            "One sentence. The argument the post makes, stated so a reader could "
            "disagree with it. Not the topic, and not a list of what the post covers."
        ),
    )
    reader_promise: str = Field(
        ..., description="What the reader can do afterwards that they could not before"
    )
    out_of_scope: list[str] = Field(
        default_factory=list,
        description=(
            "Subjects the research supports that this post deliberately leaves out. "
            "Never empty: a post that excludes nothing has no thesis."
        ),
    )
    sections: list[OutlineSection] = Field(default_factory=list)
    working_title: str = ""
    # Set in code by OutlineGate, never by the model — same contract as
    # AuthorClaimSet.voice_mode. Every deterministic repair the gate made, so the
    # review report can show the human what was wrong with the plan.
    warnings: list[str] = Field(default_factory=list)

    @property
    def selected_claim_ids(self) -> list[str]:
        """Every claim id the outline uses, in order, without duplicates."""
        seen: dict[str, None] = {}
        for section in self.sections:
            for claim_id in section.claim_ids:
                seen.setdefault(claim_id, None)
        return list(seen)

    @property
    def content_sections(self) -> list[OutlineSection]:
        """The sections that become H2s, excluding any Contents/Sources slip."""
        skip = {"contents", "sources", "contenido", "fuentes"}
        return [s for s in self.sections if s.title.strip().casefold() not in skip]


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


class Draft(BaseModel):
    title: str
    slug: str
    meta_description: str
    primary_keyword: str
    category: str
    tags: list[str] = Field(default_factory=list)
    post_format: str = "analysis"
    excerpt: str = ""
    thesis: str = Field(
        "", description="The approved outline's thesis, restated verbatim. Do not reword it."
    )
    markdown: str = Field(..., description="Full post body in Markdown, starting with the H1")
    cover_concept: str = Field(
        "",
        description=(
            "Neon-lit graphic scene for the cover, drawn from the post's own subject "
            "matter — shapes, geometry and light. Never any text, logos or UI."
        ),
    )
    internal_links: list[str] = Field(default_factory=list)
    word_count: int = 0
    read_minutes: int = 0
    revision: int = 0
    changelog: str = Field("", description="What changed in this revision, empty on first draft")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class RuleFinding(BaseModel):
    rule_id: str
    severity: Severity
    location: str = Field("", description="Heading, line or quoted snippet where it occurs")
    problem: str
    fix: str = Field(..., description="Concrete rewrite instruction the writer can act on")


class ValidationReportDraft(BaseModel):
    """The validator agent's structured output — everything the model produces.

    ``measurements`` is deliberately absent here: it is a free-form ``dict`` that
    the code-side detectors fill in afterwards, and a free-form dict cannot be
    expressed under Azure's strict structured-output rule (every object must set
    ``additionalProperties: false``, which an open dict by definition cannot).
    Binding this slim schema as the agent ``response_format`` is what keeps the
    validator call valid; ``ValidationReport`` adds the code-only field back for
    internal use. See the output_schema block in validation_rules.yaml.
    """

    validator: str
    score: int = Field(..., ge=0, le=100)
    passed: bool
    findings: list[RuleFinding] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    summary: str = ""
    resolved_since_last_iteration: list[str] = Field(
        default_factory=list, description="Rule ids that fired last round and no longer do"
    )


class ValidationReport(ValidationReportDraft):
    # Filled by the code-side detectors, not the model: numeric facts the model
    # must never be asked to count (avg_sentence_words, section_word_counts,
    # h2_count, dash_hits, banned_word_hits, ...). Never bind this schema to an
    # agent — bind ValidationReportDraft, which omits this open dict.
    measurements: dict[str, Any] = Field(default_factory=dict)


class ReviewOutcome(BaseModel):
    approved: bool
    revision: int
    overall_score: float
    blockers: int
    reports: list[ValidationReport] = Field(default_factory=list)
    source_verdict: SourceVerdict | None = None
    revision_instructions: str = ""


# ---------------------------------------------------------------------------
# Final package handed to the publisher
# ---------------------------------------------------------------------------


class CoverImage(BaseModel):
    path: str = ""
    prompt: str = ""
    alt_text: str = ""
    width: int = 0
    height: int = 0
    model: str = ""
    media_id: int | None = Field(None, description="WordPress media library id once uploaded")
    error: str = Field("", description="Why generation failed, when it did")

    @property
    def ok(self) -> bool:
        return bool(self.path) and not self.error


class PublishTarget(BaseModel):
    platform: Literal["wordpress", "file"] = "wordpress"
    post_id: int | None = None
    status: str = "draft"
    link: str = ""
    edit_link: str = ""
    featured_media_id: int | None = None


class PostPackage(BaseModel):
    draft: Draft
    dossier: ResearchDossier
    outline: PostOutline | None = None
    outline_path: str = Field("", description="Where the outline JSON was saved on disk")
    outcome: ReviewOutcome
    voice_mode: str = "analysis"
    author_claims: list[AuthorClaim] = Field(default_factory=list)
    notes_path: str = ""
    markdown_path: str = ""
    report_path: str = ""
    dossier_path: str = Field("", description="Where the research JSON was saved on disk")
    cover: CoverImage | None = None
    published: PublishTarget | None = None
    translation: Draft | None = Field(None, description="Localised version of the draft")
    translation_language: str = ""
    translation_path: str = ""
    translation_published: PublishTarget | None = None


# ---------------------------------------------------------------------------
# Newsletters
#
# The editor is handed a numbered list of candidate articles and returns a plan
# referring to them by id. It never supplies a URL, which is the whole point:
# `IssuePublisher` resolves every id back to the candidate it was given, so a
# fabricated link cannot reach an issue. An email cannot be un-sent, so this is
# the one place in the news subsystem where a hallucination would be permanent.
# ---------------------------------------------------------------------------


class NewsletterItem(BaseModel):
    """One article as the editor wants it to appear."""

    article_id: int = Field(..., description="The id from the candidate list. Never invent one.")
    headline: str = Field(..., description="Rewritten for the digest, under 90 characters")
    blurb: str = Field(..., description="One or two sentences: why this matters to the reader")
    section: str = Field(..., description="id of a section from the newsletter policy")


class NewsletterSection(BaseModel):
    id: str = Field(..., description="Section id from the policy")
    title: str = Field(..., description="Heading as it should appear")
    items: list[NewsletterItem] = Field(default_factory=list)


class NewsletterIssueDraft(BaseModel):
    """What the editor produces. Bound to the agent as its response_format."""

    subject: str = Field(..., description="Email subject line — specific, no clickbait")
    preheader: str = Field("", description="The preview line after the subject in an inbox")
    intro: str = Field("", description="A short paragraph framing the issue. May be empty.")
    sections: list[NewsletterSection] = Field(default_factory=list)
    omitted: list[int] = Field(
        default_factory=list,
        description="Candidate ids deliberately left out, so the choice is auditable",
    )


class FeedSuggestion(BaseModel):
    """One place a scout thinks is worth following.

    A *suggestion*, not a feed. Every one of these is fetched and parsed before
    the operator ever sees it, and anything that does not resolve to a real feed
    with recent entries is discarded — so a hallucinated URL cannot reach the
    review. The model is being asked where to look, not what is true.
    """

    url: str = Field(..., description="The feed URL, or the site's home page")
    name: str = Field(..., description="What to call it")
    topics: list[str] = Field(
        default_factory=list, description="Which watch topics it covers"
    )
    reason: str = Field(..., description="One sentence: why this is worth following")


class FeedSuggestionSet(BaseModel):
    suggestions: list[FeedSuggestion] = Field(default_factory=list)
    notes: str = Field("", description="Anything the operator should know about the sweep")
