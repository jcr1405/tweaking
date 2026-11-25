"""Process Manager
Authoritative manager for asynchronous, bounded-wait OS subprocesses under Windows Job Object containment.
Coordinates stream collectors, bounded synchronous handoffs, persistent artifact storage, and event notifications.
"""

import os
import sys
import time
import uuid
import shutil
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List

from runtime.process_manager.models import ProcessState, ManagedProcess
from runtime.process_manager.output_collector import ProcessOutputCollector

logger = logging.getLogger("presence.process_manager")


class ProcessManager:
    """Supervises OS subprocess execution with bounded synchronous wait and background handoff."""

    def __init__(
        self,
        base_tasks_dir: Optional[Path] = None,
        event_bus: Optional[Any] = None,
        task_registry: Optional[Any] = None,
        permission_boundary: Optional[Any] = None,
    ):
        if base_tasks_dir:
            self.base_tasks_dir = Path(base_tasks_dir)
        else:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                self.base_tasks_dir = Path(local_app_data) / "Presence" / "tasks"
            else:
                self.base_tasks_dir = Path("./data/tasks").resolve()

        self.base_tasks_dir.mkdir(parents=True, exist_ok=True)
        self.event_bus = event_bus
        self.task_registry = task_registry
        self.permission_boundary = permission_boundary

        self._lock = threading.RLock()
        self._processes: Dict[str, ManagedProcess] = {}
        self._supervisors: Dict[str, threading.Thread] = {}

    def _sanitize_env(self, custom_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Scrubs sensitive API keys from child environment while preserving system variables."""
        env = dict(os.environ)
        # Scrub credentials
        for sensitive_var in [
            "GEMINI_API_KEY",
            "FREELLMAPI_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "PRESENCE_SECRET_KEY"
        ]:
            env.pop(sensitive_var, None)

        if custom_env:
            env.update(custom_env)
        return env

    def run_command(
        self,
        command: str,
        args: Optional[List[str]] = None,
        wait_ms: int = 5000,
        is_daemon: bool = False,
        cwd: Optional[str] = None,
        conversation_id: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[float] = None,
        shell: str = "powershell",
    ) -> Dict[str, Any]:
        """Executes a command with bounded wait.
        
        If the process exits within wait_ms, returns synchronous results.
        If the process continues past wait_ms, backgroundes it and returns an execution receipt.
        """
        if not command or not command.strip():
            return {"status": "ERROR", "error": "Empty command provided."}

        # Validate working directory
        effective_cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else os.getcwd()
        if not os.path.isdir(effective_cwd):
            return {"status": "ERROR", "error": f"Working directory does not exist: {effective_cwd}"}

        # Apply Permission Boundary check if configured
        if self.permission_boundary:
            is_allowed, reason = self.permission_boundary.check_permission(
                tool_name="presence.shell.execute",
                context={"command": command, "cwd": effective_cwd}
            )
            if not is_allowed:
                return {
                    "status": "PERMISSION_DENIED",
                    "error": f"Command rejected by permission boundary: {reason}"
                }

        task_id = f"proc_{uuid.uuid4().hex[:12]}"
        task_dir = self.base_tasks_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = str(task_dir / "stdout.log")
        stderr_path = str(task_dir / "stderr.log")

        managed = ManagedProcess(
            task_id=task_id,
            command=command,
            args=list(args) if args else [],
            cwd=effective_cwd,
            state=ProcessState.STARTING,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            is_daemon=is_daemon,
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
        )

        clean_env = self._sanitize_env(env)
        proc_ref = None

        try:
            if sys.platform == "win32":
                from runtime.platform_win32 import Win32SuspendedLauncher, Win32JobObject
                job = Win32JobObject(name=f"Presence_Job_{task_id}")
                job.create(kill_on_close=True)
                managed.job_ref = job

                # Format command through shell interpreter for robust pipeline, redirection, and script support
                clean_shell = shell.strip().lower()
                if clean_shell == "cmd":
                    cmd_exe = "cmd.exe"
                    cmd_args = ["/c", command]
                    if args:
                        cmd_args.extend(args)
                else:
                    cmd_exe = "powershell.exe"
                    cmd_args = [
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy", "Bypass",
                        "-Command", command
                    ]
                    if args:
                        cmd_args.extend(args)

                proc_ref = Win32SuspendedLauncher.launch(
                    command=cmd_exe,
                    args=cmd_args,
                    env=clean_env,
                    cwd=effective_cwd,
                    job=job,
                    no_window=True
                )
                managed.pid = proc_ref.pid
                managed.process_ref = proc_ref

                collector = ProcessOutputCollector(
                    stdout_pipe=proc_ref.stdout,
                    stderr_pipe=proc_ref.stderr,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path
                )
                managed.collector = collector
                collector.start()

            else:
                # Non-Windows POSIX fallback
                import subprocess
                full_cmd = [command] + (list(args) if args else [])
                p = subprocess.Popen(
                    full_cmd,
                    cwd=effective_cwd,
                    env=clean_env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=isinstance(command, str) and not args,
                )
                managed.pid = p.pid
                managed.process_ref = p

                collector = ProcessOutputCollector(
                    stdout_pipe=p.stdout,
                    stderr_pipe=p.stderr,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path
                )
                managed.collector = collector
                collector.start()

        except Exception as launch_err:
            managed.state = ProcessState.FAILED
            managed.error_message = str(launch_err)
            managed.completed_at = time.time()
            logger.error(f"Failed to launch command '{command}': {launch_err}")
            return {
                "status": "LAUNCH_FAILED",
                "task_id": task_id,
                "error": str(launch_err)
            }

        managed.state = ProcessState.RUNNING
        managed.started_at = time.time()

        with self._lock:
            self._processes[task_id] = managed

        # Notify TaskRegistry if available
        if self.task_registry:
            try:
                from runtime.observability.models import TaskType
                t_type = getattr(TaskType, "SUBPROCESS", None) or getattr(TaskType, "TOOL_EXECUTION", None)
                t_node = self.task_registry.register_task(
                    task_id=task_id,
                    task_type=t_type,
                    display_name=f"Process [{managed.pid}]: {command[:40]}",
                    conversation_id=conversation_id,
                    metadata={"task_id": task_id, "pid": managed.pid, "command": command},
                    cancellable=True,
                    cancellation_hook=lambda: self.kill_process(task_id, reason="Cancelled via TaskRegistry")
                )
                self.task_registry.start_task(t_node.task_id)
            except Exception as tr_err:
                logger.debug(f"TaskRegistry registration error: {tr_err}")

        # Bounded synchronous wait loop
        wait_seconds = max(0.0, wait_ms / 1000.0)
        deadline = time.time() + wait_seconds
        has_finished = False

        while time.time() < deadline:
            exit_code = self._poll_raw_process(proc_ref)
            if exit_code is not None:
                has_finished = True
                break
            time.sleep(0.05)

        if has_finished:
            return self._finalize_completed_process(managed, exit_code)

        # Process did not complete within wait_ms -> transition to background execution
        sup_thread = threading.Thread(
            target=self._supervise_background_process,
            args=(task_id, timeout_seconds),
            daemon=True,
            name=f"Supervisor-{task_id}"
        )
        with self._lock:
            self._supervisors[task_id] = sup_thread
        sup_thread.start()

        if self.event_bus:
            try:
                self.event_bus.publish({
                    "event_type": "PROCESS_BACKGROUNDED",
                    "task_id": task_id,
                    "pid": managed.pid,
                    "command": command,
                    "wait_ms": wait_ms
                })
            except Exception:
                pass

        return {
            "status": "BACKGROUNDED",
            "task_id": task_id,
            "pid": managed.pid,
            "message": f"Command continues running in background (exceeded {wait_ms}ms wait threshold).",
            "wait_ms": wait_ms,
            "stdout_preview": managed.collector.get_stdout_preview(max_chars=1000) if managed.collector else "",
            "stderr_preview": managed.collector.get_stderr_preview(max_chars=1000) if managed.collector else "",
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
        }

    def _poll_raw_process(self, proc_ref: Any) -> Optional[int]:
        """Polls raw process object for completion across Win32 and subprocess implementations."""
        if hasattr(proc_ref, "poll"):
            return proc_ref.poll()
        return None

    def _finalize_completed_process(self, managed: ManagedProcess, exit_code: int) -> Dict[str, Any]:
        """Finalizes process state, drains stream collectors, and closes OS handles."""
        managed.completed_at = time.time()
        managed.exit_code = exit_code
        managed.state = ProcessState.COMPLETED if exit_code == 0 else ProcessState.FAILED

        if managed.collector:
            managed.collector.wait_completion(timeout=1.0)

        # Cleanly close OS handles
        if hasattr(managed.process_ref, "close"):
            try:
                managed.process_ref.close()
            except Exception:
                pass

        stdout_prev = managed.collector.get_stdout_preview() if managed.collector else ""
        stderr_prev = managed.collector.get_stderr_preview() if managed.collector else ""

        if self.task_registry:
            try:
                if managed.state == ProcessState.COMPLETED:
                    self.task_registry.complete_task(managed.task_id)
                else:
                    self.task_registry.fail_task(managed.task_id, error_message=f"Exit code: {exit_code}")
            except Exception:
                pass

        return {
            "status": "SUCCESS" if exit_code == 0 else "FAILED",
            "task_id": managed.task_id,
            "pid": managed.pid,
            "exit_code": exit_code,
            "duration_seconds": managed.duration_seconds,
            "stdout": stdout_prev,
            "stderr": stderr_prev,
            "stdout_path": managed.stdout_path,
            "stderr_path": managed.stderr_path,
            "total_bytes": managed.collector.total_bytes_collected if managed.collector else 0
        }

    def _supervise_background_process(self, task_id: str, timeout_seconds: Optional[float]) -> None:
        """Background thread supervising a detached/backgrounded process."""
        with self._lock:
            managed = self._processes.get(task_id)
        if not managed:
            return

        start_time = managed.started_at or time.time()
        timeout = timeout_seconds or 3600.0  # Default 1 hour safety ceiling

        while managed.state == ProcessState.RUNNING:
            if time.time() - start_time > timeout:
                logger.warning(f"Process {task_id} (PID {managed.pid}) timed out after {timeout}s.")
                self.kill_process(task_id, reason=f"Process timed out after {timeout} seconds.", terminal_state=ProcessState.TIMED_OUT)
                break

            exit_code = self._poll_raw_process(managed.process_ref)
            if exit_code is not None:
                self._finalize_completed_process(managed, exit_code)
                if self.event_bus:
                    try:
                        self.event_bus.publish({
                            "event_type": "PROCESS_COMPLETED",
                            "task_id": task_id,
                            "pid": managed.pid,
                            "exit_code": exit_code,
                            "duration_seconds": managed.duration_seconds
                        })
                    except Exception:
                        pass
                break

            time.sleep(0.2)

    def get_process_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Returns the full runtime status and log tail for a managed process."""
        with self._lock:
            managed = self._processes.get(task_id)
        if not managed:
            return None

        # Check for state transition if still marked running
        if managed.state == ProcessState.RUNNING:
            exit_code = self._poll_raw_process(managed.process_ref)
            if exit_code is not None:
                self._finalize_completed_process(managed, exit_code)

        res = managed.to_dict()
        if managed.collector:
            res["stdout_tail"] = managed.collector.read_tail(stream="stdout", max_lines=40)
            res["stderr_tail"] = managed.collector.read_tail(stream="stderr", max_lines=40)
            res["total_bytes_collected"] = managed.collector.total_bytes_collected
        return res

    def send_input(self, task_id: str, input_text: str) -> bool:
        """Delivers stdin text to a running managed process."""
        with self._lock:
            managed = self._processes.get(task_id)
        if not managed or managed.state != ProcessState.RUNNING or not managed.process_ref:
            return False

        try:
            stdin_stream = getattr(managed.process_ref, "stdin", None)
            if stdin_stream and not stdin_stream.closed:
                data = input_text.encode("utf-8")
                stdin_stream.write(data)
                stdin_stream.flush()
                return True
        except Exception as e:
            logger.error(f"Error sending input to process {task_id}: {e}")
        return False

    def kill_process(
        self,
        task_id: str,
        reason: str = "Terminated by user",
        terminal_state: ProcessState = ProcessState.CANCELLED
    ) -> bool:
        """Terminates the process and its entire Job Object process tree."""
        with self._lock:
            managed = self._processes.get(task_id)
        if not managed or managed.state.is_terminal:
            return False

        managed.state = terminal_state
        managed.error_message = reason
        managed.completed_at = time.time()

        if managed.collector:
            managed.collector.stop()

        # Hard terminate the Job Object container first
        if managed.job_ref and hasattr(managed.job_ref, "terminate"):
            try:
                managed.job_ref.terminate(exit_code=1)
            except Exception:
                pass

        # Terminate root process directly
        if managed.process_ref:
            if hasattr(managed.process_ref, "terminate"):
                try:
                    managed.process_ref.terminate(exit_code=1)
                except Exception:
                    pass
            if hasattr(managed.process_ref, "close"):
                try:
                    managed.process_ref.close()
                except Exception:
                    pass

        if self.task_registry:
            try:
                if terminal_state == ProcessState.TIMED_OUT and hasattr(self.task_registry, "timeout_task"):
                    self.task_registry.timeout_task(task_id, error_message=reason)
                else:
                    self.task_registry.cancel_task(task_id, reason=reason)
            except Exception:
                pass

        if self.event_bus:
            try:
                self.event_bus.publish({
                    "event_type": "PROCESS_CANCELLED",
                    "task_id": task_id,
                    "pid": managed.pid,
                    "reason": reason
                })
            except Exception:
                pass

        return True

    def list_processes(self, include_completed: bool = True) -> List[Dict[str, Any]]:
        """Lists managed processes."""
        with self._lock:
            procs = list(self._processes.values())

        results = []
        for p in procs:
            if not include_completed and p.state.is_terminal:
                continue
            results.append(p.to_dict())
        return results

    def cleanup(self) -> None:
        """Terminates any non-daemon running processes on application shutdown."""
        with self._lock:
            active_ids = [
                tid for tid, p in self._processes.items()
                if not p.state.is_terminal and not p.is_daemon
            ]
        for tid in active_ids:
            self.kill_process(tid, reason="Application shutdown cleanup")
