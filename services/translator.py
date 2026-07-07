"""Translate between Anthropic Messages API and OpenAI Chat Completions API formats."""

import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Optional

_logger = logging.getLogger(__name__)

from schemas.anthropic import MessagesRequest
from schemas.openai import (
    ChatCompletionRequest,
    ChatMessage,
    FunctionDefinition,
    ToolDefinition,
)


# ---------------------------------------------------------------------------
# XML tool-call parser
# ---------------------------------------------------------------------------

def _parse_xml_param_value(pvalue: str) -> Any:
    """Parse an XML parameter value, preserving strings that look like numbers.

    Plain ``json.loads`` would silently convert a bare ``12345`` to the integer
    ``12345``, which breaks tools whose schema declares the parameter as a
    ``string`` (e.g. ``taskId``).  We only attempt JSON decoding for values that
    are clearly structured JSON: objects ``{…}``, arrays ``[…]``, or the
    JSON literals ``true``, ``false``, and ``null``.  Everything else is kept as
    a plain Python string.
    """
    if not pvalue:
        return pvalue
    first = pvalue[0]
    if first in ("{", "[") or pvalue in ("true", "false", "null"):
        try:
            return json.loads(pvalue)
        except (json.JSONDecodeError, ValueError):
            pass
    return pvalue


def _parse_xml_tool_calls_regex(xml_src: str) -> list[dict[str, Any]]:
    """
    Regex-based fallback parser for <function_calls> XML.

    Used when ET.fromstring() fails because parameter values contain
    unescaped special characters (e.g. shell commands, file paths).
    """
    tool_blocks: list[dict[str, Any]] = []
    for invoke_match in re.finditer(
        r'<invoke\s+name=["\']([^"\']+)["\']>(.*?)</invoke>',
        xml_src,
        re.DOTALL,
    ):
        tool_name = invoke_match.group(1)
        invoke_body = invoke_match.group(2)
        params: dict[str, Any] = {}
        for param_match in re.finditer(
            r'<parameter\s+name=["\']([^"\']+)["\']>(.*?)</parameter>',
            invoke_body,
            re.DOTALL,
        ):
            pname = param_match.group(1)
            pvalue = param_match.group(2).strip()
            params[pname] = _parse_xml_param_value(pvalue)
        tool_blocks.append({
            "type": "tool_use",
            "id": f"toolu_{uuid.uuid4().hex[:8]}",
            "name": tool_name,
            "input": params,
        })
    return tool_blocks


def _parse_xml_tool_calls(
    text: str,
) -> tuple[Optional[str], list[dict[str, Any]]]:
    """Detect <function_calls> XML in text and convert to tool_use blocks.

    Uses the regex parser as the primary strategy because parameter values often
    contain unescaped XML special characters (shell commands with ``<``, ``>``,
    ``|``, ``&``).  ET.fromstring() is tried first only when the source is small
    enough that a strict parse is likely to succeed cleanly; otherwise we go
    straight to regex to avoid noisy ParseError warnings.
    """
    if "<function_calls>" not in text:
        return text, []

    pre, _, xml_part = text.partition("<function_calls>")
    xml_src = "<function_calls>" + xml_part
    closing = "</function_calls>"
    if closing in xml_src:
        xml_src = xml_src[: xml_src.index(closing) + len(closing)]

    tool_blocks: list[dict[str, Any]] = []

    # Try ET first only when the source looks clean (no bare < or > outside tags).
    # A cheap heuristic: if the parameter content contains unescaped angle brackets
    # that would break strict XML, skip ET and go straight to regex.
    _param_content_re = re.compile(
        r'<parameter\s+name=["\'][^"\']*["\']>(.*?)</parameter>',
        re.DOTALL,
    )
    has_unescaped = any(
        ("<" in m.group(1) or ">" in m.group(1) or "&" in m.group(1))
        for m in _param_content_re.finditer(xml_src)
    )

    if not has_unescaped:
        try:
            root = ET.fromstring(xml_src)
            for invoke in root.findall("invoke"):
                tool_name = invoke.get("name", "")
                params: dict[str, Any] = {}
                for param in invoke.findall("parameter"):
                    pname = param.get("name", "")
                    pvalue = (param.text or "").strip()
                    params[pname] = _parse_xml_param_value(pvalue)
                tool_blocks.append({
                    "type": "tool_use",
                    "id": f"toolu_{uuid.uuid4().hex[:8]}",
                    "name": tool_name,
                    "input": params,
                })
        except ET.ParseError:
            # Unexpected parse failure even without obvious special chars — fall through to regex
            tool_blocks = []

    if not tool_blocks:
        # Primary path for shell commands / any XML with unescaped special chars
        tool_blocks = _parse_xml_tool_calls_regex(xml_src)
        if not tool_blocks:
            _logger.warning(
                "XML tool-call parse found no invoke blocks. "
                "Treating as plain text. Source preview: %.200r",
                xml_src,
            )
            return text, []

    remaining = pre.rstrip() or None
    return remaining, tool_blocks


def _parse_tool_call_tags(
    text: str,
) -> tuple[Optional[str], list[dict[str, Any]]]:
    """Detect <tool_call>JSON</tool_call> blocks in text and convert to tool_use blocks.

    Some models (e.g. Qwen, Mistral via OpenAI-compatible endpoints) return tool
    calls wrapped in <tool_call> tags with a JSON body, e.g.:

        <tool_call>
        {"name": "bash", "arguments": {"command": "ls -la"}}
        </tool_call>

    The JSON may have either an ``arguments`` key (OpenAI-style) or an ``input``
    key (Anthropic-style).
    """
    if "<tool_call>" not in text:
        return text, []

    tool_blocks: list[dict[str, Any]] = []
    remaining_parts: list[str] = []
    last_end = 0

    for m in re.finditer(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL):
        # Collect text before this tag
        before = text[last_end: m.start()].strip()
        if before:
            remaining_parts.append(before)
        last_end = m.end()

        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as json_err:
            _logger.warning(
                "tool_call tag contained invalid JSON (%s); treating as plain text. Raw: %.200r",
                json_err,
                raw,
            )
            remaining_parts.append(m.group(0))
            continue

        if isinstance(obj, dict):
            # Support both "arguments" (OpenAI-style) and "input" (Anthropic-style) keys
            input_data = obj.get("arguments") or obj.get("input") or {}
            # arguments may itself be a JSON string
            if isinstance(input_data, str):
                try:
                    input_data = json.loads(input_data)
                except json.JSONDecodeError:
                    input_data = {"raw": input_data}
            tool_blocks.append({
                "type": "tool_use",
                "id": obj.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                "name": obj.get("name", ""),
                "input": input_data,
            })
        else:
            remaining_parts.append(m.group(0))

    # Collect any trailing text
    tail = text[last_end:].strip()
    if tail:
        remaining_parts.append(tail)

    if not tool_blocks:
        return text, []

    remaining = "\n".join(remaining_parts).strip() or None
    return remaining, tool_blocks


def _parse_tool_use_tags(
    text: str,
) -> tuple[Optional[str], list[dict[str, Any]]]:
    """Detect <tool_use>JSON</tool_use> blocks in text and convert to tool_use blocks.

    The IBM watsonx model sometimes returns tool calls wrapped in <tool_use> tags
    with a JSON body instead of the <function_calls> XML format, e.g.:

        <tool_use>
          {"type": "tool_use", "name": "Skill", "id": "toolu_01",
           "input": {"name": "deep-research", "args": "..."}}
        </tool_use>
    """
    if "<tool_use>" not in text:
        return text, []

    tool_blocks: list[dict[str, Any]] = []
    remaining_parts: list[str] = []
    last_end = 0

    for m in re.finditer(r"<tool_use>(.*?)</tool_use>", text, re.DOTALL):
        # Collect text before this tag
        before = text[last_end: m.start()].strip()
        if before:
            remaining_parts.append(before)
        last_end = m.end()

        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as json_err:
            _logger.warning(
                "tool_use tag contained invalid JSON (%s); treating as plain text. Raw: %.200r",
                json_err,
                raw,
            )
            # Not valid JSON — treat the whole thing as plain text
            remaining_parts.append(m.group(0))
            continue

        # Accept either a full tool_use block or a plain object
        if isinstance(obj, dict):
            tool_blocks.append({
                "type": "tool_use",
                "id": obj.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                "name": obj.get("name", ""),
                "input": obj.get("input", {}),
            })
        else:
            remaining_parts.append(m.group(0))

    # Collect any trailing text
    tail = text[last_end:].strip()
    if tail:
        remaining_parts.append(tail)

    if not tool_blocks:
        return text, []

    remaining = "\n".join(remaining_parts).strip() or None
    return remaining, tool_blocks


# ---------------------------------------------------------------------------
# XML-mode helpers (upstream does not support native tool_calls)
# ---------------------------------------------------------------------------

def _tool_use_block_to_xml(block: dict[str, Any]) -> str:
    """Render a tool_use content block back to the XML format the upstream expects."""
    name = block.get("name", "")
    input_data = block.get("input", {})
    params_xml = "\n".join(
        f'    <parameter name="{k}">{v}</parameter>'
        for k, v in input_data.items()
    )
    return (
        f"<function_calls>\n"
        f"  <invoke name=\"{name}\">\n"
        f"{params_xml}\n"
        f"  </invoke>\n"
        f"</function_calls>"
    )


def _tool_result_block_to_xml(block: dict[str, Any]) -> str:
    """Render a tool_result content block as XML result text."""
    tool_use_id = block.get("tool_use_id", "")
    content = block.get("content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        content = "\n".join(parts)
    is_error = block.get("is_error", False)
    status = "error" if is_error else "success"
    return (
        f"<function_results>\n"
        f"  <result tool_use_id=\"{tool_use_id}\" status=\"{status}\">\n"
        f"    {content}\n"
        f"  </result>\n"
        f"</function_results>"
    )


def tools_to_system_prompt(tools: list[Any]) -> str:
    """Render tool definitions as a system-prompt block for XML-mode upstreams.

    Public alias used by the web-search agentic loop to force XML mode on the
    first iteration when the upstream does not support native tool_calls.
    """
    return _tools_to_system_prompt(tools)


def _tools_to_system_prompt(tools: list[Any]) -> str:
    """Render tool definitions as a system-prompt block for XML-mode upstreams."""
    lines = [
        "You have access to the following tools. To call a tool, respond with XML in this format:",
        "",
        "<function_calls>",
        '  <invoke name="TOOL_NAME">',
        '    <parameter name="PARAM_NAME">value</parameter>',
        "  </invoke>",
        "</function_calls>",
        "",
        "Available tools:",
    ]
    for t in tools:
        if isinstance(t, dict):
            name = t.get("name", "")
            desc = t.get("description", "")
            schema = t.get("input_schema", {})
        else:
            name = getattr(t, "name", "")
            desc = getattr(t, "description", "") or ""
            schema = getattr(t, "input_schema", {})
        lines.append(f"\n- {name}: {desc}")
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        for pname, pinfo in props.items():
            req = " (required)" if pname in required else ""
            pdesc = pinfo.get("description", "") if isinstance(pinfo, dict) else ""
            ptype = pinfo.get("type", "") if isinstance(pinfo, dict) else ""
            lines.append(f"  - {pname} ({ptype}){req}: {pdesc}")
    return "\n".join(lines)


def _is_xml_mode(request: MessagesRequest) -> bool:
    """Return True if the conversation already contains tool_use/tool_result blocks."""
    for msg in request.messages:
        content = msg.content
        if isinstance(content, list):
            for block in content:
                btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                if btype in ("tool_use", "tool_result"):
                    return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_system_text(system: Any) -> Optional[str]:
    """Extract plain text from Anthropic system prompt (str or list of blocks)."""
    if system is None:
        return None
    if isinstance(system, str):
        return system
    texts: list[str] = []
    for block in system:
        if isinstance(block, dict):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
        else:
            if getattr(block, "type", None) == "text":
                texts.append(getattr(block, "text", ""))
    return "\n\n".join(texts) if texts else None


def _anthropic_content_to_openai(
    content: Any,
) -> tuple[Optional[str], Optional[list[dict[str, Any]]]]:
    """Convert Anthropic content blocks to OpenAI content string + tool_calls.

    Images are converted to proper OpenAI image_url objects.
    A single plain-text block is simplified back to a bare string.
    """
    if isinstance(content, str):
        return content, None

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    openai_content_parts: list[dict[str, Any]] = []
    has_image = False

    for block in content:
        if isinstance(block, dict):
            btype = block.get("type")
        else:
            btype = getattr(block, "type", None)
            block = block.model_dump() if hasattr(block, "model_dump") else vars(block)

        if btype == "text":
            text_val = block.get("text", "")
            text_parts.append(text_val)
            openai_content_parts.append({"type": "text", "text": text_val})
        elif btype == "document":
            source = block.get("source", {})
            src_type = source.get("type", "")
            if src_type == "text":
                doc_text = source.get("text", "")
                text_parts.append(doc_text)
                openai_content_parts.append({"type": "text", "text": doc_text})
            elif src_type == "url":
                doc_text = f"[Attached document: {source.get('url', 'unknown')}]"
                text_parts.append(doc_text)
                openai_content_parts.append({"type": "text", "text": doc_text})
            else:
                # base64 or unsupported — emit a placeholder (IBM ICA doesn't support native docs)
                media_type = source.get("media_type", "application/octet-stream")
                title = block.get("title") or media_type
                doc_text = f"[Attached document: {title}]"
                text_parts.append(doc_text)
                openai_content_parts.append({"type": "text", "text": doc_text})
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })
        elif btype == "image":
            source = block.get("source", {})
            src_type = source.get("type", "base64")
            has_image = True
            if src_type == "base64":
                media_type = source.get("media_type", "image/jpeg")
                data = source.get("data", "")
                openai_content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"{media_type};base64,{data}"},
                })
            elif src_type == "url":
                openai_content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": source.get("url", "")},
                })

    # If there are no images and only one text part, simplify to a plain string
    if not has_image and not tool_calls and len(text_parts) == 1:
        return text_parts[0], None
    if not has_image and not tool_calls:
        combined = "\n".join(text_parts)
        return combined if combined else None, None

    # Mixed content (text + images) → multipart list
    combined_text: Optional[str] = None
    if not has_image:
        combined_text = "\n".join(text_parts) if text_parts else None
        return combined_text, tool_calls if tool_calls else None

    return openai_content_parts if openai_content_parts else None, tool_calls if tool_calls else None


def _build_tool_result_content(block: Any) -> str:
    """Extract and normalise string content from a tool_result block.

    Handles None, str, list of content blocks, and arbitrary dicts.
    """
    if isinstance(block, dict):
        content = block.get("content", "")
    else:
        content = getattr(block, "content", "")

    if content is None:
        return "No content provided"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif "text" in item:
                    parts.append(item["text"])
                else:
                    try:
                        parts.append(json.dumps(item, ensure_ascii=False))
                    except Exception:
                        parts.append(str(item))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(content, dict):
        if content.get("type") == "text":
            return content.get("text", "")
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    return str(content) if content else ""


# ---------------------------------------------------------------------------
# Anthropic -> OpenAI request translation
# ---------------------------------------------------------------------------

def anthropic_to_openai_request(
    request: MessagesRequest,
    target_model: str,
    force_xml_tools: bool = False,
) -> ChatCompletionRequest:
    """Convert an Anthropic MessagesRequest to an OpenAI ChatCompletionRequest.

    When the conversation contains tool_use/tool_result blocks (XML mode), the
    upstream model does not understand native OpenAI tool_calls. In that case we:
      1. Inject tool definitions into the system prompt as plain text.
      2. Re-render tool_use blocks as XML text in assistant turns.
      3. Re-render tool_result blocks as XML result text in user turns.
      4. Do NOT send the 'tools' or 'tool_choice' fields to the upstream.

    ``force_xml_tools`` enables XML mode from the very first request. Required
    for upstreams that silently strip the native ``tools`` parameter (IBM ICA
    does — the model never sees the definitions, so the first tool call can
    never happen and the conversation-content heuristic never triggers).
    """
    xml_mode = force_xml_tools or _is_xml_mode(request)

    # Build system prompt
    system_text = _get_system_text(request.system) or ""
    if xml_mode and request.tools:
        tool_hint = _tools_to_system_prompt(request.tools)
        system_text = (tool_hint + "\n\n" + system_text).strip()
    # Inject agentic tool-use reminder when tools are present (non-XML mode).
    # IBM ICA sometimes responds with plain text instead of calling a tool for
    # simple action requests (e.g. "open finder"). This reminder nudges the model
    # to prefer tool calls over text descriptions.
    if not xml_mode and request.tools:
        tool_names = [t.name for t in request.tools]
        reminder = (
            "IMPORTANT: You have access to tools listed above. When the user asks you "
            "to perform any action (run a command, read/write a file, open an application, "
            "search the web, etc.), you MUST call the appropriate tool rather than "
            "describing the action in plain text or a code block. "
            f"Available tools include: {', '.join(tool_names)}."
        )
        system_text = (system_text + "\n\n" + reminder).strip()
    if system_text:
        openai_messages: list[ChatMessage] = [ChatMessage(role="system", content=system_text)]
    else:
        openai_messages = []

    for msg in request.messages:
        role = msg.role
        content = msg.content

        # Guard: None content
        if content is None:
            openai_messages.append(ChatMessage(role=role, content=None))
            continue

        if isinstance(content, str):
            openai_messages.append(ChatMessage(role=role, content=content))
            continue

        # Normalize to list of dicts
        blocks: list[dict[str, Any]] = []
        for b in content:
            if isinstance(b, dict):
                blocks.append(b)
            else:
                blocks.append(b.model_dump() if hasattr(b, "model_dump") else vars(b))

        if xml_mode:
            # Flatten everything to plain text for the upstream
            parts: list[str] = []
            for block in blocks:
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "document":
                    source = block.get("source", {})
                    src_type = source.get("type", "")
                    if src_type == "text":
                        parts.append(source.get("text", ""))
                    elif src_type == "url":
                        parts.append(f"[Attached document: {source.get('url', 'unknown')}]")
                    else:
                        media_type = source.get("media_type", "application/octet-stream")
                        title = block.get("title") or media_type
                        parts.append(f"[Attached document: {title}]")
                elif btype == "tool_use":
                    parts.append(_tool_use_block_to_xml(block))
                elif btype == "tool_result":
                    parts.append(_tool_result_block_to_xml(block))
            openai_messages.append(ChatMessage(role=role, content="\n".join(parts)))
        else:
            has_tool_result = any(b.get("type") == "tool_result" for b in blocks)
            if has_tool_result and role == "user":
                other_parts: list[dict[str, Any]] = []
                for block in blocks:
                    if block.get("type") == "tool_result":
                        result_content = _build_tool_result_content(block)
                        openai_messages.append(ChatMessage(
                            role="tool",
                            content=result_content,
                            tool_call_id=block.get("tool_use_id", ""),
                        ))
                    else:
                        other_parts.append(block)
                if other_parts:
                    text, _ = _anthropic_content_to_openai(other_parts)
                    if text:
                        openai_messages.append(ChatMessage(role="user", content=text))
            else:
                text_or_parts, tool_calls = _anthropic_content_to_openai(blocks)
                msg_kwargs: dict[str, Any] = {"role": role, "content": text_or_parts}
                if tool_calls:
                    msg_kwargs["tool_calls"] = tool_calls
                openai_messages.append(ChatMessage(**msg_kwargs))

    # Only send tools/tool_choice when NOT in xml_mode
    openai_tools: Optional[list[ToolDefinition]] = None
    tool_choice: Optional[Any] = None
    if not xml_mode and request.tools:
        custom_tools = list(request.tools)
        openai_tools = [
            ToolDefinition(
                function=FunctionDefinition(
                    name=t.name,
                    description=t.description,
                    parameters=t.input_schema,
                )
            )
            for t in custom_tools
        ] or None
        if request.tool_choice:
            tc = request.tool_choice
            tc_type = tc.get("type") if isinstance(tc, dict) else getattr(tc, "type", "auto")
            tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if tc_type == "none":
                tool_choice = "none"
            elif tc_type == "auto":
                tool_choice = "auto"
            elif tc_type == "any":
                # Use "auto" for compatibility with backends that don't support "required"
                tool_choice = "auto"
            elif tc_type == "tool" and tc_name:
                # IBM ICA only accepts tool_choice as a plain string; fall back to "auto"
                tool_choice = "auto"
            else:
                tool_choice = "auto"

    # Extended thinking: IBM ICA uses an OpenAI-compatible endpoint that does NOT
    # support the Anthropic-specific "thinking" parameter — forwarding it causes
    # HTTP 400 ("max_tokens must be greater than thinking.budget_tokens") or a
    # silent HTTP 404 with an empty body after a 3-minute timeout.
    #
    # Strategy:
    #   1. Read thinking/budget_tokens from the incoming request.
    #   2. Ensure max_tokens > budget_tokens (IBM ICA enforces this even when we
    #      strip thinking, because the upstream model still uses extended thinking
    #      internally when the budget is set via system-prompt conventions).
    #   3. Do NOT include thinking in the ChatCompletionRequest sent upstream.
    thinking = request.thinking if hasattr(request, "thinking") else None
    max_tokens = request.max_tokens

    # IBM ICA rejects requests with small max_tokens values when the thinking field
    # is present (even when type=disabled).  The model appears to require max_tokens
    # to exceed an internal budget threshold.  Normal Claude Code requests use 32000;
    # we enforce a floor of 16384 to cover summary/reflection calls that Claude Code
    # sends with max_tokens=64.
    _MIN_MAX_TOKENS = 16384
    if max_tokens < _MIN_MAX_TOKENS:
        _logger.info(
            "max_tokens=%d is below minimum %d; bumping to %d",
            max_tokens,
            _MIN_MAX_TOKENS,
            _MIN_MAX_TOKENS,
        )
        max_tokens = _MIN_MAX_TOKENS

    if thinking and isinstance(thinking, dict):
        budget = thinking.get("budget_tokens", 0)
        thinking_type = thinking.get("type", "")
        _logger.debug(
            "thinking field present: type=%r budget_tokens=%d max_tokens=%d (after floor)",
            thinking_type,
            budget,
            max_tokens,
        )
        # For enabled thinking: also ensure max_tokens > budget_tokens
        if thinking_type == "enabled" and max_tokens <= budget:
            new_max = budget + 4096
            _logger.warning(
                "thinking enabled: max_tokens (%d) <= budget_tokens (%d); "
                "bumping max_tokens to %d before forwarding to upstream",
                max_tokens,
                budget,
                new_max,
            )
            max_tokens = new_max

    return ChatCompletionRequest(
        model=target_model,
        messages=openai_messages,
        max_tokens=max_tokens,
        temperature=request.temperature,
        top_p=request.top_p,
        stop=request.stop_sequences,
        stream=request.stream,
        # Ask streaming upstreams to emit a final usage chunk so real token
        # counts can be reported to the client; ignored by upstreams that
        # don't support it (the estimation fallback covers those).
        stream_options={"include_usage": True} if request.stream else None,
        tools=openai_tools,
        tool_choice=tool_choice,
        # thinking is intentionally NOT forwarded — IBM ICA does not support it
        # and returns HTTP 400 / silent 404 when it is present.
    )


# ---------------------------------------------------------------------------
# OpenAI -> Anthropic response translation
# ---------------------------------------------------------------------------

def _openai_finish_reason_to_anthropic(finish_reason: Optional[str]) -> str:
    """Map OpenAI finish_reason to Anthropic stop_reason."""
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "stop_sequence",
    }
    return mapping.get(finish_reason or "", "end_turn")


def openai_to_anthropic_response(
    oai_response: dict[str, Any],
    original_request: MessagesRequest,
) -> dict[str, Any]:
    """Convert an OpenAI ChatCompletion response to Anthropic Messages response."""
    response_id = oai_response.get("id", f"msg_{uuid.uuid4().hex}")
    model = original_request.model
    usage = oai_response.get("usage", {})

    choices = oai_response.get("choices", [])
    choice = choices[0] if choices else {}
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason")

    content_blocks: list[dict[str, Any]] = []

    # Text content — check for embedded XML tool calls first
    text_content = message.get("content")
    if text_content:
        # Try <function_calls> XML format first
        remaining_text, xml_tool_blocks = _parse_xml_tool_calls(text_content)
        if xml_tool_blocks:
            if remaining_text:
                content_blocks.append({"type": "text", "text": remaining_text})
            content_blocks.extend(xml_tool_blocks)
        else:
            # Try <tool_use>JSON</tool_use> format (IBM watsonx)
            remaining_text2, tag_tool_blocks = _parse_tool_use_tags(text_content)
            if tag_tool_blocks:
                if remaining_text2:
                    content_blocks.append({"type": "text", "text": remaining_text2})
                content_blocks.extend(tag_tool_blocks)
            else:
                # Try <tool_call>JSON</tool_call> format (Qwen / Mistral style)
                remaining_text3, tc_tool_blocks = _parse_tool_call_tags(text_content)
                if tc_tool_blocks:
                    if remaining_text3:
                        content_blocks.append({"type": "text", "text": remaining_text3})
                    content_blocks.extend(tc_tool_blocks)
                else:
                    content_blocks.append({"type": "text", "text": text_content})

    # Structured tool calls from the OpenAI response
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        func = tc.get("function", {})
        raw_args = func.get("arguments", "{}")
        try:
            input_data = json.loads(raw_args)
        except json.JSONDecodeError as json_err:
            _logger.warning(
                "Failed to parse tool_call arguments as JSON (%s); using raw string. "
                "Tool=%r  args preview: %.200r",
                json_err,
                func.get("name", "?"),
                raw_args,
            )
            input_data = {"raw": raw_args}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
            "name": func.get("name", ""),
            "input": input_data,
        })

    # Ensure at least one content block
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    has_tool_use = any(b.get("type") == "tool_use" for b in content_blocks)
    if has_tool_use and finish_reason not in ("tool_calls", "function_call"):
        stop_reason = "tool_use"
    else:
        stop_reason = _openai_finish_reason_to_anthropic(finish_reason)

    return {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# OpenAI streaming chunk -> Anthropic SSE events
# ---------------------------------------------------------------------------

def _count_text_delta_chars(events: list[dict[str, Any]]) -> int:
    """Sum the character length of all text_delta events in a batch."""
    total = 0
    for evt in events:
        if evt.get("type") == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") == "text_delta":
                total += len(delta.get("text", ""))
    return total


def openai_stream_to_anthropic_events(
    chunk: dict[str, Any],
    message_id: str,
    model: str,
    block_index: int,
    current_tool_calls: dict[int, dict[str, Any]],
    sent_message_start: bool,
    sent_content_block_start: dict[int, bool],
    accumulated_text: str = "",
    usage_data: Optional[dict[str, Any]] = None,
    input_tokens_estimate: int = 0,
) -> tuple[list[dict[str, Any]], int, bool, dict[int, bool], str, dict[str, Any]]:
    """Convert a single OpenAI streaming chunk to Anthropic SSE event dicts.

    accumulated_text buffers streamed text so embedded XML tool calls can be
    detected and converted once the closing tag arrives.

    usage_data accumulates token counts from the stream's usage chunk so the
    final message_delta event carries real token numbers.
    """
    if usage_data is None:
        usage_data = {"input_tokens": 0, "output_tokens": 0}

    events: list[dict[str, Any]] = []
    choices = chunk.get("choices", [])

    # Capture usage when present in the chunk (some backends send it on last chunk)
    chunk_usage = chunk.get("usage")
    if chunk_usage:
        cache_read = 0
        prompt_details = chunk_usage.get("prompt_tokens_details", {})
        if prompt_details:
            cache_read = prompt_details.get("cached_tokens", 0)
        usage_data = {
            "input_tokens": chunk_usage.get("prompt_tokens", 0),
            "output_tokens": chunk_usage.get("completion_tokens", 0),
            "cache_read_input_tokens": cache_read,
        }

    if not sent_message_start:
        events.append({
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
        sent_message_start = True

    if not choices:
        return events, block_index, sent_message_start, sent_content_block_start, accumulated_text, usage_data

    choice = choices[0]
    delta = choice.get("delta", {})
    finish_reason = choice.get("finish_reason")

    text_delta = delta.get("content")
    if text_delta:
        accumulated_text += text_delta
        # Once a full XML marker is detected we stop streaming and buffer
        # everything until finish_reason so the whole block can be parsed.
        if "<function_calls>" not in accumulated_text and "<tool_use>" not in accumulated_text and "<tool_call>" not in accumulated_text:
            # Guard against XML markers split across chunk boundaries by
            # retaining a tail that could still be a partial prefix of either
            # marker.  Only the "safe" prefix (everything before that tail) is
            # flushed immediately.
            xml_markers = ["<function_calls>", "<tool_use>", "<tool_call>"]
            max_marker_len = max(len(m) for m in xml_markers)

            # Find the longest suffix of accumulated_text that is a prefix of
            # any XML marker we care about.
            safe_text = accumulated_text
            hold_back = ""
            tail_len = min(len(accumulated_text), max_marker_len - 1)
            for tail_size in range(tail_len, 0, -1):
                tail = accumulated_text[-tail_size:]
                if any(m.startswith(tail) for m in xml_markers):
                    safe_text = accumulated_text[:-tail_size]
                    hold_back = tail
                    break

            if safe_text:
                if not sent_content_block_start.get(block_index):
                    events.append({
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {"type": "text", "text": ""},
                    })
                    sent_content_block_start[block_index] = True
                events.append({
                    "type": "content_block_delta",
                    "index": block_index,
                    "delta": {"type": "text_delta", "text": safe_text},
                })
            accumulated_text = hold_back

    tool_calls_delta = delta.get("tool_calls") or []
    for tc_delta in tool_calls_delta:
        tc_idx = tc_delta.get("index", 0)
        tool_block_index = 1 + tc_idx
        if tc_idx not in current_tool_calls:
            current_tool_calls[tc_idx] = {
                "id": tc_delta.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                "name": "",
                "arguments": "",
            }
        tc_state = current_tool_calls[tc_idx]
        func = tc_delta.get("function", {})
        if func.get("name"):
            tc_state["name"] += func["name"]
        if tc_delta.get("id"):
            tc_state["id"] = tc_delta["id"]
        if not sent_content_block_start.get(tool_block_index):
            events.append({
                "type": "content_block_start",
                "index": tool_block_index,
                "content_block": {
                    "type": "tool_use",
                    "id": tc_state["id"],
                    "name": tc_state["name"],
                    "input": {},
                },
            })
            sent_content_block_start[tool_block_index] = True
        if func.get("arguments"):
            tc_state["arguments"] += func["arguments"]
            events.append({
                "type": "content_block_delta",
                "index": tool_block_index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": func["arguments"],
                },
            })

    if finish_reason:
        # Flush any held-back text that turned out not to be an XML marker
        if accumulated_text and "<function_calls>" not in accumulated_text and "<tool_use>" not in accumulated_text and "<tool_call>" not in accumulated_text:
            if not sent_content_block_start.get(block_index):
                events.append({
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {"type": "text", "text": ""},
                })
                sent_content_block_start[block_index] = True
            events.append({
                "type": "content_block_delta",
                "index": block_index,
                "delta": {"type": "text_delta", "text": accumulated_text},
            })
            accumulated_text = ""

        # Try <function_calls> XML format
        if accumulated_text and "<function_calls>" in accumulated_text:
            remaining_text, xml_tool_blocks = _parse_xml_tool_calls(accumulated_text)
            accumulated_text = ""
            if xml_tool_blocks:
                if remaining_text:
                    if not sent_content_block_start.get(block_index):
                        events.append({
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                        sent_content_block_start[block_index] = True
                    events.append({
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "text_delta", "text": remaining_text},
                    })
                for i, xml_tc in enumerate(xml_tool_blocks):
                    tb_idx = (block_index + 1 + i) if sent_content_block_start.get(block_index) else (block_index + i)
                    events.append({
                        "type": "content_block_start",
                        "index": tb_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": xml_tc["id"],
                            "name": xml_tc["name"],
                            "input": {},
                        },
                    })
                    sent_content_block_start[tb_idx] = True
                    events.append({
                        "type": "content_block_delta",
                        "index": tb_idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(xml_tc["input"], ensure_ascii=False),
                        },
                    })
                finish_reason = "tool_calls"

        # Try <tool_use>JSON</tool_use> format (IBM watsonx)
        elif accumulated_text and "<tool_use>" in accumulated_text:
            remaining_text, tag_tool_blocks = _parse_tool_use_tags(accumulated_text)
            accumulated_text = ""
            if tag_tool_blocks:
                if remaining_text:
                    if not sent_content_block_start.get(block_index):
                        events.append({
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                        sent_content_block_start[block_index] = True
                    events.append({
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "text_delta", "text": remaining_text},
                    })
                for i, tag_tc in enumerate(tag_tool_blocks):
                    tb_idx = (block_index + 1 + i) if sent_content_block_start.get(block_index) else (block_index + i)
                    events.append({
                        "type": "content_block_start",
                        "index": tb_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tag_tc["id"],
                            "name": tag_tc["name"],
                            "input": {},
                        },
                    })
                    sent_content_block_start[tb_idx] = True
                    events.append({
                        "type": "content_block_delta",
                        "index": tb_idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(tag_tc["input"], ensure_ascii=False),
                        },
                    })
                finish_reason = "tool_calls"

        # Try <tool_call>JSON</tool_call> format (Qwen / Mistral style)
        elif accumulated_text and "<tool_call>" in accumulated_text:
            remaining_text, tc_tool_blocks = _parse_tool_call_tags(accumulated_text)
            accumulated_text = ""
            if tc_tool_blocks:
                if remaining_text:
                    if not sent_content_block_start.get(block_index):
                        events.append({
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                        sent_content_block_start[block_index] = True
                    events.append({
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "text_delta", "text": remaining_text},
                    })
                for i, tc in enumerate(tc_tool_blocks):
                    tb_idx = (block_index + 1 + i) if sent_content_block_start.get(block_index) else (block_index + i)
                    events.append({
                        "type": "content_block_start",
                        "index": tb_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": {},
                        },
                    })
                    sent_content_block_start[tb_idx] = True
                    events.append({
                        "type": "content_block_delta",
                        "index": tb_idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(tc["input"], ensure_ascii=False),
                        },
                    })
                finish_reason = "tool_calls"

        for idx in sorted(sent_content_block_start.keys()):
            events.append({"type": "content_block_stop", "index": idx})

        stop_reason = _openai_finish_reason_to_anthropic(finish_reason)
        # Estimation fallback: when the upstream never sent a usage chunk
        # (all-zero usage_data), report chars/4 estimates instead of zeros so
        # clients' token displays and context accounting stay meaningful.
        # Streamed output size comes from the private running counter (plus
        # any text flushed in this final call) — accumulated_text can't be
        # used because the XML-detection paths reset it mid-stream.
        streamed_chars = usage_data.pop("_streamed_chars", 0) + _count_text_delta_chars(events)
        if not usage_data.get("input_tokens") and not usage_data.get("output_tokens"):
            usage_data["input_tokens"] = input_tokens_estimate
            usage_data["output_tokens"] = max(1, streamed_chars // 4)
        events.append({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": usage_data,
        })
        events.append({"type": "message_stop"})

    # Maintain the running streamed-output counter for the estimation fallback.
    # Skipped once the final message_delta has been emitted (its usage dict is
    # this same object by reference — mutating it afterwards would leak the
    # private key into the serialized event).
    if not any(e.get("type") == "message_stop" for e in events):
        chars = _count_text_delta_chars(events)
        if chars:
            usage_data["_streamed_chars"] = usage_data.get("_streamed_chars", 0) + chars

    return events, block_index, sent_message_start, sent_content_block_start, accumulated_text, usage_data
