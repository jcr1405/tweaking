"""Screen Awareness & Situational Desktop Monitoring
Maintains local-first awareness of the active desktop window, user foreground focus,
and environmental privacy boundaries.
Features:
- Foreground window tracking via Win32 GetForegroundWindow & psutil.
- Sensitive window masking (Bitwarden, 1Password, KeePass, InPrivate, Banking, etc.).
- Ephemeral in-memory screen observation with zero cloud transmission and zero disk retention.
- Observation pause/resume controls for user privacy.
- State change listener callbacks for companion awareness.
"""

import time
import logging
from typing import Optional, List, Dict, Any, Callable, Tuple
from dataclasses import dataclass, field
import psutil

try:
    import win32gui
    import win32process
except ImportError:
    win32gui = None
    win32process = None

logger = logging.getLogger("presence.desktop.awareness")

# Canonical sensitive application patterns and window title markers
SENSITIVE_PROCESS_NAMES = {
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe",
    "lastpass.exe", "authenticator.exe", "dashlane.exe", "nordpass.exe",
    "enpass.exe", "credentialui.exe", "consent.exe"
}

SENSITIVE_TITLE_KEYWORDS = (
    "password", "bitwarden", "1password", "keepass", "credential manager",
    "private browsing", "incognito", "inprivate", "sign in", "login",
    "master password", "pin entry", "banking", "two-factor", "2fa",
    "security key", "webauthn", "credit card", "payment"
)


@dataclass
class DesktopStateSnapshot:
    """Represents a momentary snapshot of desktop foreground situational state."""
    timestamp: float
    hwnd: int
    window_title: str
    process_name: str
    pid: int
    is_sensitive: bool
    observation_active: bool
    screen_width: int = 1920
    screen_height: int = 1080
    frame_buffer: Optional[bytes] = None  # Ephemeral in-memory only

    def to_summary(self) -> Dict[str, Any]:
        """Returns safe, privacy-respecting summary metadata."""
        return {
            "timestamp": self.timestamp,
            "window_title": "[MASKED: SENSITIVE WINDOW]" if self.is_sensitive else self.window_title,
            "process_name": self.process_name,
            "pid": self.pid,
            "is_sensitive": self.is_sensitive,
            "observation_active": self.observation_active,
            "has_buffer": self.frame_buffer is not None,
        }


class ScreenAwarenessMonitor:
    """Local-first screen awareness engine monitoring foreground state and privacy."""

    def __init__(self, observation_enabled: bool = True):
        self._observation_active: bool = observation_enabled
        self._last_snapshot: Optional[DesktopStateSnapshot] = None
        self._listeners: List[Callable[[DesktopStateSnapshot], None]] = []
        self._custom_sensitive_keywords: List[str] = []

    @property
    def is_observation_active(self) -> bool:
        """Returns whether screen observation is currently active."""
        return self._observation_active

    def pause_observation(self) -> None:
        """Pauses desktop observation; no screenshots or state capture will occur."""
        self._observation_active = False
        logger.info("Desktop screen observation PAUSED by user or policy.")

    def resume_observation(self) -> None:
        """Resumes desktop observation."""
        self._observation_active = True
        logger.info("Desktop screen observation RESUMED.")

    def toggle_observation(self) -> bool:
        """Toggles observation state and returns the new state."""
        self._observation_active = not self._observation_active
        logger.info(f"Desktop screen observation toggled: active={self._observation_active}")
        return self._observation_active

    def add_sensitive_keyword(self, keyword: str) -> None:
        """Adds a user-defined sensitive title keyword to privacy masking rules."""
        kw = keyword.strip().lower()
        if kw and kw not in self._custom_sensitive_keywords:
            self._custom_sensitive_keywords.append(kw)

    def is_window_sensitive(self, title: str, process_name: str) -> bool:
        """Evaluates whether the specified window contains sensitive content."""
        proc = process_name.lower().strip()
        if proc in SENSITIVE_PROCESS_NAMES:
            return True

        title_lower = title.lower().strip()
        for kw in SENSITIVE_TITLE_KEYWORDS:
            if kw in title_lower:
                return True

        for kw in self._custom_sensitive_keywords:
            if kw in title_lower:
                return True

        return False

    def add_state_listener(self, listener: Callable[[DesktopStateSnapshot], None]) -> None:
        """Subscribes a listener to foreground desktop state transitions."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def sample_current_state(self, capture_frame: bool = False) -> DesktopStateSnapshot:
        """Samples the active foreground window state and returns a snapshot.
        If observation is paused or the window is sensitive, visual frames are strictly suppressed.
        """
        now = time.time()

        if not self._observation_active:
            snap = DesktopStateSnapshot(
                timestamp=now,
                hwnd=0,
                window_title="[OBSERVATION PAUSED]",
                process_name="none",
                pid=0,
                is_sensitive=False,
                observation_active=False,
                frame_buffer=None,
            )
            self._last_snapshot = snap
            return snap

        hwnd = 0
        title = ""
        proc_name = ""
        pid = 0

        if win32gui is not None and win32process is not None:
            try:
                hwnd = win32gui.GetForegroundWindow()
                if hwnd and win32gui.IsWindow(hwnd):
                    title = win32gui.GetWindowText(hwnd) or ""
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid:
                        try:
                            proc = psutil.Process(pid)
                            proc_name = proc.name()
                        except Exception:
                            proc_name = "unknown.exe"
            except Exception as e:
                logger.debug(f"Win32 foreground window query error: {e}")
        else:
            # Fallback for headless or mock test environments
            hwnd = 1001
            title = "Visual Studio Code"
            proc_name = "Code.exe"
            pid = 1234

        is_sens = self.is_window_sensitive(title, proc_name)

        # Ephemeral frame capture strictly conditioned on privacy
        frame_bytes: Optional[bytes] = None
        if capture_frame and not is_sens and self._observation_active:
            try:
                from runtime.desktop.capture import DesktopCaptureService
                cap_service = DesktopCaptureService()
                frame = cap_service.capture_screen()
                if frame is not None and hasattr(frame, "image"):
                    import io
                    buf = io.BytesIO()
                    frame.image.save(buf, format="PNG")
                    frame_bytes = buf.getvalue()
            except Exception as e:
                logger.debug(f"Frame capture error in awareness monitor: {e}")

        snapshot = DesktopStateSnapshot(
            timestamp=now,
            hwnd=hwnd,
            window_title=title,
            process_name=proc_name,
            pid=pid,
            is_sensitive=is_sens,
            observation_active=self._observation_active,
            frame_buffer=frame_bytes,
        )

        # Check if foreground window changed from previous sample
        prev = self._last_snapshot
        self._last_snapshot = snapshot

        if prev and (prev.hwnd != snapshot.hwnd or prev.window_title != snapshot.window_title):
            for listener in list(self._listeners):
                try:
                    listener(snapshot)
                except Exception as e:
                    logger.debug(f"Listener error in awareness monitor: {e}")

        return snapshot

    def get_last_snapshot(self) -> Optional[DesktopStateSnapshot]:
        """Returns the most recent desktop snapshot without triggering new Win32 queries."""
        return self._last_snapshot
