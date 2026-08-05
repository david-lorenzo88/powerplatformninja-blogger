"""Configuration loading: .env + the YAML/Markdown files under config/."""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Repo root = three levels up from this file (src/ppn_blogger/settings.py)
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _load_yaml(name: str) -> dict[str, Any]:
    """Kept for callers that want a file read regardless of the active source."""
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(slots=True)
class FoundrySettings:
    project_endpoint: str = field(default_factory=lambda: _env("FOUNDRY_PROJECT_ENDPOINT"))
    model: str = field(default_factory=lambda: _env("FOUNDRY_MODEL", "gpt-5"))
    fast_model: str = field(default_factory=lambda: _env("FOUNDRY_MODEL_FAST"))
    credential_mode: str = field(default_factory=lambda: _env("AZURE_CREDENTIAL_MODE", "cli").lower())
    # "auto" | "true" | "false". Reasoning models reject `temperature` outright
    # with a 400, which kills a run that may already have cost several minutes
    # of research, so this is detected rather than assumed.
    temperature_support: str = field(
        default_factory=lambda: _env("FOUNDRY_TEMPERATURE_SUPPORT", "auto").lower()
    )

    @property
    def is_configured(self) -> bool:
        return bool(self.project_endpoint and self.model)

    @property
    def supports_temperature(self) -> bool:
        """Whether the reasoning model accepts a `temperature` parameter.

        gpt-5, o1, o3 and o4 are reasoning models: they do their own sampling
        and return
        ``400 Unsupported parameter: 'temperature' is not supported with this
        model``. Override with FOUNDRY_TEMPERATURE_SUPPORT=true/false.
        """
        if self.temperature_support in {"true", "yes", "1", "on"}:
            return True
        if self.temperature_support in {"false", "no", "0", "off"}:
            return False
        name = self.model.lower().lstrip("-_")
        return not name.startswith(("gpt-5", "gpt5", "o1", "o3", "o4"))


@dataclass(slots=True)
class WordPressSettings:
    url: str = field(default_factory=lambda: _env("WP_URL").rstrip("/"))
    username: str = field(default_factory=lambda: _env("WP_USERNAME"))
    app_password: str = field(default_factory=lambda: _env("WP_APP_PASSWORD"))
    default_status: str = field(default_factory=lambda: _env("WP_DEFAULT_STATUS", "draft"))
    auto_push: bool = field(default_factory=lambda: _env_bool("WP_AUTO_PUSH", True))
    verify_tls: bool = field(default_factory=lambda: _env_bool("WP_VERIFY_TLS", True))
    # core/code ignores this; the Syntax-highlighting Code Block plugin uses it.
    # Inert either way, so it is on by default.
    code_language_attribute: bool = field(
        default_factory=lambda: _env_bool("WP_CODE_LANGUAGE_ATTR", True)
    )

    @property
    def api_base(self) -> str:
        return f"{self.url}/wp-json/wp/v2"

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.username and self.app_password)


@dataclass(slots=True)
class SearchSettings:
    """How the agents reach the open web.

    ``foundry`` (default) uses the server-side web search tool built into Azure
    AI Foundry — the model calls it inside the service, so there is no third
    party key and no extra Azure resource to create. ``tavily`` and ``brave``
    call those APIs from this process instead, via the local ``web_search`` tool.
    ``none`` disables open-web search entirely, leaving the RSS feeds and
    Microsoft Learn.
    """

    provider: str = field(default_factory=lambda: _env("SEARCH_PROVIDER", "foundry").lower())
    tavily_key: str = field(default_factory=lambda: _env("TAVILY_API_KEY"))
    brave_key: str = field(default_factory=lambda: _env("BRAVE_API_KEY"))
    context_size: str = field(default_factory=lambda: _env("SEARCH_CONTEXT_SIZE", "medium").lower())
    user_country: str = field(default_factory=lambda: _env("SEARCH_USER_COUNTRY", "ES"))

    @property
    def uses_hosted_tool(self) -> bool:
        """True when search happens inside Foundry rather than in this process."""
        return self.provider == "foundry"

    @property
    def uses_local_tool(self) -> bool:
        return self.provider in {"tavily", "brave"}

    @property
    def is_configured(self) -> bool:
        if self.provider == "foundry":
            return True  # nothing to configure; Microsoft manages the Bing resource
        if self.provider == "tavily":
            return bool(self.tavily_key)
        if self.provider == "brave":
            return bool(self.brave_key)
        return False

    @property
    def status_detail(self) -> str:
        if self.provider == "foundry":
            return "provider=foundry (hosted web search, no key required)"
        if self.provider == "none":
            return "provider=none — feeds + Microsoft Learn only"
        key = self.tavily_key if self.provider == "tavily" else self.brave_key
        state = "key set" if key else f"set {self.provider.upper()}_API_KEY"
        return f"provider={self.provider} ({state})"


@dataclass(slots=True)
class CoverSettings:
    """Cover image generation.

    Three request shapes, picked automatically from the provider and model name:

    * **MAI** (``MAI-Image-*``) — Microsoft's own image models. Custom route
      ``/mai/v1/images/generations`` with ``width``/``height`` integers. No
      ``quality``, no ``n``, and a hard cap of 1,048,576 total pixels.
    * **OpenAI-compatible on Azure** (``gpt-image-*``, ``FLUX-*``) —
      ``/openai/v1/images/generations`` with a ``size`` string.
    * **OpenAI direct** — api.openai.com with ``OPENAI_API_KEY``.
    """

    enabled: bool = field(default_factory=lambda: _env_bool("COVER_ENABLED", True))
    # "foundry" = your Azure resource (auto-detects MAI vs OpenAI-compatible),
    # "mai" = force the MAI route, "openai" = api.openai.com directly.
    provider: str = field(default_factory=lambda: _env("COVER_PROVIDER", "foundry").lower())
    model: str = field(default_factory=lambda: _env("COVER_MODEL", "MAI-Image-2.5-Pro"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_org: str = field(default_factory=lambda: _env("OPENAI_ORG_ID"))
    size: str = field(default_factory=lambda: _env("COVER_SIZE", "1536x1024"))
    quality: str = field(default_factory=lambda: _env("COVER_QUALITY", "high"))
    api_version: str = field(default_factory=lambda: _env("COVER_API_VERSION", "preview"))
    endpoint: str = field(default_factory=lambda: _env("COVER_ENDPOINT").rstrip("/"))
    api_key: str = field(default_factory=lambda: _env("COVER_API_KEY"))
    upload_to_wordpress: bool = field(default_factory=lambda: _env_bool("COVER_UPLOAD_TO_WP", True))

    @property
    def derived_endpoint(self) -> str:
        """Resource root inferred from the Foundry project endpoint.

        FOUNDRY_PROJECT_ENDPOINT looks like
        ``https://<resource>.services.ai.azure.com/api/projects/<project>``.
        The images API lives on the resource root, so everything from ``/api``
        onwards is dropped.
        """
        project = _env("FOUNDRY_PROJECT_ENDPOINT").rstrip("/")
        if not project:
            return ""
        marker = "/api/projects/"
        return project.split(marker)[0] if marker in project else project

    @property
    def uses_openai(self) -> bool:
        return self.provider == "openai"

    @property
    def uses_mai(self) -> bool:
        """MAI models speak their own protocol, not the OpenAI images API.

        Detected from the model name so that setting COVER_MODEL is enough;
        COVER_PROVIDER=mai forces it.
        """
        if self.provider == "mai":
            return True
        return self.provider == "foundry" and self.model.lower().startswith("mai-")

    @property
    def dimensions(self) -> tuple[int, int]:
        """COVER_SIZE parsed to (width, height). Falls back to 1248x832."""
        try:
            width, height = (int(part) for part in self.size.lower().split("x", 1))
            return width, height
        except Exception:  # noqa: BLE001
            return 1248, 832

    @property
    def is_configured(self) -> bool:
        if not self.enabled:
            return False
        if self.uses_openai:
            return bool(self.openai_api_key)
        return bool(self.endpoint or self.derived_endpoint)

    @property
    def route(self) -> str:
        if self.uses_openai:
            return "openai"
        return "mai" if self.uses_mai else "azure-openai"

    @property
    def status_detail(self) -> str:
        if self.uses_openai:
            state = "OPENAI_API_KEY set" if self.openai_api_key else "set OPENAI_API_KEY"
            return f"openai · {self.model} {self.size} ({state})"
        target = self.endpoint or self.derived_endpoint or "<no endpoint>"
        return f"{self.route} · {self.model} {self.size} @ {target}"


@dataclass(slots=True)
class TranslationSettings:
    """Post-approval translation of the English draft."""

    # Opt-in. Translation is decided per draft, not applied to everything.
    enabled: bool = field(default_factory=lambda: _env_bool("TRANSLATE_ENABLED", False))
    push: bool = field(default_factory=lambda: _env_bool("TRANSLATE_PUSH", True))
    # Only translate drafts that cleared the validators.
    only_when_approved: bool = field(
        default_factory=lambda: _env_bool("TRANSLATE_ONLY_WHEN_APPROVED", True)
    )


@dataclass(slots=True)
class RunSettings:
    max_revision_rounds: int = field(default_factory=lambda: _env_int("PPN_MAX_REVISION_ROUNDS", 3))
    max_source_rounds: int = field(default_factory=lambda: _env_int("PPN_MAX_SOURCE_ROUNDS", 2))
    # Wall-clock ceilings. A run that exceeds these fails loudly instead of
    # hanging forever on a stalled model call.
    # Generous by design: topic discovery legitimately runs 10-20 minutes, and a
    # full post with both loops can run an hour. These exist to break a genuine
    # hang, not to cut short honest work.
    suggest_timeout_minutes: int = field(default_factory=lambda: _env_int("PPN_SUGGEST_TIMEOUT_MINUTES", 40))
    write_timeout_minutes: int = field(default_factory=lambda: _env_int("PPN_WRITE_TIMEOUT_MINUTES", 90))
    output_dir: Path = field(default_factory=lambda: ROOT / _env("PPN_OUTPUT_DIR", "drafts"))
    research_dir: Path = field(default_factory=lambda: ROOT / _env("PPN_RESEARCH_DIR", "research"))
    topics_dir: Path = field(default_factory=lambda: ROOT / _env("PPN_TOPICS_DIR", "topics"))
    log_level: str = field(default_factory=lambda: _env("PPN_LOG_LEVEL", "INFO").upper())


@dataclass(slots=True)
class Settings:
    foundry: FoundrySettings = field(default_factory=FoundrySettings)
    wordpress: WordPressSettings = field(default_factory=WordPressSettings)
    search: SearchSettings = field(default_factory=SearchSettings)
    cover: CoverSettings = field(default_factory=CoverSettings)
    translation: TranslationSettings = field(default_factory=TranslationSettings)
    run: RunSettings = field(default_factory=RunSettings)

    # Config documents are pulled from the active ConfigSource (YAML files by
    # default, the database when the server is running) and cached until that
    # source reports a new version token.
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)
    _cache_token: str = field(default="", repr=False)

    def _document(self, name: str, *, text: bool = False) -> Any:
        from .config_source import get_config_source

        source = get_config_source()
        token = source.version_token()
        if token != self._cache_token:
            self._cache = {}
            self._cache_token = token
        if name not in self._cache:
            self._cache[name] = source.get_text(name) if text else source.get_mapping(name)
        return self._cache[name]

    @property
    def blog_profile(self) -> dict[str, Any]:
        return self._document("blog_profile")

    @property
    def topics(self) -> dict[str, Any]:
        return self._document("topics")

    @property
    def sources(self) -> dict[str, Any]:
        return self._document("sources")

    @property
    def validation(self) -> dict[str, Any]:
        return self._document("validation_rules")

    @property
    def style_guide(self) -> str:
        return self._document("style_guide", text=True)

    # -- convenience views used by the agents -------------------------------

    @property
    def translation_profile(self) -> dict[str, Any]:
        """Target language, localised headings and terms to keep in English."""
        return dict(self.blog_profile.get("translation", {}))

    @property
    def structure(self) -> dict[str, Any]:
        """House structural conventions (ToC, section count, Fuentes, ...)."""
        return dict(self.blog_profile.get("structure", {}))

    @property
    def language(self) -> str:
        return str(self.blog_profile.get("blog", {}).get("language", "es"))

    @property
    def watch_areas(self) -> list[dict[str, Any]]:
        return list(self.topics.get("watch_areas", []))

    @property
    def feeds(self) -> list[dict[str, Any]]:
        return list(self.sources.get("feeds", []))

    @property
    def trust_tiers(self) -> dict[str, Any]:
        return dict(self.sources.get("trust_tiers", {}))

    @property
    def blocked_domains(self) -> list[str]:
        return [d.lower() for d in self.sources.get("blocked_domains", [])]

    @property
    def declined_domains(self) -> list[str]:
        """Sites turned down in a source review — never proposed again.

        Weaker than ``blocked_domains``: a declined site is simply not offered
        for approval, whereas a blocked one fails the draft that cites it.
        """
        return [d.lower() for d in self.sources.get("declined_domains") or []]

    @property
    def source_policy(self) -> dict[str, Any]:
        return dict(self.sources.get("policy", {}))

    # The six rule families in validation_rules.yaml, in the order the loop
    # governance block checks them. Group name is the family minus "_rules".
    RULE_GROUPS = (
        "honesty_rules",
        "typography_rules",
        "voice_rules",
        "content_rules",
        "structure_rules",
        "seo_rules",
    )

    # Which validator owns which families. Content judges honesty, voice and
    # content; Design judges typography, structure and SEO.
    CONTENT_GROUPS = ("honesty", "voice", "content")
    DESIGN_GROUPS = ("typography", "structure", "seo")

    def all_rules(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for group in self.RULE_GROUPS:
            for rule in self.validation.get(group, []):
                out.append({**rule, "group": group.replace("_rules", "")})
        return out

    def rules_text(self, groups: tuple[str, ...] = CONTENT_GROUPS + DESIGN_GROUPS) -> str:
        lines: list[str] = []
        for rule in self.all_rules():
            if rule["group"] not in groups:
                continue
            auto = " [auto]" if rule.get("auto") else ""
            lines.append(f"- [{rule['id']}] ({rule['severity']}){auto} {rule['rule'].strip()}")
            if hint := rule.get("check_hint"):
                lines.append(f"    how to check: {hint.strip()}")
            if fix := rule.get("fix_hint"):
                lines.append(f"    fix: {fix.strip()}")
        return "\n".join(lines)

    @property
    def banned_headings(self) -> list[str]:
        return list(self.structure.get("banned_headings", []))

    @property
    def voice_modes(self) -> dict[str, Any]:
        """The field_report / analysis definitions from blog_profile.yaml."""
        return dict(self.blog_profile.get("voice_mode", {}))

    @property
    def post_formats(self) -> list[dict[str, Any]]:
        return list(self.blog_profile.get("post_formats", []))

    def word_target(self, post_format: str, voice_mode: str = "field_report") -> tuple[int, int]:
        """Target word band for a format, scaled down for analysis posts."""
        band = (2000, 2800)
        for fmt in self.post_formats:
            if fmt.get("id") == post_format:
                lo, hi = fmt.get("target_words", band)
                band = (int(lo), int(hi))
                break
        factor = float(self.voice_modes.get(voice_mode, {}).get("word_target_factor", 1.0))
        return round(band[0] * factor), round(band[1] * factor)

    def ensure_dirs(self) -> None:
        for path in (self.run.output_dir, self.run.research_dir, self.run.topics_dir):
            path.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
