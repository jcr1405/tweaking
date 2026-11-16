# UNIVERSAL APPLICATION INTEGRATION SPECIFICATION
## Standardized Application Connectors, Browser Integration, and Bounded Authority
**Document Version:** 1.0.0-STRAT-SPEC  
**Classification:** Core Application Integration Architecture  
**Key Frameworks:** Model Context Protocol (MCP), Chrome Extension (MV3), Chrome Native Messaging, Windows UI Automation (UIA)  

---

## 1. THE UNIVERSAL APPLICATION INTEGRATION PARADIGM

Traditional desktop automation models force an AI agent to act either as a brittle screen-scraping bot (clicking pixels and typing simulated keystrokes) or as an all-powerful operating system user (executing arbitrary bash/powershell scripts with complete host privileges). Both approaches are flawed:
- **Pixel-clicking bots** are slow, brittle across UI updates, computationally expensive, and lack semantic understanding of data structures.
- **Unrestricted shell execution** is a catastrophic security risk, granting untrusted models the authority to delete files, exfiltrate data, or execute malicious payloads.

The **Universal Application Integration** paradigm replaces both approaches with **Controlled Capability Exposure**:
Applications expose their capabilities through standardized, bounded interfaces. The AI agent does not need to reverse-engineer an application's internal GUI or execute arbitrary host commands. Instead, it discovers declared tools via the **Model Context Protocol (MCP)** and invokes them through deterministic, permission-governed connectors.

```
+===================================================================================+
|                        OPENSHELL SANDBOX (Untrusted Workload)                     |
|                                                                                   |
|  +--------------------+         +----------------------------------------------+  |
|  | Autonomous Agent   | ------> | MCP Client (Model Context Protocol)          |  |
|  +--------------------+         +----------------------------------------------+  |
+===================================================================================+
                                         |
                                         | JSON-RPC 2.0 over HTTP / Local Socket
                                         | Host-Guest Network Boundary (WSL2 / Windows)
                                         v
+===================================================================================+
|                     AICE APPLICATION INTEGRATION SERVICE (Windows Host)           |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                     AICE Host Broker & Permission Gateway                   |  |
|  |  - Authenticates incoming MCP requests from OpenShell Sandbox               |  |
|  |  - Enforces Per-Tool Policies (Read vs Write, Sensitive Flags)              |  |
|  |  - Interactive User Consent Gate for Irreversible Mutations                 |  |
|  |  - Comprehensive Audit Logger                                               |  |
|  +-----------------------------------------------------------------------------+  |
|            |                        |                        |                    |
|            v                        v                        v                    v
|  +-------------------+    +-------------------+    +-------------------+    +-----------+
|  | Browser Connector |    | IDE Connector     |    | Media Connector   |    | OS API    |
|  | - Native Messaging|    | - VS Code Server/ |    | - Spotify Web API |    | Connector |
|  | - Chrome Ext (MV3)|    |   Extension API   |    |   Local IPC       |    | - Win32   |
|  +-------------------+    +-------------------+    +-------------------+    +-----------+
|            |                        |                        |                    |
|            v                        v                        v                    v
|  +-------------------+    +-------------------+    +-------------------+    +-----------+
|  | Google Chrome     |    | VS Code           |    | Spotify Desktop   |    | Windows 11|
|  | (Live User Tabs)  |    | (Active Project)  |    | (Playback State)  |    | OS Shell  |
|  +-------------------+    +-------------------+    +-------------------+    +-----------+
+===================================================================================+
```

---

## 2. MCP ARCHITECTURAL STRATEGY: HOW APPLICATION CAPABILITIES ARE EXPOSED

A critical design principle is that **we do not assume every application needs an independent, isolated MCP server executable running as a separate OS process**. Running 20 individual MCP server processes for 20 applications introduces severe RAM bloat, port contention, and lifecycle management overhead.

### 2.1 Comparative Analysis of Connector Topologies

| Integration Pattern | Memory Overhead | Complexity | Maintenance Burden | Security Isolation | Recommended Use Case |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **A. Standalone MCP Server per App** | High (~25-50MB per server process) | High (Multiple processes, ports, lifecycles) | High (Separate repos/dependencies) | High (Process isolation) | Complex third-party integrations with dedicated official servers (e.g. GitHub MCP, PostgreSQL MCP). |
| **B. Unified Domain-Based MCP Servers** | Moderate (~30MB for entire domain) | Moderate (One server routes to multiple app connectors) | Moderate (Cohesive domain types) | High (Domain boundaries) | Grouping related tools: e.g. `browser_mcp` handles Chrome, Edge, and web queries; `dev_mcp` handles VS Code and Git. |
| **C. Shared AICE Application Integration Host** | **Lowest** (~20-40MB single host process) | **Lowest** (Single central daemon routes requests) | **Lowest** (Unified codebase, single config) | **Highest** (Centralized permission & audit engine) | **RECOMMENDED CORE ARCHITECTURE.** Single native host exposing a unified MCP endpoint while multiplexing internal connectors. |
| **D. Direct Extensions / Plugins** | Embedded in App | Low | Low (Leverages app ecosystem) | App-dependent | Extending apps from within (e.g. Chrome Extension, VS Code Extension). |
| **E. Windows UI Automation (Fallback)** | Zero extra process | High (COM tree navigation) | Moderate (Windows OS updates) | Low (Operates on visual elements) | Applications lacking APIs, extensions, or IPC. |

> **Architectural Decision:**  
> Adopt **Pattern C (Shared AICE Host)** with **Pattern D (Targeted Extensions)**. A single lightweight Windows host process (`aice_host.exe`) acts as the unified MCP server for the OpenShell agent, internally dispatching requests to modular application connectors (Chrome Native Messaging, VS Code IPC, Spotify API, Win32).

---

## 3. CHROME BROWSER CONNECTOR: DETAILED DESIGN & PROOF-OF-CONCEPT

The primary application connector to be developed and verified is the **AICE Chrome Connector**.

### 3.1 Why Chrome Extension + Native Messaging Wins Over Alternatives
Before adopting a custom extension, we audited existing browser automation solutions:

| Solution | Mechanism | Handling of Existing Authenticated User Sessions | Security Posture | Verdict |
| :--- | :--- | :--- | :--- | :--- |
| **Playwright MCP** | Launches independent Chromium browser via DevTools protocol. | **Poor.** Starts completely logged out. Requires exporting session cookies (`storageState.json`) or script-based automated login. | Isolated profile, but credential export exposes tokens in plain files. | Rejected for live daily user workflows. Ideal for CI/CD testing. |
| **Chrome DevTools MCP (CDP)** | Attaches via `--remote-debugging-port=9222`. | **Good.** Attaches directly to running Chrome instance, inheriting cookies and active tabs. | **Catastrophic Security Risk.** Opening port 9222 grants unrestricted remote debugging access to *any* local process: full cookie dump, raw memory inspection, arbitrary JS injection. | Rejected for production desktop environment due to lack of authorization boundaries. |
| **browser-use / Vision Bots** | Takes screenshots, runs vision model, clicks coordinates. | **Moderate.** Operates on whatever is visually rendered. | High latency (~2-5s per action), high token cost, brittle, fails on scrolling/offscreen DOM. | Rejected as primary path; retained strictly as last-resort fallback. |
| **AICE Chrome Extension + Native Messaging** | Official Chrome Extension (MV3) + Native Messaging Host (`connectNative`). | **Optimal.** Runs directly inside the user's running, daily, authenticated Chrome profile. No debugging flags required. Zero cookie theft possible. | **Highest.** The extension exposes *strictly defined functional tools* (`get_tab_summary`, `extract_article_text`). Raw session tokens and passwords cannot be read by the agent. | **RECOMMENDED DIRECTION.** |

---

### 3.2 Detailed Chrome Extension Architecture (Manifest V3)

```
+===================================================================================+
|                               GOOGLE CHROME BROWSER                               |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                 Chrome Extension Service Worker (background.js)             |  |
|  |  - Manifest V3 Event-Driven Lifecycle                                       |  |
|  |  - chrome.runtime.connectNative("com.aice.browser_bridge")                   |  |
|  |  - Persistent Native Port keeps Service Worker alive during active session  |  |
|  |  - Dispatches actions via chrome.tabs, chrome.scripting                     |  |
|  +-----------------------------------------------------------------------------+  |
|            |                                                         |            |
|            | chrome.tabs.sendMessage                                 | Tab Query  |
|            v                                                         v            |
|  +------------------------------------+            +---------------------------+  |
|  | Injected Content Script            |            | Chrome Tab Registry       |  |
|  | (content_extractor.js)             |            | (Active Tab, URL, Title,  |  |
|  | - Scoped DOM querySelector         |            |  Window ID, Incognito)    |  |
|  | - Strips <script>, <style>, hidden |            +---------------------------+  |
|  | - Returns clean text / markdown    |                                           |
|  +------------------------------------+                                           |
+===================================================================================+
                                         ^
                                         | Chrome Native Messaging
                                         | Stdin / Stdout (4-byte length-prefixed JSON)
                                         v
+===================================================================================+
|                         AICE NATIVE MESSAGING HOST & MCP SERVER                   |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                 aice_browser_bridge.exe (Native Windows Binary)             |  |
|  |  - Registered in HKCU\Software\Google\Chrome\NativeMessagingHosts           |  |
|  |  - Stdin/Stdout JSON loop communicating with Chrome Extension               |  |
|  |  - Exposes Local MCP Server (HTTP / WebSocket / stdio) to OpenShell Sandbox  |  |
|  |  - Enforces Per-Tool Access Policies and PII Filtering                      |  |
|  +-----------------------------------------------------------------------------+  |
+===================================================================================+
```

### 3.3 Declared Capabilities (Exposed MCP Tools)
The Chrome connector exposes strictly bounded, non-destructive tools:

1. `browser_list_tabs()`:
   - Returns a structured list of open tabs: `{ id: number, title: string, url: string, active: boolean, audible: boolean }`.
   - Strips session IDs and authentication query parameters from URLs.
2. `browser_get_active_tab()`:
   - Returns metadata for the currently focused tab in the active Chrome window.
3. `browser_read_page_content(tab_id: number, selector?: string)`:
   - Injects a read-only content script to extract readable text, headers, and metadata from the specified tab.
   - If `selector` is provided, scopes extraction to matching DOM elements (e.g. `.conversation-turn` on ChatGPT/Claude).
   - Automatically ignores `<input type="password">`, hidden fields, scripts, stylesheets, and SVG paths.
4. `browser_open_tab(url: string, active: boolean)`:
   - Opens a specified URL in a new tab. Validates URL against approved schemas (`http://`, `https://`).
5. `browser_switch_tab(tab_id: number)`:
   - Brings an existing tab to the foreground.

### 3.4 Authenticated Session Handling Without Credential Exfiltration
- **Zero Token Extraction:** The extension does **not** request the `cookies` permission in `manifest.json`. It is physically impossible for the extension to read session cookies, bearer tokens, or local storage auth keys.
- **In-Context Reading:** When extracting content from an authenticated site (e.g. ChatGPT, GitHub, JIRA), the extension reads the rendered DOM text that the user already has permission to view.
- **Zero Cross-Origin Bypasses:** Content scripts execute under the browser's native Same-Origin Policy (SOP). The extension cannot execute cross-origin requests on behalf of the user unless explicitly whitelisted in host permissions.

---

## 4. GENERALIZING BEYOND BROWSERS: THE UNIVERSAL INTEGRATION HIERARCHY

To integrate the rest of the user's computing environment, AICE follows a disciplined, prioritized integration hierarchy. Developers should never drop to screen automation if a higher-tier interface exists:

```
  Tier 1: Official Native APIs (REST, Local IPC, COM, WebSockets)
     │   (Fastest, highest reliability, structured schemas)
     ▼
  Tier 2: Official Extensions & Plugin Ecosystems (VS Code, Chrome)
     │   (Operates within application security model, rich object model)
     ▼
  Tier 3: Community Maintained MCP Servers (GitHub, SQLite, Postgres)
     │   (Standardized tools, active maintenance, community vetted)
     ▼
  Tier 4: Application-Specific IPC & Scripting (Named Pipes, CLI, AppleScript/WSH)
     │   (Direct automation, medium maintenance)
     ▼
  Tier 5: Windows UI Automation (COM IUIAutomation5)
     │   (Universal fallback for GUI apps without APIs; structured accessibility tree)
     ▼
  Tier 6: Visual & Screen-Based Interaction (Windows Graphics Capture + Vision)
         (Absolute last resort for games, legacy canvas apps, or obscure GUIs)
```

### 4.1 Application Category Integration Matrix

| Category | Representative Applications | Preferred Integration Tier | Technical Mechanism | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **Browsers** | Google Chrome, Brave, MS Edge | **Tier 2 (Extension)** | Chrome Extension (MV3) + Native Messaging Host | Seamless access to live authenticated tabs without security risks of remote debugging flags. |
| **Code Editors & IDEs** | VS Code, Cursor | **Tier 1 & Tier 2** | VS Code Extension + Native IPC or CLI (`code --status`) | Native VS Code extension accesses language servers, active editors, diagnostics, and terminal sessions with zero latency. |
| **Communication** | Discord, Slack, Microsoft Teams | **Tier 1 (Official API) / Tier 5 (UIA)** | Official REST / Bot API (for read/send); UIA for active desktop window state | Official APIs provide reliable structured messaging. Writing via UIA is brittle; reading notifications via Windows Notification Listener is clean. |
| **Productivity & Notes** | Obsidian, Notion | **Tier 1 (Local Files / API)** | Obsidian: Direct Markdown filesystem access; Notion: Official REST API | Obsidian is pure local Markdown files (ideal for direct filesystem MCP reading). Notion has an official structured REST API. |
| **Media & Audio** | Spotify, VLC | **Tier 1 (Local Web API / COM)** | Spotify Local Web API / Windows Media Transport Controls (`SMTC`) | Windows System Media Transport Controls (`GlobalSystemMediaTransportControlsSessionManager`) allows play/pause/track inspection for *any* media player. |
| **File Management** | Windows Explorer | **Tier 1 (Win32 APIs)** | Windows Shell API (`IShellWindows`, `SHGetFileInfoW`), NTFS USN Journal | Win32 Shell COM interfaces query open Explorer folders directly in < 5ms without launching external tools. |
| **Legacy Apps (No API)** | Custom ERP, legacy Win32/WPF tools | **Tier 5 (Windows UIA)** | COM `IUIAutomation5` Scoped Cache Requests | Walks the native accessibility tree, reading text, invoking button patterns, and setting edit box values deterministically. |

---

## 5. SECURITY, PERMISSIONS, AND PRIVILEGE BOUNDARIES

Application connectors expose powerful capabilities. If an untrusted model can invoke any tool arbitrarily, a prompt injection attack from an untrusted webpage could cause the agent to exfiltrate documents or send unauthorized messages.

```
+===================================================================================+
|                          AICE HOST PERMISSION GATEWAY                             |
|                                                                                   |
|  Incoming Intent: browser_read_page_content(tab_id=14)                            |
|                                                                                   |
|  [CHECK 1: ORIGIN VERIFICATION]                                                   |
|  - Verified caller: OpenShell Sandbox (WSL2 localhost:59001)                     |
|  - Validated bearer token & HMAC signature                                        |
|                                                                                   |
|  [CHECK 2: TOOL CLASSIFICATION]                                                   |
|  - Tool: browser_read_page_content                                                |
|  - Authority Level: Level 0 (Observe / Read-Only)                                 |
|                                                                                   |
|  [CHECK 3: DOMAIN & SENSITIVE TARGET AUDIT]                                       |
|  - Target URL: "https://chatgpt.com/c/67...-..."                                  |
|  - Sensitive Domain Check: NOT a banking, payment, or password-manager domain     |
|  - Permitted Policy Match: "chatgpt.com/*" explicitly whitelisted for session    |
|                                                                                   |
|  [DECISION: EXECUTE AUTONOMOUSLY]                                                 |
|  - Dispatches to Native Messaging Host                                            |
|  - Logs audit record to %LOCALAPPDATA%\aice\audit.log                             |
+===================================================================================+
```

### 5.1 Least-Privilege & Read/Write Separation
Every tool exposed across an application connector must declare its mutability:
- **Read-Only Tools (`read`):** `browser_list_tabs`, `browser_read_page_content`, `media_get_status`, `explorer_get_active_folder`.  
  *Policy:* Autonomous execution permitted if the target entity matches active project whitelists.
- **Reversible Write Tools (`write_reversible`):** `browser_switch_tab`, `browser_open_tab`, `media_toggle_play`.  
  *Policy:* Autonomous execution permitted within active workspace policy.
- **Irreversible / Destructive Tools (`write_destructive`):** `browser_close_tab`, `discord_send_message`, `fs_delete_file`.  
  *Policy:* **Mandatory Interactive User Confirmation.** AICE displays a non-activating HUD confirmation prompt. The action cannot proceed until the user clicks approve or provides biometric/key confirmation.

### 5.2 Prompt Injection & Tool Poisoning Defenses
1. **Untrusted Data Tainting:** All text extracted from web pages, document files, or external chat messages is tagged as **Untrusted External Data**.
2. **Context Envelope Isolation:** Untrusted data is delivered to the agent inside strict data envelopes (e.g. `<untrusted_content>` tags or separate JSON fields) with explicit system instructions prohibiting the model from treating extracted text as execution instructions.
3. **No Direct Code Execution from Extracted Data:** Extracted web content can never be passed directly into shell execution or browser evaluation tools without re-validation by the Capability Broker.

### 5.3 Audit Logging & Instant Permission Revocation
- Every tool invocation across all application connectors is written to an append-only audit journal (`%LOCALAPPDATA%\aice\connector_audit.jsonl`).
- Users can revoke connector permissions instantly via the AICE tray icon or global hotkey (`Win+Alt+A -> Revoke Browser Access`).
- Revocation immediately severs the Native Messaging port and rejects subsequent MCP requests with `403 Forbidden`.
