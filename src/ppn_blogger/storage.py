"""Writing artefacts to disk: topic shortlists, dossiers, drafts, review reports."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import yaml

from .models import (
    AuthorClaim,
    Draft,
    PostOutline,
    PostPackage,
    ResearchDossier,
    ReviewOutcome,
    TopicSuggestionSet,
)
from .settings import get_settings
from .util import slugify


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M")


def _unique_path(directory: Path, base: str, suffix: str) -> Path:
    """A path in ``directory`` that does not exist yet: ``base``, then ``base-2``…

    Draft filenames used to be ``<date>-<slug>.md`` and a same-day regeneration
    for the same slug overwrote the previous file — which would silently destroy
    an earlier version. Each save now claims its own filename, so the version
    history the catalog records actually has content behind it.
    """
    candidate = directory / f"{base}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{base}-{counter}{suffix}"
        counter += 1
    return candidate


def split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        return yaml.safe_load(parts[1]) or {}, parts[2].lstrip("\n")
    except Exception:  # noqa: BLE001
        return {}, text


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------


def save_topic_suggestions(suggestions: TopicSuggestionSet) -> tuple[Path, Path]:
    settings = get_settings()
    settings.ensure_dirs()
    day = date.today().isoformat()
    json_path = settings.run.topics_dir / f"suggestions-{day}.json"
    md_path = settings.run.topics_dir / f"suggestions-{day}.md"

    json_path.write_text(suggestions.model_dump_json(indent=2), encoding="utf-8")

    lines = [f"# Topic suggestions — {day}", ""]
    for i, s in enumerate(suggestions.suggestions, 1):
        lines += [
            f"## {i}. {s.title}",
            "",
            f"- **Score** {s.score:.0f}/100 · fit {s.audience_fit}/5 · timeliness {s.timeliness}/5 · effort {s.effort}/5",
            f"- **Area** `{s.watch_area}` · **Format** `{s.post_format}` · **Keyword** `{s.primary_keyword}`",
            f"- **Slug** `{s.slug}`",
            "",
            f"**Problem.** {s.problem_statement}",
            "",
            f"**Angle.** {s.angle}",
            "",
            f"**Why now.** {s.why_now}",
            "",
            f"**Novelty.** {s.novelty}",
            "",
        ]
        if s.duplicate_risk:
            lines += [f"> **Overlap:** {s.duplicate_risk}", ""]
        if s.key_questions:
            lines += ["**Research must answer:**", ""]
            lines += [f"- {q}" for q in s.key_questions]
            lines.append("")
        if s.seed_sources:
            lines += ["**Seed sources:**", ""]
            lines += [f"- {u}" for u in s.seed_sources]
            lines.append("")
        lines += [f"Run it: `ppn write --topic {json_path.name} --index {i}`", "", "---", ""]

    if suggestions.discarded:
        lines += ["## Discarded", ""] + [f"- {d}" for d in suggestions.discarded]

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path


def load_topic_suggestions(path: Path) -> TopicSuggestionSet:
    settings = get_settings()
    if not path.is_absolute():
        candidate = settings.run.topics_dir / path
        path = candidate if candidate.exists() else path
    return TopicSuggestionSet.model_validate_json(path.read_text(encoding="utf-8"))


def latest_topic_file() -> Path | None:
    settings = get_settings()
    files = sorted(settings.run.topics_dir.glob("suggestions-*.json"))
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


def save_dossier(dossier: ResearchDossier) -> Path:
    settings = get_settings()
    settings.ensure_dirs()
    name = f"{date.today().isoformat()}-{slugify(dossier.topic_title)}.json"
    path = settings.run.research_dir / name
    path.write_text(dossier.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_dossier(path: Path) -> ResearchDossier:
    """Read a dossier saved by :func:`save_dossier`, for a resume/regenerate run."""
    settings = get_settings()
    if not path.is_absolute():
        candidate = settings.run.research_dir / path
        path = candidate if candidate.exists() else path
    return ResearchDossier.model_validate_json(path.read_text(encoding="utf-8"))


def save_outline(outline: PostOutline, slug_or_title: str) -> Path:
    """Write the approved outline beside the dossier it was built from.

    Written by ``OutlineGate`` *before* the outline goes anywhere, for the same
    reason ``save_dossier`` is: the plan is what the rest of the run is judged
    against, and a failure at the Writer should not cost you the decision about
    what the post argues. Named to match: ``<date>-<slug>.outline.json``.
    """
    settings = get_settings()
    settings.ensure_dirs()
    name = f"{date.today().isoformat()}-{slugify(slug_or_title)}.outline.json"
    path = settings.run.research_dir / name
    path.write_text(outline.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_outline(path: Path) -> PostOutline:
    """Read an outline saved by :func:`save_outline`."""
    settings = get_settings()
    if not path.is_absolute():
        candidate = settings.run.research_dir / path
        path = candidate if candidate.exists() else path
    return PostOutline.model_validate_json(path.read_text(encoding="utf-8"))


def save_notes(claims: list[AuthorClaim], slug_or_title: str) -> Path:
    """Write the normalised author claims beside the dossier.

    An empty list is still written, so a run always records whether notes were
    present. Named to match the dossier: ``<date>-<slug>.notes.json``.
    """
    settings = get_settings()
    settings.ensure_dirs()
    name = f"{date.today().isoformat()}-{slugify(slug_or_title)}.notes.json"
    path = settings.run.research_dir / name
    payload = {"claims": [c.model_dump() for c in claims]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


def draft_front_matter(
    draft: Draft,
    outcome: ReviewOutcome | None,
    language: str = "",
    translation_of: str = "",
) -> dict:
    data = {
        "title": draft.title,
        "slug": draft.slug,
        "description": draft.meta_description,
        # The argument the post was commissioned to make. Carried in the front
        # matter so a regeneration reads it back off disk and argues the same
        # thing, rather than re-deriving a thesis from the dossier.
        "thesis": draft.thesis,
        "primary_keyword": draft.primary_keyword,
        "category": draft.category,
        "tags": draft.tags,
        "post_format": draft.post_format,
        "word_count": draft.word_count,
        "read_minutes": draft.read_minutes,
        "revision": draft.revision,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "status": "draft",
    }
    if language:
        data["language"] = language
    if translation_of:
        # No multilingual plugin yet — this is the breadcrumb that lets you pair
        # the two posts later (Polylang, WPML, or a hand-written hreflang).
        data["translation_of"] = translation_of
    if outcome:
        data["review"] = {
            "approved": outcome.approved,
            "score": round(outcome.overall_score, 1),
            "blockers": outcome.blockers,
        }
    return data


def save_draft(
    draft: Draft,
    outcome: ReviewOutcome | None = None,
    language: str = "",
    translation_of: str = "",
) -> Path:
    settings = get_settings()
    settings.ensure_dirs()
    base = f"{date.today().isoformat()}-{draft.slug or slugify(draft.title)}"
    path = _unique_path(settings.run.output_dir, base, ".md")
    front = yaml.safe_dump(
        draft_front_matter(draft, outcome, language, translation_of),
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    path.write_text(f"---\n{front}\n---\n\n{draft.markdown.strip()}\n", encoding="utf-8")
    return path


def save_review_report(
    draft: Draft,
    outcome: ReviewOutcome,
    markdown_path: Path | None = None,
    outline: PostOutline | None = None,
) -> Path:
    settings = get_settings()
    settings.ensure_dirs()
    if markdown_path is not None:
        # Pair with the draft's actual filename, which may carry a -N suffix, so
        # list_drafts finds the report next to its draft (it derives the review
        # name from the draft stem).
        md = Path(markdown_path)
        path = md.with_name(f"{md.stem}.review.md")
    else:
        name = f"{date.today().isoformat()}-{draft.slug or slugify(draft.title)}.review.md"
        path = settings.run.output_dir / name

    lines = [
        f"# Review report — {draft.title}",
        "",
        f"- **Verdict:** {'APPROVED' if outcome.approved else 'NOT APPROVED'}",
        f"- **Overall score:** {outcome.overall_score:.1f}",
        f"- **Revisions:** {outcome.revision}",
        f"- **Open blockers:** {outcome.blockers}",
        "",
    ]

    # First thing after the verdict, deliberately. "Did this post stay on the
    # argument it was commissioned to make" is the question the human is really
    # asking, and it should be answerable without opening the draft.
    if outline is not None:
        lines += [
            "## Thesis and scope",
            "",
            f"- **Thesis:** {outline.thesis}",
            f"- **Reader promise:** {outline.reader_promise}",
            "",
        ]
        if outline.out_of_scope:
            lines += ["**Deliberately out of scope**", ""]
            lines += [f"- {item}" for item in outline.out_of_scope]
            lines += [""]
        lines += ["**Planned sections**", ""]
        for section in outline.content_sections:
            claims = ", ".join(section.claim_ids) or "no claims"
            lines += [f"- {section.title} ({section.target_words}w · {claims})"]
        lines += [""]
        if outline.warnings:
            lines += ["**Repairs the outline gate had to make**", ""]
            lines += [f"- {w}" for w in outline.warnings]
            lines += [""]

    if outcome.source_verdict:
        sv = outcome.source_verdict
        lines += [
            "## Source check",
            "",
            f"- Passed: **{sv.passed}** · average trust **{sv.average_trust:.2f}/5**",
            f"- {sv.summary}",
            "",
        ]
        for bucket, label in (
            ("unreachable_urls", "Unreachable URLs"),
            ("fabricated_urls", "Fabricated URLs"),
            ("blocked_domains_cited", "Blocked domains cited"),
            ("uncorroborated_critical_claims", "Uncorroborated critical claims"),
            ("contradictions", "Contradicted by official docs"),
        ):
            values = getattr(sv, bucket)
            if values:
                lines += [f"**{label}**", ""] + [f"- {v}" for v in values] + [""]
        if sv.findings:
            lines += ["| Severity | Claim/URL | Issue | Remediation |", "|---|---|---|---|"]
            for f in sv.findings:
                ref = f.claim_id or f.citation_id or f.url
                lines.append(
                    f"| {f.severity} | {ref} | {f.issue.replace('|', '/')} | {f.remediation.replace('|', '/')} |"
                )
            lines.append("")

    for report in outcome.reports:
        lines += [
            f"## {report.validator.title()} validator — {report.score}/100 "
            f"({'pass' if report.passed else 'fail'})",
            "",
            report.summary,
            "",
        ]
        if report.strengths:
            lines += ["**Strengths**", ""] + [f"- {s}" for s in report.strengths] + [""]
        if report.findings:
            lines += ["| Rule | Severity | Where | Problem | Fix |", "|---|---|---|---|---|"]
            for f in report.findings:
                lines.append(
                    f"| {f.rule_id} | {f.severity} | {f.location.replace('|', '/')[:60]} | "
                    f"{f.problem.replace('|', '/')} | {f.fix.replace('|', '/')} |"
                )
            lines.append("")

    measurements = next((r.measurements for r in outcome.reports if r.measurements), None)
    if measurements:
        lines += ["## Measurements", "", "| Metric | Value |", "|---|---|"]
        for key in (
            "avg_sentence_words", "longest_paragraph_sentences", "h2_count",
            "body_word_count", "placeholder_count", "dash_hits", "banned_word_hits",
        ):
            if key in measurements:
                lines.append(f"| {key} | {measurements[key]} |")
        lines.append("")

    if outcome.revision_instructions:
        lines += ["## Outstanding instructions to the writer", "", outcome.revision_instructions, ""]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def save_package(package: PostPackage) -> Path:
    settings = get_settings()
    settings.ensure_dirs()
    name = f"{date.today().isoformat()}-{package.draft.slug}.package.json"
    path = settings.run.output_dir / name
    path.write_text(json.dumps(package.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8")
    return path
