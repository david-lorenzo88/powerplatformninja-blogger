"""Chat client construction.

The whole crew talks to Azure AI Foundry through ``FoundryChatClient``. Two
clients are exposed: a *reasoning* client for the agents that do the hard
thinking (researcher, writer, validators, source checker) and an optional
*fast* client for the high-volume scouts. If ``FOUNDRY_MODEL_FAST`` is not set,
both are the same client.
"""

from __future__ import annotations

import functools
from typing import Any

from agent_framework import BaseChatClient

from .settings import get_settings


def _credential() -> Any:
    settings = get_settings()
    if settings.foundry.credential_mode == "default":
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential()
    from azure.identity import AzureCliCredential

    return AzureCliCredential()


@functools.lru_cache(maxsize=4)
def _build(model: str) -> BaseChatClient:
    from agent_framework.foundry import FoundryChatClient

    settings = get_settings()
    if not settings.foundry.project_endpoint:
        raise RuntimeError(
            "FOUNDRY_PROJECT_ENDPOINT is not set. Copy .env.example to .env and fill it in, "
            "or run with --dry-run to use the offline stub client."
        )
    return FoundryChatClient(
        project_endpoint=settings.foundry.project_endpoint,
        model=model,
        credential=_credential(),
    )


def reasoning_client() -> BaseChatClient:
    """Client for agents that need the strongest model."""
    return _build(get_settings().foundry.model)


def fast_client() -> BaseChatClient:
    """Client for cheap, high-volume work (scouts). Falls back to the main model."""
    settings = get_settings()
    return _build(settings.foundry.fast_model or settings.foundry.model)


class ClientBundle:
    """Passed to every agent factory so tests can swap in a stub."""

    def __init__(self, reasoning: BaseChatClient | None = None, fast: BaseChatClient | None = None):
        self._reasoning = reasoning
        self._fast = fast

    @property
    def reasoning(self) -> BaseChatClient:
        return self._reasoning if self._reasoning is not None else reasoning_client()

    @property
    def fast(self) -> BaseChatClient:
        if self._fast is not None:
            return self._fast
        return self._reasoning if self._reasoning is not None else fast_client()


def default_clients() -> ClientBundle:
    return ClientBundle()


# ---------------------------------------------------------------------------
# Hosted (server-side) web search
# ---------------------------------------------------------------------------


def hosted_web_search_tools(client: BaseChatClient) -> list[Any]:
    """Return Foundry's built-in web search tool, when that provider is selected.

    ``FoundryChatClient.get_web_search_tool()`` is GA and needs no extra Azure
    setup — the Bing resource behind it is managed by Microsoft, so there is no
    connection to create and no API key to buy. The search executes inside the
    service, which also means the results never round-trip through this process.

    Returns an empty list (rather than raising) whenever the client cannot
    provide one — the stub client during a dry run, an older SDK, or a provider
    other than ``foundry``. The agents keep their local tools either way.
    """
    from agent_framework import SupportsWebSearchTool

    settings = get_settings()
    if not settings.search.uses_hosted_tool:
        return []
    if not isinstance(client, SupportsWebSearchTool):
        return []

    kwargs: dict[str, Any] = {}
    if settings.search.context_size in {"low", "medium", "high"}:
        kwargs["search_context_size"] = settings.search.context_size
    if settings.search.user_country:
        kwargs["user_location"] = {"country": settings.search.user_country}

    try:
        return [client.get_web_search_tool(**kwargs)]
    except Exception as exc:  # noqa: BLE001 - degrade to feeds + Learn instead of failing the run
        import logging

        logging.getLogger("ppn").warning(
            "Foundry hosted web search unavailable (%s). Falling back to feeds and "
            "Microsoft Learn; set SEARCH_PROVIDER=tavily with a key to restore open-web search.",
            exc,
        )
        return []
