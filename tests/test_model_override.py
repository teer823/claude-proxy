"""Tests for emergency per-backend model overrides."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from routers.messages import _apply_forced_model, _resolve_backend


def _settings(routing_file: str, **overrides: object) -> SimpleNamespace:
    values = {
        "model_routing_file": routing_file,
        "openai_base_url": "https://ica1.example/v1",
        "openai_api_key": "ica1-key",
        "default_model": "ica1-default",
        "ica2_base_url": "https://ica2.example/v1",
        "ica2_api_key": "ica2-key",
        "force_model_override": False,
        "ica1_force_model": "",
        "ica2_force_model": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ModelOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.routing_file = Path(self.temp_dir.name) / "routing.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_route(self, backend: str, model: str) -> None:
        self.routing_file.write_text(
            json.dumps(
                {"client-model": {"backend": backend, "model": model}}
            ),
            encoding="utf-8",
        )

    def test_override_disabled_preserves_resolved_model(self) -> None:
        settings = _settings(
            str(self.routing_file),
            ica1_force_model="ica1-emergency",
        )
        self.assertEqual(
            _apply_forced_model(settings, "ica1", "normally-resolved"),
            "normally-resolved",
        )

    def test_empty_force_model_safely_preserves_resolved_model(self) -> None:
        settings = _settings(
            str(self.routing_file),
            force_model_override=True,
        )
        self.assertEqual(
            _apply_forced_model(settings, "ica1", "normally-resolved"),
            "normally-resolved",
        )

    def test_ica1_route_uses_ica1_force_model(self) -> None:
        self._write_route("ica1", "ica1-routed")
        settings = _settings(
            str(self.routing_file),
            force_model_override=True,
            ica1_force_model="ica1-emergency",
            ica2_force_model="ica2-emergency",
        )

        url, headers, model = _resolve_backend("client-model", settings)

        self.assertEqual(url, "https://ica1.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer ica1-key")
        self.assertEqual(model, "ica1-emergency")

    def test_ica2_route_uses_ica2_force_model(self) -> None:
        self._write_route("ica2", "ica2-routed")
        settings = _settings(
            str(self.routing_file),
            force_model_override=True,
            ica1_force_model="ica1-emergency",
            ica2_force_model="ica2-emergency",
        )

        url, headers, model = _resolve_backend("client-model", settings)

        self.assertEqual(url, "https://ica2.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer ica2-key")
        self.assertEqual(model, "ica2-emergency")

    def test_unavailable_ica2_fallback_uses_ica1_force_model(self) -> None:
        self._write_route("ica2", "ica2-routed")
        settings = _settings(
            str(self.routing_file),
            ica2_base_url="",
            force_model_override=True,
            ica1_force_model="ica1-emergency",
            ica2_force_model="ica2-emergency",
        )

        url, headers, model = _resolve_backend("client-model", settings)

        self.assertEqual(url, "https://ica1.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer ica1-key")
        self.assertEqual(model, "ica1-emergency")


if __name__ == "__main__":
    unittest.main()