# 03 — SECURITY & DEFENSIVE ENGINEERING REVIEW
## Comprehensive Forensic Security Audit: Authorization Lattice, Win32 Job Objects, SSRF Mitigation, and Containment Boundaries

**Document ID:** PRESENCE-CAPSTONE-AUDIT-03  
**Audit Phase:** Phase 2 — Independent Architectural Investigations (Subagent B)  
**Target:** `runtime.permissions`, `runtime.process_manager`, `runtime.search.ssrf`, `runtime.mcp`, `runtime.artifacts`  
**Date:** 2026-11-20  
**Status:** Complete  

---

## 1. Executive Summary

Subagent B conducted an adversarial security and defensive engineering review of Presence. The audit assessed the system's defenses against authorization bypass, privilege escalation, process escapement, remote command injection, Server-Side Request Forgery (SSRF), secret leakage, and cross-conversation artifact tampering.

### Security Posture Verdict: **HARDENED DEFENSIVE ARCHITECTURE**
The Presence implementation demonstrates robust, defense-in-depth principles:
- **Zero-Tolerance Process Containment:** All child processes are assigned to Win32 Job Objects before execution begins via suspended creation (`CREATE_SUSPENDED`), guaranteeing that subprocesses cannot orphan or escape kernel termination.
- **Socket-Level SSRF Protection:** Outgoing HTTP traffic is inspected at both the pre-connect DNS resolution and the post-connect socket level (`sock.getpeername()[0]`), effectively mitigating DNS rebinding (TOCTOU) attacks.
- **Strict Environment Sanitization:** Child processes have all AI provider credentials (`GEMINI_API_KEY`, `FREELLMAPI_API_KEY`, etc.) completely stripped before execution.
- **Permission Tier Segregation:** Mutating plan cancellation (`presence.cancel_plan`) requires `L2_EXECUTE_REVERSIBLE`, while read-only inspection (`presence.manage_plan`) is strictly locked to `L0_OBSERVE`.

---

## 2. In-Depth Defense-in-Depth Analysis

### 2.1 Permission Boundary & Authority Lattice (`runtime/permissions.py`)
Presence establishes four explicit permission tiers:
```text
L0_OBSERVE (Read-only, zero side-effects)
     ▲
L1_RECOMMEND (Advisory, plan drafting)
     ▲
L2_EXECUTE_REVERSIBLE (File writes, process spawning, subagents, plan cancellation)
     ▲
L3_PROTECTED (Irreversible file deletion, external process termination)
```
- **Session Ceiling Enforcement:** Every `AgentSession` holds an active authority ceiling. Any tool whose declaration tier exceeds the active ceiling is rejected with `PermissionDeniedError` before invocation.
- **Subagent Tier Inheritance:** Child subagents inherit an authority ceiling that is strictly less than or equal to their parent session. A subagent cannot be granted `L3_PROTECTED` by a parent running at `L2_EXECUTE_REVERSIBLE`.

### 2.2 Win32 Job Object Containment (`runtime/process_manager/`)
- **Suspended Process Creation:** Child processes are spawned using `Win32SuspendedLauncher.launch()` with the Win32 flag `CREATE_SUSPENDED` (0x00000004).
- **Kernel-Level Termination Guarantee:** The process PID is explicitly assigned to a Windows Job Object configured with:
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
  Only after the job assignment succeeds is the main thread resumed via `ResumeThread()`.
- **Orphan Prevention:** If Presence is abruptly killed (e.g., via Task Manager or OS power failure), the Windows kernel automatically closes the Job Object handle, immediately terminating all child processes and process trees without leaving zombie processes.

### 2.3 SSRF & DNS Rebinding Defenses (`runtime/search/ssrf.py`)
Presence enforces multi-layered network destination validation:
1. **Scheme Validation:** Only `http://` and `https://` are permitted. Schemes like `file://`, `gopher://`, `dict://`, `data:` are rejected.
2. **Userinfo Rejection:** Rejects credentials embedded in URLs (`http://user:pass@host`) to prevent URL parser confusion attacks.
3. **Disallowed Subnets & Encoded Formats:** Blocks:
   - Loopback (`127.0.0.0/8`, `::1`)
   - Private subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
   - Link-local and Cloud metadata (`169.254.169.254`, `fe80::/10`)
   - Carrier-grade NAT (`100.64.0.0/10`)
   - 6to4 relay (`2002::/16`)
   - Integer, hexadecimal, and octal-encoded IP representations (e.g. `2130706433`, `0x7f000001`).
4. **Socket-Level Peer IP Inspection:**
   `SSRFSafeBackend` intercepts `httpcore` network socket connections and verifies `sock.getpeername()[0]` after TCP handshake completion. If a DNS server returns a public IP during resolution but redirects to an internal IP upon connection, the socket is immediately terminated with `ConnectError`.

### 2.4 Credential & Secret Scrubbing
- **Process Environment Sanitization:** `ProcessManager._sanitize_env` explicitly purges all sensitive keys (`GEMINI_API_KEY`, `FREELLMAPI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `PRESENCE_SECRET_KEY`) from the environment dictionary passed to child processes.
- **Log and Journal Sanitization:** The event journal and trajectory store store tool payloads without dumping raw environment variables or process secrets.

### 2.5 Artifact Store Filesystem Isolation (`runtime/artifacts/store.py`)
- **Path Traversal Guards:** Artifacts are addressed via UUIDs (`art_<hex>`) and stored under `artifacts/<conversation_id>/`.
- Any attempt to access paths with `..` or relative directory traversal is rejected.
- Offloaded outputs are stored as read-only `.bin` or `.json` files, preventing script execution within the storage repository.

---

## 3. Potential Security Enhancements & Observations

1. **PowerShell ExecutionPolicy Bypass:**
   In `ProcessManager.run_command`, PowerShell commands are executed with `-ExecutionPolicy Bypass`. While required to run administrative and companion automation scripts on Windows 11 systems with restricted script policies, it relies entirely on the `PermissionBoundary` to prevent untrusted script execution.
2. **Tool Output Content Sanitization:**
   While tool inputs are checked for authority levels, large web fetch results or command outputs returned from external sources are offloaded directly to `ArtifactStore`. If an external webpage contains prompt injection attacks, the content is offloaded safely to disk, but preview snippets re-injected into subsequent prompt assemblies should continue to be treated as untrusted data.

---

## 4. Subagent B Verdict & Sign-Off

The security architecture of Presence is **exceptionally robust**. Containment mechanisms, SSRF defenses, and permission segregation satisfy strict enterprise desktop security standards.
