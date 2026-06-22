"""Anthropic-to-OpenAI proxy server entry point."""

import logging
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse as StarletteStreamingResponse


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
    # Debug logging
    debug_mode: bool = False  # set DEBUG_MODE=true to enable request/response file logging
    debug_log_dir: str = "logs"  # directory where daily debug logs are written
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
# Debug logging setup
# ---------------------------------------------------------------------------

_debug_log = None  # set below when debug_mode is on

if settings.debug_mode:
    from services.debug_logger import setup_debug_logger

    _debug_log = setup_debug_logger(settings.debug_log_dir)
    logger.info(
        "Debug mode ENABLED — writing request/response logs to: %s/",
        settings.debug_log_dir,
    )

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


# ---------------------------------------------------------------------------
# Debug middleware — only active when debug_mode is True
# ---------------------------------------------------------------------------


class DebugLoggingMiddleware(BaseHTTPMiddleware):
    """Capture every request body and response body and write them to the
    daily-rotating debug log file in human-readable (pretty-printed) form.

    Streaming (SSE) responses are transparently tee-d: the bytes are
    collected as they flow through, so the client receives them in real time
    while the full payload is logged once streaming completes.
    """

    def __init__(self, app) -> None:  # type: ignore[override]
        super().__init__(app)
        from services.debug_logger import get_debug_logger

        self._log = get_debug_logger()

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if self._log is None:
            return await call_next(request)

        from services.debug_logger import log_request, log_response

        request_id = uuid.uuid4().hex[:12]

        # Read request body; Starlette caches it on the scope so the route
        # handler can still read it normally after we consume it here.
        raw_body = await request.body()

        log_request(
            logger=self._log,
            request_id=request_id,
            method=request.method,
            path=str(request.url),
            headers=dict(request.headers),
            body=raw_body,
        )

        # Store request_id on request.state so route handlers can pass it
        # through to forward_request / stream_request for upstream log correlation.
        request.state.debug_request_id = request_id

        # Call the actual handler
        response = await call_next(request)

        content_type = response.headers.get("content-type", "")
        is_streaming = "text/event-stream" in content_type

        # Collect body chunks while still forwarding them to the client.
        body_chunks: list[bytes] = []

        if is_streaming:
            # Wrap the original async body iterator to tee bytes into body_chunks,
            # then log after the last chunk is yielded.
            original_iterator = response.body_iterator  # type: ignore[attr-defined]

            async def _tee_and_log():
                async for chunk in original_iterator:
                    body_chunks.append(chunk)
                    yield chunk
                # All chunks sent — now safe to log
                log_response(
                    logger=self._log,
                    request_id=request_id,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=b"".join(body_chunks),
                    is_streaming=True,
                )

            return StarletteStreamingResponse(
                content=_tee_and_log(),
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )
        else:
            # Non-streaming: buffer entire body, log it, return it.
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                body_chunks.append(chunk)
            full_body = b"".join(body_chunks)

            log_response(
                logger=self._log,
                request_id=request_id,
                status_code=response.status_code,
                headers=dict(response.headers),
                body=full_body,
                is_streaming=False,
            )

            from starlette.responses import Response as StarletteResponse

            return StarletteResponse(
                content=full_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )


if settings.debug_mode:
    app.add_middleware(DebugLoggingMiddleware)
    logger.info("DebugLoggingMiddleware registered.")


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


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
