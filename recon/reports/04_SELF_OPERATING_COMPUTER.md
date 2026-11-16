# Reconnaissance Report 04: Self-Operating Computer

## 1. Application Overview
* **Repository:** `OthersideAI/self-operating-computer`
* **Maintainer:** OthersideAI / Matt Shumer
* **Target OS:** Cross-platform (Windows, macOS, Linux)
* **Stated Goal:** An experimental open-source framework enabling multimodal vision models to operate computers like a human.

---

## 2. Feature Inventory
* **Percentage-Based Coordinate System:** Maps all screen clicks to normalized decimal coordinates (`0.0` to `1.0`), decoupling the AI from the monitor's physical pixel resolution or display scaling.
* **Integrated Voice Mode:** Features a hands-free `WhisperMic` integration that transcribes verbal objectives directly into execution commands.
* **Visual Cursor Aiming Indicator:** Draws a brief circular trajectory around target coordinates before clicking so the user can visually track what the AI is targeting.
* **Direct VLM-to-Action Protocol:** Translates model outputs directly into basic atomic commands: `click`, `write`, `press`.

---

## 3. Architecture Analysis

```
User Voice / Text Prompt
          │
          ▼
┌───────────────────────────────────────────────┐
│ Execution Loop (operate.py)                   │
│ • Captures active screen                      │
│ • Builds multimodal conversation history      │
│ • Calls Vision API (get_next_action)          │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│ Coordinate Normalizer & Operating System      │
│ • Converts 0.0 - 1.0 -> screen pixel bounds   │
│ • Highlights target with circle animation     │
│ • Dispatches pyautogui click / write / press  │
└───────────────────────────────────────────────┘
```

### Key Modules Inspected:
* `operate/operate.py`: Controls the execution loop, loop count limits, and voice listener integration (`WhisperMic`).
* `operate/utils/operating_system.py`: Implements `OperatingSystem` class with `click_at_percentage`, `write`, and `press`.
* `operate/models/prompts.py`: Defines the system instructions formatting output into structured action dictionaries.

---

## 4. Technology Identification
* **Core Runtime:** Python 3.10+
* **Input Emulation:** `pyautogui`
* **Voice Recognition:** `whisper_mic` / OpenAI Whisper
* **Terminal UI:** `prompt_toolkit`

---

## 5. Strengths and Weaknesses

### Strengths:
1. **Resolution Independence:** By asking the model for percentage values (`{"x": 0.45, "y": 0.82}`) rather than raw pixel coordinates, it works across 1080p, 1440p, 4K, and 125%/150% Windows display scaling without breaking.
2. **Minimalist Codebase:** Very small, readable Python structure that is easy to understand and modify.

### Weaknesses:
1. **Lacks State Memory & Task Decomposition:** It only looks at the immediate screenshot; it doesn't maintain a structured multi-step checklist (Scratchpad), so it often gets lost on tasks longer than 3–4 clicks.
2. **Voice Has No Speaker Verification:** Uses vanilla Whisper, which accepts voice from anyone within microphone range.
3. **No Native OS Awareness:** Does not check Windows notification listeners, system telemetry, or native UI trees.

---

## 6. Reusable Engineering Concepts for Our Companion
1. **Normalized [0, 1000] Coordinate Protocol:** We must adopt normalized coordinates in our Gemini Vision prompt. Asking Gemini for coordinates between 0 and 1000 and multiplying by `(screen_width / 1000, screen_height / 1000)` completely neutralizes Windows display scaling bugs!
2. **Visual Focus Indicator:** Giving visual feedback before a click reassures the user and makes the companion feel physical and alive.
