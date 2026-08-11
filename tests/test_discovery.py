"""Feed discovery, and the gate that makes its output trustworthy.

The assertion this file exists for is
`test_a_suggestion_that_is_not_a_feed_never_reaches_the_operator`. A model
suggests places to look; every URL it names is fetched and parsed *before* the
review is written. So approving a feed cannot mean adding a URL nobody checked —
which matters, because an approved feed goes into a poller that will call it
every six hours forever.

This is `sources.py`'s rule carried over: the review is code, never judgement.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ppn_blogger import news
from ppn_blogger.server import discovery


@pytest.fixture
async def store(database_url):
    from ppn_blogger.server import db

    await db.init_db()
    yield


class _Suggestion:
    """What the scout returns — a guess, with no guarantee behind it."""

    def __init__(self, url: str, name: str = "A source", topics=None, reason: str = "because"):
        self.url = url
        self.name = name
        self.topics = topics or ["ai"]
        self.reason = reason


def _feed(entries: int = 3, title: str = "Example Blog") -> news.FeedFetch:
    return news.FeedFetch(
        status=200,
        title=title,
        site_url="https://example.com/",
        entries=[
            news.FetchedEntry(
                title=f"Post {i}",
                url=f"https://example.com/{i}",
                published=datetime(2026, 8, 9, tzinfo=UTC),
                entry_key=news.url_hash(f"https://example.com/{i}"),
            )
            for i in range(entries)
        ],
    )


def _probes(monkeypatch, table: dict[str, news.FeedFetch], discovered=None):
    """Map URL -> what probing it returns. Anything else is an empty page."""

    async def probe(url, **kw):
        return table.get(url, news.FeedFetch(status=200, entries=[]))

    async def discover_feeds(url, **kw):
        return (discovered or {}).get(url, [])

    monkeypatch.setattr(news, "probe", probe)
    monkeypatch.setattr(news, "discover_feeds", discover_feeds)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


async def test_a_suggestion_that_is_not_a_feed_never_reaches_the_operator(
    store, monkeypatch
) -> None:
    """The one that matters. A plausible URL with nothing behind it is discarded."""
    _probes(monkeypatch, {"https://real.example/feed": _feed()})

    candidates = await discovery._verify(
        [
            _Suggestion("https://real.example/feed", name="Real"),
            _Suggestion("https://invented.example/feed", name="Invented"),
            _Suggestion("https://also-not-there.example/rss", name="Also not there"),
        ],
        known=set(),
        declined=set(),
    )

    assert [c["url"] for c in candidates] == ["https://real.example/feed"]
    # And what survives carries evidence, not the model's say-so.
    assert candidates[0]["entry_count"] == 3
    assert candidates[0]["sample_titles"]


async def test_a_site_url_is_resolved_to_the_feed_behind_it(store, monkeypatch) -> None:
    """The scout may name a home page; the same discovery the paste box uses applies."""
    _probes(
        monkeypatch,
        {"https://example.com/feed.xml": _feed()},
        discovered={"https://example.com": ["https://example.com/feed.xml"]},
    )

    candidates = await discovery._verify(
        [_Suggestion("https://example.com", name="Example")], known=set(), declined=set()
    )
    assert [c["url"] for c in candidates] == ["https://example.com/feed.xml"]
    assert candidates[0]["suggested_from"] == "https://example.com"


async def test_an_empty_feed_is_not_offered(store, monkeypatch) -> None:
    """A feed with nothing in it looks broken the moment it is added."""
    _probes(monkeypatch, {"https://quiet.example/feed": news.FeedFetch(status=200, entries=[])})
    candidates = await discovery._verify(
        [_Suggestion("https://quiet.example/feed")], known=set(), declined=set()
    )
    assert candidates == []


async def test_a_feed_we_already_follow_is_not_offered(store, monkeypatch) -> None:
    from ppn_blogger.server import news_store

    await news_store.create_feed("https://example.com/feed", name="Already here")
    _probes(monkeypatch, {"https://example.com/feed": _feed()})

    known, declined = await discovery._already_seen()
    candidates = await discovery._verify(
        [_Suggestion("https://example.com/feed")], known=known, declined=declined
    )
    assert candidates == []


async def test_the_same_feed_suggested_twice_is_offered_once(store, monkeypatch) -> None:
    _probes(monkeypatch, {"https://example.com/feed": _feed()})
    candidates = await discovery._verify(
        [
            _Suggestion("https://example.com/feed"),
            # Same feed, different spelling — canonicalisation catches it.
            _Suggestion("https://www.example.com/feed/?utm_source=x"),
        ],
        known=set(),
        declined=set(),
    )
    assert len(candidates) == 1


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


async def _review(monkeypatch, urls=("https://a.example/feed", "https://b.example/feed")) -> int:
    _probes(monkeypatch, {u: _feed() for u in urls})
    candidates = await discovery._verify(
        [_Suggestion(u, name=u) for u in urls], known=set(), declined=set()
    )
    return await discovery.create(None, "find me sources", candidates)


async def test_approving_creates_the_feeds(store, monkeypatch) -> None:
    from ppn_blogger.server import news_store

    review_id = await _review(monkeypatch)
    review = await discovery.get(review_id)
    assert review is not None and review["candidate_count"] == 2

    result = await discovery.decide(
        review_id,
        [
            {"url": "https://a.example/feed", "approved": True, "name": "A", "realtime": False},
            {"url": "https://b.example/feed", "approved": False},
        ],
    )

    assert result["added"] == 1 and result["declined"] == 1
    feeds = await news_store.list_feeds()
    assert [f["url"] for f in feeds] == ["https://a.example/feed"]
    assert feeds[0]["origin"] == "discovered"


async def test_a_refused_feed_is_never_offered_again(store, monkeypatch) -> None:
    """The point of remembering a refusal: the next sweep must not re-ask."""
    review_id = await _review(monkeypatch, urls=("https://nope.example/feed",))
    await discovery.decide(
        review_id, [{"url": "https://nope.example/feed", "approved": False}]
    )

    assert len(await discovery.list_declined()) == 1

    known, declined = await discovery._already_seen()
    _probes(monkeypatch, {"https://nope.example/feed": _feed()})
    again = await discovery._verify(
        [_Suggestion("https://nope.example/feed")], known=known, declined=declined
    )
    assert again == []


async def test_a_review_can_only_be_decided_once(store, monkeypatch) -> None:
    review_id = await _review(monkeypatch, urls=("https://a.example/feed",))
    await discovery.decide(review_id, [{"url": "https://a.example/feed", "approved": True}])

    with pytest.raises(ValueError, match="already approved"):
        await discovery.decide(review_id, [{"url": "https://a.example/feed", "approved": True}])


async def test_a_url_that_was_not_in_the_review_is_refused(store, monkeypatch) -> None:
    """The verdict may only speak about what the operator was actually shown."""
    review_id = await _review(monkeypatch, urls=("https://a.example/feed",))
    with pytest.raises(ValueError, match="Not in this review"):
        await discovery.decide(
            review_id, [{"url": "https://somewhere-else.example/feed", "approved": True}]
        )


async def test_approving_something_added_meanwhile_is_not_an_error(store, monkeypatch) -> None:
    """A feed added between the sweep and the verdict is a no-op, not a failure."""
    from ppn_blogger.server import news_store

    review_id = await _review(monkeypatch, urls=("https://a.example/feed",))
    await news_store.create_feed("https://a.example/feed", name="Added by hand")

    result = await discovery.decide(
        review_id, [{"url": "https://a.example/feed", "approved": True}]
    )
    assert result["added"] == 0
    assert (await discovery.get(review_id))["status"] == "approved"


async def test_an_unknown_review_is_a_key_error(store) -> None:
    with pytest.raises(KeyError):
        await discovery.decide(999999, [])


async def test_pending_count_drives_the_nav_badge(store, monkeypatch) -> None:
    assert await discovery.pending_count() == 0
    review_id = await _review(monkeypatch, urls=("https://a.example/feed",))
    assert await discovery.pending_count() == 1

    await discovery.cancel(review_id)
    assert await discovery.pending_count() == 0
    # Cancelling twice is not an error, it just does nothing.
    assert await discovery.cancel(review_id) is False


# ---------------------------------------------------------------------------
# The brief
#
# A sweep with no aim returns the sites you already follow. These assert that
# the operator's words actually reach the model and survive into the review —
# the wiring existed before the box that fills it did, and dead wiring looks
# exactly like working wiring from the outside.
# ---------------------------------------------------------------------------


BRIEF = "Power Platform ALM: pipelines, solution deployment, environment strategy"


def test_a_brief_governs_the_prompt_and_the_sections_become_context() -> None:
    from ppn_blogger import prompts
    from ppn_blogger.settings import get_settings

    settings = get_settings()
    aimed = prompts.feed_scout_discovery_instructions(settings, BRIEF)

    assert BRIEF in aimed
    # Not merely present — stated as the thing that decides.
    assert "This is the brief" in aimed
    assert "follow the brief" in aimed
    # The standing sections stay, demoted to context rather than a quota.
    assert "for context" in aimed


def test_without_a_brief_the_sections_are_the_aim() -> None:
    from ppn_blogger import prompts
    from ppn_blogger.settings import get_settings

    general = prompts.feed_scout_discovery_instructions(get_settings(), "")

    assert "<topics>" in general
    assert "This is the brief" not in general


async def test_the_brief_reaches_the_scout_and_is_stored_verbatim(store, monkeypatch) -> None:
    """End to end through the real `sweep` and `_ask`, stubbed at the model itself.

    Deliberately does *not* patch `_ask` — the wiring under test is the path
    from the operator's words to the agent's instructions, so stubbing it out
    would leave nothing worth asserting.
    """
    from ppn_blogger import agents, util
    from ppn_blogger.models import FeedSuggestion, FeedSuggestionSet

    seen: dict[str, str] = {}

    class _Agent:
        async def run(self, messages):
            seen["message"] = messages[0].text
            return "raw response"

    def build(settings, clients, brief=""):
        seen["brief"] = brief
        return _Agent()

    monkeypatch.setattr(agents, "build_feed_discovery_scout", build)
    monkeypatch.setattr(
        util,
        "parse_model",
        lambda response, model: FeedSuggestionSet(
            suggestions=[
                FeedSuggestion(url="https://alm.example/feed", name="ALM Weekly", reason="why")
            ],
            notes="a note nobody asked for",
        ),
    )
    _probes(monkeypatch, {"https://alm.example/feed": _feed()})

    result = await discovery.sweep(BRIEF)

    # In the instructions, where it still governs at search number nine.
    assert seen["brief"] == BRIEF
    assert BRIEF in seen["message"]
    assert result["candidate_count"] == 1

    # Stored as the operator typed it — not the scout's commentary, which is
    # what the review screen would otherwise show back as "you asked for".
    review = await discovery.get(int(result["review_id"]))
    assert review["instruction"] == BRIEF
    assert "nobody asked for" not in review["instruction"]
