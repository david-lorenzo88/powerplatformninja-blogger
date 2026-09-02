"""Sending an issue, and what happens when it goes wrong.

The assertion this file exists for is `test_a_channel_that_raises_never_destroys_the_issue`.
An issue costs real model calls; a push service having a bad afternoon must not
be able to lose one. Same doctrine as cover generation and the WordPress push.

The second is that a *permanent* failure — a bad address, an unapproved template
— is not retried. Retrying those three times only delays the moment someone
notices, and on a per-conversation-billed channel it costs money to do so.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from ppn_blogger.server import delivery, newsletters
from ppn_blogger.server.channels import CHANNELS, DeliveryResult


@pytest.fixture
async def store(database_url):
    from ppn_blogger.server import db

    await db.init_db()
    yield


async def _article() -> int:
    """A real article row, because an issue item has a foreign key to one.

    Hard-coding `article_id=1` used to work locally and fail on SQL Server:
    SQLite ships with `PRAGMA foreign_keys` off and accepts the orphan, while
    Azure SQL rejects the INSERT. The pragma is on now, so this helper is what
    keeps these tests honest rather than merely passing.
    """
    from ppn_blogger.server.db import Article, session
    from ppn_blogger.server.news_store import create_feed

    feed = await create_feed("https://example.com/feed", name="Example")
    async with session() as s:
        article = Article(
            feed_id=feed["id"],
            entry_key="e1",
            url_hash="h1",
            url="https://example.com/one",
            title="Something shipped",
        )
        s.add(article)
        await s.commit()
        return article.id


async def _issue(status: str = "draft") -> dict:
    """A newsletter with one stored issue, ready to send."""
    article_id = await _article()
    letter = await newsletters.create("Weekly")
    return await newsletters.save_issue(
        letter["id"],
        {
            "subject": "Three things",
            "preheader": "a quiet week",
            "intro": "hello",
            "sections": [
                {
                    "id": "ai",
                    "items": [
                        {
                            "article_id": article_id,
                            "headline": "Something shipped",
                            "blurb": "why it matters",
                            "url": "https://example.com/one",
                        }
                    ],
                }
            ],
            "article_ids": [article_id],
        },
        {"markdown": "# Three things", "html": "<p>hi</p>", "text_body": "Three things"},
        status=status,
    )


def _channel(monkeypatch, channel_id: str, result):
    """Replace one channel's send with a canned outcome."""
    calls: list[str] = []

    async def send(issue, target):
        calls.append(target.address or "broadcast")
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(CHANNELS[channel_id], "send", send)
    monkeypatch.setattr(type(CHANNELS[channel_id]), "is_configured", property(lambda self: True))
    return calls


# ---------------------------------------------------------------------------
# The doctrine
# ---------------------------------------------------------------------------


async def test_a_channel_that_raises_never_destroys_the_issue(store, monkeypatch) -> None:
    issue = await _issue()
    await newsletters.create_recipient("email", "someone@example.com", name="Someone")
    _channel(monkeypatch, "email", RuntimeError("provider on fire"))

    result = await delivery.deliver_issue(issue["id"])

    assert result["sent"] == 0
    assert result["failed"] + result["pending"] == 1

    # The issue survives intact, with its body, ready to try again.
    after = await newsletters.get_issue(issue["id"])
    assert after is not None
    assert after["markdown"] == "# Three things"
    assert after["subject"] == "Three things"
    assert after["status"] in ("sending", "failed")


async def test_every_delivery_row_exists_before_anything_is_sent(store, monkeypatch) -> None:
    """Intent durable before side effect — the same ordering as reviews.decide.

    A process that dies halfway must leave rows showing how far it got, rather
    than a silence that could mean anything.
    """
    issue = await _issue()
    for i in range(3):
        await newsletters.create_recipient("email", f"p{i}@example.com")

    seen: list[int] = []

    async def send(payload, target):
        # By the time the first send runs, all three rows already exist.
        seen.append(len(await delivery.list_deliveries(issue["id"])))
        return DeliveryResult(True, provider_message_id="ok")

    monkeypatch.setattr(CHANNELS["email"], "send", send)
    monkeypatch.setattr(type(CHANNELS["email"]), "is_configured", property(lambda self: True))

    await delivery.deliver_issue(issue["id"])
    assert seen and min(seen) == 3


# ---------------------------------------------------------------------------
# Retry semantics
# ---------------------------------------------------------------------------


async def test_a_permanent_failure_is_not_retried(store, monkeypatch) -> None:
    issue = await _issue()
    await newsletters.create_recipient("whatsapp", "+34600111222", name="A number")
    calls = _channel(
        monkeypatch, "whatsapp", DeliveryResult(False, error="template not approved", permanent=True)
    )

    result = await delivery.deliver_issue(issue["id"])

    assert len(calls) == 1  # tried once, not three times
    assert result["failed"] == 1 and result["pending"] == 0
    row = result["deliveries"][0]
    assert row["next_retry_at"] is None
    assert "template" in row["error"]


async def test_a_permanent_failure_parks_the_recipient(store, monkeypatch) -> None:
    """A dead address stops being retried on every future issue, without being deleted."""
    issue = await _issue()
    recipient = await newsletters.create_recipient("email", "gone@example.com")
    _channel(monkeypatch, "email", DeliveryResult(False, error="mailbox not found", permanent=True))

    await delivery.deliver_issue(issue["id"])

    rows = await newsletters.list_recipients()
    parked = next(r for r in rows if r["id"] == recipient["id"])
    assert parked["failed_at"] is not None
    assert "mailbox" in parked["last_error"]


async def test_a_transient_failure_is_scheduled_for_another_go(store, monkeypatch) -> None:
    issue = await _issue()
    await newsletters.create_recipient("email", "flaky@example.com")
    _channel(monkeypatch, "email", DeliveryResult(False, error="503 upstream"))

    result = await delivery.deliver_issue(issue["id"])

    assert result["pending"] == 1
    assert result["deliveries"][0]["next_retry_at"] is not None

    # Not due *yet* — the backoff is the point. A retry that fires immediately
    # is not a retry, it is the same request again.
    assert await delivery.due_retries() == []

    # Once the backoff has elapsed, the retry job picks it up.
    from sqlalchemy import select

    from ppn_blogger.server.db import Delivery, session, utcnow

    async with session() as s:
        row = (
            await s.execute(select(Delivery).where(Delivery.issue_id == issue["id"]))
        ).scalars().first()
        row.next_retry_at = utcnow() - timedelta(seconds=1)
        await s.commit()

    assert issue["id"] in await delivery.due_retries()


async def test_retry_leaves_what_already_worked_alone(store, monkeypatch) -> None:
    """Retrying after a partial failure must not send twice to everyone it worked for."""
    issue = await _issue()
    await newsletters.create_recipient("email", "good@example.com", name="Good")
    await newsletters.create_recipient("email", "bad@example.com", name="Bad")

    async def send(payload, target):
        if "bad@" in target.address:
            return DeliveryResult(False, error="nope", permanent=True)
        return DeliveryResult(True, provider_message_id="ok")

    monkeypatch.setattr(CHANNELS["email"], "send", send)
    monkeypatch.setattr(type(CHANNELS["email"]), "is_configured", property(lambda self: True))

    first = await delivery.deliver_issue(issue["id"])
    assert first["sent"] == 1 and first["failed"] == 1

    attempted: list[str] = []

    async def send_again(payload, target):
        attempted.append(target.address)
        return DeliveryResult(True, provider_message_id="ok")

    monkeypatch.setattr(CHANNELS["email"], "send", send_again)
    await delivery.retry_issue(issue["id"])

    assert attempted == ["bad@example.com"]


# ---------------------------------------------------------------------------
# Telegram: what is permanent, and what is merely wrong
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code: int, description: str = "") -> None:
        self.status_code = status_code
        self._description = description

    def json(self) -> dict:
        return {"ok": False, "description": self._description}

    @property
    def text(self) -> str:
        return self._description


def _telegram_replying(monkeypatch, response) -> list[dict]:
    """Capture what would be posted to the Bot API."""
    import httpx

    from ppn_blogger.settings import get_settings

    monkeypatch.setattr(get_settings().telegram, "bot_token", "test-token")
    sent: list[dict] = []

    class FakeClient:
        def __init__(self, *a, **kw) -> None: ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None: ...

        async def post(self, url, json=None):
            sent.append(json or {})
            return response

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return sent


async def test_a_malformed_message_does_not_disqualify_the_chat(monkeypatch) -> None:
    """*can't parse entities* is our bug, not a dead address.

    Treating it as permanent is what parked a live group over a headline with an
    underscore in it, and a parked recipient makes every later issue report "no
    recipients for this newsletter".
    """
    from ppn_blogger.server import channels

    sent = _telegram_replying(
        monkeypatch,
        _Response(400, "Bad Request: can't parse entities: Can't find end of the entity"),
    )
    result = await channels.send_telegram("-100", "<b>oops", parse_mode="HTML")

    assert result.ok is False
    assert result.permanent is False  # retried, and the recipient stays
    assert sent[0]["parse_mode"] == "HTML"


async def test_a_dead_chat_is_permanent(monkeypatch) -> None:
    from ppn_blogger.server import channels

    _telegram_replying(monkeypatch, _Response(400, "Bad Request: chat not found"))
    assert (await channels.send_telegram("-100", "hi")).permanent is True

    _telegram_replying(monkeypatch, _Response(403, "Forbidden: bot was blocked by the user"))
    assert (await channels.send_telegram("-100", "hi")).permanent is True


async def test_the_relay_sends_no_parse_mode_at_all(monkeypatch) -> None:
    """A feed's headline is verbatim text, so nothing may interpret it."""
    from ppn_blogger.server import channels

    sent = _telegram_replying(monkeypatch, _Response(400, "whatever"))
    await channels.send_telegram("-100", "Power_Platform *ships* [preview]")
    assert "parse_mode" not in sent[0]


def test_truncation_never_cuts_through_markup() -> None:
    """A blind slice through `&amp;` or a `<b>` tag is itself a 400."""
    from ppn_blogger.server.channels import TELEGRAM_LIMIT, _fit

    body = "<b>Title</b>\n" + "\n".join(f"• item {i} &amp; more" for i in range(600))
    assert len(body) > TELEGRAM_LIMIT

    fitted = _fit(body, "HTML")
    assert len(fitted) <= TELEGRAM_LIMIT
    assert fitted.count("<b>") == fitted.count("</b>")
    assert not fitted.endswith("&am") and "&amp" not in fitted.split("\n")[-1][-4:]

    # Plain text has nothing to protect, so it is cut at the cap.
    assert len(_fit(body, "")) == TELEGRAM_LIMIT


# ---------------------------------------------------------------------------
# Configuration and status
# ---------------------------------------------------------------------------


async def test_an_unconfigured_channel_is_skipped_not_failed(store) -> None:
    """Nothing is broken — the channel simply is not set up.

    A red row would send someone looking for a fault that does not exist.
    """
    issue = await _issue()
    await newsletters.create_recipient("telegram", "-1001234567890", name="A group")

    result = await delivery.deliver_issue(issue["id"])

    assert result["skipped"] == 1 and result["failed"] == 0
    # The issue goes back to reviewable rather than failed: it can still be sent.
    after = await newsletters.get_issue(issue["id"])
    assert after is not None and after["status"] == "ready"


async def test_a_broadcast_channel_gets_one_row_with_no_recipient(store) -> None:
    """Web push goes to every subscribed browser, which is not a recipient row.

    So a broadcast channel gets exactly one delivery with `recipient_id` null —
    and can only be added once, because there is nothing to tell two of them
    apart.
    """
    issue = await _issue()
    await newsletters.create_recipient("manual", "", name="Copy out")

    with pytest.raises(ValueError, match="already on the list"):
        await newsletters.create_recipient("manual", "", name="Copy out again")

    result = await delivery.deliver_issue(issue["id"])
    assert result["total"] == 1
    assert result["deliveries"][0]["recipient_id"] is None


async def test_one_issue_goes_out_on_every_configured_channel(store, monkeypatch) -> None:
    """The assertion behind auto-send: sending is a fan-out, not a first match.

    An unattended issue has nobody to notice that only one channel was used, so
    this pins the shape: every enabled recipient gets a delivery row on their own
    channel, a broadcast channel gets its single row, and all of them are sent in
    the one pass.
    """
    issue = await _issue()
    await newsletters.create_recipient("telegram", "-1001", name="The group")
    await newsletters.create_recipient("telegram", "424242", name="David")
    await newsletters.create_recipient("email", "david@example.com", name="David")
    await newsletters.create_recipient("manual", "", name="Copy out")

    telegram = _channel(monkeypatch, "telegram", DeliveryResult(True, provider_message_id="t"))
    email = _channel(monkeypatch, "email", DeliveryResult(True, provider_message_id="e"))
    manual = _channel(monkeypatch, "manual", DeliveryResult(True))

    result = await delivery.deliver_issue(issue["id"])

    assert result["total"] == 4
    assert result["sent"] == 4 and result["failed"] == 0
    assert sorted(telegram) == ["-1001", "424242"]
    assert email == ["david@example.com"]
    assert manual == ["broadcast"]
    assert {d["channel"] for d in result["deliveries"]} == {"telegram", "email", "manual"}


async def test_sending_with_no_recipients_says_so(store) -> None:
    issue = await _issue()
    with pytest.raises(ValueError, match="No recipients"):
        await delivery.deliver_issue(issue["id"])


async def test_a_skipped_issue_cannot_be_sent(store) -> None:
    issue = await _issue(status="skipped")
    with pytest.raises(ValueError, match="nothing to send"):
        await delivery.deliver_issue(issue["id"])


async def test_a_recipient_can_be_limited_to_one_newsletter(store, monkeypatch) -> None:
    issue = await _issue()
    other = await newsletters.create("Something else")
    await newsletters.create_recipient(
        "email", "only-other@example.com", newsletter_ids=[other["id"]]
    )
    await newsletters.create_recipient("email", "everything@example.com")
    _channel(monkeypatch, "email", DeliveryResult(True, provider_message_id="ok"))

    result = await delivery.deliver_issue(issue["id"])
    assert result["total"] == 1
    assert result["deliveries"][0]["recipient"] == "everything@example.com"


async def test_the_same_address_cannot_be_added_twice(store) -> None:
    await newsletters.create_recipient("email", "Someone@Example.com")
    with pytest.raises(ValueError, match="already on the list"):
        # Case and spacing must not create a second row.
        await newsletters.create_recipient("email", "  someone@example.com ")


async def test_a_whatsapp_number_normalises_to_e164(store) -> None:
    created = await newsletters.create_recipient("whatsapp", "+34 600 111 222")
    assert created["address"] == "+34600111222"
    with pytest.raises(ValueError, match="already on the list"):
        await newsletters.create_recipient("whatsapp", "34600111222")


async def test_an_unknown_channel_is_refused(store) -> None:
    with pytest.raises(ValueError, match="Unknown channel"):
        await newsletters.create_recipient("carrier-pigeon", "somewhere")


def test_every_channel_declares_its_shape() -> None:
    """Broadcast channels have no per-recipient target; the rest must have one."""
    assert CHANNELS["webpush"].broadcast is True
    assert CHANNELS["manual"].broadcast is True
    for channel_id in ("email", "telegram", "whatsapp"):
        assert CHANNELS[channel_id].broadcast is False
