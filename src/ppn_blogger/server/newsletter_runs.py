"""One issue, end to end: candidates in, a stored issue out.

The join between the pure graph in ``workflows.py`` and the database. Kept apart
from ``newsletters.py`` so that module stays free of workflow imports and can be
read as plain storage.

Ordering is the only subtle thing here, and it follows ``DossierGate``: the issue
is written **before** the run reports success and before anything is sent. An
issue costs real model calls, and a delivery failure, a cancelled run or a
process dying must never be able to destroy one.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..settings import get_settings
from . import newsletters
from .db import utcnow

logger = logging.getLogger("ppn.server.newsletter")


async def compose_and_store(
    newsletter_id: int,
    *,
    run_id: str = "",
    instruction: str = "",
    on_event: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Generate one issue and file it. Returns the run result."""
    from .. import newsletter_render as render
    from ..executors import NewsletterCandidate
    from ..workflows import compose_issue

    moment = now or utcnow()
    newsletter = await newsletters.get(newsletter_id)
    if newsletter is None:
        raise KeyError(f"No newsletter {newsletter_id}")

    material = await newsletters.candidates(newsletter_id, now=moment)
    window_from = _parse(material["window_from"])
    window_to = _parse(material["window_to"])
    rows = material["candidates"]

    logger.info(
        "'%s': %d candidate(s) in the window, %d already used",
        newsletter["name"],
        len(rows),
        material.get("already_used", 0),
    )

    candidates = [
        NewsletterCandidate(
            id=int(r["id"]),
            title=r["title"],
            url=r["url"],
            source=r["source"],
            published=r["published"],
            summary=r["summary"],
        )
        for r in rows
    ]

    composed = await compose_issue(
        newsletter,
        candidates,
        window_from=material["window_from"][:10],
        window_to=material["window_to"][:10],
        instruction=instruction,
        on_event=on_event,
    )

    payload = {
        "subject": composed.subject,
        "preheader": composed.preheader,
        "intro": composed.intro,
        "sections": composed.sections,
        "article_ids": composed.article_ids,
        "generated_on": composed.generated_on,
        "skipped_reason": composed.skipped_reason,
    }

    if composed.skipped_reason:
        # Recorded rather than dropped: a quiet week should be visible as a
        # decision the system made, not as an issue that mysteriously never
        # appeared.
        issue = await newsletters.save_issue(
            newsletter_id,
            payload,
            {"markdown": "", "html": "", "text_body": ""},
            run_id=run_id,
            window_from=window_from,
            window_to=window_to,
            status="skipped",
        )
        logger.info("no issue this time: %s", composed.skipped_reason)
        return {
            "newsletter_id": newsletter_id,
            "issue_id": issue["id"],
            "skipped": True,
            "reason": composed.skipped_reason,
            "item_count": 0,
            "candidates": len(candidates),
        }

    rendered = render.render_all(payload, name=newsletter["name"], settings=get_settings())
    issue = await newsletters.save_issue(
        newsletter_id,
        payload,
        rendered,
        run_id=run_id,
        window_from=window_from,
        window_to=window_to,
        status="draft",
    )

    if composed.dropped:
        logger.warning(
            "%d item(s) the editor named were not in the candidate list and were dropped",
            len(composed.dropped),
        )

    return {
        "newsletter_id": newsletter_id,
        "issue_id": issue["id"],
        "number": issue["number"],
        "subject": issue["subject"],
        "item_count": issue["item_count"],
        "candidates": len(candidates),
        "dropped": len(composed.dropped),
        "omitted": len(composed.omitted),
        "skipped": False,
    }


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
