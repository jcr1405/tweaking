"""Presence Windows Shell Model Context Protocol (MCP) Server
Provides controlled, supervised command execution, script evaluation, and environment
diagnostics on Windows for Presence agents.
Backed by the authoritative ProcessManager and Windows Job Object containment.
Implements stdio transport via the official MCP 2.x SDK (mcp.server.mcpserver.MCPServer).
"""

import os
import sys
import time
import shutil
import platform
from typing import Optional, Dict, Any, List

from mcp.server.mcpserver import MCPServer
from runtime.process_manager import ProcessManager

# Initialize MCP Server for Shell
server = MCPServer("shell")

# Singleton ProcessManager instance for the shell server
_process_manager = ProcessManager()

MAX_OUTPUT_BYTES = 256 * 1024  # 256 KB output ceiling


def _truncate_output(text: str, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    """Safely bounds output strings to prevent memory or context saturation."""
    raw_bytes = text.encode("utf-8", errors="replace")
    if len(raw_bytes) > max_bytes:
        truncated = raw_bytes[:max_bytes].decode("utf-8", errors="ignore")
        return truncated + f"\n\n[... Output truncated: exceeded {max_bytes // 1024} KB limit ...]"
    return text


@server.tool()
def execute_command(
    command: str,
    cwd: Optional[str] = None,
    timeout: float = 30.0,
    wait_ms: int = 5000,
    is_daemon: bool = False,
    shell: str = "powershell"
) -> Dict[str, Any]:
    """Executes a command on Windows under Job Object supervision with bounded wait.
    
    If the command completes within wait_ms, returns immediate synchronous stdout and stderr.
    If it continues past wait_ms, it is seamlessly transitioned to background execution and
    returns a task receipt with a task_id and log artifact references.
    
    Args:
        command: The shell command line string to execute.
        cwd: Optional working directory for command execution.
        timeout: Execution timeout ceiling in seconds (default: 30.0, max: 3600.0).
        wait_ms: Milliseconds to wait synchronously before backgrounding (default: 5000).
        is_daemon: Whether the command is an indefinite background service/daemon.
        shell: Shell interpreter: 'powershell' (default) or 'cmd'.
    """
    if not command or not command.strip():
        return {"status": "ERROR", "error": "Empty command provided."}

    effective_cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else os.getcwd()
    if not os.path.isdir(effective_cwd):
        return {"status": "ERROR", "error": f"Working directory does not exist: {effective_cwd}"}

    timeout = max(1.0, min(float(timeout), 3600.0))
    wait_ms = max(100, min(int(wait_ms), 60000))

    res = _process_manager.run_command(
        command=command,
        wait_ms=wait_ms,
        is_daemon=is_daemon,
        cwd=effective_cwd,
        timeout_seconds=timeout,
        shell=shell
    )

    # Safely bound output strings if synchronous
    if "stdout" in res and isinstance(res["stdout"], str):
        res["stdout"] = _truncate_output(res["stdout"])
    if "stderr" in res and isinstance(res["stderr"], str):
        res["stderr"] = _truncate_output(res["stderr"])

    return res


@server.tool()
def get_process_status(task_id: str) -> Dict[str, Any]:
    """Returns the live status, exit code, execution time, and log tail for a managed process.
    
    Args:
        task_id: The unique task identifier returned when the process was launched.
    """
    status = _process_manager.get_process_status(task_id.strip())
    if not status:
        return {"status": "NOT_FOUND", "error": f"No managed process with task_id '{task_id}'"}
    return status


@server.tool()
def kill_process(task_id: str, reason: str = "Terminated by user") -> Dict[str, Any]:
    """Terminates a running managed process and its entire Windows Job Object process tree.
    
    Args:
        task_id: The unique task identifier of the process to terminate.
        reason: Optional human-readable reason for termination.
    """
    success = _process_manager.kill_process(task_id.strip(), reason=reason)
    return {
        "task_id": task_id,
        "terminated": success,
        "message": f"Process {task_id} terminated." if success else f"Could not terminate process {task_id} (not found or already terminal)."
    }


@server.tool()
def send_process_input(task_id: str, input_text: str) -> Dict[str, Any]:
    """Sends text input to the standard input (stdin) of an active managed process.
    
    Args:
        task_id: The unique task identifier of the running process.
        input_text: The string to write to standard input.
    """
    success = _process_manager.send_input(task_id.strip(), input_text)
    return {
        "task_id": task_id,
        "delivered": success,
        "error": None if success else f"Process {task_id} is not running or stdin is unavailable."
    }


@server.tool()
def list_processes(include_completed: bool = True) -> Dict[str, Any]:
    """Lists all active and recently completed processes managed by Presence."""
    procs = _process_manager.list_processes(include_completed=include_completed)
    return {
        "count": len(procs),
        "processes": procs
    }


@server.tool()
def run_powershell_script(
    script: str,
    timeout: float = 60.0,
    wait_ms: int = 5000,
    is_daemon: bool = False
) -> Dict[str, Any]:
    """Executes a multi-line PowerShell script under Windows Job Object containment.
    
    Args:
        script: Multi-line PowerShell script text.
        timeout: Execution timeout ceiling in seconds (default: 60.0, max: 3600.0).
        wait_ms: Milliseconds to wait synchronously before backgrounding (default: 5000).
        is_daemon: Whether the script runs as a persistent background daemon.
    """
    if not script or not script.strip():
        return {"status": "ERROR", "error": "Empty script provided."}

    return execute_command(
        command=script,
        timeout=timeout,
        wait_ms=wait_ms,
        is_daemon=is_daemon,
        shell="powershell"
    )


@server.tool()
def get_system_info() -> Dict[str, Any]:
    """Returns detailed Windows system specifications, environment details, and hardware architecture."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        cpu_count = psutil.cpu_count(logical=True)
        cpu_percent = psutil.cpu_percent(interval=None)
        mem_info = {
            "total_gb": round(mem.total / (1024 ** 3), 2),
            "available_gb": round(mem.available / (1024 ** 3), 2),
            "percent_used": mem.percent,
        }
    except ImportError:
        mem_info = "psutil not available"
        cpu_count = os.cpu_count()
        cpu_percent = "N/A"

    return {
        "hostname": platform.node(),
        "os_name": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "current_user": os.environ.get("USERNAME", "unknown"),
        "logical_cpus": cpu_count,
        "cpu_usage_percent": cpu_percent,
        "memory": mem_info,
        "working_directory": os.getcwd(),
    }


@server.tool()
def which(command: str) -> Dict[str, Any]:
    """Finds the absolute path of an executable on the system PATH.
    
    Args:
        command: Name of executable to look up (e.g. 'git', 'node', 'python', 'pwsh').
    """
    path = shutil.which(command.strip())
    return {
        "command": command,
        "found": path is not None,
        "path": path or "Not found in system PATH"
    }


if __name__ == "__main__":
    server.run("stdio")
