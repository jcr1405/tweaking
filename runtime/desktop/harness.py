"""
Presence Desktop Acceptance Test Harness.

Provides a deterministic native Win32 test application exposing both
semantic Accessibility (UIA) controls and a custom-drawn visual canvas button.
Used for end-to-end integration testing of:
- Semantic UIA patterns (Invoke, Toggle, Value, Selection, Scroll)
- Multimodal local vision perception & SendInput coordinate clicks
- Contextual action authorization and safety policy enforcement
- Recycled handle and process security verification
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Optional

from runtime.desktop.models import DesktopWindow
from runtime.desktop.windows import WindowsManager

logger = logging.getLogger("presence.desktop.harness")

HARNESS_PROC_SCRIPT = os.path.join(os.path.dirname(__file__), "test_harness_proc.py")


class PresenceDesktopHarness:
    """
    Manages the lifecycle of the isolated 'Presence Desktop Acceptance Harness' process.
    """

    def __init__(
        self,
        python_exe: Optional[str] = None,
        state_file: Optional[str] = None,
    ):
        self.python_exe = python_exe or sys.executable
        if state_file:
            self.state_file = state_file
            self._temp_state_file = False
        else:
            fd, path = tempfile.mkstemp(prefix="presence_harness_", suffix=".json")
            os.close(fd)
            self.state_file = path
            self._temp_state_file = True

        self._process: Optional[subprocess.Popen] = None
        self._hwnd: Optional[int] = None
        self._window_id: Optional[str] = None

    def start(self, timeout: float = 10.0) -> DesktopWindow:
        """Starts the native harness application and waits for it to become ready."""
        if self._process and self._process.poll() is None:
            logger.info("Harness already running with PID %d", self._process.pid)
            wm = WindowsManager()
            win = wm.find_window_by_title("Presence Desktop Acceptance Harness")
            if win:
                return win

        cmd = [self.python_exe, HARNESS_PROC_SCRIPT, self.state_file]
        logger.info("Spawning desktop acceptance harness: %s", cmd)

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        start_time = time.time()
        ready = False
        while time.time() - start_time < timeout:
            if os.path.exists(self.state_file):
                try:
                    state = self.get_state()
                    if state.get("ready"):
                        self._hwnd = state.get("hwnd")
                        ready = True
                        break
                except Exception:
                    pass
            time.sleep(0.1)

        if not ready or not self._hwnd:
            self.stop()
            raise TimeoutError(f"Desktop acceptance harness failed to start within {timeout}s.")

        # Give Windows a moment to register UIA providers and layout
        time.sleep(0.3)

        wm = WindowsManager()
        win = wm.find_window_by_title("Presence Desktop Acceptance Harness")
        if not win:
            # Fallback by hwnd
            all_wins = wm.list_windows()
            for w in all_wins:
                if w.hwnd == self._hwnd:
                    win = w
                    break

        if not win:
            self.stop()
            raise RuntimeError("Harness window created but not discoverable by WindowsManager.")

        self._window_id = win.window_id
        return win

    def get_state(self) -> Dict[str, Any]:
        """Reads current state from the harness state file."""
        if not os.path.exists(self.state_file):
            return {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug("Failed to read harness state file: %s", e)
            return {}

    def get_window(self, wm: Optional[WindowsManager] = None) -> Optional[DesktopWindow]:
        """Retrieves the DesktopWindow object from WindowsManager."""
        mgr = wm or WindowsManager()
        if self._window_id:
            w = mgr.get_window(self._window_id)
            if w:
                return w
        return mgr.find_window_by_title("Presence Desktop Acceptance Harness")

    def get_window_id(self, wm: Optional[WindowsManager] = None) -> str:
        """Returns the ephemeral window_id for the harness window."""
        win = self.get_window(wm)
        if not win:
            raise RuntimeError("Harness window is not currently open or tracked.")
        self._window_id = win.window_id
        return win.window_id

    def is_running(self) -> bool:
        """Checks if harness subprocess is active."""
        return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        """Terminates the harness subprocess and cleans up temporary state file."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=3.0)
            except Exception:
                try:
                    self._process.kill()
                    self._process.wait(timeout=1.0)
                except Exception:
                    pass
            self._process = None

        if self._temp_state_file and os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except Exception:
                pass

    def __enter__(self) -> PresenceDesktopHarness:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
