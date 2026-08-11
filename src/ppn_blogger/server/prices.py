"""Reading token prices out of the Azure Retail Prices API.

The API is public and unauthenticated, honours ``currencyCode`` and
``armRegionName``, and does carry every Foundry token meter. What it does not
have is a usable name. One region carries over four hundred GPT rows called
things like ``5.4 Batch cd inp Dz 1M Tokens`` and ``gpt 4.1 Inp regnl Tokens``,
mixing ``1K`` and ``1M`` units, and the naming is not regular across families —
gpt-5 is ``5 pp`` while gpt-5.4 is plain ``5.4``. Matching a deployment name
onto that automatically is guesswork, and a wrong guess is invisible: it
produces a plausible number against the wrong meter.

So the mapping is never guessed. It is **bound once by a human** and then reused
verbatim:

* :func:`candidates` runs a *targeted* query and hands back the handful of rows
  that could plausibly be the operator's — for gpt-5 in one region that is
  exactly six, input/output/cached across Global and Data Zone. Narrow enough to
  read, which is the whole point.
* :func:`refresh` looks the bound meter names back up by exact match. It can
  move a price; it cannot repoint one at a different meter. That is what makes
  the unattended weekly refresh safe to auto-apply.

Nothing here raises into a caller. A price refresh that fails must leave the
last known prices in place — the alternative is an outage at Microsoft turning
into a run that cannot be costed, or worse, a scheduler job that dies.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("ppn.server.prices")

ENDPOINT = "https://prices.azure.com/api/retail/prices"
SERVICE = "Foundry Models"
# Generous: this is a background refresh, not a request path, and the API is
# occasionally slow when a filter matches a lot before paging.
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# A guard, not a limit — a targeted query returns single figures. Following
# pages forever on a mis-typed filter is how a weekly job becomes a bill.
MAX_PAGES = 5

# Meter names are per 1K or per 1M tokens; prices are stored per 1M.
_UNIT_SCALE = {"1M": 1.0, "1K": 1000.0}

# How a deployment name is turned into a search fragment. Only ever used to
# *offer* candidates — never to pick one.
_PREFIXES = {
    "gpt-5": "5 pp",
    "gpt-5-mini": "5 mini pp",
    "gpt-5-nano": "5 nano",
    "gpt-4.1": "gpt 4.1",
    "gpt-4o": "gpt 4o",
    "o3": "o3",
    "o4-mini": "o4 mini",
}

DIRECTIONS = ("input", "cached_input", "output")


def search_fragment(model: str) -> str:
    """The `contains(meterName, …)` fragment to offer candidates for a model.

    Longest configured prefix wins, so `gpt-5-mini` does not resolve through the
    `gpt-5` entry. Unknown models fall back to the bare name, which usually
    matches nothing — an empty candidate list is a fine answer, and far better
    than a confident wrong one.
    """
    best = ""
    for key in _PREFIXES:
        if model.startswith(key) and len(key) > len(best):
            best = key
    return _PREFIXES.get(best, model)


async def _query(filter_expr: str, currency: str) -> list[dict[str, Any]]:
    """Every row matching a filter. Returns [] rather than raising."""
    items: list[dict[str, Any]] = []
    params: dict[str, str] | None = {"$filter": filter_expr, "currencyCode": currency}
    url = ENDPOINT
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as http:
            for _ in range(MAX_PAGES):
                response = await http.get(url, params=params)
                response.raise_for_status()
                body = response.json()
                items.extend(body.get("Items") or [])
                nxt = body.get("NextPageLink")
                if not nxt:
                    break
                # The next-page link already carries the filter and currency.
                url, params = nxt, None
    except Exception as exc:  # noqa: BLE001 - a price lookup must never raise
        logger.warning("Azure retail prices lookup failed (%s): %s", type(exc).__name__, exc)
        return []
    return items


def _per_million(row: dict[str, Any]) -> float | None:
    """Normalise a row to a price per 1M tokens."""
    scale = _UNIT_SCALE.get(str(row.get("unitOfMeasure") or "").strip())
    if scale is None:
        return None
    try:
        return float(row["retailPrice"]) * scale
    except (KeyError, TypeError, ValueError):
        return None


def classify(meter: str, tier: str) -> str | None:
    """Which of input / cached_input / output a meter name is, if any.

    The grammar, read off live data:
    ``<model> [Batch] [cd] inp|opt <Gl|Dz|regnl> 1M Tokens``. ``cd`` marks a
    cached-input meter and must be tested before plain input, or every cached
    row would also read as input.

    Batch and provisioned meters are rejected outright: this crew makes ordinary
    synchronous calls, and pricing them at the batch rate would understate every
    run by about half.
    """
    name = f" {meter.lower()} "
    if " batch " in name or " ptu" in name or "provisioned" in name:
        return None
    if f" {tier.lower()} " not in name:
        return None
    if " cd " in name or " cchd " in name or " cached " in name:
        return "cached_input"
    if " inp " in name or " input " in name:
        return "input"
    if " opt " in name or " outp " in name or " output " in name:
        return "output"
    return None


async def candidates(model: str, region: str, currency: str, tier: str) -> list[dict[str, Any]]:
    """The meters that could price this model, for a human to choose from."""
    fragment = search_fragment(model)
    rows = await _query(
        f"serviceName eq '{SERVICE}' and armRegionName eq '{region}' "
        f"and contains(meterName, '{fragment}')",
        currency,
    )

    out: list[dict[str, Any]] = []
    for row in rows:
        meter = str(row.get("meterName") or "")
        price = _per_million(row)
        if price is None:
            continue
        out.append(
            {
                "meter": meter,
                "direction": classify(meter, tier),
                "price_per_million": price,
                "currency": row.get("currencyCode") or currency,
                "unit": row.get("unitOfMeasure"),
                "product": row.get("productName"),
            }
        )
    # The ones that classify cleanly first — those are what the operator wants,
    # but the rest are still shown, because a model this does not understand is
    # exactly when a human needs to see everything.
    out.sort(key=lambda r: (r["direction"] is None, r["direction"] or "", r["meter"]))
    return out


async def suggest_binding(model: str, region: str, currency: str, tier: str) -> dict[str, str]:
    """The obvious meter for each direction, when there is exactly one.

    Deliberately silent where it is ambiguous: two candidates for `output` means
    no suggestion for `output`, not a coin toss. The operator fills the gap.
    """
    rows = await candidates(model, region, currency, tier)
    by_direction: dict[str, list[str]] = {}
    for row in rows:
        if row["direction"]:
            by_direction.setdefault(row["direction"], []).append(row["meter"])
    return {d: names[0] for d, names in by_direction.items() if len(names) == 1}


async def lookup(meters: dict[str, str], region: str, currency: str) -> dict[str, float]:
    """Current price per 1M tokens for already-bound meter names.

    One query per model rather than per meter: the three names share a prefix in
    every case seen so far, but relying on that would be a guess, so this filters
    on the region and matches names in Python.
    """
    wanted = {name for name in meters.values() if name}
    if not wanted:
        return {}

    rows = await _query(
        f"serviceName eq '{SERVICE}' and armRegionName eq '{region}'", currency
    )
    by_name = {str(r.get("meterName") or ""): r for r in rows}

    out: dict[str, float] = {}
    for direction, meter in meters.items():
        row = by_name.get(meter)
        if row is None:
            logger.warning("bound meter %r no longer exists in %s", meter, region)
            continue
        price = _per_million(row)
        if price is not None:
            out[direction] = price
    return out


async def refresh(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare every bound meter against the live feed.

    Returns one entry per direction that has a binding, whether or not it moved,
    so the caller can show "checked, unchanged" rather than an empty screen that
    looks like a failure. Applying is the caller's decision — this only reports.
    """
    region = str(document.get("region") or "")
    currency = str(document.get("currency") or "USD")
    models = document.get("models") or {}
    if not region or not isinstance(models, dict):
        return []

    # One fetch for the whole region, shared across models: the alternative is a
    # query per model for data that arrives in the same page.
    rows = await _query(f"serviceName eq '{SERVICE}' and armRegionName eq '{region}'", currency)
    by_name = {str(r.get("meterName") or ""): r for r in rows}

    changes: list[dict[str, Any]] = []
    for model, entry in models.items():
        if not isinstance(entry, dict):
            continue
        meters = entry.get("meters") or {}
        for direction in DIRECTIONS:
            meter = str(meters.get(direction) or "")
            if not meter:
                continue
            row = by_name.get(meter)
            new = _per_million(row) if row else None
            old = entry.get(direction)
            changes.append(
                {
                    "model": model,
                    "direction": direction,
                    "meter": meter,
                    "old": None if old is None else float(old),
                    "new": new,
                    "found": row is not None,
                    "changed": new is not None and (old is None or abs(float(old) - new) > 1e-9),
                }
            )
    return changes


def apply(document: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    """A copy of the document with the moved prices written in.

    Only rows that were actually found and actually moved are applied — a meter
    that has vanished from the feed leaves the last known price alone rather
    than blanking it, because a stale price is a better estimate than none.

    The hand-set `images` and `tools` sections are never touched: Azure
    publishes no matchable meter for either, so they are the operator's numbers
    and the refresh has no business overwriting them.
    """
    import copy

    updated = copy.deepcopy(document)
    for change in changes:
        if not change.get("changed") or change.get("new") is None:
            continue
        entry = updated.setdefault("models", {}).setdefault(change["model"], {})
        entry[change["direction"]] = round(float(change["new"]), 6)
    return updated
