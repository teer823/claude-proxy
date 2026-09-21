"""Tests for errors embedded in HTTP-200 upstream SSE streams."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.proxy import (
    UpstreamSSEError,
    _extract_sse_error,
    stream_to_completion,
)


class ExtractSSEErrorTests(unittest.TestCase):
    def test_extracts_openai_style_error_message(self) -> None:
        chunk = {
            "error": {
                "message": "The security token included in the request is invalid.",
                "type": "api_error",
            }
        }

        self.assertEqual(
            _extract_sse_error(chunk),
            "The security token included in the request is invalid.",
        )

    def test_returns_none_for_normal_completion_chunk(self) -> None:
        chunk = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "delta": {"content": "hello"},
                    "finish_reason": None,
                }
            ],
        }

        self.assertIsNone(_extract_sse_error(chunk))


class StreamToCompletionErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_does_not_assemble_embedded_error_as_empty_completion(self) -> None:
        payload = {
            "error": {
                "message": "DS5865: The security token included in the request is invalid.",
                "type": "api_error",
            }
        }

        async def failing_stream(*args, **kwargs):
            raise UpstreamSSEError(payload["error"]["message"], payload)
            yield  # pragma: no cover - makes this an async generator

        with patch("services.proxy.stream_request", failing_stream):
            with self.assertRaisesRegex(
                UpstreamSSEError,
                "security token included in the request is invalid",
            ):
                await stream_to_completion(
                    "https://ica.example/chat/completions",
                    {"Authorization": "Bearer test"},
                    {"model": "test-model", "messages": []},
                    request_id="test1234",
                )


if __name__ == "__main__":
    unittest.main()