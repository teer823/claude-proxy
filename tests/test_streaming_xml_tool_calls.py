"""Regression tests for XML tool-call handling in the streaming translator.

Every scenario here is reproduced from real traffic captured in
``logs/proxy_debug.log`` on 2026-09-21 (ICA-2 ``claude-opus-5``) and
``logs/proxy_debug.log.2026-09-10`` (ICA-1 ``claude-sonnet-4-6``).
"""

import json
import unittest

from schemas.anthropic import MessagesRequest
from services.translator import (
    _XML_STOP_SEQUENCE,
    anthropic_to_openai_request,
    openai_stream_to_anthropic_events,
)


def _run_stream(content_chunks, finish_reason="stop"):
    """Feed ``content_chunks`` through the streaming translator.

    Returns ``(events, text, tool_uses)`` where ``text`` is everything the client
    would render and ``tool_uses`` is the list of recovered tool_use blocks with
    their accumulated input JSON.
    """
    state = {
        "block_index": 0,
        "current_tool_calls": {},
        "sent_message_start": False,
        "sent_content_block_start": {},
        "accumulated_text": "",
        "usage_data": {"input_tokens": 0, "output_tokens": 0},
    }
    all_events = []

    chunks = [
        {"choices": [{"index": 0, "delta": {"content": c}}]} for c in content_chunks
    ]
    chunks.append({"choices": [{"index": 0, "finish_reason": finish_reason, "delta": {}}]})

    for chunk in chunks:
        (
            events,
            state["block_index"],
            state["sent_message_start"],
            state["sent_content_block_start"],
            state["accumulated_text"],
            state["usage_data"],
        ) = openai_stream_to_anthropic_events(
            chunk=chunk,
            message_id="msg_test",
            model="claude-opus-5",
            block_index=state["block_index"],
            current_tool_calls=state["current_tool_calls"],
            sent_message_start=state["sent_message_start"],
            sent_content_block_start=state["sent_content_block_start"],
            accumulated_text=state["accumulated_text"],
            usage_data=state["usage_data"],
        )
        all_events.extend(events)

    text = "".join(
        e["delta"]["text"]
        for e in all_events
        if e.get("type") == "content_block_delta"
        and e.get("delta", {}).get("type") == "text_delta"
    )

    tool_uses = []
    inputs_by_index = {}
    for e in all_events:
        if e.get("type") == "content_block_start":
            blk = e.get("content_block", {})
            if blk.get("type") == "tool_use":
                tool_uses.append({"index": e["index"], "name": blk.get("name"), "input": ""})
        elif (
            e.get("type") == "content_block_delta"
            and e.get("delta", {}).get("type") == "input_json_delta"
        ):
            inputs_by_index.setdefault(e["index"], "")
            inputs_by_index[e["index"]] += e["delta"]["partial_json"]
    for tu in tool_uses:
        tu["input"] = inputs_by_index.get(tu["index"], "")

    return all_events, text, tool_uses


def _stop_reason(events):
    for e in events:
        if e.get("type") == "message_delta":
            return e["delta"]["stop_reason"]
    return None


class TestShreddedMarkerRecovery(unittest.TestCase):
    """The marker is split across many chunks (ICA-2 behaviour)."""

    # Verbatim from logs/proxy_debug.log around line 52700.
    SHREDDED_CHUNKS = [
        "",
        "_",
        "cal",
        "ls>",
        "\n<inv",
        'oke name="Read">',
        '\n<parameter name="file_',
        'path">/Users/P',
        "akawat.T/.cla",
        "ude/projects/-Users",
        "-Pakawat-T",
        "-Working/memory/project",
        "_kgov_datafl",
        "ow.md</parameter>",
        "\n</invoke>",
        "\n>",
        "",
    ]

    def test_no_raw_xml_reaches_client(self):
        _, text, _ = _run_stream(self.SHREDDED_CHUNKS)
        self.assertNotIn("<invoke", text)
        self.assertNotIn("<parameter", text)
        self.assertNotIn("</invoke>", text)
        # The orphaned wrapper fragment from the original bug must not appear.
        self.assertNotIn("_calls>", text)

    def test_tool_call_is_recovered(self):
        events, _, tool_uses = _run_stream(self.SHREDDED_CHUNKS)
        self.assertEqual(len(tool_uses), 1)
        self.assertEqual(tool_uses[0]["name"], "Read")
        self.assertEqual(
            json.loads(tool_uses[0]["input"])["file_path"],
            "/Users/Pakawat.T/.claude/projects/-Users-Pakawat-T-Working/memory/project_kgov_dataflow.md",
        )
        self.assertEqual(_stop_reason(events), "tool_use")


class TestBareInvokeRecovery(unittest.TestCase):
    """The <function_calls> wrapper is missing entirely (ICA-2 behaviour)."""

    def test_bare_invoke_becomes_tool_use(self):
        chunks = [
            "Applying the ownership wording fix.\n\n",
            '<invoke name="Edit">\n',
            '<parameter name="file_path">/tmp/a.md</parameter>\n',
            '<parameter name="old_string">foo</parameter>\n',
            '<parameter name="new_string">bar</parameter>\n',
            "</invoke>",
        ]
        events, text, tool_uses = _run_stream(chunks)

        self.assertNotIn("<invoke", text)
        self.assertIn("Applying the ownership wording fix.", text)
        self.assertEqual(len(tool_uses), 1)
        self.assertEqual(tool_uses[0]["name"], "Edit")
        parsed = json.loads(tool_uses[0]["input"])
        self.assertEqual(parsed["file_path"], "/tmp/a.md")
        self.assertEqual(parsed["old_string"], "foo")
        self.assertEqual(parsed["new_string"], "bar")
        self.assertEqual(_stop_reason(events), "tool_use")

    def test_multiple_bare_invokes_all_recovered(self):
        chunks = [
            '<invoke name="Edit">\n<parameter name="path">/a</parameter>\n</invoke>\n',
            '<invoke name="Edit">\n<parameter name="path">/b</parameter>\n</invoke>',
        ]
        _, text, tool_uses = _run_stream(chunks)
        self.assertNotIn("<invoke", text)
        self.assertEqual(len(tool_uses), 2)
        self.assertEqual([t["name"] for t in tool_uses], ["Edit", "Edit"])


class TestFabricatedToolResults(unittest.TestCase):
    """The model invents tool results it never received (ICA-2 behaviour)."""

    def test_fabricated_result_is_not_streamed_to_client(self):
        # Verbatim shape from logs/proxy_debug.log request 31639abe8a14.
        chunks = [
            "Applying the ownership wording fix and ",
            "with '**Last updated:** 2026-09-21'.\n",
            "</result>\n\n<result>\n",
            "The file /tmp/bulk-payment.md has been updated. ",
            "All occurrences have been replaced.",
        ]
        _, text, tool_uses = _run_stream(chunks)

        self.assertNotIn("</result>", text)
        self.assertNotIn("<result>", text)
        self.assertNotIn("has been updated", text)
        self.assertIn("Applying the ownership wording fix", text)
        self.assertEqual(tool_uses, [])

    def test_fabricated_function_results_block_suppressed(self):
        chunks = ["Done.\n<function_results>\n<result>fake</result>\n</function_results>"]
        _, text, _ = _run_stream(chunks)
        self.assertNotIn("<function_results>", text)
        self.assertNotIn("<result>", text)
        self.assertIn("Done.", text)


class TestIca1BehaviourUnchanged(unittest.TestCase):
    """ICA-1 emits the wrapper and invoke atomically; output must be unaffected."""

    def test_atomic_wrapper_chunk_is_parsed(self):
        # 1426 of 1428 ICA-1 wrapper chunks arrive like this (single chunk).
        chunks = [
            'Reading the file.\n<function_calls>\n  <invoke name="Read">\n'
            '    <parameter name="file_path">/tmp/x.md</parameter>\n'
            "  </invoke>\n</function_calls>"
        ]
        events, text, tool_uses = _run_stream(chunks)

        self.assertNotIn("<function_calls>", text)
        self.assertIn("Reading the file.", text)
        self.assertEqual(len(tool_uses), 1)
        self.assertEqual(tool_uses[0]["name"], "Read")
        self.assertEqual(json.loads(tool_uses[0]["input"])["file_path"], "/tmp/x.md")
        self.assertEqual(_stop_reason(events), "tool_use")

    def test_plain_prose_passes_through_unchanged(self):
        chunks = ["Hello, ", "this is a normal ", "answer with no tools."]
        events, text, tool_uses = _run_stream(chunks)
        self.assertEqual(text, "Hello, this is a normal answer with no tools.")
        self.assertEqual(tool_uses, [])
        self.assertEqual(_stop_reason(events), "end_turn")

    def test_prose_containing_angle_bracket_is_not_swallowed(self):
        """A lone '<' in prose must not stall or truncate the stream."""
        chunks = ["if a ", "< b and c ", "> d then done"]
        _, text, _ = _run_stream(chunks)
        self.assertEqual(text, "if a < b and c > d then done")

    def test_markdown_html_tag_passes_through(self):
        chunks = ["Use the ", "<div> element ", "here."]
        _, text, _ = _run_stream(chunks)
        self.assertEqual(text, "Use the <div> element here.")


class TestXmlStopSequenceGating(unittest.TestCase):
    """The stop sequence is opt-in per backend so ICA-1 is left untouched."""

    def _request(self, stop_sequences=None):
        return MessagesRequest(
            model="claude-opus-5",
            max_tokens=32000,
            messages=[{"role": "user", "content": "hi"}],
            stop_sequences=stop_sequences,
        )

    def test_stop_sequence_injected_when_enabled(self):
        translated = anthropic_to_openai_request(
            self._request(),
            "claude-opus-5",
            force_xml_tools=True,
            xml_stop_sequence=True,
        )
        self.assertIn(_XML_STOP_SEQUENCE, translated.stop or [])

    def test_stop_sequence_absent_when_disabled(self):
        """ICA-1 path: request must be identical to previous behaviour (stop=None)."""
        translated = anthropic_to_openai_request(
            self._request(),
            "global/anthropic.claude-sonnet-4-6",
            force_xml_tools=True,
            xml_stop_sequence=False,
        )
        self.assertIsNone(translated.stop)

    def test_client_stop_sequences_are_preserved(self):
        translated = anthropic_to_openai_request(
            self._request(stop_sequences=["END"]),
            "claude-opus-5",
            force_xml_tools=True,
            xml_stop_sequence=True,
        )
        self.assertIn("END", translated.stop)
        self.assertIn(_XML_STOP_SEQUENCE, translated.stop)

    def test_client_stop_sequences_untouched_when_disabled(self):
        translated = anthropic_to_openai_request(
            self._request(stop_sequences=["END"]),
            "global/anthropic.claude-sonnet-4-6",
            force_xml_tools=True,
            xml_stop_sequence=False,
        )
        self.assertEqual(translated.stop, ["END"])

    def test_no_stop_sequence_outside_xml_mode(self):
        translated = anthropic_to_openai_request(
            self._request(),
            "claude-opus-5",
            force_xml_tools=False,
            xml_stop_sequence=True,
        )
        self.assertIsNone(translated.stop)


class TestBackendGatingHelpers(unittest.TestCase):
    """Backend id derivation and stop-sequence gating in the router."""

    class _Settings:
        ica2_base_url = "https://api.servicesessentials.ibm.com/v1"
        xml_stop_sequence_backends = "ica2"

    def test_backend_id_detection(self):
        from routers.messages import _backend_id_for_url

        s = self._Settings()
        self.assertEqual(
            _backend_id_for_url(
                "https://api.servicesessentials.ibm.com/v1/chat/completions", s
            ),
            "ica2",
        )
        self.assertEqual(
            _backend_id_for_url("https://sg.ica.ibm.com/ica/apis/v3/chat/completions", s),
            "ica1",
        )

    def test_only_ica2_gets_stop_sequence_by_default(self):
        from routers.messages import _xml_stop_sequence_enabled

        s = self._Settings()
        self.assertTrue(_xml_stop_sequence_enabled("ica2", s))
        self.assertFalse(_xml_stop_sequence_enabled("ica1", s))

    def test_empty_setting_disables_everywhere(self):
        from routers.messages import _xml_stop_sequence_enabled

        s = self._Settings()
        s.xml_stop_sequence_backends = ""
        self.assertFalse(_xml_stop_sequence_enabled("ica1", s))
        self.assertFalse(_xml_stop_sequence_enabled("ica2", s))

    def test_ica2_disabled_means_all_urls_are_ica1(self):
        from routers.messages import _backend_id_for_url

        s = self._Settings()
        s.ica2_base_url = ""
        self.assertEqual(
            _backend_id_for_url(
                "https://api.servicesessentials.ibm.com/v1/chat/completions", s
            ),
            "ica1",
        )


class TestToolSystemPromptRules(unittest.TestCase):
    """The injected prompt must forbid fabricating tool results."""

    def test_rules_present(self):
        from services.translator import tools_to_system_prompt

        prompt = tools_to_system_prompt(
            [{"name": "Read", "description": "Read a file", "input_schema": {}}]
        )
        self.assertIn("</function_calls>", prompt)
        self.assertIn("STOP generating immediately", prompt)
        self.assertIn("NEVER write <function_results>", prompt)
        self.assertIn("Read", prompt)


if __name__ == "__main__":
    unittest.main()