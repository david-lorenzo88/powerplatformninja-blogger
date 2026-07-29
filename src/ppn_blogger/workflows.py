"""The two Agent Framework workflow graphs.

`build_topic_discovery_workflow`
    scout_dispatcher ─┬─▶ news_scout  ─┐
                      ├─▶ feed_scout  ─┼─▶ scout_aggregator ─▶ topic_editor ─▶ topic_publisher
                      └─▶ docs_scout  ─┘

`build_post_workflow`
    brief_builder ─▶ researcher ─▶ dossier_gate ─▶ source_checker ─▶ source_gate
                          ▲                                              │
                          └───────────── (source loop, max N) ───────────┤
                                                                         ▼
                                                    writer ─▶ draft_gate ─┬─▶ content_validator ─┐
                                                      ▲                   └─▶ design_validator  ─┤
                                                      │                                          ▼
                                                      └──── (revision loop, max N) ──── review_gate ─▶ finalizer
                                                                                                          │
                                                                       (only when approved) translator ◀──┘
                                                                                  │
                                                                          translation_gate ─▶ output

`build_post_workflow(resume_from_dossier=True)` swaps the entry point for
`dossier_entry`, which loads research already saved on disk and enters at the
source checker — so a failure after the research stage never pays for it twice.
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
    DossierEntry,
    DossierGate,
    DraftGate,
    Finalizer,
    NotesGate,
    ResumePayload,
    ReviewGate,
    RunState,
    ScoutAggregator,
    ScoutDispatcher,
    SourceGate,
    TopicPublisher,
    TranslationGate,
)
from .models import PostPackage, ResearchDossier, TopicSuggestion, TopicSuggestionSet
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
) -> PostWorkflow:
    settings = settings or get_settings()
    clients = clients or default_clients()
    state = RunState(notes_text=notes_text)

    brief = BriefBuilder(state, settings)
    entry = (
        DossierEntry(state, settings, skip_source_check=skip_source_check)
        if resume_from_dossier
        else brief
    )
    normalizer = AgentExecutor(A.build_notes_normalizer(settings, clients), id=A.NOTES_NORMALIZER)
    notes_gate = NotesGate(state)
    researcher = AgentExecutor(A.build_researcher(settings, clients), id=A.RESEARCHER)
    dossier_gate = DossierGate(state)
    source_checker = AgentExecutor(A.build_source_checker(settings, clients), id=A.SOURCE_CHECKER)
    source_gate = SourceGate(state, settings)
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
    budget = 40 + 10 * (settings.run.max_source_rounds + settings.run.max_revision_rounds)

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
            builder.add_edge(entry, writer)
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
        .add_edge(source_gate, writer)
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


async def write_post(
    topic: TopicSuggestion,
    *,
    settings: Settings | None = None,
    clients: ClientBundle | None = None,
    push_to_wordpress: bool | None = None,
    make_cover: bool | None = None,
    translate: bool | None = None,
    notes_text: str = "",
    on_event: Any = None,
) -> PostPackage:
    built = build_post_workflow(
        settings,
        clients,
        push_to_wordpress=push_to_wordpress,
        make_cover=make_cover,
        translate=translate,
        notes_text=notes_text,
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
