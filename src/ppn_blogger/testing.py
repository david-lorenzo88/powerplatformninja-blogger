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

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date
from typing import Any

from agent_framework import BaseChatClient, ChatResponse, ChatResponseUpdate, Content, Message
from pydantic import BaseModel

from .models import (
    AuthorClaim,
    AuthorClaimSet,
    Citation,
    Claim,
    Draft,
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
_SAMPLE_MARKDOWN = """# Elastic tables in Dataverse: what you give up to scale

Standard Dataverse tables start to hurt past a few million rows, and the usual
answer (partitioning) doesn't help. The throttling limits apply per environment,
not per table, so more partitions buy nothing.

Elastic tables solve that problem, but they take several relational features with
them, and you're probably using some of them. Here is the full list, plus a
migration checklist you can follow.

## Contents
- [What elastic tables actually are](#what-elastic-tables-actually-are)
- [Why partitioning a standard table solves nothing](#why-partitioning-a-standard-table-solves-nothing)
- [Which features stop working](#which-features-stop-working)
- [How to model the partition key](#how-to-model-the-partition-key)
- [How to move the data without downtime](#how-to-move-the-data-without-downtime)
- [What changes for your plugins and flows](#what-changes-for-your-plugins-and-flows)
- [Licensing and capacity impact](#licensing-and-capacity-impact)
- [What to watch carefully](#what-to-watch-carefully)
- [My take](#my-take)

## What elastic tables actually are

Dataverse tables backed by distributed storage that scales writes horizontally, in
exchange for giving up part of the relational model. You trade joins for throughput.

## Why partitioning a standard table solves nothing

Request limits are allocated per user, per rolling day, not per table. Splitting one
big table into ten changes nothing, because the ceiling was never the table.

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

## What changes for your plugins and flows

The plugin pipeline doesn't change, but review any synchronous step that assumes
server-side calculated aggregates. Those aggregates aren't there anymore.

## Licensing and capacity impact

Capacity consumption is measured the same way as for standard tables, so a move
here won't blow up the bill on its own.

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
- [Por qué particionar una tabla estándar no resuelve nada](#por-que-particionar-una-tabla-estandar-no-resuelve-nada)
- [Qué funcionalidades dejan de funcionar](#que-funcionalidades-dejan-de-funcionar)
- [Cómo modelar la partition key](#como-modelar-la-partition-key)
- [Cómo mover los datos sin cortes](#como-mover-los-datos-sin-cortes)
- [Qué cambia para tus plugins y flows](#que-cambia-para-tus-plugins-y-flows)
- [Impacto en licenciamiento y capacidad](#impacto-en-licenciamiento-y-capacidad)
- [Lo que conviene observar con cautela](#lo-que-conviene-observar-con-cautela)
- [Mi lectura](#mi-lectura)

## Qué son realmente las elastic tables

Tablas de Dataverse respaldadas por almacenamiento distribuido que escala la
escritura horizontalmente, a cambio de renunciar a parte del modelo relacional.

## Por qué particionar una tabla estándar no resuelve nada

Los límites de peticiones se asignan por usuario y por día, no por tabla. Partir una
tabla grande en diez no cambia nada, porque el techo nunca fue la tabla.

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

## Qué cambia para tus plugins y flows

El pipeline de plugins no cambia, pero revisa cualquier paso síncrono que asuma
agregados calculados en servidor. Esos agregados ya no están.

## Impacto en licenciamiento y capacidad

El consumo de capacidad se mide igual que en las tablas estándar, así que un cambio
aquí no dispara la factura por sí solo.

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
            )
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


def _dossier(clean: bool) -> ResearchDossier:
    citations = [
        Citation(
            id="S1",
            title="Elastic tables overview",
            url="https://learn.microsoft.com/en-us/power-apps/developer/data-platform/elastic-tables",
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


class StubChatClient(BaseChatClient):
    """Returns schema-valid canned responses. No network, no credentials."""

    def __init__(self, *, exercise_loops: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._exercise_loops = exercise_loops
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
        if model is ResearchDossier:
            n = self._bump("dossier")
            return _dossier(clean=not self._exercise_loops or n > 1)
        if model is SourceVerdict:
            n = self._bump("source")
            return _source_verdict(passed=not self._exercise_loops or n > 1)
        if model is Draft:
            # Writer and Translator share the Draft schema — tell them apart by task.
            if "Translate this approved post" in full:
                return _translated_draft()
            return _draft(revision=self._bump("draft"))
        if model in (ValidationReport, ValidationReportDraft):
            n = self._bump("validation")
            validator = "design" if "Structure & Design" in full or n % 2 == 0 else "content"
            strict = self._exercise_loops and n <= 2
            return _validation(validator, strict)
        raise NotImplementedError(f"StubChatClient has no canned response for {model.__name__}")

    def _inner_get_response(  # type: ignore[override]
        self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any
    ):
        response_format = options.get("response_format")
        structured = isinstance(response_format, type) and issubclass(response_format, BaseModel)
        # Resolve exactly once — _payload advances the counters that drive the
        # deliberate first-round failures, so calling it twice would skip a loop.
        value = self._payload(response_format, messages) if structured else None
        text = value.model_dump_json() if value is not None else "(stub)"

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

            return self._build_response_stream(
                _updates(), response_format=response_format if structured else None
            )

        async def _complete() -> ChatResponse:
            return ChatResponse(
                messages=Message(role="assistant", contents=[text]),
                value=value,
                response_format=response_format if structured else None,
                model="stub",
            )

        return _complete()


def _chunks(text: str, size: int = 400) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def stub_clients(*, exercise_loops: bool = True):
    from .clients import ClientBundle

    client = StubChatClient(exercise_loops=exercise_loops)
    return ClientBundle(reasoning=client, fast=client)
