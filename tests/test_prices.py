"""The Azure price client, offline.

Every test here runs against a payload recorded from the live retail API
(``tests/fixtures/azure_retail_prices.json``) rather than the API itself. The
suite must not need the network, and a pricing feed that changes under CI would
make these tests fail for reasons that have nothing to do with the code.

The recorded rows are the real six gpt-5 meters for one region, plus two rows
chosen for what they break: a ``1K``-unit meter, and a Batch meter that must
never be mistaken for the synchronous one.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from ppn_blogger.server import prices

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "azure_retail_prices.json").read_text()
)


@pytest.fixture
def recorded(monkeypatch):
    """Serve the recorded payload, filtered the way the real API filters."""
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        rows = FIXTURE["Items"]
        expr = params.get("$filter", "")
        if "contains(meterName, '" in expr:
            fragment = expr.split("contains(meterName, '")[1].split("')")[0]
            rows = [r for r in rows if fragment.lower() in r["meterName"].lower()]
        return httpx.Response(200, json={"Items": rows, "NextPageLink": None})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return calls


# ---------------------------------------------------------------------------
# Reading the feed's naming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "meter,tier,expected",
    [
        ("5 pp inp Gl 1M Tokens", "Gl", "input"),
        ("5 pp opt Gl 1M Tokens", "Gl", "output"),
        ("5 pp cd inp Gl 1M Tokens", "Gl", "cached_input"),
        ("5 pp inp Dz 1M Tokens", "Dz", "input"),
        # Right meter, wrong tier: Global and Data Zone differ by ~10%, so
        # silently accepting the other one is a wrong number, not a near miss.
        ("5 pp inp Dz 1M Tokens", "Gl", None),
        # Batch is roughly half price and this crew never uses it.
        ("5 pp Batch inp Gl 1M Tokens", "Gl", None),
        ("gpt 4.1 Inp regnl Tokens", "regnl", "input"),
    ],
)
def test_classify_reads_the_meter_grammar(meter, tier, expected):
    assert prices.classify(meter, tier) == expected


def test_cached_is_tested_before_input():
    """`cd inp` contains `inp`; ordering is what stops it reading as input."""
    assert prices.classify("5 pp cd inp Gl 1M Tokens", "Gl") == "cached_input"


@pytest.mark.parametrize(
    "model,fragment",
    [
        ("gpt-5", "5 pp"),
        # Longest prefix wins, or mini would resolve through the gpt-5 entry.
        ("gpt-5-mini", "5 mini pp"),
        # The dated name the service actually reports.
        ("gpt-5-2025-08-07", "5 pp"),
        # Unknown models get their own name, which matches nothing. An empty
        # candidate list beats a confident wrong one.
        ("some-new-model", "some-new-model"),
    ],
)
def test_search_fragment(model, fragment):
    assert prices.search_fragment(model) == fragment


def test_prices_are_normalised_to_one_million_tokens():
    """The feed mixes 1K and 1M units; stored prices are always per 1M."""
    assert prices._per_million({"unitOfMeasure": "1M", "retailPrice": 2.5}) == 2.5
    assert prices._per_million({"unitOfMeasure": "1K", "retailPrice": 0.0022}) == pytest.approx(2.2)
    assert prices._per_million({"unitOfMeasure": "1 Hour", "retailPrice": 1.0}) is None


# ---------------------------------------------------------------------------
# Binding and refreshing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_are_few_enough_to_choose_from(recorded):
    rows = await prices.candidates("gpt-5", "eastus", "USD", "Gl")
    # The targeted query is the whole reason this is workable: six real meters,
    # not the four hundred a broad query returns.
    assert len(rows) == 7  # six gpt-5 meters plus the Batch row
    classified = {r["direction"]: r["meter"] for r in rows if r["direction"]}
    assert classified == {
        "input": "5 pp inp Gl 1M Tokens",
        "output": "5 pp opt Gl 1M Tokens",
        "cached_input": "5 pp cd inp Gl 1M Tokens",
    }
    assert "contains(meterName, '5 pp')" in recorded[0]["$filter"]


@pytest.mark.asyncio
async def test_suggest_binding_picks_only_the_unambiguous(recorded):
    binding = await prices.suggest_binding("gpt-5", "eastus", "USD", "Gl")
    assert binding == {
        "input": "5 pp inp Gl 1M Tokens",
        "cached_input": "5 pp cd inp Gl 1M Tokens",
        "output": "5 pp opt Gl 1M Tokens",
    }


@pytest.mark.asyncio
async def test_refresh_reports_moves_against_bound_names(recorded):
    document = {
        "currency": "USD",
        "region": "eastus",
        "models": {
            "gpt-5": {
                "input": 1.25,  # stale
                "cached_input": 0.25,  # current
                "output": 20.0,  # current
                "meters": {
                    "input": "5 pp inp Gl 1M Tokens",
                    "cached_input": "5 pp cd inp Gl 1M Tokens",
                    "output": "5 pp opt Gl 1M Tokens",
                },
            }
        },
    }
    changes = {c["direction"]: c for c in await prices.refresh(document)}

    assert changes["input"]["changed"] is True
    assert changes["input"]["new"] == 2.5
    # Unchanged directions are still reported, so the UI can say "checked" —
    # an empty result would be indistinguishable from a failed lookup.
    assert changes["output"]["changed"] is False
    assert changes["cached_input"]["changed"] is False

    applied = prices.apply(document, list(changes.values()))
    assert applied["models"]["gpt-5"]["input"] == 2.5
    assert applied["models"]["gpt-5"]["output"] == 20.0
    # The source document must not be mutated in place — the caller decides.
    assert document["models"]["gpt-5"]["input"] == 1.25


@pytest.mark.asyncio
async def test_a_vanished_meter_keeps_the_last_known_price(recorded):
    document = {
        "currency": "USD",
        "region": "eastus",
        "models": {
            "gpt-5": {"input": 1.25, "meters": {"input": "a meter that no longer exists"}}
        },
    }
    changes = await prices.refresh(document)
    assert changes[0]["found"] is False
    assert changes[0]["changed"] is False

    applied = prices.apply(document, changes)
    # A stale price is a better estimate than a blank one.
    assert applied["models"]["gpt-5"]["input"] == 1.25


@pytest.mark.asyncio
async def test_the_refresh_never_touches_hand_set_prices(recorded):
    """Azure has no matchable meter for covers or hosted search, so those
    numbers are the operator's and the refresh must leave them alone."""
    document = {
        "currency": "USD",
        "region": "eastus",
        "models": {"gpt-5": {"input": 1.25, "meters": {"input": "5 pp inp Gl 1M Tokens"}}},
        "images": {"MAI-Image-2.5-Pro": {"per_image": 0.07}},
        "tools": {"web_search": {"per_call": 0.035}},
    }
    applied = prices.apply(document, await prices.refresh(document))
    assert applied["images"] == {"MAI-Image-2.5-Pro": {"per_image": 0.07}}
    assert applied["tools"] == {"web_search": {"per_call": 0.035}}


@pytest.mark.asyncio
async def test_an_unreachable_api_yields_nothing_rather_than_raising(monkeypatch):
    """A refresh is best-effort. Microsoft being down must not break a run,
    fail a scheduler tick, or blank the prices already in the document."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: original(*a, **{**kw, "transport": transport})
    )

    assert await prices.candidates("gpt-5", "eastus", "USD", "Gl") == []

    document = {
        "region": "eastus",
        "models": {"gpt-5": {"input": 1.25, "meters": {"input": "5 pp inp Gl 1M Tokens"}}},
    }
    changes = await prices.refresh(document)
    assert all(c["found"] is False for c in changes)
    assert prices.apply(document, changes)["models"]["gpt-5"]["input"] == 1.25


@pytest.mark.asyncio
async def test_a_document_with_no_region_does_not_query_at_all(recorded):
    assert await prices.refresh({"models": {"gpt-5": {}}}) == []
    assert recorded == []
