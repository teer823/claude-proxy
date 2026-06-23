"""Async HTTP proxy service — forwards requests to the OpenAI-compatible backend."""

import json
import logging
from typing import Any, AsyncIterator

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)


def _get_debug_log():
    """Return the debug logger if debug mode is active, else None."""
    try:
        from services.debug_logger import get_debug_logger
        return get_debug_logger()
    except Exception:
        return None

# "" — built via chr() to survive markdown rendering
_DATA_PREFIX = chr(100) + chr(97) + chr(116) + chr(97) + chr(58)  # d-a-t-a-:


def _log_upstream_error(url: str, status_code: int, body: str) -> None:
    """Log a structured error message for a non-200 upstream response.

    Attempts to extract a machine-readable error code and description from the
    response body when it is JSON (e.g. OpenAI-style ``{"error": {"code": ...,
    "message": ...}}`` or a top-level ``{"message": ...}``).  Falls back to a
    plain body preview when the body is not valid JSON.
    """
    error_code: str | None = None
    error_message: str | None = None
    error_type: str | None = None

    try:
        data = json.loads(body)
        # OpenAI-style: {"error": {"code": ..., "message": ..., "type": ...}}
        if isinstance(data, dict):
            err = data.get("error") or {}
            if isinstance(err, dict):
                error_code = err.get("code") or err.get("status")
                error_message = err.get("message")
                error_type = err.get("type")
            # Flat style: {"message": ..., "code": ...}
            if error_message is None:
                error_message = data.get("message") or data.get("detail")
            if error_code is None:
                error_code = data.get("code") or data.get("status")
    except (json.JSONDecodeError, ValueError):
        pass  # body is not JSON; will log raw preview below

    if error_message or error_code:
        logger.error(
            "Upstream returned HTTP %s | url=%s | error_code=%s | error_type=%s | error_message=%s",
            status_code,
            url,
            error_code,
            error_type,
            error_message,
        )
    else:
        logger.error(
            "Upstream returned HTTP %s | url=%s | body_preview=%s",
            status_code,
            url,
            body[:500],
        )


async def forward_request(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    read_timeout: float = 300.0,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Send a non-streaming POST request and return the parsed JSON response.

    ``read_timeout`` controls how long (in seconds) to wait for the upstream to
    start or continue sending a response.  Connect and write timeouts are kept
    shorter since those phases are not affected by response-generation time.
    ``request_id`` is threaded through from the middleware for log correlation.
    """
    debug_log = _get_debug_log()
    if debug_log and request_id:
        from services.debug_logger import log_upstream_request
        log_upstream_request(debug_log, request_id, url, headers, payload)

    rid_tag = f"[{request_id}] " if request_id else ""
    logger.debug("%supstream → POST %s", rid_tag, url)

    timeout = httpx.Timeout(connect=10.0, write=60.0, read=read_timeout, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            logger.error(
                "Request to upstream timed out | url=%s | error_type=%s | detail=%s",
                url,
                type(exc).__name__,
                exc,
            )
            raise HTTPException(status_code=504, detail=f"Upstream request timed out: {exc}")
        except httpx.ConnectError as exc:
            logger.error(
                "Failed to connect to upstream | url=%s | error_type=%s | detail=%s",
                url,
                type(exc).__name__,
                exc,
            )
            raise HTTPException(status_code=502, detail=f"Upstream connection failed: {exc}")
        except httpx.RequestError as exc:
            logger.error(
                "Request to upstream failed | url=%s | error_type=%s | detail=%s",
                url,
                type(exc).__name__,
                exc,
            )
            raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}")

        logger.debug("%supstream ← %d (non-stream)", rid_tag, response.status_code)
        if response.status_code != 200:
            _log_upstream_error(url, response.status_code, response.text)
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Upstream error: {response.text[:500]}",
            )

        try:
            data = response.json()
        except Exception as exc:
            logger.error(
                "Failed to parse upstream JSON | url=%s | error_type=%s | detail=%s | body_preview=%s",
                url,
                type(exc).__name__,
                exc,
                response.text[:200],
            )
            raise HTTPException(status_code=502, detail="Upstream returned invalid JSON")

        if debug_log and request_id:
            from services.debug_logger import log_upstream_response
            log_upstream_response(debug_log, request_id, response.status_code, data, is_streaming=False)

        return data


async def stream_request(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    read_timeout: float = 300.0,
    request_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    Send a streaming POST request and yield parsed OpenAI SSE chunks as dicts.
    Skips [DONE] and malformed lines.

    ``read_timeout`` controls how long (in seconds) to wait between received
    bytes from the upstream SSE stream.
    ``request_id`` is threaded through from the middleware for log correlation.
    """
    debug_log = _get_debug_log()
    if debug_log and request_id:
        from services.debug_logger import log_upstream_request
        log_upstream_request(debug_log, request_id, url, headers, payload)

    rid_tag = f"[{request_id}] " if request_id else ""
    logger.debug("%supstream → POST %s (stream)", rid_tag, url)

    timeout = httpx.Timeout(connect=10.0, write=60.0, read=read_timeout, pool=5.0)
    chunks_for_log: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    body_text = body.decode(errors="replace")
                    _log_upstream_error(url, response.status_code, body_text)
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Upstream error: {body_text[:500]}",
                    )

                logger.debug("%supstream ← %d (stream)", rid_tag, response.status_code)
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if not line.startswith(_DATA_PREFIX):
                        continue
                    # Strip "" prefix; .strip() handles optional trailing space.
                    # IBM ICA sends "{...}" (no space); standard SSE sends " {...}"
                    data = line[len(_DATA_PREFIX):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("Skipping unparseable SSE line: %s", data[:200])
                        continue
                    if debug_log and request_id:
                        chunks_for_log.append(chunk)
                    yield chunk

        except httpx.TimeoutException as exc:
            logger.error(
                "Streaming request to upstream timed out | url=%s | error_type=%s | detail=%s",
                url,
                type(exc).__name__,
                exc,
            )
            raise HTTPException(status_code=504, detail=f"Upstream stream timed out: {exc}")
        except httpx.ConnectError as exc:
            logger.error(
                "Failed to connect to upstream (stream) | url=%s | error_type=%s | detail=%s",
                url,
                type(exc).__name__,
                exc,
            )
            raise HTTPException(status_code=502, detail=f"Upstream connection failed: {exc}")
        except httpx.RequestError as exc:
            logger.error(
                "Streaming request to upstream failed | url=%s | error_type=%s | detail=%s",
                url,
                type(exc).__name__,
                exc,
            )
            raise HTTPException(status_code=502, detail=f"Upstream stream failed: {exc}")

    if debug_log and request_id and chunks_for_log:
        from services.debug_logger import log_upstream_response
        log_upstream_response(debug_log, request_id, 200, chunks_for_log, is_streaming=True)
