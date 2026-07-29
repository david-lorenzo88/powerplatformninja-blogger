"""Code-side rule detectors and measurements.

The v2 ruleset carries a ``detector`` regex on 21 rules. Those regexes run here,
in Python, *before* either validator model is called — a machine counts dashes,
banned words and sentence lengths, and the model is left to judge only the rules
marked ``auto: false``. Two reasons this lives in code and not in a prompt:

* A regex is deterministic. "Zero em dashes" is a fact, not an opinion, and a
  model asked to grep its own output misses hits.
* The numbers on a ``ValidationReport`` (``measurements``) must be counted, never
  estimated. A model that is asked "what is the average sentence length" makes
  something up.

``run_detectors`` is invoked from ``executors.py`` on the draft before the fan-out
to the validators. The findings it returns are merged into the reports so a
blocker it raises gates the run exactly like a model blocker would.

The one rule that matters most to get right is the typography pair T01/T02: they
must never fire inside a fenced code block, an inline code span or a URL, where a
hyphen or a dash is correct. ``prose_only`` masks those regions before the
prose-scoped detectors run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .models import AuthorClaim, RuleFinding
from .settings import Settings

# ---------------------------------------------------------------------------
# Masking: turn markdown into "prose only" for the prose-scoped detectors
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"(?s)```.*?```")
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_URL = re.compile(r"https?://[^\s)]+")
# A markdown list bullet at line start. T02's detector matches a spaced hyphen,
# and a bullet ``- item`` reads as exactly that (newline, hyphen, space), so the
# bullet marker is blanked before the prose detectors run. This is the T02
# false-positive the ruleset warns about.
_BULLET = re.compile(r"(?m)^([ \t]*)[-*+](\s)")


def _blank(match: re.Match[str]) -> str:
    """Replace a span with spaces, preserving newlines so line/offset survive."""
    return "".join("\n" if ch == "\n" else " " for ch in match.group(0))


def prose_only(markdown: str) -> str:
    """Mask fenced code, inline code spans, URLs and list bullets.

    T01/T02 (and every other prose-scoped detector) run against this, so a
    hyphen in ``low-code``, a slug, a CLI flag or a URL cannot be mistaken for
    punctuation, and a list bullet is not read as a spaced hyphen. Length and
    newline positions are preserved, so a match offset still points at the real
    text.
    """
    masked = _FENCE.sub(_blank, markdown)
    masked = _INLINE_CODE.sub(_blank, masked)
    masked = _URL.sub(_blank, masked)
    masked = _BULLET.sub(lambda m: f"{m.group(1)} {m.group(2)}", masked)
    return masked


# Detectors that must only see prose. The rest run against the raw markdown
# (headings, code fences, image syntax and links are structural, not prose).
_PROSE_SCOPED = {
    "H03", "T01", "T02", "T04", "T06",
    "V01", "V02", "V05", "V07", "V08", "V14", "V15",
}


@lru_cache(maxsize=8)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def compile_all(settings: Settings) -> dict[str, re.Pattern[str]]:
    """Every rule that carries a detector, compiled. Used by the test suite."""
    out: dict[str, re.Pattern[str]] = {}
    for rule in settings.all_rules():
        if detector := rule.get("detector"):
            out[rule["id"]] = re.compile(detector)
    return out


# ---------------------------------------------------------------------------
# Sections and sentences
# ---------------------------------------------------------------------------

_H2 = re.compile(r"(?m)^##\s+(.+?)\s*$")
_WORD = re.compile(r"\b[\w'\-]+\b")
_SENTENCE_SPLIT = re.compile(r"[.!?]+(?:\s+|$)")


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split into (h2_title, body_text) pairs, in document order."""
    parts: list[tuple[str, str]] = []
    matches = list(_H2.finditer(markdown))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        parts.append((m.group(1).strip(), markdown[start:end]))
    return parts


def _sentences(text: str) -> list[str]:
    chunks = _SENTENCE_SPLIT.split(text.strip())
    return [c.strip() for c in chunks if c.strip()]


def _words(text: str) -> int:
    return len(_WORD.findall(text))


def compute_measurements(markdown: str, settings: Settings) -> dict[str, Any]:
    """Numeric facts about the draft. Counted, never estimated.

    Keys match the output_schema block in validation_rules.yaml.
    """
    prose = prose_only(markdown)
    toc = str(settings.structure.get("toc_heading", "Contents"))
    sources = str(settings.structure.get("sources_heading", "Sources"))

    prose_sections = split_sections(prose)
    section_word_counts: dict[str, int] = {}
    short_per_section: dict[str, int] = {}
    for (title, _raw), (_t2, body) in zip(
        split_sections(markdown), prose_sections, strict=False
    ):
        if title in {toc, sources}:
            continue
        section_word_counts[title] = _words(body)
        short_per_section[title] = sum(1 for s in _sentences(body) if _words(s) < 8)

    all_sentences = _sentences(prose)
    total_words = sum(_words(s) for s in all_sentences)
    avg = round(total_words / len(all_sentences), 1) if all_sentences else 0.0

    longest_para = 0
    for para in re.split(r"\n\s*\n", prose):
        longest_para = max(longest_para, len(_sentences(para)))

    dash = compile_all(settings).get("T01")
    banned = compile_all(settings).get("V01")
    placeholder = compile_all(settings).get("H05")

    return {
        "avg_sentence_words": avg,
        "short_sentences_per_section": short_per_section,
        "longest_paragraph_sentences": longest_para,
        "section_word_counts": section_word_counts,
        "h2_count": len(section_word_counts),
        "placeholder_count": len(placeholder.findall(markdown)) if placeholder else 0,
        "dash_hits": len(dash.findall(prose)) if dash else 0,
        "banned_word_hits": len(banned.findall(prose)) if banned else 0,
    }


# ---------------------------------------------------------------------------
# Detector run
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DetectorRun:
    findings: list[RuleFinding] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    # Detector hits for the auto:false rules, handed to the model as a starting
    # point rather than a verdict.
    hints: str = ""


_NUMERIC = re.compile(r"\d+(?:[.,]\d+)?")


def _snippet(text: str, start: int, end: int, width: int = 60) -> str:
    lead = max(0, start - width // 3)
    return text[lead:end + width // 3].strip().replace("\n", " ")


def _finding(rule: dict[str, Any], location: str, problem: str = "") -> RuleFinding:
    return RuleFinding(
        rule_id=rule["id"],
        severity=rule["severity"],
        location=location[:200],
        problem=problem or rule["rule"].strip().split(". ")[0],
        fix=(rule.get("fix_hint") or rule.get("check_hint") or "See the rule text.").strip(),
    )


def _h03_untraceable(
    hits: list[re.Match[str]], dossier_blob: str, claims_blob: str
) -> list[str]:
    """Measured numbers in the draft that appear in neither source."""
    haystack = f"{dossier_blob}\n{claims_blob}"
    haystack_digits = re.sub(r"[,\s]", "", haystack)
    out: list[str] = []
    for m in hits:
        num = _NUMERIC.search(m.group(0))
        if not num:
            continue
        raw = num.group(0)
        bare = raw.replace(",", "")
        if raw in haystack or bare in haystack_digits:
            continue
        out.append(m.group(0).strip())
    return out


def run_detectors(
    markdown: str,
    *,
    groups: tuple[str, ...],
    settings: Settings,
    dossier_blob: str = "",
    author_claims: list[AuthorClaim] | None = None,
    slug: str = "",
) -> DetectorRun:
    """Run every detector for ``groups`` and return findings + measurements.

    ``auto: true`` rules are decided here; ``auto: false`` rules are left to the
    model, with their detector hits handed over as hints. H03 is the exception:
    its number-tracing is deterministic, so it is computed here even though the
    model still judges the version numbers and error codes a regex cannot catch.
    """
    prose = prose_only(markdown)
    claims_blob = "\n".join(c.text for c in (author_claims or []))
    toc = str(settings.structure.get("toc_heading", "Contents"))
    sources_heading = str(settings.structure.get("sources_heading", "Sources"))
    max_callouts = int(settings.structure.get("max_callouts", 3))

    findings: list[RuleFinding] = []
    hints: list[str] = []

    for rule in settings.all_rules():
        if rule["group"] not in groups or "detector" not in rule:
            continue
        rid = rule["id"]
        pattern = _compiled(rule["detector"])
        target = prose if rid in _PROSE_SCOPED else markdown

        # H03 — number traceability. Deterministic, so computed in code.
        if rid == "H03":
            untraceable = _h03_untraceable(
                list(pattern.finditer(prose)), dossier_blob, claims_blob
            )
            for value in untraceable:
                findings.append(
                    _finding(
                        rule,
                        location=value,
                        problem=f"The number {value!r} is in neither the dossier nor the author notes.",
                    )
                )
            continue

        if not rule.get("auto"):
            hit_list = [m.group(0).strip() for m in pattern.finditer(target)][:5]
            if hit_list:
                hints.append(f"{rid}: candidate hits {hit_list}")
            continue

        # --- auto: true rules are decided here ---------------------------
        if rid == "E04":
            if slug and not pattern.fullmatch(slug):
                findings.append(_finding(rule, location=slug))
            continue

        if rid == "S08":
            # Links are only an inline citation above the Sources section.
            above = markdown.split(f"## {sources_heading}", 1)[0]
            hit = pattern.search(above)
            if hit:
                findings.append(
                    _finding(rule, location=_snippet(above, hit.start(), hit.end()),
                             problem="Inline citation in the body; links belong under Sources.")
                )
            continue

        if rid == "S09":
            # The bare-fence regex also matches CLOSING fences, so pair the
            # fences and flag only an opening fence that carries no language.
            fences = list(re.finditer(r"(?m)^[ \t]*```([^\n]*)$", markdown))
            for i, m in enumerate(fences):
                if i % 2 == 0 and not m.group(1).strip():
                    findings.append(
                        _finding(rule, location=_snippet(markdown, m.start(), m.end()),
                                 problem="A fenced code block opens without a language tag.")
                    )
            continue

        if rid == "S04":
            for m in pattern.finditer(markdown):
                title = m.group(0).strip()
                if title in {f"## {toc}", f"## {sources_heading}"}:
                    continue
                findings.append(_finding(rule, location=title))
            continue

        if rid == "H05":
            all_hits = list(pattern.finditer(markdown))
            in_prose = {m.start() for m in pattern.finditer(prose)}
            for m in all_hits:
                if m.start() not in in_prose:  # placeholder hidden in code/inline
                    findings.append(
                        _finding(rule, location=m.group(0),
                                 problem="Placeholder inside a code span, where a reader could copy it.")
                    )
            if len(all_hits) > 5:
                findings.append(
                    _finding(rule, location=f"{len(all_hits)} placeholders",
                             problem="More than five placeholders — the research or notes are too thin.")
                )
            continue

        if rid == "V14":
            if not pattern.search(prose):
                findings.append(
                    _finding(rule, location="whole post",
                             problem="No contractions anywhere — the draft reads like documentation.")
                )
            continue

        if rid == "V02":
            hits = list(pattern.finditer(prose))
            if len(hits) > 1:
                findings.append(
                    _finding(rule, location=_snippet(prose, hits[1].start(), hits[1].end()),
                             problem=f"The antithesis frame appears {len(hits)} times; at most once.")
                )
            continue

        if rid == "S13":
            hits = list(pattern.finditer(markdown))
            if len(hits) > max_callouts:
                findings.append(
                    _finding(rule, location=f"{len(hits)} callouts",
                             problem=f"{len(hits)} callouts; the cap is {max_callouts}.")
                )
            continue

        # Default polarity: any hit is a violation. Report the first, count all.
        hits = list(pattern.finditer(target))
        if hits:
            note = f" ({len(hits)} hits)" if len(hits) > 1 else ""
            findings.append(
                _finding(rule, location=_snippet(target, hits[0].start(), hits[0].end()) + note)
            )

    return DetectorRun(
        findings=findings,
        measurements=compute_measurements(markdown, settings),
        hints="\n".join(hints),
    )
