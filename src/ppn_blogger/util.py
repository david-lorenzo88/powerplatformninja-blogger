"""Small helpers shared by executors, publishers and the CLI."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from agent_framework import AgentExecutorResponse, AgentResponse, Message
from pydantic import BaseModel, ValidationError

from .settings import get_settings

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger("ppn")


def setup_logging() -> None:
    """Configure logging so the console shows pipeline progress, not framework chatter.

    Agent Framework logs every superstep at INFO, which for a full post run means
    dozens of lines that say nothing about the draft. Those loggers are raised to
    WARNING unless PPN_LOG_LEVEL=DEBUG, in which case everything comes back.
    """
    level = getattr(logging, get_settings().run.log_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("azure").setLevel(logging.WARNING)
    # trafilatura complains about malformed anchors on almost every page it parses.
    logging.getLogger("trafilatura").setLevel(logging.ERROR)

    if level > logging.DEBUG:
        # Agent Framework logs two INFO lines per tool call and one per superstep.
        # Our own `ppn.tools` logger reports the same activity in one line, so the
        # framework's copy is pure duplication until you are actually debugging.
        logging.getLogger("agent_framework").setLevel(logging.WARNING)


tool_logger = logging.getLogger("ppn.tools")


def log_tool(name: str, detail: str = "") -> None:
    """One concise line per tool call, so a stalled run is diagnosable."""
    tool_logger.info("%s %s", name, detail[:110])


class _DropMessages(logging.Filter):
    """Suppress specific framework warnings that are expected and not actionable."""

    def __init__(self, *fragments: str) -> None:
        super().__init__()
        self._fragments = fragments

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(fragment in message for fragment in self._fragments)


def silence_expected_warning(logger_name: str, *fragments: str) -> None:
    target = logging.getLogger(logger_name)
    if not any(isinstance(f, _DropMessages) for f in target.filters):
        target.addFilter(_DropMessages(*fragments))


# ---------------------------------------------------------------------------
# Structured-output extraction
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def _json_candidates(text: str) -> list[str]:
    out: list[str] = []
    for match in _FENCE.finditer(text):
        out.append(match.group(1))
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        out.append(stripped)
    # Greedy first-brace-to-last-brace as a final attempt
    first, last = stripped.find("{"), stripped.rfind("}")
    if first != -1 and last > first:
        out.append(stripped[first : last + 1])
    return out


def parse_model(source: AgentExecutorResponse | AgentResponse | str, model: type[T]) -> T:
    """Coerce an agent result into ``model``.

    Uses the framework's structured output (``AgentResponse.value``) when the
    provider honoured ``response_format``, and falls back to scraping JSON out of
    the text otherwise. Raises ValueError with the raw text attached when both
    paths fail, so failures are debuggable instead of silent.
    """
    if isinstance(source, AgentExecutorResponse):
        response: AgentResponse | None = source.agent_response
        text = response.text if response else ""
    elif isinstance(source, AgentResponse):
        response, text = source, source.text
    else:
        response, text = None, str(source)

    if response is not None:
        value = getattr(response, "value", None)
        if isinstance(value, model):
            return value
        if isinstance(value, dict):
            try:
                return model.model_validate(value)
            except ValidationError:
                pass

    errors: list[str] = []
    for candidate in _json_candidates(text):
        try:
            return model.model_validate(json.loads(candidate))
        except (json.JSONDecodeError, ValidationError) as exc:
            errors.append(str(exc)[:200])

    raise ValueError(
        f"Could not parse a {model.__name__} from the agent response. "
        f"Attempts: {errors[:2]}\n--- raw response ---\n{text[:1500]}"
    )


def user_message(text: str) -> Message:
    return Message(role="user", contents=[text])


def as_json(obj: Any, *, limit: int | None = None) -> str:
    if isinstance(obj, BaseModel):
        data = obj.model_dump(mode="json")
    else:
        data = obj
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    return text if limit is None else text[:limit]


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def slugify(value: str, max_len: int = 60) -> str:
    try:
        from slugify import slugify as _s

        return _s(value, max_length=max_len)
    except Exception:  # noqa: BLE001
        value = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
        return value[:max_len].strip("-")


def word_count(markdown: str) -> int:
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"[#>*_`\[\]()]", " ", text)
    return len(re.findall(r"\b[\w'-]+\b", text))


def strip_h1(markdown: str) -> tuple[str, str]:
    """Split a leading H1 off the body. WordPress renders the title separately."""
    lines = markdown.lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip(), "\n".join(lines[1:]).lstrip("\n")
    return "", markdown
