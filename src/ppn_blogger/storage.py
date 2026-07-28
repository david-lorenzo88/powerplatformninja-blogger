"""Writing artefacts to disk: topic shortlists, dossiers, drafts, review reports."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import yaml

from .models import (
    Draft,
    PostPackage,
    ResearchDossier,
    ReviewOutcome,
    TopicSuggestionSet,
)
from .settings import get_settings
from .util import slugify


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M")


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
    if draft.image_briefs:
        data["image_briefs"] = draft.image_briefs
    return data


def save_draft(
    draft: Draft,
    outcome: ReviewOutcome | None = None,
    language: str = "",
    translation_of: str = "",
) -> Path:
    settings = get_settings()
    settings.ensure_dirs()
    name = f"{date.today().isoformat()}-{draft.slug or slugify(draft.title)}.md"
    path = settings.run.output_dir / name
    front = yaml.safe_dump(
        draft_front_matter(draft, outcome, language, translation_of),
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    path.write_text(f"---\n{front}\n---\n\n{draft.markdown.strip()}\n", encoding="utf-8")
    return path


def save_review_report(draft: Draft, outcome: ReviewOutcome) -> Path:
    settings = get_settings()
    settings.ensure_dirs()
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
