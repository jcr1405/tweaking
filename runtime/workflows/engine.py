"""Presence Deterministic Workflow Engine Core
Coordinates concurrent DAG branch execution, task tree integration,
subsystem adapters, and persistence checkpointing.
"""

import time
import uuid
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, Any, List, Optional, Set, Tuple

from runtime.workflows.models import (
    WorkflowDefinition,
    WorkflowNode,
    WorkflowNodeExecution,
    WorkflowRun,
    WorkflowStatus,
    NodeState,
    ExecutionType,
)
from runtime.workflows.validator import WorkflowValidator
from runtime.workflows.scheduler import WorkflowScheduler
from runtime.workflows.adapters import get_adapter_for_type, NodeExecutionContext, NodeExecutionResult
from runtime.agent_executor import CancellationToken
from runtime.observability.models import TaskType, TaskState

logger = logging.getLogger("presence.workflows.engine")


class WorkflowEngine:
    """Authoritative execution coordinator for deterministic workflow DAGs."""

    def __init__(
        self,
        persistence: Optional[Any] = None,
        artifact_store: Optional[Any] = None,
        tool_registry: Optional[Any] = None,
        process_manager: Optional[Any] = None,
        subagent_manager: Optional[Any] = None,
        task_registry: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        permission_boundary: Optional[Any] = None,
        local_supervisor: Optional[Any] = None,
        desktop_controller: Optional[Any] = None,
    ):
        self.persistence = persistence
        self.artifact_store = artifact_store
        self.tool_registry = tool_registry
        self.process_manager = process_manager
        self.subagent_manager = subagent_manager
        self.task_registry = task_registry
        self.event_bus = event_bus
        self.permission_boundary = permission_boundary
        self.local_supervisor = local_supervisor
        self.desktop_controller = desktop_controller

        self._active_runs: Dict[str, WorkflowRun] = {}
        self._active_schedulers: Dict[str, WorkflowScheduler] = {}
        self._active_cancel_tokens: Dict[str, CancellationToken] = {}
        self._lock = threading.RLock()

    def execute_workflow(
        self,
        definition: WorkflowDefinition,
        conversation_id: str,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> WorkflowRun:
        """Executes a validated workflow DAG synchronously or via thread worker."""
        # 1. Structural DAG Validation
        WorkflowValidator.validate(definition)

        run_id = f"wf_{uuid.uuid4().hex[:12]}"
        cancel_token = cancellation_token or CancellationToken()

        run = WorkflowRun(
            run_id=run_id,
            workflow_id=definition.workflow_id,
            conversation_id=conversation_id,
            status=WorkflowStatus.RUNNING,
            start_time=time.time(),
        )

        with self._lock:
            self._active_runs[run_id] = run
            self._active_cancel_tokens[run_id] = cancel_token

        # 2. Bridge Root Task into ActiveTaskRegistry
        root_task_id = None
        if self.task_registry:
            try:
                root_task = self.task_registry.register_task(
                    task_type=TaskType.WORKFLOW,
                    display_name=f"Workflow: {definition.name}",
                    conversation_id=conversation_id,
                    cancellable=True,
                    cancellation_hook=lambda: self.cancel_workflow(run_id, "Cancelled via task hook"),
                    metadata={"workflow_id": definition.workflow_id, "run_id": run_id}
                )
                self.task_registry.start_task(root_task.task_id)
                root_task_id = root_task.task_id
                run.root_task_id = root_task_id
            except Exception as e:
                logger.warning(f"Failed to register root workflow task in TaskRegistry: {e}")

        # 3. Persist Initial Run Record
        if self.persistence:
            try:
                self.persistence.save_workflow_run(
                    run_id=run.run_id,
                    workflow_id=definition.workflow_id,
                    conversation_id=conversation_id,
                    name=definition.name,
                    status=run.status.value,
                    failure_policy=definition.failure_policy.value,
                    start_time=run.start_time,
                    definition_json=json.dumps(definition.to_dict()),
                )
            except Exception as e:
                logger.error(f"Failed to persist initial workflow run '{run_id}': {e}")

        self._emit_event("WORKFLOW_STARTED", {
            "run_id": run_id,
            "workflow_id": definition.workflow_id,
            "conversation_id": conversation_id,
            "root_task_id": root_task_id,
        })

        scheduler = WorkflowScheduler(definition, run_id)
        with self._lock:
            self._active_schedulers[run_id] = scheduler

        return self._run_scheduler_loop(
            run_id=run_id,
            run=run,
            definition=definition,
            scheduler=scheduler,
            conversation_id=conversation_id,
            cancel_token=cancel_token,
            root_task_id=root_task_id,
        )

    def _run_scheduler_loop(
        self,
        run_id: str,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        scheduler: WorkflowScheduler,
        conversation_id: str,
        cancel_token: CancellationToken,
        root_task_id: Optional[str] = None,
    ) -> WorkflowRun:
        # Populate / refresh node executions in run object
        for nid, ne in scheduler.node_executions.items():
            run.node_executions[nid] = ne

        node_task_ids: Dict[str, str] = {}
        in_flight_futures: Dict[Future, str] = {}

        max_workers = min(definition.max_parallel_nodes, max(1, len(definition.nodes)))
        pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"wf_{run_id[:6]}")

        def _execute_node_worker(node_id: str) -> Tuple[str, NodeExecutionResult]:
            node = scheduler.node_map[node_id]
            adapter = get_adapter_for_type(node.execution_type)

            context = NodeExecutionContext(
                workflow_run_id=run_id,
                conversation_id=conversation_id,
                node_executions=dict(scheduler.node_executions),
                cancellation_token=cancel_token,
                artifact_store=self.artifact_store,
                tool_registry=self.tool_registry,
                process_manager=self.process_manager,
                subagent_manager=self.subagent_manager,
                permission_boundary=self.permission_boundary,
                local_supervisor=self.local_supervisor,
                desktop_controller=self.desktop_controller,
            )

            res = adapter.execute(node, context)
            return node_id, res

        try:
            while not scheduler.is_workflow_terminal():
                if cancel_token.is_cancelled or scheduler.is_cancelled():
                    scheduler.request_cancellation()
                    # Cancel in-flight futures if possible
                    for f in list(in_flight_futures.keys()):
                        f.cancel()
                    break

                # Query scheduler for nodes ready to dispatch
                ready_node_ids = scheduler.get_ready_nodes()

                for nid in ready_node_ids:
                    node = scheduler.node_map[nid]
                    step_task_id = None

                    # Register child task in TaskRegistry
                    if self.task_registry and root_task_id:
                        try:
                            st = self.task_registry.register_task(
                                task_type=TaskType.WORKFLOW_STEP,
                                display_name=f"[{node.execution_type.value}] {node.name}",
                                parent_task_id=root_task_id,
                                conversation_id=conversation_id,
                                metadata={"node_id": nid, "run_id": run_id}
                            )
                            self.task_registry.start_task(st.task_id)
                            step_task_id = st.task_id
                            node_task_ids[nid] = step_task_id
                        except Exception as e:
                            logger.debug(f"Error registering step task for node '{nid}': {e}")

                    scheduler.mark_running(nid, task_id=step_task_id)
                    ne = scheduler.node_executions[nid]
                    run.node_executions[nid] = ne

                    # Checkpoint node state
                    if self.persistence:
                        try:
                            self.persistence.save_workflow_node(
                                run_id=run_id,
                                node_id=nid,
                                name=node.name,
                                execution_type=node.execution_type.value,
                                phase=node.phase,
                                state=ne.state.value,
                                task_id=step_task_id,
                                start_time=ne.start_time,
                            )
                        except Exception as e:
                            logger.debug(f"Error checkpointing running node '{nid}': {e}")

                    self._emit_event("WORKFLOW_NODE_STARTED", {
                        "run_id": run_id,
                        "node_id": nid,
                        "task_id": step_task_id,
                        "execution_type": node.execution_type.value,
                    })

                    future = pool.submit(_execute_node_worker, nid)
                    in_flight_futures[future] = nid

                # If no work is ready and no futures are running, break (all remaining skipped/deadlocked)
                if not ready_node_ids and not in_flight_futures:
                    break

                # Check for timed-out in-flight nodes
                now = time.time()
                timed_out = []
                for f, nid in list(in_flight_futures.items()):
                    node = scheduler.node_map[nid]
                    if node.timeout_seconds and node.timeout_seconds > 0:
                        ne = scheduler.node_executions[nid]
                        if ne.start_time and (now - ne.start_time) > node.timeout_seconds:
                            f.cancel()
                            timed_out.append((f, nid, node.timeout_seconds))

                for f, nid, t_sec in timed_out:
                    in_flight_futures.pop(f, None)
                    node = scheduler.node_map[nid]
                    node_task_id = node_task_ids.get(nid)
                    err_msg = f"Node '{nid}' timed out after {t_sec}s."
                    scheduler.mark_failed(
                        nid,
                        error_message=err_msg,
                    )
                    if self.task_registry and node_task_id:
                        self.task_registry.fail_task(
                            node_task_id,
                            error_message=err_msg,
                            metadata={"exit_code": 124},
                        )
                    self._emit_event("WORKFLOW_NODE_FAILED", {
                        "run_id": run_id,
                        "node_id": nid,
                        "error": err_msg,
                    })

                # Wait for any running future to complete
                done_futures = [f for f in in_flight_futures if f.done()]
                if not done_futures:
                    time.sleep(0.05)
                    continue

                for df in done_futures:
                    nid = in_flight_futures.pop(df)
                    node = scheduler.node_map[nid]
                    node_task_id = node_task_ids.get(nid)

                    try:
                        _, exec_res = df.result()
                    except Exception as err:
                        exec_res = NodeExecutionResult(
                            state=NodeState.FAILED,
                            error_message=f"Unhandled thread execution exception: {err}"
                        )

                    # Update scheduler state
                    if exec_res.state == NodeState.COMPLETED:
                        scheduler.mark_completed(
                            nid,
                            result_payload=exec_res.result_payload,
                            artifact_id=exec_res.artifact_id,
                        )
                        if self.task_registry and node_task_id:
                            self.task_registry.complete_task(
                                node_task_id,
                                artifact_id=exec_res.artifact_id,
                                metadata={"exit_code": 0},
                            )
                        self._emit_event("WORKFLOW_NODE_COMPLETED", {
                            "run_id": run_id,
                            "node_id": nid,
                            "artifact_id": exec_res.artifact_id,
                        })

                    elif exec_res.state == NodeState.WAITING_FOR_APPROVAL:
                        scheduler.mark_waiting_approval(
                            nid,
                            error_message=exec_res.error_message or "Node awaiting user approval",
                            result_payload=exec_res.result_payload,
                            artifact_id=exec_res.artifact_id,
                        )
                        if self.task_registry and node_task_id:
                            task = self.task_registry.get_task(node_task_id)
                            if task:
                                task.metadata["waiting_for_approval"] = True
                                task.metadata["waiting_reason"] = exec_res.error_message or "Awaiting approval"
                        self._emit_event("WORKFLOW_NODE_WAITING_APPROVAL", {
                            "run_id": run_id,
                            "node_id": nid,
                            "reason": exec_res.error_message,
                        })

                    elif exec_res.state == NodeState.CANCELLED:
                        ne = scheduler.node_executions[nid]
                        ne.state = NodeState.CANCELLED
                        ne.error_message = exec_res.error_message
                        ne.end_time = time.time()
                        if self.task_registry and node_task_id:
                            self.task_registry.cancel_task(node_task_id, reason=exec_res.error_message or "Cancelled")

                    else:
                        ne = scheduler.node_executions[nid]
                        # Idempotency and Side-Effect Safety Audit (Section 8.3 & SYS-23)
                        # Distinguish read-only/idempotent operations from non-idempotent actions
                        payload = node.payload or {}
                        is_explicit_idempotent = payload.get("idempotent")
                        is_side_effecting = payload.get("side_effecting", False)
                        is_desktop_input = (
                            node.execution_type == ExecutionType.DESKTOP_OPERATION
                            and str(payload.get("action_type", "")).lower() in ("click_coordinate", "type_text", "press_key", "hotkey", "scroll")
                        )
                        is_safe_to_retry = True
                        if is_explicit_idempotent is False or is_side_effecting or (is_desktop_input and is_explicit_idempotent is not True):
                            is_safe_to_retry = False
                            logger.warning(
                                f"Node '{nid}' is non-idempotent or side-effecting; suppressing blind retry after failure."
                            )

                        if is_safe_to_retry and ne.retry_count < node.max_retries:
                            ne.retry_count += 1
                            logger.info(f"Retrying node '{nid}' (attempt {ne.retry_count}/{node.max_retries})...")
                            if node.retry_backoff_seconds > 0:
                                time.sleep(min(1.0, node.retry_backoff_seconds))
                            future = pool.submit(_execute_node_worker, nid)
                            in_flight_futures[future] = nid
                            continue

                        scheduler.mark_failed(
                            nid,
                            error_message=exec_res.error_message or "Node execution failed",
                            result_payload=exec_res.result_payload,
                            artifact_id=exec_res.artifact_id,
                        )
                        if self.task_registry and node_task_id:
                            self.task_registry.fail_task(
                                node_task_id,
                                error_message=exec_res.error_message or "Node execution failed",
                                metadata={"exit_code": 1},
                            )
                        self._emit_event("WORKFLOW_NODE_FAILED", {
                            "run_id": run_id,
                            "node_id": nid,
                            "error": exec_res.error_message,
                        })

                    # Update and persist completed node checkpoint
                    ne = scheduler.node_executions[nid]
                    run.node_executions[nid] = ne
                    if self.persistence:
                        try:
                            self.persistence.save_workflow_node(
                                run_id=run_id,
                                node_id=nid,
                                name=node.name,
                                execution_type=node.execution_type.value,
                                phase=node.phase,
                                state=ne.state.value,
                                task_id=ne.task_id,
                                start_time=ne.start_time,
                                end_time=ne.end_time,
                                duration_seconds=ne.duration_seconds,
                                result_payload=ne.result_payload,
                                artifact_id=ne.artifact_id,
                                error_message=ne.error_message,
                            )
                        except Exception as e:
                            logger.error(f"Error persisting node completion for '{nid}': {e}")

        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # 4. Finalize Workflow Status and Metrics
        run.end_time = time.time()
        run.duration_seconds = max(0.0, run.end_time - (run.start_time or run.end_time))
        run.status = scheduler.get_final_status()

        # Identify final artifact deliverable if present (from AGGREGATE node or final node)
        for nid, ne in run.node_executions.items():
            node = scheduler.node_map.get(nid)
            if node and node.execution_type == ExecutionType.AGGREGATE and ne.artifact_id:
                run.final_artifact_id = ne.artifact_id

        if not run.final_artifact_id:
            # Fallback to the latest produced artifact
            for nid in reversed(list(run.node_executions.keys())):
                ne = run.node_executions[nid]
                if ne.artifact_id:
                    run.final_artifact_id = ne.artifact_id
                    break

        # Finalize root task in TaskRegistry
        if self.task_registry and root_task_id:
            try:
                if run.status == WorkflowStatus.COMPLETED:
                    self.task_registry.complete_task(
                        root_task_id,
                        artifact_id=run.final_artifact_id,
                        metadata={"exit_code": 0},
                    )
                elif run.status == WorkflowStatus.CANCELLED:
                    self.task_registry.cancel_task(root_task_id, reason="Workflow cancelled")
                elif run.status == WorkflowStatus.AWAITING_APPROVAL:
                    task = self.task_registry.get_task(root_task_id)
                    if task:
                        task.metadata["waiting_for_approval"] = True
                        task.metadata["waiting_reason"] = "Workflow paused awaiting user approval"
                else:
                    self.task_registry.fail_task(
                        root_task_id,
                        error_message="One or more workflow nodes failed",
                        metadata={"exit_code": 1},
                    )
            except Exception as e:
                logger.debug(f"Error finalizing root workflow task: {e}")

        # Update final record in Persistence
        if self.persistence:
            try:
                self.persistence.update_workflow_run_status(
                    run_id=run.run_id,
                    status=run.status.value,
                    end_time=run.end_time,
                    duration_seconds=run.duration_seconds,
                    final_artifact_id=run.final_artifact_id,
                    error_message=run.error_message,
                )
            except Exception as e:
                logger.error(f"Failed to persist final workflow status: {e}")

        if run.status == WorkflowStatus.COMPLETED:
            ev_name = "WORKFLOW_COMPLETED"
        elif run.status == WorkflowStatus.AWAITING_APPROVAL:
            ev_name = "WORKFLOW_AWAITING_APPROVAL"
        else:
            ev_name = "WORKFLOW_FAILED"

        self._emit_event(ev_name, {
            "run_id": run_id,
            "workflow_id": definition.workflow_id,
            "status": run.status.value,
            "duration_seconds": run.duration_seconds,
            "final_artifact_id": run.final_artifact_id,
        })

        with self._lock:
            if run.status != WorkflowStatus.AWAITING_APPROVAL:
                self._active_schedulers.pop(run_id, None)
                self._active_cancel_tokens.pop(run_id, None)

        return run

    def cancel_workflow(self, run_id: str, reason: str = "User cancelled") -> bool:
        """Cooperatively cancels an active workflow execution."""
        with self._lock:
            cancel_token = self._active_cancel_tokens.get(run_id)
            scheduler = self._active_schedulers.get(run_id)
            if cancel_token:
                cancel_token.cancel()
            if scheduler:
                scheduler.request_cancellation()
                return True
        return False

    def resume_workflow(
        self,
        run_id: str,
        consent_tokens: Optional[Dict[str, str]] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> WorkflowRun:
        """Resumes a workflow run that paused in AWAITING_APPROVAL status with verified consent tokens."""
        with self._lock:
            scheduler = self._active_schedulers.get(run_id)
            run = self._active_runs.get(run_id)
            if not run:
                run = self.get_run(run_id)

        if not run:
            raise ValueError(f"Workflow run '{run_id}' not found.")

        if run.status != WorkflowStatus.AWAITING_APPROVAL:
            logger.warning(f"Workflow '{run_id}' is not awaiting approval (current status: {run.status.value}).")
            return run

        if not scheduler:
            # Reconstruct scheduler from persistence
            rec = self.persistence.get_workflow_run(run_id) if self.persistence else None
            if not rec or not rec.get("definition_json"):
                raise RuntimeError(f"Cannot resume workflow '{run_id}': definition not found.")
            def_dict = json.loads(rec["definition_json"])
            from runtime.workflows.models import WorkflowDefinition
            definition = WorkflowDefinition.from_dict(def_dict)
            scheduler = WorkflowScheduler(definition, run_id)
            for nid, ne in run.node_executions.items():
                if nid in scheduler.node_executions:
                    scheduler.node_executions[nid] = ne
            with self._lock:
                self._active_schedulers[run_id] = scheduler

        # Apply consent tokens to any nodes in WAITING_FOR_APPROVAL
        waiting_nodes = [
            nid for nid, ne in scheduler.node_executions.items()
            if ne.state == NodeState.WAITING_FOR_APPROVAL
        ]
        tokens_map = consent_tokens or {}
        for nid in waiting_nodes:
            token = tokens_map.get(nid) or next(iter(tokens_map.values()), None)
            if token:
                scheduler.resume_node_approval(nid, token)
            else:
                logger.warning(f"No consent token provided for node '{nid}'; remains awaiting approval.")

        run.status = WorkflowStatus.RUNNING
        cancel_token = cancellation_token or self._active_cancel_tokens.get(run_id) or CancellationToken()
        with self._lock:
            self._active_cancel_tokens[run_id] = cancel_token

        return self._run_scheduler_loop(
            run_id=run_id,
            run=run,
            definition=scheduler.definition,
            scheduler=scheduler,
            conversation_id=run.conversation_id,
            cancel_token=cancel_token,
            root_task_id=getattr(run, "root_task_id", None),
        )

    def reject_workflow_approval(self, run_id: str, reason: str = "User rejected consent") -> WorkflowRun:
        """Rejects approval for a paused workflow, cancelling waiting nodes and failing closed."""
        with self._lock:
            scheduler = self._active_schedulers.get(run_id)
            run = self._active_runs.get(run_id)
            if not run:
                run = self.get_run(run_id)

        if not run:
            raise ValueError(f"Workflow run '{run_id}' not found.")

        if scheduler:
            waiting_nodes = [
                nid for nid, ne in scheduler.node_executions.items()
                if ne.state == NodeState.WAITING_FOR_APPROVAL
            ]
            for nid in waiting_nodes:
                scheduler.cancel_node_approval(nid, reason=reason)
            run.status = scheduler.get_final_status()
        else:
            run.status = WorkflowStatus.CANCELLED

        run.end_time = time.time()
        run.error_message = reason

        if self.persistence:
            try:
                self.persistence.update_workflow_run_status(
                    run_id=run_id,
                    status=run.status.value,
                    end_time=run.end_time,
                    duration_seconds=run.duration_seconds,
                    final_artifact_id=run.final_artifact_id,
                    error_message=reason,
                )
            except Exception as e:
                logger.error(f"Failed to update rejected workflow status: {e}")

        self._emit_event("WORKFLOW_CANCELLED", {
            "run_id": run_id,
            "reason": reason,
        })
        with self._lock:
            self._active_schedulers.pop(run_id, None)
            self._active_cancel_tokens.pop(run_id, None)
        return run

    def get_run(self, run_id: str) -> Optional[WorkflowRun]:
        with self._lock:
            if run_id in self._active_runs:
                return self._active_runs[run_id]
        if self.persistence:
            rec = self.persistence.get_workflow_run(run_id)
            if rec:
                # Reconstruct WorkflowRun from persisted record
                run = WorkflowRun(
                    run_id=rec["run_id"],
                    workflow_id=rec["workflow_id"],
                    conversation_id=rec["conversation_id"],
                    status=WorkflowStatus(rec["status"]),
                    start_time=rec["start_time"],
                    end_time=rec["end_time"],
                    duration_seconds=rec["duration_seconds"],
                    final_artifact_id=rec["final_artifact_id"],
                    error_message=rec["error_message"],
                )
                for nd in rec.get("nodes", []):
                    ne = WorkflowNodeExecution(
                        node_id=nd["node_id"],
                        workflow_run_id=run.run_id,
                        state=NodeState(nd["state"]),
                        task_id=nd["task_id"],
                        start_time=nd["start_time"],
                        end_time=nd["end_time"],
                        duration_seconds=nd["duration_seconds"],
                        result_payload=nd["result_payload"],
                        artifact_id=nd["artifact_id"],
                        error_message=nd["error_message"],
                    )
                    run.node_executions[nd["node_id"]] = ne
                return run
        return None

    def list_runs(self, conversation_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if self.persistence:
            return self.persistence.list_workflow_runs(conversation_id=conversation_id, limit=limit)
        return []

    def _emit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.event_bus:
            try:
                self.event_bus.publish(event_type, payload)
            except Exception as e:
                logger.debug(f"Failed to publish workflow event '{event_type}': {e}")
