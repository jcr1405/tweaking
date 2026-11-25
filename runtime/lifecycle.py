"""Bootstrap and Lifecycle Management Subsystem
Orchestrates component registration, startup sequencing, single-instance mutex,
unclean shutdown detection, and controlled reverse termination.
"""

import os
import sys
import time
import uuid
import logging
import threading
from enum import Enum, auto
from typing import Optional, Dict, Any, List

from .platform_win32 import Win32SingleInstanceMutex, get_process_memory_info, register_console_ctrl_handler
from .events import EventBus
from .permissions import PermissionBoundary
from .persistence import PersistenceManager
from .commands import CommandDispatcher
from .logging import setup_logger
from .artifacts.store import ArtifactStore
from .summaries import GlobalSummaryManager
from .mcp.schemas import SchemaCache
from .mcp.supervisor import MCPSupervisor
from .mcp.config import load_mcp_configuration
from .session import AgentSessionFactory, AgentSession
from .search import SearchGovernor, GovernorPolicy

logger = logging.getLogger("presence.lifecycle")


class RuntimeState(Enum):
    CREATED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    TERMINATED = auto()


class PresenceRuntime:
    """The central Presence Runtime coordinator."""

    def __init__(
        self,
        mutex_name: str = "Local\\PresenceRuntime_SingleInstanceMutex",
        db_path: Optional[str] = None,
        log_dir: Optional[str] = None,
        enable_context_awareness: bool = False,
        mcp_config_path: Optional[Any] = None,
        schema_cache_dir: Optional[Any] = None,
        artifact_dir: Optional[Any] = None,
        summary_db_path: Optional[Any] = None,
        search_policy: Optional[GovernorPolicy] = None,
    ):
        self.instance_id: str = str(uuid.uuid4())
        self.pid: int = os.getpid()
        self.start_time: float = 0.0
        self.state: RuntimeState = RuntimeState.CREATED
        self._shutdown_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

        # 1. Platform Single-Instance Mutex
        self.mutex = Win32SingleInstanceMutex(mutex_name)

        # 2. Central Logging
        setup_logger(log_dir=db_path.parent if db_path and hasattr(db_path, "parent") else None)

        # 3. Core Subsystems
        self.events = EventBus()
        self.permissions = PermissionBoundary()
        self.persistence = PersistenceManager(db_path=db_path)
        self.commands = CommandDispatcher(permissions=self.permissions, runtime_ref=self)

        # 4. Shared Application-Level Infrastructure
        from pathlib import Path
        self.schema_cache = SchemaCache(cache_dir=Path(schema_cache_dir) if schema_cache_dir else None)
        self.mcp_supervisor = MCPSupervisor(schema_cache=self.schema_cache)
        self.artifact_store = ArtifactStore(root_dir=Path(artifact_dir) if artifact_dir else None)
        self.summary_manager = GlobalSummaryManager(db_path=Path(summary_db_path) if summary_db_path else None)
        self.mcp_config_path = Path(mcp_config_path) if mcp_config_path else None
        self.search_governor = SearchGovernor(policy=search_policy or GovernorPolicy())

        # 5b. Runtime Observability Facade (Phase 2B)
        from runtime.observability import RuntimeObservabilityService
        self.observability = RuntimeObservabilityService(runtime=self)

        # 5c. Process Manager (Phase 1)
        from runtime.process_manager import ProcessManager
        self.process_manager = ProcessManager(
            event_bus=self.events,
            task_registry=self.observability.task_registry,
            permission_boundary=self.permissions,
        )

        # 5c-2. Desktop Controller (Phase 2E / 2E.2)
        from runtime.desktop.controller import DesktopController
        self.desktop_controller = DesktopController()

        # 5. Session Composition Factory
        self.session_factory = AgentSessionFactory(
            runtime=self,
            mcp_supervisor=self.mcp_supervisor,
            search_governor=self.search_governor,
            summary_manager=self.summary_manager,
            artifact_store=self.artifact_store,
            task_registry=self.observability.task_registry,
            process_manager=self.process_manager,
        )

        # 5d. Ephemeral Subagent Manager (Phase 4)
        from runtime.subagents import SubagentManager
        self.subagent_manager = SubagentManager(
            session_factory=self.session_factory,
            task_registry=self.observability.task_registry,
            event_bus=self.events,
            artifact_store=self.artifact_store,
        )
        self.session_factory.subagent_manager = self.subagent_manager

        # 5d-2. Local Operations Supervisor (Phase 2A / 2D)
        from runtime.local_operations import LocalOperationsSupervisor
        self.local_supervisor = LocalOperationsSupervisor(
            session_factory=self.session_factory,
            coordinator=None,
            task_registry=self.observability.task_registry,
            event_bus=self.events,
            artifact_store=self.artifact_store,
            subagent_manager=self.subagent_manager,
            desktop_controller=self.desktop_controller,
        )
        self.session_factory.supervisor = self.local_supervisor

        # 5e. Deterministic Workflow Engine (Phase 5)
        from runtime.workflows import WorkflowEngine
        self.workflow_engine = WorkflowEngine(
            persistence=self.persistence,
            artifact_store=self.artifact_store,
            process_manager=self.process_manager,
            subagent_manager=self.subagent_manager,
            task_registry=self.observability.task_registry,
            event_bus=self.events,
            permission_boundary=self.permissions,
            local_supervisor=self.local_supervisor,
            desktop_controller=self.desktop_controller,
        )
        self.session_factory.workflow_engine = self.workflow_engine

        # 5f. Task Planning & Execution Ledger Service (TDEL)
        from runtime.task_planning import TaskPlanningService
        self.task_planning_service = TaskPlanningService(
            workflow_engine=self.workflow_engine,
            persistence=self.persistence,
            artifact_store=self.artifact_store,
            task_registry=self.observability.task_registry,
            event_bus=self.events,
            local_supervisor=self.local_supervisor,
            desktop_controller=self.desktop_controller,
        )
        self.session_factory.task_planning_service = self.task_planning_service

        # 6. Context Awareness (Phase 2C)
        self.enable_context_awareness = enable_context_awareness
        self.context_manager: Optional[Any] = None
        self.foreground_source: Optional[Any] = None
        if self.enable_context_awareness:
            from .context.manager import ContextManager
            from .context.sources.foreground_window import Win32ForegroundWindowSource
            self.context_manager = ContextManager(event_bus=self.events)
            self.foreground_source = Win32ForegroundWindowSource(
                event_bus=self.events,
                on_snapshot_created=self.context_manager.set_current_snapshot,
            )
            self.context_manager.register_source(self.foreground_source)

        self._unclean_shutdowns_detected: List[Dict[str, Any]] = []

    def start(self) -> bool:
        """Initializes and starts the runtime. Returns False if another instance is already running."""
        if self.state != RuntimeState.CREATED:
            logger.warning(f"Cannot start runtime: Already in state {self.state.name}")
            return False

        self.state = RuntimeState.STARTING
        logger.info(f"Presence Runtime initializing [Instance: {self.instance_id}, PID: {self.pid}]")

        # 1. Enforce Win32 Single-Instance Mutex
        if not self.mutex.acquire():
            logger.error(f"Duplicate instance detected ({self.mutex.mutex_name}). Terminating this instance.")
            self.state = RuntimeState.TERMINATED
            self.persistence.close()
            return False

        self.start_time = time.time()

        # 2. Register Win32 Console / Signal Handler
        def _on_console_ctrl(ctrl_type: int) -> bool:
            logger.info(f"Received OS control signal ({ctrl_type}). Ordering runtime shutdown...")
            self.stop()
            return True

        register_console_ctrl_handler(_on_console_ctrl)

        # 3. Detect and Log Unclean Shutdowns from previous runs
        self._unclean_shutdowns_detected = self.persistence.detect_unclean_shutdowns(self.instance_id)
        if self._unclean_shutdowns_detected:
            for record in self._unclean_shutdowns_detected:
                logger.warning(
                    f"UNCLEAN SHUTDOWN DETECTED: Previous instance {record['instance_id']} (PID {record['pid']}) "
                    f"died unexpectedly at {time.ctime(record['last_heartbeat_utc'])}"
                )

        # 4. Record Initial Start State
        self.persistence.record_start(self.instance_id, self.pid)

        # 4b. Reconcile Interrupted Workflows and Task Plans from previous crash
        try:
            self.persistence.reconcile_interrupted_workflows()
            if hasattr(self, "task_planning_service") and self.task_planning_service:
                self.task_planning_service.reconcile_startup()
        except Exception as e:
            logger.debug(f"Error during workflow and plan reconciliation: {e}")

        # 5. Start Heartbeat Background Thread
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="PresenceHeartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

        # 5b. Load MCP Configuration and Register Servers (Lazy Discovery)
        try:
            configs = load_mcp_configuration(self.mcp_config_path)
            for cfg in configs:
                self.mcp_supervisor.register_server(cfg)
            if configs:
                logger.info(f"Loaded and registered {len(configs)} MCP server configuration(s).")
        except Exception as e:
            logger.error(f"Failed to load MCP configuration: {e}")

        # 6. Publish Lifecycle Event
        self.state = RuntimeState.RUNNING
        self.events.publish("RuntimeStarted", {
            "instance_id": self.instance_id,
            "pid": self.pid,
            "start_time": self.start_time,
            "unclean_shutdowns_count": len(self._unclean_shutdowns_detected),
        })

        logger.info("Presence Runtime successfully started in RUNNING state.")
        return True

    def stop(self) -> None:
        """Gracefully terminates all subsystems and flushes state."""
        if self.state in (RuntimeState.STOPPING, RuntimeState.TERMINATED):
            return

        self.state = RuntimeState.STOPPING
        logger.info("Initiating graceful Presence Runtime shutdown...")

        # 1. Signal background loops
        self._shutdown_event.set()

        # Stop foreground context source if running
        if self.foreground_source is not None:
            try:
                from .context.contracts import ContextSourceState
                if self.foreground_source.state == ContextSourceState.RUNNING:
                    self.foreground_source.stop_sync(timeout=2.0)
            except Exception as e:
                logger.error(f"Error stopping foreground source during runtime shutdown: {e}")

        # Stop all subagents
        if hasattr(self, "subagent_manager") and self.subagent_manager:
            try:
                self.subagent_manager.kill_all()
            except Exception as e:
                logger.error(f"Error terminating subagents during runtime shutdown: {e}")

        # Stop and clean up OS child processes
        if hasattr(self, "process_manager") and self.process_manager:
            try:
                self.process_manager.cleanup()
            except Exception as e:
                logger.error(f"Error cleaning up processes during runtime shutdown: {e}")

        # Stop all MCP child processes
        try:
            self.mcp_supervisor.stop_all()
        except Exception as e:
            logger.error(f"Error stopping MCP supervisor during runtime shutdown: {e}")

        # Close global summary manager
        try:
            self.summary_manager.close()
        except Exception as e:
            logger.error(f"Error closing summary manager: {e}")

        # Close search traffic governor
        try:
            self.search_governor.close()
        except Exception as e:
            logger.error(f"Error closing search governor: {e}")

        # 2. Publish Stopping Event
        try:
            self.events.publish("RuntimeStopped", {
                "instance_id": self.instance_id,
                "uptime_seconds": time.time() - self.start_time if self.start_time else 0,
            })
        except Exception as e:
            logger.error(f"Error publishing RuntimeStopped event: {e}")

        # 3. Record Clean Shutdown in Persistence
        try:
            self.persistence.record_clean_shutdown(self.instance_id)
            self.persistence.close()
        except Exception as e:
            logger.error(f"Error recording clean shutdown in persistence: {e}")

        # 4. Release Mutex
        self.mutex.release()

        self.state = RuntimeState.TERMINATED
        logger.info("Presence Runtime successfully terminated.")

    def _heartbeat_loop(self) -> None:
        """Periodic background thread recording health and heartbeat to SQLite."""
        while not self._shutdown_event.wait(timeout=5.0):
            if self.state == RuntimeState.RUNNING:
                try:
                    self.persistence.record_heartbeat(self.instance_id)
                except Exception as e:
                    logger.error(f"Failed to record runtime heartbeat: {e}")

    def get_health_status(self) -> Dict[str, Any]:
        """Returns structured health and performance metrics."""
        uptime = time.time() - self.start_time if self.start_time else 0.0
        mem_info = get_process_memory_info(self.pid)
        status = {
            "instance_id": self.instance_id,
            "state": self.state.name,
            "pid": self.pid,
            "uptime_seconds": uptime,
            "events_published": self.events.total_events_published,
            "clean_recovery": len(self._unclean_shutdowns_detected) == 0,
            "unclean_shutdowns_detected": len(self._unclean_shutdowns_detected),
            "memory": {
                "working_set_bytes": mem_info.working_set_bytes,
                "working_set_mb": mem_info.working_set_mb,
                "private_bytes": mem_info.private_bytes,
                "private_mb": mem_info.private_mb,
                "peak_working_set_bytes": mem_info.peak_working_set_bytes,
            },
        }
        if self.context_manager is not None:
            snap = self.context_manager.get_current_snapshot()
            status["context_awareness"] = {
                "enabled": True,
                "has_snapshot": snap is not None,
                "snapshot_id": snap.snapshot_id if snap else None,
            }
        if hasattr(self, "mcp_supervisor") and self.mcp_supervisor:
            status["mcp_servers"] = [
                {
                    "server_id": s.server_id,
                    "state": s.state.value,
                    "tools_count": s.tools_count,
                    "memory_mb": s.memory_mb,
                }
                for s in self.mcp_supervisor.get_all_statuses()
            ]
        if hasattr(self, "search_governor") and self.search_governor:
            status["search_governor"] = self.search_governor.get_status()
        return status

    def wait_for_shutdown(self, timeout: Optional[float] = None) -> bool:
        """Blocks until the runtime shutdown event is triggered."""
        return self._shutdown_event.wait(timeout=timeout)

    def create_session(
        self,
        conversation_id: Optional[str] = None,
        title: Optional[str] = None,
        workspace_path: Optional[str] = None,
        model_provider: Optional[Any] = None,
        permission_boundary: Optional[Any] = None,
        include_default_tools: bool = True,
        include_mcp_tools: bool = True,
        include_search_tools: bool = True,
        include_task_tools: bool = True,
        include_subagent_tools: bool = True,
        include_workflow_tools: bool = True,
        include_delegation_tools: Optional[bool] = None,
        include_desktop_tools: bool = False,
        desktop_controller: Optional[Any] = None,
        custom_tools: Optional[List[Any]] = None,
        parent_conversation_id: Optional[str] = None,
        nesting_depth: int = 0,
        supervisor: Optional[Any] = None,
        session_role: Optional[str] = None,
        is_cloud_principal: Optional[bool] = None,
        **kwargs: Any,
    ) -> AgentSession:
        """Creates an isolated AgentSession bound to this runtime's infrastructure."""
        return self.session_factory.create_session(
            conversation_id=conversation_id,
            title=title,
            workspace_path=workspace_path,
            model_provider=model_provider,
            permission_boundary=permission_boundary,
            include_default_tools=include_default_tools,
            include_mcp_tools=include_mcp_tools,
            include_search_tools=include_search_tools,
            include_task_tools=include_task_tools,
            include_subagent_tools=include_subagent_tools,
            include_workflow_tools=include_workflow_tools,
            include_delegation_tools=include_delegation_tools,
            include_desktop_tools=include_desktop_tools,
            desktop_controller=desktop_controller or self.desktop_controller,
            custom_tools=custom_tools,
            parent_conversation_id=parent_conversation_id,
            nesting_depth=nesting_depth,
            supervisor=supervisor or self.local_supervisor,
            session_role=session_role,
            is_cloud_principal=is_cloud_principal,
            **kwargs,
        )

    @property
    def is_running(self) -> bool:
        return self.state == RuntimeState.RUNNING
