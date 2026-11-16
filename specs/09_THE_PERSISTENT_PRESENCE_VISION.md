# THE PERSISTENT INTELLIGENT PRESENCE VISION
## Product Definition, Experience Model, and Architectural Pillars
**Document Version:** 1.1.0-VISION-AUDIT  
**Classification:** Product North Star & Human-Computer Interaction Standard  
**Target Device:** Modern Windows 11 Laptop (AMD Ryzen AI / Discrete NVIDIA RTX GPU, 16GB–32GB RAM)  
**Revision Date:** November 2026  

---

## 1. THE NORTH STAR: A BEING IN THE LAPTOP

### 1.1 Beyond the Chatbox and the Launcher
The contemporary AI paradigm is fundamentally broken by friction. Interacting with an AI today requires the user to:
1. Open a browser or desktop application window.
2. Select an AI vendor, model, or conversation thread.
3. Manually reconstruct context by typing lengthy explanations, copying file paths, or pasting screenshots.
4. Receive a block of text, copy code back to an editor, run it manually, and paste the error back.
5. Close the window, losing the operational context of the machine.

Even desktop command launchers (such as Raycast, PowerToys Run, or Flow Launcher) only provide transient, single-turn transaction prompts. They execute a query, launch an executable, and instantly vanish. They possess zero temporal memory, zero peripheral awareness, and zero persistent agency.

**Our Vision:** We are not building an LLM wrapper, an agent framework, a command-line interpreter, or a desktop search bar. We are creating a **persistent, intelligent presence integrated into the user's everyday laptop experience**.

```
TRADITIONAL PARADIGM: Fragmented Tool Usage
┌────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│ User Mind  │ ──> │ Open Chrome  │ ──> │ Paste Error │ ──> │ Copy to Code │
└────────────┘     └──────────────┘     └─────────────┘     └──────────────┘
                          │                    │                   │
                     (Manual Re-contextualization on every step)

INTENDED PARADIGM: Ambient Desktop Co-Pilot
┌──────────────────────────────────────────────────────────────────────────┐
│                             THE LAPTOP                                   │
│                                                                          │
│   Active Work: VS Code / Terminal / Chrome / Spotify                     │
│                                                                          │
│   ▲                                                                      │
│   │ Event-Driven Context Observation (Focused app, branch, tab, file)    │
│   ▼                                                                      │
│   ┌──────────────────────────────────────────────────────────────────┐   │
│   │                 PERSISTENT INTELLIGENT PRESENCE                  │   │
│   │ - Available instantly via hotkey, voice, or unobtrusive HUD      │   │
│   │ - Understands what you are doing without manual explanation      │   │
│   │ - Remembers previous decisions, interrupted tasks, and context   │   │
│   │ - Coordinates cross-application actions with bounded authority   │   │
│   └──────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

The presence feels like a capable, discreet digital companion that resides inside the machine. It is available when the user approaches the computer, remains aware of ongoing work, remembers previous decisions, executes cross-application workflows when asked, and quietly yields resources when the user is gaming or running heavy local workloads.

---

## 2. THE SIX CAPABILITY PILLARS

### 2.1 Pillar 1: Persistent Presence (Always Available, Zero Friction)
- **Logon Synchronization:** The presence initializes automatically when the user signs into Windows (Session 1+). It respects the Windows security boundary: it never executes in Session 0 before authentication and never attempts to bypass the Windows lock screen.
- **Background Continuity:** The presence remains alive independently of whether its visible interface (HUD or chat window) is open. Closing the window does **not** terminate active background reasoning, task supervision, or context perception.
- **Instant Invocation:** Available in < 50ms via:
  - Global configurable keyboard shortcut (e.g. `Win+Space` or `Alt+Space`).
  - Low-overhead local voice wake-word or push-to-talk.
  - Ambient screen-edge HUD tap.
- **Fault-Tolerant Recovery:** Survives unexpected application crashes, Windows Explorer restarts, display resolution changes, and laptop sleep/wake cycles. If interrupted by a system reboot, it recovers its previous task state cleanly.

---

### 2.2 Pillar 2: Contextual Awareness (Event-Driven vs. Heavy Recording)
The presence understands the user's immediate operational context without requiring manual narration:
- **Active Application & Window:** Knows which application is currently focused (e.g., VS Code editing `parser.rs` on branch `feat/mcp`).
- **Browser State:** Knows the active tab title and URL in the user's running browser session (e.g., reading a GitHub PR or API documentation).
- **Project State:** Detects the active Git workspace, recent test outcomes, or active build errors.
- **Media Activity:** Knows what is currently playing via Windows System Media Transport Controls.

> **Forensic Reality Check on Awareness (The 24/7 Recording Fallacy):**  
> Previous assumptions that continuous 24/7 video recording and audio transcription can run at "~20MB RAM and zero CPU" have been forensically refuted. Continuous Whisper transcription and OCR pipelines consume **500MB to 1.5GB+ RAM** and cause constant battery drain and CPU/GPU thrashing.  
> **The Awareness Standard:** Awareness must be **event-driven, selective, and lightweight**:
> 1. Use native OS event hooks (`SetWinEventHook`) to observe foreground window transitions (< 1ms, zero CPU).
> 2. Use browser extension connectors to query active tab URLs on-demand.
> 3. Disable continuous audio loopback recording by default; audio transcription should be strictly push-to-talk or on-demand meeting capture.
> 4. Respect privacy boundaries: banking domains, password managers, and incognito windows are strictly excluded from capture.

---

### 2.3 Pillar 3: Continuity & Memory (The Temporal Thread)
Human intelligence relies on unbroken episodic and semantic continuity. The presence maintains a three-tiered memory architecture:

```
+─────────────────────────────────────────────────────────────────────────────+
|                          THREE-TIERED MEMORY ENGINE                         |
|                                                                             |
|  1. WORKING CONTEXT BUFFER (Short-Term - Seconds to Minutes)                |
|     - Active window history, clipboard snippet, immediate shell output     |
|     - Fast in-memory circular ring buffer                                   |
|                                                                             |
|  2. EPISODIC & TASK JOURNAL (Medium-Term - Hours to Weeks)                  |
|     - Active multi-step agent plans, interrupted goals, recent workflows    |
|     - SQLite WAL journal with deterministic state flags                     |
|                                                                             |
|  3. SEMANTIC ENTITY & DECISION GRAPH (Long-Term - Months to Years)          |
|     - Architectural choices, personal preferences, project relationships    |
|     - Structured Core Memory (Letta/Mem0 model) + Local FTS5/Vector search  |
+─────────────────────────────────────────────────────────────────────────────+
```

- **Inspectable and Removable:** The user can open their memory vault at any time, search what the presence knows about a topic, edit entries, or delete facts permanently.
- **Workflow Recovery:** If the user initiates a multi-step task ("refactor the parser and run tests"), closes their laptop, and reopens it two hours later, the presence can report: *"Step 1 and 2 succeeded while you were away; Step 3 encountered a compilation error in `lexer.rs:42`. Shall I proceed with the fix?"*

---

### 2.4 Pillar 4: Bounded Agency (Deterministic Execution + Agent Delegation)
The presence is not a passive advisor; it can act inside the computing environment:
- **Fast Deterministic Actions (< 50ms):** Launching applications, switching windows, navigating browser tabs, media playback, and directory navigation execute directly via native Win32/COM APIs without invoking LLMs.
- **Intelligent Multi-Step Actions:** Complex requests are planned using reasoning models and dispatched through sandboxed agent tools (OpenShell / Open Interpreter execution engines).
- **The 4-Tier Authority Gate:**
  - *L0 (Observe):* Read-only context queries (window titles, file reads) -> **Autonomous**.
  - *L1 (Recommend):* Drafting text, suggesting diffs, preparing plans -> **Autonomous presentation**.
  - *L2 (Execute Reversible):* Editing files in approved workspace, switching tabs, running builds -> **Autonomous within policy**.
  - *L3 (Protected / Irreversible):* Deleting files, modifying credentials, sending external network payloads -> **Mandatory Interactive User Consent**.

---

### 2.5 Pillar 5: Ambient Interaction & Expressive Personality
The physical manifestation of the presence must feel calm, non-intrusive, and organic:
- **Dynamic Desktop Anchor (The Island / HUD):** A compact, hardware-accelerated, transparent overlay anchored to the top screen bezel or system tray:
  - *Collapsed State:* An elegant, subtle indicator pill (< 40px height) showing ambient status (Idle, Listening, Working).
  - *Expanded State:* Springs smoothly into an interactive card displaying summaries, action buttons, or conversational dialogue.
  - *Non-Stealing Focus:* Uses Win32 `WS_EX_NOACTIVATE` so that reading or dismissing the HUD never steals keyboard focus from the user's active code editor or game.
- **Open-Source Base:** Rather than relying on reverse-engineered proprietary code (Hitodama), the HUD must be implemented on open, modifiable foundations:
  - Modifying open-source Tauri/Rust Dynamic Island projects (e.g. `pillar-dynamic-island-for-windows` or `Dynamic-Island-for-Windows`), or
  - Clean PySide6 frameless transparent windows with Win32 DirectComposition attributes.
- **Tone:** Competent, direct, humble, and concise. Zero artificial chattiness, zero corporate disclaimers, zero sycophantic small talk.

---

### 2.6 Pillar 6: Resource Governance & Hardware Symbiosis
A companion that slows down the user's laptop is a failure.
- **Idle Overhead Budget:** CPU utilization < 0.3% when idle; Working Set RAM < 150 MB across background components when heavy AI models are uninitialized.
- **Workload Throttling:** Detects full-screen 3D gaming (DirectX/Vulkan) or heavy compilation, instantly pausing all background perception timers, OCR passes, and agent loops, and unloading local GPU/NPU weights from VRAM.
- **Power Awareness:** On battery power, reduces perceptual polling intervals by 5x and defers background indexing until the laptop is connected to AC power.
