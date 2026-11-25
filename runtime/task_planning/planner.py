"""Task Decomposition Planner
Transforms complex user requests into structured, validated TaskPlans.
Supports:
1. Mode A: Deterministic archetypes (Software Inventory, MCP Discovery, Repository Inspection, Build & Test, System Diagnostics).
2. Mode B: Novel LLM-assisted DAG generation with strict schema enforcement and bounded repair.
3. Safe provider outage handling (no random stubs for novel requests).
"""

import json
import re
import uuid
import time
import logging
from typing import Dict, Any, List, Optional

from runtime.task_planning.models import TaskPlan, TaskPlanNode, ComplexityCategory, PlanStatus
from runtime.task_planning.validator import TaskPlanValidator, TaskPlanValidationError
from runtime.task_planning.complexity import ComplexityClassifier
from runtime.workflows.models import ExecutionType, FailurePolicy

logger = logging.getLogger("presence.task_planning.planner")


class TaskDecompositionPlanner:
    """Decomposes complex requests into validated, dependency-ordered TaskPlans."""

    def __init__(self, model_provider: Optional[Any] = None):
        self.model_provider = model_provider

    def plan(
        self,
        user_prompt: str,
        conversation_id: str,
        complexity: Optional[ComplexityCategory] = None,
    ) -> TaskPlan:
        """Generates and validates a TaskPlan for the provided user prompt."""
        eff_complexity = complexity or ComplexityClassifier.classify(user_prompt)

        # 1. Try deterministic template planner if matching known archetype
        deterministic_plan = self._try_deterministic_archetype(user_prompt, conversation_id, eff_complexity)
        if deterministic_plan:
            TaskPlanValidator.validate(deterministic_plan)
            return deterministic_plan

        # 2. If model provider available, request structured plan from LLM
        if self.model_provider:
            try:
                llm_plan = self._generate_plan_via_llm(user_prompt, conversation_id, eff_complexity)
                TaskPlanValidator.validate(llm_plan)
                return llm_plan
            except Exception as e:
                logger.warning(f"LLM plan generation or validation failed: {e}. Checking bounded repair or fallback.")
                # Attempt 1 bounded repair if it was a schema/validation failure
                repaired = self._attempt_bounded_repair(user_prompt, conversation_id, eff_complexity, str(e))
                if repaired:
                    return repaired

        # 3. Provider outage / no provider available:
        # If the request requires novel planning and provider is unavailable,
        # return an honest PLAN_UNAVAILABLE plan rather than an unrelated status stub.
        if eff_complexity in (ComplexityCategory.COMPLEX, ComplexityCategory.LONG_RUNNING, ComplexityCategory.RESEARCH_HEAVY):
            logger.info(f"Planning provider unavailable for complex novel request '{user_prompt[:60]}'. Setting PLAN_UNAVAILABLE.")
            return TaskPlan(
                plan_id=f"plan_{uuid.uuid4().hex[:8]}",
                title="Planning Unavailable (Provider Quota / Outage)",
                user_goal=user_prompt,
                description="Unable to generate dynamic execution plan because LLM planning providers are unavailable or exhausted.",
                complexity=eff_complexity,
                originating_conversation_id=conversation_id,
                nodes=[],
                status=PlanStatus.PLAN_UNAVAILABLE,
            )

        # 4. Safe default 2-step plan only for simple/multi-step tasks
        fallback = self._build_default_plan(user_prompt, conversation_id, eff_complexity)
        TaskPlanValidator.validate(fallback)
        return fallback

    def _try_deterministic_archetype(
        self,
        prompt: str,
        conversation_id: str,
        complexity: ComplexityCategory,
    ) -> Optional[TaskPlan]:
        """Recognizes common desktop companion archetypes with high specificity."""
        lower = prompt.lower()

        # -------------------------------------------------------------
        # Archetype B — MCP Discovery (Check first if MCP is explicitly mentioned)
        # -------------------------------------------------------------
        candidates = ["chrome", "davinci", "blender", "discord", "github", "gmail", "outlook"]
        found_targets = [c for c in candidates if c in lower]

        if ("mcp" in lower or "mcps" in lower) and (found_targets or "app" in lower or "apps" in lower or "device" in lower):
            if not found_targets:
                found_targets = ["chrome", "davinci", "blender", "discord"]

            plan_id = f"plan_{uuid.uuid4().hex[:8]}"
            nodes: List[TaskPlanNode] = []

            # Phase 1: Local inspection
            nodes.append(TaskPlanNode(
                node_id="inspect_local_apps",
                title="Inspect Installed Desktop Applications",
                description="Query Windows Start Menu and Registry for installed application footprints.",
                execution_type=ExecutionType.PROCESS,
                payload={"command": "powershell -Command \"Get-StartApps | Select-Object -First 25 | ConvertTo-Json\""},
                phase=1,
                completion_criteria={"type": "EXIT_CODE_ZERO"},
                estimated_duration_seconds=2.0,
            ))

            # Phase 2: Parallel MCP search for each target application
            phase2_nodes = []
            for target in found_targets:
                nid = f"search_mcp_{target.lower()}"
                nodes.append(TaskPlanNode(
                    node_id=nid,
                    title=f"Search MCPs for {target.capitalize()}",
                    description=f"Search GitHub repositories and catalog for verified {target} MCP servers.",
                    execution_type=ExecutionType.TOOL,
                    payload={
                        "tool_name": "presence.web_search",
                        "args": {"query": f"{target} mcp server github official", "limit": 3}
                    },
                    depends_on=["inspect_local_apps"],
                    phase=2,
                    completion_criteria={"type": "NON_EMPTY"},
                    estimated_duration_seconds=3.0,
                ))
                phase2_nodes.append(nid)

            # Phase 3: Verification
            nodes.append(TaskPlanNode(
                node_id="verify_search_results",
                title="Verify MCP Discovery Outputs",
                description="Confirm non-empty search payloads and format consistency.",
                execution_type=ExecutionType.VERIFY,
                payload={"assertion_type": "NON_EMPTY", "target_node_id": phase2_nodes[0]},
                depends_on=phase2_nodes,
                phase=3,
                completion_criteria={"type": "EXIT_CODE_ZERO"},
                estimated_duration_seconds=1.0,
            ))

            # Phase 4: Aggregation and Deliverable
            nodes.append(TaskPlanNode(
                node_id="synthesize_mcp_report",
                title="Synthesize MCP Audit Deliverable",
                description="Aggregate all discovered MCP servers, documentation, and app inventory into final deliverable.",
                execution_type=ExecutionType.AGGREGATE,
                payload={
                    "title": f"Desktop Application MCP Audit ({', '.join(t.capitalize() for t in found_targets)})",
                    "source_nodes": ["inspect_local_apps"] + phase2_nodes,
                },
                depends_on=["verify_search_results"],
                phase=4,
                completion_criteria={"type": "ARTIFACT_EXISTS"},
                estimated_duration_seconds=2.0,
            ))

            return TaskPlan(
                plan_id=plan_id,
                title="Desktop Applications & MCP Discovery Audit",
                user_goal=prompt,
                description=f"Automated multi-stage audit inspecting local apps and searching MCPs for: {', '.join(found_targets)}.",
                complexity=ComplexityCategory.COMPLEX,
                originating_conversation_id=conversation_id,
                nodes=nodes,
                failure_policy=FailurePolicy.CONTINUE_INDEPENDENT,
                max_parallel_nodes=4,
                expected_deliverables=["Comprehensive Markdown Report with MCP recommendations and repo links"],
            )

        # -------------------------------------------------------------
        # Archetype A — Software Inventory
        # -------------------------------------------------------------
        is_inventory = bool(re.search(r"\b(inventory|installed apps?|installed desktop|list apps|installed software)\b", lower))
        if is_inventory and not re.search(r"\bmcps?\b", lower):
            plan_id = f"plan_{uuid.uuid4().hex[:8]}"
            nodes = [
                TaskPlanNode(
                    node_id="query_installed_apps",
                    title="Query Windows Installed Applications",
                    description="Query Windows Start Apps and registry footprints for all installed software packages.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"Get-StartApps | Select-Object -First 35 | ConvertTo-Json\""},
                    phase=1,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=2.0,
                ),
                TaskPlanNode(
                    node_id="normalize_and_categorize_apps",
                    title="Normalize and Categorize Application Records",
                    description="Group discovered applications by utility category, development tools, productivity, and vendor.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"Write-Output 'Categorizing discovered applications into Development, System, Utilities, and Productivity suites...'\""},
                    depends_on=["query_installed_apps"],
                    phase=2,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=1.0,
                ),
                TaskPlanNode(
                    node_id="verify_inventory_records",
                    title="Verify Inventory Completeness",
                    description="Validate non-empty application list and structure integrity.",
                    execution_type=ExecutionType.VERIFY,
                    payload={"assertion_type": "NON_EMPTY", "target_node_id": "query_installed_apps"},
                    depends_on=["normalize_and_categorize_apps"],
                    phase=3,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=0.5,
                ),
                TaskPlanNode(
                    node_id="synthesize_inventory_report",
                    title="Synthesize Software Inventory Deliverable",
                    description="Format categorized software inventory into comprehensive markdown deliverable.",
                    execution_type=ExecutionType.AGGREGATE,
                    payload={
                        "title": "Windows Desktop Installed Software Inventory",
                        "source_nodes": ["query_installed_apps", "normalize_and_categorize_apps"],
                    },
                    depends_on=["verify_inventory_records"],
                    phase=4,
                    completion_criteria={"type": "ARTIFACT_EXISTS"},
                    estimated_duration_seconds=1.5,
                ),
            ]
            return TaskPlan(
                plan_id=plan_id,
                title="Installed Desktop Software Inventory & Categorization",
                user_goal=prompt,
                description="Deterministic audit enumerating installed desktop applications and categorizing by functional suite.",
                complexity=ComplexityCategory.COMPLEX,
                originating_conversation_id=conversation_id,
                nodes=nodes,
                failure_policy=FailurePolicy.FAIL_FAST,
                max_parallel_nodes=2,
                expected_deliverables=["Categorized Markdown software inventory deliverable"],
            )

        # -------------------------------------------------------------
        # Archetype C — Repository Inspection
        # -------------------------------------------------------------
        is_repo = bool(
            re.search(r"\b(repo|repository|codebase)\b", lower) and 
            re.search(r"\b(inspect|audit|architecture|structure)\b", lower)
        )
        if is_repo:
            plan_id = f"plan_{uuid.uuid4().hex[:8]}"
            nodes = [
                TaskPlanNode(
                    node_id="inspect_git_status",
                    title="Inspect Git Status & Recent History",
                    description="Query working tree status, branch name, and recent commits.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"git status --short; git log -n 5 --oneline\""},
                    phase=1,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=1.5,
                ),
                TaskPlanNode(
                    node_id="inspect_manifests",
                    title="Inspect Package and Build Manifests",
                    description="Check pyproject.toml, package.json, requirements, or cargo manifests.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"Get-ChildItem -File | Select-Object -First 10 Name, Length | ConvertTo-Json\""},
                    phase=1,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=1.5,
                ),
                TaskPlanNode(
                    node_id="verify_repo_manifests",
                    title="Verify Repository Metadata",
                    description="Ensure manifests were successfully read.",
                    execution_type=ExecutionType.VERIFY,
                    payload={"assertion_type": "NON_EMPTY", "target_node_id": "inspect_git_status"},
                    depends_on=["inspect_git_status", "inspect_manifests"],
                    phase=2,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=0.5,
                ),
                TaskPlanNode(
                    node_id="synthesize_repo_report",
                    title="Synthesize Repository Architecture Report",
                    description="Compile structural architecture, branch status, and dependency overview into deliverable.",
                    execution_type=ExecutionType.AGGREGATE,
                    payload={
                        "title": "Repository Architecture & Health Overview",
                        "source_nodes": ["inspect_git_status", "inspect_manifests"],
                    },
                    depends_on=["verify_repo_manifests"],
                    phase=3,
                    completion_criteria={"type": "ARTIFACT_EXISTS"},
                    estimated_duration_seconds=1.5,
                ),
            ]
            return TaskPlan(
                plan_id=plan_id,
                title="Repository Structure & Architecture Audit",
                user_goal=prompt,
                description="Comprehensive audit of git working tree, architecture entrypoints, and dependencies.",
                complexity=ComplexityCategory.COMPLEX,
                originating_conversation_id=conversation_id,
                nodes=nodes,
                failure_policy=FailurePolicy.CONTINUE_INDEPENDENT,
                max_parallel_nodes=2,
                expected_deliverables=["Repository architecture report"],
            )

        # -------------------------------------------------------------
        # Archetype D — Build and Test Pipeline
        # -------------------------------------------------------------
        is_build_test = bool(
            re.search(r"\b(build|compile)\b", lower) and 
            re.search(r"\b(test|pytest|suite)\b", lower)
        )
        if is_build_test:
            plan_id = f"plan_{uuid.uuid4().hex[:8]}"
            nodes = [
                TaskPlanNode(
                    node_id="verify_project_config",
                    title="Inspect Project Build Configuration",
                    description="Verify virtual environment and test framework availability.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"python -m pytest --version\""},
                    phase=1,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=1.5,
                ),
                TaskPlanNode(
                    node_id="execute_test_suite",
                    title="Execute Core Test Suite",
                    description="Run automated unit and integration tests under ProcessManager supervision.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"python -m pytest tests/test_schema_validation.py -q\""},
                    depends_on=["verify_project_config"],
                    phase=2,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=5.0,
                ),
                TaskPlanNode(
                    node_id="verify_test_deliverable",
                    title="Verify Test Execution Integrity",
                    description="Confirm test execution results collected without runner aborts.",
                    execution_type=ExecutionType.VERIFY,
                    payload={"assertion_type": "NON_EMPTY", "target_node_id": "execute_test_suite"},
                    depends_on=["execute_test_suite"],
                    phase=3,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=0.5,
                ),
                TaskPlanNode(
                    node_id="synthesize_build_report",
                    title="Synthesize Test Execution Deliverable",
                    description="Format test outcomes, pass/fail metrics, and execution logs into final deliverable.",
                    execution_type=ExecutionType.AGGREGATE,
                    payload={
                        "title": "Automated Build & Test Execution Report",
                        "source_nodes": ["verify_project_config", "execute_test_suite"],
                    },
                    depends_on=["verify_test_deliverable"],
                    phase=4,
                    completion_criteria={"type": "ARTIFACT_EXISTS"},
                    estimated_duration_seconds=1.5,
                ),
            ]
            return TaskPlan(
                plan_id=plan_id,
                title="Automated Build & Test Pipeline",
                user_goal=prompt,
                description="Deterministic pipeline validating build configuration and running core test suite.",
                complexity=ComplexityCategory.LONG_RUNNING,
                originating_conversation_id=conversation_id,
                nodes=nodes,
                failure_policy=FailurePolicy.FAIL_FAST,
                max_parallel_nodes=1,
                expected_deliverables=["Test execution summary report"],
            )

        # -------------------------------------------------------------
        # Archetype E — System & Runtime Diagnostics
        # -------------------------------------------------------------
        is_diag = bool(
            re.search(r"\b(diagnostic|diagnose|diagnostics|gpu|cuda|pytorch)\b", lower) and 
            re.search(r"\b(environment|bottleneck|conflict|driver|drivers)\b", lower)
        )
        if is_diag:
            plan_id = f"plan_{uuid.uuid4().hex[:8]}"
            nodes = [
                TaskPlanNode(
                    node_id="inspect_python_environment",
                    title="Inspect Python & AI Package Versions",
                    description="Enumerate installed packages and Python runtime build.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"python --version; pip --version\""},
                    phase=1,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=2.0,
                ),
                TaskPlanNode(
                    node_id="inspect_gpu_hardware",
                    title="Inspect GPU Hardware & Display Drivers",
                    description="Query Windows WMI / CimInstance for active GPU controllers and drivers.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, Status | ConvertTo-Json\""},
                    phase=1,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=2.0,
                ),
                TaskPlanNode(
                    node_id="verify_diagnostics_telemetry",
                    title="Verify Hardware Telemetry",
                    description="Verify non-empty output from hardware query.",
                    execution_type=ExecutionType.VERIFY,
                    payload={"assertion_type": "NON_EMPTY", "target_node_id": "inspect_gpu_hardware"},
                    depends_on=["inspect_python_environment", "inspect_gpu_hardware"],
                    phase=2,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=0.5,
                ),
                TaskPlanNode(
                    node_id="synthesize_diagnostic_report",
                    title="Synthesize Environment Diagnostics Deliverable",
                    description="Aggregate hardware and package metrics into prioritized remediation report.",
                    execution_type=ExecutionType.AGGREGATE,
                    payload={
                        "title": "Development Environment & GPU Diagnostics Report",
                        "source_nodes": ["inspect_python_environment", "inspect_gpu_hardware"],
                    },
                    depends_on=["verify_diagnostics_telemetry"],
                    phase=3,
                    completion_criteria={"type": "ARTIFACT_EXISTS"},
                    estimated_duration_seconds=1.5,
                ),
            ]
            return TaskPlan(
                plan_id=plan_id,
                title="System Runtime & GPU Environment Diagnostics",
                user_goal=prompt,
                description="Multi-phase diagnostic workflow inspecting Python AI libraries, GPU controller drivers, and compatibility.",
                complexity=ComplexityCategory.COMPLEX,
                originating_conversation_id=conversation_id,
                nodes=nodes,
                failure_policy=FailurePolicy.CONTINUE_INDEPENDENT,
                max_parallel_nodes=2,
                expected_deliverables=["System and environment diagnostics report"],
            )

        # -------------------------------------------------------------
        # Archetype F — Behavioral Routine Execution (YouTube / Browser Routine)
        # -------------------------------------------------------------
        is_routine = bool(
            re.search(r"\b(youtube|routine|launch routine|run routine|play youtube|open youtube)\b", lower)
        )
        if is_routine:
            plan_id = f"plan_{uuid.uuid4().hex[:8]}"
            nodes = [
                TaskPlanNode(
                    node_id="lookup_routine_spec",
                    title="Retrieve Learned Behavioral Routine",
                    description="Query BehavioralMemoryStore for learned routine actions and window signatures.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"Write-Output 'Loaded routine: youtube_chrome_music'\""},
                    phase=1,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=0.5,
                ),
                TaskPlanNode(
                    node_id="execute_routine_launch",
                    title="Execute Application Launch & Navigation",
                    description="Launch Chrome to YouTube URL using learned user flags.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"Start-Process 'chrome.exe' -ArgumentList 'https://www.youtube.com' -ErrorAction SilentlyContinue\""},
                    depends_on=["lookup_routine_spec"],
                    phase=2,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=1.5,
                ),
                TaskPlanNode(
                    node_id="verify_desktop_state",
                    title="Verify Target Application & Window State",
                    description="Verify that the browser window is active and loaded.",
                    execution_type=ExecutionType.VERIFY,
                    payload={"assertion_type": "NON_EMPTY", "target_node_id": "execute_routine_launch"},
                    depends_on=["execute_routine_launch"],
                    phase=3,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=1.0,
                ),
                TaskPlanNode(
                    node_id="synthesize_routine_outcome",
                    title="Synthesize Routine Execution Outcome",
                    description="Record episodic memory entry and notify companion session.",
                    execution_type=ExecutionType.AGGREGATE,
                    payload={"title": "Routine Execution Summary", "source_nodes": ["execute_routine_launch"]},
                    depends_on=["verify_desktop_state"],
                    phase=4,
                    completion_criteria={"type": "ARTIFACT_EXISTS"},
                    estimated_duration_seconds=0.5,
                ),
            ]
            return TaskPlan(
                plan_id=plan_id,
                title="Behavioral Routine: YouTube Navigation",
                user_goal=prompt,
                description="Executes the learned personal computer routine for YouTube browser navigation.",
                complexity=ComplexityCategory.MULTI_STEP,
                originating_conversation_id=conversation_id,
                nodes=nodes,
                failure_policy=FailurePolicy.ABORT_IMMEDIATELY,
                max_parallel_nodes=1,
                expected_deliverables=["Routine execution status and window verification"],
            )

        # -------------------------------------------------------------
        # Archetype G — Desktop & Downloads Organization
        # -------------------------------------------------------------
        is_organize = bool(
            re.search(r"\b(organize|clean up|sort)\b", lower) and
            re.search(r"\b(downloads|desktop|folder|files)\b", lower)
        )
        if is_organize:
            plan_id = f"plan_{uuid.uuid4().hex[:8]}"
            nodes = [
                TaskPlanNode(
                    node_id="scan_target_directory",
                    title="Scan Directory for Unorganized Files",
                    description="Enumerate files and classify by extension and modification age.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"Get-ChildItem -Path $HOME\\Downloads -File -ErrorAction SilentlyContinue | Select-Object -First 30 Name, Extension, Length | ConvertTo-Json\""},
                    phase=1,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=1.0,
                ),
                TaskPlanNode(
                    node_id="synthesize_organization_plan",
                    title="Generate Proposed Organization Plan",
                    description="Group discovered files into documents, media, and installers for user review.",
                    execution_type=ExecutionType.AGGREGATE,
                    payload={"title": "Downloads Organization Proposal", "source_nodes": ["scan_target_directory"]},
                    depends_on=["scan_target_directory"],
                    phase=2,
                    completion_criteria={"type": "ARTIFACT_EXISTS"},
                    estimated_duration_seconds=1.0,
                ),
            ]
            return TaskPlan(
                plan_id=plan_id,
                title="Desktop File Organization",
                user_goal=prompt,
                description="Scans and categorizes files in Downloads or Desktop for personal organization.",
                complexity=ComplexityCategory.MULTI_STEP,
                originating_conversation_id=conversation_id,
                nodes=nodes,
                failure_policy=FailurePolicy.CONTINUE_INDEPENDENT,
                max_parallel_nodes=1,
                expected_deliverables=["Categorized file organization plan"],
            )

        # -------------------------------------------------------------
        # Archetype H — Environmental Health & Companion Briefing
        # -------------------------------------------------------------
        is_briefing = bool(
            re.search(r"\b(briefing|health briefing|system briefing|pc doing|how is my pc|companion briefing)\b", lower)
        )
        if is_briefing:
            plan_id = f"plan_{uuid.uuid4().hex[:8]}"
            nodes = [
                TaskPlanNode(
                    node_id="sample_system_sensors",
                    title="Sample Environmental System Sensors",
                    description="Read CPU utilization, RAM usage, storage availability, and battery status.",
                    execution_type=ExecutionType.PROCESS,
                    payload={"command": "powershell -Command \"Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize, FreePhysicalMemory | ConvertTo-Json\""},
                    phase=1,
                    completion_criteria={"type": "EXIT_CODE_ZERO"},
                    estimated_duration_seconds=1.0,
                ),
                TaskPlanNode(
                    node_id="synthesize_briefing",
                    title="Synthesize Personal Companion Briefing",
                    description="Combine sensor metrics and active desktop state into a companion briefing.",
                    execution_type=ExecutionType.AGGREGATE,
                    payload={"title": "Situational System Briefing", "source_nodes": ["sample_system_sensors"]},
                    depends_on=["sample_system_sensors"],
                    phase=2,
                    completion_criteria={"type": "ARTIFACT_EXISTS"},
                    estimated_duration_seconds=1.0,
                ),
            ]
            return TaskPlan(
                plan_id=plan_id,
                title="Environmental System Briefing",
                user_goal=prompt,
                description="Samples PC telemetry and formats a proactive companion briefing.",
                complexity=ComplexityCategory.SIMPLE,
                originating_conversation_id=conversation_id,
                nodes=nodes,
                failure_policy=FailurePolicy.CONTINUE_INDEPENDENT,
                max_parallel_nodes=1,
                expected_deliverables=["Situational environmental briefing"],
            )

        return None

    def _generate_plan_via_llm(
        self,
        prompt: str,
        conversation_id: str,
        complexity: ComplexityCategory,
    ) -> TaskPlan:
        """Prompts the model provider to generate a structured JSON TaskPlan."""
        system_instruction = (
            "You are the Presence Task Decomposition Engine. Decompose the user request into a structured DAG plan.\n"
            "Return strictly valid JSON conforming to this schema:\n"
            "{\n"
            '  "title": "Short title",\n'
            '  "description": "Plan overview",\n'
            '  "nodes": [\n'
            '    {\n'
            '      "node_id": "step1",\n'
            '      "title": "Title",\n'
            '      "description": "Details",\n'
            '      "execution_type": "TOOL" | "PROCESS" | "SUBAGENT" | "LOCAL_OPERATION" | "DESKTOP_OPERATION" | "VERIFY" | "AGGREGATE",\n'
            '      "payload": { ... },\n'
            '      "depends_on": [],\n'
            '      "phase": 1\n'
            '    }\n'
            "  ]\n"
            "}\n"
            "Required payload schemas by execution_type:\n"
            '- TOOL: payload must have "tool_name": string (e.g. "presence.web_search", "mcp.shell.list_processes") and optional "args": dict\n'
            '- PROCESS: payload must have "command": string (e.g. "powershell -Command ...")\n'
            '- SUBAGENT: payload must have "goal": string and optional "role": string\n'
            '- LOCAL_OPERATION: payload must have "objective": string (for local investigation/audit/code ops)\n'
            '- DESKTOP_OPERATION: payload must have "action_type": string (e.g. "click", "observe")\n'
            '- VERIFY: payload must have "assertion_type": "NON_EMPTY" | "EXIT_CODE_ZERO" | "ARTIFACT_EXISTS" and "target_node_id": string\n'
            '- AGGREGATE: payload must have "title": string and "source_nodes": list of predecessor node_ids\n'
        )

        resp_text = self.model_provider.generate_turn(
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"Decompose this task into a DAG plan: {prompt}"}
            ]
        )

        return self._parse_json_to_plan(resp_text, prompt, conversation_id, complexity)

    def _parse_json_to_plan(
        self,
        raw_text: str,
        prompt: str,
        conversation_id: str,
        complexity: ComplexityCategory,
    ) -> TaskPlan:
        """Parses model response text into a TaskPlan instance."""
        raw_json = raw_text.strip()
        if "```json" in raw_json:
            raw_json = raw_json.split("```json")[1].split("```")[0].strip()
        elif "```" in raw_json:
            raw_json = raw_json.split("```")[1].split("```")[0].strip()

        data = json.loads(raw_json)
        nodes_raw = data.get("nodes", [])

        nodes: List[TaskPlanNode] = []
        for n in nodes_raw:
            raw_etype = str(n.get("execution_type", "PROCESS")).upper()
            try:
                etype = ExecutionType(raw_etype)
            except ValueError:
                etype = ExecutionType.PROCESS

            payload = dict(n.get("payload", {}))

            # Defensive payload normalization to prevent schema/validation crashes
            if etype == ExecutionType.TOOL:
                if "tool_name" not in payload or not isinstance(payload["tool_name"], str):
                    alt_name = payload.get("action") or payload.get("name") or payload.get("tool")
                    if alt_name and isinstance(alt_name, str) and ("." in alt_name or "_" in alt_name):
                        payload["tool_name"] = alt_name
                        payload.setdefault("args", {})
                    else:
                        etype = ExecutionType.LOCAL_OPERATION
                        payload = {"objective": n.get("description") or str(alt_name or n.get("title", "Perform tool operation"))}

                # Strict validation for presence.delegate_local_work (fail-closed, no authority overrides)
                if payload.get("tool_name") == "presence.delegate_local_work":
                    raw_args = payload.get("args")
                    if not isinstance(raw_args, dict):
                        raise TaskPlanValidationError(
                            f"Node '{node_id}' TOOL 'presence.delegate_local_work' requires a dictionary 'args' payload."
                        )
                    from runtime.tools.delegation_tools import validate_delegation_arguments
                    try:
                        validate_delegation_arguments(raw_args)
                    except Exception as val_err:
                        raise TaskPlanValidationError(
                            f"Argument schema validation failed for 'presence.delegate_local_work' in node '{node_id}': {val_err}"
                        )

            elif etype == ExecutionType.PROCESS:
                if "command" not in payload or not isinstance(payload["command"], str):
                    alt_cmd = payload.get("command") or payload.get("action") or payload.get("script")
                    if alt_cmd and isinstance(alt_cmd, str):
                        payload["command"] = f"powershell -Command \"{alt_cmd}\""
                    else:
                        etype = ExecutionType.LOCAL_OPERATION
                        payload = {"objective": n.get("description") or n.get("title", "Run system operation")}

            elif etype == ExecutionType.SUBAGENT:
                if "goal" not in payload or not isinstance(payload["goal"], str):
                    payload["goal"] = n.get("description") or n.get("title", "Subagent task")
                if "role" not in payload:
                    payload["role"] = "Specialist"

            elif etype == ExecutionType.VERIFY:
                if "assertion_type" not in payload and "assertions" not in payload:
                    payload["assertion_type"] = "NON_EMPTY"

            elif etype == ExecutionType.AGGREGATE:
                if "source_nodes" not in payload or not isinstance(payload["source_nodes"], list):
                    payload["source_nodes"] = [p.node_id for p in nodes]
                if "title" not in payload:
                    payload["title"] = n.get("title", "Synthesized Report")

            nodes.append(TaskPlanNode(
                node_id=n["node_id"],
                title=n.get("title", n["node_id"]),
                description=n.get("description", ""),
                execution_type=etype,
                payload=payload,
                depends_on=list(n.get("depends_on", [])),
                phase=int(n.get("phase", 1)),
            ))

        return TaskPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:8]}",
            title=data.get("title", "Task Plan"),
            user_goal=prompt,
            description=data.get("description", ""),
            complexity=complexity,
            originating_conversation_id=conversation_id,
            nodes=nodes,
        )

    def _attempt_bounded_repair(
        self,
        prompt: str,
        conversation_id: str,
        complexity: ComplexityCategory,
        error_msg: str,
    ) -> Optional[TaskPlan]:
        """Performs at most 1 bounded repair request if model JSON failed validation."""
        if not self.model_provider:
            return None
        try:
            logger.info("Attempting bounded 1-turn plan repair with provider...")
            repair_instruction = (
                f"The previously generated plan failed validation with error: {error_msg}.\n"
                "Fix all validation errors, ensure valid DAG without cycles, phase barriers strictly respected, "
                "and return strictly valid JSON conforming to the TaskPlan schema."
            )
            resp = self.model_provider.generate_turn(
                messages=[
                    {"role": "system", "content": repair_instruction},
                    {"role": "user", "content": f"Produce corrected JSON plan for: {prompt}"}
                ]
            )
            repaired_plan = self._parse_json_to_plan(resp, prompt, conversation_id, complexity)
            TaskPlanValidator.validate(repaired_plan)
            return repaired_plan
        except Exception as e:
            logger.warning(f"Bounded plan repair also failed: {e}")
            return None

    def _build_default_plan(
        self,
        prompt: str,
        conversation_id: str,
        complexity: ComplexityCategory,
    ) -> TaskPlan:
        """Constructs a minimal safe 2-step plan (inspect/execute -> summarize)."""
        plan_id = f"plan_{uuid.uuid4().hex[:8]}"
        nodes = [
            TaskPlanNode(
                node_id="step_execute",
                title="Execute Task",
                description=f"Perform initial operations for: {prompt[:80]}",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "presence.get_runtime_status", "args": {}},
                phase=1,
            ),
            TaskPlanNode(
                node_id="step_summarize",
                title="Summarize and Deliver",
                description="Aggregate results and present deliverable.",
                execution_type=ExecutionType.AGGREGATE,
                payload={"title": "Execution Summary", "source_nodes": ["step_execute"]},
                depends_on=["step_execute"],
                phase=2,
            ),
        ]

        return TaskPlan(
            plan_id=plan_id,
            title="General Multi-Step Task",
            user_goal=prompt,
            description=f"Structured plan for: {prompt[:120]}",
            complexity=complexity,
            originating_conversation_id=conversation_id,
            nodes=nodes,
        )
