# NVIDIA OPENSHELL INTEGRATION STUDY
## Agent Execution, Sandboxing, and Windows Host Governance
**Document Version:** 1.0.0-STRAT-SPEC  
**Classification:** Subsystem Integration Analysis & Feasibility Study  
**Target Platform:** Windows 11 x64 (AMD Ryzen AI / Discrete NVIDIA RTX GPU) with WSL 2 and Docker Desktop  

---

## 1. EXECUTIVE SUMMARY & INVESTIGATION OBJECTIVE

The original architectural baseline for AICE (as specified in `specs/01` through `specs/04`) assumed that our runtime must independently implement every layer of the agent computing stack: Windows Service bootstrapping, Win32 Job Object process supervision, subprocess isolation, custom credential proxies, and task lifecycle management.

This investigation evaluates **NVIDIA OpenShell** (`github.com/NVIDIA/OpenShell`), an open-source (Apache 2.0) agent execution, sandboxing, and governance runtime. 

### Core Verdict:
1. **Adopt OpenShell as the Agent Execution and Sandboxing Backend:** OpenShell provides a production-grade, policy-enforced isolation runtime for autonomous agents (Claude Code, LangChain Deep Agents, Copilot CLI, custom agents) with declarative YAML policies, kernel-level confinement, and supervisor-based credential injection. Independently rebuilding this infrastructure in AICE is redundant and technically unjustified.
2. **Recognize OpenShell's Hard Scope Boundaries:** OpenShell is strictly a **Linux-based workload sandbox and network governance engine**. It possesses **zero Windows desktop automation**, zero Windows UI Automation (UIA) awareness, zero screen capture capabilities, and zero native integration with running Windows GUI applications (e.g. Chrome, VS Code, Discord, Spotify).
3. **The Division of Labor:** OpenShell governs the *untrusted agent execution environment* inside Linux containers / microVMs. AICE acts as the *Windows Ambient Coordinator and Universal Application Integration Layer* on the host, exposing controlled, application-specific interfaces across the host boundary.

---

## 2. OPENSHELL ARCHITECTURE & CORE CAPABILITIES

```
+===================================================================================+
|                              HOST ENVIRONMENT (WSL2 / LINUX)                      |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                        OPENSHELL GATEWAY (Control Plane)                    |  |
|  |  - Sandbox Lifecycle Management (Create, Start, Pause, Destroy)              |  |
|  |  - Declarative Policy Repository (YAML Configuration)                       |  |
|  |  - Client Authentication & Scoped Bearer Token Issuance                     |  |
|  +-----------------------------------------------------------------------------+  |
|                                         |                                         |
|                                         v (Policy & Routing Enforcement)          |
|  +-----------------------------------------------------------------------------+  |
|  |                      OPENSHELL SUPERVISOR (Trust Broker)                    |  |
|  |  - Operates Outside the Agent Sandbox Boundary                              |  |
|  |  - Network Proxy & Egress Filtering (DNS Resolution & TCP Interception)     |  |
|  |  - OPA (Open Policy Agent) Rules Engine & Header Injection                  |  |
|  |  - Credential Vault & Placeholder Substitution (Keys Never Enter Sandbox)   |  |
|  |  - Payload Inspection & Secret Redaction Middleware                         |  |
|  +-----------------------------------------------------------------------------+  |
|                                         |                                         |
|                        Isolated Bridge / UNIX Socket (Protected)                 |
|                                         |                                         |
|  +-----------------------------------------------------------------------------+  |
|  |                      AGENT SANDBOX (Untrusted Workload)                     |  |
|  |  - Linux Landlock: Kernel-Enforced Filesystem Path Confinement             |  |
|  |  - Seccomp-BPF: System Call Whitelisting & Filtering                        |  |
|  |  - Network Namespaces: Zero Raw Egress (All Traffic Forced to Supervisor)   |  |
|  |  - Autonomous Agent Framework (Claude Code, LangChain, Custom CLI)          |  |
|  |  - Ephemeral / Volume-Mounted Project Workspace                            |  |
|  +-----------------------------------------------------------------------------+  |
+===================================================================================+
```

### 2.1 The Three-Tier Architectural Model
1. **The Gateway (Control Plane):**  
   The management interface that coordinates sandboxes across compute drivers (Docker, Podman, Kubernetes, or microVMs). It validates user requests, provisions sandboxes, manages state, and compiles declarative policies into active runtime constraints.
2. **The Supervisor (Trust Broker):**  
   Runs in the host domain completely isolated from the agent. The supervisor acts as a mandatory intermediary for all external actions. When an agent attempts an outbound API call or tool execution, the request is intercepted by the supervisor, evaluated against declarative policies, injected with authentic credentials (if authorized), and relayed to the destination. Raw API keys (OpenAI, Gemini, Anthropic) never enter the sandbox memory.
3. **The Sandbox (Untrusted Execution Domain):**  
   The restricted environment where the agent executes. OpenShell confines the agent process using modern Linux kernel security primitives:
   - **Linux Landlock:** Programmatic access control restricting filesystem operations strictly to declared directory subtrees. Even processes running with root UID inside the container cannot bypass Landlock path restrictions.
   - **Seccomp-BPF:** Berkeley Packet Filter system call filters that block dangerous kernel calls (`ptrace`, kernel keyring manipulation, raw socket creation).
   - **Network Namespaces:** An outer network fence isolating the container network stack. Direct egress to the internet is completely blocked; traffic must route through the supervisor's local proxy port.

---

## 3. COMPARATIVE ANALYSIS: OPENSHELL CAPABILITIES VS. AICE REQUIREMENTS

To establish exact architectural boundaries, the capabilities required by an ambient personal computing environment are evaluated against OpenShell's direct capabilities:

| Subsystem / Capability | OpenShell Direct Capability | Requires Integration / Adapter | Outside OpenShell Scope | Architectural Evaluation |
| :--- | :---: | :---: | :---: | :--- |
| **Agent Sandbox Lifecycle** | **VERIFIED** | — | — | Fully provided by OpenShell Gateway (Create, pause, snapshot, kill). Replaces custom AICE Job Object supervisor for agents. |
| **Filesystem Path Policies** | **VERIFIED** | — | — | Provided via Linux Landlock. Strictly confines agents to designated project roots. |
| **Network Egress Filtering** | **VERIFIED** | — | — | Intercepted by Supervisor with OPA policy checks and domain whitelisting. |
| **Credential Injection** | **VERIFIED** | — | — | Supervisor injects actual authorization headers dynamically; agent only sees placeholders. |
| **Model / Framework Independence** | **VERIFIED** | — | — | Framework-agnostic. Runs Claude Code, LangChain Deep Agents, Copilot CLI, or custom Python/Rust agents. |
| **Desktop Application Integration** | — | — | **OUT OF SCOPE** | OpenShell has no concept of Windows desktop apps (Chrome, VS Code, Discord). Requires AICE Universal Connectors. |
| **Windows OS Awareness** | — | — | **OUT OF SCOPE** | OpenShell runs in a Linux container; it cannot inspect Win32 window titles, UIA trees, or DirectX full screen. |
| **Live Authenticated Browser Session** | — | **REQUIRES ADAPTER** | — | OpenShell cannot touch user Chrome profiles directly. Requires AICE Native Messaging Bridge + MCP. |
| **Fast-Path Local Dispatch (<100ms)** | — | — | **OUT OF SCOPE** | OpenShell is an agent governance sandbox, not a low-latency UI command router. Fast path must run on Windows host. |
| **Ambient Presence / Boot Persistence** | — | **REQUIRES ADAPTER** | — | Requires Windows Service / WSL2 background daemon persistence. |

---

## 4. WINDOWS DEPLOYMENT FEASIBILITY (WSL 2 & DOCKER DESKTOP)

Because OpenShell relies on Linux kernel security primitives (Landlock LSM, Linux Network Namespaces, Seccomp-BPF), it cannot execute as a native Win32 process. On Windows 11, it must run inside **Windows Subsystem for Linux (WSL 2)** or **Docker Desktop (WSL 2 backend)**.

### 4.1 Deployment Architecture on Windows 11

```
+===================================================================================+
|                             WINDOWS 11 HOST (Session 1)                           |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                        AICE Windows Ambient Coordinator                     |  |
|  |  - Native Win32 Background Host (aice_host.exe)                             |  |
|  |  - Fast-Path Dispatcher (< 100ms local execution)                           |  |
|  |  - Universal Application Connectors (Chrome Extension, Native Messaging)   |  |
|  |  - Local MCP Server Infrastructure (stdio / HTTP)                           |  |
|  |  - Resource Governor (DirectX Gaming & Thermal Sensor)                      |  |
|  +-----------------------------------------------------------------------------+  |
|                                         |                                         |
|                     WSL2 Virtual Network / localhost port forward                |
|                     (or Hyper-V vsock / UNIX domain socket via Named Pipe)        |
|                                         |                                         |
+===================================================================================+
|                               WSL 2 LINUX VM (Ubuntu 24.04 LTS)                   |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                        NVIDIA OpenShell Runtime                             |  |
|  |  - OpenShell Gateway daemon                                                 |  |
|  |  - OpenShell Supervisor proxy                                               |  |
|  |  +-----------------------------------------------------------------------+  |  |
|  |  | OpenShell Agent Sandbox (Docker / Podman / crun)                       |  |  |
|  |  | - Agent (Claude Code / LangChain)                                     |  |  |
|  |  | - Filesystem: Native ext4 virtual disk (high IOPS)                   |  |  |
|  |  | - Landlock & Seccomp enforced                                         |  |  |
|  |  +-----------------------------------------------------------------------+  |  |
|  +-----------------------------------------------------------------------------+  |
+===================================================================================+
```

### 4.2 Technical Realities & Constraints of WSL 2 Deployment
1. **Filesystem Performance (9P vs. ext4):**
   - Accessing Windows host files from WSL 2 via `/mnt/c/` utilizes the Plan 9 (9P) filesystem protocol, which exhibits severe I/O latency (5x–20x slower than native ext4) for deep file trees, git operations, and node_modules.
   - **Architectural Mandate:** Agent project workspaces must reside inside native WSL 2 ext4 storage (`\\wsl$\Ubuntu\home\...`) or synchronize with the host via structured diffs, rather than executing direct high-frequency file operations across `/mnt/c/`.
2. **Networking & Localhost Bridging:**
   - In modern Windows 11 (WSL 2 release 2.0+), Mirrored Networking (`networkingMode=mirrored` in `.wslconfig`) enables seamless `localhost` sharing between Windows host applications and WSL 2 processes.
   - This allows an MCP client running inside an OpenShell sandbox in WSL 2 to connect directly to an MCP server hosted on the Windows host at `localhost:<port>` with sub-millisecond network latency.
3. **Resource Overhead & Cold Starts:**
   - WSL 2 virtual machine initialization requires 1.5s–3.0s on cold boot. Once running, the lightweight Linux VM consumes ~400MB–800MB RAM.
   - OpenShell sandboxes provisioned via lightweight containers (`crun` or `podman`) spin up within 200ms–500ms once WSL 2 is warm.
4. **GPU / NPU Acceleration:**
   - NVIDIA GPUs are fully accessible inside WSL 2 via the NVIDIA Container Toolkit and CUDA on WSL driver. Local quantized models (RTX 5050) can execute inside WSL 2 with zero virtualization penalty.

---

## 5. REUSE VS. INDEPENDENT IMPLEMENTATION MATRIX

Based on this forensic evaluation, we formally partition the system responsibilities:

```
+-----------------------------------------------------------------------------------+
| REUSE FROM OPENSHELL                      | INDEPENDENTLY IMPLEMENT IN AICE       |
+-------------------------------------------+---------------------------------------+
| 1. Agent Sandbox Creation & Teardown      | 1. Windows Host Ambient Coordinator   |
| 2. Linux Landlock Filesystem Confinement  | 2. Fast-Path Deterministic Dispatcher |
| 3. Network Egress Filtering & OPA Policies| 3. Universal Application Connectors   |
| 4. Supervisor Credential Injection        | 4. Chrome Extension Native Bridge     |
| 5. Process Seccomp Sandboxing             | 5. Windows Resource Governor (Gaming) |
| 6. Framework-Agnostic Agent Harnessing    | 6. Windows UI Automation Fallback     |
+-----------------------------------------------------------------------------------+
```

### 5.1 What OpenShell Eliminates from AICE Scope
- **No need to write a custom agent sandbox:** OpenShell provides container/microVM creation and teardown out of the box.
- **No need to implement custom network proxying & secret scrubbing:** OpenShell Supervisor handles domain whitelisting, credential injection, and egress logging.
- **No need to implement custom container isolation on Windows:** OpenShell leverages mature Linux kernel mechanisms (Landlock, seccomp, namespaces) inside WSL 2, providing significantly stronger security than Win32 Job Objects alone can offer against compromised code.

### 5.2 What AICE Must Provide That OpenShell Cannot
- **The Windows Bridge:** Connecting the sandboxed agent to the user's active desktop applications (Chrome, VS Code, Discord).
- **The Fast Path:** An instant, deterministic local router (<100ms) that executes routine commands directly on Windows without waking up WSL 2 or invoking an LLM.
- **Desktop Context Perception:** Ambient awareness of active window focus, full-screen gaming state, and display configuration.

---

## 6. FEASIBILITY VERDICT & RECOMMENDATION

> **Strategic Directive:**  
> **Adopt NVIDIA OpenShell as the Agent Execution and Sandboxing Substrate.**  
> Cease all work on building a bespoke agent container/sandbox on Windows. Pivot AICE's architectural focus entirely to **Universal Application Integration, Windows Environment Coordination, and Fast-Path Dispatch**.
