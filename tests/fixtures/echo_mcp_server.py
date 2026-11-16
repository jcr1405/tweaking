"""Test MCP Server Fixture
Lightweight local MCP server used for automated test suites.
Implements standard tools, large payload generation, and crash simulation.
"""

import sys
import os
from mcp.server.mcpserver import MCPServer

server = MCPServer("test_fixture_server")


@server.tool()
def echo(message: str) -> str:
    """Echoes the input message back."""
    return f"ECHO: {message}"


@server.tool()
def generate_large_payload(line_count: int = 1500) -> str:
    """Generates a large text payload exceeding the 40 KB threshold for testing artifact offload."""
    lines = [f"Line {i:05d}: Presence MCP automated test data payload line with padding content." for i in range(line_count)]
    return "\n".join(lines)


@server.tool()
def get_env_var(var_name: str) -> str:
    """Returns the value of an environment variable as seen inside the MCP child process."""
    return os.environ.get(var_name, "<NOT_SET>")


@server.tool()
def trigger_crash() -> str:
    """Simulates an unhandled process crash."""
    os._exit(42)


if __name__ == "__main__":
    server.run("stdio")
