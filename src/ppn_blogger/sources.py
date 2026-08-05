"""Harvesting, vetting and filing the sites a wide-web sweep turns up.

The curated feed list in ``sources.yaml`` is deliberately narrow, which is what
makes the research trustworthy and also what makes topic discovery repetitive:
the scouts keep finding the same nine feeds. Exploration mode lifts that ceiling
by letting the scouts range across the open web — but an unvetted domain must
never silently become a citation, so the sweep stops at a list of *candidate
sites* and waits for a human verdict.

Everything here is pure: reports in, candidates out; decisions in, a new
``sources.yaml`` out. No I/O, no agents. That is on purpose — what the operator
is asked to approve must be a faithful record of where the scouts actually went,
not a model's account of it.

The one piece of real machinery is :func:`merge_into_yaml_text`, which edits the
config document line by line rather than re-serialising it. ``sources.yaml`` is
half explanatory comments and is edited by hand in the Config screen; a
round-trip through ``yaml.safe_dump`` would silently throw all of that away. The
surgical edit is verified against the mapping the same decisions produce, and
falls back to a full dump if the two ever disagree.
"""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

import yaml

from .models import ScoutReport, SourceCandidate, SourceDecision
from .tools import classify_domain, domain_of

# Tier applied to a site nobody has classified yet. Score 2 in the shipped
# config: usable as a lead, still needs corroboration before it carries a claim.
DEFAULT_NEW_TIER = "community_unverified"


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------


def harvest_candidates(
    reports: list[ScoutReport], *, declined: list[str] | None = None
) -> list[SourceCandidate]:
    """Group everything the scouts reported by the site it came from.

    New sites sort first, then by how much they contributed: the list opens on
    the decisions that actually need making.
    """
    declined_set = {d.lower().strip() for d in (declined or []) if d}
    grouped: dict[str, list[tuple[str, Any]]] = {}
    for report in reports:
        for item in report.items:
            domain = domain_of(item.url)
            if not domain or domain in declined_set:
                continue
            tier, _ = classify_domain(item.url)
            if tier == "blocked":
                continue
            grouped.setdefault(domain, []).append((report.scout, item))

    candidates: list[SourceCandidate] = []
    for domain, entries in grouped.items():
        # Two scouts finding the same page is one item, not two. `scouts` below
        # still records everyone who found it, so nothing about the site's reach
        # is lost — but the operator should not read the same URL three times.
        items: list[Any] = []
        seen: set[str] = set()
        for _, item in entries:
            if item.url in seen:
                continue
            seen.add(item.url)
            items.append(item)
        tier, _ = classify_domain(f"https://{domain}/")
        known = tier not in {"unknown", "blocked"}
        names = Counter(i.source_name for i in items if i.source_name)
        candidates.append(
            SourceCandidate(
                domain=domain,
                name=names.most_common(1)[0][0] if names else domain,
                known=known,
                current_tier=tier,
                suggested_tier=tier if known else DEFAULT_NEW_TIER,
                item_count=len(items),
                scouts=sorted({scout for scout, _ in entries}),
                items=items,
            )
        )

    candidates.sort(key=lambda c: (c.known, -c.item_count, c.domain))
    return candidates


def filter_reports(reports: list[ScoutReport], approved: list[str]) -> list[ScoutReport]:
    """Drop every item that did not come from an approved site.

    The editor never sees the rejected material, so a source the operator turned
    down cannot influence the shortlist by the back door — not as a signal, not
    as corroboration, not as a seed URL.
    """
    allowed = {d.lower().strip() for d in approved if d}
    out: list[ScoutReport] = []
    for report in reports:
        kept = [i for i in report.items if domain_of(i.url) in allowed]
        out.append(ScoutReport(scout=report.scout, items=kept, notes=report.notes))
    return out


# ---------------------------------------------------------------------------
# Filing the verdict back into sources.yaml
# ---------------------------------------------------------------------------


def apply_decisions(sources: dict[str, Any], decisions: list[SourceDecision]) -> dict[str, Any]:
    """Return ``sources.yaml`` as it should look once the decisions are filed.

    Approving a site adds its domain to the chosen tier (moving it if it already
    sat in another one). Declining a site the config has never heard of records
    it in ``declined_domains`` so it is never proposed again — which is not the
    same as blocking it, and deliberately weaker.

    Declining a site that already carries a tier does nothing here: unchecking a
    trusted source means "not for this shortlist", and quietly demoting the feed
    list from a topic run would be a much larger decision than the operator made.
    """
    result = copy.deepcopy(sources)
    tiers = result.setdefault("trust_tiers", {})
    declined: list[str] = [str(d).lower() for d in result.get("declined_domains") or []]

    for decision in decisions:
        domain = decision.domain.lower().strip()
        if not domain:
            continue

        if not decision.approved:
            already_tiered = any(
                domain in [str(d).lower() for d in (tier.get("domains") or [])]
                for tier in tiers.values()
            )
            if not already_tiered and domain not in declined:
                declined.append(domain)
            continue

        if decision.tier not in tiers:
            raise ValueError(f"Unknown trust tier: {decision.tier}")
        for tier_id, tier in tiers.items():
            domains = [str(d) for d in (tier.get("domains") or [])]
            if tier_id == decision.tier:
                if domain not in [d.lower() for d in domains]:
                    domains.append(domain)
            else:
                domains = [d for d in domains if d.lower() != domain]
            tier["domains"] = domains
        if domain in declined:
            declined.remove(domain)

    result["declined_domains"] = declined
    return result


def merge_into_yaml_text(text: str, decisions: list[SourceDecision]) -> str:
    """Apply the decisions to the raw config document, keeping its comments.

    Falls back to a plain dump if the line edit does not reproduce exactly what
    :func:`apply_decisions` computed — losing the comments is bad, writing config
    nobody predicted is worse.
    """
    current = yaml.safe_load(text) or {}
    expected = apply_decisions(current, decisions)

    try:
        edited = _edit_lines(text, current, expected)
        if edited is not None and (yaml.safe_load(edited) or {}) == expected:
            return edited
    except Exception:  # noqa: BLE001 - any surprise in the text falls back to a dump
        pass
    return yaml.safe_dump(expected, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Line-level YAML editing
# ---------------------------------------------------------------------------


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _meaningful(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _child_key(lines: list[str], start: int, end: int, key: str) -> int | None:
    """Index of ``key:`` among the direct children of a block.

    "Direct child" is the shallowest meaningful indent in the range rather than a
    hard-coded two spaces, so reindenting the document does not break this.
    """
    indents = [_indent(lines[i]) for i in range(start, end) if _meaningful(lines[i])]
    if not indents:
        return None
    base = min(indents)
    for i in range(start, end):
        line = lines[i]
        if _meaningful(line) and _indent(line) == base and line.strip().startswith(f"{key}:"):
            return i
    return None


def _block_end(lines: list[str], key_index: int, end: int) -> int:
    """First line after ``key_index`` that is no longer inside its block."""
    indent = _indent(lines[key_index])
    for i in range(key_index + 1, end):
        if _meaningful(lines[i]) and _indent(lines[i]) <= indent:
            return i
    return end


def _sequence_lines(lines: list[str], start: int, end: int) -> list[int]:
    return [i for i in range(start, end) if _meaningful(lines[i]) and lines[i].lstrip().startswith("- ")]


def _scalar(line: str) -> str:
    return line.lstrip()[2:].strip().strip("\"'")


def _edit_lines(text: str, current: dict[str, Any], expected: dict[str, Any]) -> str | None:
    """Add and remove single sequence entries in place. None means "cannot"."""
    lines = text.splitlines()

    for tier_id, tier in (expected.get("trust_tiers") or {}).items():
        before = [
            str(d) for d in ((current.get("trust_tiers") or {}).get(tier_id, {}).get("domains") or [])
        ]
        after = [str(d) for d in (tier.get("domains") or [])]
        if before == after:
            continue
        lines = _rewrite_sequence(lines, ["trust_tiers", tier_id, "domains"], before, after)
        if lines is None:
            return None

    before = [str(d) for d in (current.get("declined_domains") or [])]
    after = [str(d) for d in (expected.get("declined_domains") or [])]
    if before != after:
        lines = _rewrite_sequence(lines, ["declined_domains"], before, after)
        if lines is None:
            return None

    return "\n".join(lines) + "\n"


_DECLINED_HEADER = [
    "",
    "# Sites turned down in a source review. Not blocked — merely never offered",
    "# for approval again. Delete a line here to have it proposed once more.",
]


def _locate(lines: list[str], path: list[str]) -> tuple[int, int, int] | None:
    """Resolve a key path to ``(key_index, block_start, block_end)``."""
    start, end = 0, len(lines)
    key_index: int | None = None
    for key in path:
        key_index = _child_key(lines, start, end, key)
        if key_index is None:
            return None
        end = _block_end(lines, key_index, end)
        start = key_index + 1
    assert key_index is not None
    return key_index, start, end


def _rewrite_sequence(
    lines: list[str], path: list[str], before: list[str], after: list[str]
) -> list[str] | None:
    """Add and remove entries one at a time, re-locating the block each time.

    Editing one entry per pass costs a few extra scans of a 110-line file and
    removes the whole class of bugs where an earlier deletion invalidates the
    line numbers a later insertion was computed from.
    """
    out: list[str] | None = list(lines)
    for domain in (d for d in before if d not in after):
        out = _remove_item(out, path, domain)
        if out is None:
            return None
    for domain in (d for d in after if d not in before):
        out = _append_item(out, path, domain)
        if out is None:
            return None
    return out


def _remove_item(lines: list[str], path: list[str], domain: str) -> list[str] | None:
    located = _locate(lines, path)
    if located is None:
        return None
    _, start, end = located
    for i in _sequence_lines(lines, start, end):
        if _scalar(lines[i]) == domain:
            return lines[:i] + lines[i + 1 :]
    return lines  # already absent


def _append_item(lines: list[str], path: list[str], domain: str) -> list[str] | None:
    located = _locate(lines, path)
    if located is None:
        # Only a missing top-level declined_domains is recoverable: append it.
        if path != ["declined_domains"]:
            return None
        return lines + _DECLINED_HEADER + ["declined_domains:", f"  - {domain}"]

    key_index, start, end = located
    key_line = lines[key_index]
    if key_line.rstrip().endswith("[]"):
        # An empty flow list has no entry to copy indentation from — expand it.
        out = list(lines)
        out[key_index] = key_line.rstrip()[: -len("[]")].rstrip()
        out.insert(key_index + 1, f"{' ' * (_indent(key_line) + 2)}- {domain}")
        return out

    item_indexes = _sequence_lines(lines, start, end)
    anchor = item_indexes[-1] if item_indexes else key_index
    prefix = " " * (_indent(lines[anchor]) if item_indexes else _indent(key_line) + 2)
    return lines[: anchor + 1] + [f"{prefix}- {domain}"] + lines[anchor + 1 :]
