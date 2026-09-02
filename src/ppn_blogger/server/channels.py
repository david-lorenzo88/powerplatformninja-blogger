"""Getting one issue to one recipient, on whichever channel they use.

A ``Protocol`` rather than an ABC, matching ``config_source.ConfigSource`` — the
one other pluggable seam in this codebase.

**No ``send`` may raise.** Same doctrine as cover generation, the WordPress push
and ``push.notify_run_finished``: an issue that cost real model calls must not be
lost because a push service is having a bad afternoon. Every failure comes back
as a ``DeliveryResult``, and the caller decides whether it is worth retrying.

The channels differ far more than "send some text" suggests, and the differences
are platform facts rather than choices:

* **Web push and manual** need no vendor at all, so they work the day this ships.
* **Email** is the only one that carries the whole issue. ACS is the real target;
  Container Apps blocks outbound port 25, so raw SMTP is local-development only.
* **Telegram** is the only channel that can post to a **group** — a group or
  channel is just a chat id (negative for groups), and the bot must be added to
  it. Messages cap at 4096 characters, so the issue goes as a short digest.
* **WhatsApp has no group API.** Meta's Cloud API messages individual numbers,
  and a newsletter is business-initiated outside the 24-hour service window, so
  it can only be a **pre-approved template**, billed per conversation. The
  template therefore carries a teaser and a link, never the issue.

Nothing here drives WhatsApp Web. Libraries that do can post to groups and will
get the number banned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..settings import get_settings

logger = logging.getLogger("ppn.server.channels")

TELEGRAM_LIMIT = 4096


@dataclass(slots=True)
class DeliveryResult:
    ok: bool
    provider_message_id: str = ""
    error: str = ""
    # A bad address, an unsubscribed number, a 410 — retrying will never help,
    # so the delivery goes straight to failed and the recipient is parked.
    permanent: bool = False


@dataclass(slots=True)
class IssuePayload:
    """One issue, in every form a channel might need."""

    id: int
    newsletter_name: str
    subject: str
    preheader: str
    html: str
    text_body: str
    markdown: str
    short: str
    item_count: int
    url: str = ""  # where it can be read, when a channel can only carry a link


@dataclass(slots=True)
class RecipientRef:
    id: int | None
    channel: str
    address: str
    name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Channel(Protocol):
    id: str
    label: str
    # Broadcast channels have no per-recipient target: web push goes to every
    # subscribed browser, which is not a recipient row.
    broadcast: bool

    @property
    def is_configured(self) -> bool: ...

    @property
    def status_detail(self) -> str: ...

    async def send(self, issue: IssuePayload, target: RecipientRef) -> DeliveryResult: ...


# ---------------------------------------------------------------------------
# No vendor required
# ---------------------------------------------------------------------------


class WebPushChannel:
    """Reuses the Web Push that already works. Zero new configuration.

    A notification cannot carry an issue, so this is a pointer: it says an issue
    is ready and deep-links to it. That makes it the only channel that is useful
    on day one with nothing set up.
    """

    id = "webpush"
    label = "Web push"
    broadcast = True

    @property
    def is_configured(self) -> bool:
        return get_settings().push.is_configured

    @property
    def status_detail(self) -> str:
        return "ready" if self.is_configured else "VAPID keys are not set"

    async def send(self, issue: IssuePayload, target: RecipientRef) -> DeliveryResult:
        from . import push

        try:
            delivered = await push.notify(
                issue.newsletter_name or "Newsletter",
                issue.subject or f"{issue.item_count} items",
                f"/newsletters/issues/{issue.id}",
                f"ppn-issue-{issue.id}",
            )
        except Exception as exc:  # noqa: BLE001 - a channel never raises
            return DeliveryResult(False, error=f"{type(exc).__name__}: {exc}"[:400])
        if delivered:
            return DeliveryResult(True, provider_message_id=f"{delivered} device(s)")
        return DeliveryResult(False, error="no subscribed devices")


class ManualChannel:
    """Marks the issue delivered so you can copy it out yourself.

    Sounds like a no-op, and is the thing that makes this feature usable before
    any vendor decision has been made: the issue is generated, rendered and
    recorded as handled, and the markdown is right there to paste wherever it is
    actually going.
    """

    id = "manual"
    label = "Copy out by hand"
    broadcast = True

    @property
    def is_configured(self) -> bool:
        return True

    @property
    def status_detail(self) -> str:
        return "always available"

    async def send(self, issue: IssuePayload, target: RecipientRef) -> DeliveryResult:
        return DeliveryResult(True, provider_message_id="manual")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


class EmailChannel:
    """ACS in production, SMTP for local development.

    Split at ``EMAIL_PROVIDER`` rather than as two channels because a recipient's
    address does not change when the transport does — swapping provider must not
    mean re-entering the list.
    """

    id = "email"
    label = "Email"
    broadcast = False

    @property
    def is_configured(self) -> bool:
        return get_settings().email.is_configured

    @property
    def status_detail(self) -> str:
        return get_settings().email.status_detail

    async def send(self, issue: IssuePayload, target: RecipientRef) -> DeliveryResult:
        settings = get_settings().email
        if not target.address:
            return DeliveryResult(False, error="no address", permanent=True)
        try:
            if settings.provider == "acs":
                return await self._send_acs(issue, target, settings)
            if settings.provider == "smtp":
                return await self._send_smtp(issue, target, settings)
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, error=f"{type(exc).__name__}: {exc}"[:400])
        return DeliveryResult(False, error="EMAIL_PROVIDER is not set", permanent=True)

    async def _send_acs(self, issue: IssuePayload, target: RecipientRef, settings: Any) -> DeliveryResult:
        """Azure Communication Services Email.

        The SDK is synchronous, so it goes through ``asyncio.to_thread`` — the
        same shape ``pywebpush`` already forced. Managed identity when only the
        endpoint is configured, which is how the deployed app authenticates
        everywhere else.
        """
        import asyncio

        def _send() -> str:
            from azure.communication.email import EmailClient

            if settings.acs_connection_string:
                client = EmailClient.from_connection_string(settings.acs_connection_string)
            else:
                from azure.identity import DefaultAzureCredential

                client = EmailClient(settings.acs_endpoint, DefaultAzureCredential())

            message = {
                "senderAddress": settings.from_address,
                "recipients": {"to": [{"address": target.address, "displayName": target.name}]},
                "content": {
                    "subject": issue.subject or issue.newsletter_name,
                    "plainText": issue.text_body,
                    "html": issue.html,
                },
            }
            poller = client.begin_send(message)
            return str(poller.result().get("id", ""))

        message_id = await asyncio.to_thread(_send)
        return DeliveryResult(True, provider_message_id=message_id[:200])

    async def _send_smtp(self, issue: IssuePayload, target: RecipientRef, settings: Any) -> DeliveryResult:
        import asyncio
        import smtplib
        from email.message import EmailMessage

        def _send() -> str:
            message = EmailMessage()
            message["Subject"] = issue.subject or issue.newsletter_name
            message["From"] = f"{settings.from_name} <{settings.from_address}>"
            message["To"] = target.address
            message.set_content(issue.text_body)
            message.add_alternative(issue.html, subtype="html")

            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
            return "smtp"

        await asyncio.to_thread(_send)
        return DeliveryResult(True, provider_message_id="smtp")


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class TelegramChannel:
    """The only channel here that can post to a group.

    A group or channel is just a ``chat_id`` — negative for groups — and the bot
    has to have been added to it. Messages cap at 4096 characters, so the issue
    travels as the short digest with a link per item rather than the full body.
    """

    id = "telegram"
    label = "Telegram"
    broadcast = False

    @property
    def is_configured(self) -> bool:
        return get_settings().telegram.is_configured

    @property
    def status_detail(self) -> str:
        return get_settings().telegram.status_detail

    async def send(self, issue: IssuePayload, target: RecipientRef) -> DeliveryResult:
        if not target.address:
            return DeliveryResult(False, error="no chat id", permanent=True)
        # HTML, because `render_short` escapes every value it interpolates and
        # Telegram's HTML escaping is total. It sent Markdown once; a headline
        # with a lone `_` in it was a 400 and cost the recipient its place.
        return await send_telegram(target.address, issue.short, parse_mode="HTML")


async def send_telegram(chat_id: str, text: str, *, parse_mode: str = "") -> DeliveryResult:
    """One message to one chat. Never raises; shared with the article relay.

    ``parse_mode`` defaults to **none**, and that default is the point: with a
    parse mode set, Telegram interprets the message's punctuation and answers a
    400 if it does not balance. Only text whose every interpolated value has
    been escaped for that mode may set one — `render_short` for the newsletter
    digest, in HTML. The relay sends a feed's headline verbatim, so it sends it
    as plain text.
    """
    import httpx

    settings = get_settings().telegram
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": _fit(text, parse_mode),
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            response = await http.post(
                f"https://api.telegram.org/bot{settings.bot_token}/sendMessage", json=payload
            )
    except Exception as exc:  # noqa: BLE001
        return DeliveryResult(False, error=f"{type(exc).__name__}: {exc}"[:400])

    if response.status_code == 200:
        body = response.json()
        return DeliveryResult(
            True, provider_message_id=str(body.get("result", {}).get("message_id", ""))
        )
    detail = _describe_http(response)
    return DeliveryResult(False, error=detail, permanent=_unreachable(response.status_code, detail))


def _fit(text: str, parse_mode: str) -> str:
    """Truncate to Telegram's cap without splitting markup.

    A blind slice is safe on plain text and dangerous with a parse mode: cutting
    through `&amp;` or between `<b>` and `</b>` is itself a 400. Every tag this
    project emits opens and closes inside one line, so a line boundary is a safe
    cut — and if a single line is somehow over the cap there is nothing to
    preserve anyway.
    """
    if len(text) <= TELEGRAM_LIMIT:
        return text
    if not parse_mode:
        return text[:TELEGRAM_LIMIT]
    cut = text.rfind("\n", 0, TELEGRAM_LIMIT)
    return text[:cut] if cut > 0 else text[:TELEGRAM_LIMIT]


def _unreachable(status: int, detail: str) -> bool:
    """Whether the failure means this chat will never accept a message.

    Only that answer parks a recipient, so it has to mean the *address* is dead —
    not that this particular message was malformed. Telegram returns 400 for
    both "chat not found" and "can't parse entities", and treating the second as
    permanent is what parked a perfectly good group over a headline containing
    an underscore. A malformed message is our bug: it retries, it does not
    disqualify the recipient.
    """
    if status == 403:  # blocked, kicked, or the bot was removed from the group
        return True
    if status != 400:
        return False
    return "parse" not in detail.lower() and "entit" not in detail.lower()


class WhatsAppChannel:
    """Meta's Cloud API — individual numbers, template messages only.

    Both constraints are Meta's, not ours. There is no group API, which is why
    Telegram exists here. And a newsletter is a business-initiated message
    outside the 24-hour customer-service window, so it may only be a
    **pre-approved template**: free-form text would simply be rejected. The
    template gets the newsletter name, the item count and a link.
    """

    id = "whatsapp"
    label = "WhatsApp"
    broadcast = False

    @property
    def is_configured(self) -> bool:
        return get_settings().whatsapp.is_configured

    @property
    def status_detail(self) -> str:
        return get_settings().whatsapp.status_detail

    async def send(self, issue: IssuePayload, target: RecipientRef) -> DeliveryResult:
        import httpx

        settings = get_settings().whatsapp
        if not target.address:
            return DeliveryResult(False, error="no phone number", permanent=True)

        # Template parameters, in the order the approved template declares them.
        # Kept to three so the template stays simple enough to get approved.
        parameters = [
            {"type": "text", "text": issue.newsletter_name or "Newsletter"},
            {"type": "text", "text": str(issue.item_count)},
            {"type": "text", "text": issue.url or issue.subject[:60] or "see the app"},
        ]
        payload = {
            "messaging_product": "whatsapp",
            "to": target.address.lstrip("+"),
            "type": "template",
            "template": {
                "name": settings.template_name,
                "language": {"code": settings.template_language},
                "components": [{"type": "body", "parameters": parameters}],
            },
        }

        try:
            async with httpx.AsyncClient(timeout=20) as http:
                response = await http.post(
                    f"https://graph.facebook.com/{settings.api_version}/"
                    f"{settings.phone_number_id}/messages",
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.token}"},
                )
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, error=f"{type(exc).__name__}: {exc}"[:400])

        if 200 <= response.status_code < 300:
            body = response.json()
            messages = body.get("messages") or [{}]
            return DeliveryResult(True, provider_message_id=str(messages[0].get("id", "")))
        # 400 covers an unregistered number and an unapproved template — both
        # need a human, not a retry.
        return DeliveryResult(
            False, error=_describe_http(response), permanent=response.status_code == 400
        )


def _describe_http(response: Any) -> str:
    """A provider's own error text beats "HTTP 400" when a human has to act on it."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return f"HTTP {response.status_code}: {response.text[:200]}"
    detail = (
        body.get("description")
        or (body.get("error") or {}).get("message")
        or str(body)[:200]
    )
    return f"HTTP {response.status_code}: {detail}"[:400]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CHANNELS: dict[str, Channel] = {
    c.id: c  # type: ignore[misc]
    for c in (
        WebPushChannel(),
        ManualChannel(),
        EmailChannel(),
        TelegramChannel(),
        WhatsAppChannel(),
    )
}


def channel(channel_id: str) -> Channel | None:
    return CHANNELS.get(channel_id)


def describe_channels() -> list[dict[str, Any]]:
    """What the UI shows next to each channel, in the shape /health uses."""
    return [
        {
            "id": c.id,
            "label": c.label,
            "broadcast": c.broadcast,
            "configured": c.is_configured,
            "detail": c.status_detail,
        }
        for c in CHANNELS.values()
    ]
