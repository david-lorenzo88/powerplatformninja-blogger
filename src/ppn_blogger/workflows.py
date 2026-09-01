"""The two Agent Framework workflow graphs.

`build_topic_discovery_workflow`
    scout_dispatcher ─┬─▶ news_scout  ─┐
                      ├─▶ feed_scout  ─┼─▶ scout_aggregator ─▶ topic_editor ─▶ topic_publisher
                      └─▶ docs_scout  ─┘

`build_source_exploration_workflow` / `build_shortlist_workflow`
    The same discovery, cut in two at a human decision: the scouts sweep the open
    web and stop at `source_harvester`, which reports every site they read; once
    the operator has approved sites, `scout_replay` feeds only that material to
    the same topic editor. See the block comment above them.

`build_post_workflow`
    brief_builder ─▶ researcher ─▶ dossier_gate ─▶ source_checker ─▶ source_gate
                          ▲                                              │
                          └───────────── (source loop, max N) ───────────┤
                                                                         ▼
                                            outliner ─▶ outline_gate ─▶ writer ─▶ draft_gate ─┬─▶ content_validator ─┐
                                                                         ▲                     └─▶ design_validator ─┤
                                                                         │                                           ▼
                                                      └──── (revision loop, max N) ──── review_gate ─▶ finalizer
                                                                                                          │
                                                                       (only when approved) translator ◀──┘
                                                                                  │
                                                                          translation_gate ─▶ output

The `outliner` decides what the post argues before a word of it exists, and
`outline_gate` checks that plan against the dossier in code. It is the one stage
with no loop back: every failure it can detect has a deterministic repair, so it
fixes and records rather than re-asking. That is why the run still has exactly two
round counters.

`build_post_workflow(resume_from_dossier=True)` swaps the entry point for
`dossier_entry`, which loads research already saved on disk and enters at the
source checker — so a failure after the research stage never pays for it twice.
With `skip_source_check` it enters at the outliner instead: a regeneration still
gets a thesis, or the same research would argue something different each time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agent_framework import AgentExecutor, Workflow, WorkflowBuilder

from . import agents as A
from .clients import ClientBundle, default_clients
from .executors import (
    BriefBuilder,
    ComposedIssue,
    DossierEntry,
    DossierGate,
    DraftGate,
    Finalizer,
    IssueBuilder,
    IssuePublisher,
    IssueRequest,
    NewsletterCandidate,
    NotesGate,
    OutlineGate,
    ResumePayload,
    ReviewGate,
    RunState,
    ScoutAggregator,
    ScoutDispatcher,
    ScoutReplay,
    ShortlistRequest,
    SourceGate,
    SourceHarvester,
    TopicPublisher,
    TranslationGate,
)
from .models import (
    PostPackage,
    ResearchDossier,
    ScoutReport,
    SourceReviewSet,
    TopicSuggestion,
    TopicSuggestionSet,
)
from .settings import Settings, get_settings

logger = logging.getLogger("ppn.workflow")

DEFAULT_TOPIC_INSTRUCTION = (
    "Find what is worth writing about on the Power Platform Ninja blog right now."
)


# ---------------------------------------------------------------------------
# Topic discovery
# ---------------------------------------------------------------------------


@dataclass
class TopicWorkflow:
    workflow: Workflow
    publisher: TopicPublisher


def build_topic_discovery_workflow(
    settings: Settings | None = None, clients: ClientBundle | None = None
) -> TopicWorkflow:
    settings = settings or get_settings()
    clients = clients or default_clients()

    dispatcher = ScoutDispatcher(settings)
    news = AgentExecutor(A.build_news_scout(settings, clients), id=A.NEWS_SCOUT)
    feeds = AgentExecutor(A.build_feed_scout(settings, clients), id=A.FEED_SCOUT)
    docs = AgentExecutor(A.build_docs_scout(settings, clients), id=A.DOCS_SCOUT)
    aggregator = ScoutAggregator(settings)
    editor = AgentExecutor(A.build_topic_editor(settings, clients), id=A.TOPIC_EDITOR)
    publisher = TopicPublisher()

    workflow = (
        WorkflowBuilder(
            start_executor=dispatcher,
            name="ppn-topic-discovery",
            description="Scan news, feeds and docs, then propose ranked blog topics.",
            max_iterations=30,
        )
        .add_fan_out_edges(dispatcher, [news, feeds, docs])
        .add_fan_in_edges([news, feeds, docs], aggregator)
        .add_edge(aggregator, editor)
        .add_edge(editor, publisher)
        .build()
    )
    return TopicWorkflow(workflow=workflow, publisher=publisher)


async def discover_topics(
    instruction: str = DEFAULT_TOPIC_INSTRUCTION,
    *,
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
    on_event: Any = None,
) -> TopicSuggestionSet:
    built = build_topic_discovery_workflow(settings, clients)

    if on_event is None:
        result = await built.workflow.run(instruction)
    else:
        stream = built.workflow.run(instruction, stream=True)
        async for event in stream:
            on_event(event)
        result = await stream.get_final_response()

    outputs = [o for o in result.get_outputs() if isinstance(o, TopicSuggestionSet)]
    if outputs:
        return outputs[-1]
    if built.publisher.result is not None:
        return built.publisher.result
    raise RuntimeError("Topic discovery produced no shortlist. Check the run log above.")


# ---------------------------------------------------------------------------
# Topic discovery, exploration mode
#
# Same scouts, cut in half at the point where a human has to decide:
#
#   build_source_exploration_workflow
#       scout_dispatcher ─┬─▶ news_scout (wide) ─┐
#                         ├─▶ feed_scout         ┼─▶ source_harvester ─▶ output
#                         └─▶ docs_scout         ┘
#
#   ...operator approves sites...
#
#   build_shortlist_workflow
#       scout_replay ─▶ topic_editor ─▶ topic_publisher
#
# Two graphs rather than one paused graph: the approval sits between two runs,
# so nothing has to hold a worker (or a model connection) open for however long
# the operator takes, and a server restart mid-review costs nothing.
# ---------------------------------------------------------------------------


@dataclass
class ExplorationWorkflow:
    workflow: Workflow
    harvester: SourceHarvester


def build_source_exploration_workflow(
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
    *,
    instruction: str = "",
) -> ExplorationWorkflow:
    settings = settings or get_settings()
    clients = clients or default_clients()

    dispatcher = ScoutDispatcher(settings)
    news = AgentExecutor(A.build_news_scout(settings, clients, explore=True), id=A.NEWS_SCOUT)
    feeds = AgentExecutor(A.build_feed_scout(settings, clients), id=A.FEED_SCOUT)
    docs = AgentExecutor(A.build_docs_scout(settings, clients), id=A.DOCS_SCOUT)
    harvester = SourceHarvester(settings, instruction=instruction)

    workflow = (
        WorkflowBuilder(
            start_executor=dispatcher,
            name="ppn-source-exploration",
            description="Sweep the open web and report every site found, for approval.",
            max_iterations=30,
        )
        .add_fan_out_edges(dispatcher, [news, feeds, docs])
        .add_fan_in_edges([news, feeds, docs], harvester)
        .build()
    )
    return ExplorationWorkflow(workflow=workflow, harvester=harvester)


async def explore_sources(
    instruction: str = DEFAULT_TOPIC_INSTRUCTION,
    *,
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
    on_event: Any = None,
) -> SourceReviewSet:
    """Run the scouts wide and come back with the sites they read."""
    built = build_source_exploration_workflow(settings, clients, instruction=instruction)

    if on_event is None:
        result = await built.workflow.run(instruction)
    else:
        stream = built.workflow.run(instruction, stream=True)
        async for event in stream:
            on_event(event)
        result = await stream.get_final_response()

    outputs = [o for o in result.get_outputs() if isinstance(o, SourceReviewSet)]
    if outputs:
        return outputs[-1]
    if built.harvester.result is not None:
        return built.harvester.result
    raise RuntimeError("The sweep found no sources at all. Check the run log above.")


def build_shortlist_workflow(
    settings: Settings | None = None, clients: ClientBundle | None = None
) -> TopicWorkflow:
    settings = settings or get_settings()
    clients = clients or default_clients()

    replay = ScoutReplay(settings)
    editor = AgentExecutor(A.build_topic_editor(settings, clients), id=A.TOPIC_EDITOR)
    publisher = TopicPublisher()

    workflow = (
        WorkflowBuilder(
            start_executor=replay,
            name="ppn-topic-shortlist",
            description="Turn approved sources into a ranked blog topic shortlist.",
            max_iterations=20,
        )
        .add_edge(replay, editor)
        .add_edge(editor, publisher)
        .build()
    )
    return TopicWorkflow(workflow=workflow, publisher=publisher)


async def shortlist_from_sources(
    reports: list[ScoutReport],
    approved: list[str],
    *,
    instruction: str = "",
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
    on_event: Any = None,
) -> TopicSuggestionSet:
    """Build the shortlist from a sweep whose sources have been approved."""
    built = build_shortlist_workflow(settings, clients)
    payload = ShortlistRequest(reports=reports, approved=approved, instruction=instruction)

    if on_event is None:
        result = await built.workflow.run(payload)
    else:
        stream = built.workflow.run(payload, stream=True)
        async for event in stream:
            on_event(event)
        result = await stream.get_final_response()

    outputs = [o for o in result.get_outputs() if isinstance(o, TopicSuggestionSet)]
    if outputs:
        return outputs[-1]
    if built.publisher.result is not None:
        return built.publisher.result
    raise RuntimeError("The approved sources produced no shortlist. Check the run log above.")


# ---------------------------------------------------------------------------
# Post pipeline
# ---------------------------------------------------------------------------


@dataclass
class PostWorkflow:
    workflow: Workflow
    state: RunState
    finalizer: Finalizer


def build_post_workflow(
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
    *,
    push_to_wordpress: bool | None = None,
    make_cover: bool | None = None,
    translate: bool | None = None,
    resume_from_dossier: bool = False,
    skip_source_check: bool = False,
    notes_text: str = "",
    extra_instructions: str = "",
    dossier_path: str = "",
    source_corpus: list[str] | None = None,
) -> PostWorkflow:
    settings = settings or get_settings()
    clients = clients or default_clients()
    # A corpus is the operator's decision that this post rests on these pages and
    # no others. It reshapes two agents rather than adding a stage: the Researcher
    # loses every way of reaching anything else, and the Source Checker stops
    # measuring against rules a fixed corpus cannot meet.
    corpus = list(source_corpus or [])
    state = RunState(
        notes_text=notes_text,
        extra_instructions=extra_instructions,
        dossier_path=dossier_path,
        source_corpus=corpus,
    )

    brief = BriefBuilder(state, settings)
    entry = (
        DossierEntry(state, settings, skip_source_check=skip_source_check)
        if resume_from_dossier
        else brief
    )
    normalizer = AgentExecutor(A.build_notes_normalizer(settings, clients), id=A.NOTES_NORMALIZER)
    notes_gate = NotesGate(state)
    researcher = AgentExecutor(
        A.build_researcher(settings, clients, corpus_only=bool(corpus)), id=A.RESEARCHER
    )
    dossier_gate = DossierGate(state)
    source_checker = AgentExecutor(
        A.build_source_checker(settings, clients, operator_sourced=bool(corpus)),
        id=A.SOURCE_CHECKER,
    )
    source_gate = SourceGate(state, settings)
    outliner = AgentExecutor(A.build_outliner(settings, clients), id=A.OUTLINER)
    outline_gate = OutlineGate(state, settings)
    writer = AgentExecutor(A.build_writer(settings, clients), id=A.WRITER)
    draft_gate = DraftGate(state, settings)
    content_validator = AgentExecutor(
        A.build_content_validator(settings, clients), id=A.CONTENT_VALIDATOR
    )
    design_validator = AgentExecutor(
        A.build_design_validator(settings, clients), id=A.DESIGN_VALIDATOR
    )
    review_gate = ReviewGate(state, settings)
    finalizer = Finalizer(
        state,
        settings,
        push_to_wordpress=push_to_wordpress,
        make_cover=make_cover,
        translate=translate,
    )
    translator = AgentExecutor(A.build_translator(settings, clients), id=A.TRANSLATOR)
    translation_gate = TranslationGate(state, settings, push_to_wordpress=push_to_wordpress)

    # Enough iterations for max_source_rounds + max_revision_rounds plus slack.
    # The outline stage adds three fixed supersteps and no round of its own, so it
    # raises the constant rather than the per-round multiplier.
    budget = 50 + 10 * (settings.run.max_source_rounds + settings.run.max_revision_rounds)

    builder = WorkflowBuilder(
        start_executor=entry,
        name="ppn-post-pipeline",
        description="Research, source-check, write, validate and publish one blog post draft.",
        max_iterations=budget,
    )

    if resume_from_dossier:
        # Research already exists on disk: enter at the source check (or straight
        # at the writer). The researcher is not part of this graph at all, so the
        # source loop has nowhere to go back to and is not wired.
        builder.add_edge(entry, source_checker)
        if skip_source_check:
            builder.add_edge(entry, outliner)
    else:
        # Two ways out of the brief: through the notes normalizer when there are
        # real notes, or straight to the researcher when there are none.
        builder.add_edge(brief, normalizer)
        builder.add_edge(brief, researcher)
        builder.add_edge(normalizer, notes_gate)
        builder.add_edge(notes_gate, researcher)
        builder.add_edge(researcher, dossier_gate)
        builder.add_edge(dossier_gate, source_checker)
        builder.add_edge(source_gate, researcher)   # source loop

    workflow = (
        builder
        .add_edge(source_checker, source_gate)
        .add_edge(source_gate, outliner)
        .add_edge(outliner, outline_gate)
        .add_edge(outline_gate, writer)
        .add_edge(writer, draft_gate)
        .add_fan_out_edges(draft_gate, [content_validator, design_validator])
        .add_fan_in_edges([content_validator, design_validator], review_gate)
        .add_edge(review_gate, writer)       # revision loop
        .add_edge(review_gate, finalizer)
        .add_edge(finalizer, translator)     # post-approval localisation
        .add_edge(translator, translation_gate)
        .build()
    )
    return PostWorkflow(workflow=workflow, state=state, finalizer=finalizer)


async def topic_from_brief(
    brief: str,
    sources: list[str] | None = None,
    *,
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
) -> tuple[TopicSuggestion, list[str]]:
    """Turn an operator's own brief into a topic and the corpus it names.

    Upstream of the post pipeline, not part of it — the same relationship topic
    discovery has to it, and the reason this is a plain agent call rather than a
    node: the write graph is about turning *a topic* into a draft, and drawing
    two more boxes on its canvas for every ordinary run would be a worse trade
    than the one log line this costs.

    The links are code, never judgement. They are read out of the brief in
    Python, `seed_sources` is overwritten with exactly that list, and the model's
    own answer for that field is discarded — so a run cannot end up resting on a
    URL the operator never wrote. The taxonomy is clamped the same way: an
    invented watch area or post format becomes the configured fallback rather
    than reaching a prompt that lists the real ones.
    """
    from .util import extract_urls, parse_model, slugify, user_message

    settings = settings or get_settings()
    clients = clients or default_clients()

    # A caller that has already worked the corpus out wins: the server fetches
    # every link before it queues the run and replaces each with where it landed,
    # and re-reading the brief here would put the unresolved spellings back
    # alongside the resolved ones — the same page, twice, one of them a stranger
    # to the citation check.
    corpus = list(sources) if sources else extract_urls(brief)

    listing = "\n".join(f"- {url}" for url in corpus) or "- (none)"
    prompt = (
        "The author wants this post. Turn it into one TopicSuggestion.\n\n"
        f"<brief>\n{brief.strip()}\n</brief>\n\n"
        "<supplied_sources>\nThese are the only pages the post may be built from. "
        "They are attached by code — do not repeat them in your answer.\n"
        f"{listing}\n</supplied_sources>"
    )
    agent = A.build_brief_interpreter(settings, clients)
    topic = parse_model(await agent.run([user_message(prompt)]), TopicSuggestion)

    # Config order, not set order: the fallback has to be the same one every run,
    # and the profiles list the general-purpose entry first for exactly this.
    areas = [a["id"] for a in settings.watch_areas]
    formats = [f["id"] for f in settings.blog_profile.get("post_formats", []) if f.get("id")]
    topic = topic.model_copy(
        update={
            "seed_sources": corpus,
            "slug": slugify(topic.slug or topic.title),
            "watch_area": topic.watch_area if topic.watch_area in areas else (areas or [""])[0],
            "post_format": (
                topic.post_format if topic.post_format in formats else (formats or ["analysis"])[0]
            ),
        }
    )
    logger.info(
        "brief interpreted: %r (%s / %s), %d source(s)",
        topic.title,
        topic.watch_area,
        topic.post_format,
        len(corpus),
    )
    return topic, corpus


async def write_post(
    topic: TopicSuggestion,
    *,
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
    push_to_wordpress: bool | None = None,
    make_cover: bool | None = None,
    translate: bool | None = None,
    notes_text: str = "",
    extra_instructions: str = "",
    source_corpus: list[str] | None = None,
    on_event: Any = None,
) -> PostPackage:
    built = build_post_workflow(
        settings,
        clients,
        push_to_wordpress=push_to_wordpress,
        make_cover=make_cover,
        translate=translate,
        notes_text=notes_text,
        extra_instructions=extra_instructions,
        source_corpus=source_corpus,
    )

    if on_event is None:
        result = await built.workflow.run(topic)
    else:
        stream = built.workflow.run(topic, stream=True)
        async for event in stream:
            on_event(event)
        result = await stream.get_final_response()

    outputs = [o for o in result.get_outputs() if isinstance(o, PostPackage)]
    if outputs:
        return outputs[-1]
    if built.finalizer.package is not None:
        return built.finalizer.package
    raise RuntimeError("The pipeline finished without producing a draft. Check the run log above.")


async def write_post_from_dossier(
    topic: TopicSuggestion,
    dossier: ResearchDossier,
    *,
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
    push_to_wordpress: bool | None = None,
    make_cover: bool | None = None,
    translate: bool | None = None,
    skip_source_check: bool = False,
    notes_text: str = "",
    extra_instructions: str = "",
    dossier_path: str = "",
    on_event: Any = None,
) -> PostPackage:
    """Run the pipeline from an existing dossier, skipping the research stage."""
    built = build_post_workflow(
        settings,
        clients,
        push_to_wordpress=push_to_wordpress,
        make_cover=make_cover,
        translate=translate,
        resume_from_dossier=True,
        skip_source_check=skip_source_check,
        notes_text=notes_text,
        extra_instructions=extra_instructions,
        dossier_path=dossier_path,
    )
    payload = ResumePayload(topic=topic, dossier=dossier)

    if on_event is None:
        result = await built.workflow.run(payload)
    else:
        stream = built.workflow.run(payload, stream=True)
        async for event in stream:
            on_event(event)
        result = await stream.get_final_response()

    outputs = [o for o in result.get_outputs() if isinstance(o, PostPackage)]
    if outputs:
        return outputs[-1]
    if built.finalizer.package is not None:
        return built.finalizer.package
    raise RuntimeError("The pipeline finished without producing a draft.")


# ---------------------------------------------------------------------------
# Newsletter pipeline
# ---------------------------------------------------------------------------


@dataclass
class NewsletterWorkflow:
    workflow: Any
    builder: IssueBuilder
    publisher: IssuePublisher


def build_newsletter_workflow(
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
    newsletter: dict[str, Any] | None = None,
) -> NewsletterWorkflow:
    """issue_builder -> newsletter_editor -> issue_publisher.

    One agent between two code gates. The builder decides whether there is
    enough to write about at all (and can end the run without calling a model);
    the publisher resolves the editor's ids back to real articles and drops
    anything it cannot account for.
    """
    settings = settings or get_settings()
    clients = clients or default_clients()

    builder = IssueBuilder()
    editor = AgentExecutor(
        A.build_newsletter_editor(settings, clients, newsletter), id=A.NEWSLETTER_EDITOR
    )
    publisher = IssuePublisher(settings)

    workflow = (
        WorkflowBuilder(
            start_executor=builder,
            name="ppn-newsletter",
            description="Curate harvested articles into one newsletter issue.",
            max_iterations=20,
        )
        .add_edge(builder, editor)
        .add_edge(editor, publisher)
        .build()
    )
    return NewsletterWorkflow(workflow=workflow, builder=builder, publisher=publisher)


async def compose_issue(
    newsletter: dict[str, Any],
    candidates: list[NewsletterCandidate],
    *,
    window_from: str = "",
    window_to: str = "",
    instruction: str = "",
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
    on_event: Any = None,
) -> ComposedIssue:
    """Run the graph for one issue."""
    built = build_newsletter_workflow(settings, clients, newsletter)
    payload = IssueRequest(
        newsletter=newsletter,
        candidates=candidates,
        window_from=window_from,
        window_to=window_to,
        instruction=instruction,
    )
    # The publisher needs the candidate list to resolve ids against, and it is
    # not on the message path — the agent response is. Hand it over directly.
    built.publisher.request = payload

    if on_event is None:
        result = await built.workflow.run(payload)
    else:
        stream = built.workflow.run(payload, stream=True)
        async for event in stream:
            on_event(event)
        result = await stream.get_final_response()

    outputs = [o for o in result.get_outputs() if isinstance(o, ComposedIssue)]
    if outputs:
        return outputs[-1]
    if built.publisher.result is not None:
        return built.publisher.result
    if built.builder.skipped is not None:
        return built.builder.skipped
    raise RuntimeError("The newsletter graph produced no issue. Check the run log above.")
