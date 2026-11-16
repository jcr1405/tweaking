# SYSTEM ARCHITECTURE SPECIFICATION
## Ambient Intelligent Computing Environment (AICE)
**Document Version:** 1.0.0-PROD-SPEC  
**Classification:** Core System Architecture Blueprint  
**Target Platform:** Windows 11 x64 (AMD Ryzen AI / Intel Core Ultra, NVIDIA RTX / Discrete GPU, 16GB+ RAM)  

---

## 1. COMPONENT TOPOLOGY & PROCESS ARCHITECTURE

The Ambient Intelligent Computing Environment (AICE) is architected as a distributed, decoupled multi-process system structured across Windows security and session boundaries.

```
+===================================================================================+
|                               SESSION 0 (MACHINE CONTEXT)                         |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                 AICE Machine Service (aice_svc.exe)                         |  |
|  |   - Windows Service Control Manager (SCM) Lifecycle Target                  |  |
|  |   - Machine-wide Hardware & Resource Telemetry Monitor                      |  |
|  |   - Global Database Lock / Backup / WAL Checkpoint Supervisor               |  |
|  |   - Named Pipe IPC Listener Server (\\.\pipe\aice_machine_ipc)              |  |
|  |   - Job Object Supervisor (Global Scope)                                    |  |
|  +-----------------------------------------------------------------------------+  |
+===================================================================================+
                                         |
                       Windows Named Pipe IPC (Local Only)
                    Security: Explicit DACL (User SID Only)
                                         |
+===================================================================================+
|                           SESSION 1+ (INTERACTIVE USER CONTEXT)                   |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |             AICE User Session Agent (aice_host.exe)                         |  |
|  |   - Interactive Logon Bootstrap (Task Scheduler / RunKey)                   |  |
|  |   - Desktop Perception Pipeline: UIA Monitor, WGC Capture, WinHook Focus    |  |
|  |   - Event Reactor & Ambient Context Synthesizer                             |  |
|  |   - Deterministic Capability Broker & Authority Matrix Enforcement          |  |
|  |   - Planning & Multi-Agent Swarm Orchestrator                               |  |
|  |   - Local SQLite State Engine (WAL Mode) & Protobuf Step Replayer           |  |
|  +-----------------------------------------------------------------------------+  |
|           |                                                      |                |
|           v (Direct Process Assignment)                          v (Job Object)   |
|  +-----------------------------------+         +-------------------------------+  |
|  | Ambient UI / Overlay Host         |         | Subagent & Worker Task Pool   |  |
|  | (aice_ui.exe)                     |         | (aice_worker.exe, Python, CLI)|  |
|  | - Lightweight Win32 / DirectWrite |         | - Isolated Workspaces / Trees |  |
|  | - System Tray & Floating HUD      |         | - Inherited Capability Handles|  |
|  +-----------------------------------+         +-------------------------------+  |
|                                                                  |                |
|                                                                  v (Job Object)   |
|                                                +-------------------------------+  |
|                                                | MCP Tool Server Pool          |  |
|                                                | (stdio / JSON-RPC Subprocesses|  |
|                                                | - Rea, Blender, Custom MCPs)  |  |
|                                                +-------------------------------+  |
+===================================================================================+
```

### 1.1 Session 0: The Machine Service (`aice_svc.exe`)
- **Lifecycle:** Installed as an automatic Windows Service (`SERVICE_AUTO_START`). Starts during the Windows kernel boot sequence before any user logs in.
- **Privilege Context:** Runs under `NT AUTHORITY\LocalService` with minimal privileges (avoiding `SYSTEM` to uphold the principle of least privilege).
- **Core Responsibilities:**
  1. Boot-time machine health validation.
  2. Supervision of the primary persistence databases (`environment.db`, `memory.db`).
  3. Machine-wide telemetry monitoring (Global CPU, Total RAM allocation, Battery/Power status, GPU driver readiness via DXGI/NVML).
  4. Listening on the system-wide Named Pipe server `\\.\pipe\aice_machine_ipc`.
  5. Monitoring user session state changes via `WTSRegisterSessionNotification` (`WTS_SESSION_LOGON`, `WTS_SESSION_LOGOFF`, `WTS_SESSION_LOCK`, `WTS_SESSION_UNLOCK`).

### 1.2 Session 1+: The User Session Agent (`aice_host.exe`)
- **Lifecycle:** Bootstrapped automatically upon user logon via a high-priority scheduled task triggered by `AtLogOn` of the interactive user.
- **Privilege Context:** Executes in the medium-integrity user context (`SECURITY_MANDATORY_MEDIUM_RID`). Runs as the logged-in user, granting access to:
  - User desktop composition, UIA tree, and GDI/D3D displays.
  - User's DPAPI cryptographic keys (for encrypting auth tokens, credentials, and conversation journals).
  - User's roaming profile, local AppData, and user-owned code workspaces.
- **Core Responsibilities:**
  1. The central operational core: hosts the Event Reactor, the Intelligence Gateway, the Capability Broker, and the Agent Orchestrator.
  2. Spawns and supervises UI overlays and subagent tasks within assigned Windows Job Objects.
  3. Connects as an IPC client to `\\.\pipe\aice_machine_ipc` to sync machine telemetry and coordinate global checkpoints.

### 1.3 The Ambient Overlay & Tray Host (`aice_ui.exe`)
- **Architecture:** Standalone low-overhead native Win32 process rendering via DirectWrite / Direct2D. Avoids heavy Electron/Chromium runtime overhead for the primary shell.
- **Capabilities:**
  - Invisible background listener for global keyboard shortcuts (`RegisterHotKey`).
  - System tray icon with ambient status indicator (Idle, Reasoning, Executing, Throttled).
  - Floating non-stealing HUD overlay (`WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_LAYERED`) to present L1 recommendations and progress cards without stealing keyboard focus from the active user application.

---

## 2. PROCESS SUPERVISION & JOB OBJECTS (ZERO-ORPHAN RUNTIME)

A primary flaw in desktop agent runtimes is process leakage: worker subprocesses, CLI tools, and MCP servers remaining alive when the main agent crashes or is killed.

AICE enforces a **mathematically deterministic zero-orphan guarantee** using Windows Win32 Job Objects.

```
+-----------------------------------------------------------------------------+
|                          AICE Root Job Object                               |
|   Configuration:                                                            |
|     - JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 1                                |
|     - JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0                              |
|     - JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0                                     |
|     - Security: InheritHandle = FALSE                                       |
+-----------------------------------------------------------------------------+
         |                                                 |
         v                                                 v
  +-----------------------+                         +----------------------+
  | Subagent Worker Pool  |                         | MCP Server Pool      |
  | (Child Process)       |                         | (Child Process)      |
  | PID: 14208            |                         | PID: 18452           |
  +-----------------------+                         +----------------------+
```

### 2.1 Job Object Configuration Specification
Every process tree spawned by `aice_host.exe` (subagents, shell runners, MCP servers) is assigned to a dedicated Windows Job Object before execution begins:

```c
// Native Windows C++ Job Object Initializer
HANDLE hJob = CreateJobObjectW(NULL, NULL);

JOBOBJECT_EXTENDED_LIMIT_INFORMATION jeli = { 0 };
// 1. Mandatory termination of entire process tree if parent handle closes
jeli.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

// 2. Prohibit silent or explicit process breakaway
// Processes spawned by children cannot escape the job container
SetInformationJobObject(
    hJob, 
    JobObjectExtendedLimitInformation, 
    &jeli, 
    sizeof(jeli)
);

// 3. Spawn child suspended, assign to job, then resume
STARTUPINFOEXW siex = { 0 };
// Initialize attributes...
CreateProcessW(
    ..., 
    CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_PROCESS_GROUP, 
    ..., 
    &pi
);

AssignProcessToJobObject(hJob, pi.hProcess);
ResumeThread(pi.hThread);
```

### 2.2 Invariants Enforced
1. **Unconditional Cleanup:** If `aice_host.exe` crashes, is terminated via Task Manager, or experiences an unhandled SEH exception, Windows kernel immediately closes `hJob`, triggering hardware-level termination of every child, grandchild, and spawned process in the tree.
2. **Breakaway Prevention:** `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK` is explicitly disallowed. Subprocesses cannot invoke `CREATE_BREAKAWAY_FROM_JOB` to detach from the supervision tree.
3. **Resource Quotas:** Jobs can be bounded with `JOBOBJECT_CPU_RATE_CONTROL_INFORMATION` to restrict runaway worker threads from monopolizing CPU cores during heavy compilation or inference workloads.

---

## 3. INTER-PROCESS COMMUNICATION (IPC) & SECURITY

### 3.1 Windows Named Pipes with Hardened DACLs
IPC between the Session 0 Machine Service and the Session 1 User Session Agent, and between the Session Agent and external helper tools, operates over Windows Named Pipes with custom Security Descriptors.

- **Pipe Naming Convention:** `\\.\pipe\aice_ipc_{scope}_{session_id}`
- **Security Descriptor Configuration:**
  - Standard pipes with default null DACLs are vulnerable to unauthorized local connection or hijack.
  - AICE constructs an explicit Discretionary Access Control List (DACL) that permits access **only** to:
    1. The specific logged-on User SID (`WtsQueryUserToken`).
    2. `NT AUTHORITY\LocalService` / `SYSTEM`.
  - All other local SIDs (including guest, other logged-on users, or low-integrity sandboxes) are explicitly denied (`ACCESS_DENIED`).

```c
// Security Descriptor Setup (SDDL Format)
// D: (DACL)
// P  (Protected - no inheritance)
// (A;;GA;;;SY)  - Allow Generic All to SYSTEM
// (A;;GA;;;LS)  - Allow Generic All to LocalService
// (A;;GA;;;<USER_SID>) - Allow Generic All strictly to logged-in user SID
```

### 3.2 Wire Protocol (Framing & Serialization)
All IPC pipes communicate using a binary length-prefixed framing protocol to eliminate framing ambiguities and buffer injection risks:

```
+-----------------------------+-----------------------------+-------------------------------+
|   Message Length (4 Bytes)  |   Message Type (2 Bytes)    |   Payload Data (N Bytes)      |
|   uint32_t (Little-Endian)  |   uint16_t Enum             |   Protobuf / Encrypted Blob   |
+-----------------------------+-----------------------------+-------------------------------+
```

- **Maximum Frame Limit:** 16 MB. Any frame declaring a length greater than `0x01000000` is immediately dropped and causes connection termination to prevent memory exhaustion attacks.
- **Serialization:** Google Protocol Buffers v3 (`.proto`) for all internal IPC messages, schemas, telemetry streams, and capability requests.

---

## 4. PERCEPTION & SENSOR PIPELINE

AICE maintains ambient awareness through a non-invasive, event-driven sensory substrate.

```
+-----------------------------------------------------------------------------+
|                          PERCEPTUAL SUBSYSTEM                               |
|                                                                             |
|  +---------------------+  +----------------------+  +--------------------+  |
|  | Window Focus Hook   |  | UI Automation (UIA)  |  | Windows Graphics   |  |
|  | SetWinEventHook     |  | IUIAutomation        |  | Capture (WGC)      |  |
|  | (EVENT_SYSTEM_      |  | Tree Cache Request   |  | Direct3D11 / WGC   |  |
|  |  FOREGROUND)        |  | (Focused Element)    |  | Zero-Copy Buffer   |  |
|  +---------------------+  +----------------------+  +--------------------+  |
|             |                         |                         |           |
|             +-------------------------+-------------------------+           |
|                                       v                                     |
|                      +--------------------------------+                     |
|                      | Perceptual Filter & Throttler  |                     |
|                      | - Change Differencing          |                     |
|                      | - Workload Governor Check      |                     |
|                      | - Secret / Password Redaction  |                     |
|                      +--------------------------------+                     |
|                                       |                                     |
|                                       v                                     |
|                      +--------------------------------+                     |
|                      | Ambient Context State Store    |                     |
|                      | (In-Memory Current Window AST) |                     |
|                      +--------------------------------+                     |
+-----------------------------------------------------------------------------+
```

### 4.1 Window Focus & Activity Tracking
- **API:** `SetWinEventHook` monitoring `EVENT_SYSTEM_FOREGROUND` and `EVENT_OBJECT_NAMECHANGE`.
- **Latency:** Native OS callback delivers active window transition events within < 5ms.
- **Captured Metadata:** Window Handle (`HWND`), Executable Path (`GetWindowThreadProcessId` + `QueryFullProcessImageNameW`), Process Integrity Level, Window Class, and Title.

### 4.2 Accessibility & UI Automation (UIA)
- **API:** COM `IUIAutomation5` with optimized `IUIAutomationCacheRequest`.
- **Strategy:** Rather than walking the full OS desktop visual tree (which incurs 200ms–800ms COM roundtrip latency and high CPU usage), AICE deploys a **Scoped Subtree Cache Request**:
  - Anchors exclusively to the current active `HWND`.
  - Limits traversal depth to 4 levels.
  - Pre-fetches: `UIA_NamePropertyId`, `UIA_ControlTypePropertyId`, `UIA_IsEnabledPropertyId`, `UIA_BoundingRectanglePropertyId`, and `UIA_ValueValuePropertyId`.
- **Security Check:** If a window or control matches `UIA_IsPasswordPropertyId = TRUE`, its value and textual content are masked at the API capture layer.

### 4.3 High-Speed Screen Capture (WGC)
- **API:** Windows.Graphics.Capture (`WGC`) with Direct3D11 surface mapping.
- **Zero-Copy Architecture:** Interops directly with the GPU compositor (DWM) without legacy GDI `BitBlt` pipeline stalls.
- **Border Disabling:** Uses `GraphicsCaptureSession.IsBorderRequired = false` (Windows 11 build 22000+) to eliminate visual recording artifacts.
- **Trigger Policy:** Screen capture is **never continuous**. It triggers only upon explicit user request, or during targeted multi-modal visual verification steps. Frame differencing checks ensure static screens consume zero capture processing.

---

## 5. CAPABILITY BROKER & DETERMINISTIC AUTHORITY MATRIX

The Capability Broker is the non-bypassable security kernel of AICE. Intelligence models (LLMs) never hold execution handles.

```
+--------------------+
| Probabilistic LLM  |
| (Untrusted Engine) |
+--------------------+
          |
          | Declares Intent: { Tool: "execute_shell", Cmd: "npm test" }
          v
+=============================================================================+
|                       DETERMINISTIC CAPABILITY BROKER                       |
|                                                                             |
|  1. Schema & Parameter Validation                                           |
|     - Verify command strings, absolute paths, buffer limits                 |
|                                                                             |
|  2. Authority Matrix Evaluation                                             |
|     - Check Current Privilege Context & Target Resource                     |
|     - Classify: L0 (Observe) / L1 (Recommend) / L2 (Execute) / L3 (Protect) |
|                                                                             |
|  3. Policy Rule Enforcement                                                 |
|     - Path Traversal Check (Canonicalized GetFullPathNameW)                 |
|     - Secret Scanner (Token / Key / Password Regex Gate)                    |
|                                                                             |
|  4. User Consent Gate (if Level >= L3 or Unambiguous Rule Match)             |
|     - Dispatches Cryptographic / Interactive UI Confirmation Prompt         |
|                                                                             |
|  5. Transactional Audit Log                                                 |
|     - Commit Step to SQLite WAL Journal (conversations.db) BEFORE Execution |
+=============================================================================+
          |
          | Dispatches Authorized Execution Token
          v
+--------------------+
| Hardened Execution |
| Engine / OS APIs   |
+--------------------+
```

### 5.1 Authority Classification Matrix

```
       Risk Level
           ^
       L3  |  [PROTECTED EXECUTION] - Mandatory Interactive Confirmation
           |  - Delete files / Format disk
           |  - Terminate external processes / Modify Registry
           |  - Network exfiltration / Install software packages
           |
       L2  |  [REVERSIBLE EXECUTION] - Autonomous within Workspace Policy
           |  - Write / Edit files in approved project root
           |  - Execute build commands / Run local test suites
           |  - Switch virtual desktops / Focus application
           |
       L1  |  [RECOMMENDATION] - Autonomous Generation, Human Triggered
           |  - Synthesize plan / Propose architectural refactor
           |  - Suggest terminal command / Generate diff preview
           |
       L0  |  [PASSIVE OBSERVATION] - Fully Autonomous
           |  - Read window titles / Query CPU/GPU telemetry
           |  - Read-only file inspection / Query local SQLite cache
           +------------------------------------------------------------> Autonomy
```

### 5.2 Deterministic Path & Command Validation Rules
1. **Canonicalization:** Every filesystem path passed to a tool must be resolved to its Win32 canonical path via `GetFullPathNameW` and checked against junction points (`GetFileAttributesW` & `FILE_ATTRIBUTE_REPARSE_POINT`) to prevent path traversal outside designated workspace trees.
2. **Execution Gate:** Shell commands are parsed into token streams. Arbitrary shell chaining operators (`&`, `&&`, `;`, `|`, `powershell -enc`) targeting system directories (`C:\Windows\System32`, registry keys, or credential directories) automatically escalate any tool invocation from L2 to L3 (Mandatory User Consent).

---

## 6. PERSISTENCE ENGINE & DATA REPLICATION

AICE implements an append-only, crash-safe persistence layer powered by SQLite 3 with Write-Ahead Logging (WAL) and local full-text indexing.

```
+-----------------------------------------------------------------------------+
|                          PERSISTENCE TOPOLOGY                               |
|                                                                             |
|  %LOCALAPPDATA%\aice\                                                       |
|  +-- environment.db         <- Machine state, configuration, devices        |
|  +-- memory.db              <- Consolidated semantic facts & entity graph   |
|  +-- fts_index.db           <- Global SQLite FTS5 search index              |
|  +-- sessions/                                                              |
|      +-- {session_uuid}/                                                    |
|          +-- session.db     <- Canonical Protobuf step replay journal       |
|          +-- stream.jsonl   <- Streaming append-only UI presentation log    |
|          +-- steps/                                                         |
|              +-- {step_id}/                                                 |
|                  +-- output.txt  <- Offloaded large tool outputs (>46 KB)   |
|                  +-- diff.patch  <- Structured git/workspace patches        |
+-----------------------------------------------------------------------------+
```

### 6.1 Database Configuration Invariants
Every SQLite database in AICE is initialized with strict transactional pragmas:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;
PRAGMA mmap_size = 268435456; -- 256 MB Memory-Mapped I/O for high read speeds
PRAGMA busy_timeout = 5000;    -- 5s busy handler before SQLITE_BUSY
```

### 6.2 Relational Schema Specifications

#### Schema: `environment.db` (Global Machine State)
```sql
CREATE TABLE IF NOT EXISTS system_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc INTEGER NOT NULL,
    cpu_utilization_pct REAL NOT NULL,
    ram_available_mb INTEGER NOT NULL,
    gpu_utilization_pct REAL,
    battery_level_pct INTEGER,
    power_line_status TEXT CHECK(power_line_status IN ('AC', 'BATTERY', 'UNKNOWN'))
);

CREATE TABLE IF NOT EXISTS workspace_registrations (
    workspace_id TEXT PRIMARY KEY,
    canonical_path TEXT UNIQUE NOT NULL,
    friendly_name TEXT NOT NULL,
    registered_at_utc INTEGER NOT NULL,
    git_branch TEXT,
    is_active INTEGER DEFAULT 0
);
```

#### Schema: `session.db` (Per-Session Canonical Step Journal)
```sql
CREATE TABLE IF NOT EXISTS conversation_metadata (
    conversation_id TEXT PRIMARY KEY,
    parent_conversation_id TEXT,
    created_at_utc INTEGER NOT NULL,
    model_identifier TEXT NOT NULL,
    nesting_depth INTEGER DEFAULT 0,
    status TEXT CHECK(status IN ('ACTIVE', 'IDLE', 'COMPLETED', 'FAILED', 'KILLED'))
);

CREATE TABLE IF NOT EXISTS execution_steps (
    step_index INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    step_type TEXT CHECK(step_type IN ('USER_INPUT', 'PLANNER_RESPONSE', 'TOOL_CALL', 'TOOL_RESULT', 'SYSTEM_NOTIFICATION')),
    created_at_utc INTEGER NOT NULL,
    protobuf_payload BLOB NOT NULL,     -- Complete serialized Step Protobuf
    has_offloaded_output INTEGER DEFAULT 0,
    offload_file_path TEXT,
    FOREIGN KEY(conversation_id) REFERENCES conversation_metadata(conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_step_lookup ON execution_steps(conversation_id, step_index);
```

#### Schema: `fts_index.db` (Global Fast Search Engine)
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS global_knowledge_fts USING fts5(
    document_uri,
    document_type,  -- 'CONVERSATION', 'WORKSPACE_FILE', 'MEMORY_ENTITY'
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
```

### 6.3 Dual-Journal Verification & Output Offloading
- **Single Source of Truth:** `session.db` (`execution_steps` table containing raw Protobuf payloads) is the sole transactional source of truth.
- **Streaming Mirror:** `stream.jsonl` is written concurrently as an asynchronous, non-blocking append stream utilized by frontend UI consumers for smooth progressive rendering.
- **Threshold Offloading:** Any tool output, diff, or terminal stdout exceeding **46,080 bytes** (45 KB) is stripped from the primary database row, persisted to an atomic file `steps/{step_id}/output.txt`, and referenced via `offload_file_path`. This prevents SQLite B-tree bloat and preserves memory mapped read performance.

---

## 7. CRASH RECOVERY & THE 7-STATE TASK LIFECYCLE

AICE implements an explicit, deterministic recovery state machine. Tasks never hang in indeterminate states upon crash or restart.

```
       +--------------+
       |   CREATED    |
       +--------------+
              | (Schedule / Dispatch)
              v
       +--------------+          Process Crash / System Reboot
       |   RUNNING    | ---------------------------------------------+
       +--------------+                                              |
              |                                                      v
      +-------+-------+                                     +------------------+
      | (Success)     | (Error)                             |   INTERRUPTED    |
      v               v                                     +------------------+
+-----------+   +-----------+                                        |
| COMPLETED |   |  FAILED   |                                        | System Recovery
+-----------+   +-----------+                                        v
                                                            +------------------+
                                                            |    RECOVERY      |
                                                            +------------------+
                                                                     |
                                             +-----------------------+-----------------------+
                                             | (Task Idempotent?)                            | (Non-Idempotent / External)
                                             v                                               v
                                    +------------------+                           +------------------+
                                    |     RESUMED      |                           |     RESTARTED    |
                                    | (Resume Step)    |                           | (Abort & Prompt) |
                                    +------------------+                           +------------------+
```

### 7.1 The Seven States
1. **CREATED:** Intent defined, task parameters validated, capability token minted, but no OS process or worker spawned.
2. **RUNNING:** Process actively executing within assigned Job Object; heartbeat being recorded to `session.db`.
3. **COMPLETED:** Process exited with code 0; outputs validated, offloaded if necessary, and step transaction committed.
4. **FAILED:** Process exited with non-zero code or internal error; failure diagnostics recorded.
5. **INTERRUPTED:** Detected on startup if a task in `session.db` holds state `RUNNING`, but its recorded Windows Process ID (PID) no longer exists in the OS process table or belongs to a different process creation time (`GetProcessTimes`).
6. **RECOVERY:** Recovery engine inspects task metadata:
   - Evaluates whether task was marked `IDEMPOTENT` (e.g., read-only analysis, compilation check, test suite).
   - Verifies whether output files exist partially on disk.
7. **RESUMED / RESTARTED:**
   - *Resumed:* If task was idempotent, execution restarts from the last uncommitted step index.
   - *Restarted / Aborted:* If task was non-reversible (e.g. file rename, network dispatch), the state machine halts, flags the interrupted state, and requests user decision via L1/L3 UI prompt.

---

## 8. WORKLOAD THROTTLING & RESOURCE GOVERNANCE

A personal computing environment must never degrade user gaming, 3D rendering, video editing, or compilation performance.

```
+-----------------------------------------------------------------------------+
|                          RESOURCE GOVERNOR                                  |
|                                                                             |
|  Sensors:                                                                   |
|  - SHQueryUserNotificationState (DirectX Fullscreen, DND, Presentation)     |
|  - DXGI Frame Rate & VRAM Query (NVIDIA NVML / DXGI Adapter 0)              |
|  - GetSystemPowerStatus (Battery vs. AC, Battery Saver Active)              |
|                                                                             |
|  State Evaluations:                                                         |
|  +-----------------------------------------------------------------------+  |
|  | STATE: GAMING / FULLSCREEN 3D                                         |  |
|  | Actions: Suspend all background agents. Stop WGC capture.             |  |
|  |          Release local GPU VRAM weights. Drop CPU priority to IDLE.   |  |
|  +-----------------------------------------------------------------------+  |
|  | STATE: HIGH SYSTEM LOAD (Compiling, Video Encoding > 85% CPU)         |  |
|  | Actions: Throttle perception loop from 100ms to 5000ms.               |  |
|  |          Pause background FTS index rebuilding.                       |  |
|  +-----------------------------------------------------------------------+  |
|  | STATE: BATTERY SAVER ACTIVE (< 20% Battery)                           |  |
|  | Actions: Disable local NPU/GPU model execution. Yield all idle jobs.  |  |
|  +-----------------------------------------------------------------------+  |
+-----------------------------------------------------------------------------+
```

### 8.1 DirectX & Full-Screen Detection
- **Primary Mechanism:** `SHQueryUserNotificationState(&pquns)`.
- Returns `QUNS_RUNNING_D3D_FULL_SCREEN` when a DirectX or Vulkan game is executing full-screen.
- **Secondary Mechanism:** DXGI swap chain polling on the primary monitor.
- **Governor Reaction:** When `QUNS_RUNNING_D3D_FULL_SCREEN` or `QUNS_PRESENTATION_MODE` is active:
  1. All screen capture and UIA polling timers are immediately paused (`Sleep(INFINITE)` on wait handle).
  2. Background inference engines unload active VRAM model weights.
  3. Worker Job Objects are throttled to 5% CPU cap via `JOBOBJECT_CPU_RATE_CONTROL_INFORMATION`.

---

## 9. MODEL & INFERENCE ABSTRACTION LAYER

AICE decouples reasoning intelligence from system execution through a unified dual-tier inference architecture.

```
                               +----------------------------+
                               |     Agent Orchestrator     |
                               +----------------------------+
                                             |
                                             v
                               +----------------------------+
                               |  Inference Routing Engine  |
                               +----------------------------+
                                             |
                     +-----------------------+-----------------------+
                     |                                               |
                     v (Local Sensitive / Offline)                   v (High-Complexity Planning)
       +----------------------------+                  +----------------------------+
       |   Local Hardware Engine    |                  |  External Sovereign Gateway|
       |  - DirectML / ONNX Runtime |                  |  - Gemini 1.5/2.0 API      |
       |  - AMD Ryzen NPU (NPU V1)  |                  |  - Claude 3.5 Sonnet       |
       |  - Local RTX 5050 TensorRT |                  |  - Local Ollama Endpoint   |
       +----------------------------+                  +----------------------------+
```

### 9.1 Local Hardware Acceleration (On-Device Sovereignty)
- **DirectML & ONNX Runtime:** Leverages Windows DirectML for vendor-agnostic NPU and discrete GPU execution.
- **Hardware Targets on Host Machine:**
  - **AMD Ryzen AI NPU:** Offloads ambient sensory classification, PII scrubbing, and fast token embeddings at < 2W power draw.
  - **NVIDIA GeForce RTX 5050 (8GB VRAM):** Hosts fast quantized local planners (e.g. Qwen2.5-Coder-7B-Instruct 4-bit, DeepSeek-R1-Distill-8B) for zero-latency offline code edits and local planning.

### 9.2 External Sovereign Gateway (Cloud Reasoning)
- **PII / Secret Scrubbing Gate:** Before any prompt is emitted to an external API (Gemini, Anthropic, OpenAI), it passes through a deterministic regex and local NPU redaction pipeline. File paths containing usernames, API keys, passwords, and sensitive system tokens are replaced with synthetic cryptographic placeholders.
- **Provider Agnosticism:** Communication uses standard HTTPS POST endpoints returning structured JSON Schema function calling objects. Model providers can be swapped in real-time without affecting any underlying system tool or broker logic.
