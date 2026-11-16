"""Tests for MCP Schema Caching and Lazy Hydration Infrastructure
Verifies disk persistence, SHA-256 integrity validation, invalidation, and declaration schemas.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.mcp.models import MCPToolInfo
from runtime.mcp.schemas import SchemaCache


class TestSchemaCache(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_schema_cache_"))
        self.cache = SchemaCache(cache_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cache_save_and_load(self):
        """Verifies saving a tool schema and reloading it with SHA-256 integrity verification."""
        tool = MCPToolInfo(
            server_id="fs_server",
            tool_name="read_directory",
            description="Lists files in a given directory.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Folder path"}
                },
                "required": ["path"]
            }
        )

        saved_path = self.cache.save_tool_schema(tool)
        self.assertTrue(saved_path.exists())

        loaded = self.cache.load_tool_schema("fs_server", "read_directory")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.server_id, "fs_server")
        self.assertEqual(loaded.tool_name, "read_directory")
        self.assertEqual(loaded.description, "Lists files in a given directory.")
        self.assertEqual(loaded.input_schema["required"], ["path"])

    def test_corrupt_cache_integrity_invalidation(self):
        """Verifies that tampering with a cached file causes cache invalidation."""
        tool = MCPToolInfo(
            server_id="sec_server",
            tool_name="check_auth",
            description="Checks user authorization status.",
            input_schema={"type": "object", "properties": {"token": {"type": "string"}}}
        )
        saved_path = self.cache.save_tool_schema(tool)

        # Tamper with the schema file by modifying the input_schema without updating the hash
        import json
        with open(saved_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["input_schema"]["properties"]["tampered"] = {"type": "boolean"}
        with open(saved_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        # Load should detect hash mismatch, delete the file, and return None
        loaded = self.cache.load_tool_schema("sec_server", "check_auth")
        self.assertIsNone(loaded)
        self.assertFalse(saved_path.exists())

    def test_server_invalidation_and_all_invalidation(self):
        """Verifies per-server and global schema invalidation."""
        tools = [
            MCPToolInfo("server_a", "tool_1", "desc", {}),
            MCPToolInfo("server_a", "tool_2", "desc", {}),
            MCPToolInfo("server_b", "tool_3", "desc", {})
        ]
        for t in tools:
            self.cache.save_tool_schema(t)

        self.assertEqual(len(self.cache.load_server_schemas("server_a")), 2)
        self.assertEqual(len(self.cache.load_server_schemas("server_b")), 1)

        # Invalidate server_a
        deleted_count = self.cache.invalidate_server("server_a")
        self.assertEqual(deleted_count, 2)
        self.assertEqual(len(self.cache.load_server_schemas("server_a")), 0)
        self.assertEqual(len(self.cache.load_server_schemas("server_b")), 1)

        # Invalidate all
        all_deleted = self.cache.invalidate_all()
        self.assertEqual(all_deleted, 1)
        self.assertEqual(len(self.cache.load_server_schemas("server_b")), 0)

    def test_to_declaration_schema(self):
        """Verifies declaration conversion conforms to standard function declaration format."""
        tool = MCPToolInfo(
            server_id="math_srv",
            tool_name="calculate_sum",
            description="Calculates arithmetic sum.",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"}
                },
                "required": ["a", "b"]
            }
        )
        declaration = self.cache.to_declaration(tool)
        self.assertEqual(declaration["name"], "mcp.math_srv.calculate_sum")
        self.assertEqual(declaration["description"], "Calculates arithmetic sum.")
        self.assertEqual(declaration["parameters"]["type"], "object")
        self.assertIn("a", declaration["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
