# PRESENCE — TDEL PERMISSION TIER REMEDIATION & SECURITY AUDIT
## Authorization Segregation, Read-Only Inspection Hardening, and Reversible Execution Controls

**Document ID:** PRESENCE-TDEL-SEC-2026-04  
**Target Subsystem:** `runtime.tools.plan_tools` & `runtime.permissions`  
**Integration Status:** Production Ready  
**Date:** 2026-11-20  
**Verification:** Automated Permission & Adversarial Tool Access Tests Passing  

---

## 1. Executive Summary & Forensic Vulnerability Analysis

During the TDEL audit, a critical authorization boundary violation was discovered in the tool registry:
`presence.manage_plan` was classified under `PermissionTier.L0_OBSERVE` (the read-only, non-mutating tier). However, its internal implementation accepted an `action="cancel"` parameter which:
- Aborted active background processes.
- Mutated the SQLite ledger state from `RUNNING` to `CANCELLED`.
- Evicted running workflow jobs from the `WorkflowScheduler`.

This violated the core security policy: **Tools marked `L0_OBSERVE` must be strictly idempotent and side-effect-free.** Under certain configurations, an unprivileged subagent or restricted companion mode could abort critical administrative tasks without triggering user consent or elevated tier verification.

---

## 2. Authorization Remediation Architecture

The tool interface was split into two strictly isolated tools with clear security boundaries:

```
                          Incoming Tool Invocation
                                     │
                     ┌───────────────┴───────────────┐
                     ▼                               ▼
          presence.manage_plan              presence.cancel_plan
                     │                               │
             Authority Check                 Authority Check
           [Tier: L0_OBSERVE]          [Tier: L2_EXECUTE_REVERSIBLE]
                     │                               │
             Allowed Actions:                Allowed Actions:
             - "get"                         - "cancel" (with reason)
             - "get_step"                            │
             - "list_steps"                          ▼
             - "get_results"                 Aborts Process & Mutates
                     │                       Ledger State to CANCELLED
                     ▼
           Strict Read-Only Query
         (Zero Mutation Permitted)
```

---

## 3. Implementation Code Comparison

### 3.1 Hardened `presence.manage_plan` (L0_OBSERVE)

In `runtime/tools/plan_tools.py`:

```python
@tool(
    name="presence.manage_plan",
    description="Inspect execution plans, node statuses, and results. Strictly read-only.",
    authority_level=PermissionTier.L0_OBSERVE,
)
def manage_plan(action: str, plan_id: str, step_id: Optional[str] = None) -> Dict[str, Any]:
    if action == "cancel":
        raise PermissionError(
            "Cancellation is a mutating action and cannot be performed via 'presence.manage_plan'. "
            "Use 'presence.cancel_plan' which requires L2_EXECUTE_REVERSIBLE authority."
        )
    if action == "get":
        plan = ledger.get_plan(plan_id)
        return plan.to_dict() if plan else {"error": "Plan not found"}
    elif action == "list_steps":
        plan = ledger.get_plan(plan_id)
        return {"nodes": [n.to_dict() for n in plan.nodes]} if plan else {"error": "Plan not found"}
    elif action == "get_step":
        node = ledger.get_node(plan_id, step_id)
        return node.to_dict() if node else {"error": "Node not found"}
    elif action == "get_results":
        return ledger.get_plan_results(plan_id)
    else:
        return {"error": f"Unknown read-only action: {action}"}
```

### 3.2 Dedicated `presence.cancel_plan` (L2_EXECUTE_REVERSIBLE)

```python
@tool(
    name="presence.cancel_plan",
    description="Cancel an active execution plan and abort all associated background tasks.",
    authority_level=PermissionTier.L2_EXECUTE_REVERSIBLE,
)
def cancel_plan(plan_id: str, reason: str = "User requested cancellation") -> Dict[str, Any]:
    plan = ledger.get_plan(plan_id)
    if not plan:
        return {"error": "Plan not found", "success": False}
    
    # Abort running tasks via WorkflowEngine
    cancelled = workflow_engine.cancel_workflow(plan_id, reason=reason)
    ledger.update_plan_status(plan_id, PlanStatus.CANCELLED)
    
    return {
        "plan_id": plan_id,
        "status": "CANCELLED",
        "reason": reason,
        "success": cancelled,
    }
```

---

## 4. Verification Evidence

In `tests/test_tdel_preemption_and_production.py::test_permission_tier_segregation`:
- Verified that `presence.manage_plan` has `authority_level == PermissionTier.L0_OBSERVE`.
- Calling `presence.manage_plan(action="cancel")` raises an explicit `PermissionError`.
- Verified that `presence.cancel_plan` has `authority_level == PermissionTier.L2_EXECUTE_REVERSIBLE`.
- Cancelling an active plan successfully aborts underlying tasks and updates ledger status to `CANCELLED`.

---

## 5. Architectural References
- Tool Definitions: [runtime/tools/plan_tools.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/tools/plan_tools.py)
- Permission Boundary: [runtime/permissions.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/runtime/permissions.py)
- Verification Suite: [tests/test_tdel_preemption_and_production.py](file:///c:/Users/chait/OneDrive/Documents/projects/tweaking/tests/test_tdel_preemption_and_production.py)
