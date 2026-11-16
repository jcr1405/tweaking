# END-TO-END EXPERIENCE VALIDATION
## Scenario-Based Testing, Architecture Verification, and Acceptance Criteria
**Document Version:** 1.1.0-VALIDATION-AUDIT  
**Classification:** Operational Scenario Verification Standard  
**Test Platform:** Windows 11 x64 (AMD Ryzen AI / Discrete NVIDIA RTX GPU)  
**Revision Date:** November 2026  

---

## 1. THE FIVE CORE OPERATIONAL WORKFLOWS

We evaluate our revised foundation triad (Tauri/PySide6 Desktop Presence Host + OpenShell Sandbox + Native Connectors + SQLite Core Memory) against the five essential user workflows.

```
+===================================================================================+
| WORKFLOW A: Persistent Availability (Logon bootstrap & instant hotkey invocation) |
| WORKFLOW B: Context Awareness (Active browser page query without copying URLs)     |
| WORKFLOW C: Cross-Application Operation (Browser + IDE + Local Project workflow)  |
| WORKFLOW D: Continuity & Task Recovery (Surviving application & OS restarts)      |
| WORKFLOW E: Ambient Interaction & Resource Symbiosis (Gaming & silent throttling) |
+===================================================================================+
```

---

## 2. DETAILED WORKFLOW AUDIT & CODE BREAKDOWN

### WORKFLOW A: Persistent Availability
**The Scenario:** Windows boots, the user logs into Session 1. The companion is immediately available without manually opening terminal windows, servers, or browsers. User hits `Alt+Space`; presence responds in < 50ms.

- **What Already Works Today:**
  - Windows Task Scheduler `AtLogOn` / Startup Run Key automatically launches background processes in Session 1.
  - Tauri / PySide6 native window initialization with `RegisterHotKey`.
- **What Needs Configuration:**
  - Configure `AutoStart` plugin in Tauri (or `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`).
  - Configure global hotkey binding (`Alt+Space`).
- **What Requires Source Modifications:**
  - In `pillar-dynamic-island` or PySide6 host: Ensure window initializes in **hidden / collapsed pill state** (39px height) without taking focus (`WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`).
- **What Genuinely Needs New Code:**
  - Fast-Path Dispatch Router (~80 lines): Regex matcher mapping commands like `"open <project>"` to registered workspace directory paths and executing `ShellExecuteExW` directly in < 20ms without invoking an LLM.
- **Measurable Acceptance Criteria:**
  - [ ] Invocation to visible HUD animation: **< 40ms**.
  - [ ] Memory footprint in collapsed idle state: **< 60 MB RAM**.
  - [ ] Zero focus loss from the active desktop window upon HUD appearance.

---

### WORKFLOW B: Context Awareness (In-Context Browser Query)
**The Scenario:** The user is reading a technical article or documentation in Chrome. They trigger the HUD and ask: *"Summarize this and check if we support it."* The companion extracts the article content directly without requiring the user to copy-paste the URL or take a screenshot.

- **What Already Works Today:**
  - Chrome Extension Manifest V3 APIs (`chrome.tabs.query`, `chrome.scripting.executeScript`).
  - Chrome Native Messaging protocol for local JSON communication.
- **What Needs Configuration:**
  - Register Native Messaging Host manifest in Windows Registry (`HKCU\Software\Google\Chrome\NativeMessagingHosts\com.aice.browser_bridge`).
- **What Requires Source Modifications:**
  - None in Chrome itself.
- **What Genuinely Needs New Code:**
  - The Native Messaging Host binary (`aice_browser_bridge.exe` in Rust/Python, ~150 lines): reads 4-byte length-prefixed JSON from Chrome on stdin/stdout, exposing a local socket/pipe endpoint.
  - The Chrome Extension (`background.js` and `content_extractor.js`, ~200 lines): extracts clean readability text from the active tab and strips hidden fields, scripts, and password inputs.
- **Measurable Acceptance Criteria:**
  - [ ] Time from prompt submit to DOM text extraction: **< 50ms**.
  - [ ] First summary token displayed on screen: **< 1.0s (p50)**.
  - [ ] Zero session cookies or passwords accessed or exported.

---

### WORKFLOW C: Cross-Application Operation
**The Scenario:** The user asks: *"Find the discussion about AetherTime in my browser, compare it with the current implementation in VS Code, and prepare a report."*

- **What Already Works Today:**
  - NVIDIA OpenShell (WSL 2): Confines file reading and AST parsing of the VS Code project directory using Linux Landlock.
  - Chrome Extension: Queries open tabs matching the keyword "AetherTime".
  - SQLite Core Memory: Holds registered project paths and historical decision summaries.
- **What Needs Configuration:**
  - OpenShell declarative policy granting read-only access to `/mnt/c/.../projects/aethertime`.
- **What Requires Source Modifications:**
  - OpenShell agent prompt: Include tool definitions for `browser_get_tab_content` and local `fs_read_file`.
- **What Genuinely Needs New Code:**
  - Host Coordinator Planner (~120 lines): Decomposes the user intent into:
    1. Query browser connector for matching tab text.
    2. Dispatch read-only file queries to OpenShell sandbox.
    3. Synthesize the comparison report and present it in the expanded HUD.
- **Measurable Acceptance Criteria:**
  - [ ] End-to-end task initiates first tool action within **1.5s**.
  - [ ] Code inspection is strictly confined to the declared repository path.
  - [ ] Complete report renders formatted Markdown directly inside the HUD card.

---

### WORKFLOW D: Continuity & Task Recovery Across Restarts
**The Scenario:** The user begins a complex multi-file refactoring task. The laptop is rebooted mid-task. Upon logging back in, the companion restores its context, reports what was completed, and awaits approval to continue.

- **What Already Works Today:**
  - SQLite WAL mode ensures database transactions committed prior to reboot are 100% uncorrupted.
  - Git worktrees track uncommitted file modifications on disk.
- **What Needs Configuration:**
  - Enable SQLite WAL mode (`PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL;`).
- **What Requires Source Modifications:**
  - None in SQLite.
- **What Genuinely Needs New Code:**
  - The 7-State Task Lifecycle Machine in Host Memory (~100 lines):
    - On startup, queries `task_journal` for rows with status `RUNNING`.
    - Detects interrupted tasks, inspects disk/git diffs, and prompts the user via HUD notification: *"Interrupted task detected: AetherTime refactoring. 2 of 5 files updated. Resume?"*
- **Measurable Acceptance Criteria:**
  - [ ] Interrupted tasks are detected within **100ms** of user logon.
  - [ ] Zero duplicate non-idempotent writes executed without explicit user consent.

---

### WORKFLOW E: Ambient Interaction & Resource Symbiosis (Gaming)
**The Scenario:** The user is playing a demanding 3D game (e.g. Mech Arena or Cyberpunk) in borderless windowed mode. The companion remains available if explicitly summoned via hotkey, but consumes near-zero background CPU/GPU resources and never interrupts gameplay.

- **What Already Works Today:**
  - Windows API `SHQueryUserNotificationState` detects fullscreen applications.
  - Win32 `GetForegroundWindow` and `GetWindowRect` inspect active window dimensions.
- **What Needs Configuration:**
  - Set governor polling interval to 10 seconds during gaming state.
- **What Requires Source Modifications:**
  - In Desktop Host: Implement multi-signal gaming detector:
    - If `QUNS_RUNNING_D3D_FULL_SCREEN` is true OR foreground window matches monitor resolution without standard window borders (`WS_CAPTION | WS_THICKFRAME`), activate `STATE_GAMING`.
- **What Genuinely Needs New Code:**
  - Resource Throttling Handler (~60 lines):
    - Pauses optional background OCR/perception threads.
    - Sends signal to OpenShell/Ollama to unload local model weights from discrete GPU VRAM.
    - Suppresses non-critical ambient notifications.
- **Measurable Acceptance Criteria:**
  - [ ] Idle CPU usage during gaming: **< 0.1%**.
  - [ ] Dedicated VRAM allocated by companion: **< 30 MB** (model weights unloaded).
  - [ ] Zero focus stealing or frame stutter introduced to the 3D application.
