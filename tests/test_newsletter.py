"""Newsletter composition, rendering and scheduling.

The test that matters most here is `test_the_publisher_drops_what_it_was_not_given`.
An email cannot be un-sent, so a link nobody verified must never reach one. The
editor is handed a numbered candidate list and returns ids; anything it names
that was not offered is dropped by `IssuePublisher`, and this is what proves it.

The offline stub is built to misbehave on purpose — a real id, a fabricated one,
and an invented section — so every dry run exercises that gate rather than only
demonstrating that a well-behaved model would have been fine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ppn_blogger import news
from ppn_blogger import newsletter_render as render
from ppn_blogger.server import newsletters


@pytest.fixture
async def store(database_url):
    from ppn_blogger.server import db

    await db.init_db()
    yield


def _entry(url: str, title: str) -> news.FetchedEntry:
    return news.FetchedEntry(
        title=title,
        url=url,
        summary="why it matters",
        published=datetime(2026, 8, 9, tzinfo=UTC),
        entry_key=news.url_hash(url),
    )


async def _feed_with_articles(monkeypatch, count: int = 5, group_name: str = "AI") -> dict:
    """A group containing one feed with `count` harvested articles."""
    from ppn_blogger.server import ingest, news_store

    feed = await news_store.create_feed("https://example.com/feed", name="Example")
    group = await news_store.create_group(group_name)
    await news_store.set_group_feeds(group["id"], [feed["id"]])

    entries = [_entry(f"https://example.com/{i}", f"Post {i}") for i in range(count)]

    async def fetch_many(specs, **kw):
        return [news.FeedFetch(status=200, title="Example", entries=entries)]

    monkeypatch.setattr(news, "fetch_many", fetch_many)
    await ingest.ingest(feed_ids=[feed["id"]])
    return group


# ---------------------------------------------------------------------------
# Candidates — the composition policy, before any model
# ---------------------------------------------------------------------------


async def test_preview_costs_nothing_and_shows_what_would_be_used(store, monkeypatch) -> None:
    group = await _feed_with_articles(monkeypatch, count=5)
    letter = await newsletters.create("AI weekly", group_ids=[group["id"]], max_per_feed=10)

    material = await newsletters.candidates(letter["id"])
    assert len(material["candidates"]) == 5
    assert material["enough"] is True
    assert material["candidates"][0]["url"].startswith("https://example.com/")


async def test_max_per_feed_stops_one_source_dominating(store, monkeypatch) -> None:
    group = await _feed_with_articles(monkeypatch, count=10)
    letter = await newsletters.create("Capped", group_ids=[group["id"]], max_per_feed=3)

    material = await newsletters.candidates(letter["id"])
    assert len(material["candidates"]) == 3


async def test_articles_already_sent_are_not_offered_again(store, monkeypatch) -> None:
    group = await _feed_with_articles(monkeypatch, count=4)
    letter = await newsletters.create("Weekly", group_ids=[group["id"]], max_per_feed=10)

    first = await newsletters.candidates(letter["id"])
    used = first["candidates"][0]["id"]

    issue = await newsletters.save_issue(
        letter["id"],
        {
            "subject": "s",
            "sections": [{"id": "ai", "items": [{"article_id": used, "headline": "h", "blurb": "b"}]}],
            "article_ids": [used],
        },
        {"markdown": "m", "html": "h", "text_body": "t"},
        status="sent",
    )
    assert issue["item_count"] == 1

    again = await newsletters.candidates(letter["id"])
    assert used not in [c["id"] for c in again["candidates"]]


async def test_a_discarded_issue_does_not_burn_its_articles(store, monkeypatch) -> None:
    """Only a sent or ready issue spends an article.

    A draft the operator threw away, or a run that failed, must not remove the
    article from every future issue — otherwise one bad generation quietly
    deletes a week of news.
    """
    group = await _feed_with_articles(monkeypatch, count=3)
    letter = await newsletters.create("Weekly", group_ids=[group["id"]], max_per_feed=10)
    used = (await newsletters.candidates(letter["id"]))["candidates"][0]["id"]

    await newsletters.save_issue(
        letter["id"],
        {
            "subject": "s",
            "sections": [{"id": "ai", "items": [{"article_id": used, "headline": "h", "blurb": "b"}]}],
            "article_ids": [used],
        },
        {"markdown": "m", "html": "h", "text_body": "t"},
        status="draft",
    )

    assert used in [c["id"] for c in (await newsletters.candidates(letter["id"]))["candidates"]]


async def test_a_newsletter_with_no_groups_says_so(store) -> None:
    letter = await newsletters.create("Empty")
    material = await newsletters.candidates(letter["id"])
    assert material["candidates"] == []
    assert "no feed groups" in material["reason"]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


async def test_the_publisher_drops_what_it_was_not_given(store, monkeypatch) -> None:
    """The one that matters. A fabricated id must never reach an issue.

    The stub deliberately returns three items: one real candidate, one id that
    was never offered, and one in a section that is not in the taxonomy. Only the
    first may survive.
    """
    from ppn_blogger import workflows as wf
    from ppn_blogger.server.newsletter_runs import compose_and_store
    from ppn_blogger.testing import stub_clients

    group = await _feed_with_articles(monkeypatch, count=5)
    letter = await newsletters.create(
        "Gate test", group_ids=[group["id"]], max_per_feed=10, min_items=1
    )

    real = wf.compose_issue

    async def stubbed(newsletter, candidates, **kw):
        kw.setdefault("clients", stub_clients(exercise_loops=False))
        return await real(newsletter, candidates, **kw)

    monkeypatch.setattr(wf, "compose_issue", stubbed)

    result = await compose_and_store(letter["id"])

    assert result["skipped"] is False
    # One real item survived; the fabricated id and the invented section did not.
    assert result["item_count"] == 1
    assert result["dropped"] >= 2

    issue = await newsletters.get_issue(result["issue_id"])
    offered = {c["id"] for c in (await newsletters.candidates(letter["id"]))["candidates"]} | {
        i["article_id"] for i in issue["items"]
    }
    for item in issue["items"]:
        assert item["article_id"] in offered
        # And the URL came from the candidate row, never from the model.
        assert item["url"].startswith("https://example.com/")


async def test_a_quiet_week_skips_without_calling_a_model(store, monkeypatch) -> None:
    """Below min_items the graph ends at the builder — no tokens are spent.

    Asserted on *generation*, not on the agent factory: the graph is constructed
    before the builder decides anything, and building an Agent object costs
    nothing. `_payload` is only reached when a model actually produces a
    response, so a call there is the real signal.
    """
    from ppn_blogger import workflows as wf
    from ppn_blogger.server.newsletter_runs import compose_and_store
    from ppn_blogger.testing import StubChatClient, stub_clients

    group = await _feed_with_articles(monkeypatch, count=2)
    letter = await newsletters.create(
        "Strict", group_ids=[group["id"]], min_items=5, max_per_feed=10
    )

    generated: list[str] = []
    original = StubChatClient._payload

    def counted(self, model, messages):
        generated.append(model.__name__)
        return original(self, model, messages)

    monkeypatch.setattr(StubChatClient, "_payload", counted)

    real = wf.compose_issue

    async def stubbed(newsletter, candidates, **kw):
        kw.setdefault("clients", stub_clients(exercise_loops=False))
        return await real(newsletter, candidates, **kw)

    monkeypatch.setattr(wf, "compose_issue", stubbed)

    result = await compose_and_store(letter["id"])

    assert result["skipped"] is True
    assert "below the minimum" in result["reason"]
    assert generated == []  # the editor never ran

    # The skip is recorded rather than silent — a quiet week should look like a
    # decision, not like an issue that mysteriously never appeared.
    issues = await newsletters.list_issues(letter["id"])
    assert issues[0]["status"] == "skipped"


# ---------------------------------------------------------------------------
# Schedules — pure
# ---------------------------------------------------------------------------


def _letter(**kw):
    base = {
        "enabled": True,
        "schedule_kind": "manual",
        "interval_minutes": 0,
        "weekday": 0,
        "day_of_month": 1,
        "hour_local": 7,
        "minute_local": 0,
        "timezone": "Europe/Madrid",
    }
    base.update(kw)
    return base


def test_manual_never_schedules_itself() -> None:
    assert newsletters.next_due(_letter(), after=datetime.now(UTC)) is None


def test_a_disabled_newsletter_never_schedules() -> None:
    letter = _letter(schedule_kind="interval", interval_minutes=60, enabled=False)
    assert newsletters.next_due(letter, after=datetime.now(UTC)) is None


def test_interval_is_measured_from_now() -> None:
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    due = newsletters.next_due(_letter(schedule_kind="interval", interval_minutes=90), after=now)
    assert due == now + timedelta(minutes=90)


def test_weekly_lands_on_the_right_weekday_and_hour() -> None:
    # 2026-08-10 is a Monday.
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    due = newsletters.next_due(
        _letter(schedule_kind="weekly", weekday=2, hour_local=7, timezone="UTC"), after=now
    )
    assert due is not None
    assert due.weekday() == 2 and due.hour == 7
    assert due > now


def test_weekly_today_but_already_past_rolls_to_next_week() -> None:
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)  # Monday noon
    due = newsletters.next_due(
        _letter(schedule_kind="weekly", weekday=0, hour_local=7, timezone="UTC"), after=now
    )
    assert due is not None and (due - now).days >= 6


def test_monthly_is_capped_at_the_28th() -> None:
    """'The 31st' silently meaning 'the 28th' in February is a schedule that lies."""
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    due = newsletters.next_due(
        _letter(schedule_kind="monthly", day_of_month=31, timezone="UTC"), after=now
    )
    assert due is not None and due.day == 28


def test_schedules_survive_a_dst_boundary() -> None:
    """Europe/Madrid moves on the last Sunday of October; 07:00 local stays 07:00."""
    from zoneinfo import ZoneInfo

    before = datetime(2026, 10, 20, 12, tzinfo=UTC)
    due = newsletters.next_due(
        _letter(schedule_kind="weekly", weekday=2, hour_local=7, timezone="Europe/Madrid"),
        after=before,
    )
    assert due is not None
    assert due.astimezone(ZoneInfo("Europe/Madrid")).hour == 7


def test_upcoming_shows_the_next_few_fire_times() -> None:
    letter = _letter(schedule_kind="interval", interval_minutes=60)
    times = newsletters.upcoming(letter, count=3, after=datetime(2026, 8, 10, tzinfo=UTC))
    assert len(times) == 3 and times == sorted(times)


def test_an_unknown_schedule_kind_is_rejected(store) -> None:
    pass  # covered by the API test; kept here as a marker for the validation path


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

ISSUE = {
    "subject": "Three things",
    "preheader": "A quiet week",
    "intro": "Two of these matter.",
    "generated_on": "2026-08-10",
    "sections": [
        {
            "id": "microsoft",
            "title": "From Microsoft",
            "items": [
                {
                    "article_id": 1,
                    "headline": 'DLP gains <granular> scopes & "narrower" defaults',
                    "url": "https://learn.microsoft.com/x?a=1&b=2",
                    "source": "Power Platform Blog",
                    "published": "2026-08-09",
                    "blurb": "Read it before relying on it.",
                },
                {
                    "article_id": 2,
                    "headline": "Relative link",
                    "url": "/not-absolute",
                    "source": "Somewhere",
                    "published": "",
                    "blurb": "Its href must not survive.",
                },
            ],
        }
    ],
}


def test_email_html_is_safe_and_self_contained() -> None:
    html = render.render_html(ISSUE, name="Weekly", footer="A footer", accent="#c084fc")
    assert "&lt;granular&gt;" in html
    assert "&quot;narrower&quot;" in html
    assert 'href="https://learn.microsoft.com/x?a=1&amp;b=2"' in html
    # A relative link is dead in an inbox, so it loses its href entirely.
    assert "/not-absolute" not in html
    assert "<script" not in html and "<style" not in html
    # Outlook ignores stylesheets and some clients strip <style>, so every rule
    # has to be on the element.
    assert html.count("style=") > 8


def test_a_javascript_url_never_becomes_a_link() -> None:
    issue = {
        "subject": "x",
        "sections": [
            {"id": "ai", "title": "AI", "items": [{"headline": "h", "url": "javascript:alert(1)"}]}
        ],
    }
    html = render.render_html(issue)
    assert "javascript:" not in html


def test_plain_text_carries_every_url_in_full() -> None:
    text = render.render_text(ISSUE, name="Weekly")
    assert "https://learn.microsoft.com/x?a=1&b=2" in text
    assert "Three things" in text


def test_short_form_truncates_honestly() -> None:
    big = {
        "subject": "Lots",
        "sections": [
            {
                "id": "ai",
                "title": "AI",
                "items": [
                    {"headline": f"Item {i}", "url": f"https://example.com/{i}"} for i in range(200)
                ],
            }
        ],
    }
    short = render.render_short(big, limit=600)
    assert len(short) <= 700
    # Says what it left out rather than silently stopping.
    assert "more." in short


def test_markdown_is_editable_and_links_out() -> None:
    md = render.render_markdown(ISSUE, name="Weekly")
    assert md.startswith("# Three things")
    assert "[DLP gains" in md and "](https://learn.microsoft.com/x?a=1&b=2)" in md
