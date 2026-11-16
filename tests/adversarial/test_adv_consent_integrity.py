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


if __name__ == "__main__":
    unittest.main()
