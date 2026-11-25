"""Headless Agent Session Composition Layer
Provides a UI-independent foundation for constructing, configuring, and executing
conversational agent sessions composed with application-level infrastructure (MCP Supervisor,
Artifact Storage, Trajectory Persistence, Schema Cache, and Permission Boundary).

Architectural Structure:
PresenceRuntime (Application Lifetime)
  └── AgentSessionFactory (Application Lifetime)
        └── AgentSession (Session / Conversation Lifetime)
              ├── TrajectoryStore & EventJournal (Conversation Isolated)
              ├── ToolRegistry (Session Isolated: Built-ins + Approved MCP Tools)
              ├── PermissionBoundary (Session Policy Authority)
              └── AgentExecutor (Multi-Step Execution Engine)
"""

import os
import uuid
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Iterator, Callable

from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore, Step
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler
from runtime.permissions import PermissionBoundary
from runtime.tools.registry import ToolRegistry, ToolDefinition, build_default_tools
from runtime.tools.search_tools import build_search_tools
from runtime.tools.task_tools import build_task_tools
from runtime.search.governor import SearchGovernor
from runtime.agent_executor import AgentExecutor, AgentExecutionEvent, CancellationToken
from runtime.artifacts.store import ArtifactStore
from runtime.mcp.supervisor import MCPSupervisor
from runtime.providers.base import IModelProvider

logger = logging.getLogger("presence.session")


class AgentSession:
    """Represents an active, isolated conversational agent session.
    Composes session-level persistence, tool catalog, and model execution.
    UI-independent: interacts solely via clean streaming event generators.
    """

    def __init__(
        self,
        conversation_id: str,
        metadata: ConversationMetadata,
        trajectory_store: TrajectoryStore,
        event_journal: EventJournal,
        summary_manager: GlobalSummaryManager,
        tool_registry: ToolRegistry,
        permission_boundary: PermissionBoundary,
        model_provider: IModelProvider,
        agent_executor: AgentExecutor,
        artifact_store: Optional[ArtifactStore] = None,
        context_assembler: Optional[ContextAssembler] = None,
        event_bus: Optional[Any] = None,
        task_planning_service: Optional[Any] = None,
        session_role: str = "cloud_principal",
        is_cloud_principal: bool = True,
    ):
        self.conversation_id = conversation_id
        self.metadata = metadata
        self.trajectory_store = trajectory_store
        self.event_journal = event_journal
        self.summary_manager = summary_manager
        self.tool_registry = tool_registry
        self.permission_boundary = permission_boundary
        self.model_provider = model_provider
        self.agent_executor = agent_executor
        self.artifact_store = artifact_store or ArtifactStore()
        self.context_assembler = context_assembler
        self.event_bus = event_bus
        self.task_planning_service = task_planning_service
        self.session_role = session_role
        self.is_cloud_principal = is_cloud_principal
        self.pending_notifications: List[Dict[str, Any]] = []
        self.is_closed: bool = False

        if self.event_bus and hasattr(self.event_bus, "subscribe"):
            self._subscribe_task_events()

    def _subscribe_task_events(self) -> None:
        """Subscribes session to background task completion and lifecycle events."""
        def _on_event(event):
            p = getattr(event, "payload", {}) or {}
            c_id = p.get("conversation_id")
            if not c_id or c_id == self.conversation_id:
                self.pending_notifications.append({
                    "event_type": getattr(event, "event_type", "TASK_EVENT"),
                    "payload": p
                })
        self._task_listener = _on_event
        for ev in ("TASK_COMPLETED", "TASK_FAILED", "TASK_CANCELLED", "PROCESS_COMPLETED", "PROCESS_FAILED", "PROCESS_CANCELLED"):
            try:
                self.event_bus.subscribe(ev, self._task_listener)
            except Exception:
                pass

    def execute_turn(
        self,
        user_input: str,
        system_prompt: str = "You are a persistent intelligent desktop presence.",
        desktop_ambient_context: Optional[Dict[str, Any]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        max_tool_iterations: Optional[int] = None,
        request_config: Optional[Any] = None
    ) -> Iterator[AgentExecutionEvent]:
        """Executes a full user turn, yielding normalized AgentExecutionEvents.

        Raises:
            RuntimeError: If session is already closed.
        """
        if self.is_closed:
            raise RuntimeError(f"Cannot execute turn on closed AgentSession '{self.conversation_id}'.")

        # Drain any pending background task updates into ambient context
        if self.pending_notifications:
            notes = list(self.pending_notifications)
            self.pending_notifications.clear()
            if desktop_ambient_context is None:
                desktop_ambient_context = {}
            desktop_ambient_context["task_notifications"] = notes

        # -------------------------------------------------------------
        # TDEL Conversational Preemption Pipeline
        # Intercepts multi-step, complex, and long-running requests BEFORE
        # incurring heavy conversation reconstruction and model streaming tokens.
        # -------------------------------------------------------------
        token = cancellation_token or CancellationToken()
        if token.is_cancelled:
            yield AgentExecutionEvent(
                event_type="TURN_CANCELLED",
                payload={"reason": "Turn cancelled before execution."}
            )
            return

        if self.task_planning_service:
            from runtime.task_planning.complexity import ComplexityClassifier
            from runtime.task_planning.models import PlanStatus
            from runtime.trajectory import Step, StepType, StepStatus

            cat = ComplexityClassifier.classify(user_input)
            is_disc = ComplexityClassifier.is_discussion_only(user_input)
            is_plan_only = ComplexityClassifier.is_plan_only(user_input)

            if is_disc:
                logger.info(
                    f"Intent classified as DISCUSSION_ONLY in session '{self.conversation_id}' "
                    f"[category={cat.value}]. Proceeding to direct conversational model turn."
                )
            elif ComplexityClassifier.requires_decomposition(cat):
                logger.info(
                    f"Conversational preemption triggered for '{cat.value}' request in session '{self.conversation_id}' "
                    f"[plan_only={is_plan_only}]."
                )

                # Record user input in trajectory
                user_idx = self.trajectory_store.get_next_step_index()
                user_step = Step(
                    idx=user_idx,
                    step_type=StepType.USER_INPUT,
                    status=StepStatus.DONE,
                    step_payload={"content": user_input}
                )
                self.trajectory_store.append_step(user_step)
                if self.event_journal:
                    self.event_journal.append_step_event(user_step)

                meta = self.trajectory_store.get_metadata()
                title = meta.title if meta else "Active Conversation"
                workspace = meta.workspace_path if meta else ""
                self.summary_manager.index_step_text(
                    conversation_id=self.conversation_id,
                    step_idx=user_idx,
                    title=title,
                    workspace_path=workspace,
                    user_body=user_input
                )

                yield AgentExecutionEvent(
                    event_type="TURN_STARTED",
                    step_index=user_idx,
                    payload={"user_input": user_input}
                )

                yield AgentExecutionEvent(
                    event_type="PLAN_CREATED",
                    step_index=user_idx,
                    payload={"complexity": cat.value, "title": f"Plan for: {user_input[:50]}", "plan_only": is_plan_only}
                )

                # Execute via deterministic or novel workflow bridge
                plan_res = self.task_planning_service.evaluate_and_execute(
                    user_prompt=user_input,
                    conversation_id=self.conversation_id,
                    cancellation_token=token,
                    execute_dag=not is_plan_only
                )

                if plan_res.get("handled_by_workflow"):
                    if plan_res.get("status") == "SUCCESS":
                        yield AgentExecutionEvent(
                            event_type="PLAN_COMPLETED",
                            step_index=user_idx,
                            payload=plan_res
                        )
                        # Extract deliverable text
                        art_id = plan_res.get("final_deliverable_artifact_id") or plan_res.get("ledger_artifact_id")
                        final_text = ""
                        if art_id and self.artifact_store:
                            try:
                                read_res = self.artifact_store.read_artifact(self.conversation_id, art_id)
                                final_text = read_res.content
                            except Exception:
                                pass
                        if not final_text:
                            if plan_res.get("plan_only"):
                                final_text = (
                                    f"Task plan `{plan_res.get('plan_id')}` constructed successfully in plan-only mode.\n"
                                    f"Total nodes: {plan_res.get('total_nodes', 0)}. DAG execution was bypassed as requested."
                                )
                            else:
                                final_text = (
                                    f"Task completed successfully via deterministic workflow `{plan_res.get('workflow_id')}`.\n"
                                    f"Completed {plan_res.get('nodes_completed', 0)}/{plan_res.get('total_nodes', 0)} nodes "
                                    f"in {plan_res.get('duration_seconds', 0.0):.2f}s."
                                )

                        yield AgentExecutionEvent(
                            event_type="TEXT_DELTA",
                            step_index=user_idx,
                            payload={"delta": final_text}
                        )

                        # Record model response step
                        resp_idx = self.trajectory_store.get_next_step_index()
                        resp_step = Step(
                            idx=resp_idx,
                            step_type=StepType.AGENT_TURN,
                            status=StepStatus.DONE,
                            step_payload={"content": final_text}
                        )
                        self.trajectory_store.append_step(resp_step)
                        if self.event_journal:
                            self.event_journal.append_step_event(resp_step)

                        yield AgentExecutionEvent(
                            event_type="TURN_COMPLETED",
                            step_index=user_idx,
                            payload={"status": "COMPLETED", "plan_id": plan_res.get("plan_id")}
                        )
                        return
                    else:
                        # Workflow cancelled or failed
                        yield AgentExecutionEvent(
                            event_type="PLAN_FAILED",
                            step_index=user_idx,
                            payload=plan_res
                        )
                        if plan_res.get("plan_status") == "CANCELLED" or token.is_cancelled:
                            err_msg = plan_res.get("reason") or "Task plan execution was cancelled."
                            yield AgentExecutionEvent(
                                event_type="TEXT_DELTA",
                                step_index=user_idx,
                                payload={"delta": err_msg}
                            )
                            err_idx = self.trajectory_store.get_next_step_index()
                            err_step = Step(
                                idx=err_idx,
                                step_type=StepType.AGENT_TURN,
                                status=StepStatus.DONE,
                                step_payload={"content": err_msg}
                            )
                            self.trajectory_store.append_step(err_step)
                            if self.event_journal:
                                self.event_journal.append_step_event(err_step)

                            yield AgentExecutionEvent(
                                event_type="TURN_CANCELLED",
                                step_index=user_idx,
                                payload={"error": err_msg}
                            )
                            return
                        else:
                            # Workflow execution failed: do NOT cancel the turn.
                            # Fall through to conversational companion agent executor so the model
                            # responds directly, explains the situation, or executes interactively.
                            fail_reason = plan_res.get("reason") or f"Automated workflow did not succeed (Status: {plan_res.get('plan_status')})."
                            logger.info(
                                f"Workflow for plan '{plan_res.get('plan_id')}' did not succeed ({fail_reason}). "
                                f"Falling through to interactive conversational agent executor."
                            )
                            if desktop_ambient_context is None:
                                desktop_ambient_context = {}
                            task_notes = desktop_ambient_context.setdefault("task_notifications", [])
                            task_notes.append({
                                "event_type": "PLAN_FALLTHROUGH",
                                "payload": {
                                    "task_id": plan_res.get("plan_id", "workflow_plan"),
                                    "display_name": f"Automated plan unviable ({fail_reason[:100]}). Handle request interactively."
                                }
                            })

                elif plan_res.get("status") == "PLAN_UNAVAILABLE":
                    logger.info(
                        f"Dynamic task planner unavailable for '{user_input[:50]}'. "
                        f"Falling through to interactive conversational agent executor."
                    )
                    if desktop_ambient_context is None:
                        desktop_ambient_context = {}
                    task_notes = desktop_ambient_context.setdefault("task_notifications", [])
                    task_notes.append({
                        "event_type": "PLAN_UNAVAILABLE",
                        "payload": {
                            "task_id": plan_res.get("plan_id", "planning_unavailable"),
                            "display_name": "Dynamic task planner unavailable. Handle request conversationally and interactively."
                        }
                    })

        # Ground conversational model with ground-truth capability snapshot
        if self.is_cloud_principal and "[AUTHORITATIVE RUNTIME CAPABILITY STATUS]" not in system_prompt:
            try:
                from runtime.capabilities import RuntimeCapabilitySnapshot
                snap = RuntimeCapabilitySnapshot.capture(session=self, runtime=getattr(self, "runtime", None))
                system_prompt = system_prompt + snap.build_system_prompt_snippet()
            except Exception as cap_err:
                logger.debug(f"Could not append capability snapshot: {cap_err}")

        yield from self.agent_executor.execute_turn(
            user_input=user_input,
            system_prompt=system_prompt,
            desktop_ambient_context=desktop_ambient_context,
            cancellation_token=cancellation_token,
            max_tool_iterations=max_tool_iterations,
            request_config=request_config
        )

    def get_history(self) -> List[Step]:
        """Returns the full sequence of steps recorded for this session."""
        return self.trajectory_store.list_steps()

    def get_step_count(self) -> int:
        """Returns the total number of steps in this session's trajectory."""
        return self.trajectory_store.get_step_count()

    def close(self) -> None:
        """Closes session-lifetime resources (trajectory DB, event journal).
        Note: Does NOT terminate application-level infrastructure (MCP Supervisor, ArtifactStore).
        """
        if self.is_closed:
            return

        self.is_closed = True
        if self.event_bus and hasattr(self, "_task_listener"):
            for ev in ("TASK_COMPLETED", "TASK_FAILED", "TASK_CANCELLED", "PROCESS_COMPLETED", "PROCESS_FAILED", "PROCESS_CANCELLED"):
                try:
                    self.event_bus.unsubscribe(ev, self._task_listener)
                except Exception:
                    pass
        try:
            self.trajectory_store.close()
        except Exception as e:
            logger.warning(f"Error closing TrajectoryStore for session '{self.conversation_id}': {e}")

        try:
            self.event_journal.close()
        except Exception as e:
            logger.warning(f"Error closing EventJournal for session '{self.conversation_id}': {e}")

        logger.info(f"AgentSession '{self.conversation_id}' closed cleanly.")

    def __enter__(self) -> "AgentSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class AgentSessionFactory:
    """Application-level factory that composes fully-configured, isolated AgentSessions.
    Binds shared long-lived infrastructure (MCPSupervisor, GlobalSummaryManager, ArtifactStore)
    to per-session isolated components (TrajectoryStore, ToolRegistry, PermissionBoundary).
    """

    def __init__(
        self,
        runtime: Optional[Any] = None,
        mcp_supervisor: Optional[MCPSupervisor] = None,
        search_governor: Optional[SearchGovernor] = None,
        summary_manager: Optional[GlobalSummaryManager] = None,
        artifact_store: Optional[ArtifactStore] = None,
        default_model_provider: Optional[IModelProvider] = None,
        conversations_dir: Optional[Path] = None,
        logs_dir: Optional[Path] = None,
        task_registry: Optional[Any] = None,
        process_manager: Optional[Any] = None,
        subagent_manager: Optional[Any] = None,
        workflow_engine: Optional[Any] = None,
        supervisor: Optional[Any] = None,
    ):
        self.runtime = runtime
        self.mcp_supervisor = mcp_supervisor
        self.search_governor = search_governor
        self.summary_manager = summary_manager or GlobalSummaryManager()
        self.artifact_store = artifact_store or ArtifactStore()
        self.default_model_provider = default_model_provider
        self.conversations_dir = conversations_dir
        self.logs_dir = logs_dir
        self.task_registry = task_registry
        self.process_manager = process_manager
        self.subagent_manager = subagent_manager
        self.workflow_engine = workflow_engine
        self.supervisor = supervisor

    def create_session(
        self,
        conversation_id: Optional[str] = None,
        title: Optional[str] = None,
        workspace_path: Optional[str] = None,
        model_provider: Optional[IModelProvider] = None,
        permission_boundary: Optional[PermissionBoundary] = None,
        include_default_tools: bool = True,
        include_mcp_tools: bool = True,
        include_search_tools: bool = True,
        include_task_tools: bool = True,
        include_subagent_tools: bool = True,
        include_workflow_tools: bool = True,
        include_delegation_tools: Optional[bool] = None,
        include_desktop_tools: bool = False,
        desktop_controller: Optional[Any] = None,
        custom_tools: Optional[List[ToolDefinition]] = None,
        parent_conversation_id: Optional[str] = None,
        nesting_depth: int = 0,
        event_bus: Optional[Any] = None,
        supervisor: Optional[Any] = None,
        session_role: Optional[str] = None,
        is_cloud_principal: Optional[bool] = None,
    ) -> AgentSession:
        """Constructs an isolated, fully composed AgentSession.

        Guarantees:
        1. Conversation isolation: dedicated TrajectoryStore and EventJournal per session.
        2. Tool isolation: dedicated ToolRegistry per session, populated with approved MCP tools.
        3. Application infrastructure reuse: shares the long-lived MCPSupervisor, SchemaCache,
           ArtifactStore, and GlobalSummaryManager.
        """
        cid = conversation_id or str(uuid.uuid4())
        session_title = title or "Interactive Session"

        # Determine cloud principal vs child/local execution role
        from runtime.config import is_local_only_mode
        local_mode = is_local_only_mode()
        effective_is_cloud = is_cloud_principal if is_cloud_principal is not None else (nesting_depth == 0 and not local_mode)
        effective_role = session_role or ("personal_companion" if local_mode else ("cloud_principal" if effective_is_cloud else f"child_depth_{nesting_depth}"))
        effective_supervisor = supervisor or self.supervisor or getattr(self.runtime, "local_supervisor", None)
        effective_desktop = desktop_controller or getattr(self.runtime, "desktop_controller", None)
        if include_delegation_tools is None:
            include_delegation_tools = bool(nesting_depth == 0 and effective_supervisor)
        if local_mode:
            include_desktop_tools = True

        # 1. Trajectory and Journal (Isolated per session)
        store = TrajectoryStore(conversation_id=cid, db_dir=self.conversations_dir)
        meta = store.get_metadata()
        if not meta:
            meta = ConversationMetadata(
                conversation_id=cid,
                title=session_title,
                workspace_path=workspace_path or "",
                parent_conversation_id=parent_conversation_id or "",
                nesting_depth=nesting_depth
            )
            store.save_metadata(meta)
        else:
            if title and meta.title != title:
                meta.title = title
                store.save_metadata(meta)

        self.summary_manager.upsert_summary(
            conversation_id=cid,
            title=meta.title,
            workspace_path=meta.workspace_path,
            parent_conversation_id=parent_conversation_id or "",
            nesting_depth=nesting_depth
        )

        journal = EventJournal(conversation_id=cid, logs_dir=self.logs_dir)

        # 2. Permission Boundary and Tool Catalog (Isolated per session)
        session_boundary = permission_boundary or PermissionBoundary()
        session_registry = ToolRegistry()

        # 3. Attach default L0 observation tools
        if include_default_tools:
            default_reg = build_default_tools(
                runtime_status_fn=lambda: (
                    self.runtime.get_health_status()
                    if self.runtime and hasattr(self.runtime, "get_health_status")
                    else {"status": "ok", "state": "RUNNING"}
                ),
                conversation_info_fn=lambda: {
                    "conversation_id": cid,
                    "step_count": store.get_step_count(),
                    "title": meta.title
                },
                search_conversations_fn=lambda q, lim: self.summary_manager.search_conversations(q, lim),
                read_artifact_fn=lambda a_id, off, lim, m: (
                    self.artifact_store.read_artifact(
                        conversation_id=cid if self.artifact_store.get_metadata(cid, a_id) is not None else "global_search",
                        artifact_id=a_id, offset=off, limit=lim, mode=m
                    ).to_dict()
                ),
                permission_boundary=session_boundary
            )
            for tool_def in default_reg.list_tools():
                session_registry.register(tool_def, permission_boundary=session_boundary)

        # 4. Attach approved tools from the application-level MCPSupervisor
        # CRITICAL SECURITY RULE: Do not expose raw MCP catalogues directly to the Cloud Principal.
        # Cloud Principal delegates operational work to LocalOperationsSupervisor via presence.delegate_local_work.
        effective_include_mcp = include_mcp_tools and (not effective_is_cloud or nesting_depth > 0)
        if effective_include_mcp and self.mcp_supervisor:
            self.mcp_supervisor.register_tools_with_registry(
                tool_registry=session_registry,
                permission_boundary=session_boundary
            )

        # 4b. Attach native governed search and fetch tools
        if include_search_tools and self.search_governor:
            search_tools = build_search_tools(
                governor=self.search_governor,
                artifact_store=self.artifact_store,
                current_conversation_id=cid,
            )
            for st in search_tools:
                session_registry.register(st, permission_boundary=session_boundary)

        # 4c. Attach native task management tools (Phase 2)
        effective_task_reg = self.task_registry
        if not effective_task_reg and self.runtime and hasattr(self.runtime, "observability") and self.runtime.observability:
            effective_task_reg = self.runtime.observability.task_registry

        if include_task_tools and effective_task_reg:
            task_tools = build_task_tools(
                task_registry=effective_task_reg,
                process_manager=self.process_manager
            )
            for tt in task_tools:
                session_registry.register(tt, permission_boundary=session_boundary)

        # 4d. Attach native subagent tools (Phase 4)
        if include_subagent_tools and self.subagent_manager and nesting_depth < 2:
            from runtime.tools.subagent_tools import build_subagent_tools
            subagent_tools = build_subagent_tools(
                subagent_manager=self.subagent_manager,
                current_conversation_id=cid
            )
            for sat in subagent_tools:
                session_registry.register(sat, permission_boundary=session_boundary)

        # 4d. Resolve Model Provider (Phase 1 Multi-Provider Coordinator & Failover)
        provider = model_provider or self.default_model_provider
        if not provider:
            from runtime.config import is_local_only_mode
            from runtime.providers.gemini import GeminiModelProvider
            from runtime.providers.freellmapi import FreeLLMAPIProvider
            from runtime.providers.ollama import OllamaModelProvider
            from runtime.providers.fake import FakeModelProvider
            from runtime.providers.coordinator import MultiProviderCoordinator

            local_only = is_local_only_mode()
            pref_provider = ("ollama" if local_only else os.environ.get("PRESENCE_PROVIDER", "freellmapi")).lower()
            if pref_provider == "fake":
                provider = FakeModelProvider()
            else:
                available_providers: Dict[str, Any] = {}

                # 1. Ollama
                try:
                    o_prov = OllamaModelProvider()
                    v_o, _ = o_prov.validate_configuration()
                    if v_o:
                        available_providers["ollama"] = o_prov
                except Exception:
                    pass

                # 2. FreeLLMAPI (Cloud - skipped in local-only mode)
                if not local_only:
                    try:
                        f_prov = FreeLLMAPIProvider()
                        v_f, _ = f_prov.validate_configuration()
                        if v_f:
                            available_providers["freellmapi"] = f_prov
                    except Exception:
                        pass

                    # 3. Gemini (Cloud - skipped in local-only mode)
                    try:
                        g_prov = GeminiModelProvider()
                        v_g, _ = g_prov.validate_configuration()
                        if v_g:
                            available_providers["gemini"] = g_prov
                    except Exception:
                        pass

                if available_providers:
                    primary = pref_provider if pref_provider in available_providers else (
                        "ollama" if "ollama" in available_providers else next(iter(available_providers))
                    )
                    fallbacks = [p for p in (["ollama"] if local_only else ["ollama", "gemini", "freellmapi"]) if p != primary and p in available_providers]
                    ebus = event_bus or getattr(self.runtime, "events", None)
                    provider = MultiProviderCoordinator(
                        providers=available_providers,
                        primary_provider=primary,
                        fallback_order=fallbacks,
                        event_bus=ebus,
                        conversation_id=cid
                    )
                else:
                    provider = FakeModelProvider()

        # 4e. Deterministic Workflow Tools (Phase 5)
        bound_planning_service = None
        if include_workflow_tools and self.workflow_engine:
            from runtime.tools.workflow_tools import build_workflow_tools
            from runtime.workflows.engine import WorkflowEngine
            bound_engine = WorkflowEngine(
                persistence=self.workflow_engine.persistence,
                artifact_store=self.artifact_store,
                tool_registry=session_registry,
                process_manager=self.process_manager,
                subagent_manager=self.subagent_manager,
                task_registry=effective_task_reg,
                event_bus=event_bus or getattr(self.runtime, "events", None),
                permission_boundary=session_boundary,
                local_supervisor=effective_supervisor,
                desktop_controller=effective_desktop,
            )
            workflow_tools = build_workflow_tools(
                workflow_engine=bound_engine,
                current_conversation_id=cid
            )
            for wt in workflow_tools:
                session_registry.register(wt, permission_boundary=session_boundary)

            # 4f. Task Planning and Ledger Tools (TDEL)
            from runtime.task_planning import TaskPlanningService
            from runtime.tools.plan_tools import build_plan_tools
            bound_planning_service = TaskPlanningService(
                workflow_engine=bound_engine,
                persistence=self.workflow_engine.persistence,
                artifact_store=self.artifact_store,
                task_registry=effective_task_reg,
                event_bus=event_bus or getattr(self.runtime, "events", None),
                model_provider=provider,
                local_supervisor=effective_supervisor,
                desktop_controller=effective_desktop,
            )
            plan_tools = build_plan_tools(
                planning_service=bound_planning_service,
                current_conversation_id=cid
            )
            for pt in plan_tools:
                session_registry.register(pt, permission_boundary=session_boundary)

        # 4g. Desktop Automation Tools (Phase 2E)
        # CRITICAL SECURITY RULE: Do not expose raw desktop tools directly to the Cloud Principal.
        effective_include_desktop = include_desktop_tools and (not effective_is_cloud or nesting_depth > 0)
        if effective_include_desktop:
            from runtime.tools.desktop_tools import register_desktop_tools
            register_desktop_tools(
                session_registry,
                controller=effective_desktop,
            )

        # 4h. Companion Health and Behavioral Routine Tools
        try:
            from runtime.tools.companion_tools import build_companion_tools
            companion_tools = build_companion_tools(desktop_controller=effective_desktop)
            for ct in companion_tools:
                session_registry.register(ct, permission_boundary=session_boundary)
        except Exception as e:
            logger.debug(f"Companion tools registration skipped: {e}")

        # 5. Attach any session-specific custom tools
        if custom_tools:
            for ct in custom_tools:
                session_registry.register(ct, permission_boundary=session_boundary)


        # 7. Context Assembler and Agent Executor
        from runtime.config import is_local_only_mode
        assembler = ContextAssembler(
            trajectory_store=store,
            summary_manager=self.summary_manager,
            max_context_tokens=12288 if is_local_only_mode() else 8192,
            output_headroom=2048,
        )

        task_reg = effective_task_reg
        evt_bridge = None
        if self.runtime and hasattr(self.runtime, "observability") and self.runtime.observability:
            evt_bridge = self.runtime.observability.event_bridge

        executor = AgentExecutor(
            trajectory_store=store,
            event_journal=journal,
            summary_manager=self.summary_manager,
            permission_boundary=session_boundary,
            tool_registry=session_registry,
            model_provider=provider,
            context_assembler=assembler,
            artifact_store=self.artifact_store,
            task_registry=task_reg,
            event_bridge=evt_bridge,
        )

        session = AgentSession(
            conversation_id=cid,
            metadata=meta,
            trajectory_store=store,
            event_journal=journal,
            summary_manager=self.summary_manager,
            tool_registry=session_registry,
            permission_boundary=session_boundary,
            model_provider=provider,
            agent_executor=executor,
            artifact_store=self.artifact_store,
            context_assembler=assembler,
            event_bus=event_bus or getattr(self.runtime, "events", None),
            task_planning_service=bound_planning_service,
            session_role=effective_role,
            is_cloud_principal=effective_is_cloud,
        )

        # 4g. Attach Cloud Principal Local Delegation Tool (Phase 2D)
        if include_delegation_tools and effective_supervisor and nesting_depth == 0 and effective_is_cloud:
            from runtime.tools.delegation_tools import build_delegate_local_work_tool
            del_tool = build_delegate_local_work_tool(
                supervisor=effective_supervisor,
                parent_session=session,
            )
            session_registry.register(del_tool, permission_boundary=session_boundary)

        logger.info(f"Composed AgentSession '{cid}' with {len(session_registry.list_tools())} registered tool(s).")
        return session
