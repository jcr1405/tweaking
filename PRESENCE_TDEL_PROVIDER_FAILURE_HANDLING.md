# PRESENCE — TDEL PROVIDER FAILURE & NOVEL PLANNING REPAIR
## Schema Enforcement, Bounded 1-Turn Repair, and Safe Outage Degradation

**Document ID:** PRESENCE-TDEL-FAILSAFE-2026-03  
**Target Subsystem:** `runtime.task_planning.planner` & `runtime.task_planning.service`  
**Integration Status:** Production Ready  
**Date:** 2026-11-20  
**Verification:** Automated Fault Injection & Provider Outage Tests Passing  

---

## 1. Executive Summary

During live desktop operations with free-tier or rate-limited model providers (such as OpenRouter, Cloudflare Workers AI 10k neuron limits, or Gemini API free tiers), provider failures and transient quota exhaustion are inevitable. As documented in user runtime traces:

```text
Presence: Error - FreeLLMAPI generation error (HTTP 502): {"error":{"message":"All 3 routed attempt(s) failed with upstream provider errors (daily_quota_exhausted x2, upstream_error x1)... Cloudflare API error 429: you have used up your daily free allocation of 10,000 neurons..."}}
```

The initial audit revealed that when the provider failed during novel request planning, the system previously fell back to a hardcoded diagnostic stub. This created severe operational hazards: a user requesting database maintenance or network clustering would suddenly have diagnostic commands executed against their local GPU or Python environment!

This remediation introduces:
1. **Strict JSON Schema Validation** on all LLM-generated DAG plans.
2. **Deterministic 1-Turn Bounded Repair** (`_attempt_bounded_repair`) with error feedback.
3. **Safe Provider Outage Degradation (`PlanStatus.PLAN_UNAVAILABLE`)**: Completely eliminating irrelevant fallback stubs.

---

## 2. Planning Failure States and Flow

```
                      Novel Complex User Request
                                 │
                                 ▼
                     TaskDecompositionPlanner
                                 │
                     Call LLM Provider (Turn 1)
                                 │
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
         [Provider Success]              [Provider Error]
                 │                     (HTTP 502 / 429 / Outage)
                 │                               │
                 ▼                               ▼
       Validate Plan Schema             Set PLAN_UNAVAILABLE
                 │                               │
        ┌────────┴────────┐                      │
        ▼                 ▼                      │
     [Valid]          [Invalid]                  │
        │                 │                      │
        │                 ▼                      │
        │       Bounded Repair (Turn 2)          │
        │                 │                      │
        │        ┌────────┴────────┐             │
        │        ▼                 ▼             │
        │     [Valid]          [Invalid]         │
        │        │                 │             │
        └────────┼─────────────────┘             │
                 │                               │
                 ▼                               ▼
         Plan Status: READY             Plan Status: UNAVAILABLE
                 │                               │
                 ▼                               ▼
          WorkflowEngine                 Informative Message
       (Deterministic Run)             Delivered via TEXT_DELTA
```

---

## 3. Implementation Details

### 3.1 Strict Schema Validation and Bounded Repair

In `runtime/task_planning/planner.py`, LLM responses are parsed and validated against `TaskPlanValidator`. If validation fails, exactly **one** repair attempt is permitted to protect token usage:

```python
try:
    plan_dict = json.loads(content)
except json.JSONDecodeError as e:
    logger.warning("Novel plan output was not valid JSON: %s. Attempting bounded repair.", e)
    plan_dict = self._attempt_bounded_repair(prompt, content, str(e))
    if not plan_dict:
        return self._make_unavailable_plan(prompt, conversation_id, "Model failed to generate valid JSON plan.")

# Validate structural integrity, DAG acyclicity, and required fields
plan = self._dict_to_plan(plan_dict, prompt, conversation_id, complexity)
try:
    TaskPlanValidator.validate(plan)
    plan.status = PlanStatus.PLAN_READY
    return plan
except TaskPlanValidationError as ve:
    logger.warning("Plan validation failed: %s. Attempting bounded repair.", ve)
    repaired_dict = self._attempt_bounded_repair(prompt, json.dumps(plan_dict), str(ve))
    if repaired_dict:
        try:
            repaired_plan = self._dict_to_plan(repaired_dict, prompt, conversation_id, complexity)
            TaskPlanValidator.validate(repaired_plan)
            repaired_plan.status = PlanStatus.PLAN_READY
            return repaired_plan
        except Exception:
            pass
    return self._make_unavailable_plan(prompt, conversation_id, f"Plan schema validation failed: {ve}")
```

### 3.2 Provider Outage Handling

When the model provider raises a `RuntimeError`, connection failure, or quota exhaustion, `planner.py` immediately logs the incident and constructs an explicit `PLAN_UNAVAILABLE` record:

```python
except Exception as e:
    logger.error("LLM Provider failed during task decomposition: %s", e)
    return self._make_unavailable_plan(prompt, conversation_id, f"Provider error: {e}")

def _make_unavailable_plan(self, prompt: str, conversation_id: str, reason: str) -> TaskPlan:
    """Safely yields PLAN_UNAVAILABLE without hallucinating tasks or running random stubs."""
    return TaskPlan(
        plan_id=f"plan_unavail_{uuid.uuid4().hex[:8]}",
        title="Plan Unavailable",
        user_goal=prompt,
        description=reason,
        complexity=ComplexityCategory.COMPLEX,
        originating_conversation_id=conversation_id,
        nodes=[],
        status=PlanStatus.PLAN_UNAVAILABLE,
        failure_policy=FailurePolicy.FAIL_FAST,
        max_parallel_nodes=1,
    )
```

### 3.3 Session Preemption Integration

In `AgentSession.execute_turn()`, if `plan_res.get("status") == "PLAN_UNAVAILABLE"`, execution stops safely before invoking any processes or tools:

```python
if plan_res.get("status") == "PLAN_UNAVAILABLE":
    msg = f"Presence: Unable to generate execution plan ({plan_res.get('message', 'Provider unavailable')}). Please retry shortly or execute specific commands directly."
    yield TurnEvent(event_type="TEXT_DELTA", payload={"text": msg})
    yield TurnEvent(event_type="TURN_COMPLETED", payload={"final_response": msg})
    return
```

---

## 4. Verification Evidence

In `tests/test_tdel_preemption_and_production.py::test_preemption_path_g_provider_outage`:
- Provider was configured with `MockModelProvider(mode="failure")` simulating an upstream HTTP 502 / Cloudflare 429 quota exhaustion.
- A complex novel request (`"Diagnose unknown server kernel bug and configure custom cluster topology"`) was submitted.
- Test verified:
  1. `PLAN_UNAVAILABLE` status safely emitted.
  2. Zero execution processes spawned.
  3. Informative text delta delivered cleanly to user.
  4. Session completed turn without crash.

---

## 5. Architectural References
- Planner: [runtime/task_planning/planner.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/task_planning/planner.py)
- Validator: [runtime/task_planning/validator.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/task_planning/validator.py)
- Test Case: [tests/test_tdel_preemption_and_production.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/tests/test_tdel_preemption_and_production.py)
