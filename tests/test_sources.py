"""The source review's pure half: harvest, filter, and file the verdict.

No agents, no I/O. These are the functions the approval screen and the CLI both
depend on being exactly right — a bug here either hides a site from the operator
or writes a trust tier nobody asked for.
"""

from __future__ import annotations

import pytest
import yaml

from ppn_blogger.models import ScoutReport, SignalItem, SourceDecision
from ppn_blogger.settings import CONFIG_DIR
from ppn_blogger.sources import (
    apply_decisions,
    filter_reports,
    harvest_candidates,
    merge_into_yaml_text,
)


def _item(url: str, title: str = "t", name: str | None = None) -> SignalItem:
    return SignalItem(
        title=title, url=url, source_name=name, why_it_matters="because", watch_area="dataverse"
    )


def _reports() -> list[ScoutReport]:
    return [
        ScoutReport(
            scout="news_scout",
            items=[
                _item("https://learn.microsoft.com/a", name="Microsoft Learn"),
                _item("https://newblog.example/post-1", name="New Blog"),
                _item("https://newblog.example/post-2", name="New Blog"),
            ],
        ),
        ScoutReport(
            scout="feed_scout",
            items=[_item("https://www.matthewdevaney.com/x", name="Matthew Devaney")],
        ),
    ]


def test_harvest_groups_by_site_and_puts_new_ones_first():
    candidates = harvest_candidates(_reports())
    assert [c.domain for c in candidates] == [
        "newblog.example",  # new, and the largest contributor
        "learn.microsoft.com",
        "matthewdevaney.com",
    ]

    new = candidates[0]
    assert new.known is False
    assert new.current_tier == "unknown"
    assert new.suggested_tier == "community_unverified"
    assert new.item_count == 2
    assert new.scouts == ["news_scout"]
    assert new.name == "New Blog"

    known = candidates[1]
    assert known.known is True
    # A site the config already trusts is offered at the tier it already has —
    # approving it must never quietly demote it.
    assert known.current_tier == known.suggested_tier == "official"

    # www. is not a different site.
    assert candidates[2].domain == "matthewdevaney.com"


def test_harvest_counts_a_page_two_scouts_found_only_once():
    reports = [
        ScoutReport(scout="news_scout", items=[_item("https://learn.microsoft.com/a")]),
        ScoutReport(scout="docs_scout", items=[_item("https://learn.microsoft.com/a")]),
    ]
    [candidate] = harvest_candidates(reports)
    assert candidate.item_count == 1
    assert [i.url for i in candidate.items] == ["https://learn.microsoft.com/a"]
    # ...but both scouts are still credited with reaching it.
    assert candidate.scouts == ["docs_scout", "news_scout"]


def test_harvest_never_offers_a_declined_site():
    candidates = harvest_candidates(_reports(), declined=["NewBlog.Example"])
    assert "newblog.example" not in {c.domain for c in candidates}


def test_filter_reports_removes_everything_unapproved():
    filtered = filter_reports(_reports(), ["learn.microsoft.com"])
    urls = [i.url for r in filtered for i in r.items]
    assert urls == ["https://learn.microsoft.com/a"]
    # The reports themselves survive, so the editor still sees who found nothing.
    assert [r.scout for r in filtered] == ["news_scout", "feed_scout"]


def test_apply_decisions_adds_moves_and_declines():
    sources = {
        "trust_tiers": {
            "official": {"score": 5, "domains": ["learn.microsoft.com"]},
            "community_trusted": {"score": 4, "domains": ["matthewdevaney.com"]},
            "community_unverified": {"score": 2, "domains": ["reddit.com"]},
        }
    }
    result = apply_decisions(
        sources,
        [
            SourceDecision(domain="newblog.example", approved=True, tier="community_trusted"),
            SourceDecision(domain="reddit.com", approved=True, tier="community_trusted"),
            SourceDecision(domain="spam.example", approved=False),
            SourceDecision(domain="matthewdevaney.com", approved=False),
        ],
    )
    tiers = result["trust_tiers"]
    assert "newblog.example" in tiers["community_trusted"]["domains"]
    # Promoting moves a domain rather than duplicating it.
    assert "reddit.com" in tiers["community_trusted"]["domains"]
    assert "reddit.com" not in tiers["community_unverified"]["domains"]
    # Turning down an unknown site records it; turning down an already-trusted
    # one is a "not this time", not a demotion.
    assert result["declined_domains"] == ["spam.example"]
    assert "matthewdevaney.com" in tiers["community_trusted"]["domains"]

    # The input is never mutated — the caller still has the old config to diff.
    assert sources["trust_tiers"]["community_unverified"]["domains"] == ["reddit.com"]


def test_apply_decisions_rejects_an_unknown_tier():
    with pytest.raises(ValueError, match="Unknown trust tier"):
        apply_decisions(
            {"trust_tiers": {"official": {"domains": []}}},
            [SourceDecision(domain="x.example", approved=True, tier="made_up")],
        )


def test_approving_a_declined_site_puts_it_back_in_circulation():
    sources = {
        "trust_tiers": {"community_trusted": {"domains": []}},
        "declined_domains": ["second-thoughts.example"],
    }
    result = apply_decisions(
        sources,
        [SourceDecision(domain="second-thoughts.example", approved=True, tier="community_trusted")],
    )
    assert result["declined_domains"] == []
    assert result["trust_tiers"]["community_trusted"]["domains"] == ["second-thoughts.example"]


def test_merge_keeps_the_comments_in_the_real_config_file():
    text = (CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8")
    merged = merge_into_yaml_text(
        text,
        [
            SourceDecision(domain="newblog.example", approved=True, tier="community_trusted"),
            SourceDecision(domain="spam.example", approved=False),
        ],
    )
    # Half of sources.yaml is explanation. A round-trip through yaml.safe_dump
    # would throw all of it away and leave an unreadable document in the editor.
    assert "# Trust tiers used by the Source Checker" in merged
    assert "# Never cite these" in merged
    assert merged.count("- benediktbergmann.eu") == 1

    parsed = yaml.safe_load(merged)
    assert "newblog.example" in parsed["trust_tiers"]["community_trusted"]["domains"]
    assert parsed["declined_domains"] == ["spam.example"]
    # ...and it still says exactly what apply_decisions says it should.
    assert parsed == apply_decisions(
        yaml.safe_load(text),
        [
            SourceDecision(domain="newblog.example", approved=True, tier="community_trusted"),
            SourceDecision(domain="spam.example", approved=False),
        ],
    )


def test_merge_falls_back_to_a_dump_when_the_text_cannot_be_edited():
    # A flow-style mapping has no line to insert into; correctness wins over
    # formatting, so the merge re-serialises rather than guessing.
    text = "trust_tiers: {official: {score: 5, domains: [learn.microsoft.com]}}\n"
    merged = merge_into_yaml_text(
        text, [SourceDecision(domain="x.example", approved=True, tier="official")]
    )
    parsed = yaml.safe_load(merged)
    assert parsed["trust_tiers"]["official"]["domains"] == ["learn.microsoft.com", "x.example"]
