# PRESENCE — TDEL REMEDIATION BASELINE (PHASE 0)
## Forensic Baseline, Defect Reproduction, and Service Contract Inspection

**Audit Date:** 2026-11-20  
**Project:** Presence — Windows-native persistent desktop AI companion  
**Runtime:** Python 3.12.10 (x64) on Windows 11 Enterprise  
**Subsystem:** Task Decomposition & Execution Ledger (TDEL)  
**Deliverable:** Phase 0 Baseline Inspection & Reproduction Report  

---

## 1. Executive Summary

This document establishes the verified baseline of the Presence codebase prior to applying remediation changes for TDEL production integration.

The three defects identified in the master audit were reproduced forensically in isolated execution without modifying production code:
1. **Defect 1 (Conversational Preemption Gap):** Verified. `AgentSession.execute_turn()` forwards directly to `AgentExecutor.execute_turn()`, which calls `self.provider.generate_stream()`. Neither `TaskPlanningService` nor `ComplexityClassifier` is invoked in this path.
2. **Defect 2 (Archetype Fragility / Inventory Failure):** Verified. When prompted with `"Inventory my installed desktop applications and categorize them."`, `TaskDecompositionPlanner` returns a generic 2-step stub calling `presence.status` because the archetype matcher strictly requires the substring `"mcp"`.
3. **Defect 3 (Permission Tier Mismatch):** Verified. `presence.manage_plan` exposes `action="cancel"` under `PermissionTier.L0_OBSERVE` (read-only), allowing state-mutating cancellations without mutation authority.

---

## 2. Forensic Wiring Trace & Earliest Safe Insertion Point

### Actual Current Turn Trace
```text
ChatView.action_submit_chat(user_text)
    │
    ▼
AgentSession.execute_turn(user_input, ...)
    │
    ▼
AgentExecutor.execute_turn(user_input, ...)
    │
    ├─ TrajectoryStore.append_step("USER", ...)
    ├─ SummaryManager.index_step_text(...)
    ├─ ContextAssembler.assemble(...)  <-- Reconstructs entire conversation from SQLite
    ├─ ToolRegistry.get_declarations()
    │
    ▼
Provider.generate_stream(...)  <-- Model HTTP Call (Exhausts quotas in ReAct loops)
```

### Earliest Safe Insertion Point
The ideal insertion point is **at the start of `AgentSession.execute_turn()`** (or immediately inside `AgentExecutor.execute_turn()` right after logging the initial user turn event).
At this point:
- The user prompt is known and clean.
- The session `conversation_id` is bound.
- No heavy SQL message reconstruction or prompt assembly has occurred.
- No LLM provider calls have been initiated.
- The existing `self.task_planning_service` instance attached to `PresenceRuntime` and injected via `AgentSessionFactory` can be queried in sub-millisecond deterministic time ($\approx 0.05\text{ ms}$).

If `ComplexityClassifier.requires_decomposition()` is `True`:
- Intercept the execution turn.
- Invoke `TaskPlanningService.evaluate_and_execute()`.
- Stream the progress events and deliver the final deliverable without entering the ReAct model loop.
- If it returns `handled_by_workflow: False` (e.g. `SIMPLE` conversational prompt or conversational discussion), continue to the standard `AgentExecutor` provider loop.

---

## 3. Actual `TaskPlanningService` Contract

Inspection of `runtime/task_planning/service.py`:

```python
class TaskPlanningService:
    def __init__(
        self,
        workflow_engine: WorkflowEngine,
        persistence: Optional[PersistenceManager] = None,
        artifact_store: Optional[ArtifactStore] = None,
        task_registry: Optional[ActiveTaskRegistry] = None,
        event_bus: Optional[EventBus] = None,
        model_provider: Optional[Any] = None,
    ): ...

    def evaluate_and_execute(
        self,
        prompt: str,
        conversation_id: str,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> Dict[str, Any]: ...

    def create_plan(self, prompt: str, conversation_id: str) -> TaskPlan: ...
    def get_plan_status(self, plan_id: str) -> Optional[Dict[str, Any]]: ...
    def list_plans(self, conversation_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]: ...
    def reconcile_startup(self) -> List[Dict[str, Any]]: ...
```

### Key Contract Characteristics
1. **Execution Model:** `evaluate_and_execute()` is synchronous; it runs `WorkflowEngine.execute_workflow()`, which coordinates parallel threads via `ThreadPoolExecutor` while blocking the caller until DAG completion or cancellation.
2. **Return Envelope:**
   - If handled: `{"handled_by_workflow": True, "status": "SUCCESS", "plan_id": ..., "plan_status": "COMPLETED", "workflow_id": ..., "final_deliverable_artifact_id": ..., "ledger_artifact_id": ..., "nodes_completed": 7, "total_nodes": 7}`.
   - If not handled (e.g. SIMPLE): `{"handled_by_workflow": False, "complexity": "SIMPLE", "reason": "..."}`.
3. **Cancellation:** Accepts `CancellationToken`, maps cooperative cancellation to `WorkflowEngine`, and records `PlanStatus.CANCELLED`.
4. **Provider Outage:** Caught by `_generate_plan_via_llm()` exception handler, but currently falls back to `_build_default_plan()`, which needs remediation to return `PLAN_UNAVAILABLE` when dynamic planning is truly required.

---

## 4. Exact Defect Reproduction Details

### Defect 1: Conversational Loop Bypasses TDEL
- **File:** `runtime/session.py` lines 96–135.
- **Evidence:** `AgentSession.execute_turn()` simply forwards `user_input` to `self.agent_executor.execute_turn()`. No planning or complexity checks occur.
- **Impact:** Normal chat users never benefit from DAG planning; complex tasks stay in the ReAct loop and exhaust token quotas.

### Defect 2: Software Inventory Prompt Fails Archetype Matching
- **File:** `runtime/task_planning/planner.py` line 67.
- **Evidence:** `if ("mcp" in lower or "mcps" in lower) and ...` requires `"mcp"` in the string.
- **Test Prompt:** `"Inventory my installed desktop applications and categorize them."`
- **Output:** Returns `General Multi-Step Task` (2-step stub: `step_execute` calling `presence.status` $\to$ aggregate summary).
- **Impact:** Common Windows desktop management commands produce useless status stubs instead of real application inventory DAGs.

### Defect 3: State-Mutating Cancellation Under L0_OBSERVE
- **File:** `runtime/tools/plan_tools.py` line 52.
- **Evidence:** `manage_plan_tool` has `authority_tier=PermissionTier.L0_OBSERVE` while defining `properties.action.enum = ["list_plans", "get_status", "cancel"]`.
- **Impact:** Violates least privilege; read-only roles can cancel active background workflows.

---

## 5. Phase 0 Gate Sign-Off

The baseline has been forensically established and all three defects are reproduced with root causes confirmed. We are cleared to proceed with **Phase 1: Interactive Conversational Preemption**.
