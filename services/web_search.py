"""Web search service supporting DuckDuckGo and Tavily providers."""

import asyncio
import logging
from typing import Optional

_logger = logging.getLogger(__name__)

# Maximum number of search results to return
_DEFAULT_MAX_RESULTS = 5


def _format_results(results: list[dict]) -> str:
    """Format a list of search result dicts into a readable string for tool_result content."""
    if not results:
        return "No results found."
    parts: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("url", r.get("href", ""))
        snippet = r.get("snippet", r.get("body", r.get("content", "")))
        parts.append(f"{i}. {title}\n   URL: {url}\n   {snippet}")
    return "\n\n".join(parts)


async def _search_duckduckgo(query: str, max_results: int) -> str:
    """Run a DuckDuckGo text search and return formatted results."""
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError(
            "ddgs is not installed. Run: pip install ddgs"
        ) from exc

    def _sync_search() -> list[dict]:
        with DDGS() as ddgs_client:
            return list(ddgs_client.text(query, max_results=max_results))

    results = await asyncio.to_thread(_sync_search)
    _logger.debug("DuckDuckGo search for %r returned %d results", query, len(results))
    return _format_results(results)


async def _search_tavily(query: str, max_results: int, api_key: str) -> str:
    """Run a Tavily search and return formatted results."""
    if not api_key:
        raise ValueError(
            "TAVILY_API_KEY is not configured. Set it in your .env file."
        )
    try:
        from tavily import AsyncTavilyClient
    except ImportError as exc:
        raise RuntimeError(
            "tavily-python is not installed. Run: pip install tavily-python"
        ) from exc

    client = AsyncTavilyClient(api_key=api_key)
    response = await client.search(query, max_results=max_results)

    raw_results = response.get("results", [])
    # Normalise Tavily result keys to the common schema
    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in raw_results
    ]
    _logger.debug("Tavily search for %r returned %d results", query, len(results))
    return _format_results(results)


async def perform_web_search(
    query: str,
    provider: str = "duckduckgo",
    tavily_api_key: Optional[str] = None,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> str:
    """Execute a web search using the configured provider.

    Args:
        query: The search query string.
        provider: Either ``"duckduckgo"`` or ``"tavily"``.
        tavily_api_key: API key — required when provider is ``"tavily"``.
        max_results: Maximum number of results to return.

    Returns:
        A formatted string containing the search results, ready to be
        used as ``tool_result`` content in the Anthropic conversation.
    """
    provider = (provider or "duckduckgo").lower().strip()
    _logger.info("Web search via %s: %r", provider, query)

    if provider == "tavily":
        return await _search_tavily(query, max_results, tavily_api_key or "")
    elif provider == "duckduckgo":
        return await _search_duckduckgo(query, max_results)
    else:
        raise ValueError(
            f"Unknown web search provider {provider!r}. "
            "Valid values are 'duckduckgo' or 'tavily'."
        )