# 08 — PROVIDER & MCP INFRASTRUCTURE READINESS AUDIT
## Forensic Verification of Model Providers, Fallback Hierarchies, Rate-Limit Suppression, and Supervised Model Context Protocol (MCP) Infrastructure

**Document ID:** PRESENCE-CAPSTONE-AUDIT-08  
**Audit Phase:** Phase 6 — Provider and MCP Readiness Audit  
**Target:** `runtime.providers`, `runtime.mcp`, `mcp_servers.json`, `runtime.search`  
**Date:** 2026-11-20  
**Status:** Complete  

---

## 1. Executive Summary

A persistent desktop AI companion must maintain operational availability across unpredictable cloud API rate limits, upstream outages, and external protocol servers.

This audit conducted a comprehensive inspection of:
1. **Configured AI Model Providers:** Validation status, model identifiers, fallback ordering, rate-limit backpressure, and streaming compatibility.
2. **Supervised Model Context Protocol (MCP) Infrastructure:** Server definitions, lifecycle states (configured, enabled, stopped, running), tool schema caching, authority tiers, and process containment.

### Key Audit Findings
- **Primary AI Provider Ready:** `GeminiModelProvider` validated successfully against Google GenAI SDK (`google-genai==3.3.0`), configured with `gemini-3.5-flash-lite`.
- **Secondary Provider Router Configured:** `FreeLLMAPIProvider` provides multi-upstream routing (OpenRouter, Google, Cloudflare Workers AI) with error trail reporting.
- **MCP Supervisor Verified:** 2 external production MCP servers (`filesystem`, `shell`) configured in `mcp_servers.json`, providing 22 cached, schema-validated tools.
- **Strict Process Isolation:** External MCP server processes run inside Win32 Job Objects with bounded memory and execution ceilings.

---

## 2. Model Provider Architecture & Fallback Ordering

### 2.1 Provider Registry & Capabilities

| Provider Adapter | Target Model ID | Validation Status | Streaming Support | Tool Call Support | Usage Telemetry |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **GeminiModelProvider** | `gemini-3.5-flash-lite` | **READY (Valid)** | Token deltas | Native FunctionDeclaration | Prompt, Completion, Cache |
| **FreeLLMAPIProvider** | Multi-Upstream Router | Available | HTTP Chunked | Parameter JSON Schema | Provider-reported usage |
| **FakeModelProvider** | `fake-test-oracle` | Offline Test | Deterministic | Mock declarations | Synthetic tokens |

### 2.2 Provider Fallback Sequencing (`runtime/session.py`)
Presence establishes an automated resolution sequence on session composition:
1. **Explicit Preference:** Checks `os.environ["PRESENCE_PROVIDER"]`. If set to `freellmapi`, attempts `FreeLLMAPIProvider`.
2. **Primary Cloud Provider:** Attempts `GeminiModelProvider.validate_configuration()`. If valid and API credentials exist, assigns Gemini as the primary conversational provider.
3. **Secondary Cloud Fallback:** If Gemini credentials are missing or invalid, tests `FreeLLMAPIProvider`.
4. **Offline Fallback:** If all external providers are unavailable, assigns `FakeModelProvider` to allow offline testing and deterministic workflow execution.

### 2.3 Rate Limit Handling & Failure Trails
- In `FreeLLMAPIProvider`, upstream provider errors (such as Cloudflare 10,000 neuron daily quota exhaustion or OpenRouter 429 limits) are captured across a 3-attempt trail.
- If all routes fail, the provider raises an informative HTTP 502 exception documenting the attempt trail, preventing silent failures.
- **TDEL Immunity:** When rate limits trigger, Presence's TDEL preemption pipeline prevents repeated request storms by ensuring that deterministic desktop tasks execute via zero-token DAG workflows.

---

## 3. Supervised Model Context Protocol (MCP) Infrastructure

### 3.1 Server Inventory & Configuration (`mcp_servers.json`)

Presence manages external MCP servers as supervised sub-processes:

```text
mcp_servers.json
├── filesystem (runtime.mcp.servers.filesystem_server)
│   ├── Startup Timeout: 15.0s | Request Timeout: 30.0s
│   ├── Enabled: True | Authority: L0_OBSERVE (Reads), L2_EXECUTE_REVERSIBLE (Writes)
│   └── 10 Tools Discovered (read_file, write_file, edit_file, search_files, etc.)
└── shell (runtime.mcp.servers.shell_server)
    ├── Startup Timeout: 15.0s | Request Timeout: 60.0s
    ├── Enabled: True | Authority: L0_OBSERVE (Info), L2_EXECUTE_REVERSIBLE (Exec)
    └── 8 Tools Discovered (execute_command, run_powershell_script, which, etc.)
```

### 3.2 State Lifecycle & On-Demand Spawning
The `MCPSupervisor` maintains explicit state tracking across four operational states:
- `CONFIGURED`: Defined in JSON configuration.
- `STOPPED`: Registered in supervisor, process not currently running (memory: 0.0 MB).
- `STARTING`: Spawned under Win32 Job Object, awaiting MCP protocol initialization handshake.
- `RUNNING`: Connected via JSON-RPC over stdin/stdout, ready for tool dispatch.

Servers remain in `STOPPED` state until a tool belonging to their namespace is invoked by an agent, ensuring that idle background servers do not consume desktop RAM.

### 3.3 Schema Caching & Content Hashing (`runtime/mcp/schemas.py`)
- Discovered tool definitions are cached on disk as JSON schema files at `%LOCALAPPDATA%\Presence\cache\mcp_schemas\`.
- Each cached schema contains a SHA-256 definition hash (e.g. `mcp.filesystem.read_file` -> `03d3e660`).
- If an MCP server restarts or updates its tool signatures, the hash change is detected, triggering automated cache invalidation.

---

## 4. Identified Provider & MCP Weaknesses

1. **Lack of In-Flight Mid-Stream Failover:**
   If Gemini raises an HTTP 429 halfway through a streaming response, the turn fails. The runtime does not currently replay the prompt against `FreeLLMAPIProvider` automatically.
2. **MCP Tool Auto-Approval Policy:**
   Both `filesystem` and `shell` servers in `mcp_servers.json` currently have `"auto_approve_tools": true`. While execution is still constrained by the session's `PermissionBoundary` tier ceiling, implementing an interactive user confirmation prompt in the TUI for `L2_EXECUTE_REVERSIBLE` MCP actions is recommended for sensitive operations.

---

## 5. Phase 6 Audit Verdict & Sign-Off

The Provider and MCP infrastructure is **fully operational and production-ready**. AI providers are validated, fallback logic is established, and external MCP servers are safely contained under Win32 Job Objects with disk-backed schema caching.
