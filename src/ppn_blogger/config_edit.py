"""Rendering a typed proposal into an edit of a config document.

This module is the trust boundary. A model upstream produces a *typed* proposal —
a rule, a note, a scalar — and everything that turns it into document text happens
here, in code, under an allowlist. Nothing a model wrote is ever pasted into a
config document, and no code path here can rewrite a document wholesale.

Three properties are load-bearing, and each was chosen against an easier option.

**Append-only, never modify.** An existing rule's text, severity, ``auto`` flag
and detector are immutable to this module, and ``honesty_rules`` is closed to it
entirely. A learner able to weaken H01 could learn its way out of the fabrication
gate, and the evidence it works from — what a human changed while polishing prose
— says nothing about fabrication anyway.

**No fallback dump for a hand-written document.** ``sources.merge_into_yaml_text``
falls back to ``yaml.safe_dump`` when its line surgery fails, which is the right
call there. It is the wrong call for ``validation_rules.yaml``: 26 KB of which
most is explanation, including the comment block documenting the
``auto:``-with-no-detector trap that once let an eleven-section draft score 93.5.
Losing that to a bookkeeping failure is worse than losing the proposal, so
``append_rule``, ``set_profile_scalar`` and ``insert_under_heading`` return a
Refusal instead and the candidate is discarded with a reason. A test reads those
functions' source to be sure none of them can reach a dump.

``append_guidance`` is the one exception, and only because
``config/agent_guidance.yaml`` is generated and maintained by this feature: its
explanation is a fixed header, never interleaved with the entries, so it is
re-attached explicitly after the dump.

**Anchors resolve against fence-masked text.** ``config/style_guide.md`` §8 holds
``## Contents``, ``## <Section 1>`` and ``## Sources`` *inside a fenced block* — a
skeleton the Writer copies literally. Matching an anchor without masking fences
would splice editorial policy into that template, where it would be reproduced as
prose in every post.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from .detectors import COMPUTED_RULES, prose_only

# ---------------------------------------------------------------------------
# Line-level YAML helpers
#
# Promoted here from sources.py, which imports them back. They are the only part
# of that module's line surgery that is path-generic; its *editors* handle scalar
# sequence items only and do not generalise to a mapping in a sequence, which is
# what a validation rule is.
# ---------------------------------------------------------------------------


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def meaningful(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def child_key(lines: list[str], start: int, end: int, key: str) -> int | None:
    """Index of ``key:`` among the direct children of a block.

    "Direct child" is the shallowest meaningful indent in the range rather than a
    hard-coded two spaces, so reindenting the document does not break this.
    """
    indents = [indent_of(lines[i]) for i in range(start, end) if meaningful(lines[i])]
    if not indents:
        return None
    base = min(indents)
    for i in range(start, end):
        line = lines[i]
        if meaningful(line) and indent_of(line) == base and line.strip().startswith(f"{key}:"):
            return i
    return None


def block_end(lines: list[str], key_index: int, end: int) -> int:
    """First line after ``key_index`` that is no longer inside its block."""
    indent = indent_of(lines[key_index])
    for i in range(key_index + 1, end):
        if meaningful(lines[i]) and indent_of(lines[i]) <= indent:
            return i
    return end


def locate(lines: list[str], path: list[str]) -> tuple[int, int, int] | None:
    """Resolve a key path to ``(key_index, block_start, block_end)``."""
    start, end = 0, len(lines)
    key_index: int | None = None
    for key in path:
        key_index = child_key(lines, start, end, key)
        if key_index is None:
            return None
        end = block_end(lines, key_index, end)
        start = key_index + 1
    assert key_index is not None
    return key_index, start, end


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------

#: The only documents this module will ever write.
#:
#: ``sources`` is absent deliberately: trusted domains belong to the source
#: review, and a learner adding one would route around "the review is code, never
#: judgement". ``model_prices`` is absent because a meter binding is a human act
#: and a wrong binding reads exactly like a right answer.
WRITABLE_DOCUMENTS = frozenset({"validation_rules", "style_guide", "blog_profile", "agent_guidance"})

#: Rule families the learner may append to. ``honesty_rules`` is not one of them.
WRITABLE_RULE_GROUPS = frozenset(
    {
        "typography_rules",
        "voice_rules",
        "content_rules",
        "focus_rules",
        "structure_rules",
        "seo_rules",
    }
)

#: A learned rule may never block or downgrade a run on its own.
WRITABLE_SEVERITIES = frozenset({"minor", "info"})

_GROUP_PREFIX = {
    "honesty_rules": "H",
    "typography_rules": "T",
    "voice_rules": "V",
    "content_rules": "C",
    "focus_rules": "F",
    "structure_rules": "S",
    "seo_rules": "E",
}

#: Scalar keys in blog_profile.yaml the learner may set, with hard bounds.
#: Anything absent from this mapping is refused, including every identity,
#: translation and taxonomy key.
PROFILE_SCALARS: dict[str, tuple[int, int]] = {
    "structure.min_sections": (3, 8),
    "structure.max_sections": (3, 10),
    "structure.min_section_words": (120, 400),
    "structure.max_section_words": (300, 900),
    "structure.max_callouts": (0, 6),
    "structure.opening_paragraphs": (1, 3),
}

#: The one list the learner may append to, and the best thing it can learn.
PROFILE_LISTS = frozenset({"structure.banned_headings"})


@dataclass(slots=True)
class Refusal:
    """Why a proposal was not rendered. Always shown to the operator."""

    reason: str


# ---------------------------------------------------------------------------
# Regex safety
# ---------------------------------------------------------------------------

# Python's re has no timeout, so the obviously catastrophic shapes are stopped at
# the door rather than in a worker. This is a cheap first pass, not the guarantee:
# the guarantee is the gate running every candidate detector in a separate process
# under a wall-clock timeout, because a thread cannot be killed.
#
# What is refused is a group holding *one* quantified atom, itself quantified —
# (a+)+, (\w*)*, ([a-z]+)* — where the inner and outer quantifiers match the same
# text and backtracking explodes. Deliberately NOT refused is the far commoner
# (-[a-z0-9]+)* idiom, where a literal separator makes the alternation
# unambiguous: E04's slug check is written that way, and a check that rejects the
# rules already shipped is miscalibrated.
_NESTED_QUANTIFIER = re.compile(r"\(\s*(?:\[[^\]]*\]|\\?\w|\.)\s*[+*]\s*\)\s*[+*]")
_BACKREFERENCE = re.compile(r"\\[1-9]")
# Lookbehind is deliberately *not* refused. Python accepts only the fixed-width
# form and raises on the variable-width one that backtracks, so `re.compile`
# below already enforces the safe subset — and T02, one of the two rules this
# whole masking layer exists for, is written with one.
# A leading (?i), (?m), (?s) or a combination is ordinary and nine of the shipped
# detectors use one. What is refused is a flag group anywhere else — Python
# rejects those at compile time from 3.11 anyway — and (?x), which makes
# whitespace insignificant and so lets a pattern read as something it is not.
_LEADING_FLAGS = re.compile(r"^\(\?[ims]+\)")
_ANY_FLAGS = re.compile(r"\(\?[aiLmsux]+\)")
# Calibrated against the longest detector the ruleset already carries: V01's
# banned-vocabulary alternation is 566 characters, and extending exactly that
# kind of list is among the most useful things this feature can learn. Length is
# not what makes a pattern dangerous — nested quantifiers are — so the cap is a
# sanity bound rather than the safety check, and a test keeps it above the
# longest shipped rule.
MAX_DETECTOR_LENGTH = 700


def check_detector(pattern: str) -> Refusal | None:
    """None when the pattern is safe to compile and run. A Refusal otherwise.

    This is the cheap syntactic half. The expensive half — running it under a
    wall-clock timeout in a separate process — belongs to the gate, because a
    thread cannot be killed and a hung worker takes the readiness probe with it.
    """
    if not pattern:
        return Refusal("no detector supplied")
    if len(pattern) > MAX_DETECTOR_LENGTH:
        return Refusal(f"detector is {len(pattern)} characters, over the {MAX_DETECTOR_LENGTH} cap")
    if _NESTED_QUANTIFIER.search(pattern):
        return Refusal("detector nests quantifiers, which backtracks catastrophically")
    if _BACKREFERENCE.search(pattern):
        return Refusal("detector uses a backreference")
    if _ANY_FLAGS.search(_LEADING_FLAGS.sub("", pattern, count=1)):
        return Refusal("detector sets inline flags other than a leading (?i), (?m) or (?s)")
    try:
        re.compile(pattern)
    except re.error as exc:
        return Refusal(f"detector does not compile: {exc}")
    return None


# A second, cheaper denylist over the proposal's prose. A delta can carry text
# from pages the researcher read, so a proposal is treated as untrusted content
# even though a human wrote the edit it came from (OWASP agentic ASI06).
_DENIED_CONTENT = (
    (re.compile(r"https?://", re.I), "proposal contains a URL"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "proposal contains an email address"),
    (re.compile(r"\b(?:api[_ -]?key|secret|token|password|bearer)\b", re.I), "proposal names a credential"),
    (re.compile(r"\b(?:ignore|disregard|override)\b.{0,30}\b(?:instruction|rule|previous)", re.I),
     "proposal reads as an instruction to the agent rather than a rule"),
    (re.compile(r"\b(?:tool|connector|permission|mcp)\b", re.I), "proposal mentions tools or permissions"),
)


def check_content(*texts: str) -> Refusal | None:
    """None when nothing in the proposal's prose looks like an injected payload."""
    blob = "\n".join(t for t in texts if t)
    for pattern, reason in _DENIED_CONTENT:
        if pattern.search(blob):
            return Refusal(reason)
    return None


# ---------------------------------------------------------------------------
# Rule ids
# ---------------------------------------------------------------------------

_RULE_ID = re.compile(r"^([A-Z])(\d+)$")


def next_rule_id(all_rules: list[dict[str, Any]], group: str) -> str | None:
    """The next free id in a group, allocated by code and never by a model.

    Checked against every id in the ruleset *and* against ``COMPUTED_RULES`` —
    reusing S02 or F04 would silently reassign a rule the detector layer decides
    in Python, which no test would catch until a draft scored wrongly.
    """
    prefix = _GROUP_PREFIX.get(group)
    if prefix is None:
        return None
    taken: set[int] = set()
    reserved = {r for r in COMPUTED_RULES}
    for rule in all_rules:
        match = _RULE_ID.match(str(rule.get("id", "")))
        if match and match.group(1) == prefix:
            taken.add(int(match.group(2)))
    for number in range(1, 100):
        candidate = f"{prefix}{number:02d}"
        if number not in taken and candidate not in reserved:
            return candidate
    return None


# ---------------------------------------------------------------------------
# validation_rules.yaml — append one rule
# ---------------------------------------------------------------------------


def _wrap(text: str, width: int = 88) -> list[str]:
    out: list[str] = []
    line = ""
    for word in (text or "").split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out or [""]


def _render_rule(rule: dict[str, Any], item_indent: int) -> list[str]:
    """One rule as YAML lines, in the style the document already uses."""
    pad = " " * item_indent
    child = " " * (item_indent + 2)
    lines = [f"{pad}- id: {rule['id']}"]

    lines.append(f"{child}rule: >-")
    lines.extend(f"{child}  {piece}" for piece in _wrap(rule["rule"]))
    lines.append(f"{child}severity: {rule['severity']}")
    lines.append(f"{child}auto: {'true' if rule.get('auto') else 'false'}")
    if rule.get("detector"):
        # Single-quoted, with the YAML escape for an embedded quote. Regexes are
        # full of backslashes and a double-quoted scalar would mangle them.
        escaped = str(rule["detector"]).replace("'", "''")
        lines.append(f"{child}detector: '{escaped}'")
        lines.append(f"{child}prose_only: {'true' if rule.get('prose_only') else 'false'}")
    for key in ("check_hint", "fix_hint"):
        if rule.get(key):
            lines.append(f"{child}{key}: >-")
            lines.extend(f"{child}  {piece}" for piece in _wrap(rule[key]))
    return lines


def append_rule(text: str, group: str, rule: dict[str, Any]) -> str | Refusal:
    """Append one rule to a group, preserving every comment in the document."""
    if group not in WRITABLE_RULE_GROUPS:
        return Refusal(f"{group} is closed to the learner")
    if rule.get("severity") not in WRITABLE_SEVERITIES:
        return Refusal(f"a learned rule may not be {rule.get('severity')!r}")

    current = yaml.safe_load(text) or {}
    if group not in current:
        return Refusal(f"{group} is not present in the document")
    if any(str(r.get("id")) == rule["id"] for r in (current.get(group) or [])):
        return Refusal(f"rule id {rule['id']} is already taken")

    lines = text.splitlines()
    found = locate(lines, [group])
    if found is None:
        return Refusal(f"could not find {group} in the document text")
    _, start, end = found

    items = [i for i in range(start, end) if meaningful(lines[i]) and lines[i].lstrip().startswith("- ")]
    if not items:
        return Refusal(f"{group} holds no rules to append after")
    item_indent = indent_of(lines[items[-1]])

    # Insert after the last meaningful line of the block, so a trailing comment
    # that belongs to the *next* section is not swallowed into this one.
    insert_at = end
    while insert_at > start and not meaningful(lines[insert_at - 1]):
        insert_at -= 1

    edited = lines[:insert_at] + _render_rule(rule, item_indent) + lines[insert_at:]
    rendered = "\n".join(edited) + "\n"

    expected = {**current, group: [*(current.get(group) or []), _expected_rule(rule)]}
    return _verified(rendered, expected)


def _expected_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """What the appended rule must parse back as. Computed independently of the
    text, so the round-trip check is a real check and not a restatement."""
    out: dict[str, Any] = {
        "id": rule["id"],
        "rule": " ".join(str(rule["rule"]).split()),
        "severity": rule["severity"],
        "auto": bool(rule.get("auto")),
    }
    if rule.get("detector"):
        out["detector"] = str(rule["detector"])
        out["prose_only"] = bool(rule.get("prose_only"))
    for key in ("check_hint", "fix_hint"):
        if rule.get(key):
            out[key] = " ".join(str(rule[key]).split())
    return out


# ---------------------------------------------------------------------------
# blog_profile.yaml — set one whitelisted scalar, or append to one list
# ---------------------------------------------------------------------------


def set_profile_scalar(text: str, key: str, value: str) -> str | Refusal:
    """Set a whitelisted numeric key, within its bounds and its invariants."""
    bounds = PROFILE_SCALARS.get(key)
    if bounds is None:
        return Refusal(f"{key} is not a key the learner may set")
    try:
        number = int(str(value).strip())
    except ValueError:
        return Refusal(f"{value!r} is not a whole number")
    low, high = bounds
    if not low <= number <= high:
        return Refusal(f"{key}={number} is outside the permitted range {low}-{high}")

    current = yaml.safe_load(text) or {}
    path = key.split(".")
    expected = _deep_set(current, path, number)

    invariant = _profile_invariants(expected)
    if invariant is not None:
        return invariant

    lines = text.splitlines()
    found = locate(lines, path)
    if found is None:
        return Refusal(f"could not find {key} in the document text")
    key_index, _, _ = found
    line = lines[key_index]
    leading = line[: len(line) - len(line.lstrip(" "))]
    comment = ""
    if "#" in line.split(":", 1)[1]:
        comment = "  #" + line.split(":", 1)[1].split("#", 1)[1]
    lines[key_index] = f"{leading}{path[-1]}: {number}{comment}"

    return _verified("\n".join(lines) + "\n", expected)


def _profile_invariants(profile: dict[str, Any]) -> Refusal | None:
    """Cross-key rules a single bounded scalar cannot enforce on its own."""
    structure = profile.get("structure") or {}
    lo, hi = structure.get("min_sections"), structure.get("max_sections")
    if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
        return Refusal(f"min_sections ({lo}) would exceed max_sections ({hi})")
    wlo, whi = structure.get("min_section_words"), structure.get("max_section_words")
    if isinstance(wlo, int) and isinstance(whi, int) and wlo > whi:
        return Refusal(f"min_section_words ({wlo}) would exceed max_section_words ({whi})")
    return None


def _deep_set(mapping: dict[str, Any], path: list[str], value: Any) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in mapping.items()}
    cursor = out
    for key in path[:-1]:
        nested = cursor.get(key)
        cursor[key] = dict(nested) if isinstance(nested, dict) else {}
        cursor = cursor[key]
    cursor[path[-1]] = value
    return out


# ---------------------------------------------------------------------------
# agent_guidance.yaml — append one standing instruction
# ---------------------------------------------------------------------------

#: The only agents that may be given learned guidance. Not the validators: a
#: learner able to coach the checker can teach it to accept the writer's
#: mistakes, and the loop stops being supervised by anything.
GUIDANCE_AGENTS = frozenset({"writer", "outliner"})


def append_guidance(
    text: str, agent: str, entry: dict[str, Any], *, max_entries: int = 8
) -> str | Refusal:
    """Append one guidance entry for one agent, with its provenance.

    Whole-document YAML here rather than line surgery: this file is generated and
    maintained by this feature, its comments are a fixed header that
    ``yaml.safe_dump`` never sees, and its entries carry structured provenance
    that is far easier to get right as data than as text. The cap is real — an
    instruction block that grows without bound degrades the output it exists to
    improve.
    """
    if agent not in GUIDANCE_AGENTS:
        return Refusal(f"{agent} may not be given learned guidance")

    current = yaml.safe_load(text) or {}
    agents = dict(current.get("agents") or {})
    block = dict(agents.get(agent) or {})
    entries = list(block.get("guidance") or [])

    ceiling = int((current.get("meta") or {}).get("max_entries", max_entries) or max_entries)
    if len(entries) >= ceiling:
        return Refusal(
            f"{agent} already carries {len(entries)} lessons, the configured ceiling. "
            "Prune before adding another."
        )
    if any(str(e.get("text", "")).strip() == entry["text"].strip() for e in entries):
        return Refusal("that guidance is already present")

    numbers = [int(str(e.get("id", "G00"))[1:]) for e in entries if str(e.get("id", "")).startswith("G")]
    entry = {"id": f"G{max(numbers, default=0) + 1:02d}", **entry}

    block["guidance"] = [*entries, entry]
    agents[agent] = block
    updated = {**current, "agents": agents}

    header = _leading_comments(text)
    body = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=88)
    return f"{header}{body}" if header else body


def _leading_comments(text: str) -> str:
    """The comment header at the top of a document, so a dump keeps it.

    Only safe because this document's explanation is entirely in its header —
    which is why the same trick is not used for ``validation_rules.yaml``, where
    the comments are interleaved with the rules they explain.
    """
    out: list[str] = []
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            break
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return ("\n".join(out) + "\n\n") if out else ""


# ---------------------------------------------------------------------------
# style_guide.md — insert beneath an existing heading
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{2,3})\s+(.*?)\s*$")


def insert_under_heading(text: str, anchor: str, note: str) -> str | Refusal:
    """Add lines beneath an existing heading. Never deletes, never rewrites.

    The anchor is matched against fence-masked text: §8 of the style guide holds
    headings inside a fenced skeleton the Writer copies literally, and inserting
    policy there would put it in every post.
    """
    anchor = (anchor or "").strip()
    if not _HEADING.match(anchor):
        return Refusal("anchor is not a level-2 or level-3 heading")
    if not (note or "").strip():
        return Refusal("nothing to insert")
    if len(note.splitlines()) > 6:
        return Refusal("a style note may not exceed six lines")

    lines = text.splitlines()
    masked = prose_only(text).splitlines()

    matches = [i for i, line in enumerate(masked) if line.strip() == anchor and lines[i].strip() == anchor]
    if not matches:
        return Refusal(f"no heading {anchor!r} outside a code fence")
    if len(matches) > 1:
        return Refusal(f"heading {anchor!r} appears more than once; it is not a stable anchor")

    start = matches[0]
    level = len(_HEADING.match(anchor).group(1))
    boundary = len(lines)
    for i in range(start + 1, len(lines)):
        found = _HEADING.match(masked[i])
        if found and len(found.group(1)) <= level:
            boundary = i
            break
    while boundary > start + 1 and not lines[boundary - 1].strip():
        boundary -= 1

    addition = ["", *note.strip().splitlines()]
    edited = lines[:boundary] + addition + lines[boundary:]
    rendered = "\n".join(edited) + ("\n" if text.endswith("\n") else "")

    if not _is_insert_only(lines, rendered.splitlines()):
        return Refusal("the edit would have changed existing text, not only added to it")
    return rendered


def _is_insert_only(before: list[str], after: list[str]) -> bool:
    """Every original line still present, in its original order."""
    cursor = iter(after)
    return all(any(line == candidate for candidate in cursor) for line in before)


# ---------------------------------------------------------------------------
# The round-trip check
# ---------------------------------------------------------------------------


def _verified(rendered: str, expected: dict[str, Any]) -> str | Refusal:
    """Parse the edited text back and require it to equal the expected mapping.

    ``expected`` is computed from the parsed document rather than from the text
    that was just written, so this catches a line edit that produced valid YAML
    saying something nobody asked for. There is no dump fallback: a mismatch
    discards the proposal.
    """
    try:
        parsed = yaml.safe_load(rendered) or {}
    except yaml.YAMLError as exc:
        return Refusal(f"the edit did not produce valid YAML: {exc}")
    if parsed != expected:
        return Refusal("the edit did not reproduce the change that was computed")
    return rendered
