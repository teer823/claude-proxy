"""Tests for model-specific OpenAI output-token parameter names."""

import unittest

from schemas.anthropic import MessagesRequest
from services.translator import (
    _uses_max_completion_tokens,
    anthropic_to_openai_request,
)


def _request(max_tokens: int = 32000) -> MessagesRequest:
    return MessagesRequest(
        model="client-model",
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=max_tokens,
    )


class TokenParameterCompatibilityTests(unittest.TestCase):
    def test_gpt5_uses_max_completion_tokens_only(self) -> None:
        translated = anthropic_to_openai_request(
            _request(),
            "global/gpt-5",
        )
        payload = translated.model_dump(exclude_none=True)

        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["max_completion_tokens"], 32000)

    def test_anthropic_model_keeps_max_tokens_only(self) -> None:
        translated = anthropic_to_openai_request(
            _request(),
            "global/anthropic.claude-sonnet-4-6",
        )
        payload = translated.model_dump(exclude_none=True)

        self.assertEqual(payload["max_tokens"], 32000)
        self.assertNotIn("max_completion_tokens", payload)

    def test_reasoning_model_detection_handles_namespaced_models(self) -> None:
        self.assertTrue(_uses_max_completion_tokens("global/gpt-5"))
        self.assertTrue(_uses_max_completion_tokens("global/gpt-5-mini"))
        self.assertTrue(_uses_max_completion_tokens("openai/o3-mini"))
        self.assertFalse(
            _uses_max_completion_tokens("global/anthropic.claude-sonnet-4-6")
        )

    def test_minimum_token_floor_applies_to_new_parameter(self) -> None:
        translated = anthropic_to_openai_request(
            _request(max_tokens=64),
            "global/gpt-5",
        )
        payload = translated.model_dump(exclude_none=True)

        self.assertEqual(payload["max_completion_tokens"], 16384)
        self.assertNotIn("max_tokens", payload)


if __name__ == "__main__":
    unittest.main()