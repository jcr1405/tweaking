# PRESENCE — TDEL ARCHITECTURAL AUDIT (PHASE 0)
## Forensic Baseline, Runtime Wiring Trace, and Single-Source-of-Truth Verification

**Audit Date:** 2026-11-20  
**Runtime:** Python 3.12.10 (x64) on Windows 11 Enterprise  
**Subsystem:** Task Decomposition & Execution Ledger (TDEL)  
**Audit Target:** Architecture Baseline & Integration Integrity  
**Artifact Status:** Official Phase 0 Forensic Audit  

---

## 1. Executive Summary

This audit establishes the baseline architectural reality of the Task Decomposition & Execution Ledger (TDEL) within Presence.

The audit team performed source inspections across 19 core repository files spanning planning, workflow orchestration, observability, persistence, agent execution, and TUI presentation.

### Primary Audit Findings

1. **Service Completeness:** The core TDEL components (`TaskPlanningService`, `ComplexityClassifier`, `TaskDecompositionPlanner`, `TaskPlanValidator`, `TaskExecutionLedger`, `LedgerProjectionRenderer`, `WorkflowExecutionBridge`, `TaskPlanRecoveryManager`) are implemented, strongly typed, and pass their unit and integration test suites.
2. **Authoritative State Architecture:** Single-source-of-truth invariants are strictly maintained. SQLite WAL (`task_plans`, `workflow_runs`, `workflow_nodes`) is authoritative. `TASK_LEDGER.md` is a derived, read-only projection artifact. The LLM has zero direct authority to mutate task execution states.
3. **Execution Path Disconnection in Conversational Loop:** While `TaskPlanningService` is fully functional and registered as agent tools (`presence.create_plan` and `presence.manage_plan`), standard conversational user turns in `ChatView` $\to$ `AgentSession.execute_turn` $\to$ `AgentExecutor.execute_turn` **do not automatically route through the `ComplexityClassifier` / `TaskPlanningService` pre-execution pipeline**. Instead, user prompts are sent directly to the model provider (`self.provider.generate_stream`). TDEL is only triggered if the LLM decides to emit a tool call for `presence.create_plan`, or if invoked directly via code. If an LLM provider fails (e.g. HTTP 502 / 429 quota exhaustion), the turn aborts before any deterministic plan can execute.

---

## 2. File-by-File Forensic Inspection

| Component / File | Architecture Role | Forensic Assessment |
| :--- | :--- | :--- |
| `runtime/task_planning/models.py` | Data contracts (`TaskPlan`, `TaskPlanNode`, `ComplexityCategory`, `PlanStatus`) | **VERIFIED.** Strongly typed dataclasses with serialization (`to_dict`, `from_dict`), dependency lists, phase integers, and completion criteria dictionaries. |
| `runtime/task_planning/complexity.py` | Heuristic prompt complexity classifier | **VERIFIED.** Deterministic rule engine evaluating length, prefixes, enumeration patterns, chaining keywords, and domain markers (`LONG_RUNNING`, `RESEARCH_HEAVY`, `COMPLEX`, `MULTI_STEP`, `SIMPLE`). Zero LLM overhead. |
| `runtime/task_planning/planner.py` | Plan generator | **VERIFIED WITH ARCHETYPE CONSTRAINT.** Implements deterministic archetype generation for Desktop Apps & MCP Discovery. Implements LLM-assisted structured JSON generation (`_generate_plan_via_llm`) with fallback to a minimal safe 2-step default plan (`_build_default_plan`). |
| `runtime/task_planning/validator.py` | DAG structural and security validator | **VERIFIED.** Strict DAG validation using Kahn's algorithm (cycle rejection), phase barrier ordering, node ID uniqueness, edge caps (max 50 nodes, 100 edges), and execution type validation. |
| `runtime/task_planning/ledger.py` | SQLite-backed authoritative state manager | **VERIFIED.** Computes SHA-256 definition hashes for plan provenance. Commits to `task_plans` table. Enforces database column authority over cached JSON. Integrates with `ActiveTaskRegistry` and `EventBus`. |
| `runtime/task_planning/projection.py` | Markdown projection renderer | **VERIFIED.** Pure deterministic renderer generating `TASK_LEDGER.md` containing plan metadata, phase grouping, state icons (`[x]`, `[/]`, `[ ]`, `[!]`, `[-]`, `[?]`), execution durations, and artifact links. |
| `runtime/task_planning/bridge.py` | Phase 5 Workflow conversion & coordinator | **VERIFIED.** Converts `TaskPlan` into `WorkflowDefinition`, renders initial ledger projection, executes through `WorkflowEngine`, records node execution metrics, and commits final status. |
| `runtime/task_planning/service.py` | Unified façade (`TaskPlanningService`) | **VERIFIED.** Orchestrates classification, planning, validation, ledger persistence, bridge execution, and recovery. |
| `runtime/task_planning/recovery.py` | Startup crash reconciliation | **VERIFIED.** Scans for unclosed plans, updates status to `INTERRUPTED`, regenerates ledger projection, and isolates running processes as `OUTCOME_UNKNOWN`. |
| `runtime/workflows/models.py` | Phase 5 workflow primitives | **VERIFIED.** Authoritative workflow run, node execution, and dependency models. |
| `runtime/workflows/engine.py` | Deterministic DAG execution engine | **VERIFIED.** Executes topological waves, enforces timeouts, collects subprocess outputs, and commits node execution state to SQLite. |
| `runtime/persistence.py` | SQLite WAL schema & CRUD | **VERIFIED.** Tables `task_plans`, `workflow_runs`, `workflow_nodes` managed with WAL mode, transactions, and foreign keys. |
| `runtime/observability/task_registry.py` | Active task tracking and hierarchy | **VERIFIED.** In-memory thread-safe hierarchical task registry with parent-child links, cancellation tokens, and runtime telemetry. |
| `runtime/lifecycle.py` | Runtime initialization & startup recovery | **VERIFIED.** `PresenceRuntime` instantiates `TaskPlanningService` and runs `task_planning_service.reconcile_startup()` during system boot. |
| `runtime/session.py` | `AgentSession` & `AgentSessionFactory` | **VERIFIED.** Registers `presence.manage_plan` and `presence.create_plan` into the session tool registry. |
| `runtime/agent_executor.py` | LLM ReAct turn executor | **WIRING GAP IDENTIFIED.** Does not invoke `ComplexityClassifier` prior to provider invocation. |
| `runtime/tools/plan_tools.py` | Plan management tool definitions | **PARTIAL.** Implements `list_plans`, `get_status`, `cancel`, and `create_plan`. Does not implement `pause`, `resume`, `retry`, or `revise`. `cancel` incorrectly marked as `L0_OBSERVE`. |
| `runtime/tui/views/tasks_view.py` | View 04 TUI task tree | **VERIFIED.** Renders hierarchical task tree from `TaskRegistry`. Does not render `TASK_LEDGER.md` text directly; lacks automatic EventBus live-subscription refresh without manual click. |
| `runtime/tui/views/chat_view.py` | View 01 TUI chat interface | **VERIFIED.** Renders tool calls and artifact links. Does not render inline task progress ledgers. |

---

## 3. Architecture Wiring Trace (0.1)

```text
User Input (ChatView)
    │
    ▼
AgentSession.execute_turn()
    │
    ▼
AgentExecutor.execute_turn()
    │
    ├─► [ACTUAL RUNTIME PATH] ──────────────────────────────────────────┐
    │   1. Reconstruct context from SQLite (assembler.assemble)          │
    │   2. Fetch tool declarations (including presence.create_plan)     │
    │   3. Call provider.generate_stream() [LLM Provider Call]          │
    │        │                                                          │
    │        ├─ If Model calls presence.create_plan:                    │
    │        │       │                                                  │
    │        │       ▼                                                  │
    │        │  TaskDecompositionPlanner.plan()                         │
    │        │       │                                                  │
    │        │       ▼                                                  │
    │        │  TaskPlanValidator.validate()                            │
    │        │       │                                                  │
    │        │       ▼                                                  │
    │        │  TaskExecutionLedger.create_plan()                       │
    │        │       │                                                  │
    │        │       ▼                                                  │
    │        │  Returns Plan ID to Model (Execution deferred)          │
    │        │                                                          │
    │        └─ If Model generates direct tool calls:                   │
    │                Standard sequential ReAct loop (tokens accumulate) │
    │                                                                   │
    └─► [DIRECT SERVICE PATH: TaskPlanningService.evaluate_and_execute()]─┘
        1. ComplexityClassifier.classify(prompt)
             │
             ├─ SIMPLE: Return False (standard conversational response)
             │
             ▼ COMPLEX / RESEARCH_HEAVY / MULTI_STEP / LONG_RUNNING
        2. TaskDecompositionPlanner.plan(prompt)
             │
             ├─ Mode A: Deterministic Archetype (e.g. MCP & App audit)
             │
             └─ Mode B: LLM-assisted JSON DAG -> Fallback default plan
        3. TaskPlanValidator.validate(plan)
        4. TaskExecutionLedger.create_plan(plan) [SHA-256 Hash -> SQLite]
        5. WorkflowExecutionBridge.execute_plan()
             │
             ├─ Initial LedgerProjectionRenderer.render_markdown() -> ArtifactStore
             │
             ├─ WorkflowEngine.execute_workflow() [TOPOLOGICAL DAG WAVES]
             │    ├─ ProcessManager (Windows Job Objects)
             │    ├─ ToolRegistry & PermissionBoundary
             │    └─ SubagentManager
             │
             ├─ Extract authoritative node execution states
             │
             ├─ Final LedgerProjectionRenderer.render_markdown() -> TASK_LEDGER.md
             │
             └─ Commit terminal status (COMPLETED / FAILED / CANCELLED)
```

### Critical Architectural Finding
There is a fundamental divergence between the **Direct Service Pipeline** (`TaskPlanningService.evaluate_and_execute`) and the **Interactive TUI Chat Loop** (`AgentExecutor.execute_turn`).
Because `AgentExecutor` does not call `ComplexityClassifier` before invoking `provider.generate_stream`, the system cannot deterministically preempt complex tasks into DAG workflows without first asking the LLM. If the LLM provider experiences quota exhaustion or network errors (such as the Cloudflare HTTP 502 / 429 rate limit), the conversational session fails completely even though deterministic archetypes exist.

---

## 4. Single Source of Truth Verification (0.2)

1. **Authoritative Execution State:** SQLite WAL tables (`task_plans`, `workflow_runs`, `workflow_nodes`) are the sole authoritative persistence layer. All state transitions (`PENDING` $\to$ `RUNNING` $\to$ `COMPLETED` / `FAILED` / `OUTCOME_UNKNOWN`) are written to the database with timestamps and error records.
2. **Derived Markdown Projections:** `TASK_LEDGER.md` is strictly a derived projection created by `LedgerProjectionRenderer.render_markdown()`. The renderer reads from database snapshots or runtime objects. Manually editing markdown checkboxes (`[ ]` to `[x]`) on disk has zero impact on runtime state. If the markdown file is deleted or corrupted, it is regenerated idempotently from SQLite.
3. **LLM Non-Authority:** The LLM has zero authority to mark a task as completed. Task completion is determined deterministically by `WorkflowEngine` based on process exit codes (`EXIT_CODE_ZERO`), non-empty tool returns (`NON_EMPTY`), or artifact existence (`ARTIFACT_EXISTS`).
4. **Task Registry Alignment:** `ActiveTaskRegistry` acts as an in-memory observable mirror of active tasks. The ledger registers a root `TaskNode` in the registry upon plan creation, and workflow execution registers child nodes, maintaining an accurate hierarchy in memory.

---

## 5. Lifecycle and Recovery Integration (0.3)

1. **Initialization Sequence:** In `runtime/lifecycle.py`:
   - Persistence manager initializes SQLite WAL schema (Step 1).
   - EventBus and TaskRegistry initialized (Step 3).
   - WorkflowEngine initialized (Step 5e).
   - `TaskPlanningService` initialized (Step 5f) and attached to `PresenceRuntime.task_planning_service` and `AgentSessionFactory`.
   - Singletons are strictly enforced; no duplicate services are created.
2. **Startup Crash Recovery:** During `PresenceRuntime.start()` (Step 4b):
   - `task_planning_service.reconcile_startup()` is invoked.
   - It queries `persistence.reconcile_interrupted_plans()`.
   - Any plan left in `IN_PROGRESS` or `DRAFT` from an unclean shutdown is marked `INTERRUPTED`.
   - Completed workflow nodes are preserved. Running processes are marked `OUTCOME_UNKNOWN`.
   - A recovered `TASK_LEDGER.md` projection is rendered and stored in `ArtifactStore`.
3. **Shutdown Order:** `PresenceRuntime.stop()` terminates background heartbeat threads, stops the summary manager and search governor, records clean shutdown in SQLite, releases the Windows named mutex, and closes the database connection cleanly.

---

## 6. Audit Conclusion & Phase Gate 0 Sign-Off

- **Phase 0 Status:** **PASS WITH ADVISORY.**
- **Advisory:** While the TDEL engine, ledger, bridge, validator, and recovery subsystems are architecturally sound and verified, production conversational integration requires an explicit pre-execution hook in `AgentExecutor` or `AgentSession` to enable deterministic preemption of complex requests before LLM streaming.
