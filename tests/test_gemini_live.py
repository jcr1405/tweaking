"""Opt-In Live Integration Test for Google Gemini API
Skipped automatically if GEMINI_API_KEY environment variable is not present.
"""

import os
import unittest
from runtime.providers.gemini import GeminiModelProvider, _GENAI_AVAILABLE


class TestGeminiLive(unittest.TestCase):
    """Opt-in live integration test against Google Gemini API."""

    def test_live_gemini_generation(self):
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise unittest.SkipTest("GEMINI_API_KEY environment variable not set. Skipping live test.")

        if not _GENAI_AVAILABLE:
            raise unittest.SkipTest("google-genai SDK not available. Skipping live test.")

        provider = GeminiModelProvider(
            api_key=api_key,
            temperature=0.0,
            max_output_tokens=512
        )

        valid, msg = provider.validate_configuration()
        self.assertTrue(valid, f"Provider configuration invalid: {msg}")

        chunks = list(provider.generate_stream(
            system_prompt="You are a test oracle. Follow instructions exactly.",
            messages=[{"role": "user", "content": "Respond with the single word PONG."}]
        ))

        full_text = "".join(c.text_delta for c in chunks)
        self.assertIn("PONG", full_text.upper())

        # Verify usage metadata
        last_chunk = chunks[-1]
        self.assertIsNotNone(last_chunk.usage)
        self.assertGreater(last_chunk.usage.get("prompt_tokens", 0), 0)

        provider.close()


if __name__ == "__main__":
    unittest.main()
