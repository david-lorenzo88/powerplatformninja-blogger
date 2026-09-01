"""WordPress integration.

Pushes a finished draft into powerplatformninja.com through the WordPress REST
API v2, as an **unpublished draft** by default. Authentication uses an
Application Password (WP Admin → Users → Profile → Application Passwords), which
is the supported way to call the REST API without a plugin.

The Markdown body is converted into Gutenberg block markup, so the post opens in
the block editor as real blocks (headings, lists, code, tables, quotes) rather
than one lump of classic HTML.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .models import CoverImage, Draft, PublishTarget
from .settings import ROOT, get_settings
from .util import strip_h1

logger = logging.getLogger("ppn.wordpress")

STATE_FILE = ROOT / ".ppn_state" / "wp_posts.json"
MEDIA_STATE_FILE = ROOT / ".ppn_state" / "wp_media.json"


class WordPressError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Markdown -> Gutenberg blocks
#
# This blog has no in-body images (rule S11 is a blocker on any image), so the
# converter no longer has an image-placeholder path: no ![alt](IMAGE:slug), no
# [SCREENSHOT: ...] normalisation, no empty core/image slot. The only image is
# the cover, uploaded separately and set as featured_media — never in the body.
# ---------------------------------------------------------------------------


def escape_code(raw: str) -> str:
    """Escape code exactly the way Gutenberg's core/code block does.

    Gutenberg validates a block by re-running its `save()` and comparing the
    markup. Anything we serialise differently is reported in the editor as
    "this block contains unexpected or invalid content" — so this must match
    core/code, not generic HTML escaping:

    * ``&``, ``<``, ``>`` are escaped
    * ``[`` becomes ``&#91;`` so shortcodes never execute inside code
    * **quotes are left alone** — `html.escape` turns them into `&quot;`, which
      is why a JSON block (nothing but quotes) failed validation on every line
    """
    return (
        raw.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("[", "&#91;")
    )


def markdown_to_blocks(markdown_body: str) -> str:
    """Convert Markdown into Gutenberg block markup."""
    import markdown as md
    from bs4 import BeautifulSoup, NavigableString

    rendered = md.markdown(
        markdown_body,
        extensions=["fenced_code", "tables", "sane_lists", "attr_list", "md_in_html"],
        output_format="html",
    )
    soup = BeautifulSoup(rendered, "html.parser")

    blocks: list[str] = []
    for node in soup.contents:
        if isinstance(node, NavigableString):
            if node.strip():
                blocks.append(_paragraph(str(node).strip()))
            continue
        blocks.append(_node_to_block(node))
    return "\n\n".join(b for b in blocks if b)


def _paragraph(inner_html: str, class_name: str | None = None) -> str:
    attrs = f' {{"className":"{class_name}"}}' if class_name else ""
    css = f' class="{class_name}"' if class_name else ""
    return f"<!-- wp:paragraph{attrs} -->\n<p{css}>{inner_html}</p>\n<!-- /wp:paragraph -->"


def _inner(node: Any) -> str:
    return "".join(str(c) for c in node.contents).strip()


def _node_to_block(node: Any) -> str:
    name = node.name

    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        # An H1 inside the body would clash with the post title; demote it.
        level = max(level, 2)
        return (
            f'<!-- wp:heading {{"level":{level}}} -->\n'
            f"<h{level}>{_inner(node)}</h{level}>\n"
            f"<!-- /wp:heading -->"
        )

    if name == "p":
        return _paragraph(_inner(node))

    if name in {"ul", "ol"}:
        ordered = name == "ol"
        attrs = ' {"ordered":true}' if ordered else ""
        items = []
        for li in node.find_all("li", recursive=False):
            items.append(f"<!-- wp:list-item -->\n<li>{_inner(li)}</li>\n<!-- /wp:list-item -->")
        body = "\n".join(items)
        return (
            f"<!-- wp:list{attrs} -->\n<{name}>\n{body}\n</{name}>\n<!-- /wp:list -->"
        )

    if name == "pre":
        code = node.find("code")
        language = ""
        if code is not None:
            for cls in code.get("class") or []:
                if cls.startswith("language-"):
                    language = cls.removeprefix("language-")
        raw = code.get_text() if code is not None else node.get_text()
        escaped = escape_code(raw.rstrip("\n"))
        # The language lives in the block delimiter only. core/code renders a
        # bare <code> with no class; adding one breaks block validation. The
        # attribute itself is inert in core and is picked up by the
        # Syntax-highlighting Code Block plugin if you install it.
        meta = ""
        if language and get_settings().wordpress.code_language_attribute:
            meta = f' {{"language":"{language}"}}'
        return (
            f"<!-- wp:code{meta} -->\n"
            f'<pre class="wp-block-code"><code>{escaped}</code></pre>\n'
            f"<!-- /wp:code -->"
        )

    if name == "blockquote":
        inner_blocks = "\n".join(_node_to_block(c) for c in node.find_all(recursive=False))
        if not inner_blocks:
            inner_blocks = _paragraph(_inner(node))
        return (
            '<!-- wp:quote -->\n<blockquote class="wp-block-quote">\n'
            f"{inner_blocks}\n</blockquote>\n<!-- /wp:quote -->"
        )

    if name == "table":
        return (
            "<!-- wp:table -->\n"
            f'<figure class="wp-block-table"><table>{_inner(node)}</table></figure>\n'
            "<!-- /wp:table -->"
        )

    if name == "hr":
        return '<!-- wp:separator -->\n<hr class="wp-block-separator"/>\n<!-- /wp:separator -->'

    if name in {"figure", "div", "section"}:
        return f"<!-- wp:html -->\n{node!s}\n<!-- /wp:html -->"

    return _paragraph(_inner(node))


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


class WordPressClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.cfg = settings.wordpress
        if not self.cfg.is_configured:
            raise WordPressError(
                "WordPress is not configured. Set WP_URL, WP_USERNAME and WP_APP_PASSWORD in .env."
            )
        token = base64.b64encode(
            f"{self.cfg.username}:{self.cfg.app_password.replace(' ', '')}".encode()
        ).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "User-Agent": "ppn-blogger/0.1",
        }

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.cfg.api_base,
            headers=self._headers,
            timeout=httpx.Timeout(45.0, connect=15.0),
            verify=self.cfg.verify_tls,
            follow_redirects=True,
        )

    async def verify_connection(self) -> dict[str, Any]:
        async with self._http() as http:
            resp = await http.get("/users/me", params={"context": "edit"})
        if resp.status_code == 401:
            raise WordPressError(
                "WordPress rejected the credentials (401). Check WP_USERNAME and that the "
                "Application Password is still valid."
            )
        if resp.status_code >= 400:
            raise WordPressError(f"WordPress /users/me returned {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def _resolve_term(self, taxonomy: str, name: str) -> int | None:
        """Find a category/tag by name, creating it if it does not exist."""
        async with self._http() as http:
            resp = await http.get(f"/{taxonomy}", params={"search": name, "per_page": "20"})
            if resp.status_code < 400:
                for term in resp.json():
                    if term.get("name", "").strip().lower() == name.strip().lower():
                        return int(term["id"])
            created = await http.post(f"/{taxonomy}", json={"name": name})
        if created.status_code < 400:
            return int(created.json()["id"])
        if created.status_code == 400:
            payload = created.json()
            existing = (payload.get("data") or {}).get("term_id")
            if existing:
                return int(existing)
        logger.warning("Could not resolve %s '%s': %s", taxonomy, name, created.text[:200])
        return None

    async def upload_media(
        self, path: Path, *, alt_text: str = "", title: str = "", strict: bool = False
    ) -> int | None:
        """Upload an image to the media library and return its id.

        Returns None rather than raising: a missing cover is not a reason to lose
        a finished post. ``strict`` inverts that for the one caller where it is
        wrong — an operator who pressed a button labelled "send this image to
        WordPress" is owed the reason it did not happen, not a silent no-op and a
        line in a log they cannot read.
        """
        if not path.exists():
            if strict:
                raise WordPressError(f"The cover image is not on disk: {path}")
            logger.warning("cover file missing, skipping upload: %s", path)
            return None
        data = path.read_bytes()
        headers = {
            **self._headers,
            "Content-Type": "image/png",
            "Content-Disposition": f'attachment; filename="{path.name}"',
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.cfg.api_base,
                headers=headers,
                timeout=httpx.Timeout(120.0, connect=15.0),
                verify=self.cfg.verify_tls,
                follow_redirects=True,
            ) as http:
                resp = await http.post("/media", content=data)
            if resp.status_code >= 400:
                logger.error("media upload failed (%s): %s", resp.status_code, resp.text[:300])
                if strict:
                    raise WordPressError(
                        f"WordPress rejected the image ({resp.status_code}): {resp.text[:300]}"
                    )
                return None
            media_id = int(resp.json()["id"])
        except WordPressError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("media upload failed: %s", exc)
            if strict:
                raise WordPressError(f"Uploading the image failed: {exc}") from exc
            return None

        if alt_text or title:
            try:
                async with self._http() as http:
                    await http.post(
                        f"/media/{media_id}",
                        json={"alt_text": alt_text, "title": title or alt_text},
                    )
            except Exception as exc:  # noqa: BLE001 - the image is already uploaded
                logger.warning("could not set alt text on media %s: %s", media_id, exc)

        logger.info("uploaded cover to media library: id=%s", media_id)
        return media_id

    async def ensure_media(
        self, slug: str, cover: CoverImage, *, title: str = "", strict: bool = False
    ) -> int | None:
        """The media id for this cover, uploading only when the bytes are new.

        Every publish carries the cover, and a publish is a button an operator
        presses several times on the same post — without this, each press would
        upload another identical PNG and the media library would fill with
        copies of the same artwork. The memo is keyed by the *content* digest,
        so regenerating the cover genuinely does upload a new image and the
        featured image changes, while re-publishing an unchanged one does not.
        """
        if cover.media_id:
            return cover.media_id
        path = Path(cover.path)
        if not path.exists():
            if strict:
                raise WordPressError(f"The cover image is not on disk: {path}")
            return None

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        remembered = _remembered_media(slug, digest)
        if remembered is not None:
            cover.media_id = remembered
            return remembered

        media_id = await self.upload_media(
            path, alt_text=cover.alt_text, title=title, strict=strict
        )
        if media_id:
            cover.media_id = media_id
            _remember_media(slug, digest, media_id)
        return media_id

    async def set_featured_media(self, post_id: int, media_id: int) -> dict[str, Any]:
        """Point an existing post at a media item, touching nothing else.

        Deliberately not a body update: the cover is the one thing being changed,
        and re-sending the content would overwrite whatever was edited in the
        WordPress editor since the draft was pushed.
        """
        async with self._http() as http:
            resp = await http.post(f"/posts/{post_id}", json={"featured_media": media_id})
        if resp.status_code >= 400:
            raise WordPressError(
                f"WordPress rejected the featured image ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()

    async def resolve_post_id(self, slug: str, post_id: int | None = None) -> int | None:
        """Which WordPress post this draft is, by id, then memory, then slug."""
        if post_id:
            return int(post_id)
        return _remembered_id(slug) or await self._find_by_slug(slug)

    async def _find_by_slug(self, slug: str) -> int | None:
        async with self._http() as http:
            resp = await http.get(
                "/posts",
                params={"slug": slug, "status": "draft,pending,publish,future,private",
                        "per_page": "1", "_fields": "id"},
            )
        if resp.status_code < 400 and resp.json():
            return int(resp.json()[0]["id"])
        return None

    async def upsert_draft(
        self,
        draft: Draft,
        *,
        status: str | None = None,
        cover: CoverImage | None = None,
    ) -> PublishTarget:
        status = status or self.cfg.default_status
        title, body_markdown = strip_h1(draft.markdown)
        title = draft.title or title
        content = markdown_to_blocks(body_markdown)

        category_id = await self._resolve_term("categories", draft.category) if draft.category else None
        tag_ids = []
        for tag in draft.tags[:8]:
            term_id = await self._resolve_term("tags", tag)
            if term_id:
                tag_ids.append(term_id)

        payload: dict[str, Any] = {
            "title": title,
            "slug": draft.slug,
            "status": status,
            "content": content,
            "excerpt": draft.excerpt or draft.meta_description,
            "meta": {},
            "comment_status": "open",
        }
        if category_id:
            payload["categories"] = [category_id]
        if tag_ids:
            payload["tags"] = tag_ids

        media_id: int | None = None
        if cover is not None and cover.ok and get_settings().cover.upload_to_wordpress:
            media_id = await self.ensure_media(draft.slug, cover, title=draft.title)
            if media_id:
                cover.media_id = media_id
                payload["featured_media"] = media_id

        post_id = _remembered_id(draft.slug) or await self._find_by_slug(draft.slug)

        async with self._http() as http:
            if post_id:
                resp = await http.post(f"/posts/{post_id}", json=payload)
                action = "updated"
            else:
                resp = await http.post("/posts", json=payload)
                action = "created"

        if resp.status_code >= 400:
            raise WordPressError(
                f"WordPress rejected the post ({resp.status_code}): {resp.text[:500]}"
            )

        data = resp.json()
        post_id = int(data["id"])
        _remember_id(draft.slug, post_id)
        logger.info("WordPress draft %s: id=%s slug=%s", action, post_id, draft.slug)

        return PublishTarget(
            platform="wordpress",
            post_id=post_id,
            status=data.get("status", status),
            link=data.get("link", ""),
            edit_link=f"{self.cfg.url}/wp-admin/post.php?post={post_id}&action=edit",
            featured_media_id=media_id,
        )


# ---------------------------------------------------------------------------
# Tiny slug -> post id memory so re-runs update instead of duplicating
# ---------------------------------------------------------------------------


def _load_state() -> dict[str, int]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _remembered_id(slug: str) -> int | None:
    return _load_state().get(slug)


def _remember_id(slug: str, post_id: int) -> None:
    state = _load_state()
    state[slug] = post_id
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _load_media_state() -> dict[str, dict[str, Any]]:
    if not MEDIA_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(MEDIA_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _remembered_media(slug: str, digest: str) -> int | None:
    """The media id already uploaded for exactly these bytes, if any.

    A separate file from ``wp_posts.json`` rather than a second value in it: that
    file is already on disk in every environment with a ``{slug: int}`` shape, and
    widening it in place would make an existing one unreadable. A miss here costs
    one duplicate upload, never a wrong image, which is why this can live in a
    file the container may lose rather than needing a database column.
    """
    entry = _load_media_state().get(slug)
    if isinstance(entry, dict) and entry.get("digest") == digest:
        media_id = entry.get("media_id")
        return int(media_id) if media_id else None
    return None


def _remember_media(slug: str, digest: str, media_id: int) -> None:
    state = _load_media_state()
    state[slug] = {"digest": digest, "media_id": int(media_id)}
    MEDIA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEDIA_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


async def push_draft(
    draft: Draft, *, status: str | None = None, cover: CoverImage | None = None
) -> PublishTarget:
    return await WordPressClient().upsert_draft(draft, status=status, cover=cover)


async def push_cover(
    draft: Draft, cover: CoverImage, *, post_id: int | None = None
) -> PublishTarget:
    """Send a cover to a post that already exists and make it the featured image.

    The counterpart to ``push_draft`` for the case the pipeline cannot cover: the
    art was regenerated after the post was pushed, so the words on WordPress are
    right and only the image is stale. Nothing but ``featured_media`` is written.

    Unlike the pipeline's push this **raises**. The doctrine that a WordPress
    failure must never sink a run is about work that would otherwise be lost;
    here there is nothing to lose and someone is waiting for an answer, so a
    failure has to be reportable rather than swallowed.
    """
    client = WordPressClient()
    target_id = await client.resolve_post_id(draft.slug, post_id)
    if target_id is None:
        raise WordPressError(
            f"No WordPress post found for '{draft.slug}'. Publish the draft first, "
            "then send the image."
        )

    media_id = await client.ensure_media(draft.slug, cover, title=draft.title, strict=True)
    if media_id is None:  # pragma: no cover - strict=True raises instead
        raise WordPressError("The cover could not be uploaded to the media library.")

    data = await client.set_featured_media(target_id, media_id)
    _remember_id(draft.slug, target_id)
    logger.info("featured image set: post=%s media=%s", target_id, media_id)

    return PublishTarget(
        platform="wordpress",
        post_id=target_id,
        status=data.get("status", ""),
        link=data.get("link", ""),
        edit_link=f"{client.cfg.url}/wp-admin/post.php?post={target_id}&action=edit",
        featured_media_id=media_id,
    )


def preview_blocks(markdown_path: Path) -> str:
    """Render a local markdown draft to Gutenberg markup without publishing."""
    _, body = strip_h1(markdown_path.read_text(encoding="utf-8"))
    return markdown_to_blocks(body)
