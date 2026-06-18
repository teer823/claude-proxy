"""Router for the Anthropic Messages API endpoint."""

import json
import logging
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from schemas.anthropic import MessagesRequest
from services.proxy import forward_request, stream_request
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


def _build_upstream_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _sse_event(event_type: str, data: dict[str, Any]) -> str:
    """Format a single SSE message."""
    return f"event: {event_type}\n {json.dumps(data, ensure_ascii=False)}\n\n"


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


def _check_unsupported_tools(request: MessagesRequest) -> JSONResponse | None:
    """Return a 400 error if any unsupported built-in Anthropic tools are present.

    web_search_* tools are handled natively by this proxy and are therefore
    allowed through. All other non-function built-in types are rejected.
    """
    if not request.tools:
        return None
    for tool in request.tools:
        ttype = _tool_type(tool)
        tool_name = (
            tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "unknown")
        )
        if ttype and ttype != "function" and not _is_web_search_tool(tool):
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


# ---------------------------------------------------------------------------
# Streaming (non-web-search path)
# ---------------------------------------------------------------------------

async def _stream_anthropic_events(
    upstream_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    original_model: str,
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

    async for chunk in stream_request(upstream_url, headers, payload):
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
    The modified request replaces web_search_* built-in tools with a standard
    function tool so the upstream model knows it can call web_search.
    """
    if not request.tools:
        return request, False

    had_web_search = any(_is_web_search_tool(t) for t in request.tools)
    if not had_web_search:
        return request, False

    # Keep non-web-search tools and add the function tool definition
    remaining_tools = [t for t in request.tools if not _is_web_search_tool(t)]
    data = request.model_dump()
    data["tools"] = [
        (t if isinstance(t, dict) else t.model_dump()) for t in remaining_tools
    ]
    # Inject the web_search function tool so upstream knows it can call it
    data["tools"].append(_WEB_SEARCH_FUNCTION_TOOL)
    # Encourage the model to use tools (auto lets it decide when to search)
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

    # Append assistant turn with all content blocks (including the tool_use block)
    assistant_content = assistant_response.get("content", [])
    data["messages"].append({"role": "assistant", "content": assistant_content})

    # Append user turn with the tool_result
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
) -> dict[str, Any]:
    """Execute the agentic tool-call loop for web_search.

    IBM ICA ignores native OpenAI tool definitions and tool_choice, so this loop
    always uses XML mode: tool definitions are injected into the system prompt and
    the model is instructed to respond with <function_calls> XML.  The translator's
    existing XML parsers detect and extract the tool call from the response text.

    Returns the final Anthropic-format response dict.
    """
    current_request, _ = _strip_web_search_tools(request)

    for iteration in range(_MAX_TOOL_ITERATIONS):
        logger.debug("Agentic loop iteration %d/%d", iteration + 1, _MAX_TOOL_ITERATIONS)

        # Always force XML mode: inject the web_search tool into the system prompt
        # and remove native tools so the upstream never sees tool definitions
        # (which IBM ICA silently ignores).
        req_data = current_request.model_dump()

        # Build the XML-mode system prompt with the tool description
        tool_hint = tools_to_system_prompt(req_data.get("tools") or [_WEB_SEARCH_FUNCTION_TOOL])
        existing_system = req_data.get("system") or ""
        if isinstance(existing_system, list):
            existing_system = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in existing_system
            )
        req_data["system"] = (tool_hint + "\n\n" + existing_system).strip()

        # Strip tools and tool_choice — we send XML instructions instead
        req_data["tools"] = None
        req_data["tool_choice"] = None

        iter_request = MessagesRequest(**req_data)
        openai_req = anthropic_to_openai_request(iter_request, _get_settings().default_model)
        payload = openai_req.model_dump(exclude_none=True)
        # Always non-streaming inside the loop so we can inspect tool calls
        payload["stream"] = False

        oai_response = await forward_request(upstream_url, headers, payload)
        anthropic_response = openai_to_anthropic_response(oai_response, current_request)

        # Check if the model wants to call web_search
        content_blocks = anthropic_response.get("content", [])
        web_search_calls = [
            b for b in content_blocks
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "web_search"
        ]

        if not web_search_calls:
            logger.debug("Agentic loop complete after %d iteration(s)", iteration + 1)
            # Restore the original model name in the response
            anthropic_response["model"] = request.model
            return anthropic_response

        # Execute each web_search call and append results
        for call in web_search_calls:
            tool_use_id = call.get("id", f"toolu_{uuid.uuid4().hex[:8]}")
            query = call.get("input", {}).get("query", "")
            logger.info(
                "Executing web_search (iteration %d): query=%r provider=%s",
                iteration + 1,
                query,
                provider,
            )
            try:
                search_result = await perform_web_search(
                    query=query,
                    provider=provider,
                    tavily_api_key=tavily_api_key or None,
                )
            except Exception as exc:
                logger.error("web_search failed: %s", exc)
                search_result = f"Search failed: {exc}"

            current_request = _append_tool_result(
                current_request, anthropic_response, tool_use_id, search_result
            )

    # Reached the iteration cap — return whatever the last response was
    logger.warning("Agentic loop hit iteration cap (%d)", _MAX_TOOL_ITERATIONS)
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

    # message_start
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
    When a web_search built-in tool is present, the proxy executes the search
    itself and feeds the results back to the model (agentic loop).
    """
    if (error_response := _check_unsupported_tools(request)) is not None:
        return error_response

    settings = _get_settings()
    target_model = settings.default_model
    logger.debug(
        "Incoming model=%r target model=%r stream=%s has_web_search=%s",
        request.model,
        target_model,
        request.stream,
        request.tools and any(_is_web_search_tool(t) for t in request.tools),
    )

    upstream_url = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
    headers = _build_upstream_headers(settings.openai_api_key)

    has_web_search = bool(
        request.tools and any(_is_web_search_tool(t) for t in request.tools)
    )

    # --- Web-search agentic loop path ---
    if has_web_search:
        final_response = await _run_web_search_agentic_loop(
            request=request,
            upstream_url=upstream_url,
            headers=headers,
            provider=settings.web_search_provider,
            tavily_api_key=settings.tavily_api_key,
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
        return StreamingResponse(
            _stream_anthropic_events(upstream_url, headers, payload, request.model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    oai_response = await forward_request(upstream_url, headers, payload)
    anthropic_response = openai_to_anthropic_response(oai_response, request)
    return JSONResponse(content=anthropic_response)
