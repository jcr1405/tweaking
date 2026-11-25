# 02 — RUNTIME ARCHITECTURE REVIEW
## Forensic Investigation of Agent Lifecycle, Conversational Preemption, Workflow Scheduling, and Context Reconstruction

**Document ID:** PRESENCE-CAPSTONE-AUDIT-02  
**Audit Phase:** Phase 2 — Independent Architectural Investigations (Subagent A)  
**Target:** `runtime.session`, `runtime.agent_executor`, `runtime.task_planning`, `runtime.workflows`, `runtime.reconstruction`  
**Date:** 2026-11-20  
**Status:** Complete  

---

## 1. Executive Summary

Subagent A conducted an exhaustive forensic investigation into the core conversational and execution architecture of Presence. The investigation focused on how conversational requests transition from user input into deterministic DAG workflows or iterative ReAct loops, how context is reconstructed and compacted, and how subagent hierarchies are managed.

### Key Verified Capabilities
1. **Conversational Preemption Active:** `AgentSession.execute_turn` successfully intercepts complex requests before LLM streaming, evaluating task complexity via `ComplexityClassifier` and routing eligible requests directly to `TaskPlanningService`.
2. **Zero-Token Archetypes Verified:** Standard desktop workloads (software inventory, repository health, MCP discovery, build verification, system diagnostics) execute via validated DAGs with zero LLM token consumption.
3. **Sliding-Window Compaction:** `ContextAssembler` and `compact_intermediate_tool_output` prevent quadratic token explosion by compacting prior tool outputs into concise semantic summaries while retaining full outputs in `ArtifactStore`.
4. **DAG Acyclicity and Phase Grouping:** `WorkflowValidator` and `WorkflowScheduler` enforce topological dependency order, parallelizing independent nodes while honoring explicit dependencies and verification barriers.

---

## 2. In-Depth Component Analysis

### 2.1 AgentSession & AgentExecutor Lifecycle
- **Session Boundary Isolation:** `AgentSessionFactory` composes `AgentSession` with conversation-isolated `ToolRegistry` and `PermissionBoundary` instances. Each session receives a distinct UUID, trajectory database, and event journal.
- **Preemption Interception:**
  ```python
  # runtime/session.py
  if self.task_planning_service is not None:
      if not ComplexityClassifier.is_discussion_only(user_input):
          complexity = self.task_planning_service.classifier.classify(user_input)
          if complexity != ComplexityCategory.SIMPLE:
              # Divert to TDEL execution
  ```
  This eliminates unbounded ReAct loops for routine administrative tasks.
- **Turn State Lifecycle:**
  `TURN_STARTED` -> `PLAN_CREATED` (if preempted) -> `PLAN_COMPLETED` -> `TEXT_DELTA` -> `AGENT_TURN` -> `TURN_COMPLETED`.
  If not preempted:
  `TURN_STARTED` -> `MODEL_REQUESTED` -> `THINKING_DELTA` / `TEXT_DELTA` -> `TOOL_REQUESTED` -> `TOOL_STARTED` -> `TOOL_COMPLETED` -> `TURN_COMPLETED`.

### 2.2 Workflow Engine DAG Scheduling (`runtime/workflows/`)
- **Topological Phasing:** The `WorkflowScheduler` partitions execution into discrete phases:
  - Phase 1: Independent inspection / collection tasks (executed concurrently up to `max_parallel_nodes`).
  - Phase 2+: Dependent transformation, testing, or validation nodes.
  - Verification Nodes: Execute assertions (`EXIT_CODE_ZERO`, `NON_EMPTY`, `REGEX_MATCH`) against prior node outputs.
  - Final Phase: Aggregation nodes that synthesize deliverables into `ArtifactStore`.
- **Failure Policy Semantics:**
  - `FAIL_FAST`: Immediately aborts running tasks and cancels unstarted downstream nodes upon the first node failure.
  - `CONTINUE_INDEPENDENT`: Allows non-dependent branches to complete while marking dependent nodes as `SKIPPED`.

### 2.3 Subagent Lifecycle & Isolation (`runtime/subagents/`)
- **Parent-Child Decoupling:** Subagents run in isolated conversation contexts (`conv_<uuid>`) with their own SQLite trajectory and event journals.
- **Recursion Depth Ceiling:** Enforces `max_recursion_depth = 3`. Subagents cannot spawn subagents beyond this ceiling.
- **Concurrency Guards:** Max 5 global subagents, max 3 subagents per parent conversation.
- **Tool Allowlist Enforcement:** Child sessions undergo strict role-based tool filtering (e.g., `RESEARCH` subagents are stripped of process management, filesystem modification, and nested subagent invocation tools).

### 2.4 Context Reconstruction & Sliding-Window Compaction (`runtime/reconstruction.py`)
- **Deterministic Replay:** Context is rebuilt directly from SQLite steps rather than maintained as volatile in-memory chat objects.
- **Tool Output Offloading:** When tool outputs exceed 40 KB, they are offloaded to `ArtifactStore` on disk (`artifacts/<conv_id>/<art_id>.bin`), and only a preview snippet is written to the trajectory step.
- **Compaction Heuristic:** Intermediate tool calls from earlier iterations within the same turn are compacted to `{"status": status, "artifact_id": art_id, "summary": "..."}`. This prevents quadratic context bloat during multi-iteration turns.

---

## 3. Architectural Weaknesses & Gaps Identified

1. **Mid-Stream Provider Failover Gap:**
   If the active model provider (e.g., Gemini) raises a rate-limit exception (`429 Too Many Requests`) midway through a streaming token generation, the turn terminates with `TURN_FAILED`. Presence does not currently checkpoint partial thought streams and fail over dynamically to `FreeLLMAPIProvider`.
2. **Ambient Context Source Wiring:**
   The foreground window awareness source (`runtime/context/`) is not started by default unless `--context-awareness` is passed on the command line. When Presence runs as an interactive companion in the TUI, it operates without ambient desktop window awareness unless manually enabled.
3. **Subagent Memory Accounting:**
   Subagent memory and token consumption are tracked in the child conversation's trajectory store, but are not aggregated back into the parent session's top-level `GlobalSummaryManager` metadata.

---

## 4. Subagent A Verdict & Sign-Off

The core runtime and workflow architecture is structurally sound, highly modular, and achieves reliable, token-efficient execution. The identified gaps represent optimization and resilience opportunities rather than architectural blockers.
