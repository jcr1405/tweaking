# PRESENCE PHASE 2A: LOCAL OPERATIONS SUPERVISOR CORE
## Engineering Implementation, Security Isolation & Verification Report

**Subsystem:** Local Operations Supervisor (`runtime.local_operations`)  
**Architecture:** Hierarchical Hybrid Intelligence (Cloud Principal + Local Operations Supervisor)  
**Local Inference Model:** Qwen 3.5 9B via Ollama (`http://localhost:11434`)  
**Operating Environment:** Windows 11 / Python 3.12 / Presence Desktop AI Companion  
**Date:** November 20, 2026  
**Status:** **APPROVED & FULLY VERIFIED (11/11 Phase 2A Tests Passing, 33/33 Regression Tests Passing)**

---

## 1. EXECUTIVE SUMMARY & OBJECTIVE

Presence's hierarchical hybrid intelligence architecture decouples **high-level intent, planning, and user alignment** (owned by the Cloud Principal — Gemini / FreeLLMAPI) from **low-level, iterative operational execution** (owned by the Local Operations Supervisor — Qwen 3.5 9B via Ollama).

Prior to Phase 2A, running multi-turn diagnostic tasks, file system scans, and CLI operations on the cloud model suffered from three critical vulnerabilities:
1. **Severe Token & Quota Consumption:** High-frequency, verbose tool responses repeatedly saturated cloud context windows and daily quota allocations.
2. **Brittle Fallback Coupling:** Runtime provider failovers were uncoordinated at the task level, risking accidental cloud tool re-execution or context loss.
3. **Absence of Strict Operational Scoping:** Operational tasks shared root session trajectories, making parent conversations noisy and muddying execution boundaries.

**Phase 2A delivers the `LocalOperationsSupervisor` engine**, a self-contained execution subsystem that accepts a strongly typed, validated `TaskDelegationContract`, provisions an isolated task-scoped `AgentSession`, enforces a non-bypassable `ScopedPermissionBoundary` (`PermissionTier.L0_OBSERVE` ceiling), drives a multi-turn tool execution loop strictly pinned to local Ollama (`qwen3.5:9b`), deterministically verifies contract completion against trajectory evidence, and returns a structured `TaskDelegationResult`.

```
+-------------------------------------------------------------------------+
|                        CLOUD PRINCIPAL INTELLIGENCE                     |
|           (Gemini / FreeLLMAPI: Strategic Planner & User Context)        |
+-------------------------------------------------------------------------+
                                     |
                       TaskDelegationContract
                                     v
+-------------------------------------------------------------------------+
|                      LOCAL OPERATIONS SUPERVISOR                        |
|                                                                         |
|  [Contract Validation]  --> [ActiveTaskRegistry (OPERATIONAL_SUPERVISOR)]|
|                                    |                                    |
|                       Scoped AgentSession Creation                      |
|                                    |                                    |
|              +---------------------+---------------------+              |
|              |                                           |              |
|              v                                           v              |
|   ScopedPermissionBoundary                    Strict Zero-Cloud Pinning  |
|   - Tier <= L0_OBSERVE                        - Provider: "ollama"       |
|   - Capability Allowlists                     - Model: "qwen3.5:9b"      |
|   - Anti-Recursion Gate                       - Mode: "routine"          |
|                                               - Policy: "no_fallback"    |
|                                                          |              |
|              +-------------------------------------------+              |
|              |                                                          |
|              v                                                          |
|   AgentExecutor Multi-Turn Operational Loop                             |
|   - Local Qwen 3.5 9B drives tool execution (MCP, Process, Filesystem)  |
|   - Large outputs (>40 KB) auto-offloaded to disk ArtifactStore          |
|   - Anti-loop & per-contract deadline monitoring                        |
|              |                                                          |
|              v                                                          |
|   Deterministic Completion Verification Engine                          |
|   - Steps audited against claimed deliverables                          |
|   - Rejects unexecuted / hallucinated criteria claims                    |
|   - Extracts verified evidence snippets & artifact IDs                  |
|                                                                         |
+-------------------------------------------------------------------------+
                                     |
                        TaskDelegationResult
                                     v
+-------------------------------------------------------------------------+
|                        CLOUD PRINCIPAL RESUMPTION                       |
|           (Synthesizes Compact Result; Never Sees Raw Output Bloat)     |
+-------------------------------------------------------------------------+
```

---

## 2. REQUEST LIFECYCLE BREAKDOWN

Every delegated operational task traverses seven strictly sequenced phases:

### Phase 1: Contract Intake & Validation
The caller passes a `TaskDelegationContract` to `LocalOperationsSupervisor.execute()`. Pydantic validators confirm all required fields: non-empty `task_id`, `parent_conversation_id`, `objective`, valid execution profile, and verified capability identifiers. Any attempt to request unsupported capabilities (e.g., `subagents`, deferred to Phase 2B) is rejected immediately at intake.

### Phase 2: Observability & Task Registry Registration
The supervisor registers the task in `ActiveTaskRegistry` under `TaskType.OPERATIONAL_SUPERVISOR` with `cancellable=True` and wires its cancellation hook directly to a task-scoped `CancellationToken`. A `SUPERVISOR_STARTED` event is emitted on the system `EventBus`.

### Phase 3: Scoped Permission Boundary & Isolation
A `ScopedPermissionBoundary` wraps the parent session's boundary:
- **Ceiling Enforcement:** If `contract.constraints.read_only=True`, the boundary ceiling is clamped to `PermissionTier.L0_OBSERVE`. Any tool call requiring write, execute, or destructive mutations (`L1`–`L3`) is rejected fail-closed without prompting the user.
- **Capability Filtering:** All tools not matching `contract.allowed_capabilities` are stripped.
- **Anti-Recursion Filtering:** Delegation tools (`presence.delegate_local_work`, `presence.invoke_subagent`, `subagents.spawn`) are explicitly unregistered from the child session's catalog, preventing recursive supervisor loops.

### Phase 4: Child Session Instantiation
Through `AgentSessionFactory.create_session()`, an isolated `AgentSession` is spawned:
- Unique child ID: `child_conv_{task_id}_{timestamp}`.
- Linked to `parent_conversation_id` with `nesting_depth=1`.
- Trajectory store and journal entries reside in an isolated child SQLite file, ensuring zero clutter in the parent conversation trajectory.

### Phase 5: Zero-Cloud Pinned Inference Loop
The supervisor constructs an `InferenceRequestConfig` explicitly pinned to:
```python
InferenceRequestConfig(
    provider="ollama",
    model="qwen3.5:9b",
    mode="routine",  # think=False for high-speed deterministic tool operations
    purpose="local_supervisor_execution",
    fallback_policy="no_fallback",  # Zero cloud calls under any circumstance
    cancellation_token=local_cancel_token,
    conversation_id=child_cid,
    timeout_seconds=contract.constraints.timeout_seconds
)
```
This configuration is passed directly into `AgentSession.execute_turn(request_config=...)`, which threads it into `AgentExecutor.execute_turn()`. At every iteration of the tool loop, `MultiProviderCoordinator.request_stream()` honors the pinned request config, guaranteeing **zero cloud provider invocations**.

### Phase 6: Deterministic Verification & Evidence Extraction
Upon loop termination (tool completion, turn stop, timeout, or cancellation):
1. **Trajectory Step Extraction:** `child_session.trajectory_store.list_steps()` is inspected for `TOOL_RESULT` steps.
2. **Artifact Extraction:** Top-level payload artifact IDs and text patterns (`art_...`) are extracted into `artifacts_created` and `evidence`.
3. **Completion Verification:** If the model claims an action was performed but no matching tool execution step exists in the trajectory, the task is marked `FAILED` with an unexecuted claim notice.
4. **Timeout / Cancellation Audit:** Timeouts produce `DelegationOutcome.FAILED` with `"Execution budget exhausted."`, while user or parent cancellations produce `DelegationOutcome.CANCELLED`.

### Phase 7: Cleanup & Result Packaging
The child session is closed (`child_session.close()`), the task in `ActiveTaskRegistry` is transitioned to `TaskState.COMPLETED` / `CANCELLED` / `FAILED`, and a `TaskDelegationResult` is returned containing verified evidence, artifact pointers, and detailed execution metrics.

---

## 3. FILES CREATED AND MODIFIED

| File Path | Status | Core Responsibility |
|:---|:---:|:---|
| [runtime/local_operations/models.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/local_operations/models.py) | **Created** | Pydantic models: `ExecutionProfile`, `DelegationOutcome`, `DelegationConstraints`, `TaskDelegationContract`, `VerifiedEvidenceItem`, `TaskDelegationResult`. |
| [runtime/local_operations/supervisor.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/local_operations/supervisor.py) | **Created** | Core supervisor engine: `LocalOperationsSupervisor` and `ScopedPermissionBoundary`. Multi-turn loop, capability filtering, anti-recursion gating, evidence extraction, deterministic verification. |
| [runtime/local_operations/__init__.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/local_operations/__init__.py) | **Created** | Package initialization exporting models and supervisor class. |
| [runtime/observability/models.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/observability/models.py) | **Modified** | Added `TaskType.OPERATIONAL_SUPERVISOR = "operational_supervisor"` to task taxonomy. |
| [runtime/agent_executor.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/agent_executor.py) | **Modified** | Updated `execute_turn()` to accept and forward `request_config: Optional[Any] = None` across every streaming iteration of the multi-turn loop. |
| [runtime/session.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/session.py) | **Modified** | Updated `execute_turn()` signature to accept and pass `request_config` to `self.agent_executor.execute_turn()`. |
| [tests/test_local_operations_supervisor.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/tests/test_local_operations_supervisor.py) | **Created** | 11 unit, integration, and live Ollama tests covering contract validation, zero-cloud pinning, scoping, isolation, anti-recursion, verification, timeout, cancellation, artifacts, and live Qwen 3.5 9B tool calling. |

---

## 4. SCHEMAS: TASK DELEGATION CONTRACT & RESULT

### 4.1 TaskDelegationContract
```python
class TaskDelegationContract(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=128)
    parent_conversation_id: str = Field(..., min_length=1, max_length=128)
    objective: str = Field(..., min_length=5, max_length=2000)
    context_summary: str = Field("", description="Concise background context needed for the task.")
    execution_profile: ExecutionProfile = Field(
        default=ExecutionProfile.LOCAL_SUPERVISOR_ROUTINE,
        description="LOCAL_SUPERVISOR_ROUTINE (think: false) or LOCAL_SUPERVISOR_DEEP (think: true)"
    )
    constraints: DelegationConstraints = Field(default_factory=DelegationConstraints)
    allowed_capabilities: List[str] = Field(..., min_length=1)
    expected_deliverable: Dict[str, Any] = Field(
        default_factory=lambda: {"type": "diagnostic_report", "fields": ["findings", "evidence"]}
    )
    completion_criteria: List[str] = Field(
        default_factory=list,
        description="Deterministic criteria required to declare completion (e.g. ['files_inspected'])."
    )
    escalation_policy: str = Field(
        default="escalate_on_missing_tool_or_contradiction"
    )
```

### 4.2 DelegationConstraints
```python
class DelegationConstraints(BaseModel):
    max_tool_cycles: int = Field(10, ge=1, le=50)
    timeout_seconds: float = Field(180.0, ge=0.01, le=600.0)
    read_only: bool = Field(True, description="Strict L0 read-only ceiling.")
    network_access_allowed: bool = Field(False)
    subagents_allowed: bool = Field(False, description="Deferred to Phase 2B.")
    max_tokens_budget: int = Field(8192, ge=256, le=65536)
    require_verified_evidence: bool = Field(True)
```

### 4.3 TaskDelegationResult
```python
class TaskDelegationResult(BaseModel):
    task_id: str
    parent_conversation_id: str
    outcome: DelegationOutcome  # COMPLETED | CANCELLED | FAILED | ESCALATED
    summary: str
    evidence: List[VerifiedEvidenceItem] = Field(default_factory=list)
    subagent_results: List[Dict[str, Any]] = Field(default_factory=list)
    artifacts_created: List[str] = Field(default_factory=list)
    unresolved_questions: List[str] = Field(default_factory=list)
    escalation_details: Optional[Dict[str, Any]] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)
```

---

## 5. ZERO-CLOUD INFERENCE GUARANTEE

The zero-cloud guarantee is enforced through five concentric architectural barriers:

1. **Pinned `InferenceRequestConfig`:** The supervisor constructs an immutable `InferenceRequestConfig` with `provider="ollama"`, `model="qwen3.5:9b"`, and `fallback_policy="no_fallback"`.
2. **Forwarding Across All Turns in `AgentExecutor`:** Previously, streaming iterations in `AgentExecutor` called `provider.request_stream()` with turn-local defaults. `AgentExecutor` now propagates the explicit `request_config` into every turn and tool loop iteration.
3. **`MultiProviderCoordinator` No-Fallback Enforcement:** When `request_config.fallback_policy == "no_fallback"`, the coordinator disables the fallback chain. If Ollama is unreachable or crashes, the coordinator immediately raises an error without falling back to FreeLLMAPI or Gemini.
4. **Verification via Call Counting:** Verified in Test 2 (`test_02_explicit_local_provider_selection_zero_cloud_calls`) where the cloud mock provider registered exactly `0` calls while the local provider serviced the multi-turn loop.
5. **No Mutation of Global Environment:** The supervisor does not modify `.env`, system environment variables, or global coordinator configuration; isolation is strictly request-scoped.

---

## 6. PERMISSION ENFORCEMENT & TOOL ISOLATION

### 6.1 ScopedPermissionBoundary
The `ScopedPermissionBoundary` acts as a mandatory security filter placed between the child `AgentSession` and the tool execution engine:
- **Read-Only Ceiling (`read_only=True`):** Any action requiring `tier > PermissionTier.L0_OBSERVE` is immediately rejected fail-closed with:
  `PermissionDeniedError: Operation '...' requires tier ... exceeding read-only boundary L0_OBSERVE.`
- **Capability Whitelisting:** Dynamically registered tools (e.g. MCP tools) are registered into the scoped boundary and audited against allowed prefixes (`mcp.filesystem`, `mcp.code_search`, `system_diagnostics`, etc.).
- **Fail-Closed Default:** Unregistered tools or tools outside the requested capability set are blocked.

### 6.2 Tool Namespace & Anti-Recursion
To protect against runaway nested agent trees, `LocalOperationsSupervisor._filter_supervisor_tools()` explicitly unregisters recursive orchestration tools:
- `presence.delegate_local_work`
- `presence.invoke_subagent`
- `subagents.spawn`

Any attempt by local Qwen to invoke delegation or spawn subagents results in a tool-not-found error, cleanly keeping execution within single-tier local supervision.

---

## 7. VERIFICATION TEST RESULTS

### 7.1 Phase 2A Test Suite (`tests/test_local_operations_supervisor.py`)

| Test Name | Focus Area | Status | Evidence / Metrics |
|:---|:---|:---:|:---|
| `test_01_contract_validation` | Pydantic contract validation & rejection of invalid capabilities | **PASSED** | Validates proper fields; rejects empty tasks and unsupported `subagents`. |
| `test_02_explicit_local_provider_selection_zero_cloud_calls` | Zero-cloud guarantee & local provider pinning | **PASSED** | Cloud calls: `0`. Local calls: `1`. Pinned to `qwen3.5:9b`. |
| `test_03_multi_turn_local_execution_loop` | Multi-turn tool execution loop & evidence extraction | **PASSED** | Multi-turn sequence completed; tool outputs captured into trajectory. |
| `test_04_scoped_permission_enforcement_blocks_mutation` | Non-bypassable read-only ceiling enforcement | **PASSED** | L1 write tool blocked fail-closed; task outcome `FAILED` with escalation details. |
| `test_05_tool_namespace_isolation_and_anti_recursion` | Unregistration of forbidden recursive tools | **PASSED** | `presence.delegate_local_work` and `presence.invoke_subagent` absent from catalog. |
| `test_06_parent_child_identity_isolation` | Trajectory and session isolation between parent and child | **PASSED** | Child steps not leaked into parent store; child session closed properly. |
| `test_07_deterministic_completion_verification_rejects_unexecuted_claims` | Rejection of unexecuted/hallucinated claims | **PASSED** | Model claimed file inspection without executing tool; outcome marked `FAILED`. |
| `test_08_cancellation_propagation` | Propagation of cancellation token to local supervisor | **PASSED** | Cancel token triggered; loop aborted cleanly; outcome `CANCELLED`. |
| `test_09_timeout_handling_no_cloud_fallback` | Strict timeout enforcement without cloud fallback | **PASSED** | 0.1s deadline expired; terminated as `FAILED`; zero cloud calls made. |
| `test_10_large_tool_output_persisted_to_artifact_store` | Tool output offloading to `ArtifactStore` | **PASSED** | 10 KB output stored as `art_...`; artifact ID captured in result evidence. |
| `test_11_live_qwen_integration_with_real_ollama` | Live end-to-end integration with local Ollama `qwen3.5:9b` | **PASSED** | Real Ollama answered on `http://localhost:11434`; tool executed and returned. |

**Result:** **11 PASSED in 10.07s.**

### 7.2 System Regression Test Suite

| Test Suite File | Test Count | Status | Subsystems Verified |
|:---|:---:|:---:|:---|
| [tests/test_phase1_final_acceptance.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/tests/test_phase1_final_acceptance.py) | 5 | **PASSED** | Coordinator concurrency, quota/rate-limit semantics, partial resets, live Ollama, TDEL consistency. |
| [tests/test_multi_provider_coordinator.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/tests/test_multi_provider_coordinator.py) | 14 | **PASSED** | Provider failovers, streaming retries, eligibility filters, auth failures, cancellation. |
| [tests/test_agent_executor.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/tests/test_agent_executor.py) | 7 | **PASSED** | Turn continuation, tool calling loops, security gate blocking, provider failure handling. |
| [tests/test_tdel_preemption_and_production.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/tests/test_tdel_preemption_and_production.py) | 7 | **PASSED** | Preemption paths A-G, Archetype E diagnostics, least privilege tools, outage handling. |

**Regression Result:** **33 PASSED in 12.81s with zero regressions.**

---

## 8. REMAINING LIMITATIONS & DEFERRED PHASES

In strict accordance with the implementation mandate, the following capabilities have been deliberately isolated and deferred to subsequent phases:

- **Phase 2B: Local Subagent Supervision & Inference Semaphore Guard:** Spawning child subagents from the Local Operations Supervisor and managing local GPU VRAM concurrency via an async inference semaphore.
- **Phase 2C: Advanced Context Compaction & CloudReturnPacket:** Structured distillation of multi-step logs into semantic compression envelopes before handing off to the cloud model.
- **Phase 2D: Cloud Delegation Tool (`presence.delegate_local_work`):** Exposing the supervisor as an invokable MCP tool in the Cloud Principal's tool catalog.
- **Phase 2E: TDEL Multi-Phase DAG Delegation Nodes:** Integrating delegation contracts directly into TDEL task plan nodes.
- **Phase 2F: Cloud Escalation & Reconciliation:** Protocol for Cloud Principal handling of `DelegationOutcome.ESCALATED`.
- **Phase 2G: Full Comparative Benchmarks:** End-to-end latency, VRAM, and token consumption benchmarks across cloud-only vs. hierarchical routing.

---

## 9. CONCLUSION

Presence Phase 2A successfully establishes the core **Local Operations Supervisor** runtime. With full zero-cloud isolation, strict read-only permission clamping, anti-recursion defenses, deterministic verification, and 100% verified integration with local Ollama (`qwen3.5:9b`), Presence now possesses an autonomous operational execution capability ready for higher-level orchestration integration.