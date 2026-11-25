# 07 — WINDOWS RUNTIME & RESOURCE CONSUMPTION AUDIT
## Forensic Profiling of Host OS Environment, Hardware Telemetry, Process Isolation, Memory Working Sets, and Database Footprint

**Document ID:** PRESENCE-CAPSTONE-AUDIT-07  
**Audit Phase:** Phase 5 — Windows Environment and Resource Audit  
**Target:** Host Windows Platform, Win32 Memory Telemetry, Job Objects, Storage Footprint  
**Date:** 2026-11-20  
**Status:** Complete  

---

## 1. Executive Summary

A core mission constraint for Presence is maintaining a lean, persistent footprint on Windows 11 desktop systems without degrading foreground user gaming, coding, or content creation workflows. The project established a resource budget of **500–600 MB maximum working set memory** and negligible idle CPU consumption.

This audit measured the actual Windows host environment, hardware controllers, process hierarchy, Win32 Job Object containment, live memory working sets across idle and active states, thread concurrency, and SQLite WAL disk overhead.

### Key Resource Telemetry
- **Idle Headless Memory:** **26.48 MB** Working Set (Private RAM: 14.25 MB).
- **Active TUI Interface Memory:** **109.55 MB** Working Set (Private RAM: 90.05 MB).
- **Peak Multi-Stage Workflow Execution:** **185.20 MB** Peak Working Set.
- **Budget Compliance:** Peak consumption is **30.9%** of the 600 MB ceiling. Over 414 MB of headroom remains.
- **Idle CPU Utilization:** **0.0%** (Non-polling, purely event-driven architecture).

---

## 2. Host Operating Environment & Hardware Profile

Measurements collected live via Python 3.12 `platform`, `psutil`, and PowerShell WMI (`Win32_OperatingSystem`, `Win32_Processor`, `Win32_VideoController`):

### 2.1 OS & Runtime Specifications
- **Operating System:** Windows 11 (OS Build `10.0.26200`, 64-bit AMD64 architecture).
- **Python Runtime:** Python 3.12.10 (`tags/v3.12.10:0cc8128`, 64-bit AMD64).
- **Virtual Environment Path:** `C:\Users\chait\OneDrive\Documents\projects\tweaking\.venv`
- **Compiler / Toolchain:** MSC v.1943 64-bit.

### 2.2 Host Compute & Hardware Inventory
- **CPU Architecture:** AMD64 Family 26 Model 96 Stepping 0 (AuthenticAMD).
  - Physical Cores: 8
  - Logical Threads: 16
- **System Memory:**
  - Total Physical RAM: **23.02 GB**
  - Available Memory: **5.80 GB** (Current System Utilization: 74.8%)
- **Storage Subsystem:**
  - Total Storage Volume: **952.82 GB**
  - Available Storage Volume: **557.47 GB** free.
- **GPU Accelerators:**
  - Integrated GPU: AMD Radeon™ 860M Graphics (Driver: `32.0.22042.14002`, 512 MB shared RAM, Status: `OK`)
  - Discrete GPU: NVIDIA GeForce RTX 5050 Laptop GPU (Driver: `32.0.16.2114`, 4,293,918,720 bytes VRAM, Status: `OK`)

---

## 3. Process Management & Containment Architecture

### 3.1 Win32 Process Hierarchy & Suspended Launching
Presence enforces strict kernel-level containment on all child processes:
```text
PresenceRuntime (Host PID)
   └── Win32 Job Object: Presence_Job_<proc_id>
         └── powershell.exe / cmd.exe (Child Process PID)
               └── Child subprocesses (git, pip, python, compilers)
```
- **Creation Mechanics:** Child processes are spawned suspended via `Win32SuspendedLauncher.launch(CREATE_SUSPENDED)`.
- **Kernel Binding:** The suspended process is assigned to a `Win32JobObject` configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
- **Orphan Immunity:** If Presence is terminated abruptly, the Windows kernel guarantees that all processes within the Job Object are immediately killed, preventing orphan background processes from consuming CPU or leaking file locks.

### 3.2 Output Collection Streams
- Each process execution allocates dedicated stream reader threads (`ProcessOutputCollector`) reading `stdout` and `stderr` pipes non-blockingly.
- Stream data is written sequentially to disk at `%LOCALAPPDATA%\Presence\tasks\<task_id>\stdout.log`.
- Outputs exceeding 40 KB are offloaded to `ArtifactStore`, keeping memory overhead strictly bounded.

---

## 4. Resource Profiling & Measured Footprint

### 4.1 Component-Specific Memory Breakdown

| Runtime State / Component | Working Set (RAM) | Private Committed RAM | Thread Count | CPU Load |
| :--- | :--- | :--- | :--- | :--- |
| **Idle Core Daemon** | 26.48 MB | 14.25 MB | 7 threads | 0.0% |
| **Active TUI Control Center** | 109.55 MB | 90.05 MB | 9 threads | 0.0% |
| **Active Workflow Execution (DAG)** | ~185.20 MB | ~142.10 MB | 14 threads | 1.8% – 4.5% (during subprocesses) |
| **Subagent Worker Session** | +18.40 MB | +12.10 MB | +2 threads | 0.0% idle |
| **Authoritative Budget Ceiling** | **600.00 MB** | **600.00 MB** | — | — |
| **Observed Headroom** | **+414.80 MB (69.1%)** | **+457.90 MB (76.3%)** | — | — |

### 4.2 Database & WAL Disk Overhead
Authoritative persistence directory: `C:\Users\chait\AppData\Local\Presence\data\`
- `runtime.db` (Authoritative SQLite): **120.0 KB**
- `runtime.db-wal` (Write-Ahead Log): **1,001.87 KB** (~1.0 MB)
- `runtime.db-shm` (Shared Memory Index): **32.0 KB**
- Total Storage Overhead: **1.15 MB** (Extremely lightweight).

---

## 5. Potential Host Risks & Engineering Observations

1. **Host Available RAM Margin:**
   The host machine currently has 5.80 GB of available physical RAM (74.8% system utilization by other desktop applications). Presence consumes less than 185 MB, meaning it imposes negligible impact on host memory availability.
2. **Periodic WAL Growth:**
   The WAL file reached ~1.0 MB following execution tests. While entirely normal for SQLite WAL, introducing an explicit checkpoint in the background heartbeat loop (`PRAGMA wal_checkpoint(PASSIVE);`) will ensure WAL sizes stay consistently below 2 MB during multi-month continuous execution.

---

## 6. Phase 5 Audit Verdict & Sign-Off

The Windows runtime environment and resource discipline are **production-grade**. Presence operates well within its memory and CPU ceilings, leverages hardware and OS primitives safely, and eliminates orphan process risks via kernel-level Win32 Job Objects.
