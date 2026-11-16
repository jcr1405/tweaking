# Deep Reverse-Engineering Report: Antigravity Agent Runtime

**Forensic Target:** `C:\Users\chait\AppData\Local\Programs\antigravity\Antigravity.exe`  
**Investigation Tools:** REA MCP (`inspect_desktop_application`, `inspect_pe_imports`, `inspect_electron_fuses`, `inspect_pe_load_config`, `inspect_pe_certificates`), Windows CIM/WMI Process Telemetry, Windows WinRT diagnostics.  
**Date of Forensic Capture:** November 15, 2026  

---

## 1. Executive Summary
Antigravity is an industrial-grade, multi-process desktop agent platform. Rather than a monolithic single script, it combines a heavily hardened, 64-bit native PE32+ host binary (246.5 MB), an Electron/Chromium multi-process IPC mesh utilizing Chromium Mojo channels, and a modular Node.js plugin/sidecar execution engine running via custom loader hooks.

---

## 2. Native PE32+ Host Binary Anatomy

REA MCP disassembled and verified the PE header structures:
* **Binary Size:** 246,599,680 bytes (~246.5 MB)
* **Architecture:** AMD64 / x86_64 (`machine: 34404`, 64-bit)
* **Format:** PE32-Plus
* **Import Directory RVA:** `237965556` (Size: 160 bytes)
* **Delay-Import Directory RVA:** `237715712` (Size: 1,696 bytes)

### 2.1 Security Mitigations & Load Configuration (`inspect_pe_load_config`)
REA's probe of `IMAGE_DIRECTORY_ENTRY_LOAD_CONFIG` exposed advanced compiler-level hardening:
* **Control Flow Guard (CFG):** Fully instrumented (`IMAGE_GUARD_CF_INSTRUMENTED`).
  * Guard Function Table Address: `0x14e1e9605` (RVA `236885509`)
  * **Instrumented Function Count:** **207,240 functions** guarded against call-target hijacking.
  * Long Jump Target Table: 91 entries.
  * Extended Flow Guard (XFG): Check function pointer active at `0x14e2b3de0`.
* **Security Cookie (Stack Canaries):** Initialized at virtual address `0x14e33c040` in `.data` section to prevent stack buffer overflows.
* **Thread Local Storage (TLS) Callbacks:** 6 native initialization callbacks registered across `.text` sections (`0x140ab9460`, `0x143125550`, `0x14147efc0`, etc.) executing before the main entry point to set up thread-isolated runtime state.

---

## 3. Packaged Electron Security Fuses (`inspect_electron_fuses`)

REA MCP inspected the raw fuse sentinel bytes at offset `199556088` (Wire version 1, 9 fuses declared):

| Fuse Index | Fuse Name | Observed State | Runtime Implication |
| :--- | :--- | :--- | :--- |
| **0** | `RunAsNode` | **Enabled** (`49`) | Allows the executable to act as a pure Node.js runtime when `ELECTRON_RUN_AS_NODE=1` is passed. |
| **1** | `EnableCookieEncryption` | **Disabled** (`48`) | Cookie store does not use Windows DPAPI encryption. |
| **2** | `EnableNodeOptionsEnvironmentVariable` | **Enabled** (`49`) | Respects `NODE_OPTIONS` environment flags. |
| **3** | `EnableNodeCliInspectArguments` | **Enabled** (`49`) | Allows `--inspect` and `--inspect-brk` remote debugging flags. |
| **4** | `EnableEmbeddedAsarIntegrityValidation` | **Disabled** (`48`) | ASAR file signatures are not enforced at runtime. |
| **5** | `OnlyLoadAppFromAsar` | **Disabled** (`48`) | Engine is permitted to load standalone `.js`/`.mjs` scripts outside `app.asar`. |
| **6** | `LoadBrowserProcessSpecificV8Snapshot` | **Disabled** (`48`) | Uses standard unified V8 heap snapshot. |
| **7** | `GrantFileProtocolExtraPrivileges` | **Enabled** (`49`) | `file://` protocol has elevated local filesystem access. |
| **8** | `WasmTrapHandlers` | **Enabled** (`49`) | POSIX/Win32 signal trap handlers enabled for WebAssembly execution. |

**Key Architectural Insight:** Enabling `RunAsNode` and disabling `OnlyLoadAppFromAsar` is what allows Antigravity to spawn external sidecars and plugins using its own binary without bundling a separate `node.exe`!

---

## 4. Live Multi-Process Hierarchy & IPC Architecture

Analysis of live running processes on the system revealed a multi-process architecture rooted at Parent PID `42476`:

```
[PID 42476] Antigravity.exe (Browser Main Process)
   │
   ├── [PID 6320]  --type=gpu-process (DirectX / D3D11 Acceleration)
   ├── [PID 4580]  --type=utility --utility-sub-type=network.mojom.NetworkService
   ├── [PID 8144]  --type=renderer (UI Window - Client ID 11)
   ├── [PID 16336] --type=renderer (UI Window - Client ID 12)
   ├── [PID 32812] --type=renderer (UI Window - Client ID 13)
   │
   └── Sidecar Runtime Engine (Spawned via RunAsNode):
       ├── [PID 17992] Antigravity.exe --import=sidecar_sdk/register_loader.js launch.mjs device-hub
       ├── [PID 13748] Antigravity.exe --import=sidecar_sdk/register_loader.js launch.mjs compose-preview
       └── [PID 33096] Antigravity.exe --import=sidecar_sdk/register_loader.js main.mjs
```

### 4.1 Custom Registered Chromium Protocols
The network service registers custom, isolated URL schemes:
* `plugin://` (CORS-enabled, fetch-enabled, service-worker-enabled, code-cache-enabled)
* `agy-favicon://` (Secure local asset protocol)

### 4.2 Inter-Process Communication (Mojo Platform Channels)
Every worker process is bonded to the main broker via native Windows handle inheritance:
* Example: `--mojo-platform-channel-handle=1948`, `--mojo-platform-channel-handle=4680`
* Eliminates network TCP overhead for internal subagent messaging.

### 4.3 Windows Display Scaling Telemetry
The live renderer telemetry exposed:
* `--device-scale-factor=1.25` (125% DPI scale active on the user's display).
* Explains why rigid pixel-coordinate mouse scripts fail on Windows, and why normalized `[0, 1000]` coordinates are mandatory.

---

## 5. Architectural Blueprint for Our Companion

Reverse-engineering Antigravity directly answers several open questions from Section 11 of your specification:

1. **How to execute sidecars without bloat:** We do not need to install Node.js, Python, and separate runtimes. We can design our companion's main process to spawn background worker tasks using Python's `multiprocessing` or native subprocess pipes.
2. **Crash Resilience:** The separation between the **UI Renderer** and the **Network/Agent Utility** ensures that even if a visual window stutters or closes, the background companion daemon (`NetworkService` / Brain) never dies.
3. **Hardware Acceleration:** By passing `--type=gpu-process` to dedicated rendering, the companion's floating UI won't interfere with 3D game rendering on the RTX 5050.
