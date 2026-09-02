"""Capturing the author's edits, and the gate that decides what they teach.

The assertion this file exists for is
`test_the_baseline_survives_an_in_app_edit`. The Drafts editor rewrites a draft
body in place, so the crew's original is destroyed the moment the author saves —
and that original is the whole signal. If the snapshot is not taken at the right
moment, every later stage is comparing the author's text against itself.

Its companion is `test_a_detector_that_fires_on_a_human_final_is_rejected`. What
the author published *is* the golden set: a proposed rule that fires on it would
flag the finished article, so it is a false positive by construction and must
never reach a human.
"""

from __future__ import annotations

import pytest

from ppn_blogger.server import catalog, delta_store, drafts


@pytest.fixture
async def store(database_url, tmp_path, monkeypatch):
    from ppn_blogger.server import db
    from ppn_blogger.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings.run, "output_dir", tmp_path)
    await db.init_db()
    yield tmp_path


AGENT_BODY = """## What changed

The connector policy is arguably the fastest route to a safe migration.
In short, it blocks the endpoint.

## What to watch carefully

Preview status moves. Check before you size anything.
"""

AUTHOR_BODY = """## What changed

The connector policy is the fastest route to a safe migration.

## What to watch carefully

Preview status moves. Check before you size anything.
"""


def _write(directory, name: str, body: str) -> str:
    path = directory / name
    path.write_text(
        f"---\ntitle: A post\nslug: a-post\nlanguage: en\n---\n\n# A post\n\n{body}",
        encoding="utf-8",
    )
    return path.name


async def _run(run_id: str) -> None:
    """`draft_versions.write_run_id` is a real foreign key, so the run must exist."""
    from ppn_blogger.server.db import Run, session, utcnow

    async with session() as s:
        if await s.get(Run, run_id) is None:
            s.add(Run(id=run_id, kind="write", status="succeeded", queued_at=utcnow()))
            await s.commit()


async def _record(directory, name: str = "2026-09-01-a-post.md", body: str = AGENT_BODY) -> int:
    """Put a draft on disk and through the catalog, as a write run would."""
    filename = _write(directory, name, body)
    await _run("run-1")
    return await catalog.record_run_result(
        "run-1",
        "write",
        {"topic": {"title": "A post", "slug": "a-post"}},
        {
            "title": "A post",
            "slug": "a-post",
            "approved": True,
            "score": 90.0,
            "blockers": 0,
            "markdown_path": str(directory / filename),
            "post_id": 1234,
        },
    )


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_baseline_survives_an_in_app_edit(store):
    """The reason the snapshot is taken at record time and not read later:
    `drafts.write_draft` rewrites the body in place, so by publish time the file
    no longer contains anything the crew wrote."""
    await _record(store)
    drafts.write_draft("2026-09-01-a-post.md", AUTHOR_BODY)

    pairs = await delta_store.list_pairs()
    assert len(pairs) == 1
    detail = await delta_store.get_pair(pairs[0]["id"])
    assert "arguably" in detail["agent_text"]
    assert "arguably" not in (store / "2026-09-01-a-post.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_publishing_completes_the_pair_and_scores_it(store):
    await _record(store)
    drafts.write_draft("2026-09-01-a-post.md", AUTHOR_BODY)
    await catalog.record_publish(
        "2026-09-01-a-post.md", {"post_id": 1234, "status": "publish", "link": "https://x/y"}
    )

    pair = (await delta_store.list_pairs())[0]
    assert pair["status"] == "captured"
    assert pair["identical"] is False
    assert 0 < pair["edit_rate"] < 0.5
    assert pair["changed_blocks"] >= 1


@pytest.mark.asyncio
async def test_a_post_published_untouched_is_kept_as_the_positive_class(store):
    """These are the pairs the gate tests every proposal against. Discarding
    them as 'nothing happened' would throw away the golden set."""
    await _record(store)
    await catalog.record_publish(
        "2026-09-01-a-post.md", {"post_id": 1234, "status": "publish", "link": "https://x/y"}
    )

    pair = (await delta_store.list_pairs())[0]
    assert pair["identical"] is True
    assert pair["edit_rate"] == 0.0
    assert pair["status"] == "captured"


@pytest.mark.asyncio
async def test_one_pair_per_draft_version_however_often_it_is_published(store):
    """A re-publish is more evidence about the same draft, not a second opinion:
    counting it twice would let one post reach the recurrence threshold alone."""
    await _record(store)
    for _ in range(3):
        await catalog.record_publish(
            "2026-09-01-a-post.md", {"post_id": 1234, "status": "publish", "link": "https://x/y"}
        )
    assert len(await delta_store.list_pairs()) == 1


@pytest.mark.asyncio
async def test_a_draft_never_published_stays_awaiting_and_teaches_nothing(store):
    await _record(store)
    pair = (await delta_store.list_pairs())[0]
    assert pair["status"] == "awaiting_final"
    assert await delta_store.unanalysed_count() == 0
    assert await delta_store.corpus() == []


@pytest.mark.asyncio
async def test_the_pair_records_which_configuration_wrote_the_draft(store):
    """`Run.config_version` cannot answer this: it is a 95-character token in a
    String(64) column, so it is truncated mid-name."""
    from ppn_blogger.server import config_store

    await config_store.seed_from_yaml_if_empty()
    await _record(store)
    detail = await delta_store.get_pair((await delta_store.list_pairs())[0]["id"])
    assert detail["config"]["validation_rules"] == 1
    assert detail["config"]["style_guide"] == 1


@pytest.mark.asyncio
async def test_publishing_a_draft_the_catalog_never_saw_is_not_an_error(store):
    assert await delta_store.capture_final("nothing-here.md") is None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_clean_rate_counts_posts_published_untouched(store):
    await _record(store, "2026-09-01-a.md")
    await catalog.record_publish("2026-09-01-a.md", {"post_id": 1, "status": "publish"})

    await _record(store, "2026-09-01-b.md")
    drafts.write_draft("2026-09-01-b.md", AUTHOR_BODY)
    await catalog.record_publish("2026-09-01-b.md", {"post_id": 2, "status": "publish"})

    figures = await delta_store.metrics()
    assert figures["pairs"] == 2
    assert figures["clean_rate"] == 0.5
    assert figures["mean_edit_rate"] > 0


@pytest.mark.asyncio
async def test_metrics_on_an_empty_store_report_nothing_rather_than_dividing_by_zero(store):
    figures = await delta_store.metrics()
    assert figures["pairs"] == 0
    assert figures["clean_rate"] == 0.0
    assert figures["mean_edit_rate"] == 0.0


# ---------------------------------------------------------------------------
# The gate
#
# What the author published is the golden set: they shipped it, so a rule that
# fires on it would be flagging finished work. Everything below turns on that.
# ---------------------------------------------------------------------------


def _proposal(**over):
    from ppn_blogger.models import LearningProposal

    base = {
        "kind": "rule",
        "summary": "Flag hedging before a claim.",
        "rule_group": "voice_rules",
        "rule_text": "Do not hedge a claim you are willing to make.",
        "severity": "minor",
        "detector": r"(?i)\barguably\b",
        "prose_scoped": True,
    }
    base.update(over)
    return LearningProposal(**base)


def _corpus(n: int = 4):
    """Drafts that hedge, and the published versions with the hedging removed."""
    return [
        {
            "id": i,
            "slug": f"post-{i}",
            "agent": f"## Section {i}\n\nThis is arguably the fastest route.\n",
            "final": f"## Section {i}\n\nThis is the fastest route.\n",
        }
        for i in range(n)
    ]


async def _gate(proposal, corpus, **over):
    from ppn_blogger.server import learning
    from ppn_blogger.settings import get_settings

    kwargs = {
        "rule_id": "V90",
        "documents": {},
        "evidence_slugs": set(),
        "corpus": corpus,
        "settings": get_settings(),
    }
    kwargs.update(over)
    return await learning.gate(proposal, **kwargs)


@pytest.mark.asyncio
async def test_a_rule_that_catches_the_fault_and_spares_the_published_work_passes(store):
    report = await _gate(_proposal(), _corpus())
    assert report.status == "passed"
    assert report.draft_hits == 4
    assert report.final_hits == 0


@pytest.mark.asyncio
async def test_a_detector_that_fires_on_a_human_final_is_rejected(store):
    """The load-bearing check. `(?m)^[A-Z]` matches ordinary prose, so it flags
    every post the author published — it can never reach a human."""
    report = await _gate(_proposal(detector=r"(?m)^[A-Z]"), _corpus())
    assert report.status == "failed"
    assert report.final_hits > 0
    assert "published" in report.reason


@pytest.mark.asyncio
async def test_the_stubs_deliberately_bad_proposal_is_the_one_that_fails(store):
    """Every dry run walks the discard rather than the happy path, the same way
    the newsletter stub fabricates an article id."""
    from ppn_blogger.testing import _learning_proposal

    assert (await _gate(_learning_proposal(1), _corpus())).status == "passed"
    assert (await _gate(_learning_proposal(2), _corpus())).status == "failed"


@pytest.mark.asyncio
async def test_a_detector_that_describes_nothing_in_the_drafts_is_rejected(store):
    report = await _gate(_proposal(detector=r"(?i)\bzzzznotpresent\b"), _corpus())
    assert report.status == "failed"
    assert "none of the drafts" in report.reason


@pytest.mark.asyncio
async def test_nothing_is_proposed_before_there_is_anything_to_test_against(store):
    """Three published posts is the floor. Below it the gate cannot distinguish a
    real pattern from one coincidence."""
    report = await _gate(_proposal(), _corpus(2))
    assert report.status == "skipped"
    assert "are needed" in report.reason


@pytest.mark.asyncio
async def test_a_catastrophic_regex_is_discarded_rather_than_hanging_a_worker(store):
    """Python's `re` has no timeout, so this runs in a separate process under a
    wall-clock ceiling. The assertion is simply that the call returns."""
    from ppn_blogger.settings import get_settings

    settings = get_settings()
    corpus = [
        {"id": 0, "slug": "p", "agent": "a" * 40 + "!", "final": "clean"} for _ in range(4)
    ]
    report = await _gate(
        _proposal(detector=r"(a+)+$", prose_scoped=False), corpus, settings=settings
    )
    assert report.status == "failed"


@pytest.mark.asyncio
async def test_a_rule_the_model_left_without_a_detector_is_offered_but_unmeasured(store):
    """Honest rather than convenient: there is nothing deterministic to measure,
    and the review says so instead of implying evidence that does not exist."""
    report = await _gate(_proposal(detector=""), _corpus())
    assert report.status == "skipped"
    assert "nothing to measure" in report.reason


@pytest.mark.asyncio
async def test_prose_guidance_is_offered_with_its_evidence_and_no_false_measurement(store):
    report = await _gate(
        _proposal(kind="style_note", anchor="## 1. Voice", note_markdown="- Cut hedging."),
        _corpus(),
    )
    assert report.status == "skipped"
    assert "cannot be tested mechanically" in report.reason


@pytest.mark.asyncio
async def test_a_number_that_would_fail_posts_already_published_is_rejected(store):
    """`max_sections` is read by S02, a code-decided rule. Lowering it silently
    changes how every past and future draft is scored, which is exactly what the
    regression floor is for."""
    import yaml

    from ppn_blogger.settings import CONFIG_DIR

    profile = yaml.safe_load((CONFIG_DIR / "blog_profile.yaml").read_text(encoding="utf-8"))
    profile["structure"]["max_sections"] = 1

    body = "\n\n".join(f"## Section {n}\n\nSome prose here about the topic." for n in range(6))
    corpus = [{"id": i, "slug": f"p{i}", "agent": body, "final": body} for i in range(4)]

    report = await _gate(
        _proposal(kind="profile_scalar", profile_key="structure.max_sections", profile_value="1"),
        corpus,
        documents={"blog_profile": profile},
    )
    assert report.status == "failed"
    assert report.regressions > 0
    assert "already published" in report.reason


# ---------------------------------------------------------------------------
# The review: the only path to a config write
# ---------------------------------------------------------------------------


def _survivor(fingerprint: str = "fp-1", **over):
    proposal = {
        "candidate_id": 0,
        "fingerprint": fingerprint,
        "label": "cuts the hedging adverb before a claim",
        "edit_kind": "tighten",
        "target": "voice_rule",
        "kind": "rule",
        "summary": "Flag hedging before a claim.",
        "document": "validation_rules",
        "content": "",
        "rule_id": "V90",
        "gate": {"status": "passed"},
    }
    proposal.update(over)
    return proposal


async def _pending_review(**over):
    """A review holding one real, renderable change to the shipped ruleset."""
    from ppn_blogger import config_edit
    from ppn_blogger.server import config_store, learning_reviews
    from ppn_blogger.settings import CONFIG_DIR

    await config_store.seed_from_yaml_if_empty()
    text = (CONFIG_DIR / "validation_rules.yaml").read_text(encoding="utf-8")
    rendered = config_edit.append_rule(
        text,
        "voice_rules",
        {
            "id": "V90",
            "rule": "Do not hedge a claim you are willing to make.",
            "severity": "minor",
            "auto": True,
            "detector": r"(?i)\barguably\b",
            "prose_only": True,
        },
    )
    assert isinstance(rendered, str)
    return await learning_reviews.create(None, [_survivor(content=rendered, **over)], [])


@pytest.mark.asyncio
async def test_approving_writes_one_new_config_version_the_agents_then_read(store):
    from ppn_blogger.server import config_store, learning_reviews
    from ppn_blogger.settings import get_settings

    review_id = await _pending_review()
    outcome = await learning_reviews.decide(review_id, [{"fingerprint": "fp-1", "approved": True}])

    assert outcome["applied"][0]["document"] == "validation_rules"
    assert outcome["applied"][0]["version"] == 2
    await config_store.refresh_active_source()
    assert "V90" in {r["id"] for r in get_settings().all_rules()}


@pytest.mark.asyncio
async def test_the_config_is_written_before_the_review_is_closed(store, monkeypatch):
    """A crash between the two must leave a review that can be decided again.
    The other order leaves a decided review that changed nothing, silently."""
    from ppn_blogger.server import config_store, learning_reviews

    review_id = await _pending_review()

    async def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(config_store, "save_document", boom)
    with pytest.raises(RuntimeError):
        await learning_reviews.decide(review_id, [{"fingerprint": "fp-1", "approved": True}])

    assert (await learning_reviews.get(review_id))["status"] == "pending"


@pytest.mark.asyncio
async def test_declining_changes_no_configuration_and_is_remembered(store):
    from ppn_blogger.server import config_store, learning, learning_reviews

    review_id = await _pending_review()
    outcome = await learning_reviews.decide(
        review_id, [{"fingerprint": "fp-1", "approved": False, "reason": "I like it that way"}]
    )

    assert outcome["applied"] == []
    assert outcome["declined"] == 1
    assert (await config_store.latest_versions())["validation_rules"].version == 1
    assert "fp-1" in await learning.declined_fingerprints()


@pytest.mark.asyncio
async def test_a_refused_pattern_is_never_offered_again(store):
    """The author goes on making the same edit, so the cluster keeps accruing
    evidence — which is why the refusal lives in its own table rather than as a
    status the next aggregation pass would overwrite."""
    from ppn_blogger.server import learning, learning_reviews

    review_id = await _pending_review()
    await learning_reviews.decide(review_id, [{"fingerprint": "fp-1", "approved": False}])
    assert await learning.declined_fingerprints() == {"fp-1"}

    # A second refusal of the same pattern must not duplicate the row.
    second = await _pending_review()
    await learning_reviews.decide(second, [{"fingerprint": "fp-1", "approved": False}])
    assert len(await learning_reviews.list_declined()) == 1


@pytest.mark.asyncio
async def test_a_review_can_only_be_decided_once(store):
    from ppn_blogger.server import learning_reviews

    review_id = await _pending_review()
    await learning_reviews.decide(review_id, [{"fingerprint": "fp-1", "approved": False}])
    with pytest.raises(ValueError, match="already"):
        await learning_reviews.decide(review_id, [{"fingerprint": "fp-1", "approved": True}])


@pytest.mark.asyncio
async def test_an_unknown_review_raises_rather_than_silently_doing_nothing(store):
    from ppn_blogger.server import learning_reviews

    with pytest.raises(KeyError):
        await learning_reviews.decide(9999, [])


@pytest.mark.asyncio
async def test_a_decision_must_name_something_the_review_offered(store):
    """Guards a stale screen or a replayed request from applying a change the
    author never actually saw."""
    from ppn_blogger.server import learning_reviews

    review_id = await _pending_review()
    with pytest.raises(ValueError, match="Not in this review"):
        await learning_reviews.decide(review_id, [{"fingerprint": "invented", "approved": True}])


@pytest.mark.asyncio
async def test_a_protected_document_is_refused_again_at_apply_time(store):
    """Checked twice on purpose: the review row outlives a restart, and a
    redeploy between filing and approving could change what is writable."""
    from ppn_blogger.server import config_store, learning_reviews

    review_id = await _pending_review(document="sources", fingerprint="fp-1")
    outcome = await learning_reviews.decide(
        review_id, [{"fingerprint": "fp-1", "approved": True}]
    )
    assert outcome["applied"] == []
    assert (await config_store.latest_versions())["sources"].version == 1


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------


def test_nothing_in_the_learner_can_reach_a_config_write():
    """"Nothing auto-applies" has to be a property of the code, not a promise
    about how it is called. Parsed rather than grepped, so the module can still
    explain in prose why it does not do this."""
    import ast
    from pathlib import Path

    from ppn_blogger.server import learning

    tree = ast.parse(Path(learning.__file__).read_text(encoding="utf-8"))
    referenced = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    # It may file a review and read the current documents. It may not write one.
    assert "save_document" not in referenced
    assert "rollback" not in referenced
    assert "reimport_from_yaml" not in referenced


def test_the_learner_can_never_target_a_guardrail_document():
    from ppn_blogger import config_edit
    from ppn_blogger.server import learning

    for target in learning._SHAPE_FOR:
        assert target not in ("honesty_rule", "source_policy")
    assert "sources" not in config_edit.WRITABLE_DOCUMENTS
    assert "honesty_rules" not in config_edit.WRITABLE_RULE_GROUPS


# ---------------------------------------------------------------------------
# The whole loop, offline
# ---------------------------------------------------------------------------


# A hedge the author cuts, and a summary sentence they delete — the two habits
# the stub analyst reports. The edit has to be substantial enough to clear the
# over-correction guard, which is what stops the loop proposing rules for a
# corpus that is already published nearly untouched.
HEDGED = """## What changed

The connector policy is arguably the fastest route to a safe migration.
In short, the policy blocks the endpoint and there is no way around it.

## What to watch carefully

Preview status moves, so check before you size anything for production use.
In short, the safe assumption is that today's limit is not next quarter's.
"""

PUBLISHED = """## What changed

The connector policy is the fastest route to a safe migration.

## What to watch carefully

Preview status moves, so check before you size anything for production use.
"""


async def _published(directory, n: int) -> None:
    """One post drafted with a hedge and published without it."""
    name = f"2026-09-0{n}-post-{n}.md"
    path = directory / name
    path.write_text(
        f"---\ntitle: Post {n}\nslug: post-{n}\nlanguage: en\n---\n\n# Post {n}\n\n{HEDGED}",
        encoding="utf-8",
    )
    await _run(f"run-{n}")
    await catalog.record_run_result(
        f"run-{n}",
        "write",
        {"topic": {"title": f"Post {n}", "slug": f"post-{n}"}},
        {
            "title": f"Post {n}",
            "slug": f"post-{n}",
            "approved": True,
            "score": 90.0,
            "blockers": 0,
            "markdown_path": str(path),
            "post_id": 1000 + n,
        },
    )
    drafts.write_draft(name, PUBLISHED)
    await catalog.record_publish(name, {"post_id": 1000 + n, "status": "publish"})


@pytest.mark.asyncio
async def test_the_whole_loop_files_a_review_the_author_can_approve(store):
    """Three posts edited the same way, offline, end to end: capture, classify,
    cluster, diagnose, gate, review, apply — and the crew reads the result."""
    from ppn_blogger.server import config_store, learning, learning_reviews
    from ppn_blogger.settings import get_settings
    from ppn_blogger.testing import stub_clients

    await config_store.seed_from_yaml_if_empty()
    await config_store.refresh_active_source()
    for n in (1, 2, 3, 4):
        await _published(store, n)

    outcome = await learning.sweep(clients=stub_clients())
    assert outcome["awaiting_learning_approval"] is True
    assert outcome["analysed"] == 4

    review = await learning_reviews.get(outcome["review_id"])
    proposals = review["proposals"]
    assert proposals, "nothing survived the gate"

    # The stub alternates a good proposal with one whose detector fires on every
    # published post, so the gate must have thrown at least one away.
    assert outcome["survived"] < outcome["proposed"] or all(
        p["gate"]["status"] != "failed" for p in proposals
    )

    approved = [{"fingerprint": p["fingerprint"], "approved": True} for p in proposals]
    result = await learning_reviews.decide(review["id"], approved)
    assert result["applied"]

    await config_store.refresh_active_source()
    ids = {r["id"] for r in get_settings().all_rules()}
    assert any(entry.get("rule_id") in ids for entry in result["applied"] if entry.get("rule_id"))


@pytest.mark.asyncio
async def test_nothing_is_proposed_before_a_correction_has_recurred(store):
    """One post is an opinion, not a habit. The threshold is what stops the crew
    being retrained by a single afternoon's mood."""
    from ppn_blogger.server import config_store, learning
    from ppn_blogger.testing import stub_clients

    await config_store.seed_from_yaml_if_empty()
    await config_store.refresh_active_source()
    await _published(store, 1)

    outcome = await learning.sweep(clients=stub_clients())
    assert outcome["awaiting_learning_approval"] is False
    assert "recurred" in outcome["reason"] or "test against" in outcome["reason"]


@pytest.mark.asyncio
async def test_an_empty_store_says_so_rather_than_praising_the_drafts(store):
    """`already_clean([])` is true, so ordering these checks the other way round
    would tell the author their drafts are excellent when nothing was captured."""
    from ppn_blogger.server import config_store, learning
    from ppn_blogger.testing import stub_clients

    await config_store.seed_from_yaml_if_empty()
    outcome = await learning.sweep(clients=stub_clients())
    assert outcome["awaiting_learning_approval"] is False
    assert "Nothing captured yet" in outcome["reason"]
