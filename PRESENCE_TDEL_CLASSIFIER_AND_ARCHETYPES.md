# PRESENCE — TDEL CLASSIFIER AND DETERMINISTIC ARCHETYPES
## Generalization Heuristics, Discussion Disambiguation, and Zero-Token Execution Archetypes

**Document ID:** PRESENCE-TDEL-ARCHETYPES-2026-02  
**Target Subsystem:** `runtime.task_planning.complexity` & `runtime.task_planning.planner`  
**Integration Status:** Production Ready  
**Date:** 2026-11-20  
**Verification:** Automated E2E Test Suite (All Archetypes Passing)  

---

## 1. Executive Summary

A critical limitation identified during the TDEL audit was the narrowness of the initial deterministic planner. Previously, only a single hard-coded pattern existed, leaving most standard desktop administration, software discovery, repository auditing, and diagnostic requests dependent on provider LLM streaming. When the provider suffered rate limits or outages, these requests failed.

This remediation implements two major capabilities:
1. **Intelligent Discussion vs. Execution Disambiguation** in `ComplexityClassifier`, preventing discussion queries ("explain X", "what is Y") from triggering heavy DAG planning.
2. **Five Deterministic Production Archetypes** in `TaskDecompositionPlanner` covering the most frequent companion workloads, executing deterministically with **zero LLM tokens burned**.

---

## 2. Complexity Classifier Disambiguation Heuristics

In `runtime/task_planning/complexity.py`, the classifier has been enhanced with semantic guards to separate requests that require active desktop execution from purely conversational requests:

```python
DISCUSSION_PREFIXES = (
    "explain", "what is", "what are", "how do i", "how does", "describe",
    "tell me about", "can you explain", "summarize", "help me understand",
    "discuss", "compare", "difference between", "why is", "why does"
)

EXECUTION_OVERRIDE_VERBS = (
    "run", "execute", "start", "launch", "kill", "stop", "delete", "remove",
    "install", "uninstall", "write", "modify", "patch", "build", "compile",
    "deploy", "test", "audit", "inventory", "scan", "benchmark"
)

@classmethod
def is_discussion_only(cls, prompt: str) -> bool:
    """Returns True if the prompt is asking for conceptual explanations rather than action."""
    lower = prompt.lower().strip()
    starts_with_discussion = any(lower.startswith(p) for p in cls.DISCUSSION_PREFIXES)
    if not starts_with_discussion:
        return False
    # If the user says 'Explain how to run X and then execute it', execution overrides
    has_execution_intent = any(re.search(rf"\b{re.escape(v)}\b", lower) for v in cls.EXECUTION_OVERRIDE_VERBS)
    return not has_execution_intent
```

---

## 3. The Five Deterministic Production Archetypes

Each archetype produces a validated, multi-phase `TaskPlan` with explicit dependencies, phase groupings, execution types (`PROCESS`, `TOOL`, `VERIFY`, `AGGREGATE`), and completion criteria.

| Archetype | Domain | Trigger Pattern | Phases & Concurrency | Zero-Token? |
| :--- | :--- | :--- | :--- | :--- |
| **Archetype A** | Software Inventory | Installed apps, software packages, start menu | 4 Phases, Max Parallel: 2 | Yes (0 Tokens) |
| **Archetype B** | MCP Discovery | MCP servers, desktop application tools | 4 Phases, Max Parallel: 4 | Yes (0 Tokens) |
| **Archetype C** | Repo Inspection | Codebase, git status, manifests, architecture | 3 Phases, Max Parallel: 2 | Yes (0 Tokens) |
| **Archetype D** | Build & Test | Build, test suite, pytest, compile verification | 4 Phases, Max Parallel: 1 | Yes (0 Tokens) |
| **Archetype E** | System & AI Diag | GPU, CUDA, drivers, Python runtime conflicts | 3 Phases, Max Parallel: 2 | Yes (0 Tokens) |

### Archetype A: Desktop Software Inventory & Categorization
- **Phase 1 (`query_installed_apps`)**: Executes PowerShell `Get-StartApps | Select-Object -First 35 | ConvertTo-Json` under `ProcessManager` supervision.
- **Phase 2 (`normalize_and_categorize_apps`)**: Normalizes application footprints into functional groupings (Development, Utilities, Productivity, System).
- **Phase 3 (`verify_inventory_records`)**: Verifies non-empty output with `assertion_type: NON_EMPTY`.
- **Phase 4 (`synthesize_inventory_report`)**: Aggregates records into a formatted Markdown deliverable in `ArtifactStore`.

### Archetype B: Desktop Applications & MCP Discovery
- **Phase 1 (`inspect_local_apps`)**: Scans local applications to find candidate companion tools.
- **Phase 2 (Parallel Search)**: Spawns concurrent `presence.web_search` tool queries (up to 4 parallel workers) for verified MCP servers (e.g., Chrome, DaVinci, Blender, Discord, GitHub).
- **Phase 3 (`verify_search_results`)**: Verifies non-empty results from discovery nodes.
- **Phase 4 (`synthesize_mcp_report`)**: Synthesizes a unified Markdown MCP guide with repository URLs and setup steps.

### Archetype C: Repository Structure & Architecture Audit
- **Phase 1 (Parallel Inspection)**: Executes `git status --short; git log -n 5 --oneline` concurrently with file manifest inspection.
- **Phase 2 (`verify_repo_manifests`)**: Validates repository metadata presence.
- **Phase 3 (`synthesize_repo_report`)**: Compiles structural architecture, branch health, and dependency overview into an architecture report.

### Archetype D: Automated Build & Test Pipeline
- **Phase 1 (`verify_project_config`)**: Validates pytest test framework and Python environment.
- **Phase 2 (`execute_test_suite`)**: Executes automated test runner under sandboxed process controls.
- **Phase 3 (`verify_test_deliverable`)**: Verifies test runner output completeness.
- **Phase 4 (`synthesize_build_report`)**: Formats test pass/fail metrics and execution timings into a formal test deliverable.

### Archetype E: System & AI Diagnostics
- **Phase 1 (Parallel Hardware/Runtime Query)**: Queries Python runtime build (`python --version; pip --version`) and hardware controllers (`Get-CimInstance Win32_VideoController`).
- **Phase 2 (`verify_diagnostics_collected`)**: Validates non-empty hardware and runtime telemetry.
- **Phase 3 (`synthesize_diagnostics_report`)**: Produces an AI compute environment report with driver versions and status flags.

---

## 4. Word-Boundary Regex Specificity

To eliminate false-positive triggering (such as `"report"` matching `"repo"`, or `"infrastructure"` matching `"structure"`), all archetype triggers employ strict word-boundary regular expressions:

```python
# Specific word boundaries avoid false substring matches
is_repo = bool(
    re.search(r"\b(repo|repository|codebase)\b", lower) and 
    re.search(r"\b(inspect|audit|architecture|structure)\b", lower)
)

is_inventory = bool(re.search(r"\b(inventory|installed apps?|installed desktop|list apps|installed software)\b", lower))
is_diag = bool(
    re.search(r"\b(diagnostic|diagnose|diagnostics|gpu|cuda|pytorch)\b", lower) and 
    re.search(r"\b(environment|bottleneck|conflict|driver|drivers)\b", lower)
)
```

---

## 5. Architectural References
- Classifier: [runtime/task_planning/complexity.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/task_planning/complexity.py)
- Planner Archetypes: [runtime/task_planning/planner.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/task_planning/planner.py)
- Integration Tests: [tests/test_tdel_preemption_and_production.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/tests/test_tdel_preemption_and_production.py)
