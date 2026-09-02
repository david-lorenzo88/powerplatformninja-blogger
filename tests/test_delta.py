"""The delta learner's pure half: normalise, diff, score and cluster.

No agents, no I/O, no database. Everything the learning loop later decides rests
on these functions being exactly right — a bug here either invents an edit the
author never made, or hides one they did.
"""

from __future__ import annotations

from ppn_blogger import delta
from ppn_blogger.models import EditObservation


def _post(body: str) -> str:
    return f"---\ntitle: A post\nslug: a-post\n---\n\n# A post\n\n{body}\n"


def _obs(signature: str, kind: str = "reword_phrase", target: str = "voice_rule") -> EditObservation:
    return EditObservation(
        edit_kind=kind, target=target, signature=signature, before="b", after="a", confidence=4
    )


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_front_matter_and_the_h1_never_reach_the_diff():
    """Neither is pushed to WordPress and neither is edited as prose. Leaving
    them in reports a deletion of both on every single pair."""
    text = normalised = delta.normalise(_post("## S\n\nBody."))
    assert "title: A post" not in text
    assert "# A post" not in normalised
    assert normalised.startswith("## S")


def test_the_same_prose_in_different_encodings_is_not_an_edit():
    before = _post("## S\n\nThe author said “no” — firmly.")
    after = _post('## S\n\nThe author said "no" - firmly.')
    assert delta.score(before, after).identical is True


def test_reflowing_a_list_is_not_a_rewrite_of_every_item():
    before = _post("## S\n\n* one\n* two\n* three")
    after = _post("## S\n\n- one\n- two\n- three")
    assert delta.score(before, after).identical is True


def test_normalisation_is_idempotent():
    once = delta.normalise(_post("## S\n\nSome  text.  \n\n\n\nMore."))
    assert delta.normalise(once) == once


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_a_post_published_untouched_scores_zero():
    """The positive class. These pairs are the golden set the gate runs against,
    so they have to be recognised as unedited."""
    text = _post("## S\n\nA sentence that shipped exactly as written.")
    result = delta.score(text, text)
    assert result.identical is True
    assert result.edit_rate == 0.0
    assert result.changed_blocks == 0


def test_a_reworded_paragraph_is_one_replace_hunk():
    before = _post("## S\n\nThe agent wrote this.\n\nUntouched paragraph.")
    after = _post("## S\n\nThe author rewrote this.\n\nUntouched paragraph.")
    changed = [h for h in delta.score(before, after).hunks if h.op != "equal"]
    assert len(changed) == 1
    assert changed[0].op == "replace"
    assert changed[0].section == "S"


def test_a_deleted_paragraph_is_a_delete_not_a_replace():
    before = _post("## S\n\nKeep this.\n\nCut this.")
    after = _post("## S\n\nKeep this.")
    ops = [h.op for h in delta.score(before, after).hunks if h.op != "equal"]
    assert ops == ["delete"]


def test_a_fenced_code_block_is_one_block_however_many_blank_lines_it_holds():
    fence = "```python\na = 1\n\nb = 2\n```"
    assert delta.split_blocks(fence) == [fence]


def test_the_edit_rate_is_word_level_not_character_level():
    """A one-word change in a long paragraph must be a small number. Character
    distance would report it as large and drown the signal."""
    before = _post("## S\n\n" + " ".join(["word"] * 99) + " alpha")
    after = _post("## S\n\n" + " ".join(["word"] * 99) + " beta")
    assert delta.score(before, after).edit_rate < 0.05


def test_rewriting_a_post_wholesale_scores_near_one():
    before = _post("## S\n\nThe quick brown fox jumped over the lazy dog.")
    after = _post("## S\n\nEntirely different prose about unrelated matters here.")
    assert delta.score(before, after).edit_rate > 0.8


def test_overlap_separates_a_rephrasing_from_a_rewrite():
    """The pair of numbers is what routes an edit in code, before any model sees
    it: rephrased keeps the vocabulary, rewritten does not."""
    original = _post("## S\n\nThe connector policy blocks the Dataverse endpoint at runtime.")
    rephrased = _post("## S\n\nAt runtime the connector policy blocks the Dataverse endpoint.")
    rewritten = _post("## S\n\nLicensing changed again for seeded capacity in March.")
    assert delta.score(original, rephrased).overlap > 0.9
    assert delta.score(original, rewritten).overlap < 0.2


# ---------------------------------------------------------------------------
# The section diff
# ---------------------------------------------------------------------------


def test_a_renamed_heading_is_one_row_not_a_delete_beside_an_insert():
    before = _post("## First\n\nBody.\n\n## Second\n\nBody.")
    after = _post("## First\n\nBody.\n\n## Renamed\n\nBody.")
    changes = [s for s in delta.score(before, after).sections if s.op != "equal"]
    assert len(changes) == 1
    assert (changes[0].op, changes[0].before, changes[0].after) == ("replace", "Second", "Renamed")


def test_a_deleted_section_is_visible_as_structure():
    before = _post("## First\n\nBody.\n\n## Second\n\nBody.\n\n## Third\n\nBody.")
    after = _post("## First\n\nBody.\n\n## Third\n\nBody.")
    changes = [s for s in delta.score(before, after).sections if s.op != "equal"]
    assert [(c.op, c.before) for c in changes] == [("delete", "Second")]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def test_a_fingerprint_is_stable_across_casing_word_order_filler_and_tense():
    """Two analysts describing the same recurring edit will not choose the same
    words, order or tense. If those do not collide, nothing ever recurs and the
    threshold never fires."""
    a = delta.fingerprint("tighten", "voice_rule", "en", "Removes the hedging adverb")
    b = delta.fingerprint("tighten", "voice_rule", "en", "hedging  ADVERB removed")
    assert a == b


def test_stemming_leaves_short_words_alone():
    """Aggressive stemming collides unrelated signatures, which is worse than
    missing one: it invents recurrence that never happened."""
    assert delta._stem("is") == "is"
    assert delta._stem("less") == "less"
    assert delta._stem("hedging") == "hedg"


def test_a_lesson_learned_on_spanish_is_never_applied_to_english():
    en = delta.fingerprint("tighten", "voice_rule", "en", "removes hedging adverb")
    es = delta.fingerprint("tighten", "voice_rule", "es", "removes hedging adverb")
    assert en != es


def test_two_edit_kinds_never_share_a_fingerprint():
    a = delta.fingerprint("tighten", "voice_rule", "en", "same signature")
    b = delta.fingerprint("expand", "voice_rule", "en", "same signature")
    assert a != b


def test_recurrence_counts_distinct_posts_not_observations():
    """One post edited the same way four times is one opinion. Regenerating a
    draft must not manufacture a pattern."""
    same_post = [(7, _obs("removes hedging adverb")) for _ in range(4)]
    clusters = delta.cluster(same_post)
    assert clusters[0].occurrences == 4
    assert clusters[0].distinct_posts == 1
    assert delta.recurring(clusters, min_distinct_posts=3) == []


def test_three_distinct_posts_earn_a_proposal():
    observations = [(post, _obs("removes hedging adverb")) for post in (1, 2, 3)]
    clusters = delta.cluster(observations)
    assert delta.recurring(clusters, min_distinct_posts=3) == clusters


def test_clusters_come_back_commonest_first():
    observations = [
        (1, _obs("rare shape")),
        (2, _obs("common shape")),
        (3, _obs("common shape")),
        (4, _obs("common shape")),
    ]
    assert delta.cluster(observations)[0].label == "common shape"


# ---------------------------------------------------------------------------
# The over-correction guard
# ---------------------------------------------------------------------------


def test_a_corpus_that_is_already_clean_proposes_nothing():
    """Once the crew's output is good, further rules fire on text that was
    already right. This is the lesson automatic post-editing learned expensively."""
    assert delta.already_clean([0.01, 0.02, 0.0], threshold=0.05) is True
    assert delta.already_clean([0.30, 0.25, 0.40], threshold=0.05) is False


def test_an_empty_corpus_is_treated_as_clean_rather_than_as_evidence():
    assert delta.already_clean([], threshold=0.05) is True


def test_unparseable_front_matter_never_leaks_into_the_diff():
    """`split_front_matter` returns the whole document when the block does not
    parse, and a title containing a colon is enough to do that. The H1 would then
    survive on one side and not the other, and "the author deleted the title"
    would be offered as an editorial habit."""
    raw = (
        "---\ntitle: Elastic tables: what you give up\nslug: a\n---\n\n"
        "# Elastic tables: what you give up\n\n## S\n\nBody.\n"
    )
    normalised = delta.normalise(raw)
    assert normalised == "## S\n\nBody."
    assert "title:" not in normalised
    assert not normalised.startswith("#" + " ")


# ---------------------------------------------------------------------------
# Prose scoping for rules that did not exist when detectors.py was written
# ---------------------------------------------------------------------------


def test_a_learned_rule_can_declare_its_own_prose_scoping():
    """`_PROSE_SCOPED` is a hardcoded set of the ids shipped with v2. A rule
    allocated later — by hand or by the learner — is in neither list, so without
    a declared scope its detector runs against raw markdown and fires inside code
    fences, inline spans and URLs: exactly the T01/T02 false positives the
    masking layer exists to prevent."""
    from ppn_blogger.detectors import _is_prose_scoped

    assert _is_prose_scoped({"id": "V90", "prose_only": True}) is True
    assert _is_prose_scoped({"id": "V90", "prose_only": False}) is False
    # Undeclared and unknown: the old behaviour, which is raw markdown.
    assert _is_prose_scoped({"id": "V90"}) is False
    # The shipped rules are unaffected.
    assert _is_prose_scoped({"id": "T01"}) is True
    assert _is_prose_scoped({"id": "S02"}) is False


def test_a_learned_prose_scoped_rule_does_not_fire_inside_a_code_fence():
    """The end-to-end version of the check above, through `run_detectors`."""
    from types import SimpleNamespace

    from ppn_blogger import detectors
    from ppn_blogger.settings import get_settings

    settings = get_settings()
    learned = {
        "id": "V90",
        "group": "voice",
        "rule": "No hedging.",
        "severity": "minor",
        "auto": True,
        "detector": r"(?i)\barguably\b",
        "prose_only": True,
    }
    scoped = SimpleNamespace(
        all_rules=lambda: [learned],
        structure=settings.structure,
        blog_profile=settings.blog_profile,
        validation=settings.validation,
        word_target=settings.word_target,
        RULE_GROUPS=settings.RULE_GROUPS,
    )
    inside_a_fence = "## S\n\n```python\n# arguably the fastest\nx = 1\n```\n"
    in_the_prose = "## S\n\nThis is arguably the fastest route.\n"

    assert not detectors.run_detectors(inside_a_fence, groups=("voice",), settings=scoped).findings
    assert detectors.run_detectors(in_the_prose, groups=("voice",), settings=scoped).findings
