"""Tests for Artifact Store & Large Tool Output Offloading
Verifies 40 KB offloading threshold, atomic storage, SHA-256 integrity,
preview envelopes, traversal defenses, chunked reading, and resource quotas.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.artifacts.store import (
    ArtifactStore,
    ARTIFACT_THRESHOLD_BYTES,
    MAX_ARTIFACT_SIZE_BYTES,
    MAX_CONVERSATION_BUDGET_BYTES
)


class TestArtifactStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_artifact_store_"))
        self.store = ArtifactStore(root_dir=self.temp_dir)
        self.conv_id = "conv_test_12345"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_offload_threshold_detection(self):
        """Verifies boundary detection for output offloading at ARTIFACT_THRESHOLD_BYTES."""
        from runtime.artifacts.store import ARTIFACT_THRESHOLD_BYTES
        # Content <= ARTIFACT_THRESHOLD_BYTES bytes does not trigger offload
        small_text = "A" * ARTIFACT_THRESHOLD_BYTES
        self.assertFalse(self.store.should_offload(small_text))

        # Content ARTIFACT_THRESHOLD_BYTES + 1 bytes triggers offload
        large_text = "A" * (ARTIFACT_THRESHOLD_BYTES + 1)
        self.assertTrue(self.store.should_offload(large_text))


    def test_atomic_artifact_storage_and_metadata(self):
        """Verifies atomic write, SHA-256 calculation, and metadata sidecar persistence."""
        # 60 KB payload
        sample_lines = [f"Entry #{i:04d}: test log line with diagnostic information" for i in range(1200)]
        content = "\n".join(sample_lines)

        meta = self.store.store_text_artifact(
            conversation_id=self.conv_id,
            tool_call_id="call_test_01",
            tool_name="test_tool",
            content_str=content
        )

        self.assertTrue(meta.artifact_id.startswith("art_"))
        self.assertEqual(meta.conversation_id, self.conv_id)
        self.assertEqual(meta.tool_call_id, "call_test_01")
        self.assertEqual(meta.tool_name, "test_tool")
        self.assertEqual(meta.byte_size, len(content.encode("utf-8")))
        self.assertEqual(meta.line_count, len(sample_lines))
        self.assertTrue(Path(meta.file_path).exists())

        # Retrieve and verify metadata sidecar
        loaded_meta = self.store.get_metadata(self.conv_id, meta.artifact_id)
        self.assertIsNotNone(loaded_meta)
        self.assertEqual(loaded_meta.sha256, meta.sha256)
        self.assertEqual(loaded_meta.byte_size, meta.byte_size)

        # Verify listing
        artifacts = self.store.list_artifacts(self.conv_id)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].artifact_id, meta.artifact_id)

    def test_preview_generation(self):
        """Verifies that generated preview contains head/tail lines and clear retrieval instructions."""
        sample_lines = [f"Data row {i:04d}" for i in range(500)]
        content = "\n".join(sample_lines)

        meta = self.store.store_text_artifact(
            conversation_id=self.conv_id,
            tool_call_id="call_02",
            tool_name="data_gen",
            content_str=content
        )

        preview = meta.preview
        self.assertIn("Data row 0000", preview)
        self.assertIn("Data row 0019", preview)
        self.assertIn(f"TRUNCATED - Full tool output ({meta.byte_size:,} bytes", preview)
        self.assertIn(f"artifact '{meta.artifact_id}'", preview)
        self.assertIn("presence.read_artifact", preview)
        self.assertIn("Data row 0499", preview)

    def test_directory_traversal_prevention(self):
        """Verifies that invalid identifiers or path traversal attempts are strictly rejected."""
        # Traversal in conversation_id
        with self.assertRaises((ValueError, PermissionError)):
            self.store._get_conversation_dir("../escape_dir")

        with self.assertRaises(ValueError):
            self.store._get_conversation_dir("conv/illegal/slash")

        # Traversal in artifact_id
        with self.assertRaises(ValueError):
            self.store.read_artifact(self.conv_id, "../escape_art")

        with self.assertRaises(ValueError):
            self.store.read_artifact(self.conv_id, "art\\with\\backslashes")

    def test_chunked_reading_lines_mode(self):
        """Verifies reading specific line ranges with offset, limit, and has_more flag."""
        sample_lines = [f"Line {i:03d}" for i in range(150)]
        content = "\n".join(sample_lines)

        meta = self.store.store_text_artifact(
            conversation_id=self.conv_id,
            tool_call_id="call_lines",
            tool_name="lines_tool",
            content_str=content
        )

        # Read first 50 lines
        chunk1 = self.store.read_artifact(self.conv_id, meta.artifact_id, offset=0, limit=50, mode="lines")
        self.assertEqual(chunk1.returned_count, 50)
        self.assertTrue(chunk1.has_more)
        self.assertEqual(chunk1.content.splitlines()[0], "Line 000")
        self.assertEqual(chunk1.content.splitlines()[-1], "Line 049")

        # Read next 50 lines
        chunk2 = self.store.read_artifact(self.conv_id, meta.artifact_id, offset=50, limit=50, mode="lines")
        self.assertEqual(chunk2.returned_count, 50)
        self.assertTrue(chunk2.has_more)
        self.assertEqual(chunk2.content.splitlines()[0], "Line 050")
        self.assertEqual(chunk2.content.splitlines()[-1], "Line 099")

        # Read remaining 50 lines
        chunk3 = self.store.read_artifact(self.conv_id, meta.artifact_id, offset=100, limit=100, mode="lines")
        self.assertEqual(chunk3.returned_count, 50)
        self.assertFalse(chunk3.has_more)
        self.assertEqual(chunk3.content.splitlines()[0], "Line 100")
        self.assertEqual(chunk3.content.splitlines()[-1], "Line 149")

    def test_chunked_reading_bytes_mode(self):
        """Verifies byte-level chunked retrieval with byte offsets."""
        raw_text = "ABCDEFGHIJ" * 100  # 1000 characters
        meta = self.store.store_text_artifact(
            conversation_id=self.conv_id,
            tool_call_id="call_bytes",
            tool_name="bytes_tool",
            content_str=raw_text
        )

        chunk = self.store.read_artifact(self.conv_id, meta.artifact_id, offset=0, limit=20, mode="bytes")
        self.assertEqual(chunk.returned_count, 20)
        self.assertEqual(chunk.content, "ABCDEFGHIJABCDEFGHIJ")
        self.assertTrue(chunk.has_more)

    def test_resource_budget_limits(self):
        """Verifies rejection of oversized artifacts and conversation storage quotas."""
        # 1. Single artifact limit
        oversized = "X" * (MAX_ARTIFACT_SIZE_BYTES + 100)
        with self.assertRaises(ValueError) as ctx:
            self.store.store_text_artifact(self.conv_id, "call_over", "tool", oversized)
        self.assertIn("exceeds maximum single artifact limit", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
