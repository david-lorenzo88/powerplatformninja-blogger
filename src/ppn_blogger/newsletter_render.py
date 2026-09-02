"""Turning a composed issue into the three shapes it has to travel in.

Markdown for reading and editing, HTML for email, plain text for everything
that cannot render HTML. All three are produced once and stored, so a re-send is
byte-identical to the first attempt.

``wordpress.py`` cannot be reused here even though it also converts Markdown:
it emits Gutenberg block comments (``<!-- wp:paragraph -->``), which are
meaningless in an inbox.

The HTML is built directly from the composed issue rather than by converting the
Markdown, and that is the interesting choice. The Markdown is the operator's
editing surface and may end up containing anything; the HTML has to stay inside
the narrow subset Outlook and Gmail both render. Generating them from the same
structured source keeps that guarantee without having to sanitise prose after
the fact — which is also why there is no ``markdown``, BeautifulSoup,
``premailer`` or ``css-inline`` in here. No parsing means nothing to sanitise.

Email is a genuinely different target from the web:

* Outlook renders through Word and ignores most CSS; several clients strip
  ``<style>`` blocks entirely. **Every rule is inlined on the element.**
* No flexbox and no grid. A single-column table at 640px is the shape that
  survives everywhere.
* Every URL must be absolute — a relative link is dead in an inbox.
* A plain-text alternative is expected; without one, spam filters penalise the
  message.

One trap worth stating: ``wordpress.escape_code`` deliberately leaves quotes
unescaped so Gutenberg's block re-validation passes. In an HTML attribute that
is an injection. Everything here uses ordinary ``html.escape``.
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlparse

# Inlined on every element, because a <style> block may not survive the trip.
STYLES: dict[str, str] = {
    "body": "margin:0;padding:0;background:#0f1115;",
    "wrap": "width:100%;background:#0f1115;padding:24px 12px;",
    "card": (
        "max-width:640px;margin:0 auto;background:#151821;border-radius:12px;"
        "padding:28px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
        "Helvetica,Arial,sans-serif;color:#dbe1ea;"
    ),
    "h1": "margin:0 0 4px;font-size:22px;line-height:1.25;color:#f2f5f9;font-weight:700;",
    "meta": "margin:0 0 20px;font-size:13px;color:#8b95a5;",
    "intro": "margin:0 0 24px;font-size:15px;line-height:1.6;color:#c3ccd8;",
    "h2": (
        "margin:28px 0 12px;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;"
        "font-weight:700;"
    ),
    "item": "margin:0 0 18px;padding:0 0 18px;border-bottom:1px solid #232838;",
    "item_last": "margin:0 0 4px;padding:0;",
    "headline": "margin:0 0 6px;font-size:16px;line-height:1.35;font-weight:600;",
    "link": "text-decoration:none;",
    "blurb": "margin:0 0 6px;font-size:14px;line-height:1.6;color:#aab4c2;",
    "source": "margin:0;font-size:12px;color:#7b8595;",
    "footer": "margin:28px 0 0;padding-top:18px;border-top:1px solid #232838;"
    "font-size:12px;line-height:1.6;color:#7b8595;",
}

MAX_PREHEADER = 140


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def render_markdown(issue: dict[str, Any], *, name: str = "") -> str:
    """The readable, editable form. This is what the operator edits before sending."""
    lines: list[str] = [f"# {issue.get('subject', '').strip() or name or 'Newsletter'}", ""]
    intro = (issue.get("intro") or "").strip()
    if intro:
        lines += [intro, ""]

    for section in issue.get("sections", []):
        lines += [f"## {section.get('title', section.get('id', ''))}", ""]
        for item in section.get("items", []):
            headline = (item.get("headline") or "").strip()
            url = (item.get("url") or "").strip()
            lines.append(f"### [{headline}]({url})" if url else f"### {headline}")
            blurb = (item.get("blurb") or "").strip()
            if blurb:
                lines.append(blurb)
            source = (item.get("source") or "").strip()
            published = (item.get("published") or "").strip()
            trailer = " · ".join(x for x in (source, published) if x)
            if trailer:
                lines.append(f"*{trailer}*")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Email HTML
# ---------------------------------------------------------------------------


def render_html(issue: dict[str, Any], *, name: str = "", footer: str = "", accent: str = "#c084fc") -> str:
    """A single-column table with every style inlined.

    Built directly rather than by converting the Markdown: the Markdown is the
    operator's editing surface and may contain anything, while this has to stay
    within the subset of HTML that Outlook and Gmail both render.
    """
    subject = esc(issue.get("subject", "").strip() or name or "Newsletter")
    parts: list[str] = [
        f'<body style="{STYLES["body"]}">',
        # Hidden preheader: what an inbox shows after the subject. Without it,
        # clients scrape whatever text comes first, which is usually the title
        # again.
        _preheader_block(issue),
        f'<div style="{STYLES["wrap"]}">',
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">',
        f'<tr><td><div style="{STYLES["card"]}">',
        f'<h1 style="{STYLES["h1"]}">{subject}</h1>',
    ]

    meta = " · ".join(
        x for x in (esc(name), esc(issue.get("generated_on", "")), _count_label(issue)) if x
    )
    if meta:
        parts.append(f'<p style="{STYLES["meta"]}">{meta}</p>')

    intro = (issue.get("intro") or "").strip()
    if intro:
        parts.append(f'<p style="{STYLES["intro"]}">{esc(intro)}</p>')

    for section in issue.get("sections", []):
        title = esc(section.get("title", section.get("id", "")))
        parts.append(f'<h2 style="{STYLES["h2"]}color:{esc(accent)};">{title}</h2>')
        items = section.get("items", [])
        for index, item in enumerate(items):
            last = index == len(items) - 1
            parts.append(_item_block(item, accent=accent, last=last))

    if footer:
        parts.append(f'<p style="{STYLES["footer"]}">{esc(footer)}</p>')

    parts += ["</div></td></tr></table>", "</div>", "</body>"]
    return "\n".join(parts)


def _item_block(item: dict[str, Any], *, accent: str, last: bool) -> str:
    style = STYLES["item_last"] if last else STYLES["item"]
    headline = esc((item.get("headline") or "").strip())
    url = absolute(item.get("url", ""))
    blurb = esc((item.get("blurb") or "").strip())
    trailer = esc(
        " · ".join(
            x for x in ((item.get("source") or "").strip(), (item.get("published") or "").strip()) if x
        )
    )

    if url:
        title_html = (
            f'<a href="{esc(url)}" style="{STYLES["link"]}color:{esc(accent)};">{headline}</a>'
        )
    else:
        title_html = headline

    block = [f'<div style="{style}">', f'<p style="{STYLES["headline"]}">{title_html}</p>']
    if blurb:
        block.append(f'<p style="{STYLES["blurb"]}">{blurb}</p>')
    if trailer:
        block.append(f'<p style="{STYLES["source"]}">{trailer}</p>')
    block.append("</div>")
    return "\n".join(block)


def _preheader_block(issue: dict[str, Any]) -> str:
    text = (issue.get("preheader") or issue.get("intro") or "").strip()[:MAX_PREHEADER]
    if not text:
        return ""
    return (
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        f'mso-hide:all;">{esc(text)}</div>'
    )


def _count_label(issue: dict[str, Any]) -> str:
    n = sum(len(s.get("items", [])) for s in issue.get("sections", []))
    return f"{n} item{'' if n == 1 else 's'}" if n else ""


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


def render_text(issue: dict[str, Any], *, name: str = "", footer: str = "") -> str:
    """The alternative part. Every URL appears in full — nothing is hidden behind a link."""
    out: list[str] = [issue.get("subject", "").strip() or name or "Newsletter", ""]
    intro = (issue.get("intro") or "").strip()
    if intro:
        out += [intro, ""]

    for section in issue.get("sections", []):
        out += [(section.get("title") or section.get("id", "")).upper(), ""]
        for item in section.get("items", []):
            out.append(f"* {(item.get('headline') or '').strip()}")
            blurb = (item.get("blurb") or "").strip()
            if blurb:
                out.append(f"  {blurb}")
            url = (item.get("url") or "").strip()
            if url:
                out.append(f"  {url}")
            out.append("")

    if footer:
        out += ["--", footer]
    return "\n".join(out).rstrip() + "\n"


def render_short(issue: dict[str, Any], *, limit: int = 4000, name: str = "") -> str:
    """A chat-sized digest, for channels with a hard message length.

    Telegram caps a message at 4096 characters and WhatsApp templates are far
    shorter, so this carries headline + link only and truncates honestly rather
    than silently losing the tail.

    **HTML, and every interpolated value escaped.** This used to emit Markdown —
    `*subject*` and the headline raw — and it cost a real send: Telegram replied
    *400 Bad Request: can't parse entities* on a headline containing an
    unbalanced `_`, the delivery was recorded as a permanent failure, and the
    recipient was parked. The template being ours was never the point; the
    headlines in it come from somebody else's feed and are arbitrary text.

    Telegram's HTML mode is the safe one because its escaping is total and
    well-defined — `&`, `<`, `>` and nothing else — where legacy Markdown has no
    escape for a lone `*` at all. Tags are opened and closed within a single
    line so that truncating on a line boundary can never split one.
    """
    lines = [f"<b>{esc(issue.get('subject', '').strip() or name)}</b>", ""]
    total = 0
    shown = 0
    for section in issue.get("sections", []):
        for item in section.get("items", []):
            entry = (
                f"• {esc((item.get('headline') or '').strip())}\n"
                f"{esc((item.get('url') or '').strip())}"
            )
            # Measured after escaping: `&amp;` is what the API counts, not `&`.
            if total + len(entry) > limit - 80:
                break
            lines.append(entry)
            total += len(entry)
            shown += 1
        else:
            continue
        break

    remaining = sum(len(s.get("items", [])) for s in issue.get("sections", [])) - shown
    if remaining > 0:
        lines.append(f"\n…and {remaining} more.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def esc(value: str) -> str:
    """Ordinary escaping, quotes included.

    Explicitly not ``wordpress.escape_code``, which leaves quotes alone so that
    Gutenberg's block re-validation passes. Inside an HTML attribute that is an
    injection, and every link here goes into one.
    """
    return html.escape(str(value or ""), quote=True)


def absolute(url: str) -> str:
    """Drop anything that is not an absolute http(s) URL.

    A relative link is dead in an inbox, and a `javascript:` one in an email
    client is a problem rather than a broken link.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlparse(raw)
    except ValueError:
        return ""
    return raw if parts.scheme in {"http", "https"} and parts.netloc else ""


_WS = re.compile(r"[ \t]+")


def render_all(
    issue: dict[str, Any], *, name: str = "", settings: Any = None
) -> dict[str, str]:
    """All three renderings at once, with brand and footer from config."""
    render_cfg = settings.newsletter_render if settings is not None else {}
    accent = str(render_cfg.get("brand_colour", "#c084fc"))
    footer = _WS.sub(" ", str(render_cfg.get("footer", "") or "")).strip()
    return {
        "markdown": render_markdown(issue, name=name),
        "html": render_html(issue, name=name, footer=footer, accent=accent),
        "text_body": render_text(issue, name=name, footer=footer),
        "short": render_short(issue, name=name),
    }
