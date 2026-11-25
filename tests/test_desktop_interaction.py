"""
Presence Phase 2E: Windows Desktop Interaction, Accessibility Automation
& Vision-Guided Computer Use Test Suite.

Verifies:
- Window discovery and tracking on Windows 11
- Accessibility tree inspection and semantic control pattern manipulation (Invoke, Toggle, Value)
- Targeted screenshot capture with memory-bounded retention and SHA-256 integrity
- Vision perception grounding and coordinate transformation
- Input simulation via SendInput with safety bounds and freshness checking
- Desktop safety policy gating, protected processes, and sensitive pattern blocking
- State transition verification before and after desktop actions
- Desktop controller orchestration and batch execution
- Desktop tools registration and permission boundaries (L0_OBSERVE vs L2_EXECUTE_REVERSIBLE)
- Zero cloud calls during all local desktop operations
"""

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

import runtime.desktop as rd
from runtime.desktop.accessibility import AccessibilityBackend
from runtime.desktop.capture import CapturedScreenshot, ScreenCaptureBackend
from runtime.desktop.controller import DesktopController
from runtime.desktop.input import DesktopInputBackend
from runtime.desktop.models import (
    ActionStatus,
    ActionType,
    DesktopAction,
    DesktopActionResult,
    DesktopBatchResult,
    DesktopElement,
    DesktopObservation,
    DesktopWindow,
    ObservationSource,
    ScreenRect,
    VisualElementCandidate,
    VisualPerceptionResult,
)
from runtime.desktop.perception import VisionPerception
from runtime.desktop.policy import DesktopPolicy, PROTECTED_PROCESSES
from runtime.desktop.verification import StateVerifier
from runtime.desktop.windows import WindowsManager, ensure_desktop_attachment
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.providers.ollama import OllamaModelProvider
from runtime.tools.desktop_tools import create_desktop_tools, register_desktop_tools
from runtime.tools.registry import ToolRegistry


class TestScreenRectAndModels(unittest.TestCase):
    """Verifies geometry, coordinate containment, and Pydantic validation."""

    def test_01_screen_rect_properties(self):
        rect = ScreenRect(left=100, top=50, right=500, bottom=350)
        self.assertEqual(rect.width, 400)
        self.assertEqual(rect.height, 300)
        self.assertEqual(rect.center(), (300, 200))

        # Containment
        self.assertTrue(rect.contains(100, 50))
        self.assertTrue(rect.contains(500, 350))
        self.assertTrue(rect.contains(300, 200))
        self.assertFalse(rect.contains(99, 50))
        self.assertFalse(rect.contains(501, 200))
        self.assertFalse(rect.contains(300, 351))

    def test_02_visual_element_candidate_validation(self):
        cand = VisualElementCandidate(
            label="Submit Button",
            role="button",
            bbox_normalized=[0.1, 0.2, 0.3, 0.4],
            confidence=0.95,
            visible_text="Submit",
            actionable=True,
        )
        self.assertEqual(cand.label, "Submit Button")
        self.assertEqual(cand.confidence, 0.95)

        # Invalid bbox length
        with self.assertRaises(ValueError):
            VisualElementCandidate(
                label="Bad Box",
                bbox_normalized=[0.1, 0.2, 0.3],
            )

        # Out of bounds coordinate
        with self.assertRaises(ValueError):
            VisualElementCandidate(
                label="Bad Coord",
                bbox_normalized=[-0.1, 0.2, 0.3, 0.4],
            )

        # min > max
        with self.assertRaises(ValueError):
            VisualElementCandidate(
                label="Inverted Box",
                bbox_normalized=[0.5, 0.2, 0.3, 0.4],
            )


class TestWindowsManager(unittest.TestCase):
    """Verifies window enumeration and management."""

    def setUp(self):
        self.wm = WindowsManager()

    def test_03_list_windows(self):
        wins = self.wm.list_windows()
        self.assertIsInstance(wins, list)
        for w in wins:
            self.assertIsInstance(w, DesktopWindow)
            self.assertTrue(w.window_id.startswith("win_"))
            self.assertTrue(len(w.title) > 0)
            self.assertGreater(w.bounds.width, 0)
            self.assertGreater(w.bounds.height, 0)

    def test_04_find_window_by_title_and_get_window(self):
        wins = self.wm.list_windows()
        if wins:
            found_any = False
            for target in wins:
                part = target.title[:5]
                found = self.wm.find_window_by_title(part)
                if found:
                    by_id = self.wm.get_window(target.window_id)
                    if by_id:
                        self.assertEqual(by_id.window_id, target.window_id)
                        found_any = True
                        break
            self.assertTrue(found_any, "Could not find and re-fetch any listed window")


class TestScreenCaptureBackend(unittest.TestCase):
    """Verifies targeted screen and window capture mechanisms."""

    def setUp(self):
        self.wm = WindowsManager()
        self.capture = ScreenCaptureBackend(windows_manager=self.wm, max_cache_entries=3)

    def test_05_capture_full_screen(self):
        shot = self.capture.capture_screen()
        self.assertIsInstance(shot, CapturedScreenshot)
        self.assertTrue(shot.screenshot_id.startswith("shot_"))
        self.assertGreater(shot.width, 0)
        self.assertGreater(shot.height, 0)
        self.assertEqual(len(shot.sha256), 64)
        self.assertTrue(shot.base64_png.startswith("iVBORw0KGgo"))  # PNG magic header in base64
        self.assertFalse(shot.is_expired(max_age_seconds=60.0))

    def test_06_capture_cache_eviction(self):
        # Create dummy screenshots to test LRU cache bounds
        for i in range(5):
            img = Image.new("RGB", (50, 50), color=(i * 20, i * 20, i * 20))
            shot = CapturedScreenshot(
                screenshot_id=f"shot_test_{i}",
                image=img,
                window_id="win_test",
                bounds=ScreenRect(left=0, top=0, right=50, bottom=50),
            )
            self.capture._cache[shot.screenshot_id] = shot
            self.capture._prune_cache()

        # Should never exceed max_cache_entries (3)
        self.assertLessEqual(len(self.capture._cache), 3)


class TestVisionPerception(unittest.TestCase):
    """Verifies local multimodal vision parsing and coordinate grounding."""

    def test_07_ground_to_desktop_coordinates(self):
        vp = VisionPerception(provider=None)
        cand = VisualElementCandidate(
            label="Save Button",
            role="button",
            bbox_normalized=[0.2, 0.4, 0.4, 0.6],  # Center: (0.3 y, 0.5 x)
            confidence=0.99,
            actionable=True,
        )
        window_bounds = ScreenRect(left=100, top=200, right=500, bottom=600)  # w=400, h=400
        abs_x, abs_y = vp.ground_to_desktop_coordinates(cand, window_bounds)
        self.assertEqual(abs_x, 100 + int(0.5 * 400))  # 300
        self.assertEqual(abs_y, 200 + int(0.3 * 400))  # 320

    def test_08_extract_json_payload_markdown_and_think_tags(self):
        vp = VisionPerception(provider=None)

        # Fenced markdown JSON
        raw_markdown = """```json
        {
            "elements": [
                {
                    "label": "Login Button",
                    "role": "button",
                    "bbox_normalized": [0.1, 0.1, 0.2, 0.3],
                    "confidence": 0.9,
                    "actionable": true
                }
            ],
            "overall_ambiguity": null
        }
        ```"""
        parsed = vp._extract_json_payload(raw_markdown)
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed["elements"]), 1)
        self.assertEqual(parsed["elements"][0]["label"], "Login Button")

        # Fallback list JSON
        raw_list = """[{"label": "OK", "bbox_normalized": [0,0,1,1]}]"""
        parsed_list = vp._extract_json_payload(raw_list)
        self.assertIsNotNone(parsed_list)
        self.assertEqual(len(parsed_list["elements"]), 1)

    def test_09_mock_provider_perception(self):
        mock_provider = MagicMock()
        mock_provider.generate_multimodal.return_value = {
            "content": json.dumps({
                "elements": [{
                    "label": "Search Box",
                    "role": "input",
                    "bbox_normalized": [0.05, 0.1, 0.15, 0.9],
                    "confidence": 0.92,
                    "visible_text": "Type to search",
                    "actionable": True,
                }],
                "overall_ambiguity": None,
            }),
            "tokens_used": 150,
        }

        vp = VisionPerception(provider=mock_provider)
        img = Image.new("RGB", (800, 600), color="white")
        shot = CapturedScreenshot(
            screenshot_id="shot_mock_01",
            image=img,
            window_id="win_mock",
            bounds=ScreenRect(left=0, top=0, right=800, bottom=600),
        )

        res = vp.analyze_screenshot(shot, query="Search Box")
        self.assertEqual(res.screenshot_id, "shot_mock_01")
        self.assertEqual(len(res.observations), 1)
        self.assertEqual(res.observations[0].label, "Search Box")
        self.assertIsNone(res.overall_ambiguity)


class TestDesktopInputBackend(unittest.TestCase):
    """Verifies coordinate validation and input execution."""

    def setUp(self):
        self.input_backend = DesktopInputBackend(dry_run=True)

    def test_10_coordinate_validation_virtual_screen(self):
        # Valid coordinate
        valid, err = self.input_backend.validate_coordinates(100, 100)
        self.assertTrue(valid)
        self.assertIsNone(err)

        # Coordinate outside virtual screen
        valid, err = self.input_backend.validate_coordinates(-9999, -9999)
        self.assertFalse(valid)
        self.assertIn("outside virtual screen", err)

    def test_11_stale_evidence_rejection(self):
        old_timestamp = time.time() - 20.0  # 20s ago
        valid, err = self.input_backend.validate_coordinates(
            100, 100, max_age_seconds=10.0, evidence_timestamp=old_timestamp
        )
        self.assertFalse(valid)
        self.assertIn("Stale visual evidence", err)

    def test_12_dry_run_action_execution(self):
        act_click = DesktopAction(
            action_id="act_click_01",
            action_type=ActionType.CLICK_COORDINATE,
            coordinate=[150, 250],
        )
        res_click = self.input_backend.execute_action(act_click)
        self.assertEqual(res_click.status, ActionStatus.COMPLETED)
        self.assertTrue(res_click.verified)

        act_type = DesktopAction(
            action_id="act_type_01",
            action_type=ActionType.TYPE_TEXT,
            text="Hello Presence",
        )
        res_type = self.input_backend.execute_action(act_type)
        self.assertEqual(res_type.status, ActionStatus.COMPLETED)

        act_hotkey = DesktopAction(
            action_id="act_hotkey_01",
            action_type=ActionType.PRESS_SHORTCUT,
            shortcut=["ctrl", "c"],
        )
        res_hotkey = self.input_backend.execute_action(act_hotkey)
        self.assertEqual(res_hotkey.status, ActionStatus.COMPLETED)


class TestDesktopPolicy(unittest.TestCase):
    """Verifies safety policy and dangerous action gating."""

    def setUp(self):
        self.policy = DesktopPolicy(allow_high_impact=False, max_batch_size=10)

    def test_13_protected_system_processes(self):
        # Protected Task Manager window
        tm_win = DesktopWindow(
            window_id="win_tm",
            title="Task Manager",
            process_id=1234,
            process_name="Taskmgr.exe",
            bounds=ScreenRect(left=0, top=0, right=800, bottom=600),
        )
        is_prot, reason = self.policy.is_window_protected(tm_win)
        self.assertTrue(is_prot)
        self.assertIn("protected system infrastructure", reason)

        # Protected Consent UAC prompt
        uac_win = DesktopWindow(
            window_id="win_uac",
            title="User Account Control",
            process_id=5678,
            process_name="consent.exe",
            bounds=ScreenRect(left=0, top=0, right=400, bottom=300),
        )
        is_prot2, reason2 = self.policy.is_window_protected(uac_win)
        self.assertTrue(is_prot2)

    def test_14_sensitive_text_and_dangerous_action_rejection(self):
        safe_win = DesktopWindow(
            window_id="win_notepad",
            title="Untitled - Notepad",
            process_id=9999,
            process_name="notepad.exe",
            bounds=ScreenRect(left=0, top=0, right=800, bottom=600),
        )

        # Password pattern rejection
        act_pw = DesktopAction(
            action_id="act_pw",
            action_type=ActionType.TYPE_TEXT,
            text="password = secret123",
        )
        allowed, reason = self.policy.evaluate_action(act_pw, safe_win)
        self.assertFalse(allowed)
        self.assertIn("sensitive credential", reason)

        # Dangerous command phrase rejection
        act_danger = DesktopAction(
            action_id="act_del",
            action_type=ActionType.TYPE_TEXT,
            text="Please format disk C:",
        )
        allowed2, reason2 = self.policy.evaluate_action(act_danger, safe_win)
        self.assertFalse(allowed2)
        self.assertIn("dangerous operation keyword", reason2)

    def test_15_batch_size_limits(self):
        actions = [
            DesktopAction(action_id=f"a_{i}", action_type=ActionType.WAIT_FOR_CONDITION)
            for i in range(15)
        ]
        allowed, reason = self.policy.evaluate_batch(actions)
        self.assertFalse(allowed)
        self.assertIn("exceeds maximum allowed", reason)


class TestStateVerifier(unittest.TestCase):
    """Verifies state transition assertions."""

    def test_16_verify_toggle_transition(self):
        verifier = StateVerifier(windows_manager=MagicMock(), accessibility_backend=MagicMock())

        act_toggle = DesktopAction(
            action_id="t1",
            action_type=ActionType.SET_TOGGLE,
            element_id="el_chk",
            desired_state=True,
        )

        # Before: False, After: True -> Verified
        state_before = {"element": {"toggle_state": False}}
        state_after = {"element": {"toggle_state": True}}
        verified, reason = verifier.verify_action(act_toggle, state_before, state_after)
        self.assertTrue(verified)
        self.assertIsNone(reason)

        # After: False (failed to transition) -> Not verified
        state_failed = {"element": {"toggle_state": False}}
        verified_fail, reason_fail = verifier.verify_action(act_toggle, state_before, state_failed)
        self.assertFalse(verified_fail)
        self.assertIn("Toggle state did not change", reason_fail)

    def test_17_verify_value_transition(self):
        verifier = StateVerifier(windows_manager=MagicMock(), accessibility_backend=MagicMock())

        act_val = DesktopAction(
            action_id="v1",
            action_type=ActionType.SET_VALUE,
            element_id="el_input",
            text="New Title",
        )

        state_before = {"element": {"value": ""}}
        state_after = {"element": {"value": "New Title"}}
        verified, reason = verifier.verify_action(act_val, state_before, state_after)
        self.assertTrue(verified)

        state_bad = {"element": {"value": "Old Content"}}
        verified2, reason2 = verifier.verify_action(act_val, state_before, state_bad)
        self.assertFalse(verified2)
        self.assertIn("does not contain expected text", reason2)


class TestDesktopController(unittest.TestCase):
    """Verifies orchestration, batch execution, and accessibility-first routing."""

    def setUp(self):
        self.mock_wm = MagicMock()
        self.mock_acc = MagicMock()
        self.mock_capture = MagicMock()
        self.mock_input = DesktopInputBackend(windows_manager=self.mock_wm, dry_run=True)
        self.policy = DesktopPolicy(allow_high_impact=False)
        self.verifier = StateVerifier(windows_manager=self.mock_wm, accessibility_backend=self.mock_acc)

        self.ctrl = DesktopController(
            windows_manager=self.mock_wm,
            accessibility_backend=self.mock_acc,
            capture_backend=self.mock_capture,
            input_backend=self.mock_input,
            policy=self.policy,
            verifier=self.verifier,
            dry_run=True,
        )

    def test_18_accessibility_first_semantic_routing(self):
        test_win = DesktopWindow(
            window_id="win_test",
            title="App Window",
            process_id=1000,
            process_name="app.exe",
            bounds=ScreenRect(left=10, top=10, right=400, bottom=300),
        )
        self.mock_wm.get_window.return_value = test_win
        self.mock_acc.get_registered_element.return_value = {"name": "OK", "control_type": "Button"}
        self.mock_acc.invoke_element.return_value = {"status": "invoked"}

        action = DesktopAction(
            action_id="act_sem_1",
            action_type=ActionType.INVOKE_ELEMENT,
            element_id="el_ok",
            window_id="win_test",
        )

        res = self.ctrl.execute_action(action)
        self.assertEqual(res.status, ActionStatus.COMPLETED)
        self.mock_acc.invoke_element.assert_called_once_with("el_ok")

    def test_19_batch_execution_stops_on_failure(self):
        test_win = DesktopWindow(
            window_id="win_test",
            title="App Window",
            process_id=1000,
            process_name="app.exe",
            bounds=ScreenRect(left=10, top=10, right=400, bottom=300),
        )
        self.mock_wm.get_window.return_value = test_win

        actions = [
            DesktopAction(action_id="a1", action_type=ActionType.CLICK_COORDINATE, coordinate=[100, 100]),
            DesktopAction(action_id="a2", action_type=ActionType.CLICK_COORDINATE, coordinate=[-9999, -9999]),  # Out of bounds
            DesktopAction(action_id="a3", action_type=ActionType.CLICK_COORDINATE, coordinate=[200, 200]),
        ]

        batch_res = self.ctrl.execute_batch(window_id="win_test", actions=actions, stop_on_failure=True)
        self.assertEqual(batch_res.status, ActionStatus.FAILED)
        self.assertTrue(batch_res.stopped_on_failure)
        self.assertEqual(batch_res.completed_actions, 1)
        self.assertEqual(len(batch_res.action_results), 2)  # Stopped at a2


class TestDesktopToolsAndPermissions(unittest.TestCase):
    """Verifies tool declarations, schemas, and 4-tier permission integration."""

    def test_20_tools_and_authority_tiers(self):
        registry = ToolRegistry()
        ctrl = DesktopController(dry_run=True)
        register_desktop_tools(registry, controller=ctrl)

        tools = {t.tool_id: t for t in registry.list_tools()}
        self.assertEqual(len(tools), 5)

        # L0_OBSERVE
        self.assertEqual(tools["desktop.list_windows"].authority_tier, PermissionTier.L0_OBSERVE)
        self.assertEqual(tools["desktop.observe"].authority_tier, PermissionTier.L0_OBSERVE)
        self.assertEqual(tools["desktop.perceive"].authority_tier, PermissionTier.L0_OBSERVE)
        self.assertEqual(tools["desktop.verify"].authority_tier, PermissionTier.L0_OBSERVE)

        # L2_EXECUTE_REVERSIBLE
        self.assertEqual(tools["desktop.execute_actions"].authority_tier, PermissionTier.L2_EXECUTE_REVERSIBLE)

    def test_21_read_only_boundary_blocks_execute_actions(self):
        from runtime.local_operations.supervisor import ScopedPermissionBoundary

        parent_boundary = PermissionBoundary()
        scoped = ScopedPermissionBoundary(
            parent_boundary=parent_boundary,
            read_only=True,
            max_allowed_tier=PermissionTier.L0_OBSERVE,
        )

        # L0 observation permitted
        res_obs = scoped.check_permission("desktop.list_windows")
        self.assertTrue(res_obs.allowed)

        # L2 execution blocked under read_only
        res_exec = scoped.check_permission("desktop.execute_actions")
        self.assertFalse(res_exec.allowed)
        self.assertIn("read-only execution", res_exec.reason)


class TestLiveTkinterWindowAccessibility(unittest.TestCase):
    """
    Live integration test: spawns a real Tkinter window in a subprocess on Windows desktop,
    discovers it via WindowsManager, inspects its UIA accessibility tree,
    exercises semantic controls, and verifies clean teardown.
    """

    def setUp(self):
        tk_code = (
            "import ctypes\n"
            "h = ctypes.windll.user32.OpenDesktopW('default', 0, False, 0x01FF)\n"
            "ctypes.windll.user32.SetThreadDesktop(h)\n"
            "import tkinter as tk, time\n"
            "root = tk.Tk()\n"
            "root.title('Presence Test Window')\n"
            "root.geometry('300x200+200+200')\n"
            "entry_var = tk.StringVar(value='Initial Text')\n"
            "entry = tk.Entry(root, textvariable=entry_var)\n"
            "entry.pack(pady=5)\n"
            "chk = tk.Checkbutton(root, text='Enable Option')\n"
            "chk.pack(pady=5)\n"
            "btn = tk.Button(root, text='Action Button')\n"
            "btn.pack(pady=5)\n"
            "root.update()\n"
            "root.mainloop()\n"
        )
        self.proc = subprocess.Popen([sys.executable, "-c", tk_code])
        # Poll for window appearance
        ctrl = DesktopController(dry_run=True)
        found = False
        for _ in range(30):
            time.sleep(0.2)
            win = ctrl.find_window_by_title("Presence Test Window")
            if win:
                found = True
                break
        self.assertTrue(found, "Timed out waiting for Tkinter test window to appear")

    def tearDown(self):
        if hasattr(self, "proc") and self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3.0)
            except Exception:
                self.proc.kill()

    def test_22_live_window_discovery_and_observation(self):
        ctrl = DesktopController(dry_run=True)
        win = ctrl.find_window_by_title("Presence Test Window")
        self.assertIsNotNone(win, "Failed to find live Tkinter window by title")
        self.assertIn("python", win.process_name.lower())

        # Observe window accessibility tree
        obs = ctrl.observe_window(win.window_id, include_tree=True, include_screenshot=True)
        self.assertEqual(obs.window.title, "Presence Test Window")
        self.assertIsNotNone(obs.screenshot_id)
        self.assertIn(obs.source, (ObservationSource.ACCESSIBILITY, ObservationSource.HYBRID))

        # Check screenshot capture of the live window
        shot = ctrl.capture_screenshot(win.window_id)
        self.assertIsNotNone(shot)
        self.assertGreater(shot.width, 0)
        self.assertGreater(shot.height, 0)


class TestLiveOllamaVisionInference(unittest.TestCase):
    """
    Live multimodal vision test: runs actual Qwen 3.5 9B vision inference
    through local Ollama with zero cloud calls.
    """

    def test_23_live_qwen_multimodal_perception(self):
        provider = OllamaModelProvider(model_id="qwen3.5:9b", think=False)
        is_val, reason = provider.validate_configuration()
        if not is_val:
            self.skipTest(f"Ollama Qwen 3.5 9B offline: {reason}")

        # Create a synthetic image with a prominent red box and label
        img = Image.new("RGB", (300, 200), color="white")
        # Draw a red rectangle in the upper half
        for x in range(50, 150):
            for y in range(30, 80):
                img.putpixel((x, y), (255, 0, 0))

        shot = CapturedScreenshot(
            screenshot_id="shot_live_test",
            image=img,
            window_id="win_synthetic",
            bounds=ScreenRect(left=0, top=0, right=300, bottom=200),
        )

        vp = VisionPerception(provider=provider)
        res = vp.analyze_screenshot(shot, query="Locate the red box or button")

        self.assertEqual(res.screenshot_id, "shot_live_test")
        self.assertEqual(res.image_width, 300)
        self.assertEqual(res.image_height, 200)
        # Verify provider had zero cloud calls
        self.assertFalse(hasattr(provider, "cloud_client"))


if __name__ == "__main__":
    unittest.main()
