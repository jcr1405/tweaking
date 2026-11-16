# ARCHITECTURAL DECISIONS & MILESTONE ROADMAP
## Ambient Intelligent Computing Environment (AICE)
**Document Version:** 1.0.0-PROD-SPEC  
**Classification:** Engineering Roadmap & Decision Matrix  

---

## 1. PRIORITIZED ARCHITECTURAL DECISIONS TO SETTLE

Before writing implementation code for the runtime substrate, the following four high-priority engineering decisions must be settled. Each decision has been evaluated against performance, memory footprint, security boundaries, and Windows native integration.

---

### Decision 1: Core Systems Language for the Substrate (`aice_svc` & `aice_host`)
**Priority:** Critical (Priority 1)  
**Context:** The Machine Service (Session 0) and the User Session Agent (Session 1) require native Windows API access (Win32, COM, Job Objects, Named Pipes, DirectWrite, UIA, DXGI, DPAPI).

| Candidate | Advantages | Disadvantages | Verdict |
| :--- | :--- | :--- | :--- |
| **Rust** | Zero-cost abstractions; memory safety without GC pauses; first-class `windows-rs` crate (direct Win32/COM/WinRT projections); native Job Object and Named Pipe safety; tiny binaries (~5–10MB); zero GC overhead during gaming/high-load. | Steeper compile times; COM interface lifetime ergonomics can be verbose. | **RECOMMENDED** |
| **Go** | Fast compilation; simple concurrency (`goroutines` + channels); proven by Antigravity's `language_server.exe`. | Non-negligible runtime footprint (~30-50MB RAM minimum); GC pauses; cumbersome Win32/COM CGo interop; larger binary size (~30-200MB). | Secondary |
| **C++20 / MSVC** | Ultimate native control; direct ATL/WRL COM interop; native WinRT C++/WinRT. | Memory safety risks (UAF, buffer overflows in untrusted string parsing); lacks modern cargo-style packaging. | Tooling / Shims only |

> **Recommendation:** **Adopt Rust** for the foundational runtime substrate (`aice_svc`, `aice_host`, `aice_ui`). Use the official Microsoft `windows` and `windows-sys` crates for zero-overhead Win32/COM integration.

---

### Decision 2: Named Pipe IPC Serialization & Framing Protocol
**Priority:** High (Priority 2)  
**Context:** IPC between Session 0 and Session 1, and between Session 1 and worker subagents/UI overlay. Needs microsecond dispatch latency, rigid schema typing, and zero buffer overflow vulnerabilities.

| Protocol | Latency | Schema Rigidity | Zero-Copy Capability | Verdict |
| :--- | :--- | :--- | :--- | :--- |
| **Length-Prefixed Protobuf (v3)** | < 0.1ms | Very High (Strict `.proto` definitions, backward compatible) | Moderate (Deserialization step) | **RECOMMENDED** |
| **FlatBuffers** | < 0.02ms | High | Absolute (Direct memory read) | Overkill for IPC scale |
| **JSON-RPC 2.0 (stdio / named pipe)** | 1.0 – 5.0ms | Low (Stringly typed, schema drift) | None (Heavy allocation & string parsing) | Rejected for Internal Core (Retained only for MCP tools) |

> **Recommendation:** **Adopt 4-byte length-prefixed Protocol Buffers v3** for all internal IPC and canonical state persistence. Retain JSON-RPC 2.0 strictly at the boundary of external third-party MCP servers.

---

### Decision 3: Cryptographic Storage & Credential Isolation
**Priority:** High (Priority 3)  
**Context:** Storing user API keys (Gemini, Claude), workspace credentials, and sensitive state tokens securely on Windows.

| Mechanism | Scope | Security Posture | Verdict |
| :--- | :--- | :--- | :--- |
| **Windows DPAPI (`CryptProtectData`)** | Session 1 (User Context) | Keys tied to user's Windows logon password hash. Inaccessible to other users or Session 0 services. | **RECOMMENDED** |
| **Windows Credential Manager / Vault** | User Vault | User-visible in Control Panel; well-supported via `CredWriteW` / `CredReadW`. | Viable for API Keys |
| **Hardcoded or Plain File Storage** | Any | Zero security; catastrophic risk. | **STRICTLY PROHIBITED** |

> **Recommendation:** **Adopt Windows DPAPI** for automated database token encryption at rest, and **Windows Credential Locker** (`CredWriteW`) for named user-configured API credentials.

---

### Decision 4: Workload & Fullscreen Throttling Sensor Mechanism
**Priority:** Medium (Priority 4)  
**Context:** Detecting 3D gaming, video editing, or presentation mode to immediately suppress background agents and yield GPU/CPU resources.

| Mechanism | Polling Overhead | Precision | Verdict |
| :--- | :--- | :--- | :--- |
| **`SHQueryUserNotificationState`** | < 1 microsecond | Native Windows API designed specifically to detect `QUNS_RUNNING_D3D_FULL_SCREEN` and `QUNS_PRESENTATION_MODE`. | **RECOMMENDED** |
| **DirectX SwapChain Hooking** | High (Injection required) | Invasive; triggers anti-cheat software in games. | **STRICTLY PROHIBITED** |
| **Process Name Blacklist** | Constant string matching | Fragile; fails on new games or unlisted software. | Rejected |

> **Recommendation:** **Adopt `SHQueryUserNotificationState`** polled on an adaptive 2-second timer during idle, dropping immediately to a 10-second timer when full-screen 3D is active.

---

## 2. MILESTONE ROADMAP

The development of AICE is divided into five verifiable milestones. We do not build everything at once. We build from the hardware/OS substrate upwards.

```
+-----------------------------------------------------------------------------+
| MILESTONE 1: The Ambient Runtime Substrate (Core Foundation)                |
| - Session 0 Service & Session 1 Agent Bootstrap                             |
| - Hardened Named Pipe IPC with DACL Enforcement                             |
| - Win32 Job Object Supervision (Verified Zero-Orphan Tree)                  |
| - SQLite WAL State Engine & 7-State Recovery Machine                        |
+-----------------------------------------------------------------------------+
                                       |
                                       v
+-----------------------------------------------------------------------------+
| MILESTONE 2: Sensory Perception & Resource Governor                         |
| - WinEvent Window Focus Tracker (SetWinEventHook)                           |
| - Scoped UIA Tree Caching & Password Field Masking                          |
| - SHQueryUserNotificationState Fullscreen 3D Throttling                     |
| - On-Demand Windows Graphics Capture (WGC) Direct3D Pipeline                |
+-----------------------------------------------------------------------------+
                                       |
                                       v
+-----------------------------------------------------------------------------+
| MILESTONE 3: The Deterministic Capability Broker                            |
| - Absolute L0 - L3 Authority Matrix Gatekeeper                              |
| - Win32 Canonical Path & Traversal Sandbox Verification                     |
| - Dual-Journal Synchronization (SQLite WAL Canonical + JSONL Streaming)     |
| - 46 KB Tool Output Offloading Pipeline                                     |
+-----------------------------------------------------------------------------+
                                       |
                                       v
+-----------------------------------------------------------------------------+
| MILESTONE 4: Agent Orchestration & MCP Server Subsystem                     |
| - MCP Tool Server Supervisor (Subprocess stdio JSON-RPC)                    |
| - Subagent Multi-Workspace Isolation (Git Worktree Branching)               |
| - 180-second Execution Deadline & Cascading Cancellation Engine             |
| - Global FTS5 Workspace Indexer                                             |
+-----------------------------------------------------------------------------+
                                       |
                                       v
+-----------------------------------------------------------------------------+
| MILESTONE 5: The Ambient UI HUD & Multi-Modal Intelligence Gateway          |
| - DirectWrite Non-Activating Overlay HUD (No Focus Steal)                  |
| - DirectML / Ryzen NPU Ambient Context Classifier                           |
| - Cloud Provider-Agnostic LLM Gateway with PII Scrubbing                    |
+-----------------------------------------------------------------------------+
```

---

## 3. MILESTONE 1 DETAILED SPECIFICATION: THE AMBIENT RUNTIME SUBSTRATE

### 3.1 Objective
Establish the minimal, bulletproof, crash-resilient Windows runtime foundation before introducing any perceptual hooks, AI inference, or UI elements.

### 3.2 Deliverables for Milestone 1
1. **Machine Service Binary (`aice_svc.exe`):**
   - Implements Windows Service lifecycle (`ServiceMain`, `HandlerEx`).
   - Registers for `WTSRegisterSessionNotification`.
   - Creates and hosts secure Named Pipe server `\\.\pipe\aice_machine_ipc`.
2. **User Session Agent Binary (`aice_host.exe`):**
   - Connects to Named Pipe in Session 0; verifies DACL and session binding.
   - Initializes `environment.db` and active `session.db` with SQLite WAL pragmas.
   - Creates Root Win32 Job Object with `KILL_ON_JOB_CLOSE`.
3. **Supervised Worker Subprocess (`aice_worker.exe`):**
   - Spawned by `aice_host` in suspended mode, assigned to Job Object, resumed.
   - Executes mock long-running tasks and demonstrates zero process leakage when `aice_host` is forcefully killed (`taskkill /F /IM aice_host.exe`).
4. **Crash Recovery Test Suite:**
   - Simulates abrupt process death during active task execution.
   - Tests detection of `INTERRUPTED` state and successful promotion to `RECOVERY -> RESUMED` upon relaunch.

### 3.3 Verification Criteria
- [ ] Service starts automatically on Windows boot.
- [ ] Named Pipe denies connections from unauthorized local SIDs.
- [ ] Force-killing `aice_host.exe` results in 0 orphaned worker processes (100% kernel cleanup via Job Object).
- [ ] SQLite database remains uncorrupted after 50 consecutive simulated hard kills during active writes.
- [ ] Background idle CPU consumption is measurably < 0.1% and memory footprint is < 25 MB RAM.
