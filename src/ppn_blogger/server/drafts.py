"""Reading the artefacts on disk for the review screens."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import Draft
from ..settings import get_settings
from ..storage import split_front_matter
from ..util import slugify, strip_h1, word_count


def _dir() -> Path:
    return get_settings().run.output_dir


def list_drafts() -> list[dict[str, Any]]:
    directory = _dir()
    if not directory.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.md"), reverse=True):
        if path.name.endswith(".review.md"):
            continue
        front, body = split_front_matter(path.read_text(encoding="utf-8"))
        # Only files the pipeline wrote are drafts. Anything else that happens to
        # be a .md in this directory (a topics shortlist, a stray note) has no
        # draft front matter and must not appear as a reviewable post.
        if not front.get("slug") and not front.get("title"):
            continue
        review_path = path.with_suffix("").with_suffix(".review.md")
        cover = directory / "covers" / f"{front.get('slug', path.stem)}.png"
        items.append(
            {
                "file": path.name,
                "title": front.get("title") or strip_h1(body)[0] or path.stem,
                "slug": front.get("slug", ""),
                "language": front.get("language", get_settings().language),
                "translation_of": front.get("translation_of", ""),
                "category": front.get("category", ""),
                "tags": front.get("tags", []) or [],
                "word_count": front.get("word_count") or word_count(body),
                "read_minutes": front.get("read_minutes"),
                "generated": front.get("generated", ""),
                "review": front.get("review", {}),
                "has_review": review_path.exists(),
                "has_cover": cover.exists(),
            }
        )
    return items


def read_draft(name: str) -> dict[str, Any]:
    path = _safe_path(name)
    front, body = split_front_matter(path.read_text(encoding="utf-8"))
    review_path = path.with_suffix("").with_suffix(".review.md")
    return {
        "file": path.name,
        "front_matter": front,
        "markdown": body,
        "review": review_path.read_text(encoding="utf-8") if review_path.exists() else "",
    }


def write_draft(name: str, markdown: str) -> None:
    """Save an edited body, preserving the front matter."""
    path = _safe_path(name)
    front_text = path.read_text(encoding="utf-8").split("---", 2)
    if len(front_text) >= 3:
        path.write_text(f"---{front_text[1]}---\n\n{markdown.strip()}\n", encoding="utf-8")
    else:
        path.write_text(markdown, encoding="utf-8")


def cover_path(name: str) -> Path | None:
    path = _safe_path(name)
    front, _ = split_front_matter(path.read_text(encoding="utf-8"))
    candidate = _dir() / "covers" / f"{front.get('slug', path.stem)}.png"
    return candidate if candidate.exists() else None


def draft_to_model(path: Path, concept: str = "") -> Draft:
    """Rebuild a Draft from a saved markdown file."""
    if not path.is_absolute():
        path = _dir() / path.name
    front, body = split_front_matter(path.read_text(encoding="utf-8"))
    title, _ = strip_h1(body)
    words = front.get("word_count") or word_count(body)
    settings = get_settings()
    wpm = int(settings.structure.get("reading_speed_wpm", 200)) or 200
    return Draft(
        title=front.get("title") or title or path.stem,
        slug=front.get("slug") or slugify(front.get("title") or title or path.stem),
        meta_description=front.get("description", ""),
        primary_keyword=front.get("primary_keyword", ""),
        category=front.get("category", ""),
        tags=front.get("tags", []) or [],
        post_format=front.get("post_format", "analysis"),
        markdown=body,
        cover_concept=concept or front.get("cover_concept", ""),
        word_count=words,
        read_minutes=front.get("read_minutes") or max(1, round(words / wpm)),
    )


def _safe_path(name: str) -> Path:
    """Resolve a draft filename, refusing anything outside drafts/."""
    directory = _dir().resolve()
    path = (directory / Path(name).name).resolve()
    if directory not in path.parents and path.parent != directory:
        raise ValueError("Path escapes the drafts directory.")
    if not path.exists():
        raise FileNotFoundError(name)
    return path
