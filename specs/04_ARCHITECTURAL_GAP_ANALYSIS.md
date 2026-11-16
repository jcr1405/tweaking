# ARCHITECTURAL GAP ANALYSIS & DEFENSIVE AUDIT
## Ambient Intelligent Computing Environment (AICE)
**Document Version:** 1.0.0-PROD-AUDIT  
**Classification:** Pre-Implementation Architectural Gap Analysis & Forensic Critique  
**Audited Documents:**  
- `specs/01_SYSTEM_CONSTITUTION.md` (v1.0.0)  
- `specs/02_SYSTEM_ARCHITECTURE.md` (v1.0.0)  
- `specs/03_DECISIONS_AND_MILESTONES.md` (v1.0.0)  
- Forensic Discovery Reports (`recon/reports/00` through `10`)  

---

## 1. EXECUTIVE SUMMARY & AUDIT OBJECTIVE

This document provides an unsparing, evidence-grounded forensic critique of the AICE architecture prior to the commencement of Milestone 1 implementation. In accordance with defensive engineering principles, this audit assumes that **no architectural claim is correct merely because it appears in a previous design or reverse-engineering report**.

The audit identifies:
- **5 Confirmed Defects:** Logical, cross-session, or API contradictions that will fail at runtime if built as specified.
- **5 Unverified Assumptions:** Performance, privacy, or security claims lacking empirical baseline measurements.
- **4 Architectural Trade-Offs:** Engineering compromises requiring explicit boundary definitions.
- **6 Required Safeguards:** Critical defensive mechanisms necessary to prevent privilege escalation, process leakage, or data corruption.
- **2 Accepted Limitations:** Inherent Windows platform constraints that the architecture must accommodate rather than attempt to bypass.

---

## 2. SYSTEMATIC INVESTIGATION ACROSS THE 12 FOCUS AREAS

```
+-----------------------------------------------------------------------------------------------------------------+
| ISSUE ID  | AREA / SUBSYSTEM                      | CLASSIFICATION         | STATUS / IMPACT ON M1              |
+-----------+---------------------------------------+------------------------+------------------------------------+
| GAP-01    | 1. Session 0/1 Path Ownership         | Confirmed Defect       | CRITICAL BLOCKER (Path Collision)  |
| GAP-02    | 1. Session 0 Process Relaunch         | Architectural Trade-Off| High (Service Privilege Scope)     |
| GAP-03    | 2. DPAPI vs Session 0 Checkpointing   | Confirmed Defect       | CRITICAL BLOCKER (Crypto Failure)  |
| GAP-04    | 2. Credential Vault Scope             | Required Safeguard     | Non-Blocking for M1                |
| GAP-05    | 3. Named Pipe Boot DACL Collision     | Confirmed Defect       | CRITICAL BLOCKER (IPC Refusal)     |
| GAP-06    | 3. Pipe Squatting & Impersonation     | Required Safeguard     | High (IPC Security Invariant)      |
| GAP-07    | 4. Job Object Handle Hygiene          | Required Safeguard     | High (Zero-Orphan Invariant)       |
| GAP-08    | 4. Nested Process Breakaway Limits    | Accepted Limitation    | Non-Blocking for M1                |
| GAP-09    | 5. SQLite WAL synchronous = NORMAL    | Architectural Trade-Off| High (Durability Scope Definition) |
| GAP-10    | 5. Cross-Session SQLite Shared Memory | Confirmed Defect       | CRITICAL BLOCKER (Lock Failure)    |
| GAP-11    | 6. In-Process Broker Boundary         | Architectural Trade-Off| Non-Blocking for M1 (M3 Blocker)   |
| GAP-12    | 6. Confused Deputy via Trusted Tools  | Unverified Assumption  | Non-Blocking for M1 (M3 Policy)    |
| GAP-13    | 7. Path Canonicalization TOCTOU Race  | Confirmed Defect       | High (Filesystem I/O Protocol)     |
| GAP-14    | 8. UIA Web/Electron Password Gaps     | Accepted Limitation    | Non-Blocking for M1 (M2 Scope)     |
| GAP-15    | 8. WGC Direct3D Visual PII Redaction  | Unverified Assumption  | Non-Blocking for M1 (M2 Scope)     |
| GAP-16    | 9. SHQueryUserNotificationState Gaps  | Confirmed Defect       | Non-Blocking for M1 (M2 Scope)     |
| GAP-17    | 10. Startup Pipe Connection Race      | Required Safeguard     | CRITICAL BLOCKER (Client Crash)    |
| GAP-18    | 10. Multi-Session Pipe Binding        | Required Safeguard     | High (Fast User Switching)         |
| GAP-19    | 11. Unsubstantiated Memory & CPU Caps | Unverified Assumption  | High (Acceptance Criteria Revision)|
| GAP-20    | 12. Protobuf Framing & Partial Reads  | Required Safeguard     | High (Wire Protocol Correctness)   |
+-----------------------------------------------------------------------------------------------------------------+
```

---

### AREA 1: SESSION 0 SERVICE AND SESSION 1 USER-HOST OWNERSHIP

#### Issue GAP-01: Session 0 / Session 1 Filesystem Storage Path Inconsistency
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §1.1, §6.0
- **Current Architectural Claim:** `aice_svc.exe` runs in Session 0 as `NT AUTHORITY\LocalService` and supervises `environment.db` and `memory.db` located at `%LOCALAPPDATA%\aice\`.
- **Classification:** **Confirmed Defect**
- **Technical Explanation:**  
  In the Windows architecture, `%LOCALAPPDATA%` resolves to `C:\Users\<Username>\AppData\Local`. When `aice_svc.exe` executes in Session 0 under `NT AUTHORITY\LocalService`, its environment has no interactive user profile mounted. Its `%LOCALAPPDATA%` path resolves to `C:\Windows\ServiceProfiles\LocalService\AppData\Local`.  
  At system boot, before interactive user logon, the user's directory (`C:\Users\<User>\AppData\Local`) does not exist in the service's environment. Furthermore, `LocalService` has no ambient permissions to traverse into private user profile trees without explicit ACL modifications.  
  Specifying `%LOCALAPPDATA%\aice\` as the storage root for databases supervised by Session 0 is a structural path collision.
- **Evidence Available:** Win32 User Profile & Service Architecture documentation; verified Windows directory structures.
- **Missing Evidence:** None. This is an invariant of the Windows security subsystem.
- **Proposed Resolution:**  
  Partition filesystem storage strictly by security domain:
  1. **Machine Domain (Session 0):** `%PROGRAMDATA%\aice\` (`C:\ProgramData\aice\`). Owned by `LocalService` / `SYSTEM` with read/write ACLs granted to `Authenticated Users`. Houses machine-level telemetry, hardware state, and `environment.db`.
  2. **User Domain (Session 1+):** `%LOCALAPPDATA%\aice\` (`C:\Users\<User>\AppData\Local\aice\`). Owned exclusively by the interactive user. Houses `memory.db`, `session.db`, JSONL event streams, and scratch workspaces. Session 0 services never open or lock user-domain databases directly.
- **Verification Test:** Write a test service running under `LocalService` that attempts to open `%LOCALAPPDATA%\aice\test.db` before logon. Verify failure, then verify successful creation under `%PROGRAMDATA%\aice\test.db`.
- **Impact on Milestone 1:** **Critical Blocker.** Storage directories and database paths must be partitioned before implementing database bootstrapping.

---

#### Issue GAP-02: Session 0 Inability to Relaunch Crashed Session 1 User Agent
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §1.1, §1.2
- **Current Architectural Claim:** `aice_host.exe` is bootstrapped via Task Scheduler `AtLogOn`. `aice_svc.exe` supervises machine state in Session 0.
- **Classification:** **Architectural Trade-Off**
- **Technical Explanation:**  
  If `aice_host.exe` crashes or is closed by the user in Session 1, how is it restored?  
  If the architectural intent is for `aice_svc.exe` to act as an active supervisor that relaunches `aice_host.exe` into the active user desktop, `aice_svc.exe` must invoke `CreateProcessAsUserW` using the user's primary token obtained via `WTSQueryUserToken(session_id)`.  
  However, calling `CreateProcessAsUserW` requires `SE_ASSIGNPRIMARYTOKEN_NAME` and `SE_INCREASE_QUOTA_NAME`. `NT AUTHORITY\LocalService` does **not** possess these privileges by default. Only `NT AUTHORITY\SYSTEM` possesses the required privileges.  
  If `aice_svc.exe` runs under `LocalService`, it cannot launch processes into Session 1.
- **Evidence Available:** MSDN documentation for `CreateProcessAsUserW` and `WTSQueryUserToken`.
- **Missing Evidence:** Empirical test of service account privilege configuration on target Windows 11 installation.
- **Proposed Resolution:**  
  Decide between two clean architectural models:
  - *Option A (Least Privilege - Recommended):* Retain `LocalService` for `aice_svc.exe`. Accept that `aice_svc.exe` is a passive telemetry listener and cannot spawn user-session processes. Delegate Session 1 crash recovery to a Task Scheduler restart trigger (`RestartOnFailure`) or a lightweight user-mode watchdog.
  - *Option B (Active Supervisor):* Run `aice_svc.exe` under `NT AUTHORITY\SYSTEM`, granting it the authority to call `WTSQueryUserToken` and `CreateProcessAsUserW` to actively manage `aice_host.exe` across logons, lockouts, and crashes.
- **Verification Test:** Attempt `WTSQueryUserToken` from a service running as `LocalService` vs `SYSTEM`. Confirm token acquisition error under `LocalService`.
- **Impact on Milestone 1:** High. Must finalize service account identity and process spawning boundaries before coding `aice_svc.exe`.

---

### AREA 2: DPAPI, CREDENTIAL MANAGER AND MACHINE/USER DATA SEPARATION

#### Issue GAP-03: User-Scoped DPAPI Incompatibility with SQLite WAL and Session 0
- **Relevant Specification Section:** `specs/01_SYSTEM_CONSTITUTION.md` §3.2; `specs/02_SYSTEM_ARCHITECTURE.md` §6.0; `specs/03_DECISIONS_AND_MILESTONES.md` §1 (Decision 3)
- **Current Architectural Claim:** State databases (`environment.db`, `memory.db`) are encrypted at rest using Windows DPAPI tied to the user's logon security identifier, while `aice_svc.exe` in Session 0 supervises and checkpoints them.
- **Classification:** **Confirmed Defect**
- **Technical Explanation:**  
  This claim contains two fatal engineering impossibilities:
  1. **Cross-Principal Decryption Failure:** User-scoped DPAPI keys (`CryptProtectData` without `CRYPTPROTECT_LOCAL_MACHINE`) are derived from the user's interactive logon credentials (managed by LSA). A Session 0 service running under `LocalService` or `SYSTEM` cannot decrypt user-scoped DPAPI ciphertext because the user's master key is inaccessible to Session 0 without impersonating the logged-on user.
  2. **SQLite Paging vs Opaque Blob Encryption:** SQLite requires random access to 4 KB pages across the main `.db` file and the `-wal` file. Running `CryptProtectData` encrypts an entire contiguous buffer into an opaque blob. SQLite cannot execute SQL queries, update WAL indices, or commit transactions on an opaque DPAPI blob.  
  To encrypt a live SQLite database at rest with page-level random access, an application must use an encryption VFS (such as SQLCipher or SQLite SEE) using AES-256-GCM. DPAPI can only be used to protect the *symmetric encryption key* in the user's session, not the raw database file on disk.
- **Evidence Available:** SQLite VFS Architecture Specification; Win32 DPAPI Reference (`wincrypt.h`).
- **Missing Evidence:** None. Direct mathematical and architectural incompatibility.
- **Proposed Resolution:**  
  1. Revoke the claim that raw SQLite database files are directly encrypted with `CryptProtectData`.
  2. For Milestone 1, rely on Windows NTFS Encrypting File System (EFS) or BitLocker for physical disk protection, with standard NTFS DACLs restricting database files to the logged-in user SID.
  3. When application-level at-rest encryption is introduced (Milestone 3), deploy an AEAD page-level SQLite VFS where the 256-bit database key is stored in the Windows Credential Manager or protected via user-scoped DPAPI in Session 1 only. Session 0 never touches encrypted user databases.
- **Verification Test:** Attempt to open a database file encrypted with `CryptProtectData` via `sqlite3_open_v2`. Verify immediate failure (`SQLITE_NOTADB`).
- **Impact on Milestone 1:** **Critical Blocker.** Eliminates a flawed encryption layer from M1 database initialization.

---

#### Issue GAP-04: Credential Vault Isolation across Session Boundaries
- **Relevant Specification Section:** `specs/03_DECISIONS_AND_MILESTONES.md` §1 (Decision 3)
- **Current Architectural Claim:** Windows Credential Locker (`CredWriteW` / `CredReadW`) is used for API credentials.
- **Classification:** **Required Safeguard**
- **Technical Explanation:**  
  The Windows Credential Manager stores credentials per-user profile. Calling `CredReadW` from a Session 0 service queries the system/service credential vault, which is isolated from the user's interactive credential vault.  
  All external API tokens (Gemini, Claude, OpenAI) and user authentication secrets must be accessed and managed exclusively by `aice_host.exe` in Session 1. Session 0 must have zero credential read capabilities.
- **Evidence Available:** Windows Credential Management API Documentation.
- **Missing Evidence:** None. Standard Win32 security boundary.
- **Proposed Resolution:** Document the explicit invariant: `aice_svc.exe` has no integration with `wincred.h`. All credential storage and retrieval is performed by `aice_host.exe` in Session 1.
- **Verification Test:** Confirm that credentials written by `aice_host` in Session 1 cannot be read by `aice_svc` in Session 0.
- **Impact on Milestone 1:** Non-blocking for M1 (M1 uses no external API credentials).

---

### AREA 3: NAMED PIPE AUTHENTICATION AND AUTHORIZATION

#### Issue GAP-05: Pre-Logon Named Pipe DACL Construction Failure
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §1.1, §3.1
- **Current Architectural Claim:** `aice_svc.exe` creates `\\.\pipe\aice_machine_ipc` at boot with an explicit SDDL DACL granting access strictly to: `D:P(A;;GA;;;SY)(A;;GA;;;LS)(A;;GA;;;<USER_SID>)`.
- **Classification:** **Confirmed Defect**
- **Technical Explanation:**  
  At system boot, `aice_svc.exe` starts before any interactive user logs on. The interactive user's SID (`<USER_SID>`) is unknown and does not exist in any session state.  
  If the service creates the Named Pipe with `<USER_SID>` in the DACL at service startup, the call to `ConvertStringSecurityDescriptorToSecurityDescriptorW` will fail with `ERROR_INVALID_SID` (or an empty SID string). If it only grants access to `SY` and `LS`, when the user logs in and `aice_host.exe` attempts to connect via `CreateFileW`, the connection is rejected with `ERROR_ACCESS_DENIED`.
- **Evidence Available:** Win32 Security Descriptor Definition Language (SDDL) Specifications.
- **Missing Evidence:** None. Logical timing contradiction.
- **Proposed Resolution:**  
  Deploy a two-stage security protocol:
  1. **Initial Pipe Creation:** Create the Named Pipe with a DACL granting `GENERIC_READ | GENERIC_WRITE` to the well-known SID `NT AUTHORITY\Authenticated Users` (`AU`), combined with `FILE_FLAG_FIRST_PIPE_INSTANCE` and `PIPE_REJECT_REMOTE_CLIENTS`.
  2. **Connection Handshake Verification:** Upon client connection, the server immediately calls `GetNamedPipeClientProcessId` and `ImpersonateNamedPipeClient`. It inspects the client's token using `OpenThreadToken` to verify:
     - The client's Session ID matches the active console session (`WTSGetActiveConsoleSessionId`).
     - The client process executable matches the trusted hash or path of `aice_host.exe`.
     - The client integrity level is at least `SECURITY_MANDATORY_MEDIUM_RID`.
  3. Revert impersonation immediately via `RevertToSelf`.
- **Verification Test:** Create a Named Pipe in a Session 0 service with the proposed DACL. Connect from an interactive user session and verify successful token inspection and connection establishment.
- **Impact on Milestone 1:** **Critical Blocker.** Milestone 1 IPC cannot function without this corrected connection handshake.

---

#### Issue GAP-06: Named Pipe Squatting and Impersonation Vulnerabilities
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §3.1
- **Current Architectural Claim:** Named Pipes are protected against hijacking and unauthorized connections.
- **Classification:** **Required Safeguard**
- **Technical Explanation:**  
  1. **Squatting:** If `aice_svc.exe` stops or crashes, a malicious local process in Session 1 could create a pipe with the same name (`\\.\pipe\aice_machine_ipc`). When `aice_host.exe` connects, it would be connecting to a rogue server.  
  2. **Client Token Theft:** If `aice_host.exe` connects using standard `CreateFileW` flags, a rogue pipe server can call `ImpersonateNamedPipeClient` to steal the user's primary security token.  
  3. **Server Impersonation:** Conversely, if the server impersonates an unvalidated client, a low-privilege client could cause the server to perform actions outside its intended security boundary.
- **Evidence Available:** CWE-427 (Uncontrolled Search Path / Squatting), Microsoft Security Advisory on Named Pipe Impersonation.
- **Missing Evidence:** None. Documented Windows security best practice.
- **Proposed Resolution:**  
  1. `aice_svc.exe` must pass `FILE_FLAG_FIRST_PIPE_INSTANCE` on initial creation. If the call fails with `ERROR_ALREADY_EXISTS` or `ERROR_ACCESS_DENIED`, the service logs a critical security alert and refuses to bind.
  2. `aice_host.exe` must connect using `SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION` in the `dwFlagsAndAttributes` parameter of `CreateFileW`. This explicitly restricts the server to identification only, making token impersonation mathematically impossible for the server.
- **Verification Test:** Attempt to create a duplicate pipe with `FILE_FLAG_FIRST_PIPE_INSTANCE` and verify failure. Connect a client with `SECURITY_IDENTIFICATION` and verify that `ImpersonateNamedPipeClient` fails with `ERROR_CANNOT_IMPERSONATE`.
- **Impact on Milestone 1:** High. Must be implemented in Milestone 1 IPC connection code.

---

### AREA 4: JOB OBJECT LIFECYCLE AND PROCESS-TREE CLEANUP

#### Issue GAP-07: Unconditional "Zero-Orphan" Claim vs Handle Leaks and COM Delegation
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §2.0, §2.2
- **Current Architectural Claim:** "AICE enforces a mathematically deterministic zero-orphan guarantee using Windows Win32 Job Objects... Windows kernel immediately closes hJob, triggering hardware-level termination of every child, grandchild, and spawned process in the tree."
- **Classification:** **Required Safeguard** (Correcting an Overstated Claim)
- **Technical Explanation:**  
  The claim of an "unconditional zero-orphan guarantee" is factually incorrect under three real-world Windows operating conditions:
  1. **Handle Leakage:** The Windows kernel terminates processes assigned to a Job Object when the **last open handle** to the Job Object is closed. If any other process on the system (e.g., an antivirus filter, Sysinternals tool, or parent process handle inheritance) holds or duplicates an open handle to `hJob`, terminating `aice_host.exe` will **not** trigger process termination because the job object's reference count remains > 0.
  2. **Out-of-Job COM/RPC Delegation:** A child process (such as a node CLI or Python worker) can easily bypass the Job Object without using `CREATE_BREAKAWAY_FROM_JOB` simply by delegating execution to an external Windows service via COM/RPC (e.g., `CoCreateInstance` of `WbemScripting.SWbemLocator`, `Shell.Application`, or Windows Task Scheduler). The spawned process runs under `wmiprvse.exe`, `dllhost.exe`, or `taskeng.exe` outside the Job Object.
  3. **Race Condition during Spawn:** If a child is created with `CREATE_SUSPENDED`, and the host crashes before `AssignProcessToJobObject` executes, the suspended child is leaked permanently as an orphan.
- **Evidence Available:** Win32 Job Objects Internals; Windows Internals (Russinovich et al., Part 1, Chapter 3).
- **Missing Evidence:** None. Standard kernel object lifecycle behavior.
- **Proposed Resolution:**  
  1. Downgrade the architectural claim from "mathematically deterministic" to a "high-assurance process lifecycle containment subject to handle hygiene".
  2. Prevent the spawn race condition by utilizing modern Windows 10/11 extended process creation attributes: initialize `STARTUPINFOEXW` with `PROC_THREAD_ATTRIBUTE_JOB_LIST` and pass the job handle directly to `CreateProcessW`. The kernel assigns the process to the Job Object **atomically at creation time**, eliminating the window between `CreateProcessW` and `AssignProcessToJobObject`.
  3. Set `bInheritHandles = FALSE` on `CreateProcessW` to prevent child processes from inheriting the Job Object handle.
- **Verification Test:** Launch a process assigned to a Job Object via `PROC_THREAD_ATTRIBUTE_JOB_LIST`, force-kill the supervisor with `taskkill /F /PID <pid>`, and verify that the entire child process tree exits within 100ms.
- **Impact on Milestone 1:** High. Must be implemented in Milestone 1 process supervision code.

---

#### Issue GAP-08: Development Toolchain Failures under Rigid Breakaway Bans
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §2.1
- **Current Architectural Claim:** `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0` and `JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0` applied uniformly across all worker tasks.
- **Classification:** **Accepted Limitation**
- **Technical Explanation:**  
  Certain legitimate development tools and language runtimes (e.g., Git Credential Manager, Docker CLI, VS Code IPC helpers) attempt to spawn independent background helper processes with `CREATE_BREAKAWAY_FROM_JOB` or `DETACHED_PROCESS`.  
  Under a rigid breakaway ban, these tools will fail with `ERROR_ACCESS_DENIED` unless nested job semantics (Windows 8+) are configured properly.
- **Evidence Available:** Observed behaviors in developer toolchains running inside CI and sandbox jobs.
- **Missing Evidence:** Empirical testing of specific MCP servers and build tools under the planned Job Object limit flags.
- **Proposed Resolution:** Configure the Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` as the root policy. For nested worker pools, utilize nested Job Objects (supported natively in Windows 8+) allowing child jobs to manage their own sub-quotas without breaking out of the parent container's termination lifecycle.
- **Verification Test:** Execute `git`, `cargo`, and `npm` build commands inside the configured Job Object and verify clean execution without `ERROR_ACCESS_DENIED`.
- **Impact on Milestone 1:** Non-blocking for M1 (M1 tests synthetic workers, not complex external toolchains).

---

### AREA 5: SQLITE WAL DURABILITY AND CRASH RECOVERY

#### Issue GAP-09: `PRAGMA synchronous = NORMAL` Vulnerability to Power Loss / BSOD
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §6.1; `specs/01_SYSTEM_CONSTITUTION.md` §2.2
- **Current Architectural Claim:** "Every SQLite database in AICE is initialized with strict transactional pragmas: PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; ... memory is never volatile-only; process death causes zero state corruption or unrecoverable amnesia."
- **Classification:** **Architectural Trade-Off**
- **Technical Explanation:**  
  According to the official SQLite documentation:  
  In WAL mode with `PRAGMA synchronous = NORMAL`, the SQLite engine flushes WAL frames to the OS page cache on commit, but only syncs the physical storage medium during a WAL checkpoint (every 1000 pages by default).  
  This provides **100% durability against application crashes** (`aice_host.exe` crash, unhandled exception, `taskkill`). However, it does **not** guarantee durability across an operating system crash (BSOD), sudden power loss, or hardware lockup. If the computer loses power, transactions committed to the WAL file that have not been physically flushed to the NVMe/SSD platters via `FlushFileBuffers` may be lost or rolled back.  
  Claiming "unbroken temporal state across unexpected power loss" while using `synchronous = NORMAL` is an engineering contradiction.
- **Evidence Available:** SQLite Official Documentation ("PRAGMA synchronous", Section 3: WAL Mode).
- **Missing Evidence:** None. Direct documented behavior of SQLite.
- **Proposed Resolution:**  
  Explicitly define the durability boundary in the architecture:
  - **Standard State Updates (Telemetry, Streaming steps):** Retain `PRAGMA synchronous = NORMAL` for high throughput and zero disk I/O stalls during active work.
  - **High-Consequence State Transitions (Step Completion, L3 User Approvals, Key Milestones):** Execute an explicit `PRAGMA wal_checkpoint(PASSIVE)` or issue `PRAGMA synchronous = FULL` before committing to force a hardware-level `FlushFileBuffers` call to non-volatile storage.
- **Verification Test:** Benchmark SQLite transaction throughput under `synchronous = NORMAL` vs `synchronous = FULL` on the target NVMe drive.
- **Impact on Milestone 1:** High. Must be explicitly documented in Milestone 1 database configuration.

---

#### Issue GAP-10: Cross-Session SQLite Shared Memory (`-shm`) Lock Collisions
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §1.1, §6.0, §6.1
- **Current Architectural Claim:** `aice_svc.exe` (Session 0) acts as "Global Database Lock / Backup / WAL Checkpoint Supervisor" while `aice_host.exe` (Session 1) writes active steps to `session.db` and `environment.db`.
- **Classification:** **Confirmed Defect**
- **Technical Explanation:**  
  SQLite WAL mode relies on a shared memory index file (`<database>.db-shm`) implemented on Windows using Win32 File Mapping Objects (`CreateFileMappingW`).  
  In the Windows kernel, named kernel objects and file mappings are partitioned into per-session object namespaces (`\Sessions\1\BaseNamedObjects` vs `\BaseNamedObjects` for Session 0).  
  When processes in different sessions (Session 0 vs Session 1) running under different user accounts (`LocalService` vs interactive user) attempt to open the same SQLite WAL database concurrently:
  1. The file mapping DACL will reject the cross-principal handle request with `ERROR_ACCESS_DENIED`.
  2. SQLite will return `SQLITE_IOERR_SHMMAP` or `SQLITE_CANTOPEN`.
  3. Concurrent multi-process access across Windows session boundaries using SQLite WAL mode is fundamentally unsupported without writing a custom Win32 VFS that implements cross-session shared memory security descriptors.
- **Evidence Available:** SQLite Win32 OS Interface Source (`os_win.c`); Windows Object Namespace Architecture.
- **Missing Evidence:** None. Confirmed architectural failure mode on Windows.
- **Proposed Resolution:**  
  **Enforce Strict Single-Process Database Ownership:**
  - `aice_svc.exe` in Session 0 exclusively opens and writes to `%PROGRAMDATA%\aice\environment.db`.
  - `aice_host.exe` in Session 1 exclusively opens and writes to `%LOCALAPPDATA%\aice\memory.db` and `sessions/<id>/session.db`.
  - Session 0 and Session 1 **never open each other's databases directly**. Any telemetry or checkpoint coordination is requested via the Named Pipe IPC protocol.
- **Verification Test:** Attempt to open a WAL database simultaneously from a Session 0 service and a Session 1 user process. Confirm `SQLITE_CANTOPEN` or `SQLITE_IOERR_SHMMAP` error, then confirm 100% reliability under single-process ownership.
- **Impact on Milestone 1:** **Critical Blocker.** Eliminates cross-session database corruption and locking errors.

---

### AREA 6: CAPABILITY BROKER ISOLATION AND MODEL AUTHORITY

#### Issue GAP-11: In-Process Broker Lacks Hardware/Process Memory Boundary
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §1.2, §5.0
- **Current Architectural Claim:** `aice_host.exe` hosts the Planning Engine, the Intelligence Gateway, and the Deterministic Capability Broker.
- **Classification:** **Architectural Trade-Off**
- **Technical Explanation:**  
  The Capability Broker is described as the non-bypassable security kernel of AICE. However, if the untrusted LLM parser, network gateway, and Capability Broker execute within the same process address space (`aice_host.exe`), there is no OS-level memory boundary isolating them.  
  A memory corruption vulnerability, unsafe pointer operation, or deserialization bug in the planning engine could theoretically alter the Capability Broker's internal permission tables or hijack the execution pointer.
- **Evidence Available:** Principle of Least Privilege; Saltzer and Schroeder (1975).
- **Missing Evidence:** Evaluation of Rust language memory-safety guarantees vs multi-process broker complexity.
- **Proposed Resolution:**  
  For Milestone 1 through Milestone 3, implement the Capability Broker as an isolated, compile-time enforced module written in safe Rust (`#![forbid(unsafe_code)]`), with private fields and non-bypassable method signatures. For long-term enterprise defense-in-depth, evaluate extracting the Capability Broker into a separate low-privilege execution broker process.
- **Verification Test:** Unit test capability boundary checks with synthetic malicious intents.
- **Impact on Milestone 1:** Non-blocking for M1 (Capability Broker is Milestone 3).

---

#### Issue GAP-12: Indirect Confused-Deputy Execution via Trusted Build Commands
- **Relevant Specification Section:** `specs/01_SYSTEM_CONSTITUTION.md` §3.4; `specs/02_SYSTEM_ARCHITECTURE.md` §5.1
- **Current Architectural Claim:** Authority Matrix classifies build commands and test runs (`npm test`, `cargo build`) as Level 2 (Reversible Execution - Autonomous within Workspace Policy).
- **Classification:** **Unverified Assumption** / **Security Vulnerability**
- **Technical Explanation:**  
  Classifying build commands as autonomous Level 2 assumes that build tools are non-destructive and reversible.  
  This is a false assumption:
  - `npm test` executes arbitrary scripts defined in `package.json`. A compromised repository or dependency can execute `rmdir /s /q C:\` or exfiltrate private SSH keys.
  - `cargo build` compiles and executes `build.rs` build scripts with full Turing-complete execution authority.
  - Compilers and package managers have complete access to the network and filesystem under the user's privilege context.
- **Evidence Available:** Documented supply-chain attacks via npm post-install scripts and malicious build scripts.
- **Missing Evidence:** None. Well-documented threat vector.
- **Proposed Resolution:**  
  Update the Authority Matrix in `specs/01_SYSTEM_CONSTITUTION.md`:
  - Shell execution of arbitrary package manager targets (`npm`, `cargo`, `pip`) requires an explicit workspace trust evaluation.
  - If a workspace contains unreviewed third-party code or is not explicitly marked `TRUSTED_LOCAL_PROJECT` by the user, build tool execution must escalate to **Level 3 (Mandatory User Consent)** or execute inside an isolated container (Windows Sandbox / AppContainer).
- **Verification Test:** Test policy escalation when an untrusted repository triggers `npm install`.
- **Impact on Milestone 1:** Non-blocking for M1 (Milestone 3 concern).

---

### AREA 7: FILE ACCESS, PATH VALIDATION AND TOCTOU RISKS

#### Issue GAP-13: String-Based Path Canonicalization Vulnerable to TOCTOU Races
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §5.2
- **Current Architectural Claim:** "Canonicalization: Every filesystem path passed to a tool must be resolved to its Win32 canonical path via GetFullPathNameW and checked against junction points (GetFileAttributesW & FILE_ATTRIBUTE_REPARSE_POINT) to prevent path traversal outside designated workspace trees."
- **Classification:** **Confirmed Defect**
- **Technical Explanation:**  
  Validating a path string using `GetFullPathNameW` and `GetFileAttributesW` before calling `CreateFileW` is a classic **Time-Of-Check to Time-Of-Use (TOCTOU)** vulnerability:
  1. An attacker or concurrent process can replace an approved directory with an NTFS junction point or symlink between the verification check and the file open call.
  2. String prefix checks fail against Windows 8.3 short filename generation (e.g., `C:\PROGRA~1`), NTFS Alternate Data Streams (`file.txt:hidden`), trailing spaces, and trailing dots which the Win32 subsystem normalizes differently than basic string matchers.
- **Evidence Available:** CWE-367 (Time-of-check Time-of-use Race Condition); Microsoft Win32 Path Handling Guidelines.
- **Missing Evidence:** None. Standard security flaw in pre-check file validation.
- **Proposed Resolution:**  
  Enforce **Handle-Based Path Verification**:
  1. Open the file or directory directly using `CreateFileW` with `FILE_FLAG_OPEN_REPARSE_POINT` and appropriate sharing flags.
  2. Call `GetFinalPathNameByHandleW(hFile, lpFilePath, cchFilePath, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS)` on the open file handle.
  3. Verify that the normalized path returned by the kernel handle is strictly located within the approved canonical workspace prefix.
  4. Perform all subsequent reads/writes using that verified handle. Never re-resolve paths by string after verification.
- **Verification Test:** Write a unit test that creates a junction point racing with an open file operation. Verify that handle-based resolution successfully blocks traversal outside the sandbox.
- **Impact on Milestone 1:** High. Must be incorporated into all file I/O utility modules.

---

### AREA 8: UI AUTOMATION AND WINDOWS GRAPHICS CAPTURE PRIVACY BOUNDARIES

#### Issue GAP-14: UIA Password Masking Gaps in Modern Web and Electron Renders
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §4.2
- **Current Architectural Claim:** "If a window or control matches UIA_IsPasswordPropertyId = TRUE, its value and textual content are masked at the API capture layer."
- **Classification:** **Accepted Limitation**
- **Technical Explanation:**  
  In modern web browsers (Chrome, Edge) and Electron applications (VS Code, Slack), HTML password inputs (`<input type="password">`) do not expose `UIA_IsPasswordPropertyId = TRUE` unless the browser's accessibility engine is explicitly activated.  
  Furthermore, custom web components and multi-factor authentication (MFA) input boxes frequently fail to expose the password attribute, resulting in plain-text accessibility node exposure.  
  Relying solely on `UIA_IsPasswordPropertyId` leaves an unavoidable leakage window for web-rendered credentials.
- **Evidence Available:** Chromium Accessibility Architecture Documentation; testing on Chrome/Edge UIA trees.
- **Missing Evidence:** Empirical audit of password exposure across target user applications.
- **Proposed Resolution:**  
  Combine `UIA_IsPasswordPropertyId` with secondary heuristic guards:
  - Inspect control class names and automation IDs for password-related strings (`pwd`, `passwd`, `token`, `secret`, `creditcard`).
  - Automatically blacklist text extraction from known sensitive application classes (e.g. 1Password, Bitwarden, KeePass, Windows Credential UI dialogs).
- **Verification Test:** Query UIA trees of Chrome, Edge, and Bitwarden login forms and measure detection accuracy.
- **Impact on Milestone 1:** Non-blocking for M1 (Perception is Milestone 2).

---

#### Issue GAP-15: Direct3D Texture PII Redaction Claim is Unverified
- **Relevant Specification Section:** `specs/01_SYSTEM_CONSTITUTION.md` §3.2; `specs/02_SYSTEM_ARCHITECTURE.md` §4.3
- **Current Architectural Claim:** "Screen buffers, clipboard data... pass through a deterministic PII/Secret scrubbing pipeline before entering the persistence engine or inference context."
- **Classification:** **Unverified Assumption**
- **Technical Explanation:**  
  Windows Graphics Capture (WGC) delivers raw BGRA8 pixel buffers in Direct3D11 textures directly from the Desktop Window Manager (DWM).  
  Deterministic PII/Secret regex scrubbing operates exclusively on textual strings, not on raw pixel bitmaps. To scrub secrets from a Direct3D texture before storage or model transmission, the system would need to run continuous OCR or computer vision segmentation across 1080p/4K frames.  
  Running continuous OCR violates the system's strict < 0.1% CPU idle budget and consumes substantial GPU/NPU power.
- **Evidence Available:** WGC API Specification; OCR computational profiling.
- **Missing Evidence:** Benchmarked latency and power consumption of real-time frame OCR on the target AMD Ryzen AI NPU.
- **Proposed Resolution:**  
  1. Revoke the claim of "deterministic secret scrubbing on raw screen buffers".
  2. Implement a structural privacy boundary: screen capture is strictly on-demand. When capture is triggered, AICE queries the bounding boxes of any active windows flagged as sensitive or password-bearing via UIA, and uses Direct3D to blit solid black rectangles over those screen regions before exporting the texture.
- **Verification Test:** Trigger WGC capture with a login dialog open; verify that the login window bounding box is rendered as solid black in the captured D3D11 buffer.
- **Impact on Milestone 1:** Non-blocking for M1 (Milestone 2 concern).

---

### AREA 9: FULLSCREEN DETECTION AND RESOURCE GOVERNOR RELIABILITY

#### Issue GAP-16: `SHQueryUserNotificationState` Failure in Borderless Windowed Games
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §8.1; `specs/03_DECISIONS_AND_MILESTONES.md` §1 (Decision 4)
- **Current Architectural Claim:** "`SHQueryUserNotificationState(&pquns)` returns `QUNS_RUNNING_D3D_FULL_SCREEN` when a DirectX or Vulkan game is executing full-screen."
- **Classification:** **Confirmed Defect**
- **Technical Explanation:**  
  In Windows 10 and 11, the vast majority of modern PC games default to "Borderless Windowed" mode rather than exclusive fullscreen mode.  
  In borderless windowed mode, `SHQueryUserNotificationState` frequently returns `QUNS_ACCEPTS_NOTIFICATIONS` or `QUNS_BUSY`, failing to detect that a full-screen 3D game is actively running.  
  Relying solely on `SHQueryUserNotificationState` will leave the resource governor unaware of active gaming sessions, allowing background agent tasks to cause frame drops and GPU latency spikes.
- **Evidence Available:** Documented Windows Shell API behavior; widespread testing across DirectX 12/Vulkan gaming titles.
- **Missing Evidence:** Direct empirical validation of `SHQueryUserNotificationState` across 5 modern borderless games on the host NVIDIA RTX 5050.
- **Proposed Resolution:**  
  Deploy a **Multi-Signal Gaming Sensor**:
  1. Signal 1: `SHQueryUserNotificationState` check (`QUNS_RUNNING_D3D_FULL_SCREEN` or `QUNS_PRESENTATION_MODE`).
  2. Signal 2: Foreground Window Bounds Inspection: Query `GetForegroundWindow()`, `GetWindowRect()`, and `GetWindowLongW(GWL_STYLE)`. If the window dimensions match or exceed the monitor resolution (`GetMonitorInfoW`), lacks standard window borders/captions (`!(WS_CAPTION | WS_THICKFRAME)`), and is rendering via DirectX/Vulkan, flag as Fullscreen 3D.
  3. Signal 3: GPU Utilization Query via DXGI / NVML.
- **Verification Test:** Run a game in borderless windowed mode; verify that the multi-signal governor successfully detects the session and triggers background throttling.
- **Impact on Milestone 1:** Non-blocking for M1 (Milestone 2 concern).

---

### AREA 10: STARTUP TIMING, MULTI-SESSION BEHAVIOR AND SERVICE RECOVERY

#### Issue GAP-17: Named Pipe Connection Race Condition at User Logon
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §1.1, §1.2; `specs/03_DECISIONS_AND_MILESTONES.md` §3.2
- **Current Architectural Claim:** `aice_host.exe` connects to Named Pipe `\\.\pipe\aice_machine_ipc` immediately upon user logon.
- **Classification:** **Required Safeguard**
- **Technical Explanation:**  
  During system boot, Task Scheduler launches `aice_host.exe` as soon as the user logs on. If `aice_svc.exe` is delayed during its Service Control Manager initialization, `aice_host.exe` calling `CreateFileW` on the pipe will immediately fail with `ERROR_FILE_NOT_FOUND`.  
  If the host lacks an explicit retry and backoff state machine, the user agent will crash or abort on logon.
- **Evidence Available:** Windows Boot & Service Startup Timing Traces.
- **Missing Evidence:** Measurement of service startup latency vs user logon timing on the target machine.
- **Proposed Resolution:**  
  Implement a deterministic connection protocol in `aice_host.exe`:
  - Call `WaitNamedPipeW(pipe_path, 2000)` in a loop up to 15 times (30-second maximum timeout window).
  - If `WaitNamedPipeW` succeeds, execute `CreateFileW`.
  - If the pipe returns `ERROR_PIPE_BUSY`, retry with exponential backoff.
- **Verification Test:** Simulate delayed service startup (service creates pipe 5 seconds after logon); verify that `aice_host.exe` waits cleanly and connects without error.
- **Impact on Milestone 1:** **Critical Blocker.** Milestone 1 client bootstrap must implement this retry loop.

---

#### Issue GAP-18: Fast User Switching and Session Multiplexing Collision
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §1.1, §3.1
- **Current Architectural Claim:** Named Pipe is named `\\.\pipe\aice_machine_ipc`.
- **Classification:** **Required Safeguard**
- **Technical Explanation:**  
  If multiple users log in simultaneously via Fast User Switching or Remote Desktop (Session 1 and Session 2), multiple `aice_host.exe` processes will attempt to connect to the single Named Pipe.  
  If the pipe server was created with `nMaxInstances = 1`, the second user session will be permanently locked out with `ERROR_PIPE_BUSY`.  
  Furthermore, the service must isolate messages so that telemetry or commands from Session 1 cannot leak into Session 2.
- **Evidence Available:** Terminal Services Multi-Session Architecture.
- **Missing Evidence:** None. Standard multi-session Windows behavior.
- **Proposed Resolution:**  
  1. Configure `CreateNamedPipeW` with `nMaxInstances = PIPE_UNLIMITED_INSTANCES`.
  2. Upon connection, call `GetNamedPipeClientSessionId` to determine the caller's Windows Session ID.
  3. Bind each connection to an isolated in-memory session context inside `aice_svc.exe`.
- **Verification Test:** Connect two simultaneous mock clients from different sessions; verify that both maintain independent IPC channels.
- **Impact on Milestone 1:** High. Must be implemented in Milestone 1 IPC server loop.

---

### AREA 11: RESOURCE BUDGETS AND PERFORMANCE CLAIMS

#### Issue GAP-19: Unsubstantiated Idle Memory (< 25 MB) and CPU (< 0.1%) Guarantees
- **Relevant Specification Section:** `specs/03_DECISIONS_AND_MILESTONES.md` §3.3; `specs/02_SYSTEM_ARCHITECTURE.md` §6.1
- **Current Architectural Claim:** "Background idle CPU consumption is measurably < 0.1% and memory footprint is < 25 MB RAM."
- **Classification:** **Unverified Assumption**
- **Technical Explanation:**  
  In `specs/02_SYSTEM_ARCHITECTURE.md` §6.1, the spec mandates `PRAGMA mmap_size = 268435456` (256 MB memory-mapped I/O).  
  While MMAP reserves virtual address space rather than physical RAM, as database queries touch pages, the Windows kernel commits those pages to the process's working set.  
  Furthermore, hosting an embedded SQLite engine, WinEvent hooks, UIA COM proxies, and a Named Pipe IPC server in Rust or Go typically results in a base Working Set between 15 MB and 35 MB depending on heap fragmentation.  
  Presenting "< 25 MB RAM" and "< 0.1% CPU" as firm acceptance criteria before establishing an empirical baseline is an unverified assumption.
- **Evidence Available:** Memory footprints of native Rust/C++ Windows background daemons.
- **Missing Evidence:** Empirical memory and CPU profiling of `aice_svc` and `aice_host` on the target AMD Ryzen AI platform.
- **Proposed Resolution:**  
  Reclassify these figures as **Target Resource Budgets** rather than firm pass/fail criteria. Replace them in Milestone 1 acceptance criteria with an empirical telemetry test: measure Working Set, Private Bytes, and CPU utilization over a 1-hour idle soak test using Windows Performance Monitor (`typeperf` / ETW).
- **Verification Test:** Run a 60-minute idle soak test recording Working Set and CPU usage every 5 seconds.
- **Impact on Milestone 1:** High. Prevents premature milestone failure based on unverified targets.

---

### AREA 12: PROTOBUF FRAMING, PROTOCOL VERSIONING AND IPC FAILURE BEHAVIOR

#### Issue GAP-20: Byte-Stream Partial Reads and Lack of Protocol Versioning
- **Relevant Specification Section:** `specs/02_SYSTEM_ARCHITECTURE.md` §3.2
- **Current Architectural Claim:** IPC pipes use a 4-byte length-prefixed framing protocol over Windows Named Pipes.
- **Classification:** **Required Safeguard**
- **Technical Explanation:**  
  1. **Partial Reads:** If the Named Pipe is opened in byte-stream mode (`PIPE_TYPE_BYTE`), `ReadFile` is not guaranteed to return a complete frame. An IPC packet can be split across multiple chunks. If the reader does not implement an explicit frame accumulator state machine, partial reads will cause frame corruption and stream desynchronization.  
  2. **Alternative Message Mode:** Windows Named Pipes natively support `PIPE_TYPE_MESSAGE` and `PIPE_READMODE_MESSAGE`. In message mode, the Windows kernel guarantees atomic message boundaries.  
  3. **Protocol Versioning:** The wire frame header lacks a protocol version field. If `aice_svc.exe` is updated while `aice_host.exe` is running an older binary, protocol mismatches will cause deserialization failures without graceful error reporting.
- **Evidence Available:** Win32 Named Pipe Mode Documentation (`PIPE_TYPE_MESSAGE` vs `PIPE_TYPE_BYTE`).
- **Missing Evidence:** None. Standard network/IPC framing principle.
- **Proposed Resolution:**  
  Standardize the wire frame header to 8 bytes:
  ```
  +-------------------------+-------------------------+-------------------------+-------------------------+
  | Protocol Version (2B)   | Message Type (2B)       | Payload Length (4B)     | Payload (N Bytes)       |
  | uint16_t (0x0001)       | uint16_t Enum           | uint32_t (Little-Endian)| Protobuf Serialized Blob|
  +-------------------------+-------------------------+-------------------------+-------------------------+
  ```
  Configure the Named Pipe in `PIPE_TYPE_MESSAGE` mode to utilize kernel-level message framing, and implement a fallback accumulator if stream mode is utilized.
- **Verification Test:** Send fragmented frames (1 byte at a time) across the pipe; verify that the framing layer reconstructs the exact Protobuf payload without desynchronization.
- **Impact on Milestone 1:** High. Must be implemented in Milestone 1 IPC serialization code.

---

## 3. CRITICAL BLOCKERS FOR MILESTONE 1

The following issues must be resolved before Milestone 1 implementation begins:

1. **Storage Path Partitioning (GAP-01):** Fix `%LOCALAPPDATA%` path contradiction in Session 0. Partition storage strictly into `%PROGRAMDATA%\aice\` (Machine) and `%LOCALAPPDATA%\aice\` (User).
2. **Named Pipe Handshake & Pre-Logon DACL (GAP-05):** Fix the pre-logon `<USER_SID>` DACL collision. Implement two-stage handshake: `AU` pipe creation + caller token inspection upon connection.
3. **Cross-Session SQLite Concurrency Elimination (GAP-10):** Enforce single-process database ownership. Session 0 owns `environment.db`; Session 1 owns `memory.db` and `session.db`. Prohibit concurrent cross-session SQLite WAL mapping.
4. **Named Pipe Startup Race Condition (GAP-17):** Implement exponential backoff and `WaitNamedPipeW` polling in `aice_host.exe`.
5. **DPAPI Scope Clarification (GAP-03):** Remove claims of direct DPAPI encryption on live SQLite database files.

---

## 4. NON-BLOCKING CONCERNS (DEFERRED TO LATER MILESTONES)

The following concerns do not block Milestone 1 and are formally assigned to later milestones:

- **GAP-04 (Credential Locker Scope):** Deferred to Milestone 5 (External Cloud Gateway).
- **GAP-11 (In-Process Capability Broker Boundary):** Deferred to Milestone 3 (Capability Broker Implementation).
- **GAP-12 (Build Tool Confused-Deputy Policy):** Deferred to Milestone 3 (Authority Matrix Hardening).
- **GAP-14 (Web/Electron Password Masking Gaps):** Deferred to Milestone 2 (Perception Pipeline).
- **GAP-15 (Direct3D Frame PII Redaction Strategy):** Deferred to Milestone 2 (WGC Screen Capture).
- **GAP-16 (Borderless Windowed Gaming Detection):** Deferred to Milestone 2 (Resource Governor).

---

## 5. DECISIONS REQUIRING HUMAN APPROVAL

The following architectural decisions require explicit review and approval by the user/architect:

1. **Service Account Identity for `aice_svc.exe`:**
   - *Choice:* `NT AUTHORITY\LocalService` (Least Privilege, passive telemetry only, cannot relaunch Session 1 host) vs `NT AUTHORITY\SYSTEM` (Full control, can monitor sessions and call `CreateProcessAsUserW` to restart `aice_host.exe`).
   - *Architect Recommendation:* Adopt `LocalService` to preserve least-privilege security boundaries; rely on Task Scheduler or user-mode watchdog for Session 1 host restarts.
2. **SQLite Durability Mode:**
   - *Choice:* `PRAGMA synchronous = NORMAL` (High throughput, durable against application crashes, vulnerable to power cuts/BSOD) vs `PRAGMA synchronous = FULL` (Slower, durable against power cuts).
   - *Architect Recommendation:* Use `NORMAL` for streaming steps; execute explicit checkpoints before closing or upon high-consequence state transitions.
3. **Database Encryption Standard at Rest:**
   - *Choice:* Rely on OS-level BitLocker/EFS with NTFS ACLs vs developing a custom page-level SQLite AEAD encryption VFS.
   - *Architect Recommendation:* Rely on OS-level BitLocker and strict NTFS ACLs for Milestones 1 and 2; evaluate custom encryption VFS for Milestone 3.

---

## 6. RECOMMENDED ARCHITECTURE CORRECTIONS

To freeze the corrected baseline, the following amendments are approved for adoption:

1. **Directory Structure Standard:**
   ```
   %PROGRAMDATA%\aice\                  <- Session 0 Machine Storage
   +-- environment.db                   <- Global Machine Telemetry (Owned by LocalService)
   +-- logs\machine_svc.log

   %LOCALAPPDATA%\aice\                 <- Session 1 User Storage
   +-- memory.db                        <- Semantic Facts & Memory Graph (Owned by User)
   +-- sessions\
       +-- {session_uuid}\
           +-- session.db               <- Canonical Protobuf Step Journal
           +-- stream.jsonl             <- Streaming Event Log
   ```
2. **Handle-Based File Verification:** All filesystem path validation must use handle-based resolution (`GetFinalPathNameByHandleW`) to eliminate TOCTOU vulnerabilities.
3. **Atomic Job Object Assignment:** Use `PROC_THREAD_ATTRIBUTE_JOB_LIST` in `STARTUPINFOEXW` to assign child processes to Job Objects atomically at creation time.
4. **Multi-Signal Gaming Sensor:** Combine `SHQueryUserNotificationState` with foreground window bounds and D3D style checks to reliably detect borderless windowed games.

---

## 7. REVISED MILESTONE 1 ACCEPTANCE CRITERIA

The acceptance criteria for Milestone 1 are formally revised as follows:

- [ ] **Service Bootstrapping:** `aice_svc.exe` installs and starts automatically as a Windows Service under `LocalService`, cleanly recording machine boot telemetry to `%PROGRAMDATA%\aice\environment.db`.
- [ ] **Secured IPC Handshake:** `aice_host.exe` connects to `\\.\pipe\aice_machine_ipc` via `WaitNamedPipeW` with retry backoff. The server inspects caller token and rejects connections from unauthenticated or low-integrity SIDs.
- [ ] **Verified Process Supervision:** `aice_host.exe` spawns `aice_worker.exe` using `PROC_THREAD_ATTRIBUTE_JOB_LIST` with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Force-killing `aice_host.exe` (`taskkill /F`) terminates 100% of child processes within 200ms with zero orphans.
- [ ] **Single-Process SQLite WAL Integrity:** `aice_host.exe` performs 1,000 rapid step insertions into `session.db` under WAL mode. Simulating process hard termination mid-write results in zero database corruption on restart.
- [ ] **Empirical Baseline Telemetry:** Execute a 30-minute idle soak test recording Working Set RAM and CPU utilization. System must demonstrate stable memory usage without leaks and idle CPU utilization under 0.5% on the target machine.

---

## 8. EXPLICIT LIST OF CLAIMS THAT MUST NOT BE TREATED AS GUARANTEES

To maintain intellectual honesty, the following claims from previous reports and specifications are formally classified as **Non-Guaranteed Aspirations**:

1. **"Mathematically Deterministic Zero Orphans":** NOT guaranteed if handles are leaked by third-party tools, or if children delegate execution via out-of-process COM/RPC services.
2. **"Unbroken State across Sudden Power Loss":** NOT guaranteed under `PRAGMA synchronous = NORMAL`. Uncheckpointed WAL frames in OS page cache can be lost during hardware power failure.
3. **"Deterministic Secret Scrubbing on Screen Textures":** NOT guaranteed on raw Direct3D pixel buffers. Visual masking is heuristic and bounded by UIA bounding box queries.
4. **"Reliable DirectX Gaming Detection via SHQueryUserNotificationState":** NOT guaranteed for borderless windowed games without secondary window bounding box heuristics.
5. **"< 25 MB RAM and < 0.1% CPU Hard Ceiling":** NOT guaranteed until empirical baseline profiling is conducted on the compiled substrate binaries.
