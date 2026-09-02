"""Agent factories.

Each factory returns a configured ``agent_framework.Agent``. Agents are bound to
a Pydantic ``response_format`` so the workflow receives typed objects rather than
prose, and to the minimal tool set they need.
"""

from __future__ import annotations

from typing import Any

from agent_framework import Agent

from . import prompts, tools
from .clients import ClientBundle, hosted_web_search_tools
from .models import (
    AuthorClaimSet,
    DeltaAnalysis,
    Draft,
    FeedSuggestionSet,
    LearningProposal,
    NewsletterIssueDraft,
    PostOutline,
    ResearchDossier,
    ScoutReport,
    SourceVerdict,
    TopicSuggestion,
    TopicSuggestionSet,
    ValidationReportDraft,
)
from .settings import Settings

# Agent ids — also used as executor ids in the workflow graphs.
NEWS_SCOUT = "news_scout"
FEED_SCOUT = "feed_scout"
DOCS_SCOUT = "docs_scout"
TOPIC_EDITOR = "topic_editor"
BRIEF_INTERPRETER = "brief_interpreter"
NOTES_NORMALIZER = "notes_normalizer"
RESEARCHER = "researcher"
OUTLINER = "outliner"
WRITER = "writer"
CONTENT_VALIDATOR = "content_validator"
DESIGN_VALIDATOR = "design_validator"
SOURCE_CHECKER = "source_checker"
TRANSLATOR = "translator"
NEWSLETTER_EDITOR = "newsletter_editor"
FEED_DISCOVERY_SCOUT = "feed_discovery_scout"
DELTA_ANALYST = "delta_analyst"
LEARNING_DIAGNOSTICIAN = "learning_diagnostician"


def _opts(response_format: type, temperature: float | None = None) -> dict:
    """Build the per-agent options.

    `temperature` is dropped when the reasoning model does not accept it. Only
    the reasoning-tier agents (writer, validators, translator) ask for one, so
    the reasoning model is the right thing to check.
    """
    from .settings import get_settings

    options: dict = {"response_format": response_format}
    if temperature is not None and get_settings().foundry.supports_temperature:
        options["temperature"] = temperature
    return options


def _meter(agent_id: str, *, fast: bool = False) -> list[Any]:
    """Token accounting for one agent.

    Agent middleware rather than chat middleware, deliberately — see the module
    docstring in ``usage.py``. The model is passed in because ``AgentResponse``
    does not carry one, and ``fast`` mirrors the client the caller already
    picked, so the two can never disagree about which tier was billed.
    """
    from .settings import get_settings
    from .usage import UsageMeter

    settings = get_settings()
    if not settings.run.usage_tracking:
        return []
    model = (settings.foundry.fast_model or settings.foundry.model) if fast else settings.foundry.model
    return [UsageMeter(agent_id, model)]


def _searchable(base: list[Any], client: Any) -> list[Any]:
    """Resolve the web-search tool for an agent according to SEARCH_PROVIDER.

    - ``foundry``  → drop the local ``web_search`` function, attach Foundry's
      server-side web search tool instead.
    - ``tavily`` / ``brave`` → keep the local ``web_search`` function.
    - ``none``     → no open-web search at all; feeds and Microsoft Learn remain.
    """
    from .settings import get_settings

    settings = get_settings()
    resolved = list(base)
    if not settings.search.uses_local_tool:
        resolved = [t for t in resolved if t is not tools.web_search]
    return resolved + hosted_web_search_tools(client)


# ---------------------------------------------------------------------------
# Topic discovery crew
# ---------------------------------------------------------------------------


def build_news_scout(settings: Settings, clients: ClientBundle, *, explore: bool = False) -> Agent:
    return Agent(
        clients.fast,
        prompts.news_scout_instructions(settings, explore=explore),
        id=NEWS_SCOUT,
        name=NEWS_SCOUT,
        description="Scans the open web for recent, substantive Power Platform news.",
        tools=_searchable(tools.SCOUT_NEWS_TOOLS, clients.fast),
        default_options=_opts(ScoutReport),
        middleware=_meter(NEWS_SCOUT, fast=True),
    )


def build_feed_scout(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.fast,
        prompts.feed_scout_instructions(settings),
        id=FEED_SCOUT,
        name=FEED_SCOUT,
        description="Reads the curated first-party and MVP feeds for concrete changes.",
        tools=_searchable(tools.SCOUT_FEED_TOOLS, clients.fast),
        default_options=_opts(ScoutReport),
        middleware=_meter(FEED_SCOUT, fast=True),
    )


def build_docs_scout(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.fast,
        prompts.docs_scout_instructions(settings),
        id=DOCS_SCOUT,
        name=DOCS_SCOUT,
        description="Mines learn.microsoft.com for documented limits, licensing and preview status.",
        tools=_searchable(tools.SCOUT_DOCS_TOOLS, clients.fast),
        default_options=_opts(ScoutReport),
        middleware=_meter(DOCS_SCOUT, fast=True),
    )


def build_topic_editor(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.reasoning,
        prompts.topic_synthesizer_instructions(settings),
        id=TOPIC_EDITOR,
        name=TOPIC_EDITOR,
        description="Turns raw scout signals into a ranked, de-duplicated shortlist of post ideas.",
        tools=[tools.search_existing_posts, tools.today_tool],
        default_options=_opts(TopicSuggestionSet),
        middleware=_meter(TOPIC_EDITOR),
    )


# ---------------------------------------------------------------------------
# Post pipeline crew
# ---------------------------------------------------------------------------


def build_brief_interpreter(settings: Settings, clients: ClientBundle) -> Agent:
    """Turns the author's own brief into the topic record the pipeline expects.

    Fast tier and no tools but the date: this is transcription of an intent the
    author has already formed, not research. The URLs it is told about are
    attached to the topic by code afterwards — the agent is never asked for one,
    which is why it cannot supply one.
    """
    return Agent(
        clients.fast,
        prompts.brief_interpreter_instructions(settings),
        id=BRIEF_INTERPRETER,
        name=BRIEF_INTERPRETER,
        description="Turns an operator's free-form brief into one typed topic.",
        tools=[tools.today_tool],
        default_options=_opts(TopicSuggestion),
        middleware=_meter(BRIEF_INTERPRETER, fast=True),
    )


def build_notes_normalizer(settings: Settings, clients: ClientBundle) -> Agent:
    # Fast tier: this is extraction, not reasoning. It never touches the web —
    # it only reshapes the notes the author already wrote.
    return Agent(
        clients.fast,
        prompts.notes_normalizer_instructions(settings),
        id=NOTES_NORMALIZER,
        name=NOTES_NORMALIZER,
        description="Turns raw author notes into typed, id'd author claims. Invents nothing.",
        tools=[tools.today_tool],
        default_options=_opts(AuthorClaimSet),
        middleware=_meter(NOTES_NORMALIZER, fast=True),
    )


def build_researcher(
    settings: Settings, clients: ClientBundle, *, corpus_only: bool = False
) -> Agent:
    """The Researcher, optionally confined to a corpus the operator chose.

    ``corpus_only`` bypasses ``_searchable`` rather than filtering its result,
    and that is the whole point: ``_searchable`` is what attaches Foundry's
    *server-side* web search, so a tool list that merely drops the local
    ``web_search`` function would still leave the model able to search. What is
    left is fetching (the corpus), the blog's own archive (internal links) and
    the date.
    """
    tool_set = (
        [tools.fetch_page, tools.search_existing_posts, tools.today_tool]
        if corpus_only
        else _searchable(tools.RESEARCHER_TOOLS, clients.reasoning)
    )
    return Agent(
        clients.reasoning,
        prompts.researcher_instructions(settings, corpus_only=corpus_only),
        id=RESEARCHER,
        name=RESEARCHER,
        description="Builds an evidence-backed dossier for one topic.",
        tools=tool_set,
        default_options=_opts(ResearchDossier),
        middleware=_meter(RESEARCHER),
    )


def build_source_checker(
    settings: Settings, clients: ClientBundle, *, operator_sourced: bool = False
) -> Agent:
    """The Source Checker, keeping its tools even when the corpus is fixed.

    ``operator_sourced`` suspends the rules a chosen corpus cannot satisfy, not
    the checking. It still searches — the one thing it can find that the
    Researcher cannot act on, an official page contradicting a supplied one, is
    exactly the warning worth having, and it surfaces as an unresolved issue the
    Writer is shown rather than an endless loop.
    """
    return Agent(
        clients.reasoning,
        prompts.source_checker_instructions(settings, operator_sourced=operator_sourced),
        id=SOURCE_CHECKER,
        name=SOURCE_CHECKER,
        description="Adversarially verifies every citation and critical claim in the dossier.",
        tools=_searchable(tools.SOURCE_CHECKER_TOOLS, clients.reasoning),
        default_options=_opts(SourceVerdict),
        middleware=_meter(SOURCE_CHECKER),
    )


def build_outliner(settings: Settings, clients: ClientBundle) -> Agent:
    """Decides the one argument the post makes, and what it leaves out.

    Reasoning tier and **no tools**, deliberately. Every fact it may draw on is
    already in the dossier and has already been through the Source Checker, so a
    search tool here would only invite it to widen the scope at exactly the stage
    whose entire purpose is to narrow it. It refers to research by claim id and
    never quotes it, so it cannot fabricate — the newsletter editor's contract.
    """
    return Agent(
        clients.reasoning,
        prompts.outliner_instructions(settings),
        id=OUTLINER,
        name=OUTLINER,
        description="Turns a dossier into one argument: thesis, sections, and what is out of scope.",
        default_options=_opts(PostOutline, temperature=0.3),
        middleware=_meter(OUTLINER),
    )


def build_writer(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.reasoning,
        prompts.writer_instructions(settings),
        id=WRITER,
        name=WRITER,
        description="Writes and revises the post draft from the dossier and validator feedback.",
        tools=tools.WRITER_TOOLS,
        default_options=_opts(Draft, temperature=0.7),
        middleware=_meter(WRITER),
    )


def build_content_validator(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.reasoning,
        prompts.content_validator_instructions(settings),
        id=CONTENT_VALIDATOR,
        name=CONTENT_VALIDATOR,
        description="Editorial gate: substance, accuracy, voice, traceability to the dossier.",
        default_options=_opts(ValidationReportDraft, temperature=0.2),
        middleware=_meter(CONTENT_VALIDATOR),
    )


def build_design_validator(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.reasoning,
        prompts.design_validator_instructions(settings),
        id=DESIGN_VALIDATOR,
        name=DESIGN_VALIDATOR,
        description="Formatting, structure, readability and SEO gate.",
        default_options=_opts(ValidationReportDraft, temperature=0.2),
        middleware=_meter(DESIGN_VALIDATOR),
    )


def build_translator(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.reasoning,
        prompts.translator_instructions(settings),
        id=TRANSLATOR,
        name=TRANSLATOR,
        description="Localises an approved English draft, preserving structure and code.",
        default_options=_opts(Draft, temperature=0.3),
        middleware=_meter(TRANSLATOR),
    )


def build_newsletter_editor(
    settings: Settings, clients: ClientBundle, newsletter: dict[str, Any] | None = None
) -> Agent:
    """Chooses what goes in an issue and writes it.

    Deliberately given no web tools. `fetch_page` would invite it to wander and
    cite something outside the candidate list, which is the one thing the
    publisher gate exists to prevent — and it cannot produce a URL anyway, since
    it refers to articles by id. `today` is enough to say "this week".
    """
    return Agent(
        clients.reasoning,
        prompts.newsletter_editor_instructions(settings, newsletter or {}),
        id=NEWSLETTER_EDITOR,
        name=NEWSLETTER_EDITOR,
        description="Curates harvested articles into one newsletter issue.",
        tools=[tools.today_tool],
        default_options=_opts(NewsletterIssueDraft, 0.4),
        middleware=_meter(NEWSLETTER_EDITOR),
    )


def build_feed_discovery_scout(
    settings: Settings, clients: ClientBundle, brief: str = ""
) -> Agent:
    """Finds candidate sources. Its output is a list of guesses, and is treated as one.

    On the fast tier: this is breadth, not judgement — every URL it returns is
    fetched and parsed before the operator sees it, so the expensive model would
    be paying for confidence the pipeline does not rely on.

    ``brief`` goes into the *instructions* rather than arriving as a user turn.
    A sweep is one long tool-calling loop, and an aim stated in the system
    prompt still governs at search number nine; one stated in a user message is
    competing with everything the searches returned since.
    """
    return Agent(
        clients.fast,
        prompts.feed_scout_discovery_instructions(settings, brief),
        id=FEED_DISCOVERY_SCOUT,
        name=FEED_DISCOVERY_SCOUT,
        description="Sweeps for new feeds worth following.",
        tools=_searchable([tools.web_search, tools.fetch_page, tools.today_tool], clients.fast),
        default_options=_opts(FeedSuggestionSet),
        middleware=_meter(FEED_DISCOVERY_SCOUT, fast=True),
    )


# ---------------------------------------------------------------------------
# Supervised delta learning
# ---------------------------------------------------------------------------


def build_delta_analyst(settings: Settings, clients: ClientBundle) -> Agent:
    """Classifies the author's edits to a finished draft. Decides nothing.

    On the fast tier deliberately. This is labelling against a closed vocabulary
    with the differences already computed in code, not judgement — and it runs
    once per published post forever, so the expensive model would be paying for
    confidence the pipeline does not rely on. What it produces is counted across
    many posts before it can influence anything, and a human approves the result.

    No tools at all. Everything it needs is in the message, and a web search here
    would let a post's own content send it looking things up — the pair it is
    reading is untrusted content by construction.
    """
    return Agent(
        clients.fast,
        prompts.delta_analyst_instructions(settings),
        id=DELTA_ANALYST,
        name=DELTA_ANALYST,
        description="Classifies how a published post differs from the draft the crew wrote.",
        tools=[],
        default_options=_opts(DeltaAnalysis),
        middleware=_meter(DELTA_ANALYST, fast=True),
    )


def build_learning_diagnostician(
    settings: Settings, clients: ClientBundle, *, shape: str, context_block: str = ""
) -> Agent:
    """Proposes one typed configuration change from a recurring pattern.

    The reasoning tier, because this is the one judgement in the loop: which
    single change would stop a habit recurring, stated precisely enough to survive
    being run against every draft and every published post.

    ``shape`` is chosen by code from the cluster's target, not by the model — the
    same reason the newsletter editor is handed article ids. Each shape binds the
    same response model with different instructions, so the fields the renderer
    will read are the fields the agent was told to fill.
    """
    return Agent(
        clients.reasoning,
        prompts.learning_diagnostician_instructions(
            settings, shape=shape, context_block=context_block
        ),
        id=LEARNING_DIAGNOSTICIAN,
        name=LEARNING_DIAGNOSTICIAN,
        description="Turns a recurring edit into one reviewable configuration change.",
        tools=[],
        default_options=_opts(LearningProposal),
        middleware=_meter(LEARNING_DIAGNOSTICIAN),
    )
