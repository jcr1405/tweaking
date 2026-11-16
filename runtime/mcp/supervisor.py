"""MCP Process Supervisor
Manages lifecycle, process-tree containment, lazy schema hydration, fault isolation,
exponential restart backoff, and ToolRegistry registration for all configured MCP servers.
"""

import time
import logging
import threading
from typing import Optional, Dict, Any, List, Callable
from pathlib import Path

from runtime.mcp.models import (
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPServerStatus
)
from runtime.mcp.client import MCPClientSession
from runtime.mcp.schemas import SchemaCache
from runtime.permissions import PermissionBoundary, PermissionTier, ActionPolicy
from runtime.tools.registry import ToolRegistry, ToolDefinition

logger = logging.getLogger("presence.mcp.supervisor")


class MCPSupervisor:
    """Supervises external MCP server processes, enforces security boundaries,
    and bridges MCP tools into Presence's native ToolRegistry.
    """

    def __init__(self, schema_cache: Optional[SchemaCache] = None):
        self.schema_cache = schema_cache or SchemaCache()
        self._configs: Dict[str, MCPServerConfig] = {}
        self._sessions: Dict[str, MCPClientSession] = {}
        self._restart_counts: Dict[str, int] = {}
        self._discovered_tools: Dict[str, Dict[str, MCPToolInfo]] = {}
        self._server_locks: Dict[str, threading.RLock] = {}
        self._global_lock = threading.Lock()

    def _get_server_lock(self, server_id: str) -> threading.RLock:
        """Returns or creates a re-entrant lock for the specified server."""
        with self._global_lock:
            if server_id not in self._server_locks:
                self._server_locks[server_id] = threading.RLock()
            return self._server_locks[server_id]

    def register_server(self, config: MCPServerConfig) -> None:
        """Registers a server configuration with the supervisor."""
        self._configs[config.server_id] = config
        self._restart_counts[config.server_id] = 0
        if config.server_id not in self._discovered_tools:
            self._discovered_tools[config.server_id] = {}

        # Pre-load cached schemas if available for lazy hydration
        cached_tools = self.schema_cache.load_server_schemas(config.server_id)
        for t in cached_tools:
            t.approved = config.is_tool_approved(t.tool_name)
            self._discovered_tools[config.server_id][t.tool_name] = t

        logger.info(f"Registered MCP server configuration '{config.server_id}' ({config.name}). Cached tools: {len(cached_tools)}")

    def unregister_server(self, server_id: str) -> bool:
        """Stops and unregisters an MCP server."""
        if server_id in self._sessions:
            self.stop_server(server_id)
            del self._sessions[server_id]
        if server_id in self._configs:
            del self._configs[server_id]
        if server_id in self._restart_counts:
            del self._restart_counts[server_id]
        if server_id in self._discovered_tools:
            del self._discovered_tools[server_id]
        logger.info(f"Unregistered MCP server '{server_id}'")
        return True

    def get_server_config(self, server_id: str) -> Optional[MCPServerConfig]:
        """Retrieves server configuration."""
        return self._configs.get(server_id)

    def start_server(self, server_id: str) -> bool:
        """Starts an MCP server process, discovers its tools, and caches schemas."""
        with self._get_server_lock(server_id):
            config = self._configs.get(server_id)
            if not config:
                logger.error(f"Cannot start unknown MCP server '{server_id}'")
                return False

            if not config.enabled:
                logger.warning(f"MCP server '{server_id}' is disabled in configuration.")
                return False

            # If already running, return True
            session = self._sessions.get(server_id)
            if session and session.state == MCPServerState.READY and session.is_alive():
                return True

            # Stop any stale session
            if session:
                session.stop()

            logger.info(f"Starting MCP server '{server_id}'...")
            new_session = MCPClientSession(config)
            self._sessions[server_id] = new_session

            success = new_session.start()
            if not success:
                logger.error(f"Failed to start MCP server '{server_id}' (State: {new_session.state.value})")
                return False

            # Discover and cache tools
            tools = new_session.list_tools()
            for t in tools:
                t.approved = config.is_tool_approved(t.tool_name)
            self._discovered_tools[server_id] = {t.tool_name: t for t in tools}
            self.schema_cache.save_server_schemas(server_id, tools)
            logger.info(f"MCP server '{server_id}' started successfully with {len(tools)} tool(s).")
            return True

    def stop_server(self, server_id: str) -> bool:
        """Gracefully stops an MCP server process tree."""
        with self._get_server_lock(server_id):
            session = self._sessions.get(server_id)
            if not session:
                return True
            logger.info(f"Stopping MCP server '{server_id}'...")
            result = session.stop()
            return result

    def restart_server(self, server_id: str) -> bool:
        """Restarts an MCP server with backoff tracking."""
        with self._get_server_lock(server_id):
            config = self._configs.get(server_id)
            if not config:
                return False

            restarts = self._restart_counts.get(server_id, 0)
            if restarts >= config.max_restarts:
                logger.error(f"MCP server '{server_id}' reached maximum restart limit ({config.max_restarts}). Marking FAILED.")
                session = self._sessions.get(server_id)
                if session:
                    session.state = MCPServerState.FAILED
                return False

            # Exponential backoff
            backoff = config.restart_backoff_seconds * (2 ** restarts)
            logger.warning(f"Restarting MCP server '{server_id}' (attempt {restarts + 1}/{config.max_restarts}, backoff: {backoff:.1f}s)...")
            time.sleep(backoff)

            self.stop_server(server_id)
            self._restart_counts[server_id] = restarts + 1

            success = self.start_server(server_id)
            return success

    def start_all(self) -> None:
        """Starts all enabled servers."""
        for server_id, config in self._configs.items():
            if config.enabled:
                self.start_server(server_id)

    def stop_all(self) -> None:
        """Stops all active servers."""
        for server_id in list(self._sessions.keys()):
            self.stop_server(server_id)

    def execute_tool(self, server_id: str, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Executes a tool on an MCP server with automatic lazy startup, crash recovery, and approval checks."""
        with self._get_server_lock(server_id):
            config = self._configs.get(server_id)
            if not config:
                return {"status": "ERROR", "error": f"MCP server '{server_id}' is not configured."}

            # Check explicit tool approval policy (default-deny)
            if not config.is_tool_approved(tool_name):
                logger.warning(f"Rejected execution of unapproved MCP tool '{tool_name}' on server '{server_id}' (default-deny).")
                return {
                    "status": "ERROR",
                    "error": f"Tool '{tool_name}' on MCP server '{server_id}' is unapproved (default-deny policy).",
                    "server_id": server_id,
                    "tool": tool_name
                }

            # Lazy startup if not currently running
            session = self._sessions.get(server_id)
            if not session or not session.is_alive() or session.state != MCPServerState.READY:
                logger.info(f"Lazy-starting MCP server '{server_id}' for tool call '{tool_name}'...")
                started = self.start_server(server_id)
                if not started:
                    return {
                        "status": "ERROR",
                        "error": f"Failed to start MCP server '{server_id}' to execute tool '{tool_name}'."
                    }
                session = self._sessions.get(server_id)

            # Execute call
            result = session.call_tool(tool_name, args, timeout=config.request_timeout)

            # Check for crash during execution
            if result.get("status") == "ERROR" and (not session.is_alive() or session.state != MCPServerState.READY):
                logger.warning(f"MCP server '{server_id}' crashed or exited during execution of tool '{tool_name}'.")
                # Restart server in background to restore availability for subsequent calls
                self.restart_server(server_id)
                # Fail closed: Do NOT blindly retry non-idempotent tool mutations on crash
                return {
                    "status": "ERROR",
                    "error": f"MCP server '{server_id}' crashed or terminated during execution of tool '{tool_name}'. Tool execution was NOT replayed to prevent non-idempotent mutation hazards.",
                    "server_id": server_id,
                    "tool": tool_name,
                    "server_crashed": True
                }

            return result

    def register_tools_with_registry(
        self,
        tool_registry: ToolRegistry,
        permission_boundary: PermissionBoundary,
        server_id: Optional[str] = None
    ) -> List[str]:
        """Bridges MCP tools into Presence's native ToolRegistry under the 'mcp.<server_id>.<tool_name>' namespace."""
        registered_names: List[str] = []
        target_servers = [server_id] if server_id else list(self._configs.keys())

        for sid in target_servers:
            config = self._configs.get(sid)
            if not config or not config.enabled:
                continue

            tools_map = self._discovered_tools.get(sid, {})
            # If no tools discovered yet, try loading from schema cache
            if not tools_map:
                cached = self.schema_cache.load_server_schemas(sid)
                for c in cached:
                    tools_map[c.tool_name] = c
                self._discovered_tools[sid] = tools_map

            # If still no tools discovered, start server to discover and cache them
            if not tools_map:
                self.start_server(sid)
                tools_map = self._discovered_tools.get(sid, {})

            for t_name, t_info in tools_map.items():
                if not config.is_tool_approved(t_name):
                    logger.info(f"Skipping unapproved MCP tool '{t_name}' on server '{sid}' (default-deny policy).")
                    continue

                t_info.approved = True
                namespaced_id = t_info.namespaced_name
                tier = config.tool_tiers.get(t_name, config.default_authority_tier)

                # Create closure handler
                def make_handler(target_sid: str, target_tname: str):
                    def _handler(args):
                        mcp_res = self.execute_tool(target_sid, target_tname, args)
                        if mcp_res.get("status") == "SUCCESS":
                            return mcp_res.get("result")
                        raise RuntimeError(mcp_res.get("error", "MCP tool execution failed"))
                    return _handler

                tool_def = ToolDefinition(
                    tool_id=namespaced_id,
                    name=namespaced_id,
                    description=t_info.description or f"MCP tool '{t_name}' provided by server '{sid}'",
                    parameters=t_info.input_schema if t_info.input_schema else {"type": "object", "properties": {}},
                    authority_tier=tier,
                    handler=make_handler(sid, t_name),
                    timeout_seconds=config.request_timeout
                )

                tool_registry.register(tool_def, permission_boundary=permission_boundary)
                registered_names.append(namespaced_id)
                logger.info(f"Bridged MCP tool '{namespaced_id}' into ToolRegistry (Tier: {tier.name})")

        return registered_names

    def get_status(self, server_id: str) -> MCPServerStatus:
        """Returns diagnostic status of a server."""
        config = self._configs.get(server_id)
        if not config:
            return MCPServerStatus(server_id=server_id, state=MCPServerState.STOPPED, last_error="Unconfigured")

        session = self._sessions.get(server_id)
        restarts = self._restart_counts.get(server_id, 0)
        tools_count = len(self._discovered_tools.get(server_id, {}))

        if not session:
            return MCPServerStatus(
                server_id=server_id,
                state=MCPServerState.STOPPED,
                restarts=restarts,
                tools_count=tools_count
            )

        status = session.get_status()
        status.restarts = restarts
        status.tools_count = tools_count
        return status

    def get_all_statuses(self) -> List[MCPServerStatus]:
        """Returns diagnostic status for all configured servers."""
        return [self.get_status(sid) for sid in self._configs.keys()]
