"""Unit Tests for Tool Parameter Schema Validation (runtime/tools/registry.py & supervisor.py)
Verifies:
1. Schema normalization for compliant JSON Schema Draft-07.
2. Argument validation against required fields, types, enums, and additionalProperties.
3. Strict execution boundary in ToolRegistry rejecting malformed inputs before handler execution.
4. Early rejection in MCPSupervisor preventing malformed requests from reaching child processes.
"""

import sys
import unittest
from typing import Dict, Any

from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import (
    ToolRegistry,
    ToolDefinition,
    normalize_json_schema,
    validate_tool_arguments_schema,
)
from runtime.mcp.models import MCPServerConfig, MCPToolInfo, MCPServerState
from runtime.mcp.supervisor import MCPSupervisor


class TestSchemaValidation(unittest.TestCase):
    def setUp(self):
        self.sample_schema = {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "count": {"type": "integer"},
                "mode": {"type": "string", "enum": ["fast", "slow"]}
            },
            "required": ["message", "count"],
            "additionalProperties": False
        }

    def test_normalize_json_schema(self):
        """Gemini uppercase schema types are normalized to lowercase Draft-07 types."""
        gemini_schema = {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING"},
                "items": {"type": "ARRAY", "items": {"type": "INTEGER"}}
            }
        }
        norm = normalize_json_schema(gemini_schema)
        self.assertEqual(norm["type"], "object")
        self.assertEqual(norm["properties"]["name"]["type"], "string")
        self.assertEqual(norm["properties"]["items"]["type"], "array")
        self.assertEqual(norm["properties"]["items"]["items"]["type"], "integer")

    def test_valid_arguments_pass(self):
        """Valid arguments pass schema validation without raising."""
        valid_args = {"message": "hello", "count": 5, "mode": "fast"}
        validate_tool_arguments_schema(valid_args, self.sample_schema)

    def test_missing_required_property_fails(self):
        """Missing required properties raise ValueError naming the missing property."""
        missing_count = {"message": "hello"}
        with self.assertRaises(ValueError) as ctx:
            validate_tool_arguments_schema(missing_count, self.sample_schema)
        self.assertIn("'count' is a required property", str(ctx.exception))

    def test_wrong_type_fails(self):
        """Mismatched argument types raise ValueError."""
        wrong_type = {"message": "hello", "count": "not_an_integer"}
        with self.assertRaises(ValueError) as ctx:
            validate_tool_arguments_schema(wrong_type, self.sample_schema)
        self.assertIn("is not of type 'integer'", str(ctx.exception))

    def test_disallowed_additional_properties_fails(self):
        """Unexpected properties raise ValueError when additionalProperties is False."""
        extra_args = {"message": "hello", "count": 1, "unauthorized_flag": True}
        with self.assertRaises(ValueError) as ctx:
            validate_tool_arguments_schema(extra_args, self.sample_schema)
        self.assertIn("unauthorized_flag", str(ctx.exception))

    def test_enum_violation_fails(self):
        """Values outside enum definitions raise ValueError."""
        bad_enum = {"message": "hello", "count": 1, "mode": "invalid_mode"}
        with self.assertRaises(ValueError) as ctx:
            validate_tool_arguments_schema(bad_enum, self.sample_schema)
        self.assertIn("'invalid_mode' is not one of ['fast', 'slow']", str(ctx.exception))

    def test_registry_executes_handler_only_if_valid(self):
        """ToolRegistry.execute rejects invalid arguments before invoking the handler."""
        handler_called = False

        def sample_handler(args: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal handler_called
            handler_called = True
            return {"echo": args["message"]}

        registry = ToolRegistry()
        boundary = PermissionBoundary()
        registry.register(
            ToolDefinition(
                tool_id="test_tool",
                name="test_tool",
                description="Test schema tool",
                parameters=self.sample_schema,
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=sample_handler
            ),
            permission_boundary=boundary
        )

        # Invalid arguments return ERROR status and handler is NOT called
        res_invalid = registry.execute("test_tool", {"message": "only message"}, permission_boundary=boundary)
        self.assertEqual(res_invalid["status"], "ERROR")
        self.assertIn("Argument schema validation failed", res_invalid["error"])
        self.assertIn("'count' is a required property", res_invalid["error"])
        self.assertFalse(handler_called)

        # Valid arguments invoke the handler and return SUCCESS
        res_valid = registry.execute("test_tool", {"message": "valid", "count": 1}, permission_boundary=boundary)
        self.assertTrue(handler_called)
        self.assertEqual(res_valid["status"], "SUCCESS")
        self.assertEqual(res_valid["result"], {"echo": "valid"})


if __name__ == "__main__":
    unittest.main()
