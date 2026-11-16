# FOUNDATION SELECTION AND MODIFICATION PLAN
## Adapting Verified Open-Source Systems to Realize the Intelligent Presence
**Document Version:** 1.1.0-ADAPT-AUDIT  
**Classification:** Core Implementation Strategy & Modification Blueprint  
**Guiding Principle:** **Modify Before Creating**  
**Revision Date:** November 2026  

---

## 1. THE FOUNDATION EVALUATION: RE-AUDITED CANDIDATES

Following the forensic re-verification of Screenpipe's commercial licensing, Open Interpreter Desktop's Electron architecture, and the Tauri Dynamic Island ecosystem, we re-evaluate the candidate foundations:

```
+===================================================================================================================+
| CANDIDATE FOUNDATION       | PRIMARY CAPABILITY    | FORENSIC DEFECT / LIMITATION          | RE-AUDITED VERDICT   |
+============================+=======================+=======================================+======================+
| Screenpipe                 | WGC Screen Capture &  | RESTRICTIVE COMMERCIAL LICENSE        | Cannot embed in a    |
| (mediar-ai/screenpipe)     | Local SQLite Timeline | (Negentropy Labs, Inc.); RAM footprint| redistributable app. |
|                            |                       | is 500MB-1.5GB if Whisper/OCR run.    | Optional local sensor|
+----------------------------+-----------------------+---------------------------------------+----------------------+
| Open Interpreter Desktop   | Document Workflows,   | Full-sized Electron workstation window| Cannot be ambient HUD|
| (interpreter-workstation)  | OIX Goals, BYOM       | (~300MB RAM); lacks native UIA and WGC| Adopt execution and  |
|                            |                       | background desktop perception.        | permission patterns. |
+----------------------------+-----------------------+---------------------------------------+----------------------+
| NVIDIA OpenShell           | Linux Landlock Sandbox| Confined to WSL2/Linux containers; zero| Adopt as Governed    |
| (NVIDIA/OpenShell)         | & Supervisor Proxy    | Windows GUI/desktop perception.       | Heavy Agent Sandbox  |
+----------------------------+-----------------------+---------------------------------------+----------------------+
| Pillar Dynamic Island      | Windows Dynamic Island| UI widget only; lacks autonomous agent| **Adopt as Open-Source|
| (warpirate / aixiasang)    | (Tauri + Rust + React)| reasoning, memory, or browser bridge. | UI HUD Foundation**  |
+----------------------------+-----------------------+---------------------------------------+----------------------+
| Letta (MemGPT)             | Self-Editing Memory   | Client/Server architecture; heavy if  | Adopt Core Memory    |
| (letta-ai/letta)           | (Core + Archival)     | running entire Python server stack.   | Data Architecture    |
+===================================================================================================================+
```

---

## 2. THE REVISED STRATEGY: THE ADAPTED TRIAD

No single project in the ecosystem spans the Windows desktop composition layer, browser tab integration, and sandboxed code execution under a single permissive open-source license.

We adopt **Engineering Option C: Combine and Adapt the Strongest Open Foundations**:

```
+===================================================================================+
|                 1. AMBIENT PRESENCE HUD & FAST-PATH COORDINATOR                   |
|                                                                                   |
|  - Foundation Option A (Recommended): Fork & Adapt Pillar Dynamic Island (Tauri)  |
|    • Tech: Tauri v2 + Rust + React. Native Windows WebView2 (< 50MB RAM).         |
|    • UI: Pinned top-screen Dynamic Island pill, expanding to card.                |
|    • Win32 Integration: WS_EX_NOACTIVATE, SMTC media transport, global hotkey.    |
|  - Foundation Option B: Clean PySide6 Host implementing the Hitodama architecture |
|    • Tech: Python 3.11 + PySide6. Direct Win32 COM / DirectComposition.           |
|  - Fast-Path Dispatcher: Instant local actions (< 50ms, zero LLM tokens).         |
|  - Embedded Memory Vault: SQLite WAL with Letta-style Tiered Core Memory.         |
+===================================================================================+
                                   ▲                     ▲
        Local REST Event Stream    │                     │ Local MCP / JSON-RPC
        (Optional Personal Sensor) │                     │ (localhost:59001)
                                   ▼                     ▼
+======================================+     +======================================+
|   2. OPTIONAL DESKTOP SENSOR         |     |   3. GOVERNED AGENT SANDBOX          |
|   (Screenpipe Sidecar - Personal Use)|     |   (NVIDIA OpenShell in WSL 2)        |
|                                      |     |                                      |
| - Role: Optional local background    |     | - License: Apache 2.0 (Permissive)   |
|   OCR & window timeline search.      |     | - Role: Governed sandbox for complex |
| - Policy: Audio disabled by default; |     |   multi-step coding, file edits,     |
|   frame rate throttled to 0.1 FPS    |     |   and package installation via       |
|   to preserve battery and RAM.       |     |   Landlock and Supervisor proxy.     |
+======================================+     +======================================+
```

---

## 3. COMPONENT-BY-COMPONENT AUDIT & REASSESSMENT

We subject every proposed component to the five rigorous evaluation questions:

### 3.1 Component 1: The Ambient Desktop Host & HUD
1. *Is the functionality already available in another candidate?*  
   Yes. Open-source Tauri Dynamic Island projects (`warpirate/pillar-dynamic-island-for-windows` and `aixiasang/Dynamic-Island-for-Windows`) already implement a top-screen collapsible pill, media transport controls, and notification toasts on Windows 10/11 using Tauri and Rust.
2. *Can an existing application provide it without modification?*  
   No. Existing Dynamic Islands are media/system monitor widgets; they lack integration with agent tool loops, model streaming, and cross-application context.
3. *Can an existing application's source be extended?*  
   **Yes.** Forking and modifying `pillar-dynamic-island-for-windows` (Tauri + React + Rust) allows us to reuse its native Windows window management and WebView2 animations while adding an LLM conversation card and MCP agent status lights.
4. *Is a separate component genuinely necessary?*  
   Yes. A desktop visual anchor is required for the presence to be experienced as an ambient companion rather than a terminal script.
5. *What are the maintenance costs?*  
   Low. Tauri v2 is mature, cross-platform, and compiles to a single native binary (~15MB).

---

### 3.2 Component 2: Passive Perception (Screenpipe Re-Evaluation)
1. *Is the functionality already available in another candidate?*  
   Yes. Screenpipe is the only mature open-source/source-available tool implementing continuous Windows Graphics Capture (WGC / DirectX 11) with a local SQLite timeline API.
2. *Can an existing application provide it without modification?*  
   Yes. Screenpipe runs as a standalone CLI executable (`screenpipe.exe`) exposing `http://localhost:3030`.
3. *Can an existing application's source be extended?*  
   Technically yes, but **legally constrained** by the Screenpipe Commercial License. Modifying and distributing Screenpipe as part of a product requires a paid enterprise license.
4. *Is a separate component genuinely necessary?*  
   **No. Continuous capture is NOT required for the core experience.**  
   Foreground window tracking (`SetWinEventHook`) and active browser tab querying (Chrome Extension) can be implemented in native Win32/Tauri in < 150 lines of code. Screenpipe should be treated as an **optional, user-installed sidecar** for users who explicitly want historical OCR search.
5. *Maintenance costs:*  
   Zero if consumed strictly via its public REST API; high if maintaining a source fork.

---

### 3.3 Component 3: Heavy Agent Sandboxing (NVIDIA OpenShell)
1. *Is the functionality already available in another candidate?*  
   Open Interpreter and Docker provide execution, but OpenShell is unique in providing **kernel-level Landlock confinement combined with supervisor-level credential injection**.
2. *Can an existing application provide it without modification?*  
   **Yes.** OpenShell can be installed in WSL 2 and governed entirely through declarative YAML configuration files and its standard REST API.
3. *Can an existing application's source be extended?*  
   Yes (Apache 2.0 Go/Rust), but source modification is unnecessary; configuration alone is sufficient.
4. *Is a separate component genuinely necessary?*  
   Yes, for **untrusted or destructive code execution**. When an agent runs arbitrary npm, pip, or bash scripts, running inside OpenShell protects the host Windows filesystem.
5. *Maintenance costs:*  
   Zero custom code. Pure upstream container and policy configuration.

---

### 3.4 Component 4: Universal Browser Connector (Chrome Extension)
1. *Is the functionality already available in another candidate?*  
   Existing tools (Playwright, Chrome DevTools MCP) either start logged-out or require opening dangerous remote debugging ports (`--remote-debugging-port=9222`).
2. *Can an existing application provide it without modification?*  
   No existing tool provides read-only access to live, authenticated browser tabs without credential exposure.
3. *Can an existing application's source be extended?*  
   Yes. We adapt standard open-source Chrome Extension Manifest V3 samples using `chrome.runtime.connectNative`.
4. *Is a separate component genuinely necessary?*  
   **Yes.** A dedicated ~300-line Chrome Extension + Native Messaging host is the only secure method to read authenticated web apps without cookie theft or browser restart.
5. *Maintenance costs:*  
   Extremely low. Standard W3C WebExtensions APIs.

---

### 3.5 Component 5: Memory & Continuity (Letta-Style Tiered Memory)
1. *Is the functionality already available in another candidate?*  
   Letta (MemGPT) and Mem0 provide memory models, but running their full server stacks adds substantial memory and process overhead.
2. *Can an existing application provide it without modification?*  
   Letta can run as a Docker container, but consumes ~500MB RAM for its Python/Postgres stack.
3. *Can an existing application's source be extended?*  
   Yes. We adopt the **Core Memory / Archival Memory data schemas** from Letta, but embed them directly inside the host's native SQLite database using SQLite WAL and FTS5.
4. *Is a separate component genuinely necessary?*  
   No separate server process is needed. It is a local SQLite database table managed directly by the desktop host.
5. *Maintenance costs:*  
   Zero external dependencies.

---

## 4. MODIFICATION ROADMAP & FORK IMPACT

```
+-----------------------------------------------------------------------------------+
| COMPONENT              | STRATEGY     | BASE PROJECT           | LICENSE VERIFIED |
+------------------------+--------------+------------------------+------------------+
| Desktop HUD & Island   | FORK & ADAPT | warpirate/pillar (Tauri| Open Source      |
| Passive Sensor         | OPTIONAL CLI | mediar-ai/screenpipe   | Commercial Lic.  |
| Agent Sandbox          | CONFIGURED   | NVIDIA/OpenShell (WSL2)| Apache 2.0       |
| Browser Connector      | CLEAN MV3    | WebExtensions Standard | Open Source      |
| Memory Engine          | EMBED SCHEMA | Letta Schema + SQLite  | Apache 2.0 / PD  |
| Fast-Path Router       | NATIVE WIN32 | Win32 ShellExecute/SMTC| Public Domain    |
+-----------------------------------------------------------------------------------+
```
