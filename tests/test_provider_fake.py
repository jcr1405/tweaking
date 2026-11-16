"""Automated Unit Tests for Fake Deterministic Model Provider"""

import unittest
from runtime.providers.fake import FakeModelProvider
from runtime.providers.base import ModelStreamChunk
from runtime.agent_executor import CancellationToken


class TestFakeProvider(unittest.TestCase):
    """Validates FakeModelProvider deterministic responses, streaming, and tool calls."""

    def test_default_text_stream(self):
        provider = FakeModelProvider()
        chunks = list(provider.generate_stream(
            system_prompt="System",
            messages=[{"role": "user", "content": "Hello"}]
        ))
        self.assertGreaterEqual(len(chunks), 3)
        combined_text = "".join(c.text_delta for c in chunks)
        self.assertIn("Persistent Intelligent Presence", combined_text)
        self.assertEqual(chunks[-1].finish_reason, "STOP")

    def test_scripted_response_queue(self):
        provider = FakeModelProvider()
        scripted = [
            ModelStreamChunk(text_delta="Custom "),
            ModelStreamChunk(text_delta="Scripted "),
            ModelStreamChunk(text_delta="Output.", finish_reason="STOP")
        ]
        provider.queue_response(scripted)

        chunks = list(provider.generate_stream(
            system_prompt="System",
            messages=[{"role": "user", "content": "Run script"}]
        ))
        combined_text = "".join(c.text_delta for c in chunks)
        self.assertEqual(combined_text, "Custom Scripted Output.")

    def test_tool_call_scripting(self):
        provider = FakeModelProvider()
        scripted_tool_call = [
            ModelStreamChunk(tool_calls=[{
                "call_id": "call_status_1",
                "name": "presence.get_runtime_status",
                "args": {}
            }])
        ]
        provider.queue_response(scripted_tool_call)

        chunks = list(provider.generate_stream(
            system_prompt="System",
            messages=[{"role": "user", "content": "What is runtime status?"}]
        ))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0].tool_calls), 1)
        self.assertEqual(chunks[0].tool_calls[0]["name"], "presence.get_runtime_status")

    def test_simulated_error(self):
        provider = FakeModelProvider(simulate_error=ConnectionError("Simulated provider network drop"))
        with self.assertRaises(ConnectionError):
            list(provider.generate_stream(
                system_prompt="System",
                messages=[{"role": "user", "content": "Hello"}]
            ))

    def test_cancellation_during_stream(self):
        provider = FakeModelProvider()
        scripted = [
            ModelStreamChunk(text_delta="Chunk 1 "),
            ModelStreamChunk(text_delta="Chunk 2 "),
            ModelStreamChunk(text_delta="Chunk 3 ")
        ]
        provider.queue_response(scripted)

        token = CancellationToken()
        chunks = []
        for c in provider.generate_stream(
            system_prompt="System",
            messages=[{"role": "user", "content": "Stream"}],
            cancellation_token=token
        ):
            chunks.append(c)
            token.cancel()  # Cancel after first chunk

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text_delta, "Chunk 1 ")


if __name__ == "__main__":
    unittest.main()
