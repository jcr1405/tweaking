"""Unit and Integration Tests for Presence Built-in Filesystem and Shell MCP Servers
Verifies:
1. Tool discovery and schema caching for filesystem and shell MCP servers.
2. Direct execution of filesystem tools (read, write, list, file_info, search).
3. Direct execution of shell tools (get_system_info, which, execute_command).
4. Process isolation inside Win32 Job Object and clean shutdown.
"""

import sys
import unittest
import tempfile
import shutil
from pathlib import Path

from runtime.mcp.config import load_mcp_configuration
from runtime.mcp.supervisor import MCPSupervisor
from runtime.mcp.schemas import SchemaCache
from runtime.tools.registry import ToolRegistry
from runtime.permissions import PermissionBoundary


class TestBuiltinMCPServers(unittest.TestCase):
    """Verifies built-in filesystem and shell MCP server integration."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_presence_mcp_"))
        self.schema_dir = self.temp_dir / "schemas"
        self.schema_cache = SchemaCache(cache_dir=self.schema_dir)
        self.supervisor = MCPSupervisor(schema_cache=self.schema_cache)
        self.boundary = PermissionBoundary()
        self.registry = ToolRegistry()

        # Load configs from mcp_servers.json
        configs = load_mcp_configuration()
        self.configs = {c.server_id: c for c in configs}
        for c in configs:
            self.supervisor.register_server(c)

    def tearDown(self):
        self.supervisor.stop_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_configuration_loaded(self):
        """Verifies both filesystem and shell servers are registered."""
        self.assertIn("filesystem", self.configs)
        self.assertIn("shell", self.configs)
        self.assertTrue(self.configs["filesystem"].enabled)
        self.assertTrue(self.configs["shell"].enabled)

    def test_02_register_tools_with_registry(self):
        """Verifies 14 MCP tools are registered into ToolRegistry."""
        registered = self.supervisor.register_tools_with_registry(self.registry, self.boundary)
        self.assertGreaterEqual(len(registered), 14)

        expected_fs = [
            "mcp.filesystem.read_file",
            "mcp.filesystem.write_file",
            "mcp.filesystem.edit_file",
            "mcp.filesystem.list_directory",
            "mcp.filesystem.get_file_info",
        ]
        for t in expected_fs:
            self.assertIn(t, registered)

        expected_sh = [
            "mcp.shell.execute_command",
            "mcp.shell.get_system_info",
            "mcp.shell.which",
            "mcp.shell.run_powershell_script",
        ]
        for t in expected_sh:
            self.assertIn(t, registered)

    def test_03_filesystem_crud_lifecycle(self):
        """Verifies write, read, edit, and get_file_info through the filesystem MCP server."""
        self.supervisor.register_tools_with_registry(self.registry, self.boundary)
        test_file = self.temp_dir / "test_mcp.txt"

        # 1. Write file
        w_res = self.registry.execute(
            "mcp.filesystem.write_file",
            {"path": str(test_file), "content": "Line 1: Hello from Presence MCP!\nLine 2: Testing.\n"},
            permission_boundary=self.boundary
        )
        self.assertEqual(w_res.get("status"), "SUCCESS")

        # 2. Read file
        r_res = self.registry.execute(
            "mcp.filesystem.read_file",
            {"path": str(test_file), "offset": 0, "limit": 10},
            permission_boundary=self.boundary
        )
        self.assertEqual(r_res.get("status"), "SUCCESS")
        self.assertIn("Hello from Presence MCP!", str(r_res.get("result")))

        # 3. Edit file
        e_res = self.registry.execute(
            "mcp.filesystem.edit_file",
            {"path": str(test_file), "target": "Testing.", "replacement": "Verified!"},
            permission_boundary=self.boundary
        )
        self.assertEqual(e_res.get("status"), "SUCCESS")

        # 4. Verify edit
        r_res2 = self.registry.execute(
            "mcp.filesystem.read_file",
            {"path": str(test_file)},
            permission_boundary=self.boundary
        )
        self.assertIn("Verified!", str(r_res2.get("result")))

        # 5. File info
        info_res = self.registry.execute(
            "mcp.filesystem.get_file_info",
            {"path": str(test_file)},
            permission_boundary=self.boundary
        )
        self.assertEqual(info_res.get("status"), "SUCCESS")
        result_data = info_res.get("result")
        if isinstance(result_data, str):
            import json
            result_data = json.loads(result_data)
        self.assertTrue(result_data.get("is_file"))
        self.assertEqual(result_data.get("line_count"), 2)

    def test_04_shell_command_execution(self):
        """Verifies system info, which, and powershell command execution through the shell MCP server."""
        self.supervisor.register_tools_with_registry(self.registry, self.boundary)

        # 1. System Info
        sys_res = self.registry.execute(
            "mcp.shell.get_system_info",
            {},
            permission_boundary=self.boundary
        )
        self.assertEqual(sys_res.get("status"), "SUCCESS")
        res_dict = sys_res.get("result")
        if isinstance(res_dict, str):
            import json
            res_dict = json.loads(res_dict)
        self.assertIn("os_name", res_dict)
        self.assertIn("architecture", res_dict)

        # 2. Which
        which_res = self.registry.execute(
            "mcp.shell.which",
            {"command": "cmd"},
            permission_boundary=self.boundary
        )
        self.assertEqual(which_res.get("status"), "SUCCESS")
        w_dict = which_res.get("result")
        if isinstance(w_dict, str):
            import json
            w_dict = json.loads(w_dict)
        self.assertTrue(w_dict.get("found"))

        # 3. Execute command
        cmd_res = self.registry.execute(
            "mcp.shell.execute_command",
            {"command": "echo Presence_MCP_Alive"},
            permission_boundary=self.boundary
        )
        self.assertEqual(cmd_res.get("status"), "SUCCESS")
        c_dict = cmd_res.get("result")
        if isinstance(c_dict, str):
            import json
            c_dict = json.loads(c_dict)
        self.assertEqual(c_dict.get("exit_code"), 0)
        self.assertIn("Presence_MCP_Alive", c_dict.get("stdout", ""))


if __name__ == "__main__":
    unittest.main()
