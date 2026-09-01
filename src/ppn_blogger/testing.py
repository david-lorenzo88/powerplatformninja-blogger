"""Offline stub client.

Lets the whole pipeline run — graph wiring, loops, gates, file output, Gutenberg
conversion — with no Azure endpoint and no API keys. Used by `ppn ... --dry-run`
and by the test suite.

The stub reads the ``response_format`` the agent asked for and returns a
well-formed instance of that model, so it exercises the same parsing path as a
real run. It deliberately fails the first validation round and the first source
check so both feedback loops are covered.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date
from typing import Any

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    UsageDetails,
)
from pydantic import BaseModel

from .models import (
    AuthorClaim,
    AuthorClaimSet,
    Citation,
    Claim,
    Draft,
    FeedSuggestionSet,
    NewsletterIssueDraft,
    OutlineSection,
    PostOutline,
    ResearchDossier,
    RuleFinding,
    ScoutReport,
    SignalItem,
    SourceFinding,
    SourceVerdict,
    TopicSuggestion,
    TopicSuggestionSet,
    ValidationReport,
    ValidationReportDraft,
)

TODAY = date.today().isoformat()

# The sample is the spec for the house shape and the v2 typography rules, so it
# is written to pass every auto detector: no dash characters, no in-body images,
# no first person (analysis register), contractions present, and every measured
# claim traceable to the stub dossier.
#
# Two detectors it deliberately does NOT pass, because the alternative is worse.
# The sample is ~700 words against a 1440 word floor, so C04 (word count) fires,
# and its sections are far under min_section_words, so F04 fires once. Inflating
# it to 2000 words would make this module unreadable and every future diff noisy,
# and special-casing the stub inside the production detectors is not an option.
# Both are minor findings that leave the stub run approving, and a test asserts
# the computed findings are exactly those two, which turns the compromise into a
# regression test of the new detectors rather than a silent oddity.
#
# It carries exactly six content sections: the count is checked against
# min_sections/max_sections by the suite, so a section added here without a
# config change fails the build.
_SAMPLE_MARKDOWN = """# Elastic tables in Dataverse: what you give up to scale

Standard Dataverse tables start to hurt past a few million rows, and the usual
answer (partitioning) doesn't help. The throttling limits apply per environment,
not per table, so more partitions buy nothing.

Elastic tables solve that problem, but they take several relational features with
them, and you're probably using some of them. Here is the full list, plus a
migration checklist you can follow.

## Contents
- [What elastic tables actually are](#what-elastic-tables-actually-are)
- [Which features stop working](#which-features-stop-working)
- [How to model the partition key](#how-to-model-the-partition-key)
- [How to move the data without downtime](#how-to-move-the-data-without-downtime)
- [What to watch carefully](#what-to-watch-carefully)
- [My take](#my-take)

## What elastic tables actually are

Dataverse tables backed by distributed storage that scales writes horizontally, in
exchange for giving up part of the relational model. You trade joins for throughput.

Request limits are allocated per user, per rolling day, not per table, which is why
partitioning a standard table solves nothing. Splitting one big table into ten
changes nothing, because the ceiling was never the table.

## Which features stop working

| Limit | What happens | Workaround |
|---|---|---|
| No rollup columns | The designer fails silently | Aggregate in a cloud flow |
| No calculated columns | The column is unavailable | Compute on write |

> **Important:** if your model depends on rollup columns, this blocks you before
> the migration even starts.

## How to model the partition key

Pick a value that spreads writes evenly and that you already filter on. A date
bucket works well for time-series data.

```powerfx
Set(varPartition, Text(Today(), "yyyy-mm"))
```

## How to move the data without downtime

Export the schema and the data, then import into the elastic table in a second
environment before you cut over.

```bash
pac data export --schemaFile schema.xml --dataFile data.zip
```

The plugin pipeline doesn't change, but review any synchronous step that assumes
server-side calculated aggregates. Those aggregates aren't there anymore.

## What to watch carefully

Parts of the capability shift between preview and GA, so check the status before
you size anything. There's no easy way back: returning to a standard table means
remodelling from scratch. The limits documentation changes often, and the number
that bit a project last quarter may not be the number today.

## My take

Use elastic tables when write throughput is the real bottleneck and you can live
without relational aggregation. If you're unsure, don't migrate yet. The feature
earns its place in a narrow band of high-write workloads, and nowhere else.

## Sources

- [Elastic tables overview](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/elastic-tables)
- [Dataverse service limits](https://learn.microsoft.com/en-us/power-platform/admin/api-request-limits-allocations)
"""

_SAMPLE_MARKDOWN_ES = """# Elastic tables en Dataverse: lo que pierdes por escalar

Las tablas estándar de Dataverse empiezan a doler por encima de unos millones de
filas, y la respuesta habitual (particionar) no ayuda. Los límites de throttling se
aplican por entorno y no por tabla, así que más particiones no compran nada.

Las elastic tables resuelven ese problema, pero se llevan por delante varias
funcionalidades relacionales que probablemente estés usando. Aquí tienes la lista
completa y una checklist de migración que puedes seguir.

## Contenido
- [Qué son realmente las elastic tables](#que-son-realmente-las-elastic-tables)
- [Qué funcionalidades dejan de funcionar](#que-funcionalidades-dejan-de-funcionar)
- [Cómo modelar la partition key](#como-modelar-la-partition-key)
- [Cómo mover los datos sin cortes](#como-mover-los-datos-sin-cortes)
- [Lo que conviene observar con cautela](#lo-que-conviene-observar-con-cautela)
- [Mi lectura](#mi-lectura)

## Qué son realmente las elastic tables

Tablas de Dataverse respaldadas por almacenamiento distribuido que escala la
escritura horizontalmente, a cambio de renunciar a parte del modelo relacional.

Los límites de peticiones se asignan por usuario y por día, no por tabla, y por eso
particionar una tabla estándar no resuelve nada. Partir una tabla grande en diez no
cambia nada, porque el techo nunca fue la tabla.

## Qué funcionalidades dejan de funcionar

| Límite | Qué ocurre | Alternativa |
|---|---|---|
| Sin rollup columns | El diseñador falla en silencio | Agregar en un cloud flow |
| Sin calculated columns | La columna no está disponible | Calcular en la escritura |

> **Importante:** si tu modelo depende de rollup columns, esto te bloquea antes
> de empezar la migración.

## Cómo modelar la partition key

Elige un valor que reparta la escritura de forma uniforme y por el que ya filtres.
Un bucket de fecha funciona bien para datos de serie temporal.

```powerfx
Set(varPartition, Text(Today(), "yyyy-mm"))
```

## Cómo mover los datos sin cortes

Exporta el esquema y los datos, y luego impórtalos en la elastic table en un segundo
entorno antes de hacer el cambio.

```bash
pac data export --schemaFile schema.xml --dataFile data.zip
```

El pipeline de plugins no cambia, pero revisa cualquier paso síncrono que asuma
agregados calculados en servidor. Esos agregados ya no están.

## Lo que conviene observar con cautela

Parte de la funcionalidad se mueve entre preview y GA, así que revisa el estado antes
de dimensionar. No hay vuelta atrás sencilla: volver a una tabla estándar implica
remodelar desde cero. La documentación de límites cambia a menudo.

## Mi lectura

Usa elastic tables cuando el cuello de botella real sea el throughput de escritura y
puedas vivir sin agregación relacional. Si dudas, no migres todavía.

## Fuentes

- [Elastic tables overview](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/elastic-tables)
- [Dataverse service limits](https://learn.microsoft.com/en-us/power-platform/admin/api-request-limits-allocations)
"""


def _scout_report(name: str) -> ScoutReport:
    # Three domains on purpose, spanning the three cases the source review has to
    # handle: already official, already community_trusted, and an unknown site
    # nobody has classified. A stub that only ever returned learn.microsoft.com
    # would let an exploration dry run pass with an empty approval list.
    return ScoutReport(
        scout=name,
        items=[
            SignalItem(
                title="Elastic tables reach general availability",
                url="https://learn.microsoft.com/en-us/power-apps/developer/data-platform/elastic-tables",
                published=TODAY,
                source_name="Microsoft Learn",
                why_it_matters="High-volume Dataverse scenarios now have a supported path.",
                watch_area="dataverse",
            ),
            SignalItem(
                title="What elastic tables cost you in practice",
                url="https://www.matthewdevaney.com/elastic-tables-in-practice/",
                published=TODAY,
                source_name="Matthew Devaney",
                why_it_matters="Field numbers for the throughput the docs only describe abstractly.",
                watch_area="dataverse",
            ),
            SignalItem(
                title="Dataverse throughput benchmarks nobody publishes",
                url="https://dataverse-notes.example/benchmarks",
                published=TODAY,
                source_name="Dataverse Notes",
                why_it_matters="An independent benchmark of the documented request limits.",
                watch_area="dataverse",
            ),
        ],
        notes=f"stub output from {name}",
    )


def _topic_set() -> TopicSuggestionSet:
    return TopicSuggestionSet(
        generated_on=TODAY,
        suggestions=[
            TopicSuggestion(
                title="Elastic tables in Dataverse: what you give up to scale",
                slug="dataverse-elastic-tables-tradeoffs",
                watch_area="dataverse",
                post_format="deep-dive",
                primary_keyword="Dataverse elastic tables",
                angle="The feature list nobody reads: what stops working.",
                problem_statement="Teams adopt elastic tables for throughput and discover missing features late.",
                why_now="The capability recently changed status.",
                key_questions=[
                    "Which column types are unsupported?",
                    "What are the documented request limits?",
                ],
                seed_sources=[
                    "https://learn.microsoft.com/en-us/power-apps/developer/data-platform/elastic-tables"
                ],
                novelty="A migration checklist derived from the documented limits.",
                audience_fit=5,
                timeliness=4,
                effort=3,
                score=88.0,
            )
        ],
        discarded=["Generic 'AI in Power Platform' piece — no concrete reader problem."],
    )


def _brief_topic() -> TopicSuggestion:
    """Canned brief interpretation, deliberately wrong in the two ways it can be.

    It answers with a `seed_sources` URL nobody supplied and a `post_format` that
    is not in the profile, so every dry run walks the clamp in `topic_from_brief`
    rather than only the happy path. Same reasoning as the newsletter stub
    inventing a link id.
    """
    return TopicSuggestion(
        title="Reading the operator's own sources",
        slug="operator-brief",
        watch_area="not-a-watch-area",
        post_format="listicle",
        primary_keyword="operator brief",
        angle="What the supplied pages actually establish.",
        problem_statement="The author has the sources and wants the post written from them.",
        why_now="Requested directly by the author.",
        key_questions=["What do the supplied pages say?"],
        seed_sources=["https://example.invalid/never-supplied"],
        novelty="Author-selected corpus.",
        audience_fit=4,
        timeliness=3,
        effort=3,
        score=75.0,
    )


def _dossier(clean: bool, corpus_url: str = "") -> ResearchDossier:
    citations = [
        Citation(
            id="S1",
            title="Elastic tables overview",
            url=corpus_url
            or "https://learn.microsoft.com/en-us/power-apps/developer/data-platform/elastic-tables",
            publisher="Microsoft",
            published="2026-05-12",
            tier="official",
            excerpt="Elastic tables do not support rollup columns.",
        ),
        Citation(
            id="S2",
            title="Dataverse service limits",
            url="https://learn.microsoft.com/en-us/power-platform/admin/api-request-limits-allocations",
            publisher="Microsoft",
            published="2026-04-02",
            tier="official",
            excerpt="Request limits are allocated per user, per 24 hours.",
        ),
    ]
    if not clean:
        citations.append(
            Citation(
                id="S3",
                title="Undocumented elastic table behaviour",
                url="https://example.invalid/not-a-real-page",
                publisher="Unknown",
                tier="unknown",
                excerpt="This page does not exist.",
            )
        )
    return ResearchDossier(
        topic_title="Elastic tables in Dataverse: what you give up to scale",
        primary_keyword="Dataverse elastic tables",
        post_format="deep-dive",
        summary="Elastic tables scale writes horizontally at the cost of several relational features.",
        claims=[
            Claim(
                id="C1",
                statement="Elastic tables do not support rollup columns.",
                criticality="critical",
                citation_ids=["S1"] if clean else ["S1", "S3"],
                confidence="high",
            ),
            Claim(
                id="C2",
                statement="Request limits are allocated per user per 24 hours.",
                criticality="critical",
                citation_ids=["S2"],
                confidence="high",
            ),
            # Deliberately not selected by the canned outline. A dossier where every
            # claim gets used is the one shape that never exercises the scoping: no
            # out_of_scope to derive, no omitted research to name, and the Writer's
            # narrowed view identical to the full one.
            Claim(
                id="C3",
                statement="Elastic tables are billed against the same capacity meter as standard tables.",
                criticality="supporting",
                citation_ids=["S2"],
                confidence="medium",
            ),
        ],
        citations=citations,
        examples=['pac data export --schemaFile schema.xml --dataFile data.zip'],
        gotchas=["Rollup columns fail silently in the designer."],
        limits=["No calculated columns."],
        licensing_notes=["Capacity consumption is measured the same as standard tables."],
        open_questions=[],
        suggested_outline=["TL;DR", "Why the obvious approach fails", "Migration steps", "Gotchas", "Verdict"],
        internal_link_candidates=["https://powerplatformninja.com/dataverse-performance"],
    )


def _outline() -> PostOutline:
    """Canned outliner output, deliberately broken in three ways.

    The stub's job is to walk the gate, not to prove that a well-behaved model
    would have been fine — the same doctrine as the fabricated article id in
    ``_newsletter_issue``. Every dry run therefore exercises all three repairs in
    ``repair_outline``:

    * a section citing ``C99``, which the dossier does not have (the id is dropped);
    * a non-closing section with no claims at all (the section is dropped);
    * an empty ``out_of_scope`` (derived from the unselected claims).

    Because the gate repairs rather than loops, this is safe unconditionally and
    needs no ``exercise_loops`` branch. The surviving titles match
    ``_SAMPLE_MARKDOWN`` exactly, so the draft the stub returns next still agrees
    with the plan the gate approved.
    """
    return PostOutline(
        thesis=(
            "Elastic tables are worth it only when write throughput is the real "
            "bottleneck, because they take relational aggregation with them."
        ),
        reader_promise="Decide whether to migrate, and know what breaks before you do.",
        out_of_scope=[],  # repaired by the gate
        working_title="Elastic tables in Dataverse: what you give up to scale",
        sections=[
            OutlineSection(
                title="What elastic tables actually are",
                makes_this_point="Elastic tables trade the relational model for write throughput.",
                claim_ids=["C2"],
                target_words=300,
            ),
            OutlineSection(
                title="Which features stop working",
                makes_this_point="Rollup and calculated columns are gone, which blocks most models.",
                claim_ids=["C1", "C99"],  # C99 does not exist; the gate drops it
                evidence_kind="table",
                target_words=320,
            ),
            OutlineSection(
                title="How to model the partition key",
                makes_this_point="The partition key must spread writes and match how you filter.",
                claim_ids=["C2"],
                evidence_kind="code",
                target_words=300,
            ),
            OutlineSection(
                title="How to move the data without downtime",
                makes_this_point="Stage the migration in a second environment before cutting over.",
                claim_ids=["C1"],
                evidence_kind="code",
                target_words=300,
            ),
            OutlineSection(
                title="Whatever the dossier had left over",
                makes_this_point="This section was planned with nothing behind it.",
                claim_ids=[],  # no claims, not the closing section: the gate drops it
                target_words=200,
            ),
            OutlineSection(
                title="What to watch carefully",
                makes_this_point="Preview status moves and there is no easy way back.",
                claim_ids=["C1"],
                target_words=280,
            ),
            OutlineSection(
                title="My take",
                makes_this_point="Migrate only for high-write workloads; otherwise wait.",
                claim_ids=[],  # the closing section is allowed to carry none
                target_words=260,
            ),
        ],
    )


def _author_claims() -> AuthorClaimSet:
    """Canned normalizer output for a populated notes file."""
    return AuthorClaimSet(
        claims=[
            AuthorClaim(
                id="A1",
                type="measurement",
                text="The unfiltered FetchXML returned about 40,000 rows in 11 seconds.",
                scope="my tenant, managed environment, 14 July",
            ),
            AuthorClaim(
                id="A2",
                type="failure",
                text="Turning on the elastic option in the designer had no effect until the table was recreated.",
                scope="my tenant",
            ),
            AuthorClaim(
                id="A3",
                type="exact_string",
                text="The error was 'The entity does not support rollup attributes'.",
                verbatim=True,
            ),
            AuthorClaim(
                id="A4",
                type="opinion",
                text="I would only ship this for a genuinely high-write table, not as a default.",
            ),
        ],
        summary="Four claims: one measurement, one failure, one exact string, one opinion.",
    )


def _source_verdict(passed: bool) -> SourceVerdict:
    if passed:
        return SourceVerdict(
            passed=True,
            average_trust=5.0,
            summary="All citations reachable, official and corroborated.",
        )
    return SourceVerdict(
        passed=False,
        average_trust=3.1,
        unreachable_urls=["https://example.invalid/not-a-real-page"],
        fabricated_urls=["https://example.invalid/not-a-real-page"],
        uncorroborated_critical_claims=["C1"],
        findings=[
            SourceFinding(
                citation_id="S3",
                claim_id="C1",
                url="https://example.invalid/not-a-real-page",
                issue="URL does not resolve.",
                severity="blocker",
                remediation="Remove S3 and support C1 with an official Microsoft Learn page.",
            )
        ],
        instructions_for_researcher="Drop citation S3 and re-source claim C1 from learn.microsoft.com.",
        summary="One fabricated citation found.",
    )


def _draft(revision: int) -> Draft:
    return Draft(
        title="Elastic tables in Dataverse: what you give up to scale",
        slug="dataverse-elastic-tables-tradeoffs",
        meta_description=(
            "Elastic tables scale Dataverse writes, but rollup and calculated columns stop "
            "working. The full trade-off list and a migration checklist for your tenant."
        ),
        primary_keyword="Dataverse elastic tables",
        category="Dataverse",
        tags=["Dataverse", "Performance", "Elastic tables", "Migration"],
        post_format="deep-dive",
        excerpt="What elastic tables really cost you, feature by feature.",
        markdown=_SAMPLE_MARKDOWN,
        cover_concept=(
            "Glowing wireframe data tables splitting apart into thousands of light shards that "
            "stream into a dark distributed lattice, hot cyan and magenta rim light, volumetric haze."
        ),
        internal_links=["https://powerplatformninja.com/dataverse-performance"],
        word_count=0,
        revision=revision,
        changelog="" if revision == 1 else "Addressed H01 and S03 findings.",
    )


def _translated_draft() -> Draft:
    base = _draft(revision=1)
    return base.model_copy(
        update={
            "title": "Elastic tables en Dataverse: lo que pierdes por escalar",
            "slug": "dataverse-elastic-tables-lo-que-pierdes",
            "meta_description": (
                "Las elastic tables escalan la escritura en Dataverse, pero rollups y calculated "
                "columns dejan de funcionar. Lista completa y checklist de migracion."
            ),
            "excerpt": "Lo que las elastic tables te cuestan de verdad, funcionalidad a funcionalidad.",
            "tags": ["Dataverse", "Rendimiento", "Elastic tables", "Migración"],
            "markdown": _SAMPLE_MARKDOWN_ES,
            "changelog": "",
        }
    )


def _validation(validator: str, strict: bool) -> ValidationReport:
    if not strict:
        return ValidationReport(
            validator=validator,
            score=91,
            passed=True,
            strengths=["Concrete limits table", "Every claim cited"],
            summary="Meets the bar.",
        )
    return ValidationReport(
        validator=validator,
        score=68,
        passed=False,
        findings=[
            RuleFinding(
                rule_id="H01" if validator == "content" else "S03",
                severity="blocker",
                location="## Which features stop working",
                problem="A statement is not traceable to a dossier claim."
                if validator == "content"
                else "The Contents index does not match the H2 order.",
                fix="Trace the sentence to a dossier claim or drop it."
                if validator == "content"
                else "Reorder the Contents entries to match the sections.",
            )
        ],
        strengths=["Clear problem statement"],
        summary="One blocker to fix.",
    )


def _newsletter_issue(prompt: str) -> NewsletterIssueDraft:
    """A canned issue that deliberately misbehaves in three ways.

    The stub's job is to walk the failure branches, not the happy path — the same
    reason `exercise_loops` fails the first source check. So this returns:

    * a real candidate id, taken from the brief, which must survive;
    * an id that was never offered, which `IssuePublisher` must drop;
    * a section that is not in the configured taxonomy, which must also be dropped.

    Every dry run therefore proves the anti-fabrication gate works, rather than
    proving only that a well-behaved model would have been fine.
    """
    from .models import NewsletterIssueDraft, NewsletterItem, NewsletterSection

    offered = [int(x) for x in re.findall(r"^\[(\d+)\]", prompt, flags=re.M)]
    real = offered[0] if offered else 1
    fabricated = (max(offered) + 999) if offered else 999

    return NewsletterIssueDraft(
        subject="Three things worth your time",
        preheader="A quiet week, but two of these matter",
        intro="Short week. Two of these change what you can ship.",
        sections=[
            NewsletterSection(
                id="microsoft",
                title="From Microsoft",
                items=[
                    NewsletterItem(
                        article_id=real,
                        headline="A real item from the candidate list",
                        blurb="This one was offered, so it must survive the gate.",
                        section="microsoft",
                    ),
                    NewsletterItem(
                        article_id=fabricated,
                        headline="An item nobody offered",
                        blurb="Never in the candidate list. The publisher must drop it.",
                        section="microsoft",
                    ),
                ],
            ),
            NewsletterSection(
                id="not_a_real_section",
                title="Invented taxonomy",
                items=[
                    NewsletterItem(
                        article_id=real,
                        headline="In a section that does not exist",
                        blurb="The whole section must be dropped.",
                        section="not_a_real_section",
                    )
                ],
            ),
        ],
        omitted=offered[1:3],
    )


def _feed_suggestions() -> FeedSuggestionSet:
    """Candidate sources, one of which does not exist.

    Same doctrine as `_newsletter_issue`: the stub walks the gate, not the happy
    path. `discovery._verify` fetches every URL before the operator sees it, so
    the canned set contains a plausible address with nothing behind it — a dry
    run then proves the discard happens rather than proving a well-behaved model
    would have been fine.

    That this function did not exist is why the sweep could never be run
    offline, and why an `ImportError` in `_ask` survived the whole test suite.
    """
    from .models import FeedSuggestion, FeedSuggestionSet

    return FeedSuggestionSet(
        suggestions=[
            FeedSuggestion(
                url="https://devblogs.microsoft.com/powerplatform/feed/",
                name="Power Platform Dev Blog",
                topics=["microsoft"],
                reason="The product team's own engineering blog.",
            ),
            FeedSuggestion(
                url="https://this-domain-does-not-exist.invalid/feed",
                name="A source that is not there",
                topics=["ai"],
                reason="Plausible, and nothing behind it. Must never reach the review.",
            ),
        ],
        notes="Two suggested; one of them is not real.",
    )


class StubChatClient(BaseChatClient):
    """Returns schema-valid canned responses. No network, no credentials."""

    def __init__(
        self, *, exercise_loops: bool = True, searches: int = 2, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._exercise_loops = exercise_loops
        self._searches = searches
        self._counts: dict[str, int] = {}
        # The stub deliberately does not implement tool calling — it returns the
        # canned result the tools would have led to. The framework warns once per
        # agent about that; it is expected here and says nothing useful.
        from .util import silence_expected_warning

        silence_expected_warning("agent_framework", "does not support function invoking")

    def _bump(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def _payload(self, model: type[BaseModel], messages: Sequence[Message]) -> BaseModel:
        # Match against the WHOLE conversation: the task marker sits at the top of a
        # prompt that can be tens of thousands of characters long, so a tail slice
        # would miss it.
        full = " ".join(m.text or "" for m in messages)

        if model is ScoutReport:
            return _scout_report(f"scout-{self._bump('scout')}")
        if model is AuthorClaimSet:
            return _author_claims()
        if model is TopicSuggestionSet:
            return _topic_set()
        if model is TopicSuggestion:
            return _brief_topic()
        if model is ResearchDossier:
            n = self._bump("dossier")
            # A corpus run is told so by its own brief, and the canned dossier
            # follows it: one citation inside the corpus, one outside, so the
            # repair in DossierGate both keeps and drops on every dry run.
            corpus = _first_url(full) if "The complete corpus" in full else ""
            return _dossier(clean=not self._exercise_loops or n > 1, corpus_url=corpus)
        if model is SourceVerdict:
            n = self._bump("source")
            return _source_verdict(passed=not self._exercise_loops or n > 1)
        if model is PostOutline:
            # Its own counter. The "draft" and "validation" counters drive the
            # deliberate first-round failures, and the outline stage adds no round,
            # so it must not disturb either of them.
            self._bump("outline")
            return _outline()
        if model is Draft:
            # Writer and Translator share the Draft schema — tell them apart by task.
            if "Translate this approved post" in full:
                return _translated_draft()
            return _draft(revision=self._bump("draft"))
        if model is NewsletterIssueDraft:
            return _newsletter_issue(full)
        if model is FeedSuggestionSet:
            return _feed_suggestions()
        if model in (ValidationReport, ValidationReportDraft):
            n = self._bump("validation")
            validator = "design" if "Structure & Design" in full or n % 2 == 0 else "content"
            strict = self._exercise_loops and n <= 2
            return _validation(validator, strict)
        raise NotImplementedError(f"StubChatClient has no canned response for {model.__name__}")

    def _usage(self) -> UsageDetails:
        """The token counts a real service reports.

        Without these the whole cost path — the meter, the ledger, the per-agent
        rows, the unpriced-model branch — would be dead code offline, and the
        first thing to exercise it would be a live run against Foundry.
        """
        return UsageDetails(
            input_token_count=1200,
            output_token_count=300,
            total_token_count=1500,
            cache_read_input_token_count=200,
            reasoning_output_token_count=80,
        )

    def _search_contents(self) -> list[Content]:
        """Hosted web searches, in the shape the service actually reports them.

        The hosted tool emits a *call* and a *result* content per search, both
        carrying the same ``call_id``. Reproducing that faithfully is what keeps
        the de-duplication in ``usage.count_searches`` honest — a stub that
        emitted one content per search would let a double-count through.
        """
        contents: list[Content] = []
        for i in range(self._searches):
            call_id = f"stub-search-{self._bump('search')}-{i}"
            contents.append(
                Content.from_search_tool_call(call_id=call_id, tool_name="web_search")
            )
            contents.append(
                Content.from_search_tool_result(
                    call_id=call_id, tool_name="web_search", result={}
                )
            )
        return contents

    def _inner_get_response(  # type: ignore[override]
        self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any
    ):
        response_format = options.get("response_format")
        structured = isinstance(response_format, type) and issubclass(response_format, BaseModel)
        # Resolve exactly once — _payload advances the counters that drive the
        # deliberate first-round failures, so calling it twice would skip a loop.
        value = self._payload(response_format, messages) if structured else None
        text = value.model_dump_json() if value is not None else "(stub)"
        # Note the meter does not read this `model="stub"`: `AgentResponse`
        # carries no model, so `UsageMeter` is told the *configured* deployment
        # at construction. A dry run therefore costs itself against the model a
        # real run would have used, which is the more useful answer — you can
        # estimate a post before paying for one.
        searches = self._search_contents()

        if stream:
            # The CLI runs workflows with stream=True so it can show live progress.
            # The stub must support that path too, or --dry-run would exercise
            # different code than a real run — which is exactly how a dry run
            # stops being useful.
            async def _updates() -> AsyncIterator[ChatResponseUpdate]:
                for chunk in _chunks(text):
                    yield ChatResponseUpdate(
                        role="assistant",
                        contents=[Content("text", text=chunk)],
                        model="stub",
                    )
                yield ChatResponseUpdate(
                    role="assistant",
                    contents=[Content.from_usage(usage_details=self._usage()), *searches],
                    model="stub",
                )

            return self._build_response_stream(
                _updates(), response_format=response_format if structured else None
            )

        async def _complete() -> ChatResponse:
            return ChatResponse(
                messages=Message(role="assistant", contents=[text, *searches]),
                value=value,
                response_format=response_format if structured else None,
                model="stub",
                usage_details=self._usage(),
            )

        return _complete()


def _first_url(text: str) -> str:
    from .util import extract_urls

    urls = extract_urls(text)
    return urls[0] if urls else ""


def _chunks(text: str, size: int = 400) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def stub_clients(*, exercise_loops: bool = True):
    from .clients import ClientBundle

    client = StubChatClient(exercise_loops=exercise_loops)
    return ClientBundle(reasoning=client, fast=client)
