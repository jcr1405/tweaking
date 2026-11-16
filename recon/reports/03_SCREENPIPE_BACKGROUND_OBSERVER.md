# Reconnaissance Report 03: Screenpipe (24/7 Background Ambient Observer)

## 1. Application Overview
* **Repository:** `mediar-ai/screenpipe`
* **Maintainer:** Mediar AI & Open Source Contributors
* **Target OS:** Windows 10/11, macOS, Linux
* **Stated Goal:** 24/7 local screen and audio capture engine running as a lightweight background daemon that indexes everything you see and hear into a local SQLite database for AI agents.

---

## 2. Feature Inventory
* **Zero-CPU Hardware Screen Capture:** Leverages Windows Graphics Capture (WGC / DirectX 11) to capture display frames directly from GPU memory without CPU stutter or GDI lockups.
* **Continuous Multi-Stream Audio:** Records both microphone input and speaker loopback (system audio) concurrently.
* **Local SQLite Timeline Coordinator:** Stores window titles, captured OCR text, timestamps, audio transcripts, and app metadata locally.
* **Lightweight Background Daemon:** Engineered to stay resident in memory from startup with low idle overhead (~15-30MB RAM, <1% CPU).
* **Native Windows Overlay Subsystem:** Contains `screenpipe-overlay-win` for rendering transparent desktop overlays and floating presence.
* **PII Redaction Pipeline:** Automatically redacts credit cards, passwords, and sensitive fields before sending visual data to models.

---

## 3. Architecture Analysis

```
┌─────────────────────────────────────────────────────────────┐
│                 Background Daemon (Rust Engine)             │
│                                                             │
│   Windows Graphics Capture (WGC)       System Audio / Mic   │
│         [DirectX 11 Surface]           [WASAPI Loopback]    │
│                  │                             │            │
│                  ▼                             ▼            │
│       Frame Differencing Gate           Voice Activity Det. │
│                  │                             │            │
│                  ▼                             ▼            │
│        Local OCR Pipeline              Whisper Transcriber  │
│                  │                             │            │
│                  └──────────────┬──────────────┘            │
│                                 ▼                           │
│                     Local SQLite Database                   │
│             (Window Titles, Timestamps, Text)               │
└─────────────────────────────────┬───────────────────────────┘
                                  │
                                  ▼
                     AI Agent Query Interface (REST / MCP)
```

### Key Modules Inspected:
* `screenpipe-screen/src/capture_screenshot_by_window.rs`: Uses native OS APIs to capture specific target windows or full displays.
* `screenpipe-screen/examples/wgc_session_ab.rs`: Implements Windows Graphics Capture (`Windows.Graphics.Capture`) interop for zero-overhead capture on modern Windows 10/11.
* `screenpipe-db`: SQLite schema and queries for continuous semantic context and activity history.
* `screenpipe-overlay-win`: Manages transparent, click-through, or interactive floating HUD windows on Windows desktop.

---

## 4. Technology Identification
* **Core Daemon Language:** Rust (high concurrency, low memory footprint)
* **Windows Display Capture:** `Windows.Graphics.Capture` (WinRT / DirectX 11)
* **Windows Audio Capture:** Windows Audio Session API (WASAPI)
* **Local Database:** SQLite with WAL mode (Write-Ahead Logging) for concurrent reads/writes
* **UI Overlay:** Native Win32 window flags (`WS_EX_LAYERED`, `WS_EX_TRANSPARENT`, `WS_EX_TOPMOST`) and Tauri/WebView

---

## 5. Strengths and Weaknesses

### Strengths:
1. **The Blueprint for "Always-On" Persistence:** Unlike terminal tools that execute once and quit, Screenpipe runs continuously from system startup without hogging CPU or RAM.
2. **DirectX / WGC Capture:** Bypasses legacy GDI screenshot methods, preventing the micro-stutter typical of Python `PIL.ImageGrab`.
3. **Local Timeline Memory:** Gives the AI an immediate historical memory (*"What was the name of that article I looked at 20 minutes ago?"*).

### Weaknesses:
1. **Observation-Only (Passive):** Screenpipe is strictly a sensor/recorder; it has no autonomous mouse/keyboard actuation or multi-step execution agent built in.
2. **Large Rust Compilation Footprint:** Complex compilation toolchains and native C/C++ dependencies make rapid Python prototyping harder.

---

## 6. Reusable Engineering Concepts for Our Companion
1. **Frame Differencing (Change Detection Gate):** Screenpipe doesn't run OCR or send screenshots if the screen hasn't changed. Calculating a quick hash or pixel difference of the screen saves 90% of idle API calls and CPU cycles!
2. **Local SQLite Timeline Substrate:** Maintain an ultra-lightweight SQLite table (`activity_log.db`) tracking `{timestamp, active_app, window_title, summary, status}` for the companion's long-term memory.
3. **Native Win32 Overlay Styles:** Use standard Windows extended window styles (`WS_EX_TOPMOST | WS_EX_TOOLWINDOW`) so the companion's HUD stays pinned without stealing focus from active games or work.
