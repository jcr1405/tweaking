# REVISED PLATFORM STRATEGY & STRATEGIC ROADMAP
## Reassessment of AICE, OpenShell Integration, and Universal Application Architecture
**Document Version:** 1.0.0-STRAT-SPEC  
**Classification:** Strategic Direction & Architectural Decision Standard  

---

## 1. STRATEGIC REASSESSMENT: IS AICE STILL NECESSARY?

To avoid falling victim to the sunk cost fallacy, we critically evaluate five possible outcomes for the AICE project:

```
+===================================================================================================================+
| OUTCOME CANDIDATES                                | VERDICT            | STRATEGIC RATIONALE                      |
+===================================================+====================+==========================================+
| Outcome A: Distinct Windows-Native Platform       | REJECTED           | Rebuilding an independent container,     |
| (Original Scope: Service + Custom Sandbox + Host) |                    | sandbox, and agent runtime duplicates    |
|                                                   |                    | mature open-source projects (OpenShell). |
+---------------------------------------------------+--------------------+------------------------------------------+
| Outcome B: Pure Universal App Integration Layer   | PARTIALLY VIABLE   | Solves application connectivity via MCP, |
| (Collection of MCP servers and extensions only)   |                    | but lacks the Windows ambient presence,  |
|                                                   |                    | fast-path dispatch, and resource governor|
+---------------------------------------------------+--------------------+------------------------------------------+
| Outcome C: Lightweight Windows Ambient Coordinator| **ADOPTED AS THE** | Bridges the Windows host (Session 0/1,   |
| & Universal Application Integration Layer         | **OFFICIAL FUTURE**| fast-path, ambient presence, native apps)|
| (Integrates OpenShell + Local MCP + Fast Path)    | **STRATEGY**       | with OpenShell's isolated agent runtime. |
+---------------------------------------------------+--------------------+------------------------------------------+
| Outcome D: Collection of Reusable Connectors      | INSUFFICIENT       | Connectors without a local coordinator   |
| (Loose Chrome extensions, VS Code plugins)        |                    | leave orchestration fragmented and slow. |
+---------------------------------------------------+--------------------+------------------------------------------+
| Outcome E: Completely Unnecessary                 | REJECTED           | Existing ecosystems have a major gap: no |
| (Abandon project; use existing tools as-is)       |                    | secure bridge connects isolated Linux    |
|                                                   |                    | sandboxes to running Windows desktop apps|
+===================================================================================================================+
```

### 1.1 The Genuine Platform Gap: The "Desktop-Sandbox Chasm"
Why does neither OpenShell, MCP, nor Windows alone solve the problem?
1. **NVIDIA OpenShell alone** is confined to a Linux container / WSL 2 sandbox. It has zero native ability to interact with running Windows desktop GUI applications (Chrome, VS Code, Spotify), cannot monitor Windows foreground window changes, and cannot execute low-latency (<100ms) local desktop commands.
2. **Anthropic MCP alone** is merely a wire protocol specification (JSON-RPC 2.0). It does not provide ambient presence, does not manage OS lifecycles, and offers no native bridge to authenticated browser sessions.
3. **Existing Browser Automation Tools (CDP / Playwright)** either require launching clean, unauthenticated browser instances (breaking live user workflows) or require opening `--remote-debugging-port=9222`, which exposes the entire browser, all cookies, and all sessions to unrestricted local compromise.
4. **Windows Native OS** manages hardware and processes, but does not expose structured semantic capabilities of user applications to AI reasoning engines.

> **The Unique Value of AICE (Outcome C):**  
> AICE serves as the **missing bilateral coordinator**: it lives on the Windows host as an ambient, low-latency coordinator that bridges isolated agent runtimes (like NVIDIA OpenShell) to the user's live desktop applications via bounded, capability-specific connectors—while preserving a sub-100ms deterministic fast path that bypasses AI entirely for routine actions.

---

## 2. SCOPE REVISION: WHAT STAYS, WHAT GOES, WHAT DELEGATES

To maximize engineering velocity and eliminate architectural bloat, we explicitly refactor the scope of the original architecture (`specs/01` through `specs/04`):

```
+-------------------------------------------------------------------------------------------------------------------+
| ORIGINAL AICE COMPONENT           | NEW ARCHITECTURAL STATUS | REASONING / DELEGATION TARGET                      |
+-----------------------------------+--------------------------+----------------------------------------------------+
| Bespoke Win32 Job Object Sandbox  | **DELEGATED**            | Delegated to NVIDIA OpenShell (Linux Landlock,     |
| for Agent Execution               |                          | seccomp, namespaces in WSL2).                      |
+-----------------------------------+--------------------------+----------------------------------------------------+
| Custom Network Egress Proxy &     | **DELEGATED**            | Delegated to OpenShell Supervisor (OPA policies    |
| Cloud Credential Vault            |                          | and header injection outside sandbox).             |
+-----------------------------------+--------------------------+----------------------------------------------------+
| Custom Agent Task State Machine   | **DELEGATED**            | Handled by standardized agent frameworks           |
| (Protobuf Replay, Step DB)        |                          | running inside OpenShell (Claude Code, LangChain). |
+-----------------------------------+--------------------------+----------------------------------------------------+
| Windows Session 0 Service         | **SIMPLIFIED**           | Retained solely as a lightweight boot watchdog and |
| (aice_svc.exe)                    |                          | machine telemetry monitor; no complex agent logic. |
+-----------------------------------+--------------------------+----------------------------------------------------+
| Windows Session 1 User Host       | **RETAINED & EXPANDED**  | Core host for Fast-Path router, local MCP server,  |
| (aice_host.exe)                   |                          | and application connector management.              |
+-----------------------------------+--------------------------+----------------------------------------------------+
| Deterministic Fast Path (<100ms)  | **NEW CORE PILLAR**      | Instant local command routing bypassing LLMs.      |
+-----------------------------------+--------------------------+----------------------------------------------------+
| Universal Application Connectors  | **NEW CORE PILLAR**      | Chrome Extension (MV3), Native Messaging,          |
| (Browser, IDE, Media, OS)         |                          | VS Code IPC, System Media Transport Controls.      |
+-----------------------------------+--------------------------+----------------------------------------------------+
| Resource Governor                 | **RETAINED**             | Multi-signal sensor (DirectX fullscreen detection) |
| (DirectX & Gaming Throttle)       |                          | to suppress background agent workloads during play.|
+-------------------------------------------------------------------------------------------------------------------+
```

---

## 3. PROPOSED INITIAL PROOF-OF-CONCEPT: UNIVERSAL APP CONNECTOR v0.1

### 3.1 Objective & Target Workflow
Build the minimal vertical slice demonstrating secure, live-session application integration between an isolated agent and the user's running browser:

> **Target User Workflow:**  
> *"Find a particular open or accessible ChatGPT conversation about AICE, extract its important architectural decisions, and prepare a summary."*

```
+===================================================================================+
|                        OPENSHELL SANDBOX (WSL2 Linux)                             |
|                                                                                   |
|  Agent (e.g. Claude Code / OpenShell Agent)                                       |
|    │                                                                              |
|    │ 1. Discovers tools via MCP (browser_list_tabs, browser_read_page_content)    |
|    │ 2. Issues tool call: browser_list_tabs()                                     |
|    ▼                                                                              |
|  MCP Client                                                                       |
+===================================================================================+
    │                                                                               
    │ JSON-RPC 2.0 over localhost:59001 (WSL2 -> Windows Host)                      
    ▼                                                                               
+===================================================================================+
|                    AICE NATIVE CONNECTOR SERVICE (Windows Host)                   |
|                                                                                   |
|  Local Browser MCP Server (aice_browser_bridge.exe)                               |
|    │                                                                              |
|    │ 3. Verifies tool policy & authorization (Read-Only OK)                       |
|    │ 4. Forwards request via Stdin (4-byte length-prefixed JSON)                  |
|    ▼                                                                              |
|  Chrome Native Messaging Host                                                     |
+===================================================================================+
    │                                                                               
    │ Chrome Native Messaging Pipe                                                  
    ▼                                                                               
+===================================================================================+
|                              GOOGLE CHROME (Session 1)                            |
|                                                                                   |
|  Chrome Extension (Manifest V3 Service Worker)                                    |
|    │                                                                              |
|    │ 5. Queries chrome.tabs for ChatGPT URL                                       |
|    │ 6. Injects content_extractor.js into matching tab (DOM querySelector)       |
|    │ 7. Returns extracted text (stripping auth tokens, scripts, password fields)  |
|    ▼                                                                              |
|  ChatGPT Tab (Active, Authenticated User Session)                                 |
+===================================================================================+
```

### 3.2 What the Experiment Proves
1. **No Credential Exfiltration:** The agent reads rendered conversational text without accessing session cookies, local storage, or login passwords.
2. **Zero Debugging Flags:** Chrome runs in its default, secure production state without `--remote-debugging-port`.
3. **Cross-Boundary MCP Communication:** The agent running inside an OpenShell Linux sandbox communicates reliably with a Windows native application connector.
4. **Granular Permission Enforcement:** The native connector verifies that only approved read operations on approved domains are executed.
5. **Empirical Latency Measurement:** Accurately measures the latency profile of Native Messaging + Extension DOM extraction + MCP roundtrips.

---

## 4. PRIORITIZED IMPLEMENTATION ROADMAP

We replace the original 5-milestone hardware-heavy plan with a phased integration roadmap:

```
+-----------------------------------------------------------------------------------+
| PHASE 1: Chrome Application Connector v0.1 (Current Priority)                    |
| - Chrome Extension (Manifest V3) with chrome.tabs and scoped content script       |
| - Native Messaging Host (Rust/C++ lightweight Win32 console app)                  |
| - Local Browser MCP Server exposing browser_list_tabs & browser_read_page_content |
| - Verified end-to-end extraction from live authenticated web app                  |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| PHASE 2: OpenShell Integration & Policy Hardening                                 |
| - Deploy OpenShell in WSL 2 on host machine                                       |
| - Connect OpenShell agent to the Windows Browser MCP server                       |
| - Configure OpenShell declarative YAML policies for local network access          |
| - Execute the ChatGPT conversation extraction workflow autonomously               |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| PHASE 3: Deterministic Fast-Path Dispatcher & Resource Governor                   |
| - Windows Host Fast-Path router (sub-100ms local tab switching and window focus)  |
| - Multi-signal DirectX fullscreen detector (gaming throttle)                      |
| - Performance benchmark harness measuring p50 and p95 across all execution paths  |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| PHASE 4: Expansion to Additional Application Connectors                           |
| - VS Code Connector (Editor state, active file, workspace diagnostics)            |
| - Media Connector (Windows System Media Transport Controls for Spotify/media)     |
| - Windows File Management Connector (Shell API / Explorer context)                |
+-----------------------------------------------------------------------------------+
```

---

## 5. IMMEDIATE NEXT STEPS

1. **User Review & Ratification:** Present the strategic pivot (Adopting Outcome C: AICE as the Windows Ambient Coordinator + Universal Application Integration Layer integrating OpenShell).
2. **Scaffold Phase 1 Proof-of-Concept:** Upon approval, design the exact manifest, JSON schemas, and protocol definitions for the Chrome Extension and Native Messaging Host.
