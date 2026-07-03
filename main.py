"""Anthropic-to-OpenAI proxy server entry point."""

import logging
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
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
    # Proxy authentication (for non-localhost access, e.g. via ngrok)
    proxy_api_key: str = ""  # when set, non-localhost requests must supply this key
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

import os as _os

_log_level = logging.DEBUG if settings.debug_mode else logging.INFO
_log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logging.basicConfig(
    level=_log_level,
    format=_log_format,
)
# Suppress uvicorn's per-request access log (200s are noise; errors still surface
# via the application logger and uvicorn's error logger).
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
# Suppress verbose httpx / httpcore connection logs — they add noise without value.
# In debug mode keep them at INFO so TLS/redirect info shows up if needed.
_http_log_level = logging.INFO if settings.debug_mode else logging.WARNING
logging.getLogger("httpx").setLevel(_http_log_level)
logging.getLogger("httpcore").setLevel(_http_log_level)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application log file — always write to <debug_log_dir>/proxy.log so log
# output is co-located with the debug request/response files and is not
# scattered across wherever the process was launched from.
# ---------------------------------------------------------------------------
_os.makedirs(settings.debug_log_dir, exist_ok=True)
_app_log_path = _os.path.join(settings.debug_log_dir, "proxy.log")
_file_handler = logging.FileHandler(_app_log_path, encoding="utf-8")
_file_handler.setLevel(_log_level)
_file_handler.setFormatter(logging.Formatter(_log_format))
logging.getLogger().addHandler(_file_handler)
logger.info("Application log file: %s", _app_log_path)

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

# Allow all origins so that browser-based clients and Claude Code can reach the
# proxy without CORS preflight failures.  OPTIONS requests will now return 200
# with the appropriate Access-Control-* headers instead of 405.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
from routers.messages import router as messages_router  # noqa: E402
from routers.models import router as models_router  # noqa: E402

app.include_router(messages_router)
app.include_router(models_router)


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
# API-key enforcement middleware
# Requests from localhost are always allowed.
# Requests from other origins (e.g. via ngrok) must supply
#   Authorization: Bearer <PROXY_API_KEY>
# Only active when PROXY_API_KEY is non-empty.
# ---------------------------------------------------------------------------

_LOCALHOST_IPS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
# Paths that are always public (health probe used by orchestrators).
_PUBLIC_PATHS = {"/health"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Enforce a static API key for non-localhost clients."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        required_key = settings.proxy_api_key
        # Feature disabled — let everything through.
        if not required_key:
            return await call_next(request)

        # Always allow health-check probes regardless of origin.
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        # Determine the real client IP.
        # ngrok (and most reverse proxies) set X-Forwarded-For.
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            client_ip = xff.split(",")[0].strip()
        else:
            client_ip = (request.client.host if request.client else "") or ""

        # Localhost → no key required.
        if client_ip in _LOCALHOST_IPS:
            return await call_next(request)

        # Non-localhost → validate key.
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            provided_key = auth_header[len("Bearer "):]
        elif request.headers.get("x-api-key", ""):
            provided_key = request.headers.get("x-api-key", "")
        else:
            provided_key = ""

        if provided_key != required_key:
            logger.warning(
                "Rejected unauthenticated request from %s %s %s",
                client_ip,
                request.method,
                request.url.path,
            )
            # Write a structured line to proxy_debug.log for audit purposes.
            try:
                import datetime as _dt

                _debug_log_path = _os.path.join(settings.debug_log_dir, "proxy_debug.log")
                _ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _user_agent = request.headers.get("user-agent", "-")
                _referer = request.headers.get("referer", "-")
                _line = (
                    f"{_ts} UNAUTH ip={client_ip} method={request.method}"
                    f" path={request.url.path}"
                    f" user-agent={_user_agent!r}"
                    f" referer={_referer!r}\n"
                )
                with open(_debug_log_path, "a", encoding="utf-8") as _f:
                    _f.write(_line)
            except Exception as _exc:
                logger.warning("Failed to write to proxy_debug.log: %s", _exc)
            return JSONResponse(
                status_code=401,
                content={
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "Invalid API key supplied in Authorization header.",
                    },
                },
            )

        return await call_next(request)


if settings.proxy_api_key:
    app.add_middleware(ApiKeyMiddleware)
    logger.info("ApiKeyMiddleware registered — non-localhost requests require a valid API key.")
else:
    logger.info("ApiKeyMiddleware disabled — PROXY_API_KEY is not set.")


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
