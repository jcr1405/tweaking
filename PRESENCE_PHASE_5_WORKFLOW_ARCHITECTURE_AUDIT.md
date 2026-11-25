# PRESENCE — PHASE 5 ARCHITECTURE & DESIGN AUDIT
## Deterministic Workflow Orchestration Engine (`presence.run_workflow`)

**Date:** 2026-11-19  
**System:** Presence Runtime v2.0  
**Phase:** 5A — Workflow Architecture Audit  

---

### 1. Existing Reusable Subsystems Inventory

| Subsystem | Existing Component | Reusability in Phase 5 |
| :--- | :--- | :--- |
| **Process Execution** | `ProcessManager` (`runtime/process_manager/`) | Full. Executes OS subprocesses under Windows Job Objects. Returns stable `task_id` and artifact paths. |
| **Ephemeral Subagents** | `SubagentManager` (`runtime/subagents/`) | Full. Spawns isolated worker sessions with bounded concurrency, recursion depth, and automatic artifact receipt. |
| **Tool Execution** | `ToolRegistry` (`runtime/tools/registry.py`) | Full. Executes individual registered tools with permission tier checks and timeout governance. |
| **Task State Tracking** | `ActiveTaskRegistry` (`runtime/observability/task_registry.py`) | Full. Tracks hierarchical task trees (`TaskType.WORKFLOW`, `WORKFLOW_PHASE`, `WORKFLOW_STEP`). |
| **Persistence Engine** | `PersistenceManager` (`runtime/persistence.py`) | Full. WAL-mode SQLite storage for workflow definitions, execution run states, and node checkpoints. |
| **Event Dissemination** | `EventBus` (`runtime/events.py`) | Full. Real-time event notifications (`WORKFLOW_STARTED`, `NODE_COMPLETED`, `WORKFLOW_FINISHED`). |
| **Artifact Management** | `ArtifactStore` (`runtime/artifacts/store.py`) | Full. Offloads node outputs and aggregated final deliverables with SHA-256 integrity and preview envelopes. |

---

### 2. The Architectural Need for Deterministic Workflows

Recent production telemetry demonstrates that multi-stage compound tasks (e.g., querying installed software, searching GitHub for MCPs, analyzing repos, compiling a report) cannot run reliably inside an interactive conversational ReAct loop:
1. **API Quota Exhaustion:** 21 sequential tool iterations triggered 21 distinct HTTP completions, exhausting daily free-tier neuron allocations.
2. **Quadratic Compounding:** Even with intermediate output masking, conversational turns require 3,000–9,000 tokens per iteration.
3. **Fragility:** If step 18 encounters an API timeout, the entire trajectory stalls without deterministic resume points.

**The Solution:** Phase 5 decouples **orchestration** from **conversational reasoning**. An LLM plans the Directed Acyclic Graph (DAG) once; the workflow engine executes independent nodes in native Python with **zero LLM tokens**; and the LLM is invoked only once at the conclusion to synthesize the aggregated deliverable.

---

### 3. Node Execution Types & Contract

```
       ┌──────────────────┐
       │   Workflow DAG   │
       └────────┬─────────┘
                │
    ┌───────────┼──────────────┬──────────────┬──────────────┐
    ▼           ▼              ▼              ▼              ▼
[ TOOL ]  [ SUBAGENT ]    [ PROCESS ]   [ AGGREGATE ]   [ VERIFY ]
 Registry   SubagentMgr   ProcessMgr     ArtifactStore   Assertions
```

1. **`TOOL` Node:** Executes a single registered tool (`ToolRegistry.get_tool(name).handler(args)`). Enforces permission tier.
2. **`SUBAGENT` Node:** Spawns an isolated `ManagedSubagent` (`SubagentManager.spawn_subagent(role, goal, whitelist)`). Returns structured execution receipt.
3. **`PROCESS` Node:** Spawns an asynchronous CLI command under Windows Job Object (`ProcessManager.run_command(cmd, timeout, daemon)`). Captures stdout/stderr artifacts.
4. **`AGGREGATE` Node:** Deterministically concatenates or transforms payloads/artifacts from upstream dependency nodes into a final deliverable artifact.
5. **`VERIFY` Node:** Evaluates assertions (regex match, exit code, JSON path, non-empty artifact) to validate stage prerequisites before branch progression.

---

### 4. DAG Validation Rules

The DAG validator (`runtime/workflows/validator.py`) enforces strict validation prior to launch:
- **Topological Order (Cycle Detection):** Kahn’s algorithm detects any cyclical dependency and rejects invalid definitions immediately.
- **Dangling Dependency Check:** All node `depends_on` IDs must exist in the definition.
- **Node ID Uniqueness:** Duplicate node IDs are disallowed.
- **Schema Validation:** Each execution block must strictly match its type schema (`ToolArgs`, `SubagentArgs`, `ProcessArgs`, `AggregateArgs`, `VerifyArgs`).
- **Phase Barriers:** Nodes marked with phase barriers cannot execute until all nodes in prior phases complete.

---

### 5. Deterministic Concurrency & Scheduling

- **Concurrency Pool:** Managed `ThreadPoolExecutor(max_workers=5)` for concurrent branch dispatch.
- **Dependency Release Policies:**
  - `ALL_SUCCESS` (default): All dependencies must complete with status `COMPLETED`.
  - `ANY_SUCCESS`: At least one dependency must complete successfully.
  - `OPTIONAL`: Node executes regardless of dependency status (handles best-effort fallbacks).
- **Failure Propagation Policies:**
  - `FAIL_FAST`: Any non-optional node failure immediately terminates unstarted nodes and cancels running branches.
  - `CONTINUE_INDEPENDENT_BRANCHES`: Nodes dependent on failed nodes are marked `SKIPPED`, while independent branches continue to completion.
  - `ALLOW_OPTIONAL_FAILURES`: Failures on nodes with `optional=True` are logged as warnings and do not trigger cascade failure.

---

### 6. Persistence & Crash Recovery

New SQLite WAL tables in `runtime/persistence.py`:
- `workflows`: Stores workflow definitions, status, start/end timestamps, and error messages.
- `workflow_nodes`: Stores per-node execution status, input arguments, output payloads, artifact IDs, execution duration, and retry counts.

**Crash Recovery Reconciliation:**
1. Upon engine startup, any workflow marked `RUNNING` is inspected.
2. Nodes with status `COMPLETED` and valid persisted artifacts are preserved.
3. Nodes with status `RUNNING` for side-effecting operations (`PROCESS`, non-idempotent `TOOL`) are marked `OUTCOME_UNKNOWN` / `INTERRUPTED` to prevent duplicate execution.
4. Idempotent read-only nodes can be resumed if configured.

---

### 7. Phase 5 Implementation Milestones

- **Phase 5A:** Architecture Audit & Specifications (Complete).
- **Phase 5B:** Workflow Data Models & DAG Validator.
- **Phase 5C:** Deterministic Scheduler & Execution Adapters.
- **Phase 5D:** Workflow Engine Core & TaskRegistry Bridge.
- **Phase 5E:** Persistence WAL Storage & Crash Reconciliation.
- **Phase 5F:** Native Agent Tools (`presence.run_workflow`, `presence.manage_workflow`).
- **Phase 5G:** Unit Tests, Regression Verification & Implementation Report.
