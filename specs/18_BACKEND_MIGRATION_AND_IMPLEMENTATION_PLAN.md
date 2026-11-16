# BACKEND MIGRATION & IMPLEMENTATION PLAN
## Evolutionary Migration from Milestone 0 to Antigravity-Adapted Agent Engine
**Document Version:** 1.0.0-PROD-SPEC  
**Classification:** Systems Implementation Roadmap  
**Operating Environment:** Windows 11 x64 (User Session 1+)  
**Implementation Foundation:** Python 3.12 Native Windows Substrate (`ctypes`, `sqlite3`, native threads)  

---

## 1. PURPOSE & PRINCIPLES

This plan defines the step-by-step evolutionary migration from our verified **Milestone 0 Presence Runtime** to the **Antigravity-Adapted Agent Engine**.

### Guiding Principles:
1. **Preserve Verified Substrates:** We do not discard the existing Milestone 0 implementation under `runtime/`. It already provides single-instance mutex control, memory telemetry, clean console shutdown, structured logging with token sanitization, the typed event bus, 4-tier permission gate, and WAL persistence.
2. **Modular Extension over Monolithic Rewrite:** Every existing module evolves into a specific role within the larger engine without duplicate databases, event buses, or lifecycle managers.
3. **Phased Incremental Execution:** Implementation proceeds in five discrete phases. Phase 1 is executed and empirically validated first before subsequent phases commence.
4. **Zero-Package Dependency Enforcement:** Maintain zero-package-install constraint (use Python 3.12 standard library + Win32 native APIs via `ctypes`).

---

## 2. EXISTING COMPONENT EVOLUTION MAPPING

| Existing Module | Milestone 0 Responsibility | New Backend Engine Responsibility | Action |
| :--- | :--- | :--- | :--- |
| `platform_win32.py` | Named mutex, process memory, console ctrl handler | Retains core Win32 primitives; extended with Windows Job Objects (`CreateJobObjectW`, `AssignProcessToJobObject`) and Named Pipes | **Preserve & Extend** |
| `logging.py` | Log rotation, sensitive secret & token scrubbing | Retains engine logging; extended with structured trajectory event formats and artifact write guards | **Preserve & Extend** |
| `events.py` | In-memory typed event bus with error isolation | Core internal event fabric for agent state transitions, tool execution events, and permission escalations | **Preserve & Harden** |
| `permissions.py` | 4-Tier Authority Gate (L0–L3) & consent callbacks | Authoritative security broker for all model tool calls, MCP execution requests, and subagent capabilities | **Preserve & Harden** |
| `persistence.py` | Global `runtime.db` for config & crash recovery | Retains runtime configuration and global instance state; complemented by conversation-scoped DB manager and FTS5 summaries | **Preserve & Partition** |
| `commands.py` | Sub-millisecond deterministic local commands | Core deterministic fast-path execution; bypassed for LLM reasoning but preserved for immediate engine controls | **Preserve & Extend** |
| `contracts.py` | Abstract system interfaces | Formal contracts for `IModelProvider`, `IMCPClient`, `ITrajectoryStore`, and `IWorkspaceManager` | **Preserve & Extend** |
| `lifecycle.py` | 5-State engine lifecycle coordinator | Master lifecycle coordinator managing the engine loop, worker pools, and graceful shutdown sequencing | **Preserve & Extend** |
| `main.py` | CLI entry point and diagnostic runner | Engine CLI host supporting `--headless`, `--status`, and future Named Pipe server daemon | **Preserve & Extend** |

---

## 3. IMPLEMENTATION ROADMAP: 5 BACKEND PHASES

```
+---------------------------------------------------------------------------------+
| PHASE 1: CONVERSATION & TRAJECTORY ENGINE (IMMEDIATE TARGET)                    |
| - Conversation identity & scoped SQLite DBs (conversations/<uuid>.db)          |
| - Transactional step commits (steps table with monotonic idx)                   |
| - Asynchronous streaming event log (transcript.jsonl)                           |
| - Global metadata & FTS5 search indexing (conversation_summaries.db)            |
| - Deterministic offline context reconstruction & summary sliding window         |
+---------------------------------------------------------------------------------+
                                       │
                                       ▼
+---------------------------------------------------------------------------------+
| PHASE 2: AGENT EXECUTION & TOOL INFRASTRUCTURE                                  |
| - Iterative agent turn loop (AgentExecutor) with tool reflection                |
| - Pluggable model provider adapters (Gemini API, OpenAI-compatible, Local)      |
| - Stdio MCP process supervisor & lazy on-disk schema hydration                  |
| - Dynamic tool output offloading (>40 KB threshold to artifacts)                |
| - Non-bypassable 4-tier permission gate mediation                               |
+---------------------------------------------------------------------------------+
                                       │
                                       ▼
+---------------------------------------------------------------------------------+
| PHASE 3: DURABLE TASKS & CRASH RECOVERY                                         |
| - Explicit 7-state task machine (CREATED -> RUNNING -> INTERRUPTED -> RECOVERY) |
| - 3-Scenario startup crash reconciliation (Reconnect, Resume, Recover)          |
| - Step-level idempotency evaluation and safe resumption                         |
+---------------------------------------------------------------------------------+
                                       │
                                       ▼
+---------------------------------------------------------------------------------+
| PHASE 4: MULTI-AGENT ORCHESTRATION & MESSAGING                                  |
| - Hierarchical subagent trees (parent_conversation_id, nesting depth <= 2)      |
| - File-backed message spool (%LOCALAPPDATA%\Presence\messages\spool\)           |
| - Atomic read acknowledgments (read.json) & Win32 reactive wakeups              |
| - Cascading cancellation and isolated failure propagation                       |
+---------------------------------------------------------------------------------+
                                       │
                                       ▼
+---------------------------------------------------------------------------------+
| PHASE 5: WORKSPACE ISOLATION & SUBPROCESS SUPERVISION                           |
| - Workspace modes: inherit, branch, and share                                   |
| - Git worktree isolation for concurrent authoring                               |
| - Native Win32 Job Objects with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE              |
| - Named Pipe IPC server (\\.\pipe\PresenceEnginePipe) for UI Dynamic Island     |
+---------------------------------------------------------------------------------+
```

---

## 4. DETAILED SPECIFICATION FOR PHASE 1: CONVERSATION & TRAJECTORY ENGINE

### 4.1 Subsystems to Implement

1. **`runtime/conversation.py` (Conversation Identity & Context)**:
   - Manages unique conversation UUIDs, title, creation timestamps, workspace path, and nesting depth.
   - Provides clean creation, serialization, and retrieval interfaces.

2. **`runtime/trajectory.py` (Scoped Database & Monotonic Step Engine)**:
   - Manages conversation-scoped SQLite databases (`%LOCALAPPDATA%\Presence\conversations\<uuid>.db`).
   - Configures WAL mode, normal synchronous pragmas, and foreign keys.
   - Creates and operates `steps`, `trajectory_meta`, `gen_metadata`, and `permissions_audit` tables.
   - Commits steps transactionally with monotonic primary key `idx`.
   - Replays steps in strict chronological order.

3. **`runtime/journal.py` (Streaming Event Log)**:
   - Manages append-only streaming event logs (`%LOCALAPPDATA%\Presence\logs\<uuid>\transcript.jsonl`).
   - Supports streaming event types: `USER_INPUT`, `PLANNER_RESPONSE`, `GENERIC` (deltas), `SYSTEM_MESSAGE`.
   - Handles asynchronous flushing and crash recovery reconciliation with SQLite.

4. **`runtime/summaries.py` (Global Summaries & SQLite FTS5 Search)**:
   - Operates `%LOCALAPPDATA%\Presence\data\conversation_summaries.db`.
   - Maintains the `conversation_summaries` metadata table.
   - Maintains the `conversations_fts` virtual table using SQLite FTS5 with `unicode61` tokenization and prefix search (`1 2 3`).
   - Indexes user inputs, agent responses, and tool contents across all historical conversations.
   - Provides sub-millisecond query search API (`search_conversations(query, limit=10)`).

5. **`runtime/reconstruction.py` (Deterministic Local Context Assembler)**:
   - Reassembles prompt context from local SQLite `steps` without external server dependencies.
   - Enforces configurable token budgets:
     - Under budget: Full chronological step history.
     - Over budget: Invariant instructions + `raw_summary` + sliding active turn window.
   - Operates 100% offline.

---

## 5. ACCEPTANCE CRITERIA FOR PHASE 1

1. **Conversation Isolation:** Multiple conversations can be created and run concurrently, each writing to its independent SQLite database without lock collisions.
2. **Dual-Journal Consistency:** Every step committed to the SQLite database is mirrored to the streaming JSONL event stream, with SQLite established as authoritative.
3. **Monotonic Ordering & Idempotency:** The `steps.idx` primary key prevents duplicate step insertions under all circumstances.
4. **Sub-Millisecond Search Recall:** SQLite FTS5 queries against indexed conversation text return accurate conversation matches and byte/step snippets in < 5 ms.
5. **Context Reconstruction Fidelity:** Reconstructing a multi-turn conversation after engine restart produces an exact, model-ready prompt sequence without requiring remote provider session retention.
6. **Zero Memory Bloat:** The test suite and runtime memory footprint remain well below 50 MB Working Set RAM.
7. **100% Automated Test Coverage:** Comprehensive test suite in `tests/` passing with zero errors and zero failures.

---

*Specification 18 Certified. Phase 1 Implementation Authorized.*
