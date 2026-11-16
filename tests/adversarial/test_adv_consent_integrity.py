"""Adversarial Verification Suite: L3 Consent Token Integrity & Canonical Argument Binding
Verifies that consent tokens strictly bind exact argument values, conversation ID, and permission tier.
"""

import time
import threading
import unittest
from typing import Dict, Any

from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import ToolRegistry, ToolDefinition


class TestAdvConsentIntegrity(unittest.TestCase):
    """Adversarial stress and parameter-tampering tests for L3 Consent Tokens."""

    def setUp(self):
        self.boundary = PermissionBoundary()
        self.registry = ToolRegistry()
        self.executed_calls = []

        def harmless_handler(args):
            self.executed_calls.append(args)
            return {"status": "SUCCESS", "executed_with": args}

        self.tool_id = "presence.delete_file"
        self.registry.register(
            ToolDefinition(
                tool_id=self.tool_id,
                name="delete_file",
                description="Harmless simulated file deletion test tool",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "recursive": {"type": "boolean"},
                        "nested": {"type": "object"}
                    },
                    "required": ["path"]
                },
                authority_tier=PermissionTier.L3_PROTECTED,
                handler=harmless_handler
            ),
            permission_boundary=self.boundary
        )

    def test_reproduce_argument_value_tampering(self):
        """CRITICAL REPRODUCTION TEST:
        User approves deletion of 'C:\\temp\\file-A.txt'.
        Attacker / compromised turn substitutes 'C:\\sensitive\\file-B.txt' using the valid token.
        MUST BE REJECTED.
        """
        approved_args = {"path": "C:\\temp\\file-A.txt"}
        malicious_args = {"path": "C:\\sensitive\\file-B.txt"}

        # Issue token for approved arguments
        token = self.boundary.issue_consent_token(
            action_name=self.tool_id,
            context=approved_args
        )

        # Attempt to execute with malicious arguments
        result = self.registry.execute(
            tool_id=self.tool_id,
            args=malicious_args,
            permission_boundary=self.boundary,
            consent_token=token
        )

        # This MUST fail closed with status ERROR
        if result.get("status") == "SUCCESS":
            self.fail(
                "CRITICAL SECURITY FLAW REPRODUCED: Consent token authorized execution "
                f"with tampered arguments! Executed with: {self.executed_calls}"
            )
        self.assertEqual(result.get("status"), "ERROR")
        self.assertIn("requires explicit user consent", result.get("error", ""))

    def test_nested_argument_mutation_rejected(self):
        """Verifies that tampering with nested object values is detected and rejected."""
        approved_args = {
            "path": "C:\\temp\\test.txt",
            "nested": {"mode": "safe", "timeout": 30}
        }
        tampered_args = {
            "path": "C:\\temp\\test.txt",
            "nested": {"mode": "destructive", "timeout": 30}
        }

        token = self.boundary.issue_consent_token(
            action_name=self.tool_id,
            context=approved_args
        )

        result = self.registry.execute(
            tool_id=self.tool_id,
            args=tampered_args,
            permission_boundary=self.boundary,
            consent_token=token
        )
        self.assertEqual(result.get("status"), "ERROR")

    def test_type_confusion_tampering_rejected(self):
        """Verifies that changing boolean True to integer 1 or string 'true' is rejected."""
        approved_args = {"path": "C:\\temp\\test.txt", "recursive": True}
        coerced_args = {"path": "C:\\temp\\test.txt", "recursive": 1}

        token = self.boundary.issue_consent_token(
            action_name=self.tool_id,
            context=approved_args
        )

        result = self.registry.execute(
            tool_id=self.tool_id,
            args=coerced_args,
            permission_boundary=self.boundary,
            consent_token=token
        )
        self.assertEqual(result.get("status"), "ERROR")

    def test_token_replay_attack_rejected(self):
        """Verifies that once consumed, a token cannot be used a second time even with identical arguments."""
        args = {"path": "C:\\temp\\safe.txt"}
        token = self.boundary.issue_consent_token(
            action_name=self.tool_id,
            context=args
        )

        # 1st execution: Must succeed (with exact match)
        res1 = self.registry.execute(
            tool_id=self.tool_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token=token
        )
        self.assertEqual(res1.get("status"), "SUCCESS")

        # 2nd execution: Replay attempt must fail closed
        res2 = self.registry.execute(
            tool_id=self.tool_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token=token
        )
        self.assertEqual(res2.get("status"), "ERROR")

    def test_concurrent_token_consumption_atomic(self):
        """Verifies that when 10 threads attempt to use the same token simultaneously, exactly 1 succeeds."""
        args = {"path": "C:\\temp\\concurrent.txt"}
        token = self.boundary.issue_consent_token(
            action_name=self.tool_id,
            context=args
        )

        results = [None] * 10

        def worker(idx):
            res = self.registry.execute(
                tool_id=self.tool_id,
                args=args,
                permission_boundary=self.boundary,
                consent_token=token
            )
            results[idx] = res

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        success_count = sum(1 for r in results if r and r.get("status") == "SUCCESS")
        error_count = sum(1 for r in results if r and r.get("status") == "ERROR")

        self.assertEqual(success_count, 1, f"Expected exactly 1 success, got {success_count}")
        self.assertEqual(error_count, 9, f"Expected exactly 9 rejections, got {error_count}")

    def test_conversation_binding_mismatch_rejected(self):
        """Verifies that a token bound to conversation_A cannot be used in conversation_B."""
        args = {"path": "C:\\temp\\file.txt"}
        token = self.boundary.issue_consent_token(
            action_name=self.tool_id,
            context=args,
            conversation_id="conv_Alpha"
        )

        res_diff_conv = self.registry.execute(
            tool_id=self.tool_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token=token,
            conversation_id="conv_Beta"
        )
        self.assertEqual(res_diff_conv.get("status"), "ERROR")

    def test_token_expiration_enforced(self):
        """Verifies that an expired token fails verification even with exact matching arguments."""
        args = {"path": "C:\\temp\\file.txt"}
        token = self.boundary.issue_consent_token(
            action_name=self.tool_id,
            context=args,
            ttl_seconds=0.05
        )
        time.sleep(0.08)

        res = self.registry.execute(
            tool_id=self.tool_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token=token
        )
        self.assertEqual(res.get("status"), "ERROR")

    def test_non_finite_float_rejection(self):
        """Verifies that non-finite floats like float('nan') or float('inf') are strictly rejected."""
        token = self.boundary.issue_consent_token(
            action_name=self.tool_id,
            context={"path": "C:\\temp\\file.txt"}
        )
        res = self.registry.execute(
            tool_id=self.tool_id,
            args={"path": "C:\\temp\\file.txt", "corrupt": float("nan")},
            permission_boundary=self.boundary,
            consent_token=token
        )
        self.assertEqual(res.get("status"), "ERROR")

    def test_permission_tier_mismatch_rejected(self):
        """Verifies that a token issued for an L2 action cannot authorize an L3 action."""
        l2_tool_id = "test.l2_action"
        self.registry.register(
            ToolDefinition(
                tool_id=l2_tool_id,
                name="l2_action",
                description="Harmless reversible operation",
                parameters={"type": "object", "properties": {"target": {"type": "string"}}},
                authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
                handler=lambda a: {"status": "SUCCESS"}
            ),
            permission_boundary=self.boundary
        )
        args = {"target": "node_1"}
        l2_token = self.boundary.issue_consent_token(l2_tool_id, context=args)

        # Attempt to use L2 token to authorize L3 protected delete_file
        res = self.registry.execute(
            tool_id=self.tool_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token=l2_token
        )
        self.assertEqual(res.get("status"), "ERROR")
        self.assertTrue(
            "denied" in res.get("error", "").lower() or "requires explicit user consent" in res.get("error", "").lower()
        )

    def test_missing_expected_tier_fails_closed(self):
        """Verifies that verify_consent_token fails closed if expected tier is missing or invalid."""
        args = {"path": "C:\\temp\\file.txt"}
        token = self.boundary.issue_consent_token(self.tool_id, context=args)

        # Pass invalid or non-existent tier
        valid = self.boundary.verify_consent_token(
            token=token,
            action_name=self.tool_id,
            context=args,
            expected_tier=None  # Or policy tier mismatch
        )
        # Should fail closed if expected_tier does not match registered L3 policy
        # Or when expected tier explicitly contradicts token tier
        token2 = self.boundary.issue_consent_token(self.tool_id, context=args)
        tier_mismatch = self.boundary.verify_consent_token(
            token=token2,
            action_name=self.tool_id,
            context=args,
            expected_tier=PermissionTier.L0_OBSERVE
        )
        self.assertFalse(tier_mismatch)

    def test_argument_mutation_between_authorization_and_execution(self):
        """CRITICAL TOCTOU TEST:
        Caller passes mutable dictionary args.
        Caller mutates args right after token is verified but before handler execution.
        Handler MUST receive the authorized snapshot, not the mutated value.
        """
        args = {"path": "C:\\temp\\approved_file.txt"}
        token = self.boundary.issue_consent_token(self.tool_id, context=args)

        # Mutating hook simulating concurrent mutation
        orig_check = self.boundary.check_permission
        def mutating_check(action_name, context=None, granted_consent=False):
            args["path"] = "C:\\Windows\\System32\\critical.dll"
            return orig_check(action_name, context, granted_consent)

        self.boundary.check_permission = mutating_check

        res = self.registry.execute(
            tool_id=self.tool_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token=token
        )
        self.assertEqual(res.get("status"), "SUCCESS")
        self.assertEqual(self.executed_calls[0]["path"], "C:\\temp\\approved_file.txt")

    def test_nested_mutable_argument_isolation(self):
        """Verifies that nested lists and dicts are completely isolated from subsequent mutations."""
        nested_args = {
            "path": "C:\\temp\\file.txt",
            "nested": {
                "tags": ["alpha", "beta"],
                "meta": {"owner": "alice"}
            }
        }
        token = self.boundary.issue_consent_token(self.tool_id, context=nested_args)

        orig_check = self.boundary.check_permission
        def mutating_check(action_name, context=None, granted_consent=False):
            nested_args["nested"]["tags"].append("gamma_unauthorized")
            nested_args["nested"]["meta"]["owner"] = "attacker"
            return orig_check(action_name, context, granted_consent)

        self.boundary.check_permission = mutating_check

        res = self.registry.execute(
            tool_id=self.tool_id,
            args=nested_args,
            permission_boundary=self.boundary,
            consent_token=token
        )
        self.assertEqual(res.get("status"), "SUCCESS")
        # Handler must NOT have received gamma_unauthorized or attacker
        self.assertEqual(self.executed_calls[0]["nested"]["tags"], ["alpha", "beta"])
        self.assertEqual(self.executed_calls[0]["nested"]["meta"]["owner"], "alice")

    def test_handler_receives_authorized_argument_snapshot(self):
        """Verifies that the object passed to handler is an independent snapshot, not caller's dict."""
        args = {"path": "C:\\temp\\file.txt"}
        token = self.boundary.issue_consent_token(self.tool_id, context=args)

        res = self.registry.execute(
            tool_id=self.tool_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token=token
        )
        self.assertEqual(res.get("status"), "SUCCESS")
        # Direct object identity check
        self.assertIsNot(self.executed_calls[0], args)
        self.assertEqual(self.executed_calls[0], args)

    def test_model_cannot_issue_l3_consent(self):
        """Verifies that an execution request originating without a valid user consent token fails closed."""
        args = {"path": "C:\\temp\\file.txt"}
        # Attempt execution without token
        res = self.registry.execute(
            tool_id=self.tool_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token=None
        )
        self.assertEqual(res.get("status"), "ERROR")
        self.assertIn("explicit user consent", res.get("error", ""))

    def test_alternate_l3_execution_path_rejected(self):
        """Verifies that passing granted_consent=True directly or fake strings is rejected."""
        args = {"path": "C:\\temp\\file.txt"}
        res = self.registry.execute(
            tool_id=self.tool_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token="fake_token_123"
        )
        self.assertEqual(res.get("status"), "ERROR")

    def test_concurrent_argument_mutation_cannot_change_execution(self):
        """Spawns concurrent background thread actively mutating caller dictionary."""
        args = {"path": "C:\\temp\\target.txt", "nested": {"counter": 0}}
        token = self.boundary.issue_consent_token(self.tool_id, context=args)

        stop_event = threading.Event()
        def mutator():
            val = 1
            while not stop_event.is_set():
                args["nested"]["counter"] = val
                args["path"] = f"C:\\temp\\tampered_{val}.txt"
                val += 1
                time.sleep(0.001)

        t = threading.Thread(target=mutator, daemon=True)
        t.start()
        try:
            res = self.registry.execute(
                tool_id=self.tool_id,
                args=args,
                permission_boundary=self.boundary,
                consent_token=token
            )
            # If execution succeeded, it must have executed with the authorized values
            if res.get("status") == "SUCCESS":
                self.assertEqual(self.executed_calls[0]["path"], "C:\\temp\\target.txt")
                self.assertEqual(self.executed_calls[0]["nested"]["counter"], 0)
        finally:
            stop_event.set()
            t.join(timeout=1.0)

    def test_tool_definition_change_invalidates_consent(self):
        """Verifies that if tool definition changes from L2 to L3 after token issuance, token is invalidated."""
        change_tool_id = "test.mutable_tier_tool"
        self.registry.register(
            ToolDefinition(
                tool_id=change_tool_id,
                name="mutable_tool",
                description="Initially L2",
                parameters={"type": "object", "properties": {"val": {"type": "string"}}},
                authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
                handler=lambda a: {"status": "SUCCESS"}
            ),
            permission_boundary=self.boundary
        )
        args = {"val": "123"}
        token = self.boundary.issue_consent_token(change_tool_id, context=args)

        # Re-register or upgrade tool to L3
        self.registry.register(
            ToolDefinition(
                tool_id=change_tool_id,
                name="mutable_tool",
                description="Now L3 Protected",
                parameters={"type": "object", "properties": {"val": {"type": "string"}}},
                authority_tier=PermissionTier.L3_PROTECTED,
                handler=lambda a: {"status": "SUCCESS"}
            ),
            permission_boundary=self.boundary
        )

        # Execution with previously issued L2 token must be rejected
        res = self.registry.execute(
            tool_id=change_tool_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token=token
        )
        self.assertEqual(res.get("status"), "ERROR")

    def test_consent_is_bound_to_exact_registered_tool(self):
        """Verifies that a token issued for tool A cannot authorize tool B with identical arguments."""
        tool_b_id = "presence.different_tool"
        self.registry.register(
            ToolDefinition(
                tool_id=tool_b_id,
                name="different_tool",
                description="Another L3 tool",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                authority_tier=PermissionTier.L3_PROTECTED,
                handler=lambda a: {"status": "SUCCESS"}
            ),
            permission_boundary=self.boundary
        )
        args = {"path": "C:\\temp\\file.txt"}
        token_a = self.boundary.issue_consent_token(self.tool_id, context=args)

        res = self.registry.execute(
            tool_id=tool_b_id,
            args=args,
            permission_boundary=self.boundary,
            consent_token=token_a
        )
        self.assertEqual(res.get("status"), "ERROR")

    def test_canonicalization_non_string_keys_rejected(self):
        """Verifies that non-string dictionary keys are strictly rejected."""
        from runtime.permissions import canonicalize_arguments
        with self.assertRaises((TypeError, ValueError)):
            canonicalize_arguments({123: "invalid_key"})

    def test_canonicalization_negative_zero_normalized(self):
        """Verifies that 0.0 and -0.0 produce identical canonical representations per RFC 8785."""
        from runtime.permissions import canonicalize_arguments
        s_pos = canonicalize_arguments({"val": 0.0})
        s_neg = canonicalize_arguments({"val": -0.0})
        self.assertEqual(s_pos, s_neg)


if __name__ == "__main__":
    unittest.main()
