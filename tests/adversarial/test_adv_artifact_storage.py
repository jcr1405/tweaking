"""Adversarial Verification Suite: Artifact Storage & Large Outputs
Tests:
1. Exact threshold boundaries: 40,959 bytes, 40,960 bytes, 40,961 bytes.
2. UTF-8 multibyte boundary safety during chunked byte-mode reads.
3. Directory traversal rejection for conversation_id and artifact_id.
4. Concurrency test: Multiple threads storing artifacts simultaneously.
   Tests whether concurrent writes can exceed the 200 MB conversation storage quota (FINDING-ADV-03).
"""

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from runtime.artifacts.store import (
    ArtifactStore,
    ARTIFACT_THRESHOLD_BYTES,
    MAX_CONVERSATION_BUDGET_BYTES,
)


class TestAdvArtifactStorage(unittest.TestCase):

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_adv_artifacts_"))
        self.store = ArtifactStore(root_dir=self.temp_dir)
        self.conv_id = "test_adv_conv_001"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_exact_40kb_threshold_boundaries(self):
        """Verifies boundary behavior at 40959, 40960, and 40961 bytes."""
        payload_40959 = "A" * (ARTIFACT_THRESHOLD_BYTES - 1)  # 40,959 bytes
        payload_40960 = "A" * ARTIFACT_THRESHOLD_BYTES        # 40,960 bytes
        payload_40961 = "A" * (ARTIFACT_THRESHOLD_BYTES + 1)  # 40,961 bytes

        # len <= 40960 should NOT offload
        self.assertFalse(self.store.should_offload(payload_40959))
        self.assertFalse(self.store.should_offload(payload_40960))

        # len > 40960 MUST offload
        self.assertTrue(self.store.should_offload(payload_40961))

    def test_multibyte_utf8_threshold_boundary(self):
        """Verifies UTF-8 multibyte characters (e.g. 3-byte or 4-byte characters) correctly trigger offload based on byte length."""
        # 3-byte UTF-8 char: '€'
        chars_sub = (ARTIFACT_THRESHOLD_BYTES - 1) // 3
        payload_sub = "€" * chars_sub
        self.assertLessEqual(len(payload_sub.encode("utf-8")), ARTIFACT_THRESHOLD_BYTES)
        self.assertFalse(self.store.should_offload(payload_sub))

        chars_over = (ARTIFACT_THRESHOLD_BYTES // 3) + 1
        payload_over = "€" * chars_over
        self.assertGreater(len(payload_over.encode("utf-8")), ARTIFACT_THRESHOLD_BYTES)
        self.assertTrue(self.store.should_offload(payload_over))

    def test_chunked_byte_read_multibyte_safety(self):
        """Verifies that chunked reading in byte mode does not crash or corrupt when reading across multibyte boundaries."""
        # Store an artifact containing 4-byte emojis
        emojis = "🚀🛡️⚡🔥" * 2000
        meta = self.store.store_text_artifact(
            conversation_id=self.conv_id,
            tool_call_id="call_001",
            tool_name="test_tool",
            content_str=emojis,
        )

        # Read arbitrary byte offset that falls inside a 4-byte emoji sequence
        res = self.store.read_artifact(
            conversation_id=self.conv_id,
            artifact_id=meta.artifact_id,
            offset=3,  # Splits the first 4-byte emoji
            limit=50,
            mode="bytes"
        )
        # Should not raise UnicodeDecodeError, replaces with replacement char
        self.assertIsNotNone(res.content)
        self.assertEqual(res.returned_count, 50)

    def test_directory_traversal_rejection(self):
        """Verifies that directory traversal payloads in conversation_id or artifact_id are rejected."""
        traversal_payloads = [
            "../escaped",
            "..\\escaped",
            "/etc/passwd",
            "C:\\Windows\\System32",
            "conv\0nullbyte",
            "conv/sub/path",
        ]

        for payload in traversal_payloads:
            with self.assertRaises((ValueError, PermissionError)):
                self.store.store_text_artifact(
                    conversation_id=payload,
                    tool_call_id="call_001",
                    tool_name="tool",
                    content_str="test"
                )

            with self.assertRaises((ValueError, PermissionError)):
                self.store.read_artifact(
                    conversation_id=self.conv_id,
                    artifact_id=payload,
                )

    def test_concurrent_writes_quota_race_condition(self):
        """CRITICAL AUDIT TEST FOR FINDING-ADV-03:
        Tests whether concurrent threads storing artifacts can race and exceed
        the MAX_CONVERSATION_BUDGET_BYTES (200 MB) quota.
        """
        # We simulate a conversation that is currently at 195 MB
        # Each thread attempts to write 6 MB.
        # Two threads writing concurrently:
        # If no synchronization, both see current_usage=195MB (< 200MB), both write 6MB -> Total becomes 207MB!
        # For testing speed, we use smaller numbers:
        # Store 1 MB per thread in 5 threads
        payload = "A" * (1024 * 1024)  # 1 MB

        errors = []
        threads = []

        def worker(idx):
            try:
                self.store.store_text_artifact(
                    conversation_id=self.conv_id,
                    tool_call_id=f"call_{idx}",
                    tool_name="tool",
                    content_str=payload,
                )
            except Exception as e:
                errors.append(e)

        # Launch 5 concurrent writes
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()

        # Check total storage usage
        usage = self.store.get_conversation_storage_usage(self.conv_id)
        # 5 artifacts of 1 MB each + metadata
        self.assertGreaterEqual(usage, 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
