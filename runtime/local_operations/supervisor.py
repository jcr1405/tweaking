"""Local Operations Supervisor Core
Coordinates autonomous operational execution on the local model (Qwen 3.5 9B via Ollama).
Enforces scoped execution sessions, strict non-bypassable read-only permission boundaries,
zero-cloud-inference pinning, deterministic completion verification, and structured result packaging.
"""

import time
import uuid
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from runtime.local_operations.models import (
    TaskDelegationContract,
    TaskDelegationResult,
    DelegationOutcome,
    ExecutionProfile,
    VerifiedEvidenceItem,
    OutcomeStatus,
    outcome_to_status,
    VerifiedFinding,
    ModelInterpretation,
    Contradiction,
    FailureRecord,
    SubagentReceipt,
    ArtifactReference,
    EscalationRequirement,
    CompactionMetrics,
    CloudReturnPacket,
)
from runtime.local_operations.compactor import (
    ToolOutputCompactor,
    SubagentResultCompactor,
    BudgetManager,
)
from runtime.local_operations.semantic_compactor import SemanticCompactor
from runtime.local_operations.validator import PacketValidator
from runtime.permissions import (
    PermissionBoundary,
    PermissionTier,
    AuthorizationResult,
    ConsentProvider,
)
from runtime.session import AgentSession, AgentSessionFactory
from runtime.agent_executor import CancellationToken, AgentExecutionEvent
from runtime.providers.coordinator import MultiProviderCoordinator, InferenceRequestConfig
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.observability.models import TaskType, TaskState
from runtime.events import EventBus
from runtime.artifacts.store import ArtifactStore
from runtime.trajectory import StepType

logger = logging.getLogger("presence.local_operations.supervisor")


def is_tool_allowed_by_capabilities(tool_name: str, allowed_capabilities: List[str]) -> bool:
    """Evaluates whether tool_name conforms to allowed capability scopes."""
    if not allowed_capabilities:
        return True

    clean_tool = tool_name.strip().lower()

    for cap in allowed_capabilities:
        c = cap.strip().lower()
        if c in ("*", "all", "all_read_only"):
            return True

        if clean_tool == c:
            return True

        tool_stripped = clean_tool
        if tool_stripped.startswith("mcp."):
            tool_stripped = tool_stripped[4:]
        elif tool_stripped.startswith("presence."):
            tool_stripped = tool_stripped[9:]

        c_stripped = c
        if c_stripped.startswith("mcp."):
            c_stripped = c_stripped[4:]
        elif c_stripped.startswith("presence."):
            c_stripped = c_stripped[9:]

        if tool_stripped == c_stripped:
            return True

        if tool_stripped.startswith(c_stripped + "."):
            return True
        if clean_tool.startswith(c + "."):
            return True

        if c_stripped == "filesystem" and ("file" in tool_stripped or "directory" in tool_stripped):
            return True
        if c_stripped in ("code_search", "search") and any(k in tool_stripped for k in ("search", "find", "grep")):
            return True
        if c_stripped == "desktop" and tool_stripped.startswith("desktop"):
            return True
        if c_stripped == "subagents" and any(k in tool_stripped for k in ("subagent", "agent")):
            return True

    return False


class ScopedPermissionBoundary(PermissionBoundary):
    """Authoritative scoped permission boundary that strictly enforces delegation constraints:
    Effective Permission = Parent Session Permissions ∩ Contract Constraints ∩ Allowed Capabilities.
    Immune to prompt overrides or model privilege escalation.
    """

    def __init__(
        self,
        parent_boundary: PermissionBoundary,
        read_only: bool = True,
        max_allowed_tier: PermissionTier = PermissionTier.L0_OBSERVE,
        allowed_capabilities: Optional[List[str]] = None,
        consent_provider: Optional[ConsentProvider] = None,
        subagents_allowed: bool = False,
    ):
        self.parent_boundary = parent_boundary
        self.read_only = read_only
        self.max_allowed_tier = max_allowed_tier
        self.allowed_capabilities = [c.lower() for c in (allowed_capabilities or [])]
        self.subagents_allowed = subagents_allowed
        super().__init__(consent_provider=consent_provider or parent_boundary._consent_provider)

        # Inherit parent policies
        with parent_boundary._lock:
            self._policies = dict(parent_boundary._policies)

        if self.subagents_allowed:
            from runtime.permissions import ActionPolicy
            for sub_act in (
                "presence.invoke_subagent",
                "presence.manage_subagents",
                "presence.send_subagent_message",
                "invoke_subagent",
                "manage_subagents",
                "send_subagent_message",
            ):
                if sub_act not in self._policies:
                    pol = ActionPolicy(
                        action_name=sub_act,
                        required_level=PermissionTier.L2_EXECUTE_REVERSIBLE,
                        description="Subagent coordination",
                    )
                    self._policies[sub_act] = pol
                    if hasattr(self, "parent_boundary") and self.parent_boundary:
                        self.parent_boundary.register_policy(pol)

    def register_policy(self, policy: Any) -> None:
        """Registers policy on both scoped boundary and parent boundary."""
        with self._lock:
            self._policies[policy.action_name] = policy
        if hasattr(self, "parent_boundary") and self.parent_boundary:
            self.parent_boundary.register_policy(policy)

    def check_permission(
        self,
        action_name: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        granted_consent: bool = False,
        tool_name: Optional[str] = None,
    ) -> AuthorizationResult:
        """Evaluates whether an action is permitted under the intersected authority matrix."""
        effective_name = action_name or tool_name or "unknown"

        # Ensure parent has policy if registered locally
        policy = self._policies.get(effective_name)
        if policy and effective_name not in self.parent_boundary._policies:
            self.parent_boundary.register_policy(policy)

        # 1. Authorize against parent permission boundary
        parent_auth = self.parent_boundary.check_permission(
            action_name=effective_name,
            context=context,
            granted_consent=granted_consent,
        )
        if not parent_auth.allowed:
            return parent_auth

        # 1b. Special authorization for subagent coordination tools if explicitly permitted by contract
        if self.subagents_allowed and effective_name in (
            "presence.invoke_subagent",
            "presence.send_subagent_message",
            "presence.manage_subagents",
            "invoke_subagent",
            "send_subagent_message",
            "manage_subagents",
        ):
            return AuthorizationResult(
                allowed=True,
                action_name=effective_name,
                level=parent_auth.level,
                reason="Subagent coordination permitted by delegation contract.",
            )

        # 1c. Enforce capability restrictions from delegation contract
        if self.allowed_capabilities and not any(c in ("*", "all", "all_read_only") for c in self.allowed_capabilities):
            if not is_tool_allowed_by_capabilities(effective_name, self.allowed_capabilities):
                msg = (
                    f"Operation '{effective_name}' is not permitted by delegation contract "
                    f"allowed_capabilities ({self.allowed_capabilities})."
                )
                logger.warning(f"Scoped permission capability blocked: {msg}")
                return AuthorizationResult(
                    allowed=False,
                    action_name=effective_name,
                    level=parent_auth.level,
                    reason=msg,
                )

        # 2. Enforce strict read-only constraint
        if self.read_only and parent_auth.level > PermissionTier.L0_OBSERVE:
            msg = (
                f"Operation '{effective_name}' requires authority tier {parent_auth.level.name}, "
                f"but delegation contract enforces strict read-only execution (L0_OBSERVE only)."
            )
            logger.warning(f"Scoped permission blocked: {msg}")
            return AuthorizationResult(
                allowed=False,
                action_name=effective_name,
                level=parent_auth.level,
                reason=msg,
            )

        # 3. Enforce maximum tier ceiling
        if parent_auth.level > self.max_allowed_tier:
            msg = (
                f"Operation '{effective_name}' requires {parent_auth.level.name}, which exceeds "
                f"the delegated maximum tier {self.max_allowed_tier.name}."
            )
            logger.warning(f"Scoped permission ceiling blocked: {msg}")
            return AuthorizationResult(
                allowed=False,
                action_name=effective_name,
                level=parent_auth.level,
                reason=msg,
            )

        return parent_auth


class LocalOperationsSupervisor:
    """Central service managing the Local Operations Supervisor (Qwen 3.5 9B).
    Accepts validated delegation contracts, drives local multi-turn execution sessions,
    and guarantees zero-cloud-inference execution.
    """

    def __init__(
        self,
        session_factory: AgentSessionFactory,
        coordinator: Optional[MultiProviderCoordinator] = None,
        task_registry: Optional[ActiveTaskRegistry] = None,
        event_bus: Optional[EventBus] = None,
        artifact_store: Optional[ArtifactStore] = None,
        subagent_manager: Optional[Any] = None,
        desktop_controller: Optional[Any] = None,
    ):
        self.session_factory = session_factory
        self.coordinator = coordinator or getattr(session_factory, "default_model_provider", None)
        self.task_registry = task_registry or session_factory.task_registry
        self.event_bus = event_bus
        self.artifact_store = artifact_store or session_factory.artifact_store
        self.subagent_manager = subagent_manager
        self.desktop_controller = desktop_controller

    def execute(
        self,
        contract: TaskDelegationContract,
        parent_session: Optional[AgentSession] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> TaskDelegationResult:
        """Executes a delegated task contract to completion through local Qwen inference.

        Guarantees:
        1. Zero cloud inference usage during local execution.
        2. Execution occurs in an isolated task-scoped AgentSession.
        3. PermissionBoundary enforces contract read_only constraints.
        4. Completion is verified against actual execution evidence.
        5. Returns structured TaskDelegationResult.
        """
        start_time = time.time()
        task_id = contract.task_id
        parent_cid = contract.parent_conversation_id

        # 1. Authoritative Parent Session & Identity Validation
        if parent_session:
            if parent_session.conversation_id != parent_cid:
                raise ValueError(
                    f"Parent conversation ID spoofing detected: Contract specifies '{parent_cid}', "
                    f"but active parent session is '{parent_session.conversation_id}'."
                )
        else:
            # Standalone invocation: verify that parent_cid corresponds to an existing conversation
            conv_exists = False
            if hasattr(self.session_factory, "conversations_dir") and self.session_factory.conversations_dir:
                conv_db = Path(self.session_factory.conversations_dir) / f"{parent_cid}.db"
                if conv_db.exists():
                    conv_exists = True
            if not conv_exists and hasattr(self.session_factory, "summary_manager") and self.session_factory.summary_manager:
                if self.session_factory.summary_manager.get_summary(parent_cid):
                    conv_exists = True
            if not conv_exists:
                raise ValueError(f"Parent conversation '{parent_cid}' does not exist.")

        # 2. Validate Task ID Uniqueness in ActiveTaskRegistry
        if self.task_registry and hasattr(self.task_registry, "get_task"):
            existing = self.task_registry.get_task(task_id)
            if existing and existing.state in (TaskState.QUEUED, TaskState.RUNNING):
                raise ValueError(f"Task with ID '{task_id}' is already active.")

        # 2. Allocate Task Identity & Child Cancellation Token
        child_cid = f"sup_task_{task_id}_{uuid.uuid4().hex[:8]}"
        local_cancel_token = CancellationToken()

        if cancellation_token:
            # Propagate parent cancellation into local token
            def _check_parent():
                if cancellation_token.is_cancelled:
                    local_cancel_token.cancel()
        else:
            _check_parent = lambda: None

        # 3. Register Task in ActiveTaskRegistry
        if self.task_registry:
            try:
                self.task_registry.register_task(
                    task_id=task_id,
                    task_type=TaskType.OPERATIONAL_SUPERVISOR,
                    display_name=f"Local Supervisor: {contract.objective[:40]}",
                    conversation_id=parent_cid,
                    cancellable=True,
                    cancellation_hook=local_cancel_token.cancel,
                    metadata={
                        "task_id": task_id,
                        "child_conversation_id": child_cid,
                        "execution_profile": contract.execution_profile.value,
                        "read_only": contract.constraints.read_only,
                    },
                )
                self.task_registry.start_task(task_id)
            except Exception as tr_err:
                logger.warning(f"Failed to register task in ActiveTaskRegistry: {tr_err}")

        if self.event_bus:
            try:
                self.event_bus.publish("SUPERVISOR_STARTED", {
                    "task_id": task_id,
                    "parent_conversation_id": parent_cid,
                    "child_conversation_id": child_cid,
                    "objective": contract.objective,
                    "profile": contract.execution_profile.value,
                })
            except Exception:
                pass

        # 4. Resolve Effective Permission Boundary
        parent_boundary = parent_session.permission_boundary if parent_session else PermissionBoundary()
        max_tier = (
            PermissionTier.L0_OBSERVE
            if contract.constraints.read_only
            else PermissionTier.L2_EXECUTE_REVERSIBLE
        )
        scoped_boundary = ScopedPermissionBoundary(
            parent_boundary=parent_boundary,
            read_only=contract.constraints.read_only,
            max_allowed_tier=max_tier,
            allowed_capabilities=contract.allowed_capabilities,
            subagents_allowed=contract.constraints.subagents_allowed,
        )

        # 5. Create Isolated Task-Scoped AgentSession
        child_session = self.session_factory.create_session(
            conversation_id=child_cid,
            title=f"Supervisor [{task_id}]: {contract.objective[:30]}",
            parent_conversation_id=parent_cid,
            nesting_depth=1,
            permission_boundary=scoped_boundary,
            include_default_tools=True,
            include_mcp_tools=True,
            include_search_tools=True,
            include_task_tools=False,
            include_subagent_tools=False,
            include_workflow_tools=False,
            include_delegation_tools=False,
            session_role="local_supervisor",
            is_cloud_principal=False,
            event_bus=self.event_bus,
        )
        # Ensure delegation tool is strictly excluded from local supervisor sessions
        child_session.tool_registry.unregister("presence.delegate_local_work")
        child_session.tool_registry.unregister("delegate_local_work")

        # 5a. Prune tools from registry that do not match allowed_capabilities
        if contract.allowed_capabilities and not any(c.lower() in ("*", "all", "all_read_only") for c in contract.allowed_capabilities):
            registered_tools = list(child_session.tool_registry.list_tools())
            for t in registered_tools:
                t_name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else str(t))
                if t_name and not is_tool_allowed_by_capabilities(t_name, contract.allowed_capabilities):
                    child_session.tool_registry.unregister(t_name)

        # 5b. Register Subagent Tools if allowed by delegation contract
        if contract.constraints.subagents_allowed:
            mgr = self.subagent_manager
            if mgr is None:
                from runtime.subagents.manager import SubagentManager
                mgr = SubagentManager(
                    session_factory=self.session_factory,
                    task_registry=self.task_registry,
                    event_bus=self.event_bus,
                    artifact_store=self.artifact_store,
                    max_subagents_per_parent=contract.constraints.max_subagents,
                )
                self.subagent_manager = mgr

            from runtime.tools.subagent_tools import build_subagent_tools
            sub_tools = build_subagent_tools(
                subagent_manager=mgr,
                current_conversation_id=child_cid,
                pin_local_ollama=True,
                max_parent_subagents=contract.constraints.max_subagents,
            )
            for st in sub_tools:
                child_session.tool_registry.register(st)

        # 5c. Register Desktop Tools if allowed by delegation contract
        allowed_caps = {c.lower() for c in contract.allowed_capabilities}
        if (
            "desktop" in allowed_caps
            or "desktop.*" in allowed_caps
            or any("desktop" in c for c in allowed_caps)
            or self.desktop_controller is not None
        ):
            from runtime.tools.desktop_tools import register_desktop_tools
            register_desktop_tools(child_session.tool_registry, controller=self.desktop_controller)

        # 6. Apply Capability and Anti-Recursion Filter on Tool Registry
        self._filter_supervisor_tools(
            child_session,
            contract.allowed_capabilities,
            subagents_allowed=contract.constraints.subagents_allowed,
        )

        # 7. Pin Local Inference Configuration (Strict Zero-Cloud Guarantee)
        profile_mode = (
            "reasoning"
            if contract.execution_profile == ExecutionProfile.LOCAL_SUPERVISOR_DEEP
            else "routine"
        )
        request_config = InferenceRequestConfig(
            provider="ollama",
            model="qwen3.5:9b",
            mode=profile_mode,
            purpose="local_supervisor_execution",
            fallback_policy="no_fallback",
            cancellation_token=local_cancel_token,
            conversation_id=child_cid,
            timeout_seconds=contract.constraints.timeout_seconds,
        )

        # 8. Drive Multi-Turn Operational Execution Loop
        system_prompt = (
            "You are Presence's Local Operations Supervisor powered by local Qwen.\n"
            "You execute operational computer-use and desktop interaction objectives.\n"
            "Understand the user's desktop state, navigate Windows applications, read state, verify UI elements, and execute actions.\n"
            "Operate strictly within your assigned constraints and task scopes.\n"
            "When the objective is satisfied, summarize the verified findings and actions taken clearly."
        )

        user_prompt = self._build_supervisor_prompt(contract)

        executed_tools: List[Dict[str, Any]] = []
        blocked_tools: List[Dict[str, Any]] = []
        turn_events: List[AgentExecutionEvent] = []
        final_response_text = ""
        timed_out = False
        budget_exhausted = False
        local_inferences = 0

        # Token usage accounting
        total_tokens_used = 0
        prompt_tokens_used = 0
        completion_tokens_used = 0
        estimated_prompt_tokens = max(1, len(user_prompt + system_prompt) // 4)
        estimated_completion_tokens = 0

        try:
            deadline = time.time() + contract.constraints.timeout_seconds
            event_gen = child_session.execute_turn(
                user_input=user_prompt,
                system_prompt=system_prompt,
                cancellation_token=local_cancel_token,
                max_tool_iterations=contract.constraints.max_tool_cycles,
                request_config=request_config,
            )

            for event in event_gen:
                _check_parent()
                if time.time() > deadline:
                    timed_out = True
                    local_cancel_token.cancel()
                    break

                turn_events.append(event)
                if event.event_type == "TEXT_DELTA":
                    delta = event.payload.get("delta", "")
                    final_response_text += delta
                    estimated_completion_tokens += max(1, len(delta) // 4)
                elif event.event_type == "TOOL_COMPLETED":
                    executed_tools.append(event.payload)
                elif event.event_type == "TOOL_FAILED":
                    err_msg = str(event.payload.get("error", ""))
                    if "read-only execution" in err_msg or "permission" in err_msg.lower():
                        blocked_tools.append(event.payload)
                elif event.event_type == "USAGE_UPDATED":
                    p_tok = event.payload.get("prompt_tokens", 0)
                    c_tok = event.payload.get("completion_tokens", 0)
                    t_tok = event.payload.get("total_tokens", p_tok + c_tok)
                    prompt_tokens_used += p_tok
                    completion_tokens_used += c_tok
                    total_tokens_used += t_tok
                    local_inferences += 1

                # Token budget enforcement across iterations
                current_tokens = total_tokens_used if total_tokens_used > 0 else (estimated_prompt_tokens + estimated_completion_tokens)
                if current_tokens >= contract.constraints.max_tokens_budget:
                    budget_exhausted = True
                    local_cancel_token.cancel()
                    break

        except Exception as exec_err:
            logger.error(f"Error during supervisor execution for task '{task_id}': {exec_err}")
            final_response_text += f"\n[Execution error: {exec_err}]"

        duration = time.time() - start_time
        current_tokens = total_tokens_used if total_tokens_used > 0 else (estimated_prompt_tokens + estimated_completion_tokens)
        if local_inferences == 0:
            local_inferences = max(1, len(executed_tools) + (1 if final_response_text else 0))

        # 9. Harvest Evidence from Trajectory
        steps = child_session.trajectory_store.list_steps()
        evidence_items, artifacts_created = self._extract_evidence(steps)

        # 10. Deterministic Completion Verification
        outcome, summary, unresolved = self._verify_completion(
            contract=contract,
            steps=steps,
            executed_tools=executed_tools,
            blocked_tools=blocked_tools,
            final_text=final_response_text,
            is_cancelled=local_cancel_token.is_cancelled,
            is_timed_out=timed_out,
            is_budget_exhausted=budget_exhausted,
            effective_tokens=current_tokens,
        )

        # Ensure any subagents spawned by this task are killed if cancelled or timed out
        if (local_cancel_token.is_cancelled or timed_out) and self.subagent_manager:
            try:
                self.subagent_manager.kill_all(parent_conversation_id=child_cid)
            except Exception as k_err:
                logger.warning(f"Error terminating subagents for supervisor task '{task_id}': {k_err}")

        # Harvest subagent receipts
        subagent_results: List[Dict[str, Any]] = []
        if self.subagent_manager:
            subagent_results = self.subagent_manager.list_subagents(
                parent_conversation_id=child_cid,
                include_completed=True,
            )

        # 11. Context Compaction & CloudReturnPacket Assembly (Phase 2C)
        tool_compactor = ToolOutputCompactor(artifact_store=self.artifact_store)
        subagent_compactor = SubagentResultCompactor()
        budget_manager = BudgetManager()
        validator = PacketValidator(artifact_store=self.artifact_store)

        all_verified_findings: List[VerifiedFinding] = []
        all_failures: List[FailureRecord] = []
        artifact_references: List[ArtifactReference] = []
        total_raw_bytes = 0

        # Collect failures from blocked tools
        for bt in blocked_tools:
            t_name = bt.get("name", "tool")
            err = bt.get("error", "Operation blocked by read-only permission boundary")
            all_failures.append(
                FailureRecord(
                    failure_type="PERMISSION_DENIED",
                    error_message=str(err),
                    source_tool_or_subagent=t_name,
                    evidence_refs=[f"permission_boundary:{t_name}"],
                    resolved=False,
                )
            )

        # Compact each tool result from trajectory steps
        for s in steps:
            if s.step_type == StepType.TOOL_RESULT:
                p = s.step_payload or {}
                t_name = p.get("name", "tool")
                t_call_id = p.get("tool_call_id", f"call_{s.idx}")
                raw_content = p.get("content", "")
                exit_code = p.get("exit_code")

                # Check for existing artifact ID from AgentExecutor offloading
                art_id = p.get("artifact_id")
                if art_id and self.artifact_store:
                    try:
                        # Ensure artifact is indexed under parent conversation ID
                        if not self.artifact_store.get_metadata(parent_cid, art_id):
                            c_path, _ = self.artifact_store._get_artifact_files(child_cid, art_id)
                            if c_path.exists():
                                with open(c_path, "r", encoding="utf-8", errors="replace") as af:
                                    c_text = af.read()
                                promoted_meta = self.artifact_store.store_text_artifact(
                                    conversation_id=parent_cid,
                                    tool_call_id=t_call_id,
                                    tool_name=t_name,
                                    content_str=c_text,
                                )
                                art_id = promoted_meta.artifact_id
                    except Exception as prom_err:
                        logger.debug(f"Could not promote artifact from child to parent: {prom_err}")

                f_items, fail_items, art_ref = tool_compactor.compact_tool_output(
                    tool_name=t_name,
                    tool_call_id=t_call_id,
                    content=raw_content,
                    conversation_id=parent_cid,
                    step_index=s.idx,
                    exit_code=exit_code,
                    artifact_id=art_id,
                )
                all_verified_findings.extend(f_items)
                all_failures.extend(fail_items)
                if art_ref:
                    artifact_references.append(art_ref)
                    total_raw_bytes += art_ref.byte_size
                    if art_ref.artifact_id not in artifacts_created:
                        artifacts_created.append(art_ref.artifact_id)
                else:
                    if isinstance(raw_content, str):
                        total_raw_bytes += len(raw_content.encode("utf-8"))
                    elif isinstance(raw_content, (dict, list)):
                        import json
                        total_raw_bytes += len(json.dumps(raw_content).encode("utf-8"))

        # Consolidate subagents
        subagent_receipts, deduped_findings, contradictions, sub_failures = (
            subagent_compactor.consolidate_subagents(
                subagent_records=subagent_results,
                existing_findings=all_verified_findings,
            )
        )
        all_failures.extend(sub_failures)

        for r in subagent_results:
            s_text = r.get("summary", "")
            if s_text:
                total_raw_bytes += len(s_text.encode("utf-8"))

        # Conditional Semantic Compaction (using local Qwen)
        interpretations: List[ModelInterpretation] = []
        extra_unresolved: List[str] = []
        local_provider = getattr(self.coordinator, "providers", {}).get("ollama") if self.coordinator else None

        semantic_compactor = SemanticCompactor(provider=local_provider)
        tight_budget = (total_raw_bytes > contract.constraints.context_budget_tokens * 4)

        if semantic_compactor.should_compact_semantically(
            findings_count=len(deduped_findings),
            subagents_count=len(subagent_receipts),
            has_contradictions=(len(contradictions) > 0),
            context_budget_tight=tight_budget,
        ):
            sub_summaries = [r.summary for r in subagent_receipts]
            trace_excerpts = [f.statement for f in deduped_findings[:5]]
            syn_summary, interps, unres = semantic_compactor.compact_semantically(
                contract=contract,
                findings=deduped_findings,
                subagent_summaries=sub_summaries,
                trace_excerpts=trace_excerpts,
            )
            if syn_summary and outcome == DelegationOutcome.COMPLETED:
                summary = syn_summary
            interpretations.extend(interps)
            extra_unresolved.extend(unres)

        # Escalation requirements
        escalation_reqs: List[EscalationRequirement] = []
        if outcome in (DelegationOutcome.BLOCKED, DelegationOutcome.ESCALATION_REQUIRED) or blocked_tools:
            escalation_reqs.append(
                EscalationRequirement(
                    reason="Operational actions were blocked by read-only boundaries or escalated by policy.",
                    severity="HIGH",
                    required_action="Authorize elevated privileges or execute via parent cloud session.",
                    context={"blocked_tools": [t.get("name") for t in blocked_tools]},
                )
            )
        if contradictions:
            escalation_reqs.append(
                EscalationRequirement(
                    reason=f"Detected {len(contradictions)} unresolved operational contradiction(s).",
                    severity="MEDIUM",
                    required_action="Review conflicting claims and determine authoritative source.",
                    context={"contradictions_count": len(contradictions)},
                )
            )

        # Build CloudReturnPacket
        packet = CloudReturnPacket(
            schema_version="1.0",
            task_id=task_id,
            parent_conversation_id=parent_cid,
            objective=contract.objective,
            outcome=outcome_to_status(outcome),
            summary=summary,
            verified_findings=deduped_findings,
            interpretations=interpretations,
            contradictions=contradictions,
            failures=all_failures,
            subagent_receipts=subagent_receipts,
            artifact_references=artifact_references,
            unresolved_questions=list(dict.fromkeys(unresolved + extra_unresolved)),
            escalation_requirements=escalation_reqs,
        )

        # Determine receiving model and provider context
        parent_provider_name = None
        parent_model_id = None
        parent_tokens = 0
        tool_schema_tokens = 0
        if parent_session:
            prov = getattr(parent_session, "model_provider", None)
            parent_provider_name = (
                getattr(prov, "primary_provider_name", None)
                or getattr(prov, "active_provider_name", None)
                or getattr(prov, "provider_name", None)
            )
            if parent_provider_name and parent_provider_name.startswith("coordinator[") and parent_provider_name.endswith("]"):
                parent_provider_name = parent_provider_name[12:-1]

            parent_model_id = (
                getattr(prov, "model_id", None)
                or getattr(prov, "active_model_id", None)
                or getattr(parent_session, "model_id", None)
            )
            parent_tokens = getattr(parent_session, "conversation_tokens", 0)

            # Compute tool schema overhead if parent has tool registry
            parent_tr = getattr(parent_session, "tool_registry", None)
            if parent_tr and hasattr(parent_tr, "list_tools"):
                try:
                    tools = parent_tr.list_tools()
                    import json
                    tools_json = json.dumps([t if isinstance(t, dict) else getattr(t, "to_dict", lambda: str(t))() for t in tools])
                    tool_schema_tokens = max(0, len(tools_json) // 4)
                except Exception:
                    tool_schema_tokens = 0

        eff_budget, budget_meta = budget_manager.compute_effective_budget(
            contract=contract,
            receiving_provider=parent_provider_name,
            receiving_model=parent_model_id,
            existing_conversation_tokens=parent_tokens,
            instruction_overhead_tokens=1000,
            tool_schema_overhead_tokens=tool_schema_tokens,
            reserved_reasoning_tokens=2048,
            coordinator=self.coordinator,
        )

        # Apply Budget Management
        packet = budget_manager.enforce_budget(
            packet=packet,
            target_budget_tokens=eff_budget,
            original_raw_bytes=max(total_raw_bytes, 100),
            budget_metadata=budget_meta,
        )

        # If handoff is blocked due to budget or feasibility violation, record escalation
        if not packet.compaction_metrics.is_handoff_ready:
            blocked_reason = packet.compaction_metrics.handoff_blocked_reason or "Unsafe packet budget"
            packet.escalation_requirements.append(
                EscalationRequirement(
                    reason=f"Context budget constraint violated: {blocked_reason}",
                    severity="HIGH",
                    required_action="Reduce conversation context or escalate to compact mode.",
                    context={"effective_budget": eff_budget, "budget_metadata": budget_meta},
                )
            )
            if outcome == DelegationOutcome.COMPLETED:
                outcome = DelegationOutcome.ESCALATION_REQUIRED
                packet.outcome = OutcomeStatus.NEEDS_ESCALATION

        # Validate Packet against trajectory store, subagent manager, and artifact store
        try:
            validator.validate(
                packet=packet,
                contract=contract,
                strict=False,
                trajectory_store=child_session.trajectory_store,
                subagent_manager=self.subagent_manager,
                task_conversation_id=child_cid,
            )
        except Exception as val_err:
            logger.warning(f"Packet validation warning for task '{task_id}': {val_err}")

        # 12. Finalize in ActiveTaskRegistry
        if self.task_registry:
            try:
                task_state = (
                    TaskState.COMPLETED
                    if outcome == DelegationOutcome.COMPLETED
                    else (
                        TaskState.CANCELLED
                        if outcome == DelegationOutcome.CANCELLED
                        else TaskState.FAILED
                    )
                )
                self.task_registry.complete_task(
                    task_id=task_id,
                    metadata={"outcome": outcome.value, "duration": duration},
                )
            except Exception:
                pass

        # 13. Cleanup Session
        child_session.close()

        if self.event_bus:
            try:
                self.event_bus.publish("SUPERVISOR_COMPLETED", {
                    "task_id": task_id,
                    "parent_conversation_id": parent_cid,
                    "outcome": outcome.value,
                    "duration": duration,
                    "tools_count": len(executed_tools),
                    "subagents_count": len(subagent_results),
                })
            except Exception:
                pass

        # 14. Construct TaskDelegationResult
        return TaskDelegationResult(
            task_id=task_id,
            parent_conversation_id=parent_cid,
            outcome=outcome,
            summary=summary,
            evidence=evidence_items,
            subagent_results=subagent_results,
            artifacts_created=artifacts_created,
            unresolved_questions=unresolved,
            escalation_details=(
                {"blocked_tools": blocked_tools} if blocked_tools else None
            ),
            return_packet=packet,
            metrics={
                "duration_seconds": round(duration, 3),
                "tools_executed_count": len(executed_tools),
                "steps_count": len(steps),
                "cloud_inference_calls": 0,  # 100% verified local
                "local_inference_mode": profile_mode,
                "local_inference_count": local_inferences,
                "subagents_count": len(subagent_results),
                "token_usage": {
                    "total_tokens": current_tokens,
                    "budget": contract.constraints.max_tokens_budget,
                    "budget_exhausted": budget_exhausted,
                    "prompt_tokens": prompt_tokens_used if total_tokens_used > 0 else estimated_prompt_tokens,
                    "completion_tokens": completion_tokens_used if total_tokens_used > 0 else estimated_completion_tokens,
                    "is_estimated": (total_tokens_used == 0),
                },
            },
        )

    def _filter_supervisor_tools(
        self,
        session: AgentSession,
        allowed_capabilities: List[str],
        subagents_allowed: bool = False,
    ) -> None:
        """Enforces capability allowlists and strips dangerous recursive delegation tools."""
        registry = session.tool_registry
        allowed_caps = {c.lower() for c in allowed_capabilities}

        # 1. Strictly forbid recursive delegation to other supervisors
        forbidden_tools = {
            "presence.delegate_local_work",
        }
        # If subagents are not permitted by contract, forbid all subagent tools
        if not subagents_allowed:
            forbidden_tools.update({
                "presence.invoke_subagent",
                "presence.send_subagent_message",
                "presence.manage_subagents",
                "invoke_subagent",
                "send_subagent_message",
                "manage_subagents",
                "subagents.spawn",
            })

        for f_id in forbidden_tools:
            if registry.get_tool(f_id):
                registry.unregister(f_id)

        # 2. Filter existing tools based on allowed capability categories
        for tool in list(registry.list_tools()):
            tool_id = tool.tool_id.lower()
            tier = tool.authority_tier

            # Always allow read_artifact, basic introspection, and permitted subagent tools
            if tool_id in ("presence.read_artifact", "system.status", "system_status"):
                continue
            if subagents_allowed and tool_id in (
                "presence.invoke_subagent",
                "presence.send_subagent_message",
                "presence.manage_subagents",
                "invoke_subagent",
                "send_subagent_message",
                "manage_subagents",
            ):
                continue

            # Check matching capabilities
            is_allowed = False
            for cap in allowed_caps:
                if cap == "all_read_only" and tier == PermissionTier.L0_OBSERVE:
                    is_allowed = True
                    break
                if cap in ("mcp.*", "mcp") and tool_id.startswith("mcp."):
                    is_allowed = True
                    break
                if cap in tool_id or tool_id.startswith(cap):
                    is_allowed = True
                    break
                if cap in ("mcp.filesystem", "filesystem") and ("fs." in tool_id or "filesystem" in tool_id or "file" in tool_id):
                    is_allowed = True
                    break
                if cap in ("mcp.code_search", "code_search") and ("search" in tool_id or "find" in tool_id):
                    is_allowed = True
                    break
                if cap in ("process_execution", "process") and ("process" in tool_id or "shell" in tool_id):
                    is_allowed = True
                    break
                if cap in ("process_inspection",) and ("list_processes" in tool_id or "get_process" in tool_id or "ps" in tool_id):
                    is_allowed = True
                    break
                if cap in ("mcp.system_diagnostics", "system_diagnostics") and ("diag" in tool_id or "status" in tool_id):
                    is_allowed = True
                    break
                if cap == "subagents" and "subagent" in tool_id:
                    is_allowed = True
                    break

            if not is_allowed:
                registry.unregister(tool.tool_id)

    def _build_supervisor_prompt(self, contract: TaskDelegationContract) -> str:
        """Constructs an unambiguous operational instruction prompt for local Qwen."""
        parts = [
            f"### Delegated Objective\n{contract.objective}\n",
        ]
        if contract.context_summary:
            parts.append(f"### Background Context\n{contract.context_summary}\n")

        parts.append(f"### Execution Constraints\n- Read-only: {contract.constraints.read_only}")
        parts.append(f"- Require verified evidence: {contract.constraints.require_verified_evidence}")

        if contract.completion_criteria:
            crit_lines = "\n".join(f"- {c}" for c in contract.completion_criteria)
            parts.append(f"\n### Completion Criteria\n{crit_lines}")

        return "\n".join(parts)

    def _extract_evidence(
        self, steps: List[Any]
    ) -> Tuple[List[VerifiedEvidenceItem], List[str]]:
        """Extracts factual verified evidence and created artifacts from trajectory steps."""
        evidence: List[VerifiedEvidenceItem] = []
        artifacts: List[str] = []

        for step in steps:
            if step.step_type == StepType.TOOL_RESULT:
                payload = step.step_payload or {}
                content = payload.get("content", "")
                t_name = payload.get("name", "tool")

                # Extract artifact pointer if offloaded
                art_id = payload.get("artifact_id")
                if not art_id and isinstance(content, dict):
                    art_id = content.get("artifact_id")
                if not art_id and isinstance(content, str):
                    import re
                    m = re.search(r"(?:artifact://|artifact\s*['\"]?)(art_[a-zA-Z0-9_\-]+)", content)
                    if m:
                        art_id = m.group(1)

                if art_id and art_id not in artifacts:
                    artifacts.append(art_id)

                snippet = str(content)[:600]
                evidence.append(
                    VerifiedEvidenceItem(
                        source=t_name,
                        evidence_type="stdout",
                        finding=f"Tool '{t_name}' executed in step {step.idx}.",
                        artifact_id=art_id,
                        raw_snippet=snippet,
                    )
                )

        return evidence, artifacts

    def _verify_completion(
        self,
        contract: TaskDelegationContract,
        steps: List[Any],
        executed_tools: List[Dict[str, Any]],
        blocked_tools: List[Dict[str, Any]],
        final_text: str,
        is_cancelled: bool,
        is_timed_out: bool,
        is_budget_exhausted: bool = False,
        effective_tokens: int = 0,
    ) -> Tuple[DelegationOutcome, str, List[str]]:
        """Deterministically evaluates task completion against execution evidence."""
        unresolved: List[str] = []

        if is_timed_out:
            return DelegationOutcome.FAILED, "Delegated execution timed out.", ["Execution budget exhausted."]

        if is_budget_exhausted:
            return (
                DelegationOutcome.FAILED,
                f"Delegated execution exceeded token budget ({effective_tokens} >= {contract.constraints.max_tokens_budget} tokens).",
                ["Execution token budget exhausted; remaining operational steps were aborted."]
            )

        if is_cancelled:
            return DelegationOutcome.CANCELLED, "Delegated execution was cancelled.", []

        if blocked_tools:
            unresolved.append(
                f"{len(blocked_tools)} required operational action(s) were blocked by read-only permission boundaries."
            )
            return (
                DelegationOutcome.BLOCKED,
                f"Execution blocked by permission policy: {blocked_tools[0].get('error', 'Operation denied')}",
                unresolved,
            )

        # Check required completion criteria
        if contract.completion_criteria:
            tool_names = [t.get("name", "").lower() for t in executed_tools]
            # Also check trajectory steps directly
            for s in steps:
                if s.step_type == StepType.TOOL_CALL:
                    p = s.step_payload or {}
                    if "name" in p: tool_names.append(p["name"].lower())

            for crit in contract.completion_criteria:
                crit_lower = crit.lower()
                if "test" in crit_lower and not any(
                    "test" in tn or "pytest" in tn or "run" in tn for tn in tool_names
                ):
                    unresolved.append(f"Criterion '{crit}' not satisfied: No test execution tool was invoked.")
                elif "inspect" in crit_lower and not any(
                    "read" in tn or "list" in tn or "inspect" in tn or "find" in tn for tn in tool_names
                ):
                    unresolved.append(f"Criterion '{crit}' not satisfied: No inspection tool was invoked.")

        if unresolved:
            return (
                DelegationOutcome.PARTIALLY_COMPLETED,
                f"Operational execution partially completed: {final_text[:300]}",
                unresolved,
            )

        clean_summary = final_text.strip() or "Operational objective completed successfully with verified evidence."
        return DelegationOutcome.COMPLETED, clean_summary, []
