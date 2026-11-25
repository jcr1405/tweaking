"""
Presence Desktop Input Execution Backend.

Executes physical and synthetic user inputs via Win32 SendInput:
- Mouse movement, clicks (left, right, middle, double), scroll, drag
- Text typing via Unicode keystrokes
- Hotkey combinations and navigation keys
- Bounding-box and screen geometry validation
- Visual evidence freshness checking
- Pluggable dry-run / test recording mode
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from runtime.desktop.models import (
    ActionStatus,
    ActionType,
    DesktopAction,
    DesktopActionResult,
    ScreenRect,
)
from runtime.desktop.windows import (
    WindowsManager,
    ensure_desktop_attachment,
    run_in_attached_thread,
)

logger = logging.getLogger(__name__)

# Win32 Constants for SendInput
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

# Mouse flags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

# Keyboard flags
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# System metrics
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# Virtual Key Codes
VK_MAP: Dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "clear": 0x0C,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "menu": 0x12,
    "pause": 0x13,
    "caps_lock": 0x14,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "page_up": 0x21,
    "page_down": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "select": 0x29,
    "print": 0x2A,
    "execute": 0x2B,
    "print_screen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "help": 0x2F,
    "win": 0x5B,
    "windows": 0x5B,
    "apps": 0x5D,
    "sleep": 0x5F,
    "numpad0": 0x60,
    "numpad1": 0x61,
    "numpad2": 0x62,
    "numpad3": 0x63,
    "numpad4": 0x64,
    "numpad5": 0x65,
    "numpad6": 0x66,
    "numpad7": 0x67,
    "numpad8": 0x68,
    "numpad9": 0x69,
    "multiply": 0x6A,
    "add": 0x6B,
    "separator": 0x6C,
    "subtract": 0x6D,
    "decimal": 0x6E,
    "divide": 0x6F,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}


# Win32 Structures
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", INPUT_UNION),
    ]


class DesktopInputBackend:
    """
    Direct input execution layer for desktop automation using Win32 SendInput.
    """

    def __init__(
        self,
        windows_manager: Optional[WindowsManager] = None,
        dry_run: bool = False,
        default_delay: float = 0.05,
    ):
        self.windows_manager = windows_manager or WindowsManager()
        self.dry_run = dry_run
        self.default_delay = default_delay
        self.executed_actions_log: List[Dict[str, Any]] = []

    def get_virtual_screen_bounds(self) -> ScreenRect:
        """Get total bounds covering all attached monitors."""
        user32 = ctypes.windll.user32
        v_left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        v_top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        v_width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        v_height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        return ScreenRect(
            left=v_left,
            top=v_top,
            right=v_left + v_width,
            bottom=v_top + v_height,
        )

    def validate_coordinates(
        self,
        x: int,
        y: int,
        target_window_id: Optional[str] = None,
        max_age_seconds: Optional[float] = None,
        evidence_timestamp: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validates target coordinates against virtual screen and target window bounds,
        and verifies evidence freshness.
        """
        # 1. Freshness check
        if max_age_seconds is not None and evidence_timestamp is not None:
            age = time.time() - evidence_timestamp
            if age > max_age_seconds:
                return False, f"Stale visual evidence (age={age:.2f}s > max={max_age_seconds:.2f}s)"

        # 2. Virtual screen bounds check
        screen = self.get_virtual_screen_bounds()
        if not screen.contains(x, y):
            return False, f"Coordinates ({x}, {y}) outside virtual screen bounds {screen}"

        # 3. Window bounds check
        if target_window_id:
            win = self.windows_manager.get_window(target_window_id)
            if not win:
                return False, f"Target window window_id={target_window_id} does not exist"
            if not win.is_visible:
                return False, f"Target window window_id={target_window_id} ('{win.title}') is not visible"
            if not win.bounds.contains(x, y):
                return False, f"Coordinates ({x}, {y}) outside window bounds {win.bounds}"

        return True, None

    def _send_inputs(self, inputs: List[INPUT]) -> bool:
        """Call Win32 SendInput or record if dry_run."""
        if self.dry_run:
            return True

        n_inputs = len(inputs)
        if n_inputs == 0:
            return True

        def _do_send() -> bool:
            ensure_desktop_attachment()
            arr = (INPUT * n_inputs)(*inputs)
            sent = ctypes.windll.user32.SendInput(n_inputs, ctypes.byref(arr), ctypes.sizeof(INPUT))
            return sent == n_inputs

        return bool(run_in_attached_thread(_do_send))

    def _normalize_coords(self, x: int, y: int) -> Tuple[int, int]:
        """Convert screen coordinates to normalized (0..65535) virtual desktop coordinates."""
        screen = self.get_virtual_screen_bounds()
        if screen.width <= 0 or screen.height <= 0:
            return 0, 0
        norm_x = int((x - screen.left) * 65535 / (screen.width - 1)) if screen.width > 1 else 0
        norm_y = int((y - screen.top) * 65535 / (screen.height - 1)) if screen.height > 1 else 0
        return max(0, min(65535, norm_x)), max(0, min(65535, norm_y))

    def move_to(self, x: int, y: int) -> bool:
        """Move mouse cursor to absolute screen coordinates."""
        norm_x, norm_y = self._normalize_coords(x, y)
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi.dx = norm_x
        inp.u.mi.dy = norm_y
        inp.u.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        inp.u.mi.mouseData = 0
        inp.u.mi.dwExtraInfo = None

        self.executed_actions_log.append({"action": "move_to", "x": x, "y": y})
        res = self._send_inputs([inp])
        if self.default_delay > 0:
            time.sleep(self.default_delay)
        return res

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> bool:
        """Click at absolute screen coordinates."""
        if not self.dry_run and ctypes.windll.user32:
            try:
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
            except Exception:
                pass

        self.move_to(x, y)
        norm_x, norm_y = self._normalize_coords(x, y)

        if button == "left":
            down_flag = MOUSEEVENTF_LEFTDOWN
            up_flag = MOUSEEVENTF_LEFTUP
        elif button == "right":
            down_flag = MOUSEEVENTF_RIGHTDOWN
            up_flag = MOUSEEVENTF_RIGHTUP
        elif button == "middle":
            down_flag = MOUSEEVENTF_MIDDLEDOWN
            up_flag = MOUSEEVENTF_MIDDLEUP
        else:
            raise ValueError(f"Unsupported mouse button: {button}")

        self.executed_actions_log.append(
            {"action": "click", "x": x, "y": y, "button": button, "double": double}
        )

        res = True
        for _ in range(2 if double else 1):
            inp_down = INPUT()
            inp_down.type = INPUT_MOUSE
            inp_down.u.mi.dx = norm_x
            inp_down.u.mi.dy = norm_y
            inp_down.u.mi.dwFlags = down_flag | MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

            inp_up = INPUT()
            inp_up.type = INPUT_MOUSE
            inp_up.u.mi.dx = norm_x
            inp_up.u.mi.dy = norm_y
            inp_up.u.mi.dwFlags = up_flag | MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

            r1 = self._send_inputs([inp_down])
            time.sleep(0.03)
            r2 = self._send_inputs([inp_up])
            res = res and r1 and r2
            if double:
                time.sleep(0.05)
        if self.default_delay > 0:
            time.sleep(self.default_delay)
        return res

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> bool:
        """Drag mouse from (x1, y1) to (x2, y2)."""
        self.move_to(x1, y1)
        norm_x1, norm_y1 = self._normalize_coords(x1, y1)
        norm_x2, norm_y2 = self._normalize_coords(x2, y2)

        down_flag = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
        up_flag = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP

        inp_down = INPUT()
        inp_down.type = INPUT_MOUSE
        inp_down.u.mi.dx = norm_x1
        inp_down.u.mi.dy = norm_y1
        inp_down.u.mi.dwFlags = down_flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

        # intermediate interpolation steps
        steps = 5
        move_inputs = []
        for i in range(1, steps + 1):
            ix = int(x1 + (x2 - x1) * (i / steps))
            iy = int(y1 + (y2 - y1) * (i / steps))
            n_ix, n_iy = self._normalize_coords(ix, iy)
            inp_move = INPUT()
            inp_move.type = INPUT_MOUSE
            inp_move.u.mi.dx = n_ix
            inp_move.u.mi.dy = n_iy
            inp_move.u.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
            move_inputs.append(inp_move)

        inp_up = INPUT()
        inp_up.type = INPUT_MOUSE
        inp_up.u.mi.dx = norm_x2
        inp_up.u.mi.dy = norm_y2
        inp_up.u.mi.dwFlags = up_flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

        self.executed_actions_log.append(
            {"action": "drag", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "button": button}
        )
        res = self._send_inputs([inp_down] + move_inputs + [inp_up])
        if self.default_delay > 0:
            time.sleep(self.default_delay)
        return res

    def scroll(self, x: int, y: int, clicks_vertical: int = 0, clicks_horizontal: int = 0) -> bool:
        """Scroll mouse wheel at (x, y). Negative = scroll down, Positive = scroll up."""
        self.move_to(x, y)
        norm_x, norm_y = self._normalize_coords(x, y)
        inputs = []

        if clicks_vertical != 0:
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.u.mi.dx = norm_x
            inp.u.mi.dy = norm_y
            inp.u.mi.dwFlags = MOUSEEVENTF_WHEEL | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
            inp.u.mi.mouseData = int(clicks_vertical * 120)
            inputs.append(inp)

        if clicks_horizontal != 0:
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.u.mi.dx = norm_x
            inp.u.mi.dy = norm_y
            inp.u.mi.dwFlags = MOUSEEVENTF_HWHEEL | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
            inp.u.mi.mouseData = int(clicks_horizontal * 120)
            inputs.append(inp)

        self.executed_actions_log.append(
            {"action": "scroll", "x": x, "y": y, "v": clicks_vertical, "h": clicks_horizontal}
        )
        res = self._send_inputs(inputs)
        if self.default_delay > 0:
            time.sleep(self.default_delay)
        return res

    def type_text(self, text: str) -> bool:
        """Type text using Unicode SendInput events."""
        inputs = []
        for char in text:
            # Down event
            inp_down = INPUT()
            inp_down.type = INPUT_KEYBOARD
            inp_down.u.ki.wVk = 0
            inp_down.u.ki.wScan = ord(char)
            inp_down.u.ki.dwFlags = KEYEVENTF_UNICODE

            # Up event
            inp_up = INPUT()
            inp_up.type = INPUT_KEYBOARD
            inp_up.u.ki.wVk = 0
            inp_up.u.ki.wScan = ord(char)
            inp_up.u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP

            inputs.extend([inp_down, inp_up])

        self.executed_actions_log.append({"action": "type_text", "text": text})
        res = self._send_inputs(inputs)
        if self.default_delay > 0:
            time.sleep(self.default_delay)
        return res

    def hotkey(self, keys: Sequence[str]) -> bool:
        """Press and release a combination of keys in sequence (e.g., ['ctrl', 'c'])."""
        vk_codes: List[int] = []
        for k in keys:
            normalized = k.lower().strip()
            if normalized in VK_MAP:
                vk_codes.append(VK_MAP[normalized])
            elif len(normalized) == 1:
                # Character key code
                vk_codes.append(ord(normalized.upper()))
            else:
                raise ValueError(f"Unknown key identifier: '{k}'")

        # Press keys down in order
        down_inputs = []
        for vk in vk_codes:
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki.wVk = vk
            inp.u.ki.wScan = 0
            inp.u.ki.dwFlags = 0
            down_inputs.append(inp)

        # Release keys in reverse order
        up_inputs = []
        for vk in reversed(vk_codes):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki.wVk = vk
            inp.u.ki.wScan = 0
            inp.u.ki.dwFlags = KEYEVENTF_KEYUP
            up_inputs.append(inp)

        self.executed_actions_log.append({"action": "hotkey", "keys": list(keys)})
        res = self._send_inputs(down_inputs + up_inputs)
        if self.default_delay > 0:
            time.sleep(self.default_delay)
        return res

    def release_held_inputs(self) -> None:
        """
        Emergency reset: releases all mouse buttons (left, right, middle)
        and modifier keys (Ctrl, Shift, Alt, Win) to prevent stuck input states
        upon error or cancellation.
        """
        inputs = []
        for flag in [MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEUP]:
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.u.mi.dwFlags = flag
            inputs.append(inp)

        for vk in [0x11, 0x10, 0x12, 0x5B, 0x5C]:  # VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki.wVk = vk
            inp.u.ki.dwFlags = KEYEVENTF_KEYUP
            inputs.append(inp)

        if not self.dry_run and ctypes.windll.user32:
            self._send_inputs(inputs)

    def execute_action(
        self,
        action: DesktopAction,
        evidence_timestamp: Optional[float] = None,
        max_age_seconds: float = 10.0,
    ) -> DesktopActionResult:
        """
        Executes a validated desktop action via SendInput.
        """
        start_time = datetime.now(timezone.utc).isoformat()
        start_ts = time.time()
        try:
            # Check coordinate validation if action is coordinate-based
            if action.action_type in (
                ActionType.CLICK_COORDINATE,
                ActionType.DOUBLE_CLICK,
                ActionType.RIGHT_CLICK,
                ActionType.MOVE_MOUSE,
            ):
                if not action.coordinate or len(action.coordinate) != 2:
                    return DesktopActionResult(
                        action_id=action.action_id,
                        status=ActionStatus.FAILED,
                        target_window_id=action.window_id,
                        action_type=action.action_type.value,
                        started_at=start_time,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                        failure_reason="Action requires coordinate [x, y]",
                    )
                x, y = action.coordinate
                valid, err = self.validate_coordinates(
                    x=x,
                    y=y,
                    target_window_id=action.window_id,
                    max_age_seconds=max_age_seconds if action.screenshot_id else None,
                    evidence_timestamp=evidence_timestamp,
                )
                if not valid:
                    return DesktopActionResult(
                        action_id=action.action_id,
                        status=ActionStatus.FAILED,
                        target_window_id=action.window_id,
                        action_type=action.action_type.value,
                        started_at=start_time,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                        failure_reason=f"Coordinate validation failed: {err}",
                    )

            # Bring target window to foreground if specified
            if action.window_id:
                self.windows_manager.set_foreground(action.window_id)

            success = False
            if action.action_type == ActionType.CLICK_COORDINATE:
                assert action.coordinate is not None
                success = self.click(action.coordinate[0], action.coordinate[1], button="left", double=False)
            elif action.action_type == ActionType.DOUBLE_CLICK:
                assert action.coordinate is not None
                success = self.click(action.coordinate[0], action.coordinate[1], button="left", double=True)
            elif action.action_type == ActionType.RIGHT_CLICK:
                assert action.coordinate is not None
                success = self.click(action.coordinate[0], action.coordinate[1], button="right", double=False)
            elif action.action_type == ActionType.MOVE_MOUSE:
                assert action.coordinate is not None
                success = self.move_to(action.coordinate[0], action.coordinate[1])
            elif action.action_type == ActionType.DRAG:
                if not action.coordinate or not action.drag_to:
                    return DesktopActionResult(
                        action_id=action.action_id,
                        status=ActionStatus.FAILED,
                        target_window_id=action.window_id,
                        action_type=action.action_type.value,
                        started_at=start_time,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                        failure_reason="Drag action requires coordinate and drag_to",
                    )
                success = self.drag(
                    action.coordinate[0],
                    action.coordinate[1],
                    action.drag_to[0],
                    action.drag_to[1],
                )
            elif action.action_type == ActionType.SCROLL:
                # If coordinate is given, scroll there, else current position
                coord = action.coordinate or [0, 0]
                amount = action.scroll_amount or 1
                v_clicks = -amount if action.scroll_direction == "down" else (amount if action.scroll_direction == "up" else 0)
                h_clicks = amount if action.scroll_direction == "right" else (-amount if action.scroll_direction == "left" else 0)
                success = self.scroll(coord[0], coord[1], v_clicks, h_clicks)
            elif action.action_type == ActionType.TYPE_TEXT:
                text = action.text or ""
                success = self.type_text(text)
            elif action.action_type == ActionType.PRESS_KEY:
                key = action.key or ""
                success = self.hotkey([key])
            elif action.action_type == ActionType.PRESS_SHORTCUT:
                shortcut = action.shortcut or []
                success = self.hotkey(shortcut)
            elif action.action_type == ActionType.WAIT_FOR_CONDITION:
                wait_time = float(action.scroll_amount or 1) * 0.5
                if not self.dry_run:
                    time.sleep(wait_time)
                success = True
            else:
                return DesktopActionResult(
                    action_id=action.action_id,
                    status=ActionStatus.FAILED,
                    target_window_id=action.window_id,
                    action_type=action.action_type.value,
                    started_at=start_time,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    failure_reason=f"Unsupported action type for SendInput: {action.action_type}",
                )

            return DesktopActionResult(
                action_id=action.action_id,
                status=ActionStatus.COMPLETED if success else ActionStatus.FAILED,
                target_window_id=action.window_id,
                action_type=action.action_type.value,
                started_at=start_time,
                completed_at=datetime.now(timezone.utc).isoformat(),
                verified=success,
            )
        except Exception as exc:
            logger.exception("Failed to execute desktop action %s: %s", action.action_id, exc)
            self.release_held_inputs()
            return DesktopActionResult(
                action_id=action.action_id,
                status=ActionStatus.FAILED,
                target_window_id=action.window_id,
                action_type=action.action_type.value,
                started_at=start_time,
                completed_at=datetime.now(timezone.utc).isoformat(),
                failure_reason=str(exc),
            )
