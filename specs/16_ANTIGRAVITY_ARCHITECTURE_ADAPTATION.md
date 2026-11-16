# ANTIGRAVITY ARCHITECTURE ADAPTATION
## Forensic Pattern Adaptation Standard for Persistent Intelligent Presence
**Document Version:** 1.0.0-PROD-SPEC  
**Classification:** Systems Architecture & Reverse-Engineering Adaptation Standard  
**Operating Environment:** Windows 11 x64 (User Session 1+)  
**Target Reference:** Google Antigravity Internal Runtime Architecture  

---

## 1. PURPOSE & ARCHITECTURAL DIRECTIVE

This specification formally evaluates every forensic architectural pattern extracted during our reverse-engineering of Google Antigravity and defines its concrete adaptation into the backend of our **Persistent Intelligent Presence**.

### Principles of Adaptation:
1. **Architectural & Functional Parity, Not Source-Code Duplication:** We independently reproduce the observed architectural patterns without copying proprietary source code or circumventing software protections.
2. **Distinguish Truth from Speculation:** Every adapted pattern explicitly documents its empirical evidence source, confidence classification, and operational scope.
3. **Desktop-First Optimization:** Where Antigravity features were optimized for IDE-specific code editing, we adapt them for persistent ambient desktop assistance, operating system control, and background task resilience.
4. **Preserve Milestone 0 Substrates:** We build upon our verified Windows-native foundation (Python 3.12, Win32 `ctypes`, SQLite WAL, 4-tier permission gate, single-instance mutex) rather than rewriting existing working components.

---

## 2. FORENSIC PATTERN ADAPTATION MATRIX

Below is the definitive analysis of the 14 major Antigravity architectural patterns:

### Pattern 1: Independent Compiled Backend Execution Engine

- **Original Antigravity Observation:** Antigravity physically separates its user interface (Chromium/Electron shell) from its core intelligence engine, which runs as an independent 198 MB compiled Go binary (`language_server.exe`, Go 1.28). The engine handles all conversation persistence, agent loops, MCP execution, and tool dispatching over local loopback HTTP/HTTPS and CLI subcommands (`agentapi`).
- **Evidence Source:** `recon/reports/06_RUNTIME_FORENSIC_VALIDATION.md` Section 1; PE header analysis of `resources/bin/language_server.exe`; `daemon/hub_59064.json`.
- **Confidence Level:** **VERIFIED** (Direct binary disassembly, live process inspection).
- **Intended Equivalent in Our Backend:** The Presence Runtime operates as a completely headless, independent background engine (`presence_engine`). It manages conversation lifecycles, execution trajectories, tool dispatch, and background tasks independently of any future visual presentation (Dynamic Island or HUD).
- **Required Modifications for Desktop Usage:** 
  - Antigravity uses local loopback HTTP ports (`59063`/`59064`) registered in JSON lockfiles. For our Windows desktop companion, we will use Windows Named Pipes (`\\.\pipe\PresenceEnginePipe`) for low-latency, zero-port-conflict local IPC with DACL-enforced user session security.
- **Existing Components Reused:** `runtime/lifecycle.py`, `runtime/platform_win32.py` (Named mutex, console control handlers).
- **Implementation Work:** Named pipe IPC server, headless engine supervisor loop.

---

### Pattern 2: Conversation-Scoped SQLite Relational Databases

- **Original Antigravity Observation:** Antigravity does not store conversation state in one monolithic database or flat JSON file. Every conversation is allocated a dedicated SQLite 3 database (`conversations/<uuid>.db`) containing relational tables: `trajectory_meta`, `steps`, `gen_metadata`, `parent_references`, and `permissions`.
- **Evidence Source:** `recon/reports/06_RUNTIME_FORENSIC_VALIDATION.md` Section 1; SQLite schema extraction of `00c4a4e9-7ee5-42c3-b607-4d584503da8a.db`.
- **Confidence Level:** **VERIFIED** (Direct database inspection).
- **Intended Equivalent in Our Backend:** We adopt identical conversation-scoped databases: `%LOCALAPPDATA%\Presence\conversations\<conversation_id>.db`. Each conversation owns its private relational store, isolating database locks, crash domains, and maintenance operations.
- **Required Modifications for Desktop Usage:** None. Perfect fit for desktop isolation, multi-task separation, and safe multi-agent branching.
- **Existing Components Reused:** `runtime/persistence.py` (WAL pragmas, connection pooling, ACID transaction boundaries).
- **Implementation Work:** Conversation-scoped database factory, table schemas (`steps`, `trajectory_meta`, `permissions_log`, `task_records`).

---

### Pattern 3: Dual-Journal Architecture (Transactional SQLite + Streaming JSONL)

- **Original Antigravity Observation:** Antigravity maintains two separate journals with strictly defined roles:
  1. `conversations/<uuid>.db` (SQLite): Authoritative, transactional store containing strongly typed step records (`steps.idx PRIMARY KEY`), permissions, and completion statuses.
  2. `brain/<uuid>/.system_generated/logs/transcript.jsonl`: Append-only streaming event log capturing raw user prompts, planner thinking, streaming response chunks (`GENERIC`), and UI rendering hints.
  - Forensic discovery proved that `transcript.jsonl` is an asynchronous event log containing partial streaming deltas, while SQLite is the definitive canonical state committed on turn completion.
- **Evidence Source:** `recon/reports/10_ARCHITECTURAL_FREEZE_AND_CORRECTIONS.md` Section 1; step-count comparison (289 SQLite steps vs 317 JSONL entries with 25 chunk duplicates).
- **Confidence Level:** **VERIFIED** (Direct empirical reconciliation).
- **Intended Equivalent in Our Backend:** We adopt the identical dual-journal architecture:
  - SQLite `steps` table is the **authoritative canonical state store**.
  - `transcript.jsonl` is the **asynchronous streaming event log** used by the UI to render real-time text generation and tool progress.
  - If a streaming write stalls, state is reconstructed from SQLite. If a crash occurs between stream append and SQLite commit, the engine reconciles SQLite from the trailing JSONL record.
- **Required Modifications for Desktop Usage:** None. Ensures instantaneous UI responsiveness during LLM streaming while maintaining strict transactional crash guarantees.
- **Existing Components Reused:** `runtime/persistence.py`, `runtime/logging.py`.
- **Implementation Work:** `runtime/journal.py` (asynchronous event stream writer), `runtime/trajectory.py` (atomic step committer and reconciler).

---

### Pattern 4: Global Conversation Summaries & SQLite FTS5 Indexing

- **Original Antigravity Observation:** Antigravity maintains a global database `conversation_summaries.db` with two critical tables:
  1. `conversation_summaries`: Relational metadata across all conversations (`parent_conversation_id`, `nesting_depth`, `not_fully_idle`, `killed`, `raw_summary`).
  2. `conversations_fts`: A high-performance SQLite FTS5 virtual table with `unicode61` tokenization and prefix indexing (`1 2 3`) over `title`, `workspace`, `user_body`, and `agent_body`.
  - Summaries and FTS indices are updated asynchronously in the background when an agent transitions to idle (`not_fully_idle = 0`) or crosses step thresholds.
- **Evidence Source:** `recon/reports/07_AGENT_ORCHESTRATION_DEEP_DIVE.md` Section 1; `recon/reports/08_CONTEXT_AND_TOOL_EXECUTION.md` Section 2; `conversation_summaries.db` live schema.
- **Confidence Level:** **VERIFIED** (Direct schema and query verification).
- **Intended Equivalent in Our Backend:** `%LOCALAPPDATA%\Presence\data\global_summaries.db` containing `conversation_metadata` and `conversations_fts` (SQLite FTS5). Enables sub-millisecond full-text recall of any conversation, task, or tool output ever executed on the user's laptop.
- **Required Modifications for Desktop Usage:** Add desktop-specific metadata columns: `active_window`, `focused_app`, `user_presence_state`.
- **Existing Components Reused:** Python `sqlite3` built-in FTS5 module support.
- **Implementation Work:** `runtime/summaries.py` (background indexing worker, FTS5 schema manager, search query interface).

---

### Pattern 5: Iterative Agent Execution Loop with Tool Reflection

- **Original Antigravity Observation:** A conversation is not a stateless request/response pair. It is an iterative execution loop:
  1. Receive user intent.
  2. Reconstruct context from SQLite steps + summary.
  3. Dispatch prompt to model provider.
  4. Stream model response, parsing tool invocation calls.
  5. Validate requested tools against permission gate.
  6. Execute permitted tools and capture output.
  7. Persist step transactionally to SQLite and append to JSONL.
  8. Inject tool result back into prompt and repeat loop until model emits final completion or requests clarification.
- **Evidence Source:** `recon/reports/06_RUNTIME_FORENSIC_VALIDATION.md` Section 3; `recon/reports/08_CONTEXT_AND_TOOL_EXECUTION.md` Section 1.
- **Confidence Level:** **VERIFIED** (Observed turn trajectories and step records).
- **Intended Equivalent in Our Backend:** A persistent agent execution loop (`AgentExecutor`) driving multi-turn task completion, autonomous tool chaining, and interactive pauses when user consent is required.
- **Required Modifications for Desktop Usage:** Integrated directly with the 4-tier Permission Gate (`runtime/permissions.py`) to prevent untrusted model actions from bypassing security.
- **Existing Components Reused:** `runtime/events.py`, `runtime/permissions.py`, `runtime/contracts.py`.
- **Implementation Work:** `runtime/agent.py` (execution loop, tool parser, reflection loop, cancellation token handler).

---

### Pattern 6: Hierarchical Subagent Orchestration Tree

- **Original Antigravity Observation:** Antigravity orchestrates subagents as a parent-child tree. Subagents are spawned via `invoke_subagent(TypeName, Role, Workspace)`, creating a child record in `conversation_summaries` with `parent_conversation_id`, `nesting_depth = parent.depth + 1`, and `not_fully_idle = 1`. Subagents run independently with their own conversation IDs and SQLite databases. When a parent is terminated, cascading cancellation sets `killed = 1` and cleans up temporary worktrees while strictly preserving logs and artifacts.
- **Evidence Source:** `recon/reports/07_AGENT_ORCHESTRATION_DEEP_DIVE.md` Section 1; `recon/reports/09_RUNTIME_BEHAVIOUR_AND_FAILURE_ANALYSIS.md` Section 1.
- **Confidence Level:** **VERIFIED** (Direct database record inspection of child/parent pairs).
- **Intended Equivalent in Our Backend:** Hierarchical multi-agent manager (`SubagentCoordinator`). Supports delegating long-running research, file scanning, or application monitoring to background specialist subagents without locking the main interactive conversation.
- **Required Modifications for Desktop Usage:** 
  - Restrict default nesting depth to 2 (Parent -> Specialist) to avoid runaway agent recursion on a laptop.
  - Implement hard resource quotas per child agent (RAM, CPU, execution time).
- **Existing Components Reused:** `runtime/events.py`, `runtime/lifecycle.py`.
- **Implementation Work:** `runtime/subagents.py` (subagent lifecycle coordinator, cascading tree cancellation, lineage queries).

---

### Pattern 7: File-Backed Inter-Agent Messaging Spool (`read.json`)

- **Original Antigravity Observation:** Antigravity communicates between agents, subagents, and background tasks using a persistent, file-backed message bus in `.system_generated/messages/`:
  - Each message is an atomic JSON file: `<uuid>.json` containing `sender`, `recipient`, `priority: MESSAGE_PRIORITY_HIGH`, `timestamp`, and `content`.
  - Delivery and read acknowledgments are tracked in `.system_generated/messages/read.json` mapping `{ "<uuid>": true }`.
  - Incoming messages trigger reactive wakeups on the recipient turn loop without busy-wait polling.
- **Evidence Source:** `recon/reports/06_RUNTIME_FORENSIC_VALIDATION.md` Section 1 & 2; `recon/reports/07_AGENT_ORCHESTRATION_DEEP_DIVE.md` Section 3; inspection of `12dd8929-...json`.
- **Confidence Level:** **VERIFIED** (Direct JSON file and receipt inspection).
- **Intended Equivalent in Our Backend:** File-backed message spool in `%LOCALAPPDATA%\Presence\messages\spool\` with atomic rename semantics and `read.json` state index.
- **Required Modifications for Desktop Usage:** 
  - Enhance with Win32 filesystem change notifications (`ReadDirectoryChangesW`) so that worker message arrivals wake sleeping threads instantly with zero CPU consumption.
- **Existing Components Reused:** `runtime/logging.py` (atomic file operations).
- **Implementation Work:** `runtime/messaging.py` (message envelope schema, spool writer, acknowledgment tracker, Win32 reactive wakeup watcher).

---

### Pattern 8: MCP Stdio Process Supervision & Hybrid Lazy Schema Hydration

- **Original Antigravity Observation:** Antigravity manages Model Context Protocol (MCP) servers as external child processes over `stdio` JSON-RPC. It partitions tools into:
  - **Eager Tools:** Standard system tools (`read_file`, `run_command`, `edit_file`) embedded directly into system instructions.
  - **Lazy Tools:** Over 150 specialized tools (e.g. REA reverse engineering tools) stored as individual JSON schema files on disk in `<appDataDir>/mcp/<server>/<tool>.json`. The engine passes lightweight names and descriptions to the model and hydrates full parameter schemas only when invoked.
  - Hard context deadline: 180 seconds (`context deadline exceeded`).
- **Evidence Source:** `recon/reports/06_RUNTIME_FORENSIC_VALIDATION.md` Section 1; `recon/reports/08_CONTEXT_AND_TOOL_EXECUTION.md` Section 3; schema inspection of `C:\Users\chait\.gemini\antigravity\mcp\rea\`.
- **Confidence Level:** **VERIFIED** (Direct filesystem schema inspection, live timeout verification).
- **Intended Equivalent in Our Backend:** Stdio MCP process supervisor (`MCPManager`) with on-disk lazy schema caching in `%LOCALAPPDATA%\Presence\mcp\schemas\`.
- **Required Modifications for Desktop Usage:** 
  - Dynamic timeouts: Fast local queries (e.g. app status, clipboard) receive a 10s timeout; long tasks (e.g. large file indexing, builds) receive up to 180s.
  - Lazy schema hydration reduces prompt token usage by >85%, essential for conserving cloud API quotas and local context windows.
- **Existing Components Reused:** Python standard library `subprocess`, `json`, `threading`.
- **Implementation Work:** `runtime/mcp/supervisor.py` (JSON-RPC stdio client, process watcher, lazy schema cache, deadline timer).

---

### Pattern 9: Dynamic Tool Output Offloading (>46KB Boundary)

- **Original Antigravity Observation:** When a tool execution produces voluminous output exceeding a defined threshold (~46,080 bytes), Antigravity does not dump the entire text into the active LLM context. Instead, it writes the full content to `.system_generated/steps/<step_id>/output.txt`, sanitizes a preview into the prompt, and provides a clickable file reference (`read_file`).
- **Evidence Source:** `recon/reports/06_RUNTIME_FORENSIC_VALIDATION.md` Section 2 (Item 9); `.system_generated/steps/148/output.txt`.
- **Confidence Level:** **VERIFIED** (Direct filesystem inspection of truncated command output).
- **Intended Equivalent in Our Backend:** Automatic output offloading gate in `ToolExecutor`:
  - Default threshold: 40 KB (configurable per tool).
  - Outputs under threshold are passed directly into model context.
  - Outputs exceeding threshold are written to `%LOCALAPPDATA%\Presence\artifacts\<conv_id>\step_<idx>_output.log`, returning head/tail previews and byte offsets to the model.
- **Required Modifications for Desktop Usage:** None. Prevents runaway context bloat and catastrophic token exhaustion when running CLI commands (e.g. `dir /s`, system event logs).
- **Existing Components Reused:** `runtime/logging.py` (path sanitization, safe file writing).
- **Implementation Work:** Output offload filter in `runtime/tools/executor.py`.

---

### Pattern 10: Windows Job Object Subprocess Containment

- **Original Antigravity Observation:** Antigravity binds all spawned child worker processes (such as MCP servers, CLI compilers, sidecars) to Windows Job Objects configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. When the parent engine terminates or a tool times out, closing the job handle triggers kernel-level atomic termination of the entire child process tree.
- **Evidence Source:** `recon/reports/09_RUNTIME_BEHAVIOUR_AND_FAILURE_ANALYSIS.md` Section 4; `recon/reports/10_ARCHITECTURAL_FREEZE_AND_CORRECTIONS.md` Section 4; PE imports of `CreateJobObjectW`, `AssignProcessToJobObject`.
- **Confidence Level:** **VERIFIED** (Binary imports confirmed; OS semantics verified).
- **Intended Equivalent in Our Backend:** Native Win32 Job Object supervisor (`runtime/platform_win32.py` / `runtime/jobs.py`):
  - Every external tool, background worker, or MCP server is assigned to a dedicated Job Object immediately upon creation.
  - `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is unconditionally set.
  - `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK` is set to 0 (breakaway explicitly forbidden).
- **Required Modifications for Desktop Usage:** None. Guaranteed zero orphaned background processes when our desktop companion restarts or updates.
- **Existing Components Reused:** `runtime/platform_win32.py` (Win32 ctypes bindings).
- **Implementation Work:** `runtime/jobs.py` (Job Object ctypes wrappers, suspended process launcher).

---

### Pattern 11: Multi-Workspace Isolation (Inherit, Branch, Share)

- **Original Antigravity Observation:** Antigravity defines three distinct workspace topologies for agents:
  1. `inherit`: Child operates directly in the parent directory (zero-copy, read-only or coordinated tasks).
  2. `branch`: Child receives an isolated Git clone/branch.
  3. `share`: Child receives a dedicated `git worktree` sharing the parent's underlying object database, preventing multi-gigabyte duplication while isolating work files.
- **Evidence Source:** `recon/reports/07_AGENT_ORCHESTRATION_DEEP_DIVE.md` Section 2; swarm coordinator skill rules.
- **Confidence Level:** **VERIFIED** (Direct inspection of workspace parameters and skill documentation).
- **Intended Equivalent in Our Backend:** Workspace Isolation Manager (`WorkspaceManager`):
  - For desktop file modifications, agents operate either in `inherit` (with file locking) or in `branch`/`share` using Git worktrees when a Git repository is detected.
  - Prevents concurrent agents from corrupting the user's active documents or project files.
- **Required Modifications for Desktop Usage:** 
  - Desktop companion must also support non-Git desktop folders (e.g. `Documents`, `Downloads`) using copy-on-write scratch directories (`%LOCALAPPDATA%\Presence\scratch\<conv_id>\`).
- **Existing Components Reused:** Standard library `pathlib`, `shutil`.
- **Implementation Work:** `runtime/workspaces.py` (workspace mode resolver, Git worktree lifecycle manager, scratch directory manager).

---

### Pattern 12: Local-First Context Reconstruction vs Provider KV Caching

- **Original Antigravity Observation:** Antigravity strictly distinguishes local authoritative context reconstruction from cloud-side KV cache acceleration:
  - Local SQLite `steps` and `raw_summary` deterministically reconstruct the exact prompt state offline without depending on server-side session memory.
  - Server-side KV caching (`cache_read_tokens`) is an opportunistic cloud optimization for invariant system prefixes. If the cache expires, the local engine recomputes tokens seamlessly.
- **Evidence Source:** `recon/reports/10_ARCHITECTURAL_FREEZE_AND_CORRECTIONS.md` Section 3; `transcript.jsonl` telemetry inspection.
- **Confidence Level:** **VERIFIED** (Empirical cache telemetry inspection).
- **Intended Equivalent in Our Backend:** Deterministic Context Reconstruction Engine (`ContextAssembler`):
  - Builds prompt context directly from local SQLite step history and conversation summaries.
  - Fully provider-independent: works identically whether invoking Google Gemini, Anthropic Claude, OpenAI, or a local Ollama/llama.cpp model.
  - If a provider session disconnects, the next prompt is fully self-contained.
- **Required Modifications for Desktop Usage:** Include desktop ambient state in the reconstructed context (active window, system load, time of day).
- **Existing Components Reused:** `runtime/persistence.py`, `runtime/contracts.py`.
- **Implementation Work:** `runtime/reconstruction.py` (chronological step replayer, sliding window tokenizer, prompt budget enforcer).

---

### Pattern 13: Explicit 7-State Task Lifecycle & Recovery Engine

- **Original Antigravity Observation:** Antigravity implements an explicit task recovery lifecycle:
  - `CREATED -> RUNNING -> COMPLETED | FAILED | CANCELLED | INTERRUPTED -> RECOVERY -> RESUMED | RESTARTED`.
  - Handles three concrete post-crash scenarios:
    1. Reconnecting to a still-running background process.
    2. Resuming an exited task whose exit code was logged.
    3. Recovering an interrupted/disappeared task through idempotency evaluation.
- **Evidence Source:** `recon/reports/10_ARCHITECTURAL_FREEZE_AND_CORRECTIONS.md` Section 2 & 5; task status logs and error envelopes.
- **Confidence Level:** **VERIFIED** (Empirical crash reconciliation analysis).
- **Intended Equivalent in Our Backend:** Task Recovery Engine (`TaskRecoveryManager`):
  - On engine startup, scans `%LOCALAPPDATA%\Presence\data\runtime.db` and active conversation databases for incomplete tasks (`status = RUNNING`).
  - Classifies each task into one of the three recovery scenarios: Reconnect, Resume, or Recovery.
  - Non-idempotent actions (file deletion, email dispatch) are never repeated without user re-confirmation.
- **Existing Components Reused:** `runtime/persistence.py` (`runtime_recovery` table), `runtime/permissions.py`.
- **Implementation Work:** `runtime/recovery.py` (crash scanner, PID alive check, log exit parser, idempotency evaluator).

---

### Pattern 14: Provider and Context Independence

- **Original Antigravity Observation:** Antigravity isolates model reasoning from persistence and tool execution through clear protocol buffers and provider-agnostic step payloads.
- **Evidence Source:** `recon/reports/06_RUNTIME_FORENSIC_VALIDATION.md` Section 2; `antigravity_state.pbtxt`.
- **Confidence Level:** **VERIFIED** (Architecture audit).
- **Intended Equivalent in Our Backend:** Provider Abstraction Interface (`IModelProvider`):
  - Supports Google Gemini API, OpenAI-compatible APIs, and local LLM endpoints (Ollama, LM Studio).
  - Model changes do not affect conversation history, database schemas, or tool definitions.
- **Required Modifications for Desktop Usage:** Local model support for privacy-sensitive offline desktop operation.
- **Existing Components Reused:** `runtime/contracts.py` (`IModelProvider`).
- **Implementation Work:** `runtime/providers/` (Gemini provider, OpenAI-compatible provider, local provider).

---

## 3. ADAPTATION SUMMARY

| Antigravity Pattern | Desktop Adaptation | Status |
| :--- | :--- | :--- |
| **Compiled Language Server** | Headless `presence_engine` (Named Pipe IPC) | Planned (Phases 1-5) |
| **Conversation SQLite DBs** | `conversations/<uuid>.db` | Phase 1 Core |
| **Dual Journal (SQLite + JSONL)** | Transactional SQLite + Streaming `transcript.jsonl` | Phase 1 Core |
| **FTS5 Search Index** | `global_summaries.db` (FTS5 unicode61) | Phase 1 Core |
| **Iterative Execution Loop** | Multi-turn `AgentExecutor` with permission gate | Phase 2 Core |
| **Hierarchical Subagents** | `SubagentCoordinator` (Lineage & Quotas) | Phase 4 Core |
| **File-Backed Messages** | Spool queue with Win32 reactive wakeup | Phase 4 Core |
| **Lazy MCP Schemas** | On-disk JSON schema cache + stdio JSON-RPC | Phase 2 Core |
| **Tool Output Offload** | 40 KB threshold with disk offload | Phase 2 Core |
| **Win32 Job Objects** | Kernel Job Objects (`KILL_ON_JOB_CLOSE`) | Phase 5 Core |
| **Workspace Isolation** | Git worktree + Scratch isolation | Phase 5 Core |
| **Local Reconstruction** | Offline step replay & summary compaction | Phase 1 Core |
| **7-State Recovery** | 3-scenario crash reconciler | Phase 3 Core |
| **Provider Independence** | Decoupled `IModelProvider` adapters | Phase 2 Core |

---

*Specification 16 Certified. Baseline architectural patterns approved for implementation.*
