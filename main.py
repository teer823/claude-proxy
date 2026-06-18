"""Anthropic-to-OpenAI proxy server entry point."""

import logging

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    openai_base_url: str = "https://sg.ica.ibm.com/ica/apis/v3"
    openai_api_key: str = "your-api-key-here"
    default_model: str = "global/anthropic.claude-sonnet-4-6"
    # Web search tool settings
    web_search_provider: str = "duckduckgo"  # "duckduckgo" or "tavily"
    tavily_api_key: str = ""
    # Timeout settings
    upstream_read_timeout: float = 300.0  # seconds to wait for upstream to respond/stream
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = Settings()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Suppress uvicorn's per-request access log (200s are noise; errors still surface
# via the application logger and uvicorn's error logger).
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Claude Code Proxy",
    description="Proxies Anthropic Messages API requests to an OpenAI-compatible endpoint.",
    version="1.0.0",
)

# Register routers
from routers.messages import router as messages_router  # noqa: E402

app.include_router(messages_router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Log FastAPI request validation errors (422) so they appear in the application log."""
    body = None
    try:
        body = await request.json()
    except Exception:
        pass
    logger.error(
        "Request validation error (422) %s %s — errors: %s — body: %s",
        request.method,
        request.url.path,
        exc.errors(),
        body,
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/health", tags=["health"])
async def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "default_model": settings.default_model}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "Starting proxy on port 8082 — upstream: %s — model: %s",
        settings.openai_base_url,
        settings.default_model,
    )
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8082,
        reload=False,
        log_level="info",
    )