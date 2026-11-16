"""Unit Tests for 4-Tier Authority Boundary Subsystem
"""

import unittest
from runtime.permissions import (
    PermissionBoundary,
    AuthorityLevel,
    ActionPolicy,
    PermissionDeniedError,
)


class TestPermissions(unittest.TestCase):

    def setUp(self):
        self.boundary = PermissionBoundary()

    def test_l0_observe_autonomous(self):
        result = self.boundary.check_permission("system.status")
        self.assertTrue(result.allowed)
        self.assertEqual(result.level, AuthorityLevel.L0_OBSERVE)

    def test_l2_reversible_autonomous(self):
        result = self.boundary.check_permission("config.write")
        self.assertTrue(result.allowed)
        self.assertEqual(result.level, AuthorityLevel.L2_EXECUTE_REVERSIBLE)

    def test_l3_protected_requires_consent(self):
        # Without explicit consent -> must fail
        result = self.boundary.check_permission("fs.delete_file", granted_consent=False)
        self.assertFalse(result.allowed)
        self.assertTrue(result.user_consent_required)

        # Calling authorize directly must raise PermissionDeniedError
        with self.assertRaises(PermissionDeniedError):
            self.boundary.authorize("fs.delete_file", granted_consent=False)

        # With explicit consent -> must succeed
        result_consented = self.boundary.check_permission("fs.delete_file", granted_consent=True)
        self.assertTrue(result_consented.allowed)

    def test_unregistered_action_fail_closed(self):
        result = self.boundary.check_permission("unregistered.malicious_cmd")
        self.assertFalse(result.allowed, "Unregistered actions must fail closed")

        with self.assertRaises(PermissionDeniedError):
            self.boundary.authorize("unregistered.malicious_cmd")

    def test_dynamic_consent_provider(self):
        def _mock_consent(action_name, ctx):
            return ctx.get("allow_flag", False)

        boundary_with_provider = PermissionBoundary(consent_provider=_mock_consent)
        # Context allows
        res_allowed = boundary_with_provider.check_permission(
            "fs.delete_file", context={"allow_flag": True}
        )
        self.assertTrue(res_allowed.allowed)

        # Context denies
        res_denied = boundary_with_provider.check_permission(
            "fs.delete_file", context={"allow_flag": False}
        )
        self.assertFalse(res_denied.allowed)


if __name__ == "__main__":
    unittest.main()
