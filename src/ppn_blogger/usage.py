"""What a run cost: tokens counted at one seam, priced from config.

Three decisions shape this module.

**The meter is agent middleware, not chat middleware.** Agent Framework offers
both, and the chat layer looks like the better seam — it sees the model name and
fires per round trip. It is the wrong one here: ``StubChatClient`` extends
``BaseChatClient``, the *raw* base, and ``ChatMiddlewareLayer`` is a mixin only
concrete clients carry. A chat-middleware meter therefore works against Foundry
and fires never against the stub — invisible to ``pytest`` and to every
``--dry-run``. ``AgentMiddleware`` is installed by the ``Agent`` itself, so it
fires either way. The cost is that ``AgentResponse`` carries no model name, which
is why the meter is told its model at construction.

Counting once per agent invocation loses nothing: ``FunctionInvocationLayer``
aggregates usage across the whole tool-calling loop before returning, so a
researcher that made nine searches reports all nine round trips in one figure.

**Metering must never raise.** Same doctrine as ``build_cover`` and the WordPress
push: losing a forty-minute run to a bookkeeping bug is the failure being
designed out. Every path here swallows and logs.

**Counting and pricing are separate.** Everything above the ``price`` function is
exact — the service reports it. ``price`` multiplies by a number from config and
is therefore an estimate, and the two must not be confused in the UI. Pricing is
pure so it can be tested without a model, a network or a database.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from agent_framework import AgentContext, AgentMiddleware

logger = logging.getLogger("ppn.usage")

# One row per agent invocation, or per generated image.
MODEL = "model"
IMAGE = "image"


@dataclass(slots=True)
class UsageRecord:
    """One metered unit of work: an agent invocation, or one generated image."""

    agent_id: str
    model: str
    kind: str = MODEL
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    searches: int = 0
    images: int = 0

    @property
    def billable_input_tokens(self) -> int:
        """Input tokens charged at the full rate.

        Providers report cached input *inside* the input count, not alongside it,
        so charging both at their own rate would bill the cached tokens twice.
        """
        return max(0, self.input_tokens - self.cached_input_tokens)


@dataclass(slots=True)
class Ledger:
    """Accumulates records for one run.

    Held in a ``ContextVar`` set before the run starts: child tasks inherit the
    reference and mutate it, which is what makes the fan-out scouts land in the
    same ledger without any plumbing through the workflow graph. Same pattern as
    ``server.runs.current_run_id``.

    ``sink`` lets the server persist each record as it arrives rather than at the
    end, so a cancelled or failed run keeps the cost it already incurred.
    """

    records: list[UsageRecord] = field(default_factory=list)
    sink: Callable[[UsageRecord], None] | None = None

    def add(self, record: UsageRecord) -> None:
        self.records.append(record)
        if self.sink is None:
            return
        try:
            self.sink(record)
        except Exception:  # noqa: BLE001 - bookkeeping must never sink a run
            logger.exception("usage sink failed for %s", record.agent_id)


current_ledger: contextvars.ContextVar[Ledger | None] = contextvars.ContextVar(
    "ppn_current_ledger", default=None
)


def record(item: UsageRecord) -> None:
    """File a record against the active ledger. A no-op when nothing is metering."""
    ledger = current_ledger.get()
    if ledger is not None:
        ledger.add(item)


def record_image(model: str, *, agent_id: str = "cover", count: int = 1) -> None:
    """Cover art. Priced per image, so it carries no tokens."""
    record(UsageRecord(agent_id=agent_id, model=model, kind=IMAGE, images=count))


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def count_searches(response: Any) -> int:
    """How many web searches this response performed.

    Two shapes, because the two search providers surface differently and the
    operator's choice of ``SEARCH_PROVIDER`` should not decide whether searches
    get counted:

    * Foundry's hosted tool runs inside the service and comes back as
      ``search_tool_call`` content named ``web_search``.
    * ``tavily``/``brave`` run in this process through the local ``web_search``
      ``@tool``, and appear as ordinary function calls.

    De-duplicated by ``call_id`` because the hosted path emits a *call* and a
    *result* content for the same search.
    """
    seen: set[str] = set()
    count = 0
    for content in _contents(response):
        ctype = getattr(content, "type", "")
        name = getattr(content, "tool_name", None) or getattr(content, "name", None)
        if name != "web_search":
            continue
        if ctype not in {"search_tool_call", "search_tool_result", "function_call"}:
            continue
        call_id = getattr(content, "call_id", None) or ""
        if call_id:
            if call_id in seen:
                continue
            seen.add(call_id)
        count += 1
    return count


def _contents(response: Any) -> Iterable[Any]:
    for message in getattr(response, "messages", None) or []:
        yield from getattr(message, "contents", None) or []


def _usage_record(agent_id: str, model: str, response: Any) -> UsageRecord:
    usage = getattr(response, "usage_details", None) or {}
    return UsageRecord(
        agent_id=agent_id,
        model=model,
        kind=MODEL,
        input_tokens=int(usage.get("input_token_count") or 0),
        output_tokens=int(usage.get("output_token_count") or 0),
        cached_input_tokens=int(usage.get("cache_read_input_token_count") or 0),
        reasoning_tokens=int(usage.get("reasoning_output_token_count") or 0),
        total_tokens=int(usage.get("total_token_count") or 0),
        searches=count_searches(response),
    )


class UsageMeter(AgentMiddleware):
    """Records what one agent spent, on both the streaming and buffered paths.

    Streaming needs the hook: ``context.result`` is a live stream when the
    middleware regains control, and usage only exists once it has been consumed.
    ``stream_result_hooks`` runs against the finalised response, which is where
    the framework has folded the trailing usage content in.
    """

    def __init__(self, agent_id: str, model: str) -> None:
        self.agent_id = agent_id
        self.model = model

    async def process(self, context: AgentContext, call_next: Any) -> None:
        if context.stream:
            context.stream_result_hooks.append(self._hook)
            await call_next()
            return
        await call_next()
        self._record(context.result)

    def _hook(self, response: Any) -> Any:
        self._record(response)
        return response

    def _record(self, response: Any) -> None:
        if response is None:
            return
        try:
            record(_usage_record(self.agent_id, self.model, response))
        except Exception:  # noqa: BLE001 - never lose a run to metering
            logger.exception("could not meter %s", self.agent_id)


# ---------------------------------------------------------------------------
# Pricing
#
# Everything above is exact. Everything below multiplies it by a number the
# operator configured, and is an estimate at list price — no PTU, no
# reservation, no discount.
# ---------------------------------------------------------------------------

PER_MILLION = 1_000_000
# Money is carried as integer micros of the configured currency. Floats across
# the SQLite/SQL Server seam are how this project has been bitten before, and a
# running total of thousands of per-agent figures is exactly where the drift
# would show.
MICROS = 1_000_000


@dataclass(slots=True)
class Cost:
    """A priced total. ``priced`` is false when anything went uncosted."""

    micros: int = 0
    currency: str = ""
    priced: bool = True
    unpriced_models: tuple[str, ...] = ()

    @property
    def amount(self) -> float:
        return self.micros / MICROS


def price_record(item: UsageRecord, prices: dict[str, Any]) -> tuple[int, bool]:
    """Cost one record, in micros. Returns ``(micros, priced)``.

    ``priced`` is false when no rate is configured for the model — the tokens
    are still real and still recorded, so the caller reports the count and says
    the money is unknown rather than showing a confident zero.
    """
    if item.kind == IMAGE:
        rate = _lookup(prices.get("images"), item.model)
        if rate is None:
            return 0, False
        return round(item.images * float(rate.get("per_image") or 0) * MICROS), True

    rate = _lookup(prices.get("models"), item.model)
    if rate is None:
        return 0, False

    per_million = (
        item.billable_input_tokens * float(rate.get("input") or 0)
        + item.cached_input_tokens * float(rate.get("cached_input") or 0)
        + item.output_tokens * float(rate.get("output") or 0)
    )
    micros = round(per_million / PER_MILLION * MICROS)

    # Searches are counted exactly but priced from a hand-set figure: Azure
    # publishes no per-call meter for the hosted search tool. An unset price
    # means "not counted", not "free", so it does not flip `priced`.
    search_rate = _lookup(prices.get("tools"), "web_search") or {}
    if item.searches and search_rate.get("per_call"):
        micros += round(item.searches * float(search_rate["per_call"]) * MICROS)

    return micros, True


def price(records: Iterable[UsageRecord], prices: dict[str, Any] | None) -> Cost:
    """Total a run. Pure — no model, no network, no database."""
    prices = prices or {}
    currency = str(prices.get("currency") or "USD")
    total = 0
    unpriced: list[str] = []
    for item in records:
        micros, ok = price_record(item, prices)
        total += micros
        if not ok and item.model not in unpriced:
            unpriced.append(item.model)
    return Cost(
        micros=total,
        currency=currency,
        priced=not unpriced,
        unpriced_models=tuple(unpriced),
    )


def _lookup(table: Any, model: str) -> dict[str, Any] | None:
    """Find a model's rates, tolerating the version suffix a service reports.

    A deployment called ``gpt-5`` answers as ``gpt-5-2025-08-07``. Matching the
    longest configured key that prefixes the reported name means the operator
    configures the model, not every dated build of it.
    """
    if not isinstance(table, dict) or not model:
        return None
    entry = table.get(model)
    if isinstance(entry, dict):
        return entry
    best: tuple[int, dict[str, Any]] | None = None
    for key, value in table.items():
        if isinstance(value, dict) and model.startswith(str(key)):
            if best is None or len(str(key)) > best[0]:
                best = (len(str(key)), value)
    return best[1] if best else None


def totals(records: Iterable[UsageRecord]) -> dict[str, int]:
    """Token and unit totals, independent of any price."""
    out = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "searches": 0,
        "images": 0,
        "calls": 0,
    }
    for item in records:
        out["input_tokens"] += item.input_tokens
        out["output_tokens"] += item.output_tokens
        out["cached_input_tokens"] += item.cached_input_tokens
        out["reasoning_tokens"] += item.reasoning_tokens
        out["total_tokens"] += item.total_tokens
        out["searches"] += item.searches
        out["images"] += item.images
        if item.kind == MODEL:
            out["calls"] += 1
    return out
