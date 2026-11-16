# Master Synthesis: The Stepping-Stone Blueprint for the Desktop Companion

## Executive Summary
We conducted direct code-level reconnaissance on four major desktop AI and background agent repositories:
1. **Microsoft UFO:** Windows UI Automation tree (`pywinauto` / `UIAWrapper`) & dual-agent hierarchy.
2. **Open Interpreter / Anthropic CUA:** Human-like mouse easing (`easeInOutSine`), chunked typing, and token-saving screen downscaling.
3. **Screenpipe:** 24/7 background daemon architecture, frame-differencing gates, and local SQLite activity timeline.
4. **Self-Operating Computer:** Normalized coordinate mapping `[0, 1000]` and voice integration.

Additionally, we verified the host machine's native capabilities:
* **Hardware Telemetry:** Direct RTX 5050 GPU monitoring via `nvidia-smi` and CPU/RAM via `psutil`.
* **OS Notifications:** Native Windows Toast interception via `Windows.UI.Notifications.Management.UserNotificationListener` (Confirmed: `Allowed`).

---

## The Subsystem Matrix

| Subsystem | What Existing Repos Do | Our Upgraded Implementation | Target Layer |
| :--- | :--- | :--- | :--- |
| **1. Background Persistence** | Screenpipe uses Rust daemon; others are CLI scripts. | Always-on Python/Win32 background service with system tray icon and global hotkey. | Layer 2 |
| **2. Hardware Vitals** | Ignored by most agents. | Real-time monitoring of CPU, RAM, and RTX 5050 GPU temps/usage with proactive anomaly detection. | Layer 2 |
| **3. Notification Triage** | Screen scraping (slow, fragile). | Native WinRT `UserNotificationListener` captures messages & toasts directly from the OS queue. | Layer 2 |
| **4. Visual Screen Grounding** | Pixel guessing (unreliable across display scaling). | Normalized `[0, 1000]` coordinates + WXGA (1280x800) scaling + Windows UI Tree when available. | Layer 3 |
| **5. Action Execution** | Instant cursor teleportation. | `easeInOutSine` smooth mouse travel + chunked typing with 12ms pacing. | Layer 3 |
| **6. Goal & Task Memory** | UFO uses complex heavy multi-agent; SOC has no memory. | **Dynamic Scratchpad:** Goal -> Progress Checklist -> Current Step -> Verification. | Brain |
| **7. Safety & Killswitch** | Often disabled (`FAILSAFE = False`). | Hard corner-slam failsafe + global emergency hotkey + high-consequence action confirmation. | Safety |

---

## The Stepping-Stone Roadmap

Instead of trying to build a monolithic monster all at once, we build and validate **4 independent stepping-stone modules**:

```
┌────────────────────────────────────────────────────────┐
│ STEP 1: Hardware & Notification Sentinel               │
│ • Monitors CPU, RAM, RTX 5050 GPU temps                │
│ • Intercepts native Windows Toast notifications        │
│ • Proactively alerts when anomalies occur              │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│ STEP 2: The Vision & Precision Execution Engine        │
│ • Instant screenshot with frame-differencing           │
│ • Normalized [0, 1000] coordinate targeting            │
│ • Smooth mouse travel & chunked keyboard typing        │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│ STEP 3: Multi-Step Scratchpad & Autonomous Planner     │
│ • Decomposes high-level instructions into checklists   │
│ • Paced for Gemini Free Tier (15 RPM compliant)        │
│ • Self-corrects when actions don't change screen state │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│ STEP 4: The Unified Resident Companion                 │
│ • Runs continuously from boot                          │
│ • Global summon shortcut & voice interaction           │
│ • Persistent SQLite activity and context memory        │
└────────────────────────────────────────────────────────┘
```

Each step produces a self-contained, testable Python subsystem that we verify on your machine before moving to the next.
