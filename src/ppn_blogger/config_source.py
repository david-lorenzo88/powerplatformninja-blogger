"""Where configuration comes from.

The agents read config through ``Settings`` (``settings.blog_profile``,
``settings.structure``, ``settings.style_guide``, ...). This module makes the
*source* of those documents swappable without a single prompt or agent knowing
about it:

* ``YamlConfigSource`` — the files under ``config/``. Default, used by the CLI
  and the tests, and the behaviour the project shipped with.
* the server installs a database-backed source instead, so config can be edited
  from the UI and versioned per change.

Both return exactly the same shapes, so nothing downstream changes.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Protocol

import yaml

# The documents that make up a configuration. `.md` documents are returned as
# text; `.yaml` documents as parsed dicts.
DOCUMENTS: dict[str, str] = {
    "blog_profile": "yaml",
    "topics": "yaml",
    "sources": "yaml",
    "validation_rules": "yaml",
    "newsletters": "yaml",
    "model_prices": "yaml",
    "style_guide": "markdown",
}


def filename_for(name: str) -> str:
    return f"{name}.md" if DOCUMENTS.get(name) == "markdown" else f"{name}.yaml"


class ConfigSource(Protocol):
    """Anything that can hand back the five configuration documents."""

    def get_mapping(self, name: str) -> dict[str, Any]:
        """Return a parsed YAML document. Empty dict when absent."""
        ...

    def get_text(self, name: str) -> str:
        """Return a text document (the style guide). Empty string when absent."""
        ...

    def version_token(self) -> str:
        """Opaque token that changes whenever any document changes.

        ``Settings`` uses it to know when its cache is stale, so a config edit in
        the UI takes effect on the next run without a restart.
        """
        ...


class YamlConfigSource:
    """Reads ``config/*.yaml`` and ``config/*.md`` from disk."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, name: str) -> Path:
        return self.directory / filename_for(name)

    def get_mapping(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        if not path.exists():
            raise FileNotFoundError(f"Missing config file: {path}")
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def get_text(self, name: str) -> str:
        path = self._path(name)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def version_token(self) -> str:
        stamps = []
        for name in DOCUMENTS:
            path = self._path(name)
            stamps.append(str(path.stat().st_mtime_ns) if path.exists() else "0")
        return "|".join(stamps)


class MappingConfigSource:
    """In-memory documents. Used by the DB store and by tests."""

    def __init__(self, documents: dict[str, Any] | None = None, version: str = "0") -> None:
        self._documents: dict[str, Any] = dict(documents or {})
        self._version = version

    def replace(self, documents: dict[str, Any], version: str) -> None:
        self._documents = dict(documents)
        self._version = version

    def get_mapping(self, name: str) -> dict[str, Any]:
        value = self._documents.get(name)
        return dict(value) if isinstance(value, dict) else {}

    def get_text(self, name: str) -> str:
        value = self._documents.get(name)
        return value if isinstance(value, str) else ""

    def version_token(self) -> str:
        return self._version


# ---------------------------------------------------------------------------
# Process-wide active source
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_active: ConfigSource | None = None


def set_config_source(source: ConfigSource | None) -> None:
    """Install the source every ``Settings`` instance reads from.

    Passing ``None`` restores the default YAML files.
    """
    global _active
    with _lock:
        _active = source


def get_config_source() -> ConfigSource:
    global _active
    if _active is None:
        from .settings import CONFIG_DIR

        with _lock:
            if _active is None:
                _active = YamlConfigSource(CONFIG_DIR)
    return _active
