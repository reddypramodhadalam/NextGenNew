"""Offline regression checks for complete Bedrock tool-result serialization."""

import json
import unittest
from datetime import date

from llm.bedrock_provider import BedrockProvider


class BedrockToolResponseTests(unittest.TestCase):
    def setUp(self):
        # Serialization does not need gateway configuration or a network call.
        self.provider = object.__new__(BedrockProvider)
        self.provider._last_tool_call_id = "test-tool-call"

    def test_large_snapshot_preserves_all_elements_and_flow_reminder(self):
        elements = [
            {"ref": f"el-{i}", "role": "button", "name": f"Control {i} " + "x" * 160}
            for i in range(100)
        ]
        elements.append({"ref": "el-target", "role": "a", "name": "Late target"})
        result = {"elements": elements, "_flow_reminder": "CURRENT STEP: Click Late target"}
        expected = json.dumps({"result": result}, default=str)
        self.assertGreater(expected.index("Late target"), 12000)

        message = self.provider.function_response_content("get_snapshot", result).to_message()

        self.assertEqual(message["content"], expected)
        self.assertEqual(json.loads(message["content"]), {"result": result})
        self.assertEqual(message["role"], "tool")
        self.assertEqual(message["tool_call_id"], "test-tool-call")

    def test_small_response_keeps_existing_contract(self):
        result = {"ok": True}
        message = self.provider.function_response_content("click", result).to_message()
        self.assertEqual(message, {
            "role": "tool",
            "tool_call_id": "test-tool-call",
            "content": json.dumps({"result": result}),
        })

    def test_non_snapshot_response_preserves_long_text_and_string_fallback(self):
        result = {"text": "x" * 12500 + "END", "date": date(2026, 1, 1)}
        content = self.provider.function_response_content("other_tool", result).to_message()["content"]
        self.assertEqual(json.loads(content), {
            "result": {"text": result["text"], "date": "2026-01-01"},
        })


if __name__ == "__main__":
    unittest.main()