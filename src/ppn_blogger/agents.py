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
    Draft,
    FeedSuggestionSet,
    NewsletterIssueDraft,
    ResearchDossier,
    ScoutReport,
    SourceVerdict,
    TopicSuggestionSet,
    ValidationReportDraft,
)
from .settings import Settings

# Agent ids — also used as executor ids in the workflow graphs.
NEWS_SCOUT = "news_scout"
FEED_SCOUT = "feed_scout"
DOCS_SCOUT = "docs_scout"
TOPIC_EDITOR = "topic_editor"
NOTES_NORMALIZER = "notes_normalizer"
RESEARCHER = "researcher"
WRITER = "writer"
CONTENT_VALIDATOR = "content_validator"
DESIGN_VALIDATOR = "design_validator"
SOURCE_CHECKER = "source_checker"
TRANSLATOR = "translator"
NEWSLETTER_EDITOR = "newsletter_editor"
FEED_DISCOVERY_SCOUT = "feed_discovery_scout"


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
    )


# ---------------------------------------------------------------------------
# Post pipeline crew
# ---------------------------------------------------------------------------


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
    )


def build_researcher(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.reasoning,
        prompts.researcher_instructions(settings),
        id=RESEARCHER,
        name=RESEARCHER,
        description="Builds an evidence-backed dossier for one topic.",
        tools=_searchable(tools.RESEARCHER_TOOLS, clients.reasoning),
        default_options=_opts(ResearchDossier),
    )


def build_source_checker(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.reasoning,
        prompts.source_checker_instructions(settings),
        id=SOURCE_CHECKER,
        name=SOURCE_CHECKER,
        description="Adversarially verifies every citation and critical claim in the dossier.",
        tools=_searchable(tools.SOURCE_CHECKER_TOOLS, clients.reasoning),
        default_options=_opts(SourceVerdict),
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
    )


def build_content_validator(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.reasoning,
        prompts.content_validator_instructions(settings),
        id=CONTENT_VALIDATOR,
        name=CONTENT_VALIDATOR,
        description="Editorial gate: substance, accuracy, voice, traceability to the dossier.",
        default_options=_opts(ValidationReportDraft, temperature=0.2),
    )


def build_design_validator(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.reasoning,
        prompts.design_validator_instructions(settings),
        id=DESIGN_VALIDATOR,
        name=DESIGN_VALIDATOR,
        description="Formatting, structure, readability and SEO gate.",
        default_options=_opts(ValidationReportDraft, temperature=0.2),
    )


def build_translator(settings: Settings, clients: ClientBundle) -> Agent:
    return Agent(
        clients.reasoning,
        prompts.translator_instructions(settings),
        id=TRANSLATOR,
        name=TRANSLATOR,
        description="Localises an approved English draft, preserving structure and code.",
        default_options=_opts(Draft, temperature=0.3),
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
    )


def build_feed_discovery_scout(settings: Settings, clients: ClientBundle) -> Agent:
    """Finds candidate sources. Its output is a list of guesses, and is treated as one.

    On the fast tier: this is breadth, not judgement — every URL it returns is
    fetched and parsed before the operator sees it, so the expensive model would
    be paying for confidence the pipeline does not rely on.
    """
    return Agent(
        clients.fast,
        prompts.feed_scout_discovery_instructions(settings),
        id=FEED_DISCOVERY_SCOUT,
        name=FEED_DISCOVERY_SCOUT,
        description="Sweeps for new feeds worth following.",
        tools=_searchable([tools.web_search, tools.fetch_page, tools.today_tool], clients.fast),
        default_options=_opts(FeedSuggestionSet),
    )
