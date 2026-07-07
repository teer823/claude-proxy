"""Router for the Anthropic Messages API endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from schemas.anthropic import MessagesRequest
from services.proxy import forward_request, stream_request, stream_to_completion, strip_thinking_segment
from services.translator import (
    anthropic_to_openai_request,
    openai_stream_to_anthropic_events,
    openai_to_anthropic_response,
    tools_to_system_prompt,
)
from services.web_search import perform_web_search

logger = logging.getLogger(__name__)

router = APIRouter(tags=["messages"])

# Safety cap: maximum tool-call iterations in the agentic loop.
_MAX_TOOL_ITERATIONS = 5


def _get_settings():
    """Import settings lazily to avoid circular imports."""
    from main import settings
    return settings


def _make_request_id() -> str:
    """Generate a short unique ID for correlating log lines for a single request."""
    return uuid.uuid4().hex[:8]


def _get_or_make_request_id(http_request: Request) -> str:
    """Return the debug request_id stored by the middleware, or generate a new one.

    Also stores the generated ID on request.state so downstream helpers can use it.
    """
    rid = getattr(http_request.state, "debug_request_id", None)
    if not rid:
        rid = _make_request_id()
        http_request.state.debug_request_id = rid
    return rid


def _build_upstream_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


# "" prefix built via chr() to survive markdown rendering of this source file.
_SSE_DATA_PREFIX = chr(100) + chr(97) + chr(116) + chr(97) + chr(58)  # d-a-t-a-:


def _sse_event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\n{_SSE_DATA_PREFIX} {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------

def _tool_type(tool: Any) -> str | None:
    """Return the type field of a tool (dict or Pydantic model)."""
    if isinstance(tool, dict):
        return tool.get("type")
    return getattr(tool, "type", None)


def _is_web_search_tool(tool: Any) -> bool:
    """Return True if the tool is a built-in Anthropic web_search tool."""
    t = _tool_type(tool)
    if t is None:
        return False
    return t.startswith("web_search")


# ---------------------------------------------------------------------------
# Claude Code built-in tool conversion
# ---------------------------------------------------------------------------

# Maps canonical name to equivalent function-type tool definition.
# Claude Code auto mode sends built-in tool types like "bash_20250124";
# the proxy converts them to function tools so IBM ICA can handle them.
_BUILTIN_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "bash": {
        "type": "function",
        "name": "bash",
        "description": (
            "Run commands in a bash shell. "
            "Use this to execute shell commands, run scripts, install packages, "
            "navigate the filesystem, and interact with the operating system."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute.",
                },
                "restart": {
                    "type": "boolean",
                    "description": "Restart the bash shell session (clears environment).",
                },
            },
            "required": ["command"],
        },
    },
    "str_replace_based_edit_tool": {
        "type": "function",
        "name": "str_replace_based_edit_tool",
        "description": (
            "View, create, and edit files. "
            "Supports commands: view, create, str_replace, insert, delete, undo_edit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace", "insert", "delete", "undo_edit"],
                    "description": "The editing command to execute.",
                },
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file or directory.",
                },
                "file_text": {
                    "type": "string",
                    "description": "Content to write when creating a new file.",
                },
                "old_str": {
                    "type": "string",
                    "description": "Text to search for and replace (str_replace command).",
                },
                "new_str": {
                    "type": "string",
                    "description": "Replacement text (str_replace command).",
                },
                "insert_line": {
                    "type": "integer",
                    "description": "Line number after which to insert text (insert command).",
                },
                "new_file": {
                    "type": "string",
                    "description": "Text to insert (insert command).",
                },
            },
            "required": ["command", "path"],
        },
    },
    "computer": {
        "type": "function",
        "name": "computer",
        "description": "Control the computer using mouse, keyboard, and screenshot actions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The computer action to perform.",
                },
                "coordinate": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "X,Y pixel coordinates for mouse actions.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type.",
                },
            },
            "required": ["action"],
        },
    },
    "execute_office_js": {
        "type": "function",
        "name": "execute_office_js",
        "description": (
            "Execute Office JavaScript (Office.js) API code in the context of a Microsoft Office "
            "application. Use this to interact with Word, Excel, PowerPoint, or other Office host "
            "applications via the Office.js API."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Office JavaScript code to execute.",
                },
            },
            "required": ["code"],
        },
    },
}


def _builtin_tool_canonical_name(ttype: str) -> str | None:
    """Map a built-in tool type string to its canonical function-schema key, or None."""
    if ttype.startswith("bash_"):
        return "bash"
    if ttype.startswith("str_replace_based_edit_tool_"):
        return "str_replace_based_edit_tool"
    if ttype.startswith("text_editor_"):
        # text_editor_* is the same functional interface as str_replace_based_edit_tool
        return "str_replace_based_edit_tool"
    if ttype.startswith("computer_"):
        return "computer"
    if ttype.startswith("execute_office_js_"):
        return "execute_office_js"
    # Also handle the bare name without version suffix
    if ttype == "execute_office_js":
        return "execute_office_js"
    return None


# Maps tool name → canonical schema key for tools that arrive with type='custom'.
# Some environments (e.g. Outlook add-in) send execute_office_js with type='custom'
# instead of type='execute_office_js_*'.
_CUSTOM_TYPE_NAME_MAP: dict[str, str] = {
    "execute_office_js": "execute_office_js",
}


def _is_known_builtin_tool(tool: Any) -> bool:
    """Return True if the tool is a known Claude Code built-in that can be converted."""
    t = _tool_type(tool)
    if t is None:
        return False
    if _builtin_tool_canonical_name(t) is not None:
        return True
    # Also handle tools that use type='custom' with a known name
    if t == "custom":
        name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
        if name and name in _CUSTOM_TYPE_NAME_MAP:
            return True
    return False


def _check_unsupported_tools(request: MessagesRequest) -> JSONResponse | None:
    """Return a 400 error if any truly unsupported built-in Anthropic tools are present.

    Allowed through:
      - type is None or 'function'  (standard custom tools)
      - type starts with 'web_search'  (handled by proxy agentic loop)
      - type is a known Claude Code built-in (bash_*, text_editor_*, etc.) — converted by
        _convert_builtin_tools() before forwarding to upstream
    All other non-function built-in types are rejected with 400.
    """
    if not request.tools:
        return None
    for tool in request.tools:
        ttype = _tool_type(tool)
        tool_name = (
            tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "unknown")
        )
        # Allow: no type, 'function', 'custom' (Office add-in / generic custom tools),
        # web_search_* (handled by agentic loop), and known Claude Code builtins.
        if ttype and ttype not in ("function", "custom") and not _is_web_search_tool(tool) and not _is_known_builtin_tool(tool):
            return JSONResponse(
                status_code=400,
                content={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "code": "unsupported_feature",
                        "message": (
                            f"Built-in Anthropic tool '{tool_name}' (type='{ttype}') "
                            "is not supported by this proxy."
                        ),
                    },
                },
            )
    return None


def _convert_builtin_tools(request: MessagesRequest) -> tuple[MessagesRequest, bool]:
    """Replace Claude Code built-in tools with equivalent function-type tools.

    Returns (modified_request, had_builtins).  When had_builtins is True the
    caller should ensure the translator uses XML mode for the IBM ICA backend.
    """
    if not request.tools:
        return request, False

    had_builtins = any(_is_known_builtin_tool(t) for t in request.tools)
    if not had_builtins:
        return request, False

    converted_tools = []
    seen_canonical: set[str] = set()
    for tool in request.tools:
        ttype = _tool_type(tool)
        if ttype and _is_known_builtin_tool(tool):
            canonical = _builtin_tool_canonical_name(ttype)
            # Fall back to name-based lookup for type='custom' tools
            if canonical is None and ttype == "custom":
                name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
                canonical = _CUSTOM_TYPE_NAME_MAP.get(name or "")
            if canonical and canonical not in seen_canonical:
                schema = _BUILTIN_TOOL_SCHEMAS[canonical]
                converted_tools.append(schema)
                seen_canonical.add(canonical)
                logger.debug(
                    "Converting built-in tool type=%r to function tool name=%r",
                    ttype,
                    canonical,
                )
        else:
            tool_dict = tool if isinstance(tool, dict) else tool.model_dump()
            converted_tools.append(tool_dict)

    data = request.model_dump()
    data["tools"] = converted_tools
    return MessagesRequest(**data), True


# ---------------------------------------------------------------------------
# Streaming (non-web-search path)
# ---------------------------------------------------------------------------

async def _stream_anthropic_events(
    upstream_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    original_model: str,
    read_timeout: float = 300.0,
    request_id: str | None = None,
) -> AsyncIterator[str]:
    """Translate OpenAI SSE stream into Anthropic SSE events."""
    message_id = f"msg_{uuid.uuid4().hex}"
    block_index = 0
    current_tool_calls: dict[int, dict[str, Any]] = {}
    sent_message_start = False
    sent_content_block_start: dict[int, bool] = {}
    accumulated_text = ""
    usage_data: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}

    yield _sse_event("ping", {"type": "ping"})

    # Hold an explicit reference to the inner async generator so we can close
    # it eagerly when the client disconnects or an exception occurs.
    # Without this, Python's async-generator finalizer may schedule aclose()
    # while the generator coroutine is still on the call stack, producing:
    #   RuntimeError: aclose(): asynchronous generator is already running
    inner = strip_thinking_segment(stream_request(
        upstream_url, headers, payload,
        read_timeout=read_timeout,
        request_id=request_id,
    ))
    try:
        async for chunk in inner:
            events, block_index, sent_message_start, sent_content_block_start, accumulated_text, usage_data = (
                openai_stream_to_anthropic_events(
                    chunk=chunk,
                    message_id=message_id,
                    model=original_model,
                    block_index=block_index,
                    current_tool_calls=current_tool_calls,
                    sent_message_start=sent_message_start,
                    sent_content_block_start=sent_content_block_start,
                    accumulated_text=accumulated_text,
                    usage_data=usage_data,
                )
            )
            for event in events:
                yield _sse_event(event["type"], event)
    except asyncio.CancelledError:
        # Client disconnected — close the upstream generator before re-raising
        # so httpx can tear down the connection cleanly.
        await inner.aclose()
        raise
    except Exception:
        await inner.aclose()
        raise
    else:
        # Normal completion — aclose() is a no-op on an exhausted generator
        # but ensures httpx resources are released promptly.
        await inner.aclose()


# ---------------------------------------------------------------------------
# Web-search agentic loop helpers
# ---------------------------------------------------------------------------

# Web-search function tool definition injected into requests for the upstream model.
_WEB_SEARCH_FUNCTION_TOOL = {
    "type": "function",
    "name": "web_search",
    "description": (
        "Search the web for up-to-date information. "
        "Use this tool whenever you need current facts, recent events, or information "
        "beyond your training data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web.",
            }
        },
        "required": ["query"],
    },
}


def _strip_web_search_tools(request: MessagesRequest) -> tuple[MessagesRequest, bool]:
    """Replace web_search built-in tools with an equivalent function tool definition.

    Returns a (modified_request, had_web_search) tuple.
    """
    if not request.tools:
        return request, False

    had_web_search = any(_is_web_search_tool(t) for t in request.tools)
    if not had_web_search:
        return request, False

    remaining_tools = [t for t in request.tools if not _is_web_search_tool(t)]
    data = request.model_dump()
    data["tools"] = [
        (t if isinstance(t, dict) else t.model_dump()) for t in remaining_tools
    ]
    data["tools"].append(_WEB_SEARCH_FUNCTION_TOOL)
    if not data.get("tool_choice"):
        data["tool_choice"] = {"type": "auto"}
    return MessagesRequest(**data), True


def _append_tool_result(
    request: MessagesRequest,
    assistant_response: dict[str, Any],
    tool_use_id: str,
    tool_result_content: str,
) -> MessagesRequest:
    """Append the assistant message + a tool_result user message to the conversation."""
    data = request.model_dump()

    assistant_content = assistant_response.get("content", [])
    data["messages"].append({"role": "assistant", "content": assistant_content})

    data["messages"].append({
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": tool_result_content,
            }
        ],
    })
    return MessagesRequest(**data)


async def _run_web_search_agentic_loop(
    request: MessagesRequest,
    upstream_url: str,
    headers: dict[str, str],
    provider: str,
    tavily_api_key: str,
    read_timeout: float = 300.0,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Execute the agentic tool-call loop for web_search.

    Returns the final Anthropic-format response dict.
    """
    current_request, _ = _strip_web_search_tools(request)
    rid_prefix = f"[{request_id}] " if request_id else ""

    for iteration in range(_MAX_TOOL_ITERATIONS):
        logger.debug("%sweb_search loop iter=%d/%d", rid_prefix, iteration + 1, _MAX_TOOL_ITERATIONS)

        req_data = current_request.model_dump()

        tool_hint = tools_to_system_prompt(req_data.get("tools") or [_WEB_SEARCH_FUNCTION_TOOL])
        existing_system = req_data.get("system") or ""
        if isinstance(existing_system, list):
            existing_system = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in existing_system
            )
        req_data["system"] = (tool_hint + "\n\n" + existing_system).strip()

        req_data["tools"] = None
        req_data["tool_choice"] = None

        iter_request = MessagesRequest(**req_data)
        openai_req = anthropic_to_openai_request(iter_request, _get_settings().default_model)
        payload = openai_req.model_dump(exclude_none=True)
        payload["stream"] = False

        oai_response = await stream_to_completion(upstream_url, headers, payload, read_timeout=read_timeout, request_id=request_id)
        anthropic_response = openai_to_anthropic_response(oai_response, current_request)

        content_blocks = anthropic_response.get("content", [])
        web_search_calls = [
            b for b in content_blocks
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "web_search"
        ]

        if not web_search_calls:
            logger.debug("Agentic loop complete after %d iteration(s)", iteration + 1)
            anthropic_response["model"] = request.model
            return anthropic_response

        for call in web_search_calls:
            tool_use_id = call.get("id", f"toolu_{uuid.uuid4().hex[:8]}")
            query = call.get("input", {}).get("query", "")
            logger.info(
                "%sweb_search iter=%d/%d query=%r provider=%s",
                rid_prefix,
                iteration + 1,
                _MAX_TOOL_ITERATIONS,
                query,
                provider,
            )
            try:
                search_result = await perform_web_search(
                    query=query,
                    provider=provider,
                    tavily_api_key=tavily_api_key or None,
                )
                logger.debug(
                    "%sweb_search result: %d chars",
                    rid_prefix,
                    len(search_result),
                )
            except Exception as exc:
                logger.error("%sweb_search failed: %s", rid_prefix, exc)
                search_result = f"Search failed: {exc}"

            current_request = _append_tool_result(
                current_request, anthropic_response, tool_use_id, search_result
            )

    logger.warning("%sagentic loop hit iteration cap (%d)", rid_prefix, _MAX_TOOL_ITERATIONS)
    anthropic_response["model"] = request.model
    return anthropic_response


async def _stream_from_anthropic_response(
    response: dict[str, Any],
    original_model: str,
) -> AsyncIterator[str]:
    """Convert a completed Anthropic response dict into SSE events for streaming."""
    message_id = response.get("id", f"msg_{uuid.uuid4().hex}")
    content_blocks = response.get("content", [])
    stop_reason = response.get("stop_reason", "end_turn")
    usage = response.get("usage", {"input_tokens": 0, "output_tokens": 0})

    yield _sse_event("ping", {"type": "ping"})

    yield _sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": original_model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0},
        },
    })

    for idx, block in enumerate(content_blocks):
        btype = block.get("type") if isinstance(block, dict) else "text"
        if btype == "text":
            text = block.get("text", "") if isinstance(block, dict) else str(block)
            yield _sse_event("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            yield _sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "text_delta", "text": text},
            })
            yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": idx})
        elif btype == "tool_use":
            yield _sse_event("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                    "name": block.get("name", ""),
                    "input": {},
                },
            })
            yield _sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })
            yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": idx})

    yield _sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": usage.get("output_tokens", 0)},
    })
    yield _sse_event("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# Token counting endpoint
# ---------------------------------------------------------------------------

# Rough chars-per-token ratio for estimation. Claude/GPT tokenizers average
# ~3.5-4 chars per token on English text and code; 4 keeps the estimate
# conservative (slightly low) so clients trim context a little early rather
# than overflow the upstream limit.
_CHARS_PER_TOKEN = 4


@router.post("/v1/messages/count_tokens")
async def count_tokens(http_request: Request) -> dict[str, Any]:
    """Estimate the input token count for a Messages API request.

    Anthropic clients (Claude Code included) call this endpoint to decide when
    to compact or trim conversation context. Without it the proxy returns 404
    and clients fall back to guessing. The upstream is an arbitrary
    OpenAI-compatible model with no tokenizer available here, so this returns
    a character-based estimate rather than an exact count — good enough for
    context-window management, not for billing.

    Parsed leniently (raw JSON, not the MessagesRequest schema) because
    count_tokens payloads omit fields that are required for /v1/messages,
    e.g. max_tokens.
    """
    try:
        body = await http_request.json()
    except Exception:
        return {"input_tokens": 0}

    total_chars = 0
    for key in ("messages", "system", "tools"):
        value = body.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            total_chars += len(value)
        else:
            # Serialized JSON length approximates the prompt-rendered size of
            # structured content (content blocks, tool schemas) well enough.
            total_chars += len(json.dumps(value, ensure_ascii=False))

    return {"input_tokens": max(1, total_chars // _CHARS_PER_TOKEN)}


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@router.post("/v1/messages")
async def create_message(
    request: MessagesRequest,
    http_request: Request,
) -> Any:
    """
    Proxy Anthropic Messages API requests to an OpenAI-compatible backend.
    Supports both streaming and non-streaming responses.
    Claude Code built-in tools (bash, text_editor, etc.) are converted to
    equivalent function tools before forwarding to IBM ICA.
    Web search built-in tools are handled by the proxy agentic loop.
    """
    # Assign a request ID early so all log lines for this request are correlated.
    rid = _get_or_make_request_id(http_request)
    start_time = time.monotonic()

    # Compute summary fields for the entry log line.
    tool_count = len(request.tools) if request.tools else 0
    msg_count = len(request.messages)
    logger.info(
        "[%s] → POST /v1/messages model=%s stream=%s tools=%d messages=%d",
        rid,
        request.model,
        request.stream,
        tool_count,
        msg_count,
    )

    if (error_response := _check_unsupported_tools(request)) is not None:
        elapsed = time.monotonic() - start_time
        logger.info("[%s] ← 400 unsupported_tool duration=%.2fs", rid, elapsed)
        return error_response

    # Convert Claude Code built-in tools (bash_*, text_editor_*, etc.) to function tools
    request, _ = _convert_builtin_tools(request)

    settings = _get_settings()
    target_model = settings.default_model
    # Claude Code sends haiku-class model names for cheap background chores
    # (conversation titles, summarization). Route them to SMALL_MODEL when
    # configured instead of burning the big default model on utility calls.
    if settings.small_model and "haiku" in (request.model or "").lower():
        target_model = settings.small_model
    logger.debug(
        "[%s] target_model=%r has_web_search=%s",
        rid,
        target_model,
        bool(request.tools and any(_is_web_search_tool(t) for t in request.tools)),
    )

    upstream_url = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
    headers = _build_upstream_headers(settings.openai_api_key)

    has_web_search = bool(
        request.tools and any(_is_web_search_tool(t) for t in request.tools)
    )

    # --- Web-search agentic loop path ---
    if has_web_search:
        try:
            final_response = await _run_web_search_agentic_loop(
                request=request,
                upstream_url=upstream_url,
                headers=headers,
                provider=settings.web_search_provider,
                tavily_api_key=settings.tavily_api_key,
                read_timeout=settings.upstream_read_timeout,
                request_id=rid,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start_time
            logger.error(
                "[%s] ← ERROR web_search loop failed: %s: %s  duration=%.2fs",
                rid, type(exc).__name__, exc, elapsed,
            )
            raise

        elapsed = time.monotonic() - start_time
        usage = final_response.get("usage", {})
        logger.info(
            "[%s] ← 200 stop_reason=%s input_tokens=%d output_tokens=%d duration=%.2fs",
            rid,
            final_response.get("stop_reason", "?"),
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            elapsed,
        )

        if request.stream:
            return StreamingResponse(
                _stream_from_anthropic_response(final_response, request.model),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        return JSONResponse(content=final_response)

    # --- Standard proxy path ---
    openai_request = anthropic_to_openai_request(request, target_model)
    payload = openai_request.model_dump(exclude_none=True)

    if request.stream:
        async def _logged_stream() -> AsyncIterator[str]:
            """Wrap the SSE generator to log completion metrics after stream ends."""
            final_usage: dict[str, Any] = {}
            final_stop_reason: str = "?"
            try:
                async for chunk_str in _stream_anthropic_events(
                    upstream_url, headers, payload, request.model,
                    read_timeout=settings.upstream_read_timeout,
                    request_id=rid,
                ):
                    # Extract usage/stop_reason from the last message_delta event
                    # without buffering the whole stream — just peek at the JSON.
                    try:
                        evt_data = chunk_str.split("\n", 1)[-1].strip()
                        if evt_data.startswith(" "):
                            evt_data = evt_data.strip()
                        parsed = json.loads(evt_data)
                        if parsed.get("type") == "message_delta":
                            final_stop_reason = parsed.get("delta", {}).get("stop_reason", "?") or "?"
                            final_usage = parsed.get("usage", {})
                    except Exception:
                        pass
                    yield chunk_str
            except Exception as exc:
                elapsed = time.monotonic() - start_time
                logger.error(
                    "[%s] ← ERROR stream failed: %s: %s  duration=%.2fs",
                    rid, type(exc).__name__, exc, elapsed,
                )
                raise
            else:
                elapsed = time.monotonic() - start_time
                logger.info(
                    "[%s] ← 200 stop_reason=%s input_tokens=%d output_tokens=%d duration=%.2fs (stream)",
                    rid,
                    final_stop_reason,
                    final_usage.get("input_tokens", 0),
                    final_usage.get("output_tokens", 0),
                    elapsed,
                )

        return StreamingResponse(
            _logged_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming standard path — use stream_to_completion to avoid IBM ICA's
    # ~180 s gateway timeout that silently returns HTTP 404 on long non-streaming requests.
    try:
        oai_response = await stream_to_completion(
            upstream_url, headers, payload,
            read_timeout=settings.upstream_read_timeout,
            request_id=rid,
        )
    except Exception as exc:
        elapsed = time.monotonic() - start_time
        logger.error(
            "[%s] ← ERROR forward failed: %s: %s  duration=%.2fs",
            rid, type(exc).__name__, exc, elapsed,
        )
        raise

    anthropic_response = openai_to_anthropic_response(oai_response, request)
    elapsed = time.monotonic() - start_time
    usage = anthropic_response.get("usage", {})
    logger.info(
        "[%s] ← 200 stop_reason=%s input_tokens=%d output_tokens=%d duration=%.2fs",
        rid,
        anthropic_response.get("stop_reason", "?"),
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        elapsed,
    )
    return JSONResponse(content=anthropic_response)
