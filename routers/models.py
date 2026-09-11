"""Router for the models listing endpoint.

Claude Code (and other Anthropic-compatible clients) may call ``GET
/v1/models`` to discover available models.  The proxy merges model lists
fetched from ICA-1 and ICA-2 at startup (and on demand via the admin
refresh endpoint) and returns the combined inventory.
"""

import time

from fastapi import APIRouter

router = APIRouter(tags=["models"])


def _get_settings():
    """Import settings lazily to avoid circular imports."""
    from main import settings
    return settings


@router.get("/v1/models")
async def list_models():
    """Return all known models from ICA-1 and ICA-2 in Anthropic's model-list format.

    The list is sourced from the in-memory model registry (populated at startup
    and refreshed via ``POST /admin/refresh-models``).  Each entry includes a
    ``backend`` field indicating which upstream the model lives on.

    ICA-1 models are listed first (preferred when a model exists on both backends).
    """
    from services.model_registry import get_all_model_entries, get_cache
    settings = _get_settings()

    entries = get_all_model_entries(settings)
    cache = get_cache()

    # Add created_at to each entry
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data = [
        {
            "type": "model",
            "id": entry["id"],
            "display_name": entry["display_name"],
            "backend": entry["backend"],
            "created_at": now,
        }
        for entry in entries
    ]

    first_id = data[0]["id"] if data else None
    last_id = data[-1]["id"] if data else None

    return {
        "data": data,
        "has_more": False,
        "first_id": first_id,
        "last_id": last_id,
        "registry_refreshed_at": cache.get("refreshed_at"),
    }


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    """Return details for a single model.

    Looks up the model in the registry; falls back to the requested model_id
    with ICA-1 as the backend if it is not found.
    """
    from services.model_registry import get_all_model_entries
    settings = _get_settings()

    entries = get_all_model_entries(settings)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for entry in entries:
        if entry["id"] == model_id:
            return {
                "type": "model",
                "id": entry["id"],
                "display_name": entry["display_name"],
                "backend": entry["backend"],
                "created_at": now,
            }

    # Not found in registry — return it as-is (ICA-1 default)
    return {
        "type": "model",
        "id": model_id,
        "display_name": model_id,
        "backend": "ica1",
        "created_at": now,
    }


@router.post("/admin/refresh-models", tags=["admin"])
async def refresh_models():
    """Re-fetch the model list from ICA-1 and ICA-2 and update the in-memory registry.

    Call this endpoint whenever you want to pick up newly added or removed models
    without restarting the proxy.

    Returns a summary of the refreshed model lists.
    """
    from services.model_registry import refresh_models as _refresh_models
    settings = _get_settings()
    result = await _refresh_models(settings)
    return result