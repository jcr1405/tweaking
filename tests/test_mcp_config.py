"""Unit Tests for Persistent MCP Server Configuration Boundary (runtime/mcp/config.py)
Verifies:
1. Versioned schema enforcement ("1.0").
2. Fail-closed behavior on missing configuration.
3. Default-deny tool approvals (auto_approve_tools=False).
4. Command and args parameter separation.
5. Environment variable scrubbing via security policy.
6. Local authority tier overrides.
"""

import os
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.permissions import PermissionTier
from runtime.mcp.models import MCPServerConfig
from runtime.mcp.config import (
    load_mcp_configuration,
    save_mcp_configuration,
    parse_mcp_server_config,
    get_default_mcp_config_path,
    CURRENT_CONFIG_VERSION,
)


class TestMCPConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_mcp_config_"))
        self.config_path = self.temp_dir / "mcp_servers.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_missing_config_fails_closed(self):
        """Missing configuration file returns empty list without raising."""
        nonexistent = self.temp_dir / "does_not_exist.json"
        configs = load_mcp_configuration(nonexistent)
        self.assertEqual(configs, [])

    def test_malformed_json_raises_value_error(self):
        """Malformed JSON content raises ValueError."""
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("{ invalid json [}")

        with self.assertRaises(ValueError) as ctx:
            load_mcp_configuration(self.config_path)
        self.assertIn("Malformed JSON", str(ctx.exception))

    def test_unsupported_version_rejected(self):
        """Configurations with version != '1.0' are rejected."""
        payload = {
            "version": "2.0",
            "servers": []
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        with self.assertRaises(ValueError) as ctx:
            load_mcp_configuration(self.config_path)
        self.assertIn("Unsupported MCP configuration version", str(ctx.exception))

    def test_valid_configuration_round_trip(self):
        """Valid server configurations serialize and deserialize accurately."""
        original = [
            MCPServerConfig(
                server_id="echo_service",
                name="Echo MCP Service",
                command="python",
                args=["-m", "echo"],
                cwd="C:\\tools",
                env={"MY_CUSTOM_VAR": "val123"},
                default_authority_tier=PermissionTier.L0_OBSERVE,
                tool_tiers={"echo": PermissionTier.L0_OBSERVE},
                approved_tools=["echo"],
                auto_approve_tools=False
            )
        ]
        save_mcp_configuration(original, self.config_path)
        self.assertTrue(self.config_path.exists())

        loaded = load_mcp_configuration(self.config_path)
        self.assertEqual(len(loaded), 1)
        cfg = loaded[0]
        self.assertEqual(cfg.server_id, "echo_service")
        self.assertEqual(cfg.name, "Echo MCP Service")
        self.assertEqual(cfg.command, "python")
        self.assertEqual(cfg.args, ["-m", "echo"])
        self.assertEqual(cfg.cwd, "C:\\tools")
        self.assertEqual(cfg.default_authority_tier, PermissionTier.L0_OBSERVE)
        self.assertEqual(cfg.approved_tools, ["echo"])
        self.assertFalse(cfg.auto_approve_tools)

    def test_default_deny_tool_approval(self):
        """Omitted tool approval parameters default to strict deny (False)."""
        raw = {
            "server_id": "safe_server",
            "command": "node",
            "args": ["server.js"]
        }
        cfg = parse_mcp_server_config(raw)
        self.assertFalse(cfg.auto_approve_tools)
        self.assertIsNone(cfg.approved_tools)
        self.assertEqual(cfg.default_authority_tier, PermissionTier.L0_OBSERVE)

    def test_server_id_validation(self):
        """Invalid characters in server_id are strictly rejected."""
        invalid_ids = ["bad/id", "id with spaces", "server:name", "../traversal"]
        for bad_id in invalid_ids:
            with self.assertRaises(ValueError):
                parse_mcp_server_config({
                    "server_id": bad_id,
                    "command": "python"
                })

    def test_environment_override_sanitization(self):
        """Dangerous environment variable overrides and secrets are scrubbed."""
        raw = {
            "server_id": "test_env",
            "command": "python",
            "env": {
                "COMSPEC": "C:\\malicious.exe",
                "API_KEY": "secret123",
                "SAFE_SETTING": "enabled_123"
            }
        }
        cfg = parse_mcp_server_config(raw)
        # COMSPEC is in blocked keys; API_KEY is blocked as a secret; SAFE_SETTING is retained
        self.assertNotIn("COMSPEC", cfg.env)
        self.assertNotIn("API_KEY", cfg.env)
        self.assertEqual(cfg.env.get("SAFE_SETTING"), "enabled_123")


if __name__ == "__main__":
    unittest.main()
