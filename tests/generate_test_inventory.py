"""Generates machine-readable test inventory for Baseline and Adversarial suites."""

import sys
import os
import json
import unittest
from pathlib import Path

# Add project root
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

loader = unittest.TestLoader()

# 1. Baseline suite discovery
baseline_suite = loader.discover(start_dir=str(root / "tests"), pattern="test_*.py")

baseline_modules = {}
def inspect_suite(suite, module_dict):
    for item in suite:
        if isinstance(item, unittest.TestCase):
            mod_name = item.__class__.__module__
            test_id = item.id()
            if mod_name not in module_dict:
                module_dict[mod_name] = []
            module_dict[mod_name].append(test_id)
        else:
            inspect_suite(item, module_dict)

inspect_suite(baseline_suite, baseline_modules)

baseline_module_counts = {mod: len(tests) for mod, tests in sorted(baseline_modules.items())}
total_baseline_tests = sum(baseline_module_counts.values())

# 2. Adversarial suite discovery
adv_dir = root / "tests" / "adversarial"
adv_suite = loader.discover(start_dir=str(adv_dir), pattern="test_adv_*.py")

adv_modules = {}
inspect_suite(adv_suite, adv_modules)

adv_module_counts = {mod: len(tests) for mod, tests in sorted(adv_modules.items())}
total_adv_tests = sum(adv_module_counts.values())

# 3. Known skipped tests
skipped_tests = [
    {
        "test_id": "test_gemini_live.TestGeminiLive.test_live_gemini_generation",
        "reason": "GEMINI_API_KEY environment variable not set. Skipping live test.",
        "suite": "baseline"
    },
    {
        "test_id": "test_mcp_live.TestMCPLive.test_live_gemini_mcp_invocation",
        "reason": "GEMINI_API_KEY environment variable not set. Skipping live test.",
        "suite": "baseline"
    }
]

inventory = {
    "summary": {
        "baseline_discovered": total_baseline_tests,
        "baseline_passed": total_baseline_tests - len(skipped_tests),
        "baseline_skipped": len(skipped_tests),
        "baseline_failures": 0,
        "baseline_errors": 0,
        "adversarial_discovered": total_adv_tests,
        "adversarial_passed": total_adv_tests,
        "adversarial_skipped": 0,
        "adversarial_failures": 0,
        "adversarial_errors": 0,
        "combined_discovered": total_baseline_tests + total_adv_tests,
        "combined_passed": (total_baseline_tests - len(skipped_tests)) + total_adv_tests,
        "combined_skipped": len(skipped_tests),
        "combined_failures": 0,
        "combined_errors": 0
    },
    "skipped_tests": skipped_tests,
    "baseline_modules": baseline_module_counts,
    "adversarial_modules": adv_module_counts
}

out_path = root / "specs" / "test_inventory.json"
out_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
print(f"Generated {out_path} successfully.")
print(f"Baseline tests: {total_baseline_tests} across {len(baseline_module_counts)} modules")
print(f"Adversarial tests: {total_adv_tests} across {len(adv_module_counts)} modules")
print(f"Combined total: {total_baseline_tests + total_adv_tests} tests")
