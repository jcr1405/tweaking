"""Automated Unit Tests for Tool Registry & Permission Gate Integration"""

import unittest
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import ToolDefinition, ToolRegistry, build_default_tools


class TestToolRegistry(unittest.TestCase):
    """Validates ToolRegistry tool registration, schema generation, and permission mediation."""

    def setUp(self):
        self.boundary = PermissionBoundary()
        self.registry = ToolRegistry()

    def test_registration_and_declarations(self):
        tool = ToolDefinition(
            tool_id="math.add",
            name="add",
            description="Adds two numbers.",
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"}
                },
                "required": ["a", "b"]
            },
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda args: {"sum": args["a"] + args["b"]}
        )
        self.registry.register(tool)
        self.assertIsNotNone(self.registry.get_tool("math.add"))

        declarations = self.registry.get_declarations()
        self.assertEqual(len(declarations), 1)
        self.assertEqual(declarations[0]["name"], "math.add")
        self.assertEqual(declarations[0]["description"], "Adds two numbers.")

    def test_successful_l0_tool_execution(self):
        tool = ToolDefinition(
            tool_id="test.echo",
            name="echo",
            description="Echoes input.",
            parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda args: {"echo": args.get("msg", "")}
        )
        self.registry.register(tool)

        res = self.registry.execute(
            tool_id="test.echo",
            args={"msg": "Hello Presence"},
            permission_boundary=self.boundary
        )
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["result"]["echo"], "Hello Presence")

    def test_unknown_tool_rejection_fail_closed(self):
        res = self.registry.execute(
            tool_id="unregistered.malicious_tool",
            args={},
            permission_boundary=self.boundary
        )
        self.assertEqual(res["status"], "ERROR")
        self.assertIn("not registered", res["error"])

    def test_l3_tool_blocked_without_consent(self):
        tool = ToolDefinition(
            tool_id="system.format_drive",
            name="format_drive",
            description="Protected dangerous operation.",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L3_PROTECTED,
            handler=lambda args: {"formatted": True}
        )
        self.registry.register(tool)

        res = self.registry.execute(
            tool_id="system.format_drive",
            args={},
            permission_boundary=self.boundary
        )
        self.assertEqual(res["status"], "ERROR")
        self.assertIn("blocked by security gate", res["error"])

    def test_l3_tool_permitted_with_valid_consent(self):
        # Initialize boundary with dynamic consent provider that allows test action
        boundary_with_consent = PermissionBoundary(
            consent_provider=lambda action, ctx: True
        )

        tool = ToolDefinition(
            tool_id="system.format_drive",
            name="format_drive",
            description="Protected dangerous operation.",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L3_PROTECTED,
            handler=lambda args: {"formatted": True}
        )
        self.registry.register(tool)

        res = self.registry.execute(
            tool_id="system.format_drive",
            args={},
            permission_boundary=boundary_with_consent
        )
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["result"]["formatted"])

    def test_built_in_default_tools(self):
        def fake_status():
            return {"state": "RUNNING", "pid": 1234, "working_set_mb": 22.5}

        def fake_conv_info():
            return {"conversation_id": "conv-1", "title": "Test Conv", "step_count": 4}

        def fake_search(query, limit):
            return [{"conversation_id": "conv-1", "snippet": "matched text"}]

        registry = build_default_tools(
            runtime_status_fn=fake_status,
            conversation_info_fn=fake_conv_info,
            search_conversations_fn=fake_search
        )
        self.assertEqual(len(registry.list_tools()), 3)

        # Test presence.get_runtime_status
        res1 = registry.execute("presence.get_runtime_status", {}, self.boundary)
        self.assertEqual(res1["status"], "SUCCESS")
        self.assertEqual(res1["result"]["state"], "RUNNING")

        # Test presence.search_conversations
        res2 = registry.execute("presence.search_conversations", {"query": "test"}, self.boundary)
        self.assertEqual(res2["status"], "SUCCESS")
        self.assertEqual(len(res2["result"]), 1)


if __name__ == "__main__":
    unittest.main()
