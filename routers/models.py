"""Router for the Anthropic Models API endpoint."""

import time
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["models"])

# Static list of Claude models exposed by this proxy.
# Claude Code queries this endpoint to discover available models.
_CLAUDE_MODELS = [
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-opus-4-0",
    "claude-sonnet-4-0",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-20240620",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
    "claude-3-haiku-20240307",
]

# Use a fixed epoch for `created` — these are static entries, not dynamically
# provisioned, so a stable timestamp avoids spurious cache invalidation.
_CREATED_AT = int(time.mktime(time.strptime("2024-01-01", "%Y-%m-%d")))


def _model_object(model_id: str) -> dict:
    return {
        "type": "model",
        "id": model_id,
        "display_name": model_id,
        "created_at": _CREATED_AT,
    }


@router.get("/v1/models")
async def list_models() -> JSONResponse:
    """Return the list of Claude models supported by this proxy.

    The response follows the Anthropic Models API format so that Claude Code
    and other Anthropic-compatible clients can discover available models.
    """
    data = [_model_object(m) for m in _CLAUDE_MODELS]
    logger.debug("GET /v1/models — returning %d models", len(data))
    return JSONResponse(
        content={
            "data": data,
            "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }
    )


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str) -> JSONResponse:
    """Return details for a single model.

    Returns 404 if the model ID is not in the supported list.
    """
    if model_id not in _CLAUDE_MODELS:
        logger.debug("GET /v1/models/%s — not found", model_id)
        return JSONResponse(
            status_code=404,
            content={
                "type": "error",
                "error": {
                    "type": "not_found_error",
                    "message": f"Model '{model_id}' not found.",
                },
            },
        )
    logger.debug("GET /v1/models/%s — found", model_id)
    return JSONResponse(content=_model_object(model_id))