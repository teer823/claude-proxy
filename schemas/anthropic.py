"""Pydantic models for the Anthropic Messages API format."""

from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ContentBlockText(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ContentBlockImage(BaseModel):
    type: Literal["image"] = "image"
    source: dict[str, Any]


class ContentBlockToolUse(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ContentBlockToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: Union[str, list[dict[str, Any]], None] = None
    is_error: Optional[bool] = None


class ContentBlockDocument(BaseModel):
    type: Literal["document"] = "document"
    source: dict[str, Any]
    title: Optional[str] = None
    context: Optional[str] = None
    citations: Optional[dict[str, Any]] = None
    # Allow extra fields for forward compatibility
    model_config = {"extra": "allow"}


ContentBlock = Union[
    ContentBlockText,
    ContentBlockImage,
    ContentBlockToolUse,
    ContentBlockToolResult,
    ContentBlockDocument,
]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: Union[str, list[ContentBlock]]


class SystemPrompt(BaseModel):
    type: Literal["text"] = "text"
    text: str


class Tool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    # Built-in Anthropic tool type (e.g. "web_search_20250305", "bash_20250124")
    type: Optional[str] = None
    # Allow extra fields from built-in tools (e.g. max_uses, timeout, display_height)
    model_config = {"extra": "allow"}


class ToolChoice(BaseModel):
    type: Literal["auto", "any", "tool"] = "auto"
    name: Optional[str] = None


class MessagesRequest(BaseModel):
    model: str
    messages: list[Message]
    max_tokens: int
    system: Optional[Union[str, list[SystemPrompt]]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[list[str]] = None
    stream: Optional[bool] = False
    tools: Optional[list[Tool]] = None
    tool_choice: Optional[ToolChoice] = None
    metadata: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class InputJsonDelta(BaseModel):
    type: Literal["input_json_delta"] = "input_json_delta"
    partial_json: str


class MessagesResponse(BaseModel):
    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[dict[str, Any]] = Field(default_factory=list)
    model: str
    stop_reason: Optional[str] = None
    stop_sequence: Optional[str] = None
    usage: Usage


# ---------------------------------------------------------------------------
# Streaming event models
# ---------------------------------------------------------------------------

class MessageStartEvent(BaseModel):
    type: Literal["message_start"] = "message_start"
    message: dict[str, Any]


class ContentBlockStartEvent(BaseModel):
    type: Literal["content_block_start"] = "content_block_start"
    index: int
    content_block: dict[str, Any]


class ContentBlockDeltaEvent(BaseModel):
    type: Literal["content_block_delta"] = "content_block_delta"
    index: int
    delta: dict[str, Any]


class ContentBlockStopEvent(BaseModel):
    type: Literal["content_block_stop"] = "content_block_stop"
    index: int


class MessageDeltaEvent(BaseModel):
    type: Literal["message_delta"] = "message_delta"
    delta: dict[str, Any]
    usage: dict[str, Any]


class MessageStopEvent(BaseModel):
    type: Literal["message_stop"] = "message_stop"


class PingEvent(BaseModel):
    type: Literal["ping"] = "ping"