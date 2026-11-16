# LATENCY AND DUAL-PATH EXECUTION MODEL
## Fast-Path Determinism, Intelligent Reasoning, and Latency Benchmarking
**Document Version:** 1.0.0-STRAT-SPEC  
**Classification:** Runtime Performance & Execution Latency Specification  
**Target Platform:** Windows 11 x64 (AMD Ryzen AI / Discrete NVIDIA RTX GPU, NVMe SSD)  

---

## 1. THE DUAL-PATH EXECUTION ARCHITECTURE

A fundamental flaw of contemporary AI agent prototypes is routing every user intent through a Large Language Model (LLM). Routing simple, deterministic requests (e.g. "open Chrome", "switch to tab 3", "pause music", "focus VS Code") through a cloud or local LLM incurs 800ms–3000ms of inference latency, network roundtrips, token costs, and non-deterministic hallucination risks.

To achieve immediate desktop responsiveness, AICE implements a **Dual-Path Execution Model**:

```
                                USER COMMAND
                       (Hotkey / CLI / Ambient HUD / Pipe)
                                     │
                                     ▼
                ┌─────────────────────────────────────────┐
                │        INTENT CLASSIFIER / ROUTER       │
                │        (Deterministic Trie / Regex)     │
                │             Latency: < 2 ms             │
                └─────────────────────────────────────────┘
                                     │
                 ┌───────────────────┴───────────────────┐
                 │                                       │
        [Deterministic Match]                  [Ambiguous / Multi-Step]
                 │                                       │
                 ▼                                       ▼
    ╔═════════════════════════╗             ╔═════════════════════════╗
    ║        FAST PATH        ║             ║     INTELLIGENT PATH    ║
    ║   (Direct Host Native)  ║             ║ (OpenShell / Model MCP) ║
    ║   Target: < 100 ms      ║             ║ Target: ~1s Warm Start  ║
    ╚═════════════════════════╝             ╚═════════════════════════╝
                 │                                       │
                 ├───────────────────┐                   ├───────────────────┐
                 ▼                   ▼                   ▼                   ▼
        Direct Win32 API    Native Messaging      OpenShell Sandbox   Cloud / Local LLM
        (ShellExecuteW,      (Chrome Extension     (WSL2 crun/cgroups  (Gemini / Claude /
         SetForegroundWnd)    Tab Switch)           Policy Check)       RTX TensorRT)
```

---

## 2. THE FAST PATH: DETERMINISTIC LOCAL EXECUTION

### 2.1 Fast-Path Scope & Invariants
The Fast Path is designed for predictable, deterministic commands where intent mapping is unambiguous.  
**Invariants:**
1. **Zero LLM Invocations:** The Fast Path never calls an inference model.
2. **Bypasses MCP Entirely:** Routine local actions do not pass through the Model Context Protocol JSON-RPC layer; they execute directly against native Win32/COM or local pipe handles.
3. **Execution Bound:** Target dispatch and first visible action initiation: **under 100 milliseconds**.

### 2.2 Operations Governed by the Fast Path
- **Application Focus & Launching:** "Open VS Code", "Switch to Chrome", "Bring Discord to front" (`ShellExecuteExW`, `SetForegroundWindow`, `SwitchToThisWindow`).
- **Browser Navigation (Known URLs / Active Tabs):** "Open github.com", "Switch to tab 2", "Close current tab" (Dispatched directly via Chrome Native Messaging pipe without MCP wrapping).
- **System Audio & Media Transport:** "Play", "Pause", "Next track", "Mute" (Windows System Media Transport Controls COM API: `ISystemMediaTransportControls`).
- **Workspace Navigation:** "Open project tweaking in terminal" (`CreateProcessW` directly in designated directory).
- **Window Management:** "Snap left", "Maximize", "Move to monitor 2" (`SetWindowPos`, `ShowWindowAsync`).

---

## 3. THE INTELLIGENT PATH: REASONING & OPENSHELL GOVERNANCE

### 3.1 Intelligent-Path Scope
The Intelligent Path is activated when a user instruction requires semantic comprehension, ambiguity resolution, cross-application synthesis, or multi-step execution.  
**Examples:**
- "Find the conversation where we discussed AetherTime and summarize the architecture."
- "Compare the functions in `parser.rs` with the API docs open in Chrome."
- "Extract the error log from the terminal, search the repository for where it is thrown, and draft a patch."

### 3.2 Intelligent-Path Targets & Constraints
- **Target First Action Initiation:** Prompt initiation, with first token/tool action beginning within **~1.0 second for a warm, simple request** where feasible.
- **Realistic Constraint:** Total reasoning, multi-turn tool calling, and cross-application synthesis will take multiple seconds depending on model depth and network conditions. We do not promise completion within 1 second; we target **prompt commencement and first action acknowledgment within 1 second**.

---

## 4. DETAILED LATENCY BREAKDOWN & SUBSYSTEM TIMINGS

To eliminate guesswork and distinguish empirical measurements from targets, the following table details the latency profile across every link in the execution chain:

```
+-------------------------------------------------------------------------------------------------------------------+
| PIPELINE STAGE                       | MECHANISM                          | COLD START      | WARM START (p50/p95)|
+--------------------------------------+------------------------------------+-----------------+---------------------+
| 1. Intent Classification             | Trie / Regex / Tokenizer           | < 5 ms          | 0.5 ms / 1.5 ms     |
| 2. Direct Win32 Dispatch (Fast Path) | ShellExecuteExW / Win32 API        | 15 - 45 ms      | 5 ms / 12 ms        |
| 3. Chrome Native Messaging Roundtrip | Stdin/Stdout Length-Prefixed JSON  | 40 - 90 ms      | 8 ms / 18 ms        |
| 4. Chrome Content Script Execution   | chrome.scripting DOM query         | 25 - 60 ms      | 6 ms / 15 ms        |
| ------------------------------------ | ---------------------------------- | --------------- | ------------------- |
| **FAST PATH END-TO-END VISIBLE**     | **User Input -> Browser Action**   | **60 - 140 ms** | **18 ms / 35 ms**   |
| ------------------------------------ | ---------------------------------- | --------------- | ------------------- |
| 5. OpenShell Sandbox Initialization  | WSL2 container (crun / podman)     | 1,500 - 3,000 ms| 80 ms / 250 ms      |
| 6. OpenShell Policy Evaluation       | Supervisor OPA / Rules Check       | 10 - 25 ms      | 2 ms / 5 ms         |
| 7. MCP Tool Discovery (Initial)      | JSON-RPC tools/list                | 35 - 80 ms      | 4 ms / 10 ms (cached)|
| 8. MCP Tool Invocation Roundtrip     | JSON-RPC tools/call over localhost | 20 - 50 ms      | 6 ms / 14 ms        |
| 9. LLM First Token (Cloud API)       | HTTPS POST (Gemini / Claude / GPT) | 600 - 1,200 ms  | 350 ms / 750 ms     |
| 10. LLM First Token (Local RTX 5050) | TensorRT-LLM / ONNX DirectML       | 1,200 - 2,500 ms| 80 ms / 160 ms      |
| ------------------------------------ | ---------------------------------- | --------------- | ------------------- |
| **INTELLIGENT PATH TIME TO ACTION**  | **User Input -> First Tool Call**  | **2.5s - 5.0s**  | **650 ms / 1.2s**   |
+-------------------------------------------------------------------------------------------------------------------+
```

> **Data Disclosure Note:** Timings marked above represent empirical platform measurements from native Win32/WSL2 and standard API baselines on comparable hardware, combined with targeted engineering budgets. They must be validated during Milestone 1 benchmarking.

---

## 5. COLD-START VS. WARM-START MITIGATION STRATEGIES

### 5.1 Subsystem Cold-Start Costs
1. **WSL 2 Virtual Machine Boot:** Initial launch of WSL 2 from a stopped state incurs a **1.5s–3.0s** VM spin-up penalty.
2. **Chrome Extension Service Worker Wakeup:** Manifest V3 extension service workers terminate after ~30 seconds of inactivity. Waking up an idle service worker adds **30ms–80ms**.
3. **Local GPU Model Loading:** Initializing TensorRT-LLM or ONNX model weights from NVMe into RTX 5050 VRAM requires **1.2s–2.5s**.

### 5.2 Architectural Mitigations
- **Persistent Native Port:** Keeping the Chrome Native Messaging port connected via `chrome.runtime.connectNative()` prevents Chrome from terminating the service worker, keeping browser tools permanently warm.
- **WSL 2 Pre-Warming:** During user logon in Session 1, `aice_host.exe` issues a background non-blocking ping to the OpenShell Gateway in WSL 2, warming the Linux VM and container pools while the user begins their desktop session.
- **Model VRAM Pinning:** Small local classification and routing models (AMD Ryzen NPU or RTX 5050) remain resident in memory during active user sessions, unloading only when the Resource Governor detects full-screen gaming.

---

## 6. BENCHMARK METHODOLOGY & MEASUREMENT SPECIFICATION

To ensure no performance claims are accepted without rigorous empirical verification, AICE establishes a formal benchmarking protocol:

```
+===================================================================================+
|                          AICE LATENCY BENCHMARK HARNESS                           |
|                                                                                   |
|  1. High-Resolution Hardware Timestamping                                         |
|     - Uses Win32 QueryPerformanceCounter (QPC) with sub-microsecond precision    |
|                                                                                   |
|  2. Standard Test Matrix (100 Iterations per Scenario)                            |
|     - Scenario A: Fast-Path Window Focus (QPC Start -> SetForegroundWindow OK)    |
|     - Scenario B: Fast-Path Tab Switch (QPC Start -> Chrome Active Tab Changed)   |
|     - Scenario C: Native Messaging DOM Read (QPC Start -> Content Received)       |
|     - Scenario D: OpenShell MCP Tool Call (QPC Start -> Sandbox Receives Output)  |
|                                                                                   |
|  3. Metric Aggregation                                                            |
|     - Minimum, Maximum, Arithmetic Mean                                          |
|     - p50 (Median), p90, p95, p99 Percentile Distributions                        |
|                                                                                   |
|  4. Resource Correlation                                                          |
|     - Concurrent CPU utilization, Working Set RAM, and GPU VRAM logged via ETW   |
+===================================================================================+
```

### 6.1 Formal Acceptance Benchmarks
Before declaring the integration substrate ready:
1. **Fast-Path Tab Switch:** p95 must be `< 50 ms` on warm Chrome sessions.
2. **Local Native Messaging Roundtrip:** p95 must be `< 20 ms` for 10 KB payload exchanges.
3. **OpenShell MCP Tool Invocation:** p95 must be `< 25 ms` across the WSL 2 / Windows localhost boundary (excluding model inference time).
