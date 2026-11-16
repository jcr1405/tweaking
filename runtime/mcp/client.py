"""MCP Client Session Manager
Manages a dedicated background thread running an asynchronous MCP stdio client session,
enforcing Windows Job Object subprocess containment, environment variable isolation,
and thread-safe synchronous invocation.
"""

import os
import sys
import time
import psutil
import logging
import asyncio
import threading
from typing import Optional, Dict, Any, List
from unittest.mock import patch

from mcp.client.stdio import stdio_client, StdioServerParameters
import mcp.client.stdio as mcp_stdio_mod
from mcp import ClientSession

from runtime.mcp.models import MCPServerConfig, MCPServerState, MCPToolInfo, MCPServerStatus
from runtime.platform_win32 import Win32JobObject

logger = logging.getLogger("presence.mcp.client")

# ==============================================================================
# Portions of the configuration override security policy are adapted from Goose:
# https://github.com/aaif-goose/goose
# Copyright (c) 2024-2025 Block, Inc. / AAIF
# Licensed under the Apache License, Version 2.0
# ==============================================================================

# Policy A: Disallowed environment variable overrides (Configuration Override Restrictions)
# Rejects dangerous configuration overrides associated with DLL injection, binary path manipulation,
# language runtime hijacking, and profile redirection.
DISALLOWED_OVERRIDE_KEYS = frozenset({
    # Binary and executable path manipulation
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "SYSTEMROOT",
    "WINDIR",
    # DLL & dynamic library injection
    "APPINIT_DLLS",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "LD_DEBUG",
    "LD_BIND_NOW",
    "LD_ASSUME_KERNEL",
    "DYLD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_FRAMEWORK_PATH",
    # Language runtime code injection
    "PYTHONPATH",
    "PYTHONHOME",
    "NODE_OPTIONS",
    "RUBYOPT",
    "GEM_PATH",
    "GEM_HOME",
    "CLASSPATH",
    "GO111MODULE",
    "GOROOT",
    "PERL5LIB",
    # Profile & temporary directory redirection
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "TEMP",
    "TMP",
    "SESSIONNAME",
})

# Forbidden keywords in environment variables (never pass API keys or secrets to child processes)
FORBIDDEN_ENV_KEYWORDS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "CREDENTIAL",
    "PASSWORD",
    "AUTH",
    "GEMINI",
    "GOOGLE",
    "ANTHROPIC",
    "OPENAI",
)

# Policy B: Safe environment variables allowlist for Windows child processes
# Only essential operating system environment variables are inherited from the parent.
SAFE_ENV_ALLOWLIST = frozenset({
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "COMSPEC",
    "USERNAME",
})


def validate_server_environment_overrides(
    custom_env: Optional[Dict[str, str]],
    strict: bool = False
) -> Dict[str, str]:
    """Policy A: Validates and filters configuration environment overrides.
    Rejects dangerous overrides and unapproved environment variable names.
    If strict=True, raises ValueError on the first disallowed or secret override.
    """
    if not custom_env:
        return {}

    validated: Dict[str, str] = {}
    for key, val in custom_env.items():
        key_upper = key.strip().upper()

        # Check for disallowed configuration override keys
        if key_upper in DISALLOWED_OVERRIDE_KEYS:
            msg = f"Disallowed environment variable override '{key}' (security restriction)."
            logger.warning(msg)
            if strict:
                raise ValueError(msg)
            continue

        # Check for secret / credential keyword contamination
        if any(kw in key_upper for kw in FORBIDDEN_ENV_KEYWORDS):
            msg = f"Blocked secret environment variable override '{key}' from MCP server configuration."
            logger.warning(msg)
            if strict:
                raise ValueError(msg)
            continue

        validated[key] = str(val)

    return validated


def sanitize_search_path(raw_path: Optional[str] = None) -> str:
    """Constructs a clean, safe PATH without relative directories or empty segments."""
    source_path = raw_path if raw_path is not None else os.environ.get("PATH", "")
    safe_segments = []
    for segment in source_path.split(os.pathsep):
        s = segment.strip().strip('"')
        if not s or s == "." or s == "..":
            continue
        # Only retain valid directory paths
        if os.path.isabs(s):
            safe_segments.append(s)
    return os.pathsep.join(safe_segments)


def build_isolated_environment(
    custom_env: Optional[Dict[str, str]] = None,
    server_id: Optional[str] = None
) -> Dict[str, str]:
    """Policy B: Constructs an explicit, clean child environment dictionary.
    Strictly excludes Gemini/provider secrets, parent environment noise, and dangerous overrides.
    Preserves essential Windows runtime variables and merges validated overrides.
    """
    isolated: Dict[str, str] = {}

    # 1. Inherit only essential safe Windows operating system variables
    for key, val in os.environ.items():
        key_upper = key.upper()
        if key_upper in SAFE_ENV_ALLOWLIST:
            # Check for accidental secret injection in parent environment
            if not any(kw in key_upper for kw in FORBIDDEN_ENV_KEYWORDS):
                if key_upper == "PATH":
                    isolated[key] = sanitize_search_path(val)
                else:
                    isolated[key] = val

    # 2. Guarantee critical Windows variables exist even if missing from parent os.environ
    if "SYSTEMROOT" not in {k.upper() for k in isolated} and sys.platform == "win32":
        isolated["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
    if "PATHEXT" not in {k.upper() for k in isolated} and sys.platform == "win32":
        isolated["PATHEXT"] = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC")

    # 3. Validate and merge caller custom environment overrides (Policy A)
    validated_overrides = validate_server_environment_overrides(custom_env, strict=False)
    for k, v in validated_overrides.items():
        isolated[k] = v

    # 4. Final verification assertion: absolutely no secret keywords allowed in child environment
    scrubbed: Dict[str, str] = {}
    for k, v in isolated.items():
        k_upper = k.upper()
        if any(kw in k_upper for kw in FORBIDDEN_ENV_KEYWORDS):
            logger.error(f"Critical security violation: key '{k}' contained forbidden keyword during child env build.")
            continue
        scrubbed[k] = v

    return scrubbed



class MCPClientSession:
    """Synchronous wrapper around an asynchronous MCP stdio client running in a worker thread."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.state = MCPServerState.STOPPED
        self.pid: Optional[int] = None
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.protocol_version: Optional[str] = None
        self.last_error: Optional[str] = None

        self._job: Optional[Win32JobObject] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[ClientSession] = None
        self._ready_event = threading.Event()
        self._stop_event: Optional[asyncio.Event] = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Starts the MCP server subprocess inside a Win32 Job Object and initializes protocol."""
        with self._lock:
            if self.state in (MCPServerState.READY, MCPServerState.INITIALIZING, MCPServerState.STARTING):
                logger.warning(f"MCP server '{self.config.server_id}' is already {self.state.value}.")
                return True

            self.state = MCPServerState.STARTING
            self.last_error = None
            self._ready_event.clear()

            # Create dedicated Job Object for process tree containment
            job_name = f"Presence_MCP_{self.config.server_id}_{int(time.time())}"
            self._job = Win32JobObject(name=job_name)
            if not self._job.create(kill_on_close=True):
                err = f"Failed to create Win32 Job Object for MCP server '{self.config.server_id}'"
                logger.error(err)
                self.state = MCPServerState.FAILED
                self.last_error = err
                return False

            # Create worker thread and event loop
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop,
                name=f"MCP-{self.config.server_id}",
                daemon=True
            )
            self._thread.start()

        # Wait for initialization to complete
        startup_timeout = self.config.startup_timeout
        if not self._ready_event.wait(timeout=startup_timeout):
            with self._lock:
                err = f"Startup timed out after {startup_timeout}s for MCP server '{self.config.server_id}'"
                logger.error(err)
                self.state = MCPServerState.FAILED
                self.last_error = err
                self._terminate_subprocesses()
                return False

        with self._lock:
            return self.state == MCPServerState.READY

    def _run_loop(self) -> None:
        """Background thread target that manages the asyncio event loop."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_session_runner())
        except BaseException as e:
            if not isinstance(e, (KeyboardInterrupt, SystemExit)):
                logger.error(f"MCP worker loop encountered error for '{self.config.server_id}': {e}", exc_info=True)
            with self._lock:
                self.state = MCPServerState.FAILED
                self.last_error = str(e)
        finally:
            with self._lock:
                if self.state != MCPServerState.STOPPED:
                    self.state = MCPServerState.STOPPED
                self._cleanup_loop()

    async def _async_session_runner(self) -> None:
        """Asynchronous worker task: runs stdio transport and ClientSession lifecycle."""
        self._stop_event = asyncio.Event()

        # Build isolated environment
        isolated_env = build_isolated_environment(self.config.env)

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=isolated_env,
            cwd=self.config.cwd
        )

        # Hook process creation to capture PID and assign to Win32 Job Object
        orig_create = mcp_stdio_mod._create_platform_compatible_process

        async def hook_create_process(*args, **kwargs):
            proc = await orig_create(*args, **kwargs)
            self.pid = proc.pid
            logger.info(f"Spawned MCP server '{self.config.server_id}' (PID: {self.pid})")
            if self._job:
                assigned = self._job.assign_process(self.pid)
                if assigned:
                    logger.debug(f"Assigned PID {self.pid} to Job Object '{self._job.name}'")
                else:
                    logger.warning(f"Could not assign PID {self.pid} to Job Object '{self._job.name}'")
            return proc

        try:
            with patch.object(mcp_stdio_mod, "_create_platform_compatible_process", hook_create_process):
                async with stdio_client(params) as (read_stream, write_stream):
                    with self._lock:
                        self.state = MCPServerState.INITIALIZING

                    async with ClientSession(read_stream, write_stream) as session:
                        init_result = await session.initialize()
                        with self._lock:
                            self._session = session
                            self.protocol_version = str(getattr(session, "protocol_version", "2024-11-05"))
                            if hasattr(init_result, "server_info"):
                                sinfo = init_result.server_info
                                self.server_info = {
                                    "name": getattr(sinfo, "name", ""),
                                    "version": getattr(sinfo, "version", "")
                                }
                            if hasattr(init_result, "capabilities"):
                                caps = init_result.capabilities
                                self.capabilities = caps.model_dump() if hasattr(caps, "model_dump") else {}

                            self.state = MCPServerState.READY
                            logger.info(f"MCP server '{self.config.server_id}' successfully INITIALIZED and READY.")
                            self._ready_event.set()

                        # Keep session active until stop is signaled
                        await self._stop_event.wait()
                        logger.debug(f"Stop signal received for MCP server '{self.config.server_id}'")

        except BaseException as e:
            err_msg = f"MCP session error in '{self.config.server_id}': {str(e)}"
            if not isinstance(e, (KeyboardInterrupt, SystemExit)):
                logger.error(err_msg)
            with self._lock:
                self.state = MCPServerState.FAILED
                self.last_error = err_msg
                self._ready_event.set()
        finally:
            with self._lock:
                self._session = None

    def list_tools(self) -> List[MCPToolInfo]:
        """Discovers tools exposed by the MCP server."""
        with self._lock:
            if self.state != MCPServerState.READY or not self._session or not self._loop:
                logger.warning(f"Cannot list tools: MCP server '{self.config.server_id}' is in state {self.state.value}")
                return []

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.list_tools(),
                self._loop
            )
            result = future.result(timeout=self.config.request_timeout)

            tool_list: List[MCPToolInfo] = []
            for t in getattr(result, "tools", []):
                schema = getattr(t, "input_schema", {})
                if hasattr(schema, "model_dump"):
                    schema = schema.model_dump()
                elif not isinstance(schema, dict):
                    schema = {}

                tool_list.append(MCPToolInfo(
                    server_id=self.config.server_id,
                    tool_name=t.name,
                    description=t.description or "",
                    input_schema=schema
                ))
            return tool_list
        except Exception as e:
            logger.error(f"Failed to list tools from MCP server '{self.config.server_id}': {e}")
            with self._lock:
                self.last_error = str(e)
            return []

    def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """Invokes a tool on the MCP server and returns a normalized result dictionary."""
        effective_timeout = timeout or self.config.request_timeout

        with self._lock:
            if self.state != MCPServerState.READY or not self._session or not self._loop:
                return {
                    "status": "ERROR",
                    "error": f"MCP server '{self.config.server_id}' is not in READY state (current: {self.state.value}).",
                    "server_id": self.config.server_id,
                    "tool": tool_name
                }

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(name=tool_name, arguments=arguments),
                self._loop
            )

            start_time = time.time()
            while not future.done():
                with self._lock:
                    is_ready = (self.state == MCPServerState.READY)
                if not is_ready or not self.is_alive():
                    future.cancel()
                    with self._lock:
                        self.state = MCPServerState.FAILED
                        self.last_error = f"MCP server '{self.config.server_id}' exited unexpectedly during tool call."
                    return {
                        "status": "ERROR",
                        "error": f"MCP server '{self.config.server_id}' exited unexpectedly during tool call '{tool_name}'.",
                        "server_id": self.config.server_id,
                        "tool": tool_name
                    }
                if time.time() - start_time > effective_timeout:
                    future.cancel()
                    raise TimeoutError(f"Tool call '{tool_name}' timed out after {effective_timeout}s")
                time.sleep(0.05)

            call_res = future.result()

            # Parse results
            is_error = getattr(call_res, "is_error", False)
            content_items = getattr(call_res, "content", [])
            output_parts = []

            for item in content_items:
                if hasattr(item, "text"):
                    output_parts.append(item.text)
                elif hasattr(item, "data"):
                    output_parts.append(str(item.data))
                else:
                    output_parts.append(str(item))

            joined_output = "\n".join(output_parts) if output_parts else ""

            if is_error:
                return {
                    "status": "ERROR",
                    "error": joined_output or "MCP tool indicated error without message.",
                    "server_id": self.config.server_id,
                    "tool": tool_name
                }

            return {
                "status": "SUCCESS",
                "result": joined_output,
                "server_id": self.config.server_id,
                "tool": tool_name
            }

        except TimeoutError:
            err = f"Tool call '{tool_name}' on '{self.config.server_id}' timed out after {effective_timeout}s"
            logger.error(err)
            return {"status": "ERROR", "error": err, "server_id": self.config.server_id, "tool": tool_name}
        except Exception as e:
            err = f"Execution error in MCP tool '{tool_name}' on '{self.config.server_id}': {e}"
            logger.error(err)
            with self._lock:
                self.last_error = str(e)
            return {"status": "ERROR", "error": err, "server_id": self.config.server_id, "tool": tool_name}

    def stop(self) -> bool:
        """Gracefully shuts down the MCP session and ensures process tree termination."""
        with self._lock:
            if self.state in (MCPServerState.STOPPED, MCPServerState.STOPPING):
                return True
            self.state = MCPServerState.STOPPING

        logger.info(f"Stopping MCP server '{self.config.server_id}'...")

        # Signal stop event in async loop
        if self._loop and self._loop.is_running() and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

        # Wait for thread to exit cleanly
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.config.shutdown_timeout)

        # Force terminate if still running
        self._terminate_subprocesses()

        with self._lock:
            self.state = MCPServerState.STOPPED
            self.pid = None
            self._session = None
            self._cleanup_loop()

        logger.info(f"MCP server '{self.config.server_id}' stopped.")
        return True

    def _terminate_subprocesses(self) -> None:
        """Terminates processes via Win32 Job Object and closes handle."""
        if self._job:
            try:
                self._job.terminate(exit_code=0)
            except Exception as e:
                logger.warning(f"Error terminating Job Object for '{self.config.server_id}': {e}")
            try:
                self._job.close()
            except Exception as e:
                logger.warning(f"Error closing Job Object for '{self.config.server_id}': {e}")
            self._job = None

    def _cleanup_loop(self) -> None:
        """Closes event loop safely."""
        if self._loop and not self._loop.is_running():
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None

    def is_alive(self) -> bool:
        """Checks if the subprocess is currently running."""
        if not self.pid:
            return False
        try:
            proc = psutil.Process(self.pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def get_memory_mb(self) -> float:
        """Returns process working set memory in MB."""
        if not self.pid:
            return 0.0
        try:
            proc = psutil.Process(self.pid)
            return proc.memory_info().rss / (1024 * 1024)
        except Exception:
            return 0.0

    def get_status(self) -> MCPServerStatus:
        """Returns current health and diagnostic status."""
        alive = self.is_alive()
        current_state = self.state
        if not alive and current_state == MCPServerState.READY:
            current_state = MCPServerState.FAILED

        return MCPServerStatus(
            server_id=self.config.server_id,
            state=current_state,
            pid=self.pid if alive else None,
            restarts=0,
            tools_count=0,
            memory_mb=self.get_memory_mb() if alive else 0.0,
            last_error=self.last_error,
            protocol_version=self.protocol_version
        )
