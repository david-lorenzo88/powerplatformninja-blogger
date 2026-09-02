"""Database-backed configuration, with history.

Config used to be YAML files under ``config/``. Moving it into the database is
what lets the UI edit trusted sources, watch areas and validation rules — but it
costs you ``git log`` on rule changes. Every write is therefore a **new version
row**, never an update, so you keep an audit trail, can diff two versions and can
roll back, in-app.

On first start the existing YAML files are imported as version 1, so nothing is
lost in the move.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml
from sqlalchemy import select

from ..config_source import (
    DOCUMENTS,
    MappingConfigSource,
    config_stamp,
    filename_for,
    set_config_source,
)
from ..settings import CONFIG_DIR
from .db import ConfigDocument, session, utcnow

logger = logging.getLogger("ppn.server.config")


def _parse(document: ConfigDocument) -> Any:
    if document.format == "markdown":
        return document.content
    return yaml.safe_load(document.content) or {}


async def latest_versions() -> dict[str, ConfigDocument]:
    """The newest row for each document name."""
    async with session() as s:
        rows = (
            await s.execute(
                select(ConfigDocument).order_by(
                    ConfigDocument.name, ConfigDocument.version.desc()
                )
            )
        ).scalars()
        newest: dict[str, ConfigDocument] = {}
        for row in rows:
            newest.setdefault(row.name, row)
        return newest


def _file_text(name: str) -> str:
    """The config file exactly as written, comments and all.

    Deliberately not ``yaml.safe_dump(get_mapping(name))``: half of
    ``sources.yaml`` and nearly all of ``validation_rules.yaml`` is explanation,
    and a round-trip through the parser throws every line of it away — which
    then shows up as an unreadable document in the Config screen. The file is
    already valid YAML; ``save_document`` validates it again on the way in.
    """
    path = CONFIG_DIR / filename_for(name)
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def seed_from_yaml_if_empty() -> bool:
    """Import ``config/*.yaml`` as version 1 the first time the server runs."""
    existing = await latest_versions()
    if existing:
        return False

    async with session() as s:
        for name, fmt in DOCUMENTS.items():
            content = _file_text(name)
            s.add(
                ConfigDocument(
                    name=name,
                    format=fmt,
                    content=content,
                    version=1,
                    note="Imported from config/ on first start",
                    created_at=utcnow(),
                )
            )
        await s.commit()
    logger.info("seeded %d config documents from %s", len(DOCUMENTS), CONFIG_DIR)
    return True


async def reimport_from_yaml(note: str = "Re-imported from config/") -> list[tuple[str, int]]:
    """Push the current ``config/`` files into the DB as new versions.

    ``seed_from_yaml_if_empty`` only fires on the very first start, so once the
    database is authoritative a git-only config swap never reaches a running
    server. ``ppn config reload`` calls this to append each file as a new
    version, so the new editorial ruleset takes effect without wiping history.
    """
    out: list[tuple[str, int]] = []
    for name in DOCUMENTS:
        row = await save_document(name, _file_text(name), note=note)
        out.append((name, row.version))
    return out


async def save_document(name: str, content: str, note: str = "") -> ConfigDocument:
    """Append a new version. Validates YAML before accepting it."""
    if name not in DOCUMENTS:
        raise KeyError(f"Unknown config document: {name}")
    fmt = DOCUMENTS[name]

    if fmt == "yaml":
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            # Refuse to store config the agents could not read. A YAML typo used
            # to break the next run; here it is caught at the point of editing.
            raise ValueError(f"Invalid YAML: {exc}") from exc
        if parsed is not None and not isinstance(parsed, dict):
            raise ValueError("Document must parse to a mapping.")

    current = (await latest_versions()).get(name)
    next_version = (current.version + 1) if current else 1

    async with session() as s:
        row = ConfigDocument(
            name=name,
            format=fmt,
            content=content,
            version=next_version,
            note=note[:400],
            created_at=utcnow(),
        )
        s.add(row)
        await s.commit()

    await refresh_active_source()
    logger.info("config %s saved as version %d", name, next_version)
    return row


async def history(name: str, limit: int = 50) -> list[ConfigDocument]:
    async with session() as s:
        rows = await s.execute(
            select(ConfigDocument)
            .where(ConfigDocument.name == name)
            .order_by(ConfigDocument.version.desc())
            .limit(limit)
        )
        return list(rows.scalars())


async def get_version(name: str, version: int) -> ConfigDocument | None:
    async with session() as s:
        row = await s.execute(
            select(ConfigDocument).where(
                ConfigDocument.name == name, ConfigDocument.version == version
            )
        )
        return row.scalar_one_or_none()


async def rollback(name: str, version: int) -> ConfigDocument:
    """Roll back by appending the old content as a new version.

    History is never rewritten, so a rollback is itself auditable.
    """
    target = await get_version(name, version)
    if target is None:
        raise KeyError(f"{name} has no version {version}")
    return await save_document(name, target.content, note=f"Rolled back to version {version}")


# ---------------------------------------------------------------------------
# Wiring the database into Settings
# ---------------------------------------------------------------------------

_source = MappingConfigSource()


async def refresh_active_source() -> MappingConfigSource:
    """Load the newest version of every document and publish it to Settings.

    The token doubles as the stamp recorded on every run, so there is one name
    for a configuration state rather than two that can drift. It used to be
    ``name:version|...``, which is 112 characters against a String(64) column and
    was stored truncated — see ``config_source.config_stamp``.
    """
    newest = await latest_versions()
    documents = {name: _parse(row) for name, row in newest.items()}
    token = config_stamp({name: row.version for name, row in newest.items()})
    _source.replace(documents, token)
    set_config_source(_source)
    return _source


def active_source() -> MappingConfigSource:
    return _source
