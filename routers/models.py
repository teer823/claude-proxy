"""Router for the models listing endpoint.

Claude Code (and other Anthropic-compatible clients) may call ``GET
/v1/models`` to discover available models.  The proxy pins everything to the
configured ``DEFAULT_MODEL``, so we simply advertise that single model in the
Anthropic list format.
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
    """Return the configured default model in Anthropic's model-list format."""
    settings = _get_settings()
    model_id = settings.default_model
    return {
        "data": [
            {
                "type": "model",
                "id": model_id,
                "display_name": model_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ],
        "has_more": False,
        "first_id": model_id,
        "last_id": model_id,
    }


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    """Return details for a single model (always the configured default)."""
    settings = _get_settings()
    return {
        "type": "model",
        "id": model_id,
        "display_name": settings.default_model,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
