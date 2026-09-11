"""Model registry — fetches and caches model lists from ICA-1 and ICA-2 backends."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {
    "ica1": [],       # list[str] of model IDs from ICA-1
    "ica2": [],       # list[str] of model IDs from ICA-2
    "refreshed_at": None,  # ISO-8601 string or None
}


def get_cache() -> dict[str, Any]:
    """Return the current in-memory model cache (read-only snapshot)."""
    return dict(_cache)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


async def _fetch_models(base_url: str, api_key: str, backend_name: str) -> list[str]:
    """Fetch the /models endpoint for one backend and return a list of model IDs.

    Both ICA-1 and ICA-2 return ``{"data": [{"id": "...", ...}, ...], "object": "list"}``.
    The ID format differs (ICA-1 prefixes with ``global/``; ICA-2 does not), but the
    parsing logic is identical — just extract ``data[].id``.
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = _build_headers(api_key)
    timeout = httpx.Timeout(connect=10.0, write=10.0, read=30.0, pool=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)

        if response.status_code != 200:
            logger.warning(
                "Failed to fetch models from %s (%s): HTTP %d — %s",
                backend_name, url, response.status_code, response.text[:300],
            )
            return []

        data = response.json()
        items = data.get("data") or []
        model_ids = [item["id"] for item in items if isinstance(item, dict) and item.get("id")]
        logger.info("Fetched %d models from %s (%s)", len(model_ids), backend_name, url)
        return model_ids

    except httpx.TimeoutException as exc:
        logger.warning("Timeout fetching models from %s (%s): %s", backend_name, url, exc)
        return []
    except httpx.RequestError as exc:
        logger.warning("Request error fetching models from %s (%s): %s", backend_name, url, exc)
        return []
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse models response from %s (%s): %s", backend_name, url, exc)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def refresh_models(settings: Any) -> dict[str, Any]:
    """Fetch /models from ICA-1 (and ICA-2 if configured), update the in-memory cache.

    Returns a summary dict suitable for returning from the admin refresh endpoint.
    """
    global _cache

    ica1_models = await _fetch_models(
        settings.openai_base_url,
        settings.openai_api_key,
        "ica1",
    )

    ica2_models: list[str] = []
    if settings.ica2_base_url and settings.ica2_api_key:
        ica2_models = await _fetch_models(
            settings.ica2_base_url,
            settings.ica2_api_key,
            "ica2",
        )
    else:
        logger.debug("ICA-2 not configured — skipping ICA-2 model fetch")

    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _cache = {
        "ica1": ica1_models,
        "ica2": ica2_models,
        "refreshed_at": refreshed_at,
    }

    logger.info(
        "Model registry refreshed: ica1=%d models, ica2=%d models",
        len(ica1_models), len(ica2_models),
    )
    return {
        "ica1_models": ica1_models,
        "ica2_models": ica2_models,
        "total": len(ica1_models) + len(ica2_models),
        "refreshed_at": refreshed_at,
    }


def get_all_model_entries(settings: Any) -> list[dict[str, Any]]:
    """Return all known models as a list of dicts with backend labels.

    Sources (merged, deduplicated):
    1. Models discovered from ICA-1 (in-memory cache)
    2. Models discovered from ICA-2 (in-memory cache)
    3. ``DEFAULT_MODEL`` (always included)
    4. Any models referenced in ``MODEL_ROUTING`` values

    Priority for backend labelling when a model ID appears in both caches:
    ICA-1 wins (unlimited token capacity).
    """
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []

    def _add(model_id: str, backend: str) -> None:
        if model_id not in seen:
            seen.add(model_id)
            entries.append({
                "type": "model",
                "id": model_id,
                "display_name": model_id,
                "backend": backend,
            })

    # ICA-1 models first (preferred when model exists on both)
    for mid in _cache.get("ica1") or []:
        _add(mid, "ica1")

    # ICA-2 models
    for mid in _cache.get("ica2") or []:
        _add(mid, "ica2")

    # Ensure DEFAULT_MODEL is always present
    default_model = getattr(settings, "default_model", "")
    if default_model:
        _add(default_model, "ica1")

    # Include models referenced in model_routing.json values
    try:
        import os
        routing_file = getattr(settings, "model_routing_file", "model_routing.json")
        if os.path.isfile(routing_file):
            with open(routing_file, encoding="utf-8") as fh:
                routing: dict[str, Any] = json.load(fh)
            for key, entry in routing.items():
                if key.startswith("_"):
                    continue
                if isinstance(entry, dict) and entry.get("model"):
                    backend_label = entry.get("backend", "ica1")
                    _add(entry["model"], backend_label)
    except (json.JSONDecodeError, OSError, ValueError):
        pass

    return entries