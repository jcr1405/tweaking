"""Windows Desktop Window Management
Discovers, tracks, and manages top-level application windows on Windows 11.
Provides thread-safe desktop attachment, window state inspection, and focus management.
"""

import concurrent.futures
import ctypes
import logging
from typing import List, Optional, Dict, Any, Callable, Tuple
import psutil

try:
    import win32gui
    import win32process
    import win32con
except ImportError:
    win32gui = None
    win32process = None
    win32con = None

from runtime.desktop.models import DesktopWindow, ScreenRect

logger = logging.getLogger("presence.desktop.windows")


def ensure_desktop_attachment() -> bool:
    """Ensures the current thread is attached to the interactive input or default desktop on WinSta0.
    Required for Windows background jobs or sandboxed service threads to inspect UI and dispatch inputs.
    """
    try:
        h_desk = ctypes.windll.user32.OpenInputDesktop(0, False, 0x01FF)
        if not h_desk:
            h_desk = ctypes.windll.user32.OpenDesktopW("default", 0, False, 0x01FF)
        if h_desk:
            res = ctypes.windll.user32.SetThreadDesktop(h_desk)
            return bool(res)
    except Exception as e:
        logger.debug(f"Failed to attach thread to interactive desktop: {e}")
    return False


def run_in_attached_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Executes a callable inside a dedicated worker thread attached to the interactive desktop."""
    def _worker():
        ensure_desktop_attachment()
        return func(*args, **kwargs)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_worker)
        return future.result()


class WindowsManager:
    """Manages discovery and lifecycle tracking of top-level application windows."""

    def __init__(self):
        self._window_cache: Dict[str, DesktopWindow] = {}

    def list_windows(
        self,
        include_minimized: bool = False,
        filter_visible: bool = True,
    ) -> List[DesktopWindow]:
        """Lists all visible top-level application windows on the current Windows desktop."""
        return run_in_attached_thread(self._list_windows_impl, include_minimized, filter_visible)

    def _list_windows_impl(
        self,
        include_minimized: bool = False,
        filter_visible: bool = True,
    ) -> List[DesktopWindow]:
        if win32gui is None or win32process is None:
            logger.warning("win32gui/win32process not available; cannot list windows.")
            return []

        foreground_hwnd = win32gui.GetForegroundWindow()
        windows: List[DesktopWindow] = []

        def _enum_cb(hwnd: int, _extra: Any) -> None:
            if filter_visible and (not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd)):
                return

            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return

            rect = win32gui.GetWindowRect(hwnd)
            left, top, right, bottom = rect
            width = right - left
            height = bottom - top

            if width <= 0 or height <= 0:
                return

            # Check if minimized (iconic)
            is_minimized = bool(win32gui.IsIconic(hwnd))
            if is_minimized and not include_minimized:
                return

            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                pname = psutil.Process(pid).name()
            except Exception:
                pid = 0
                pname = "unknown"

            win_id = f"win_{hwnd}_{pid}"
            is_fg = (hwnd == foreground_hwnd)

            win_obj = DesktopWindow(
                window_id=win_id,
                title=title,
                process_id=pid,
                process_name=pname,
                bounds=ScreenRect(left=left, top=top, right=right, bottom=bottom),
                is_foreground=is_fg,
                is_visible=True,
                hwnd=hwnd,
            )
            windows.append(win_obj)
            self._window_cache[win_id] = win_obj

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception as e:
            logger.error(f"Error enumerating windows: {e}")

        return windows

    def get_window(self, window_id: str) -> Optional[DesktopWindow]:
        """Retrieves a window by its ephemeral ID, re-validating that it still exists."""
        return run_in_attached_thread(self._get_window_impl, window_id)

    def _get_window_impl(self, window_id: str) -> Optional[DesktopWindow]:
        cached = self._window_cache.get(window_id)
        if cached and cached.hwnd and win32gui:
            if win32gui.IsWindow(cached.hwnd) and win32gui.IsWindowVisible(cached.hwnd):
                # Verify PID has not been recycled by OS
                try:
                    _, cur_pid = win32process.GetWindowThreadProcessId(cached.hwnd)
                    if cur_pid != cached.process_id:
                        self._window_cache.pop(window_id, None)
                        return None
                except Exception:
                    self._window_cache.pop(window_id, None)
                    return None

                # Refresh bounds and foreground state
                rect = win32gui.GetWindowRect(cached.hwnd)
                fg_hwnd = win32gui.GetForegroundWindow()
                cached.bounds = ScreenRect(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3])
                cached.is_foreground = (cached.hwnd == fg_hwnd)
                cached.title = win32gui.GetWindowText(cached.hwnd).strip() or cached.title
                return cached

        # Re-scan if not in cache or cached window changed
        all_wins = self._list_windows_impl()
        for w in all_wins:
            if w.window_id == window_id:
                return w
        return None

    def validate_window_identity(self, window_id: str) -> Tuple[bool, Optional[str]]:
        """Verifies that window_id still exists and has not been recycled or closed."""
        return run_in_attached_thread(self._validate_window_identity_impl, window_id)

    def _validate_window_identity_impl(self, window_id: str) -> Tuple[bool, Optional[str]]:
        win = self._get_window_impl(window_id)
        if not win or not win.hwnd or not win32gui:
            return False, f"Target window '{window_id}' does not exist or has been closed"

        if not win32gui.IsWindow(win.hwnd):
            return False, f"HWND {win.hwnd} is no longer a valid window"

        try:
            _, cur_pid = win32process.GetWindowThreadProcessId(win.hwnd)
            if cur_pid != win.process_id:
                return False, f"HWND {win.hwnd} handle recycled: PID changed from {win.process_id} to {cur_pid}"
        except Exception as e:
            return False, f"Failed to verify process identity: {e}"

        return True, None

    def find_window_by_title(self, query: str) -> Optional[DesktopWindow]:
        """Finds the first visible window whose title contains query (case-insensitive)."""
        query_lower = query.lower()
        all_wins = self.list_windows()
        for w in all_wins:
            if query_lower in w.title.lower():
                return w
        return None

    def set_foreground(self, window_id: str) -> bool:
        """Brings the specified window to the foreground."""
        return run_in_attached_thread(self._set_foreground_impl, window_id)

    def _set_foreground_impl(self, window_id: str) -> bool:
        win = self._get_window_impl(window_id)
        if not win or not win.hwnd or win32gui is None:
            return False

        try:
            # If minimized, restore it
            if win32gui.IsIconic(win.hwnd):
                win32gui.ShowWindow(win.hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(win.hwnd, win32con.SW_SHOW)

            # Bring to top using TOPMOST pulse to ensure visibility over background runner
            win32gui.SetWindowPos(win.hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
            win32gui.BringWindowToTop(win.hwnd)
            try:
                win32gui.SetForegroundWindow(win.hwnd)
            except Exception:
                pass
            win32gui.SetWindowPos(win.hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
            win.is_foreground = True
            return True
        except Exception as e:
            logger.warning(f"Failed to set foreground window {window_id}: {e}")
            return False
