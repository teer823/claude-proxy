"""Debug request/response logger with daily rotating log files.

Activated only when ``debug_mode=True`` in Settings. Each calendar day gets its
own log file:

    <debug_log_dir>/proxy_debug_YYYY-MM-DD.log

The logger is isolated from the root application logger so enabling it never
affects log levels on other handlers.
"""

from __future__ import annotations

import json
import logging
import os
import os.path
from logging.handlers import TimedRotatingFileHandler
from typing import Any

# ---------------------------------------------------------------------------
# Internal logger instance
# ---------------------------------------------------------------------------

_DEBUG_LOGGER_NAME = "proxy.debug"
_debug_logger: logging.Logger | None = None


def setup_debug_logger(log_dir: str) -> logging.Logger:
    """Create (or return existing) daily-rotating debug file logger.

    Safe to call multiple times — returns the same logger after the first call.
    """
    global _debug_logger
    if _debug_logger is not None:
        return _debug_logger

    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(_DEBUG_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    # Don't propagate to root logger — we only want these messages in the file.
    logger.propagate = False

    if not logger.handlers:
        log_path = os.path.join(log_dir, "proxy_debug.log")
        # Rotate at midnight; keep 30 days; suffix = YYYY-MM-DD
        handler = _ResilientTimedRotatingFileHandler(
            filename=log_path,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
            utc=False,
        )
        handler.suffix = "%Y-%m-%d"
        formatter = logging.Formatter(
            "%(asctime)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    _debug_logger = logger
    return logger


class _ResilientTimedRotatingFileHandler(TimedRotatingFileHandler):
    """TimedRotatingFileHandler that re-creates the log file if it is deleted
    while the application is running.

    Python's standard FileHandler keeps the file descriptor open; if the file
    is removed on disk the descriptor still points to the deleted inode and
    logs are silently discarded.  This subclass checks for file existence on
    every ``emit()`` call and reopens (creating a new file) when necessary.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.stream is not None and not os.path.exists(self.baseFilename):
                self.stream.close()
                self.stream = self._open()
        except Exception:
            pass  # never let log machinery crash the app
        super().emit(record)


def get_debug_logger() -> logging.Logger | None:
    """Return the debug logger if it has been initialised, else None."""
    return _debug_logger


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

def _pretty(obj: Any) -> str:
    """Return a human-readable JSON string for *obj*.

    Falls back to ``repr()`` if the object is not JSON-serialisable.
    """
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(obj)


def _truncate(text: str, max_chars: int = 50_000) -> str:
    """Truncate very long strings and append a notice."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n\n... [TRUNCATED — {len(text) - max_chars} chars omitted] ...\n\n"
        + text[-half:]
    )


def _parse_body(raw: bytes) -> Any:
    """Try to parse bytes as JSON; fall back to decoded string."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return f"<{len(raw)} raw bytes>"


def _parse_sse_body(raw: bytes) -> list[dict] | str:
    """Parse an SSE response body into a list of event dicts.

    Each ```` line is decoded as JSON where possible, making the log
    far easier to read than a wall of ``data:{...}`` lines.
    """
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return f"<{len(raw)} raw bytes>"

    events: list[dict] = []
    current: dict[str, str] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            if current:
                events.append(current)
                current = {}
        elif line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith(""):
            payload = line[len(""):].strip()
            try:
                current["data"] = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                current["data"] = payload
        elif line.startswith(":"):
            current["comment"] = line[1:].strip()

    if current:
        events.append(current)

    return events


# ---------------------------------------------------------------------------
# Public log functions
# ---------------------------------------------------------------------------

def log_request(
    logger: logging.Logger,
    request_id: str,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
) -> None:
    """Write a formatted request entry to the debug log."""
    parsed = _parse_body(body)
    safe_headers = {k: (v if k.lower() != "authorization" else "Bearer ***") for k, v in headers.items()}

    entry = (
        f"{'=' * 80}\n"
        f"REQUEST  [{request_id}]  {method} {path}\n"
        f"{'-' * 40} headers\n"
        f"{_pretty(safe_headers)}\n"
        f"{'-' * 40} body\n"
        f"{_truncate(_pretty(parsed))}\n"
    )
    logger.debug(entry)


def log_response(
    logger: logging.Logger,
    request_id: str,
    status_code: int,
    headers: dict[str, str],
    body: bytes,
    is_streaming: bool = False,
) -> None:
    """Write a formatted response entry to the debug log."""
    if is_streaming:
        parsed: Any = _parse_sse_body(body)
        body_str = _truncate(_pretty(parsed))
    else:
        parsed = _parse_body(body)
        body_str = _truncate(_pretty(parsed))

    stream_tag = " [STREAMING/SSE]" if is_streaming else ""
    entry = (
        f"RESPONSE [{request_id}]  HTTP {status_code}{stream_tag}\n"
        f"{'-' * 40} headers\n"
        f"{_pretty(dict(headers))}\n"
        f"{'-' * 40} body\n"
        f"{body_str}\n"
        f"{'=' * 80}\n"
    )
    logger.debug(entry)


def log_upstream_request(
    logger: logging.Logger,
    request_id: str,
    url: str,
    headers: dict[str, str],
    payload: Any,
) -> None:
    """Write the translated OpenAI-format request sent to the upstream (IBM ICA)."""
    safe_headers = {k: (v if k.lower() != "authorization" else "Bearer ***") for k, v in headers.items()}

    entry = (
        f"  UPSTREAM REQUEST  [{request_id}]  POST {url}\n"
        f"  {'-' * 38} headers\n"
        f"{_truncate(_pretty(safe_headers))}\n"
        f"  {'-' * 38} body (OpenAI format)\n"
        f"{_truncate(_pretty(payload))}\n"
    )
    logger.debug(entry)


def log_upstream_response(
    logger: logging.Logger,
    request_id: str,
    status_code: int,
    body: Any,
    is_streaming: bool = False,
) -> None:
    """Write the raw response received from the upstream (IBM ICA)."""
    stream_tag = " [STREAMING/SSE]" if is_streaming else ""
    entry = (
        f"  UPSTREAM RESPONSE [{request_id}]  HTTP {status_code}{stream_tag}\n"
        f"  {'-' * 38} body (OpenAI format)\n"
        f"{_truncate(_pretty(body))}\n"
    )
    logger.debug(entry)
