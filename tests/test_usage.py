"""Token accounting: the meter, the counters and the pricing arithmetic.

The meter tests run a real ``Agent`` against a stub that reports usage, because
the thing most worth protecting is that the middleware fires on *both* response
paths. Chat middleware silently does not fire against a raw ``BaseChatClient``,
which is what the stub is — so a meter written at that layer would pass a review
and record nothing offline.
"""

from __future__ import annotations

import asyncio

import pytest
from agent_framework import Agent, Message

from ppn_blogger import usage
from ppn_blogger.models import ScoutReport
from ppn_blogger.testing import StubChatClient
from ppn_blogger.usage import Ledger, UsageMeter, UsageRecord, current_ledger

PRICES = {
    "currency": "EUR",
    "models": {
        "gpt-5": {"input": 2.0, "cached_input": 0.2, "output": 20.0},
    },
    "images": {"MAI-Image-2.5-Pro": {"per_image": 0.07}},
    "tools": {"web_search": {"per_call": 0.035}},
}

# What StubChatClient reports per call.
STUB_INPUT, STUB_OUTPUT, STUB_TOTAL = 1200, 300, 1500


def _agent(client: StubChatClient) -> Agent:
    return Agent(
        client,
        "scout",
        id="probe",
        name="probe",
        default_options={"response_format": ScoutReport},
        middleware=[UsageMeter("probe", "gpt-5")],
    )


# ---------------------------------------------------------------------------
# The meter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meter_records_on_the_buffered_path():
    ledger = Ledger()
    current_ledger.set(ledger)

    await _agent(StubChatClient()).run([Message(role="user", contents=["go"])])

    assert len(ledger.records) == 1
    item = ledger.records[0]
    assert item.agent_id == "probe"
    assert item.model == "gpt-5"
    assert (item.input_tokens, item.output_tokens) == (STUB_INPUT, STUB_OUTPUT)
    assert item.cached_input_tokens == 200
    assert item.reasoning_tokens == 80


@pytest.mark.asyncio
async def test_meter_records_on_the_streaming_path():
    """The path the workflow actually uses, and the one that needs the hook.

    Worth its own test because chat middleware would pass the buffered case and
    silently record nothing here.
    """
    ledger = Ledger()
    current_ledger.set(ledger)

    stream = _agent(StubChatClient()).run([Message(role="user", contents=["go"])], stream=True)
    async for _ in stream:
        pass
    await stream.get_final_response()

    assert len(ledger.records) == 1
    assert ledger.records[0].total_tokens == STUB_TOTAL


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_meter_counts_searches_once_per_call_id(stream):
    ledger = Ledger()
    current_ledger.set(ledger)

    agent = _agent(StubChatClient(searches=3))
    if stream:
        response = agent.run([Message(role="user", contents=["go"])], stream=True)
        async for _ in response:
            pass
        await response.get_final_response()
    else:
        await agent.run([Message(role="user", contents=["go"])])

    # Three searches, six contents — the call and its result share a call_id.
    assert ledger.records[0].searches == 3


@pytest.mark.asyncio
async def test_meter_without_a_ledger_is_a_no_op():
    """The CLI can run without metering; that must not be an error path."""
    current_ledger.set(None)
    response = await _agent(StubChatClient()).run([Message(role="user", contents=["go"])])
    assert response.text


@pytest.mark.asyncio
async def test_a_broken_sink_cannot_sink_a_run():
    def explode(_: UsageRecord) -> None:
        raise RuntimeError("the database is on fire")

    ledger = Ledger(sink=explode)
    current_ledger.set(ledger)

    response = await _agent(StubChatClient()).run([Message(role="user", contents=["go"])])
    assert response.text
    assert len(ledger.records) == 1


@pytest.mark.asyncio
async def test_concurrent_agents_share_one_ledger():
    """Fan-out scouts run as child tasks; they must land in the caller's ledger."""
    ledger = Ledger()
    current_ledger.set(ledger)

    await asyncio.gather(
        *(
            _agent(StubChatClient()).run([Message(role="user", contents=["go"])])
            for _ in range(3)
        )
    )

    assert len(ledger.records) == 3


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_cached_input_is_not_billed_twice():
    """Providers report cached tokens inside the input count, not beside it."""
    item = UsageRecord(
        agent_id="w", model="gpt-5", input_tokens=1000, cached_input_tokens=400, output_tokens=0
    )
    micros, priced = usage.price_record(item, PRICES)
    assert priced
    # 600 at 2.0/M + 400 at 0.2/M = 0.0012 + 0.00008
    assert micros == round((600 * 2.0 + 400 * 0.2) / 1_000_000 * 1_000_000)


def test_unknown_model_keeps_the_tokens_and_drops_the_cost():
    cost = usage.price([UsageRecord(agent_id="w", model="stub", input_tokens=10)], PRICES)
    assert cost.micros == 0
    assert cost.priced is False
    assert cost.unpriced_models == ("stub",)


def test_a_dated_deployment_name_matches_its_configured_model():
    item = UsageRecord(agent_id="w", model="gpt-5-2025-08-07", output_tokens=1_000_000)
    micros, priced = usage.price_record(item, PRICES)
    assert priced
    assert micros == 20 * 1_000_000


def test_images_are_priced_per_image_not_per_token():
    item = UsageRecord(agent_id="cover", model="MAI-Image-2.5-Pro", kind=usage.IMAGE, images=1)
    micros, priced = usage.price_record(item, PRICES)
    assert priced
    assert micros == 70_000


def test_searches_are_added_at_the_configured_rate():
    item = UsageRecord(agent_id="s", model="gpt-5", searches=2)
    micros, _ = usage.price_record(item, PRICES)
    assert micros == 70_000


def test_an_unset_search_price_is_not_treated_as_free_model_pricing():
    """No search rate configured means the searches go uncosted, silently zero —
    but the model tokens must still price, and the run must not read 'unpriced'."""
    prices = {**PRICES, "tools": {}}
    cost = usage.price([UsageRecord(agent_id="s", model="gpt-5", searches=5)], prices)
    assert cost.priced is True
    assert cost.micros == 0


def test_totals_are_independent_of_any_price():
    records = [
        UsageRecord(agent_id="a", model="gpt-5", input_tokens=10, output_tokens=5, total_tokens=15),
        UsageRecord(agent_id="b", model="gpt-5", input_tokens=20, output_tokens=1, total_tokens=21),
        UsageRecord(agent_id="cover", model="x", kind=usage.IMAGE, images=1),
    ]
    out = usage.totals(records)
    assert out["total_tokens"] == 36
    assert out["images"] == 1
    assert out["calls"] == 2  # the image is not a model call


def test_pricing_with_no_document_configured():
    cost = usage.price([UsageRecord(agent_id="a", model="gpt-5", input_tokens=10)], None)
    assert cost.currency == "USD"
    assert cost.priced is False
