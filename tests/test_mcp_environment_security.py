"""Tests for MCP Environment Security Policies (Phase 2B.1)
Verifies Policy A (Configuration Override Restrictions) and Policy B (Child Environment Construction).
Ensures zero credential leakage, rejection of dangerous injection overrides, and preservation of required Windows variables.
"""

import os
import sys
import unittest
from unittest.mock import patch

from runtime.mcp.client import (
    validate_server_environment_overrides,
    build_isolated_environment,
    sanitize_search_path,
    DISALLOWED_OVERRIDE_KEYS,
    FORBIDDEN_ENV_KEYWORDS,
)


class TestMCPEnvironmentSecurity(unittest.TestCase):
    """Adversarial and functional test suite for MCP environment isolation."""

    def test_policy_a_disallows_path_and_binary_manipulation(self):
        """Verifies Policy A rejects attempts to override PATH, PATHEXT, COMSPEC, etc."""
        dangerous_overrides = {
            "PATH": "C:\\malicious\\bin;C:\\Windows",
            "PATHEXT": ".EXE;.BAT;.VBS",
            "COMSPEC": "C:\\malicious\\cmd.exe",
            "SystemRoot": "C:\\malicious\\windows",
            "windir": "C:\\malicious\\windows",
        }
        filtered = validate_server_environment_overrides(dangerous_overrides, strict=False)
        self.assertEqual(filtered, {}, "All binary path manipulation overrides must be rejected.")

        with self.assertRaises(ValueError):
            validate_server_environment_overrides({"PATH": "C:\\malicious"}, strict=True)

    def test_policy_a_disallows_dll_and_library_injection(self):
        """Verifies Policy A rejects APPINIT_DLLS, LD_PRELOAD, and dynamic linker overrides."""
        dll_injections = {
            "APPINIT_DLLS": "evil.dll",
            "LD_PRELOAD": "/lib/evil.so",
            "LD_LIBRARY_PATH": "/opt/evil",
            "DYLD_INSERT_LIBRARIES": "/usr/lib/evil.dylib",
        }
        filtered = validate_server_environment_overrides(dll_injections, strict=False)
        self.assertEqual(filtered, {}, "All dynamic library injection variables must be rejected.")

    def test_policy_a_disallows_language_runtime_hijacking(self):
        """Verifies Policy A rejects PYTHONPATH, NODE_OPTIONS, CLASSPATH, etc."""
        runtime_hijacking = {
            "PYTHONPATH": "C:\\malicious\\python_modules",
            "PYTHONHOME": "C:\\malicious\\python",
            "NODE_OPTIONS": "--require evil.js",
            "RUBYOPT": "-r evil",
            "CLASSPATH": "C:\\malicious\\classes",
        }
        filtered = validate_server_environment_overrides(runtime_hijacking, strict=False)
        self.assertEqual(filtered, {}, "All language runtime code injection variables must be rejected.")

    def test_policy_a_disallows_profile_and_temp_redirection(self):
        """Verifies Policy A rejects redirection of USERPROFILE, LOCALAPPDATA, TEMP, etc."""
        profile_redirections = {
            "USERPROFILE": "C:\\Users\\Attacker",
            "LOCALAPPDATA": "C:\\Users\\Attacker\\AppData\\Local",
            "TEMP": "C:\\malicious_temp",
            "TMP": "C:\\malicious_temp",
        }
        filtered = validate_server_environment_overrides(profile_redirections, strict=False)
        self.assertEqual(filtered, {}, "All profile and temporary directory redirections must be rejected.")

    def test_policy_a_permits_benign_server_specific_variables(self):
        """Verifies Policy A permits benign custom server configurations."""
        benign = {
            "SERVER_PORT": "8080",
            "LOG_LEVEL": "DEBUG",
            "DATA_DIRECTORY": "C:\\Projects\\Data",
            "MAX_CONNECTIONS": "10",
        }
        filtered = validate_server_environment_overrides(benign, strict=True)
        self.assertEqual(filtered, benign, "Benign server variables must be preserved.")

    def test_policy_b_gemini_and_provider_keys_excluded(self):
        """Verifies Policy B excludes all LLM provider keys and credentials from child environment."""
        test_parent_env = {
            "SYSTEMROOT": r"C:\Windows",
            "PATH": r"C:\Windows\System32;C:\Windows",
            "TEMP": r"C:\Temp",
            "GEMINI_API_KEY": "AIzaSySecretGeminiKey123",
            "GOOGLE_API_KEY": "AIzaSySecretGoogleKey456",
            "ANTHROPIC_API_KEY": "sk-ant-api03-secret",
            "OPENAI_API_KEY": "sk-proj-secret-token",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "DATABASE_PASSWORD": "supersecretpassword",
        }

        with patch.dict(os.environ, test_parent_env, clear=True):
            child_env = build_isolated_environment()

            # Ensure no secrets leak
            for secret_var in [
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "AWS_SECRET_ACCESS_KEY",
                "DATABASE_PASSWORD",
            ]:
                self.assertNotIn(secret_var, child_env)

            # Ensure no key in child_env contains any forbidden keyword
            for key in child_env:
                key_upper = key.upper()
                for kw in FORBIDDEN_ENV_KEYWORDS:
                    self.assertNotIn(kw, key_upper, f"Forbidden keyword '{kw}' found in child key '{key}'")

    def test_policy_b_preserves_required_windows_runtime_variables(self):
        """Verifies Policy B preserves essential Windows system variables required for process boot."""
        test_parent_env = {
            "SYSTEMROOT": r"C:\Windows",
            "SYSTEMDRIVE": "C:",
            "WINDIR": r"C:\Windows",
            "PATH": r"C:\Windows\System32;C:\Windows",
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "TEMP": r"C:\Users\Tester\AppData\Local\Temp",
            "TMP": r"C:\Users\Tester\AppData\Local\Temp",
            "USERPROFILE": r"C:\Users\Tester",
            "LOCALAPPDATA": r"C:\Users\Tester\AppData\Local",
            "APPDATA": r"C:\Users\Tester\AppData\Roaming",
            "OS": "Windows_NT",
            "PROCESSOR_ARCHITECTURE": "AMD64",
            "NUMBER_OF_PROCESSORS": "8",
            "COMSPEC": r"C:\Windows\System32\cmd.exe",
            "USERNAME": "Tester",
            "RANDOM_PARENT_SECRET_VAR": "secret123",
            "RANDOM_PARENT_APP_VAR": "appdata456",
        }

        with patch.dict(os.environ, test_parent_env, clear=True):
            child_env = build_isolated_environment()

            # Check that all essential Windows variables are present
            self.assertEqual(child_env.get("SYSTEMROOT"), r"C:\Windows")
            self.assertEqual(child_env.get("SYSTEMDRIVE"), "C:")
            self.assertEqual(child_env.get("OS"), "Windows_NT")
            self.assertEqual(child_env.get("USERNAME"), "Tester")

            # Check that arbitrary unapproved parent variables are NOT inherited
            self.assertNotIn("RANDOM_PARENT_SECRET_VAR", child_env)
            self.assertNotIn("RANDOM_PARENT_APP_VAR", child_env)

    def test_policy_b_path_sanitization(self):
        """Verifies that PATH is sanitized to exclude relative directories or empty segments."""
        raw_path = r"C:\Windows\System32;.;C:\Windows;..\evil;C:\Python312;;"
        clean_path = sanitize_search_path(raw_path)
        segments = clean_path.split(os.pathsep)

        self.assertIn(r"C:\Windows\System32", segments)
        self.assertIn(r"C:\Windows", segments)
        self.assertIn(r"C:\Python312", segments)
        self.assertNotIn(".", segments)
        self.assertNotIn("..", segments)
        self.assertNotIn(r"..\evil", segments)
        self.assertNotIn("", segments)

    def test_policy_b_merges_approved_overrides_and_rejects_dangerous(self):
        """Verifies Policy B merges safe overrides but rejects dangerous or credential overrides."""
        overrides = {
            "SAFE_SETTING": "ENABLED",
            "APPINIT_DLLS": "injected.dll",  # Must be rejected
            "GEMINI_API_KEY": "malicious_key",  # Must be rejected
            "CUSTOM_FLAG": "1",
        }
        child_env = build_isolated_environment(custom_env=overrides)

        self.assertEqual(child_env.get("SAFE_SETTING"), "ENABLED")
        self.assertEqual(child_env.get("CUSTOM_FLAG"), "1")
        self.assertNotIn("APPINIT_DLLS", child_env)
        self.assertNotIn("GEMINI_API_KEY", child_env)


if __name__ == "__main__":
    unittest.main()
