"""Scoring the difference between what the crew wrote and what the author shipped.

Pure: no I/O, no database, no agents, and it never raises. Same shape as
``sources.py`` and ``news.py``, and for the same reason — everything decided here
is decided the same way every time, and can be tested without a service.

Three deliberate choices, each of which the obvious implementation gets wrong.

**Both sides go through one normaliser.** A draft is compared against the file
the author edited in place, so the two differ in formatting the moment anything
round-trips through an editor. Normalising each side with the *same* function
means those differences cancel instead of being reported as edits. The raw text
is kept for display; only the normalised text is ever diffed or shown to a model.

**The unit is a block, and the metric is words.** Character-level edit distance
is dominated by trivial rewording, and a word-level rate over blocks is what the
machine-translation field settled on decades ago (there it is called HTER). It is
objective, free to compute and hard to game — which is why nothing in this
feature asks a model for a quality score. A model classifies *what kind* of edit
happened; arithmetic decides *how much*.

**The section diff comes first.** "The author always deletes the fifth section"
or "the closing heading is always renamed" is a far more actionable pattern than
a thousand word-level tweaks, and it falls straight out of comparing two lists of
headings. It is computed separately and reported first.

One more property worth stating: the hunks and the rate computed here are stored
on the pair and shipped to the UI, which renders them and never diffs. So unlike
the run canvas — where ``ui/src/lib/deriveNodes.ts`` must be kept a faithful port
of ``derive_nodes()`` — there is nothing here to keep in lockstep.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .detectors import split_sections
from .models import EditObservation
from .storage import split_front_matter
from .util import strip_h1

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

# Curly quotes and dashes are editorial choices the style guide has opinions
# about, but a difference in *encoding* is not an edit. Both sides are folded to
# the same characters before comparison; rules T01/T02 still judge the raw draft.
_PUNCTUATION = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
    "…": "...",
}
_PUNCT_RE = re.compile("|".join(map(re.escape, _PUNCTUATION)))

# ``*`` and ``+`` bullets mean the same thing as ``-``; an editor that reflows a
# list must not read as a rewrite of every item in it.
_BULLET = re.compile(r"(?m)^([ \t]*)[*+](\s)")
_TRAILING = re.compile(r"(?m)[ \t]+$")
_BLANKS = re.compile(r"\n{3,}")


_FRONT_MATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)


def normalise(markdown: str) -> str:
    """The comparable form of a draft body.

    Front matter and the H1 come off first. Neither reaches WordPress — the
    publisher pushes ``strip_h1(draft.markdown)[1]`` — and neither is something
    the author edits as prose, so leaving them in reports a deletion of both on
    every single pair.

    The textual fallback is not paranoia. ``split_front_matter`` returns the
    *whole document* when the block does not parse, and a title containing a
    colon is enough to do that. The H1 then survives on one side and not the
    other, and the analyst is handed "the author deleted the title" as though it
    were an editorial habit worth learning from.
    """
    body = _FRONT_MATTER.sub("", (markdown or "").lstrip(), count=1)
    _, body = split_front_matter(body)
    _, body = strip_h1(body)
    text = unicodedata.normalize("NFC", body)
    text = _PUNCT_RE.sub(lambda m: _PUNCTUATION[m.group(0)], text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}-{m.group(2)}", text)
    text = _TRAILING.sub("", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()


_WORD = re.compile(r"\b[\w'\-]+\b")


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


# ---------------------------------------------------------------------------
# Blocks — the unit of comparison
# ---------------------------------------------------------------------------

_FENCE_LINE = re.compile(r"^\s*```")


def split_blocks(text: str) -> list[str]:
    """Paragraph-ish blocks, with fenced code kept whole.

    Splitting on blank lines inside a code fence would report a reformatted
    snippet as several unrelated edits, so a fence is one block however many
    blank lines it contains.
    """
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False

    for line in (text or "").splitlines():
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
            current.append(line)
            continue
        if not line.strip() and not in_fence:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)

    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Hunk:
    """One difference, in the author's favour: ``after`` is what shipped."""

    op: str  # equal | replace | insert | delete
    before: str = ""
    after: str = ""
    section: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"op": self.op, "before": self.before, "after": self.after, "section": self.section}


@dataclass(slots=True)
class SectionChange:
    op: str  # equal | replace | insert | delete
    before: str = ""
    after: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"op": self.op, "before": self.before, "after": self.after}


@dataclass(slots=True)
class DeltaScore:
    """Everything arithmetic can say about one pair."""

    edit_rate: float = 0.0
    overlap: float = 1.0
    changed_blocks: int = 0
    total_blocks: int = 0
    identical: bool = True
    hunks: list[Hunk] = field(default_factory=list)
    sections: list[SectionChange] = field(default_factory=list)

    @property
    def edit_rate_permille(self) -> int:
        """The rate as an integer, because money and metrics never cross the
        SQLite/SQL Server seam as floats."""
        return int(round(self.edit_rate * 1000))

    def as_dict(self) -> dict[str, Any]:
        return {
            "edit_rate": round(self.edit_rate, 4),
            "overlap": round(self.overlap, 4),
            "changed_blocks": self.changed_blocks,
            "total_blocks": self.total_blocks,
            "identical": self.identical,
            "hunks": [h.as_dict() for h in self.hunks],
            "sections": [s.as_dict() for s in self.sections],
        }


def section_diff(before: str, after: str) -> list[SectionChange]:
    """Compare the two documents' H2 sequences.

    Reported first in the UI, because a structural change is legible in a way a
    prose diff is not: added, removed, reordered or renamed sections say what the
    author thought the shape of the post should have been.
    """
    lhs = [title for title, _ in split_sections(before)]
    rhs = [title for title, _ in split_sections(after)]
    out: list[SectionChange] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, lhs, rhs, autojunk=False).get_opcodes():
        if op == "equal":
            out.extend(SectionChange("equal", t, t) for t in lhs[i1:i2])
        elif op == "replace":
            # Pair them off positionally: a rename reads better as one row than
            # as a delete beside an unrelated insert.
            old, new = lhs[i1:i2], rhs[j1:j2]
            for k in range(max(len(old), len(new))):
                out.append(
                    SectionChange(
                        "replace" if k < len(old) and k < len(new) else ("delete" if k < len(old) else "insert"),
                        old[k] if k < len(old) else "",
                        new[k] if k < len(new) else "",
                    )
                )
        elif op == "delete":
            out.extend(SectionChange("delete", t, "") for t in lhs[i1:i2])
        elif op == "insert":
            out.extend(SectionChange("insert", "", t) for t in rhs[j1:j2])
    return out


def _section_for(block: str, sections: list[tuple[str, str]]) -> str:
    for title, body in sections:
        if block and block in body:
            return title
    return ""


def diff(before: str, after: str) -> list[Hunk]:
    """Block-level hunks. ``before`` and ``after`` must already be normalised."""
    lhs, rhs = split_blocks(before), split_blocks(after)
    sections = split_sections(before)
    out: list[Hunk] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, lhs, rhs, autojunk=False).get_opcodes():
        if op == "equal":
            out.extend(Hunk("equal", b, b, _section_for(b, sections)) for b in lhs[i1:i2])
        elif op == "replace":
            old, new = lhs[i1:i2], rhs[j1:j2]
            for k in range(max(len(old), len(new))):
                o = old[k] if k < len(old) else ""
                n = new[k] if k < len(new) else ""
                kind = "replace" if o and n else ("delete" if o else "insert")
                out.append(Hunk(kind, o, n, _section_for(o, sections)))
        elif op == "delete":
            out.extend(Hunk("delete", b, "", _section_for(b, sections)) for b in lhs[i1:i2])
        elif op == "insert":
            out.extend(Hunk("insert", "", b, "") for b in rhs[j1:j2])
    return out


def edit_rate(before: str, after: str) -> float:
    """Word-level edit rate, 0.0 to 1.0. The field calls this HTER.

    Normalised against the longer side so an author who deletes half the post and
    one who doubles it both score the same magnitude of change.
    """
    lhs, rhs = words(before), words(after)
    if not lhs and not rhs:
        return 0.0
    matched = sum(
        block.size for block in SequenceMatcher(None, lhs, rhs, autojunk=False).get_matching_blocks()
    )
    return 1.0 - (matched / max(len(lhs), len(rhs)))


def lexical_overlap(before: str, after: str) -> float:
    """Token overlap, 0.0 to 1.0 — a cheap stand-in for semantic similarity.

    Read together with the edit rate this routes a hunk *before any model sees
    it*: a high edit rate with high overlap is the author rephrasing, which a
    style rule can fix; a high edit rate with low overlap means the content
    itself changed, which is a research failure and not a prompt problem.
    """
    lhs, rhs = set(words(before)), set(words(after))
    if not lhs and not rhs:
        return 1.0
    if not lhs or not rhs:
        return 0.0
    return len(lhs & rhs) / len(lhs | rhs)


def score(before_raw: str, after_raw: str) -> DeltaScore:
    """Normalise both sides and measure. The one entry point callers need."""
    before, after = normalise(before_raw), normalise(after_raw)
    hunks = diff(before, after)
    changed = [h for h in hunks if h.op != "equal"]
    return DeltaScore(
        edit_rate=edit_rate(before, after),
        overlap=lexical_overlap(before, after),
        changed_blocks=len(changed),
        total_blocks=len(hunks),
        identical=before == after,
        hunks=hunks,
        sections=section_diff(before, after),
    )


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    """a an and are as at be but by for from had has have he in into is it its of on or
    that the their then there these they this to was were will with you your""".split()
)
_SLUG_STRIP = re.compile(r"[^a-z0-9\s]+")


def _stem(word: str) -> str:
    """Crude, deliberate suffix stripping.

    "removes the hedging adverb" and "removed a hedging adverb" describe the same
    recurring edit, and a fingerprint that separates them means the pattern never
    reaches three posts and nothing is ever learned. A real stemmer is a
    dependency and a source of surprises; this covers the tense and plural
    variation that actually shows up, and only where a stem of four characters
    survives, so short words are left alone.
    """
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            if suffix == "s" and word.endswith("ss"):
                return word
            return word[: -len(suffix)]
    return word


def signature_slug(signature: str) -> str:
    """A signature reduced to its meaningful tokens, stemmed and sorted.

    Two analysts describing the same recurring edit will not choose the same word
    order, the same filler or the same tense, so the tokens are stemmed, sorted
    and stripped of stopwords. Without this the recurrence threshold never fires.
    """
    tokens = _SLUG_STRIP.sub(" ", (signature or "").lower()).split()
    meaningful = {_stem(t) for t in tokens if t not in _STOPWORDS and len(t) > 2}
    return "-".join(sorted(meaningful))


def fingerprint(edit_kind: str, target: str, language: str, signature: str) -> str:
    """The clustering key. Stable across casing, word order and whitespace."""
    basis = f"{edit_kind}|{target}|{(language or 'en').lower()}|{signature_slug(signature)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class Cluster:
    """A recurring pattern, and the evidence for it."""

    fingerprint: str
    edit_kind: str
    target: str
    language: str
    label: str
    post_ids: list[int] = field(default_factory=list)
    observations: list[EditObservation] = field(default_factory=list)

    @property
    def distinct_posts(self) -> int:
        return len(set(self.post_ids))

    @property
    def occurrences(self) -> int:
        return len(self.observations)


def cluster(
    observations: list[tuple[int, EditObservation]], *, language: str = "en"
) -> list[Cluster]:
    """Group ``(post_id, observation)`` pairs by fingerprint, commonest first.

    Recurrence is counted in **distinct posts**, never in observations. One post
    edited the same way four times is one piece of evidence about the crew; four
    posts edited that way once each is four. Regenerating a draft would otherwise
    manufacture a pattern out of a single opinion.
    """
    grouped: dict[str, Cluster] = {}
    for post_id, obs in observations:
        key = fingerprint(obs.edit_kind, obs.target, language, obs.signature)
        found = grouped.get(key)
        if found is None:
            found = Cluster(
                fingerprint=key,
                edit_kind=obs.edit_kind,
                target=obs.target,
                language=language,
                label=obs.signature.strip(),
            )
            grouped[key] = found
        found.post_ids.append(post_id)
        found.observations.append(obs)
    return sorted(grouped.values(), key=lambda c: (-c.distinct_posts, -c.occurrences, c.label))


def recurring(clusters: list[Cluster], *, min_distinct_posts: int) -> list[Cluster]:
    """The clusters that have earned a proposal. Nothing else is ever offered."""
    return [c for c in clusters if c.distinct_posts >= max(1, min_distinct_posts)]


# ---------------------------------------------------------------------------
# The over-correction guard
# ---------------------------------------------------------------------------


def already_clean(rates: list[float], *, threshold: float) -> bool:
    """True when the corpus is already edited so little that a rule will hurt.

    The lesson automatic post-editing learned the expensive way: once a system's
    output is good, further corrections start firing on text that was already
    right, and the fix costs more than the fault. Below the threshold this
    feature proposes nothing and says so, rather than manufacturing work.
    """
    if not rates:
        return True
    return (sum(rates) / len(rates)) < threshold


def by_section(pairs: list[DeltaScore]) -> dict[str, float]:
    """Mean edit rate per section title, for the trend panel."""
    totals: dict[str, list[float]] = defaultdict(list)
    for pair in pairs:
        for hunk in pair.hunks:
            if not hunk.section:
                continue
            totals[hunk.section].append(0.0 if hunk.op == "equal" else 1.0)
    return {name: sum(v) / len(v) for name, v in totals.items() if v}
