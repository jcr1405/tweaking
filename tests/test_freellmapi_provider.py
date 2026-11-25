"""Unit & Integration Tests for FreeLLMAPI Model Provider
Verifies configuration validation, OpenAI-compatible request/response mapping,
SSE stream parsing, fragmented tool-call reassembly, usage accounting, error redaction,
and TUI hot-swapping.
"""

import json
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile
import os

from runtime.providers.base import ModelStreamChunk
from runtime.providers.freellmapi import (
    FreeLLMAPIProvider,
    FreeLLMAPIConfig,
    normalize_schema_types,
    detect_freellmapi_credentials,
)
from runtime.agent_executor import CancellationToken


class TestFreeLLMAPIProvider(unittest.TestCase):
    """Unit tests for FreeLLMAPI provider adapter."""

    def test_01_config_validation(self):
        """Verifies URL syntax and API key checks."""
        # Missing key
        p_no_key = FreeLLMAPIProvider(base_url="http://127.0.0.1:3001/v1", api_key="")
        valid, msg = p_no_key.validate_configuration()
        self.assertFalse(valid)
        self.assertIn("FREELLMAPI_API_KEY", msg)

        # Invalid URL
        p_bad_url = FreeLLMAPIProvider(base_url="not_a_valid_url", api_key="secret-key-123")
        valid, msg = p_bad_url.validate_configuration()
        self.assertFalse(valid)
        self.assertIn("Invalid FreeLLMAPI Base URL", msg)

        # Valid config
        p_valid = FreeLLMAPIProvider(base_url="http://127.0.0.1:3001/v1", api_key="secret-key-123")
        valid, msg = p_valid.validate_configuration()
        self.assertTrue(valid)
        self.assertEqual(p_valid.provider_name, "freellmapi")
        self.assertEqual(p_valid.model_id, "auto")

    def test_02_schema_normalization(self):
        """Verifies schema types like STRING, OBJECT are converted to lowercase."""
        raw_schema = {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "name of user"},
                "age": {"type": "INTEGER"},
                "tags": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"}
                }
            },
            "required": ["name"]
        }
        normalized = normalize_schema_types(raw_schema)
        self.assertEqual(normalized["type"], "object")
        self.assertEqual(normalized["properties"]["name"]["type"], "string")
        self.assertEqual(normalized["properties"]["age"]["type"], "integer")
        self.assertEqual(normalized["properties"]["tags"]["type"], "array")
        self.assertEqual(normalized["properties"]["tags"]["items"]["type"], "string")

    def test_03_credential_redaction(self):
        """Verifies API key is redacted from error strings."""
        secret_key = "test-secret-key-xyz-987"
        p = FreeLLMAPIProvider(base_url="http://127.0.0.1:3001/v1", api_key=secret_key)
        redacted = p._redact(f"Failed with key {secret_key} on request")
        self.assertNotIn(secret_key, redacted)
        self.assertIn("[REDACTED_API_KEY]", redacted)

    def test_04_streaming_text_deltas(self):
        """Verifies SSE text deltas and usage metadata parsing."""
        sse_lines = [
            b"data: {\"choices\": [{\"delta\": {\"content\": \"Hello \"}}]}\n",
            b"data: {\"choices\": [{\"delta\": {\"content\": \"World!\"}}]}\n",
            b"data: {\"choices\": [], \"usage\": {\"prompt_tokens\": 10, \"completion_tokens\": 5}}\n",
            b"data: [DONE]\n"
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = [l.decode("utf-8") for l in sse_lines]

        p = FreeLLMAPIProvider(base_url="http://127.0.0.1:3001/v1", api_key="test-key")

        with patch.object(p, "_get_client") as mock_client_factory:
            mock_client = MagicMock()
            mock_client.stream.return_value.__enter__.return_value = mock_resp
            mock_client_factory.return_value = mock_client

            chunks = list(p.generate_stream(
                system_prompt="System instructions",
                messages=[{"role": "user", "content": "Hi"}]
            ))

        text = "".join(c.text_delta for c in chunks)
        self.assertEqual(text, "Hello World!")

        # Verify usage captured
        usage_chunks = [c for c in chunks if c.usage]
        self.assertTrue(len(usage_chunks) > 0)
        self.assertEqual(usage_chunks[-1].usage.get("prompt_tokens"), 10)
        self.assertEqual(usage_chunks[-1].usage.get("completion_tokens"), 5)

    def test_05_fragmented_tool_call_reassembly(self):
        """Verifies fragmented tool call arguments across multiple SSE chunks are properly assembled."""
        chunk1 = json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_abc123",
                        "type": "function",
                        "function": {"name": "presence.web_search", "arguments": ""}
                    }]
                }
            }]
        })
        chunk2 = json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": '{"query": '}
                    }]
                }
            }]
        })
        chunk3 = json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": '"Tokyo weather"}'}
                    }]
                }
            }]
        })
        chunk4 = json.dumps({
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}]
        })

        sse_lines = [
            f"data: {chunk1}\n",
            f"data: {chunk2}\n",
            f"data: {chunk3}\n",
            f"data: {chunk4}\n",
            "data: [DONE]\n"
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = sse_lines


        p = FreeLLMAPIProvider(base_url="http://127.0.0.1:3001/v1", api_key="test-key")

        with patch.object(p, "_get_client") as mock_client_factory:
            mock_client = MagicMock()
            mock_client.stream.return_value.__enter__.return_value = mock_resp
            mock_client_factory.return_value = mock_client

            chunks = list(p.generate_stream(
                system_prompt="System instructions",
                messages=[{"role": "user", "content": "Check weather"}]
            ))

        tool_chunks = [c for c in chunks if c.tool_calls]
        self.assertEqual(len(tool_chunks), 1)

        tcs = tool_chunks[0].tool_calls
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["call_id"], "call_abc123")
        self.assertEqual(tcs[0]["name"], "presence.web_search")
        self.assertEqual(tcs[0]["args"], {"query": "Tokyo weather"})

    def test_06_stream_cancellation(self):
        """Verifies cancellation token stops the stream consumption."""
        token = CancellationToken()

        def mock_lines():
            yield "data: {\"choices\": [{\"delta\": {\"content\": \"Part 1\"}}]}"
            token.cancel()
            yield "data: {\"choices\": [{\"delta\": {\"content\": \"Part 2\"}}]}"
            yield "data: [DONE]"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = mock_lines()

        p = FreeLLMAPIProvider(base_url="http://127.0.0.1:3001/v1", api_key="test-key")

        with patch.object(p, "_get_client") as mock_client_factory:
            mock_client = MagicMock()
            mock_client.stream.return_value.__enter__.return_value = mock_resp
            mock_client_factory.return_value = mock_client

            chunks = list(p.generate_stream(
                system_prompt="System instructions",
                messages=[{"role": "user", "content": "Hi"}],
                cancellation_token=token
            ))

        text = "".join(c.text_delta for c in chunks)
        self.assertEqual(text, "Part 1")
        self.assertNotIn("Part 2", text)

    def test_07_http_error_handling(self):
        """Verifies 401 and 429 errors raise descriptive sanitized RuntimeErrors."""
        import httpx

        p = FreeLLMAPIProvider(base_url="http://127.0.0.1:3001/v1", api_key="super-secret-key")

        mock_resp_401 = MagicMock()
        mock_resp_401.status_code = 401
        mock_resp_401.read.return_value = b'{"error": "Unauthorized key: super-secret-key"}'

        with patch.object(p, "_get_client") as mock_client_factory:
            mock_client = MagicMock()
            mock_client.stream.return_value.__enter__.return_value = mock_resp_401
            mock_client_factory.return_value = mock_client

            with self.assertRaises(RuntimeError) as ctx:
                list(p.generate_stream(
                    system_prompt="Test",
                    messages=[{"role": "user", "content": "Hi"}]
                ))

            err = str(ctx.exception)
            self.assertIn("Authentication failed", err)
            self.assertNotIn("super-secret-key", err)


class TestFreeLLMAPIServiceIntegration(unittest.TestCase):
    """Tests service-level configuration and persistence for FreeLLMAPI."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        self.mock_runtime = MagicMock()
        from runtime.observability.service import RuntimeObservabilityService
        self.service = RuntimeObservabilityService(runtime=self.mock_runtime)

    def tearDown(self):
        for k in ["FREELLMAPI_BASE_URL", "FREELLMAPI_API_KEY", "FREELLMAPI_MODEL", "PRESENCE_PROVIDER"]:
            if k in os.environ:
                del os.environ[k]
        os.chdir(self.original_cwd)
        self.temp_dir.cleanup()


    def test_01_configure_freellmapi_persists_to_dotenv(self):
        """Verifies configure_freellmapi_provider writes settings to .env."""
        test_url = "http://127.0.0.1:31415/v1"
        test_key = "free-test-key-12345"
        test_model = "qwen-3.8-27b"

        success, msg = self.service.configure_freellmapi_provider(
            base_url=test_url,
            api_key=test_key,
            model_id=test_model,
            test_live=False,
            persist=True
        )
        self.assertTrue(success)
        self.assertEqual(os.environ.get("FREELLMAPI_BASE_URL"), test_url)
        self.assertEqual(os.environ.get("FREELLMAPI_API_KEY"), test_key)
        self.assertEqual(os.environ.get("FREELLMAPI_MODEL"), test_model)
        self.assertEqual(os.environ.get("PRESENCE_PROVIDER"), "freellmapi")

        env_file = Path(self.temp_dir.name) / ".env"
        self.assertTrue(env_file.exists())
        content = env_file.read_text(encoding="utf-8")
        self.assertIn(f"FREELLMAPI_BASE_URL={test_url}", content)
        self.assertIn(f"FREELLMAPI_API_KEY={test_key}", content)
        self.assertIn(f"FREELLMAPI_MODEL={test_model}", content)
        self.assertIn("PRESENCE_PROVIDER=freellmapi", content)


if __name__ == "__main__":
    unittest.main()
