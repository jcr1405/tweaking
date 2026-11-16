# Reconnaissance Report 01: Microsoft UFO (UI-Focused Agent for Windows)

## 1. Application Overview
* **Repository:** `microsoft/UFO`
* **Maintainer:** Microsoft Research
* **Target OS:** Windows 10 & 11
* **Stated Goal:** An open-source, dual-agent framework designed to translate natural language user requests into actionable operations directly on the Windows operating system across multiple applications.

---

## 2. Feature Inventory
* **Dual-Agent Hierarchy:** Separates high-level multi-application workflow orchestration from low-level intra-application UI manipulation.
* **Windows UI Automation (UIA) Tree Parsing:** Traverses the native Windows Accessibility tree to locate buttons, text inputs, menus, and windows without relying purely on pixel guesses.
* **Hybrid Visual + Structural Grounding:** Concurrently captures screenshots and correlates them with exact element coordinates extracted from `UIAWrapper`.
* **Action Trajectory & Experience RAG:** Stores successful execution paths in a local vector database to accelerate repetitive tasks.
* **Control Filter & Safe Path Validator:** Checks control visibility, bounds, and permissions before dispatching click or typing commands.

---

## 3. Architecture Analysis

```
User Prompt: "Summarize the latest document and draft an email"
      │
      ▼
┌───────────────────────────────────────────────┐
│ HostAgent (Application Orchestrator)          │
│ • Inspects desktop & active windows           │
│ • Selects target application (Word / Outlook) │
│ • Dispatches sub-goal to AppAgent             │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│ AppAgent (Intra-App Specialist)               │
│ • Captures screenshot + extracts UITree       │
│ • Sends multimodal prompt to LLM              │
│ • Formulates atomic UI actions                │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│ ControlReceiver / Automator (OS Execution)    │
│ • pywinauto / UIAWrapper / pyautogui          │
│ • Executes atomic click_input / type / hotkey │
│ • Validates focus and state transition        │
└───────────────────────────────────────────────┘
```

### Key Modules Inspected:
* `ufo/automator/ui_control/controller.py`: Houses `ControlReceiver` and command execution primitives. Sets `pywinauto.timings.Timings` and wraps atomic execution with exception logging.
* `ufo/automator/ui_control/ui_tree.py`: Traverses the Windows UI Automation tree via `pywinauto.controls.uiawrapper.UIAWrapper`. Extracts control properties: `id`, `name`, `control_type`, `rectangle` (`left`, `top`, `right`, `bottom`), and adjusted relative coordinates.
* `ufo/automator/ui_control/screenshot.py`: Captures window-specific and full-screen screenshots using Windows GDI / PrintWindow, normalizing DPI scaling.
* `ufo/agents/agent/host_agent.py` & `app_agent.py`: Implement the decision-making loop with system prompts tailored for Windows desktop semantics.

---

## 4. Technology Identification
* **Core Runtime:** Python 3.10+
* **OS Automation:** `pywinauto` (Windows UI Automation backend), `pyautogui`
* **Coordinate & Window APIs:** Win32 APIs via `ctypes` (`win32gui`, `win32process`, `win32api`)
* **Computer Vision / Image Ops:** `Pillow` (PIL), `opencv-python`
* **Persistence & RAG:** Local vector database (`chromadb` / `faiss`) for experience replay

---

## 5. Strengths and Weaknesses

### Strengths:
1. **Unrivaled Precision on Windows:** By combining the native Windows Accessibility tree with screenshots, it doesn't need to guess coordinates. It knows the exact pixel bounding box of every native button.
2. **True Multi-App Workflows:** The separation between `HostAgent` (switching apps) and `AppAgent` (working inside an app) keeps the context window clean.
3. **Experience Memory:** Caches previous successful action sequences so repeated workflows become faster.

### Weaknesses:
1. **API Call Explosion (Free-Tier Unfriendly):** A single mouse click can trigger 4–6 LLM calls (Host Agent plan $\rightarrow$ App Agent plan $\rightarrow$ Action execution $\rightarrow$ Reflection/Verification). This immediately exhausts free-tier rate limits (like Gemini's 15 RPM).
2. **Heavyweight & Complex:** Hundreds of files, heavy prompt templates, and significant initialization overhead. Not designed as an always-on background daemon.
3. **Laggy on Custom/Non-Standard UIs:** Electron, Flutter, or games that don't expose proper UIA accessibility nodes force UFO to degrade to pure visual guessing.

---

## 6. Reusable Engineering Concepts for Our Companion
1. **Windows UI Tree Extraction (`ui_tree.py`):** We must borrow this exact technique! When operating a Windows app, querying `pywinauto` for the active window's controls provides 100% accurate coordinates without token-expensive computer vision guesses.
2. **Relative Coordinate Normalization:** Adjusting bounding boxes relative to the target application's window rectangle rather than absolute screen coordinates prevents misclicks when windows move or resize.
3. **Fail-Safe Focus Assertion:** Before clicking or typing, calling `set_focus()` and verifying the control is enabled prevents dropped keystrokes.

---

## 7. Security Observations
* UFO sets `pyautogui.FAILSAFE = False` in several places, which is dangerous if an agent enters an infinite clicking loop.
* Lacks native OS notification listening—relies entirely on visual screen inspection to notice changes.
