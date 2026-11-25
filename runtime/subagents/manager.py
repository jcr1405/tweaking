"""Subagent Execution Manager
Orchestrates isolated child agent sessions, concurrency limits, recursion depth ceilings,
role-based tool filtering, and structured receipt propagation.
"""

import os
import time
import uuid
import logging
import threading
from typing import Optional, Dict, Any, List, Callable

from runtime.subagents.models import (
    SubagentRole,
    SubagentStatus,
    SubagentConfig,
    SubagentExecutionResult,
    ManagedSubagent,
)
from runtime.session import AgentSessionFactory, AgentSession
from runtime.agent_executor import CancellationToken, AgentExecutionEvent
from runtime.observability.models import TaskType, TaskState
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.events import EventBus
from runtime.artifacts.store import ArtifactStore

logger = logging.getLogger("presence.subagents.manager")


class SubagentManager:
    """Central supervisor for ephemeral subagents.
    
    Guarantees:
    1. Trajectory isolation: Subagents execute within their own scoped SQLite database.
    2. Zero parent trajectory pollution: Only final summary receipts return to parent.
    3. Concurrency caps: Strictly bounds global and per-parent concurrent subagents.
    4. Recursion ceiling: Enforces maximum nesting depth to prevent infinite agent loops.
    5. Clean cancellation: Subagents can be individually or bulk-terminated.
    """

    def __init__(
        self,
        session_factory: AgentSessionFactory,
        task_registry: Optional[ActiveTaskRegistry] = None,
        event_bus: Optional[EventBus] = None,
        artifact_store: Optional[ArtifactStore] = None,
        max_global_subagents: int = 3,
        max_subagents_per_parent: int = 2,
        max_recursion_depth: int = 2,
    ):
        self.session_factory = session_factory
        self.task_registry = task_registry
        self.event_bus = event_bus
        self.artifact_store = artifact_store or ArtifactStore()
        self.max_global_subagents = max_global_subagents
        self.max_subagents_per_parent = max_subagents_per_parent
        self.max_recursion_depth = max_recursion_depth

        self._lock = threading.Lock()
        self._subagents: Dict[str, ManagedSubagent] = {}

    def spawn_subagent(
        self,
        parent_conversation_id: str,
        config: SubagentConfig,
        wait_ms: int = 10000,
    ) -> Dict[str, Any]:
        """Spawns an ephemeral subagent with bounded synchronous wait.
        
        If the subagent finishes within wait_ms, returns synchronous execution result.
        Otherwise, returns a receipt indicating background execution.
        """
        if not config.prompt or not config.prompt.strip():
            return {"status": "ERROR", "error": "Subagent prompt cannot be empty."}

        # 1. Enforce Concurrency, Deduplication, and Recursion Guards
        with self._lock:
            # Deduplication guard: check if active subagent already exists with identical parent, role, and prompt
            norm_prompt = config.prompt.strip()
            for existing in self._subagents.values():
                if existing.parent_conversation_id == parent_conversation_id and not existing.status.is_terminal:
                    if existing.role == config.role and existing.prompt.strip() == norm_prompt:
                        return {
                            "status": "ERROR",
                            "error": (
                                f"Active subagent already exists for parent '{parent_conversation_id}' "
                                f"with identical role '{config.role}' and prompt."
                            ),
                            "subagent_id": existing.subagent_id,
                            "conversation_id": existing.conversation_id,
                        }

            active_global = sum(1 for s in self._subagents.values() if not s.status.is_terminal)
            if active_global >= self.max_global_subagents:
                return {
                    "status": "ERROR",
                    "error": f"Global subagent limit reached ({active_global}/{self.max_global_subagents} active)."
                }

            effective_parent_limit = self.max_subagents_per_parent
            if config.max_parent_subagents is not None:
                effective_parent_limit = min(effective_parent_limit, config.max_parent_subagents)

            active_parent = sum(
                1 for s in self._subagents.values()
                if s.parent_conversation_id == parent_conversation_id and not s.status.is_terminal
            )
            if active_parent >= effective_parent_limit:
                return {
                    "status": "ERROR",
                    "error": (
                        f"Per-parent subagent limit reached for '{parent_conversation_id}' "
                        f"({active_parent}/{effective_parent_limit} active)."
                    )
                }

        # Determine nesting depth from parent session or store
        parent_depth = 0
        if self.session_factory.conversations_dir:
            from runtime.trajectory import TrajectoryStore
            try:
                p_store = TrajectoryStore(
                    conversation_id=parent_conversation_id,
                    db_dir=self.session_factory.conversations_dir
                )
                p_meta = p_store.get_metadata()
                if p_meta:
                    parent_depth = p_meta.nesting_depth
            except Exception as e:
                logger.debug(f"Could not read parent metadata for nesting depth: {e}")

        child_depth = parent_depth + 1
        if child_depth > self.max_recursion_depth:
            return {
                "status": "ERROR",
                "error": f"Maximum subagent recursion depth exceeded ({child_depth} > {self.max_recursion_depth})."
            }

        # 2. Allocate Subagent and Child Conversation
        subagent_id = f"sub_{uuid.uuid4().hex[:12]}"
        child_cid = f"conv_{uuid.uuid4().hex[:16]}"
        cancel_token = CancellationToken()

        # 3. Role-based Tool Filtering
        tool_whitelist = self._resolve_tool_whitelist(config)

        # 4. Construct Isolated Child Session
        child_title = f"Subagent [{config.role.upper()}]: {config.prompt[:30]}"
        try:
            child_session = self.session_factory.create_session(
                conversation_id=child_cid,
                title=child_title,
                parent_conversation_id=parent_conversation_id,
                nesting_depth=child_depth,
                event_bus=self.event_bus,
                # Subagents do not get subagent, task, or delegation tools
                include_task_tools=False,
                include_subagent_tools=False,
                include_delegation_tools=False,
                session_role=f"subagent_{config.role}",
                is_cloud_principal=False,
            )
            # Ensure delegation tool is strictly excluded from subagents
            child_session.tool_registry.unregister("presence.delegate_local_work")
            child_session.tool_registry.unregister("delegate_local_work")
        except Exception as sess_err:
            logger.error(f"Failed to create child session for subagent '{subagent_id}': {sess_err}")
            return {"status": "ERROR", "error": f"Failed to initialize subagent session: {sess_err}"}

        # Apply whitelist filter to child session tools
        if tool_whitelist is not None:
            self._filter_session_tools(child_session, tool_whitelist)

        # 5. Initialize ManagedSubagent State
        managed = ManagedSubagent(
            subagent_id=subagent_id,
            conversation_id=child_cid,
            parent_conversation_id=parent_conversation_id,
            role=config.role,
            prompt=config.prompt,
            status=SubagentStatus.STARTING,
            session_ref=child_session,
            cancellation_token=cancel_token,
            task_id=subagent_id
        )

        with self._lock:
            self._subagents[subagent_id] = managed

        # 6. Register in TaskRegistry
        if self.task_registry:
            try:
                self.task_registry.register_task(
                    task_id=subagent_id,
                    task_type=TaskType.SUBAGENT,
                    display_name=f"Subagent [{config.role}]: {config.prompt[:40]}",
                    conversation_id=parent_conversation_id,
                    cancellable=True,
                    cancellation_hook=cancel_token.cancel,
                    metadata={
                        "subagent_id": subagent_id,
                        "child_conversation_id": child_cid,
                        "role": config.role,
                        "nesting_depth": child_depth
                    }
                )
                self.task_registry.start_task(subagent_id)
            except Exception as tr_err:
                logger.warning(f"Failed to register subagent in TaskRegistry: {tr_err}")

        if self.event_bus:
            try:
                self.event_bus.publish("SUBAGENT_STARTED", {
                    "subagent_id": subagent_id,
                    "conversation_id": child_cid,
                    "parent_conversation_id": parent_conversation_id,
                    "role": config.role,
                    "prompt": config.prompt[:100]
                })
            except Exception:
                pass

        # 7. Start Execution Worker Thread
        worker_done = threading.Event()
        worker_thread = threading.Thread(
            target=self._run_subagent_worker,
            args=(managed, config, worker_done),
            daemon=True,
            name=f"SubagentWorker-{subagent_id}"
        )
        managed.thread_ref = worker_thread
        managed.status = SubagentStatus.RUNNING
        managed.started_at = time.time()
        worker_thread.start()

        # 8. Bounded Synchronous Wait
        wait_seconds = max(0.0, wait_ms / 1000.0)
        completed_in_time = worker_done.wait(timeout=wait_seconds)

        if completed_in_time:
            return self._finalize_subagent_result(managed)

        # 9. Asynchronous / Background Hand-off
        return {
            "status": "BACKGROUNDED",
            "subagent_id": subagent_id,
            "task_id": subagent_id,
            "conversation_id": child_cid,
            "parent_conversation_id": parent_conversation_id,
            "role": config.role,
            "message": f"Subagent execution running in background (exceeded {wait_ms}ms wait threshold).",
            "wait_ms": wait_ms,
        }

    def _run_subagent_worker(
        self,
        managed: ManagedSubagent,
        config: SubagentConfig,
        done_event: threading.Event
    ) -> None:
        """Background worker thread executing child session turn(s)."""
        session: AgentSession = managed.session_ref
        token: CancellationToken = managed.cancellation_token
        timeout = config.timeout_seconds or 300.0

        system_prompt = config.system_prompt or self._get_role_system_prompt(config.role)

        # Resolve request_config for local Ollama pinning
        req_config = config.request_config
        if req_config is None and (config.pin_local_ollama or config.model_id == "qwen3.5:9b"):
            from runtime.providers.coordinator import InferenceRequestConfig
            req_config = InferenceRequestConfig(
                provider="ollama",
                model=config.model_id or "qwen3.5:9b",
                mode="routine",
                purpose="local_subagent_execution",
                fallback_policy="no_fallback",
                cancellation_token=token,
                conversation_id=managed.conversation_id,
            )
        managed._request_config = req_config

        final_response_text = ""
        try:
            with managed._turn_lock:
                deadline = time.time() + timeout
                turn_iter = session.execute_turn(
                    user_input=config.prompt,
                    system_prompt=system_prompt,
                    cancellation_token=token,
                    max_tool_iterations=config.max_iterations,
                    request_config=req_config,
                )

                for event in turn_iter:
                    if token.is_cancelled:
                        managed.status = SubagentStatus.CANCELLED
                        managed.error_message = "Subagent cancelled."
                        break

                    if time.time() > deadline:
                        token.cancel()
                        managed.status = SubagentStatus.TIMED_OUT
                        managed.error_message = f"Subagent timed out after {timeout} seconds."
                        break

                    if event.event_type == "TURN_COMPLETED":
                        final_response_text = event.payload.get("final_text", "")
                        managed.summary = final_response_text
                        managed.status = SubagentStatus.COMPLETED
                    elif event.event_type == "TURN_FAILED":
                        managed.status = SubagentStatus.FAILED
                        managed.error_message = event.payload.get("error", "Subagent turn failed")
                    elif event.event_type == "TURN_CANCELLED":
                        managed.status = SubagentStatus.CANCELLED
                        managed.error_message = event.payload.get("reason", "Cancelled")

        except Exception as e:
            logger.error(f"Unhandled exception in subagent '{managed.subagent_id}': {e}")
            managed.status = SubagentStatus.FAILED
            managed.error_message = str(e)
        finally:
            managed.completed_at = time.time()
            managed.duration_seconds = max(0.0, (managed.completed_at - (managed.started_at or managed.created_at)))
            managed.step_count = session.get_step_count()

            # Offload very large summary to artifact if > 30 KB
            if managed.summary and self.artifact_store.should_offload(managed.summary):
                try:
                    meta = self.artifact_store.store_text_artifact(
                        conversation_id=managed.parent_conversation_id,
                        tool_call_id=managed.subagent_id,
                        tool_name=f"subagent.{managed.role}",
                        content_str=managed.summary
                    )
                    managed.summary = f"{meta.preview}\n\n[Full Subagent Report: Artifact {meta.artifact_id} ({meta.byte_size} bytes)]"
                except Exception as art_err:
                    logger.warning(f"Could not offload subagent summary to artifact: {art_err}")

            # Notify TaskRegistry
            if self.task_registry:
                try:
                    if managed.status == SubagentStatus.COMPLETED:
                        self.task_registry.complete_task(managed.subagent_id)
                    elif managed.status == SubagentStatus.TIMED_OUT:
                        if hasattr(self.task_registry, "timeout_task"):
                            self.task_registry.timeout_task(managed.subagent_id, error_message=managed.error_message or "Timed out")
                        else:
                            self.task_registry.fail_task(managed.subagent_id, error_message="Timed out")
                    elif managed.status == SubagentStatus.CANCELLED:
                        self.task_registry.cancel_task(managed.subagent_id, reason=managed.error_message)
                    else:
                        self.task_registry.fail_task(managed.subagent_id, error_message=managed.error_message or "Failed")
                except Exception:
                    pass

            # Notify EventBus
            if self.event_bus:
                try:
                    ev_type = f"SUBAGENT_{managed.status.value}"
                    self.event_bus.publish(ev_type, {
                        "subagent_id": managed.subagent_id,
                        "conversation_id": managed.conversation_id,
                        "parent_conversation_id": managed.parent_conversation_id,
                        "role": managed.role,
                        "status": managed.status.value,
                        "duration_seconds": managed.duration_seconds,
                        "step_count": managed.step_count,
                        "summary": managed.summary[:300]
                    })
                except Exception:
                    pass

            # Cleanly close child session
            try:
                session.close()
            except Exception:
                pass

            done_event.set()

    def _finalize_subagent_result(self, managed: ManagedSubagent) -> Dict[str, Any]:
        """Formats the final execution receipt returned to the parent."""
        return {
            "status": "SUCCESS" if managed.status == SubagentStatus.COMPLETED else managed.status.value,
            "subagent_id": managed.subagent_id,
            "conversation_id": managed.conversation_id,
            "parent_conversation_id": managed.parent_conversation_id,
            "role": managed.role,
            "summary": managed.summary if managed.status == SubagentStatus.COMPLETED else (managed.error_message or "No summary available"),
            "step_count": managed.step_count,
            "duration_seconds": round(managed.duration_seconds, 3),
            "error": managed.error_message,
        }

    def _resolve_tool_whitelist(self, config: SubagentConfig) -> Optional[List[str]]:
        """Resolves allowed tools based on role and custom whitelist."""
        if config.tools_whitelist is not None:
            return config.tools_whitelist

        role = config.role.lower()
        if role == SubagentRole.RESEARCH.value:
            # Research subagents only receive read-only observation & fetching tools
            return [
                "presence.web_search",
                "presence.fetch_web_page",
                "presence.read_artifact",
                "mcp.filesystem.list_directory",
                "mcp.filesystem.read_file",
            ]
        elif role == SubagentRole.CODE_ANALYSIS.value:
            # Code analysis subagents get filesystem inspection and artifact tools (no arbitrary command execution)
            return [
                "presence.read_artifact",
                "mcp.filesystem.list_directory",
                "mcp.filesystem.read_file",
                "mcp.filesystem.get_file_info",
                "presence.web_search",
                "presence.fetch_web_page"
            ]
        elif role == SubagentRole.VERIFIER.value:
            # Verifier subagents get read-only verification tools
            return [
                "presence.web_search",
                "presence.fetch_web_page",
                "presence.read_artifact",
                "mcp.filesystem.read_file",
            ]

        # General subagent: None = inherit default tools
        return None

    def _filter_session_tools(self, session: AgentSession, whitelist: List[str]) -> None:
        """Removes tools from session's ToolRegistry not present in whitelist."""
        registry = session.tool_registry
        existing_ids = list(registry._tools.keys())
        for tid in existing_ids:
            tool_def = registry._tools[tid]
            # Check both tool_id and name
            if tid not in whitelist and tool_def.name not in whitelist:
                del registry._tools[tid]
        logger.info(f"Filtered subagent session tools down to {len(registry.list_tools())} tools.")

    def _get_role_system_prompt(self, role: str) -> str:
        """Constructs role-tailored system prompt."""
        role_lower = role.lower()
        if role_lower == SubagentRole.RESEARCH.value:
            return (
                "You are a dedicated Research Subagent. Your role is to explore public documentation, "
                "search the web, fetch relevant pages, and synthesize your findings. "
                "Provide an objective, structured summary answering the parent agent's query. "
                "Do not invent facts. Return only verified findings."
            )
        elif role_lower == SubagentRole.CODE_ANALYSIS.value:
            return (
                "You are a Code Analysis Subagent. Your role is to inspect source code, examine architectures, "
                "trace dependencies, and identify potential bugs or security risks. "
                "Provide a clear, technical, line-referenced analysis."
            )
        elif role_lower == SubagentRole.VERIFIER.value:
            return (
                "You are an Independent Verification Subagent. Your role is to verify claims, check results, "
                "and ensure specifications are satisfied without bias. Provide a concise PASS/FAIL assessment."
            )
        return (
            "You are an autonomous Subagent executing a focused, delegated task on behalf of your parent agent. "
            "Execute the requested instruction thoroughly, and provide a clear final summary."
        )

    def get_subagent_status(self, subagent_id: str) -> Optional[Dict[str, Any]]:
        """Returns the current runtime status of a subagent."""
        with self._lock:
            managed = self._subagents.get(subagent_id)
        if not managed:
            return None
        return managed.to_dict()

    def list_subagents(
        self,
        parent_conversation_id: Optional[str] = None,
        include_completed: bool = True
    ) -> List[Dict[str, Any]]:
        """Lists tracked subagents, optionally filtered by parent conversation."""
        with self._lock:
            subagents = list(self._subagents.values())

        results = []
        for s in subagents:
            if parent_conversation_id and s.parent_conversation_id != parent_conversation_id:
                continue
            if not include_completed and s.status.is_terminal:
                continue
            results.append(s.to_dict())
        return results

    def send_message(self, subagent_id: str, message: str) -> Dict[str, Any]:
        """Delivers a follow-up user prompt to an active subagent."""
        with self._lock:
            managed = self._subagents.get(subagent_id)
        if not managed:
            return {"status": "ERROR", "error": f"Subagent '{subagent_id}' not found."}
        if managed.status.is_terminal or not managed.session_ref:
            return {"status": "ERROR", "error": f"Subagent '{subagent_id}' is in terminal state ({managed.status.value})."}

        session: AgentSession = managed.session_ref
        with managed._turn_lock:
            try:
                # Execute follow-up turn synchronously
                final_text = ""
                turn_kwargs: Dict[str, Any] = {"user_input": message}
                if managed._request_config:
                    turn_kwargs["request_config"] = managed._request_config

                for event in session.execute_turn(**turn_kwargs):
                    if event.event_type == "TURN_COMPLETED":
                        final_text = event.payload.get("final_text", "")

                managed.summary = final_response_text = final_text
                managed.step_count = session.get_step_count()
                return {
                    "status": "SUCCESS",
                    "subagent_id": subagent_id,
                    "conversation_id": managed.conversation_id,
                    "response": final_response_text
                }
            except Exception as e:
                logger.error(f"Error sending message to subagent '{subagent_id}': {e}")
                return {"status": "ERROR", "error": str(e)}

    def kill_subagent(self, subagent_id: str, reason: str = "Terminated by user") -> bool:
        """Terminates an active subagent."""
        with self._lock:
            managed = self._subagents.get(subagent_id)
            if not managed or managed.status.is_terminal:
                return False

            managed.status = SubagentStatus.CANCELLED
            managed.error_message = reason
            managed.completed_at = time.time()
            managed.duration_seconds = max(0.0, managed.completed_at - (managed.started_at or managed.created_at))

        if managed.cancellation_token:
            managed.cancellation_token.cancel()

        if self.task_registry:
            try:
                self.task_registry.cancel_task(subagent_id, reason=reason)
            except Exception:
                pass

        if self.event_bus:
            try:
                self.event_bus.publish("SUBAGENT_CANCELLED", {
                    "subagent_id": subagent_id,
                    "conversation_id": managed.conversation_id,
                    "parent_conversation_id": managed.parent_conversation_id,
                    "reason": reason
                })
            except Exception:
                pass

        return True

    def kill_all(self, parent_conversation_id: Optional[str] = None) -> int:
        """Terminates all active subagents, optionally filtered by parent."""
        with self._lock:
            active_ids = [
                s.subagent_id for s in self._subagents.values()
                if not s.status.is_terminal and (not parent_conversation_id or s.parent_conversation_id == parent_conversation_id)
            ]
        count = 0
        for sid in active_ids:
            if self.kill_subagent(sid, reason="Killed all subagents"):
                count += 1
        return count
