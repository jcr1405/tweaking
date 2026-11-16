# SYSTEM CONSTITUTION
## Ambient Intelligent Computing Environment (AICE)
**Document Version:** 1.0.0-PROD-SPEC  
**Classification:** Foundational Architecture Standard  
**Operating Environment:** Windows 11 Native Runtime  

---

## 1. PURPOSE & VISION

### 1.1 The Defining Principle
The Personal Computing Environment (referred to herein as **the Environment** or **AICE**) is not an application, not a chatbot, not a desktop assistant, and not an automation script runner. It is an **independent, persistent, intelligent operational layer** operating directly on top of Microsoft Windows.

In traditional desktop computing, the operating system manages physical hardware, process lifecycles, memory isolation, and device drivers, while the human user is forced to serve as the manual orchestrator across fragmented application silos. 

The Environment fundamentally redefines this relationship:
- **Windows** manages the machine: kernel primitives, hardware drivers, memory protection, desktop composition, and security subsystem (LSA/SAM).
- **The Environment** manages the computing experience: persistent intent, cross-application operational state, contextual awareness, autonomous orchestration, proactive background assistance, and continuous machine memory.

### 1.2 Ambient Operational Continuity
The Environment does not wait to be launched by the user. It is alive the moment the machine boots into Windows and remains operational across user logons, lock states, desktop switches, and power events. It observes context passively, reasons continuously with zero UI intrusion, maintains unbroken situational awareness, and presents itself seamlessly when needed.

---

## 2. CORE PHILOSOPHY & INVARIANTS

The design, implementation, and evolution of the Environment are governed by five inviolable architectural pillars:

### 2.1 Presence (Always Alive)
- The core runtime executes as a supervised, fault-tolerant Windows Service in Session 0, bootstrapping during Windows startup prior to interactive user logon.
- Interactive components attach dynamically to user sessions (Session 1+) via secured IPC boundaries upon logon, surviving Explorer restarts, screen locks, and fast user switching.
- Absence of user interaction does not imply idle death; background reasoning, memory consolidation, health indexing, and deterministic event surveillance continue unabated.

### 2.2 Continuity (Unbroken Temporal State)
- The state of the machine, ongoing user workflows, multi-step agent plans, and ambient contexts are persistent across reboots, unexpected power loss, system crashes, and runtime upgrades.
- Every state mutation is durably committed using a crash-safe, write-ahead logged transactional architecture. Memory is never volatile-only; process death causes zero state corruption or unrecoverable amnesia.

### 2.3 Awareness (Multi-Modal Contextual Synthesis)
- The Environment maintains real-time perception of system state without causing user-perceptible resource degradation:
  - Active focused application, window hierarchies, and UI Automation (UIA) trees.
  - Filesystem mutations across monitored workspaces and user document trees.
  - Network connectivity, operational topology, and power states (AC vs. Battery).
  - Ambient hardware telemetry: CPU core scheduling, GPU load, VRAM allocation, and thermal headroom.
- Perception is adaptive: sampling frequency dynamically throttles down during high-load scenarios (e.g., full-screen 3D gaming, video rendering, compiling) and scales up during active human collaboration.

### 2.4 Agency (Bounded Authority)
- Intelligence without agency is a passive search engine; agency without boundaries is catastrophic.
- The Environment possesses native execution capabilities (filesystem I/O, process orchestration, API invocation, UI interaction) governed by an absolute, non-bypassable Capability Broker.
- Large Language Models (LLMs) and probabilistic inference engines are strictly **untrusted planners**. They possess zero ambient execution authority. Every action proposed by an intelligence engine must be validated, checked against hard constitutional policies, and dispatched through deterministic capability gates.

### 2.5 Ownership (Absolute Local Sovereignty)
- The user is the absolute sovereign owner of their machine, data, and compute environment.
- The core operational database, logs, state trees, security vaults, and event indices reside exclusively on local non-volatile storage under user-controlled ACLs.
- Telemetry, logs, and sensitive workflow traces are never exfiltrated to external vendors. Cloud inference providers, if utilized, receive strictly scrubbed, localized context envelopes on an opt-in basis, while local NPU/GPU inference retains complete functional sovereignty.

---

## 3. THE FIVE BOUNDARIES

To ensure security, deterministic execution, and architectural stability, the Environment enforces five strict isolation boundaries:

```
+-------------------------------------------------------------------------+
|                          1. SYSTEM BOUNDARY                             |
|  Windows Kernel / Windows Services / User Space Isolation (Session 0/1) |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                           2. DATA BOUNDARY                              |
|  Encrypted Local DBs / Per-Workspace Partitions / Ephemeral Token Gates |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                       3. APPLICATION BOUNDARY                           |
|  Passive OS Hooks / UIA Tree Navigation / Non-Destructive App Shims     |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                        4. AUTHORITY BOUNDARY                            |
|       Level 0 (Observe) -> Level 1 (Suggest) -> Level 2 (Execute)       |
|                  Level 3 (Protected / System State)                     |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                       5. INTELLIGENCE BOUNDARY                          |
|  Untrusted Probabilistic Reasoning  <-->  Deterministic Capability Core|
+-------------------------------------------------------------------------+
```

### 3.1 System Boundary (OS & Process Isolation)
- **Session 0 Separation:** The Machine Substrate runs as a native Windows Service in Session 0 (`NT AUTHORITY\SYSTEM` or a dedicated low-privilege service account). It has zero desktop interaction capabilities (mitigating Shatter attacks and Windows Session 0 isolation constraints).
- **Session 1+ Interaction:** The User Session Agent runs as the logged-on user in Session 1+, hosting the desktop automation subsystem, system tray/ambient UI overlays, and local audio/screen capture.
- **IPC Isolation:** Communication across the Session 0 / Session 1 boundary occurs strictly over Windows Named Pipes protected by explicit Security Descriptors (DACLs) verifying caller PID, SID, and integrity level.
- **Process Supervision:** All child processes, worker tasks, and MCP tool servers are assigned to Windows Job Objects configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and explicit breakaway restrictions.

### 3.2 Data Boundary (Zero Exposure & Storage Isolation)
- **Local Persistence Vault:** State databases (`environment.db`, `memory.db`) are encrypted at rest using Windows Data Protection API (DPAPI) tied to the user's logon security identifier.
- **Context Partitioning:** Subagents and parallel worker tasks execute in isolated workspaces (independent directories or Git worktrees). They have zero access to the host agent's internal journal or scratch buffers outside explicit capability handles.
- **Redaction Pipeline:** Screen buffers, clipboard data, and shell outputs pass through a deterministic PII/Secret scrubbing pipeline (detecting API keys, passwords, bearer tokens, personal identifiers) before entering the persistence engine or inference context.

### 3.3 Application Boundary (Non-Destructive Coexistence)
- **Zero Kernel Driver Invasions:** The Environment does not deploy undocumented kernel hooks, filter drivers, or rootkit-style memory patching.
- **Standardized OS Interfaces:** Desktop perception operates via legitimate, supported Windows APIs:
  - Windows UI Automation (UIA) COM interfaces for accessibility and element trees.
  - Windows Graphics Capture (WGC) with borderless zero-copy D3D11 textures.
  - Windows Event Log (`wevtapi.dll`) and ReadDirectoryChangesW for system events.
- **Target Application Neutrality:** The Environment treats third-party applications as black boxes. It never modifies application binaries on disk or injects unmanaged DLLs into external address spaces unless explicitly configured as an authored developer plugin.

### 3.4 Authority Boundary (The 4-Tier Security Matrix)
Every possible action within the Environment is classified into one of four rigid authority levels:

| Level | Name | Scope / Permissions | Confirmation Policy | Failure Fallback |
| :--- | :--- | :--- | :--- | :--- |
| **L0** | **Observe** | Read-only telemetry: CPU/RAM metrics, active window titles, UIA tree inspection, read-only file parsing, local DB querying. | Autonomous (Zero prompt) | Silent retry or backoff |
| **L1** | **Recommend** | Synthesizing insights, drafting commit messages, preparing scripts, proposing task plans, displaying toast notifications. | Autonomous presentation; requires user action to trigger. | Dismiss notification |
| **L2** | **Execute (Reversible)** | Creating scratch files, running sandboxed compiler checks, switching virtual desktops, writing to designated project directories. | Policy-based (Automatic for trusted projects, confirm if ambiguous). | Rollback transaction, report error |
| **L3** | **Protected (Irreversible)**| Deleting files, modifying registry, killing external system processes, installing packages, sending network payloads, modifying credentials. | Mandatory User Consent (Cryptographic or interactive confirmation). | Abort action immediately |

### 3.5 Intelligence Boundary (Untrusted Reasoning)
- Probabilistic models (LLMs, neural networks, embeddings) are treated as **untrusted calculation engines**.
- An LLM cannot execute shell commands, allocate files, open sockets, or mutate persistent memory directly.
- The model outputs structured declarative intents (Protobuf or validated JSON Schema).
- The **Deterministic Capability Broker** inspects the intent, validates parameter schemas against rigid types, verifies the security policy against the Authority Matrix, logs the operation in the transactional journal, and only then executes through a hardened native runtime.

---

## 4. AUTONOMY & REASONING MODEL

The Environment operates across three complementary operational loops:

```
+-------------------------------------------------------------------------+
|                        1. REACTIVE LOOP (< 100ms)                       |
|   Direct User Command -> Planner -> Broker -> Execution -> Feedback    |
+-------------------------------------------------------------------------+

+-------------------------------------------------------------------------+
|                        2. PROACTIVE LOOP (1s - 60s)                     |
|  Event Detection -> Context Correlation -> Plan Synthesis -> L1 Suggest |
+-------------------------------------------------------------------------+

+-------------------------------------------------------------------------+
|                        3. AMBIENT LOOP (Scheduled / Idle)               |
|  Background State Consolidation -> FTS Indexing -> Memory Pruning       |
+-------------------------------------------------------------------------+
```

### 4.1 Reactive Mode (Explicit User Intent)
- Triggered by explicit user instructions (keyboard shortcut, audio trigger, chat input, CLI pipe).
- Real-time execution with immediate feedback and progressive step execution.
- Context window is assembled dynamically from active session state, current workspace AST/FTS index, and the immediate window environment.

### 4.2 Proactive Mode (Contextual Awareness)
- Triggered by high-confidence environmental state changes (e.g., git branch switch with unresolved merge conflicts, build failure detected in active terminal, repeated error dialog from an application).
- Operates strictly at **Level 1 (Recommend)**. It generates non-intrusive ambient cards or suggestions. It never takes autonomous Level 2 or Level 3 actions without explicit user ratification.

### 4.3 Ambient Mode (Autonomous Background Engine)
- Executes during user idle periods or machine sleep-wake cycles.
- Responsible for:
  - Consolidating raw event logs into semantic memory trees.
  - Compacting and vaccuuming SQLite WAL databases.
  - Rebuilding local FTS5 search indices across updated workspace files.
  - Pruning stale temporary artifacts, worktrees, and orphaned process handles.
- Zero impact on user responsiveness: instantly yields CPU, GPU, and disk I/O when user input (mouse, keyboard, controller) is detected.

---

## 5. NON-GOALS & EXCLUSIONS

To ensure laser focus and maintain engineering integrity, the following objectives are explicitly classified as **Out of Scope (Non-Goals)**:

1. **Not a Windows Explorer Replacement:** The Environment does not replace the Windows desktop shell (`explorer.exe`), taskbar, or system tray. It integrates harmoniously with native Windows shell features.
2. **Not a Kernel-Mode Modification:** The Environment does not install kernel-mode drivers (`.sys`), modify the Windows kernel dispatch table, or alter system secure boot policies.
3. **Not a Cloud-Dependent Web Application:** The Environment is not an Electron web-app wrapper requiring an active internet connection to function. Core runtime, local persistence, state evaluation, and system control operate 100% offline.
4. **Not a Universal Autonomic Bot:** The Environment does not attempt to randomly click screen pixels based solely on computer vision heuristics when robust, deterministic APIs (UIA, Win32, COM, CLI) are available. Visual pixel clicking is an absolute last-resort fallback.
5. **Not a General-Purpose Chat Toy:** The Environment is not built to simulate human personality, small talk, or generative creative writing at the expense of computational performance and deterministic reliability.

---

## 6. AMENDMENT PROCEDURE

This System Constitution constitutes the supreme architectural specification for the Environment. Any proposed architectural change that alters:
1. The 5 Security Boundaries,
2. The Session 0/1 Process Topology,
3. The Capability Broker Authority Matrix, or
4. The Local Data Sovereignty Standard,

must undergo a formal Architectural Review, be validated by a forensic verification report, and receive explicit ratification prior to implementation.
