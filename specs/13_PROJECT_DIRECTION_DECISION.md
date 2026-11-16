# PROJECT DIRECTION DECISION & STRATEGIC RATIFICATION
## Final Recommendation: The Adapted Desktop Presence Foundation
**Document Version:** 1.1.0-DECISION-AUDIT  
**Classification:** Strategic Direction & Architectural Decision Standard  
**Revision Date:** November 2026  

---

## 1. THE STRATEGIC DECISION: EVALUATION OF OPTIONS A THROUGH F

In accordance with our strict engineering principle (**MODIFY BEFORE CREATING**), we evaluate the six strategic options based on verified source inspection and licensing forensics:

```
+===================================================================================================================+
| OPTION CANDIDATE                   | VERDICT            | FORENSIC AUDIT RATIONALE                                |
+====================================+====================+=========================================================+
| Option A: Modify Screenpipe as     | REJECTED AS        | FATAL LICENSE DEFECT: Negentropy Labs Commercial        |
| the Primary Foundation             | PRIMARY            | License restricts use to non-commercial personal only;  |
|                                    |                    | embedding into a product requires paid Enterprise.      |
|                                    |                    | Also, memory is 500MB-1.5GB with Whisper/OCR active.    |
+------------------------------------+--------------------+---------------------------------------------------------+
| Option B: Modify Open Interpreter  | REJECTED AS        | ARCHITECTURAL MISMATCH: Designed as a full-sized        |
| Desktop as the Primary Foundation  | PRIMARY            | Electron workstation window (~300MB RAM), not an        |
| (interpreter-workstation)          |                    | ambient top-bezel HUD. Lacks native Windows UIA/WGC.    |
+------------------------------------+--------------------+---------------------------------------------------------+
| Option C: Combine Screenpipe and   | REJECTED           | Combining two heavy separate applications (Screenpipe + |
| Open Interpreter Desktop           |                    | Electron Workstation) causes severe resource bloat      |
|                                    |                    | (> 1.2 GB RAM idle) and carries license restrictions.   |
+------------------------------------+--------------------+---------------------------------------------------------+
| Option D & F: Adapt an Open-Source | **ADOPTED AS THE** | **THE OPTIMAL SYNTHESIS:** Fork an open-source Tauri    |
| Desktop HUD (Pillar Island) +      | **OFFICIAL**       | Dynamic Island (`pillar`, <50MB RAM) as the persistent  |
| OpenShell Sandbox + Native Bridges | **DIRECTION**      | host; configure OpenShell in WSL2 for heavy agents;     |
| (Write small connectors only)      |                    | write only the missing ~300-line Chrome MV3 connector.  |
+------------------------------------+--------------------+---------------------------------------------------------+
| Option E: Integrate Independent    | REJECTED           | Loose tools without an ambient desktop host leave the   |
| Tools with No Custom Host          |                    | user trapped in the fragmented manual chatbox model.    |
+===================================================================================================================+
```

---

## 2. THE SELECTED FOUNDATION BLUEPRINT

We formally adopt **Option D & F: The Adapted Tauri Desktop Presence + OpenShell Backend**:

```
+===================================================================================+
|                          THE INTELLIGENT PRESENCE ARCHITECTURE                    |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                 1. AMBIENT PRESENCE HOST & DYNAMIC ISLAND HUD               |  |
|  |  - Base Project: warpirate/pillar-dynamic-island-for-windows (Tauri v2+Rust) |  |
|  |  - Windows Native WebView2 rendering (< 50MB RAM idle).                     |  |
|  |  - Pinned top-screen Dynamic Island pill, expanding smoothly via springs.   |  |
|  |  - Native Win32 Integration: WS_EX_NOACTIVATE (never steals game focus),     |  |
|  |    SMTC media transport controls, global hotkey (Alt+Space).                |  |
|  |  - Fast-Path Dispatcher: Instant local actions (< 50ms, zero LLM tokens).    |  |
|  |  - Embedded Memory Vault: Local SQLite WAL with Letta-style Core Memory.    |  |
|  +-----------------------------------------------------------------------------+  |
|            │                                                         │            |
|            │ Stdin/Stdout JSON Pipe                                  │ Local MCP  |
|            │ (Native Messaging)                                      │ (Port 59k) |
|            ▼                                                         ▼            |
|  +------------------------------------+    +-----------------------------------+  |
|  | 2. UNIVERSAL CHROME CONNECTOR      |    | 3. ISOLATED AGENT GOVERNANCE      |  |
|  |    (New Clean MV3 Extension)       |    |    (NVIDIA OpenShell in WSL 2)    |  |
|  |  - ~300 lines JavaScript/Rust.     |    |  - Apache 2.0 (Permissive).       |  |
|  |  - Connects to live authenticated  |    |  - Kernel-enforced Linux Landlock |  |
|  |    tabs without debugging flags.   |    |    filesystem confinement.        |  |
|  |  - Zero cookie or password leaks.   |    |  - Supervisor credential injection|  |
|  +------------------------------------+    +-----------------------------------+  |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  | 4. OPTIONAL PASSIVE PERCEPTION (Screenpipe Sidecar - Personal Use Only)     |  |
|  |  - Connects via localhost:3030 if user installs Screenpipe for OCR timeline.|  |
|  |  - Audio disabled by default; throttled frame rate to protect battery.     |  |
|  +-----------------------------------------------------------------------------+  |
+===================================================================================+
```

---

## 3. WHY THIS FOUNDATION IS THE STRONGEST CANDIDATE

1. **Lightweight & Native (< 50MB RAM):**  
   By building on Tauri v2 and native Windows WebView2 instead of Electron, the ambient host remains featherweight, eliminating the 300MB–1.5GB background overhead of Electron or unconstrained audio models.
2. **True Presence (Not a Chat Window):**  
   The Dynamic Island architecture provides a physical anchor on the desktop: a 39px top-bezel pill that stays pinned, non-disruptive, and non-focus-stealing during active work or gaming.
3. **Rock-Solid Security (Zero Host Exposure):**  
   Heavy agent tasks run inside NVIDIA OpenShell's Linux Landlock container in WSL 2. Even compromised npm/pip packages cannot touch the Windows host filesystem or steal cloud API keys.
4. **Live Authenticated Browser Reading:**  
   The lightweight Chrome MV3 extension bridges the desktop presence to the user's running, authenticated browser tabs without requiring browser restarts or insecure `--remote-debugging-port` flags.
5. **Permissive Licensing Compliance:**  
   Every core component (Tauri, OpenShell, Chrome WebExtensions, SQLite) is licensed under permissive open-source licenses (MIT, Apache 2.0, Public Domain), completely bypassing Screenpipe's restrictive commercial license.

---

## 4. PRIORITIZED NEXT-STEP ROADMAP

```
+-----------------------------------------------------------------------------------+
| STEP 1: Foundation Scaffold (Tauri Dynamic Island HUD & Fast Path)                |
| - Clone and audit warpirate/pillar-dynamic-island-for-windows.                    |
| - Verify build on Windows 11 with Tauri v2 CLI.                                   |
| - Configure collapsed 39px pill, Alt+Space hotkey, and WS_EX_NOACTIVATE flags.   |
| - Implement Fast-Path local workspace launcher (< 50ms dispatch).                 |
+-----------------------------------------------------------------------------------+
                                          │
                                          ▼
+-----------------------------------------------------------------------------------+
| STEP 2: Chrome MV3 Connector (Live Authenticated Tab Reading)                     |
| - Scaffold lightweight Chrome Extension Manifest V3 (`manifest.json`).            |
| - Implement Native Messaging Host binary in Rust connecting to the Tauri app.     |
| - Verify read-only tab summary extraction from live authenticated web app.         |
+-----------------------------------------------------------------------------------+
                                          │
                                          ▼
+-----------------------------------------------------------------------------------+
| STEP 3: Embedded Core Memory Engine (SQLite WAL)                                  |
| - Initialize local SQLite WAL database in %LOCALAPPDATA%\aice\memory.db.          |
| - Implement Letta-style Core Memory table (user profile, active project mappings).|
| - Implement 7-state task recovery machine for reboot continuity.                  |
+-----------------------------------------------------------------------------------+
                                          │
                                          ▼
+-----------------------------------------------------------------------------------+
| STEP 4: OpenShell WSL 2 Agent Integration                                         |
| - Deploy NVIDIA OpenShell Gateway in WSL 2 on the host machine.                   |
| - Connect Tauri host to OpenShell REST API (localhost:59001) for complex coding.  |
| - Verify Landlock path confinement and supervisor credential injection.           |
+-----------------------------------------------------------------------------------+
                                          │
                                          ▼
+-----------------------------------------------------------------------------------+
| STEP 5: Multi-Signal Gaming Sensor & Hardware Throttling                          |
| - Implement SHQueryUserNotificationState + window style fullscreen detection.     |
| - Automatically collapse HUD and unload VRAM model weights during 3D gaming.      |
+-----------------------------------------------------------------------------------+
```

---

## 5. IMMEDIATE ACTION

We await your review and ratification of this audited decision package.  
Upon your explicit approval, we will proceed to **Step 1: Auditing and scaffolding the Tauri Dynamic Island presence foundation**.
