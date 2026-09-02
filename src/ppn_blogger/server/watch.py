"""Telling you when a watched source publishes.

RSS has no push, so "real-time" here means "polled at fifteen minutes". WebSub
would be genuinely instant and would let the database keep auto-pausing, but it
needs a public unauthenticated callback endpoint, which conflicts with Easy Auth
covering the whole app — and almost none of these feeds advertise a hub. It is
the right answer if this ever becomes load-bearing.

Three properties matter more than the mechanics:

**Notify once, structurally.** The unique ``(feed_id, entry_key)`` index means an
entry republished after an edit cannot create a second row, so it cannot produce
a second notification. This module never has to remember what it sent; it only
has to ask which articles have no ``notified_at``.

**Stamp before sending.** ``notified_at`` is written before the push goes out, so
a crash mid-send costs a missed notification rather than a duplicate one. That is
the right way round: a phone buzzing twice at 3am for the same article is worse
than not buzzing at all, and the article is still in the stream either way.

**A noisy feed cannot spam.** Caps per feed and per tick collapse a flood into a
single summary. Quiet hours suppress the send *without* stamping, so nothing is
lost — the first tick after they end rolls the backlog up under the same caps.

A watched feed can also be **relayed to Telegram**, one message per article,
which is the un-composed counterpart to a newsletter: no model is called, no
issue is stored, and the message carries the headline and the link exactly as
the feed gave them. It rides on the same set of un-notified articles rather than
on a column of its own, so "watched" keeps its single meaning — tell me when
this publishes — and the same stamp covers both announcements. Which feeds are
watched is therefore also which feeds are relayed.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select, true, update

from ..settings import get_settings
from .db import Article, Feed, as_utc, session, utcnow

logger = logging.getLogger("ppn.server.watch")


def parse_quiet_hours(value: str) -> tuple[time, time] | None:
    """``"22:00-07:00"`` -> (start, end). Invalid or empty means no quiet hours."""
    raw = (value or "").strip()
    if not raw or "-" not in raw:
        return None
    start_text, _, end_text = raw.partition("-")
    try:
        start = time.fromisoformat(start_text.strip())
        end = time.fromisoformat(end_text.strip())
    except ValueError:
        logger.warning("ignoring unparseable PPN_REALTIME_QUIET_HOURS: %r", value)
        return None
    return start, end


def in_quiet_hours(now: datetime, window: tuple[time, time] | None) -> bool:
    if window is None:
        return False
    start, end = window
    moment = now.time()
    if start == end:
        return False
    if start < end:  # e.g. 01:00-06:00, inside one day
        return start <= moment < end
    return moment >= start or moment < end  # e.g. 22:00-07:00, across midnight


def _local_now() -> datetime:
    """Now in the operator's timezone, for quiet hours.

    `zoneinfo` needs a timezone database, and `python:3.11-slim` has none — the
    `tzdata` package in the server extra is what supplies it. A missing or
    misspelled zone falls back to UTC rather than raising: getting a notification
    an hour outside quiet hours is a far smaller failure than a scheduler tick
    that dies.
    """
    from zoneinfo import ZoneInfo

    name = get_settings().scheduler.timezone
    try:
        return utcnow().astimezone(ZoneInfo(name))
    except Exception:  # noqa: BLE001
        logger.warning("unknown timezone %r — using UTC for quiet hours", name)
        return utcnow()


async def pending_articles() -> list[tuple[Feed, list[Article]]]:
    """Un-notified articles from watched feeds, newest first, grouped by feed."""
    async with session() as s:
        rows = (
            await s.execute(
                select(Article, Feed)
                .join(Feed, Feed.id == Article.feed_id)
                .where(
                    Feed.realtime == true(),
                    Feed.enabled == true(),
                    Article.notified_at.is_(None),
                )
                .order_by(Article.fetched_at.desc(), Article.id.desc())
            )
        ).all()

    grouped: dict[int, tuple[Feed, list[Article]]] = {}
    for article, feed in rows:
        grouped.setdefault(feed.id, (feed, []))[1].append(article)
    return list(grouped.values())


async def notify_new_articles() -> int:
    """Announce anything new from watched feeds. Returns notifications sent.

    Never raises: this is called from a scheduler tick, and a push service having
    a bad afternoon must not stop feeds being polled.
    """
    try:
        return await _notify()
    except Exception:  # noqa: BLE001
        logger.exception("watch notification failed")
        return 0


async def _notify() -> int:
    from . import push

    settings = get_settings().news
    batches = await pending_articles()
    if not batches:
        return 0

    batches, stale = _drop_backlog(batches, settings.realtime_max_age_hours)
    if stale:
        # Stamped, not announced, and stamped even during quiet hours: it is not
        # a backlog worth holding. Marking a feed watched must not replay its
        # entire history into a chat.
        await _mark_notified([a.id for a in stale])
        logger.info("watch: %d article(s) older than %dh marked read, not announced",
                    len(stale), settings.realtime_max_age_hours)
    if not batches:
        return 0

    total = sum(len(articles) for _, articles in batches)

    if in_quiet_hours(_local_now(), parse_quiet_hours(settings.quiet_hours)):
        # Deliberately no stamping: the backlog is rolled up by the first tick
        # after the window ends, under the same caps.
        logger.info("quiet hours — holding %d article(s) from %d feed(s)", total, len(batches))
        return 0

    # Everything about to be announced is stamped first, in one statement,
    # whatever shape the announcement takes — the relay included.
    await _mark_notified([a.id for _, articles in batches for a in articles])

    # Before the push, and in its own guard: the two announcements are
    # independent, and a push service having a bad afternoon must not cost the
    # relay its articles, which are stamped either way.
    relayed = 0
    try:
        relayed = await relay_to_telegram(batches)
    except Exception:  # noqa: BLE001
        logger.exception("telegram relay failed")

    if len(batches) > settings.realtime_max_per_tick:
        # More feeds than notifications allowed: one line for the lot beats
        # silently dropping the tail.
        sent = await push.notify(
            "New across your feeds",
            f"{total} new item{'' if total == 1 else 's'} from {len(batches)} sources",
            "/articles",
            "ppn-watch",
        )
        logger.info("watch: rolled %d article(s) into one notification", total)
        return relayed + (1 if sent else 0)

    sent_count = 0
    for feed, articles in batches:
        name = feed.name or feed.title or feed.home_domain or "A feed"
        tag = f"ppn-feed-{feed.id}"
        if len(articles) > settings.realtime_max_per_feed:
            title, body = name, f"{len(articles)} new items"
        else:
            # One notification per feed regardless, with the headlines in the
            # body — a phone that buzzes three times for one source is a phone
            # whose notifications get turned off.
            title = name
            body = " · ".join(a.title or a.url for a in articles)[:300]
        if await push.notify(title, body, f"/articles?feed={feed.id}", tag):
            sent_count += 1

    logger.info("watch: %d article(s) announced from %d feed(s)", total, len(batches))
    return relayed + sent_count


def _drop_backlog(
    batches: list[tuple[Feed, list[Article]]], max_age_hours: int
) -> tuple[list[tuple[Feed, list[Article]]], list[Article]]:
    """Split what is new from what was merely never announced.

    Measured on ``fetched_at`` — when this system first saw the article — rather
    than on the feed's own published date, which is frequently missing and
    occasionally a lie. A brand-new feed's first poll is therefore all "new",
    which is right: nobody has seen it here before.
    """
    if max_age_hours <= 0:
        return batches, []
    cutoff = utcnow() - timedelta(hours=max_age_hours)
    fresh: list[tuple[Feed, list[Article]]] = []
    stale: list[Article] = []
    for feed, articles in batches:
        keep = []
        for article in articles:
            seen = as_utc(article.fetched_at)
            (keep if seen is None or seen >= cutoff else stale).append(article)
        if keep:
            fresh.append((feed, keep))
    return fresh, stale


async def relay_to_telegram(batches: list[tuple[Feed, list[Article]]]) -> int:
    """Post every new article to the relay chats. Returns messages sent.

    One message per article, because that is what was asked for: a raw feed, not
    a digest. The cap is a throttle rather than a filter — Telegram limits a
    group to roughly twenty messages a minute, and a feed's first poll can carry
    a hundred articles — so anything past it travels as a single digest message.
    Nothing is dropped, and the count of what was rolled up is stated.

    Never raises. Every failure is a logged line, because the articles are
    already stamped and a retry would be a duplicate rather than a repair.
    """
    settings = get_settings().telegram
    if not settings.relays:
        return 0

    items = [(feed, article) for feed, articles in batches for article in articles]
    cap = max(1, settings.relay_max_per_tick)
    head, tail = items[:cap], items[cap:]

    sent = 0
    for chat_id in settings.relay_chat_ids:
        for feed, article in head:
            if await _relay_one(chat_id, _article_text(feed, article)):
                sent += 1
        if tail and await _relay_one(chat_id, _digest_text(tail)):
            sent += 1

    logger.info(
        "relay: %d article(s) to %d chat(s)%s",
        len(items),
        len(settings.relay_chat_ids),
        f", {len(tail)} of them as one digest" if tail else "",
    )
    return sent


async def _relay_one(chat_id: str, text: str) -> bool:
    from .channels import send_telegram

    result = await send_telegram(chat_id, text)
    if not result.ok:
        logger.warning("relay to %s failed: %s", chat_id, result.error)
    return result.ok


def _article_text(feed: Feed, article: Article) -> str:
    """Plain text, never Markdown — see ``channels.send_telegram``."""
    source = feed.name or feed.title or feed.home_domain or article.domain
    title = (article.title or "").strip() or article.url
    lines = [title]
    if source:
        lines.append(source)
    lines.append(article.url)
    return "\n".join(lines)


def _digest_text(items: list[tuple[Feed, Article]]) -> str:
    """The overflow, as one message that says how much it is carrying."""
    lines = [f"{len(items)} more new articles:", ""]
    for _, article in items:
        entry = f"• {(article.title or '').strip() or article.url}\n{article.url}"
        # Truncating honestly beats a 4096-character wall the API would refuse.
        if sum(len(line) + 1 for line in lines) + len(entry) > 3900:
            lines.append(f"…and {len(items) - (len(lines) - 2)} not listed.")
            break
        lines.append(entry)
    return "\n".join(lines)


async def _mark_notified(article_ids: list[int]) -> None:
    if not article_ids:
        return
    now = utcnow()
    async with session() as s:
        await s.execute(
            update(Article).where(Article.id.in_(article_ids)).values(notified_at=now)
        )
        await s.commit()


async def unnotified_count() -> int:
    batches = await pending_articles()
    return sum(len(articles) for _, articles in batches)


def next_quiet_boundary(now: datetime, window: tuple[time, time] | None) -> Any:
    """Exposed for the schedule view: when quiet hours next start or end."""
    if window is None:
        return None
    start, end = window
    return end if in_quiet_hours(now, window) else start


__all__ = [
    "in_quiet_hours",
    "notify_new_articles",
    "parse_quiet_hours",
    "pending_articles",
    "relay_to_telegram",
    "unnotified_count",
]
