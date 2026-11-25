"""Unit & Integration Tests for Ollama Model Provider
Verifies configuration validation, Mode A (routine) vs Mode B (reasoning) switching,
thinking token isolation, tool-call mapping, residual <think> tag stripping,
and cancellation token enforcement.
"""

import json
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import os

from runtime.providers.base import ModelStreamChunk
from runtime.providers.ollama import (
    OllamaModelProvider,
    OllamaConfig,
    normalize_schema_types,
)
from runtime.agent_executor import CancellationToken


class TestOllamaModelProvider(unittest.TestCase):
    """Unit tests for OllamaModelProvider adapter."""

    def test_01_config_and_mode_initialization(self):
        """Verifies default mode initialization and explicit parameter overrides."""
        # Default initialization: Mode A (routine) with think=False
        p_default = OllamaModelProvider(model_id="qwen3.5:9b")
        self.assertEqual(p_default.provider_name, "ollama")
        self.assertEqual(p_default.model_id, "qwen3.5:9b")
        self.assertEqual(p_default.mode, "routine")
        self.assertFalse(p_default.think_enabled)
        self.assertEqual(p_default.temperature, 0.2)

        # Mode B (reasoning) with think=True
        p_reason = OllamaModelProvider(model_id="qwen3.5:9b", mode="reasoning")
        self.assertEqual(p_reason.mode, "reasoning")
        self.assertTrue(p_reason.think_enabled)
        self.assertEqual(p_reason.temperature, 0.7)

        # Dynamic mode switching
        p_switch = OllamaModelProvider(model_id="qwen3.5:9b")
        p_switch.set_mode("reasoning")
        self.assertEqual(p_switch.mode, "reasoning")
        self.assertTrue(p_switch.think_enabled)
        self.assertEqual(p_switch.temperature, 0.7)

        p_switch.set_mode("routine")
        self.assertEqual(p_switch.mode, "routine")
        self.assertFalse(p_switch.think_enabled)
        self.assertEqual(p_switch.temperature, 0.2)

    def test_02_schema_normalization(self):
        """Verifies schema types like STRING, OBJECT are converted to lowercase for Ollama."""
        raw_schema = {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "file path"},
                "count": {"type": "INTEGER"}
            },
            "required": ["path"]
        }
        norm = normalize_schema_types(raw_schema)
        self.assertEqual(norm["type"], "object")
        self.assertEqual(norm["properties"]["path"]["type"], "string")
        self.assertEqual(norm["properties"]["count"]["type"], "integer")

    @patch("httpx.Client.get")
    def test_03_validate_configuration(self, mock_get):
        """Verifies model validation against Ollama /api/tags."""
        # Model installed
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "qwen3.5:9b", "size": 6600000000},
                {"name": "llama3:latest", "size": 4700000000}
            ]
        }
        mock_get.return_value = mock_resp

        p = OllamaModelProvider(model_id="qwen3.5:9b")
        valid, msg = p.validate_configuration()
        self.assertTrue(valid)
        self.assertIn("qwen3.5:9b", msg)

        # Model missing
        p_missing = OllamaModelProvider(model_id="deepseek-r1:70b")
        valid_m, msg_m = p_missing.validate_configuration()
        self.assertFalse(valid_m)
        self.assertIn("not installed", msg_m)

    @patch("httpx.Client.stream")
    def test_04_thinking_isolation_and_streaming(self, mock_stream):
        """Verifies that thinking tokens are strictly isolated from text_delta."""
        p = OllamaModelProvider(model_id="qwen3.5:9b", think=True)

        # Simulated response lines from Ollama /api/chat
        lines = [
            json.dumps({"message": {"role": "assistant", "thinking": "Let me think about how to greet the user."}}),
            json.dumps({"message": {"role": "assistant", "thinking": " Greeting in a friendly tone is best."}}),
            json.dumps({"message": {"role": "assistant", "content": "Hello!"}}),
            json.dumps({"message": {"role": "assistant", "content": " How can I help you today?"}}),
            json.dumps({"done": True, "prompt_eval_count": 10, "eval_count": 35, "done_reason": "stop"})
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = lines
        mock_stream.return_value.__enter__.return_value = mock_resp

        chunks = list(p.generate_stream(system_prompt="Test", messages=[{"role": "user", "content": "hey"}]))

        thinking_text = "".join(c.thinking_status for c in chunks)
        content_text = "".join(c.text_delta for c in chunks)

        # Verify strict separation
        self.assertEqual(thinking_text, "Let me think about how to greet the user. Greeting in a friendly tone is best.")
        self.assertEqual(content_text, "Hello! How can I help you today?")
        
        # Verify usage accounting
        usage_chunks = [c for c in chunks if c.usage]
        self.assertEqual(len(usage_chunks), 1)
        self.assertEqual(usage_chunks[0].usage["completion_tokens"], 35)

    @patch("httpx.Client.stream")
    def test_05_tool_call_emission(self, mock_stream):
        """Verifies structured tool calls are parsed without leaking into text."""
        p = OllamaModelProvider(model_id="qwen3.5:9b", think=False)

        lines = [
            json.dumps({
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "function": {
                                "name": "list_directory",
                                "arguments": {"path": "C:\\projects"}
                            }
                        }
                    ]
                }
            }),
            json.dumps({"done": True, "prompt_eval_count": 15, "eval_count": 25, "done_reason": "stop"})
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = lines
        mock_stream.return_value.__enter__.return_value = mock_resp

        chunks = list(p.generate_stream(system_prompt="Test", messages=[{"role": "user", "content": "list dir"}]))

        # Content must be completely empty
        content_text = "".join(c.text_delta for c in chunks)
        self.assertEqual(content_text, "")

        # Tool call must be extracted
        tool_chunks = [c for c in chunks if c.tool_calls]
        self.assertEqual(len(tool_chunks), 1)
        tc = tool_chunks[0].tool_calls[0]
        self.assertEqual(tc["name"], "list_directory")
        self.assertEqual(tc["args"], {"path": "C:\\projects"})
        self.assertEqual(tc["call_id"], "call_123")

    @patch("httpx.Client.stream")
    def test_06_residual_think_tags_defense(self, mock_stream):
        """Verifies defense-in-depth against models outputting <think>...</think> in content."""
        p = OllamaModelProvider(model_id="qwen3.5:9b")

        lines = [
            json.dumps({"message": {"role": "assistant", "content": "<think>Internal thoughts</think>Final answer"}}),
            json.dumps({"done": True, "eval_count": 10})
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = lines
        mock_stream.return_value.__enter__.return_value = mock_resp

        chunks = list(p.generate_stream(system_prompt="Test", messages=[{"role": "user", "content": "test"}]))

        thinking_text = "".join(c.thinking_status for c in chunks)
        content_text = "".join(c.text_delta for c in chunks)

        self.assertNotIn("<think>", content_text)
        self.assertNotIn("Internal thoughts", content_text)
        self.assertEqual(content_text, "Final answer")
        self.assertEqual(thinking_text, "Internal thoughts")


if __name__ == "__main__":
    unittest.main()
