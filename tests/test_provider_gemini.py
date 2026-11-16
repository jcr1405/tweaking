"""Automated Unit Tests for Gemini Provider Adapter"""

import unittest
import os
from runtime.providers.gemini import GeminiModelProvider, _GENAI_AVAILABLE


class TestGeminiProvider(unittest.TestCase):
    """Validates GeminiModelProvider configuration, secret isolation, and error handling."""

    def test_missing_api_key_validation(self):
        # Temporarily clear key
        provider = GeminiModelProvider(api_key="")
        valid, msg = provider.validate_configuration()
        if _GENAI_AVAILABLE:
            self.assertFalse(valid)
            self.assertIn("GEMINI_API_KEY", msg)

    def test_configured_api_key_validation(self):
        provider = GeminiModelProvider(api_key="AIzaSyDummyKeyForTestingOnly1234567890")
        valid, msg = provider.validate_configuration()
        if _GENAI_AVAILABLE:
            self.assertTrue(valid)
            self.assertIn("valid", msg)

    def test_custom_model_configuration(self):
        provider = GeminiModelProvider(
            model_id="gemini-custom-pro",
            api_key="AIzaSyDummyKeyForTestingOnly1234567890",
            temperature=0.2,
            max_output_tokens=1024
        )
        self.assertEqual(provider.model_id, "gemini-custom-pro")
        self.assertEqual(provider.temperature, 0.2)
        self.assertEqual(provider.max_output_tokens, 1024)
        self.assertEqual(provider.provider_name, "gemini")

    def test_secret_not_leaked_in_exception(self):
        dummy_secret = "SECRET_KEY_NEVER_LEAK_123456789"
        provider = GeminiModelProvider(api_key=dummy_secret)
        # Attempting generate_stream with dummy key against real API will fail,
        # but the secret must never appear in the exception message
        if _GENAI_AVAILABLE:
            with self.assertRaises(RuntimeError) as ctx:
                list(provider.generate_stream(
                    system_prompt="Test",
                    messages=[{"role": "user", "content": "Hi"}]
                ))
            self.assertNotIn(dummy_secret, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
