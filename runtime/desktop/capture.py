"""Screen Capture Backend
Provides targeted, memory-bounded window and display captures for visual perception.
Enforces local-only retention, privacy boundaries, and screenshot identity tracking.
"""

import io
import time
import uuid
import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, Union
from PIL import Image, ImageGrab

from runtime.desktop.models import ScreenRect, DesktopWindow
from runtime.desktop.windows import ensure_desktop_attachment

logger = logging.getLogger("presence.desktop.capture")


class CapturedScreenshot:
    """Represents an in-memory captured screenshot with identity and provenance."""

    def __init__(
        self,
        screenshot_id: str,
        image: Image.Image,
        image_bytes: Optional[bytes] = None,
        digest_sha256: Optional[str] = None,
        window_id: str = "desktop_root",
        bounds: Optional[ScreenRect] = None,
        captured_at: Optional[str] = None,
        dpi_scale: float = 1.0,
    ):
        self.screenshot_id = screenshot_id
        self.image = image
        if image_bytes is None:
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            self.image_bytes = buf.getvalue()
        else:
            self.image_bytes = image_bytes

        if digest_sha256 is None:
            self.digest_sha256 = hashlib.sha256(self.image_bytes).hexdigest()
        else:
            self.digest_sha256 = digest_sha256

        self.window_id = window_id
        self.bounds = bounds or ScreenRect(left=0, top=0, right=image.width, bottom=image.height)
        self.captured_at = captured_at or datetime.now(timezone.utc).isoformat()
        self.dpi_scale = dpi_scale
        self.width, self.height = image.size
        self._created_time = time.time()

    @property
    def sha256(self) -> str:
        return self.digest_sha256

    @property
    def base64_png(self) -> str:
        import base64
        return base64.b64encode(self.image_bytes).decode("utf-8")

    def is_expired(self, max_age_seconds: float = 60.0) -> bool:
        return (time.time() - self._created_time) > max_age_seconds

    @property
    def captured_at_timestamp(self) -> float:
        return self._created_time


class ScreenCaptureBackend:
    """Manages window and screen captures with ephemeral in-memory storage."""

    def __init__(self, windows_manager: Optional[Any] = None, max_cache_entries: int = 5):
        self.windows_manager = windows_manager
        self.max_cache_entries = max_cache_entries
        self._cache: Dict[str, CapturedScreenshot] = {}

    def capture_window(self, window: Union[DesktopWindow, str]) -> CapturedScreenshot:
        """Captures only the target window's visible bounds."""
        target_win: Optional[DesktopWindow] = None
        if isinstance(window, str):
            if not self.windows_manager:
                raise ValueError("Cannot resolve window_id without a configured WindowsManager")
            target_win = self.windows_manager.get_window(window)
            if not target_win:
                raise ValueError(f"Window with id '{window}' not found")
        else:
            target_win = window

        bounds = target_win.bounds
        if bounds.width <= 0 or bounds.height <= 0:
            raise ValueError(f"Invalid window bounds for capture: {bounds}")

        bbox = (bounds.left, bounds.top, bounds.right, bounds.bottom)
        return self._capture_bbox(bbox=bbox, window_id=target_win.window_id, bounds=bounds)

    def capture_screen(self, region: Optional[ScreenRect] = None) -> CapturedScreenshot:
        """Captures the full display or an explicitly bounded screen region."""
        bbox = None
        bounds = region
        if region:
            bbox = (region.left, region.top, region.right, region.bottom)
        return self._capture_bbox(bbox=bbox, window_id="desktop_root", bounds=bounds)

    def _capture_bbox(
        self,
        bbox: Optional[Tuple[int, int, int, int]],
        window_id: str,
        bounds: Optional[ScreenRect] = None,
    ) -> CapturedScreenshot:
        """Thread-safe image grab on interactive desktop."""
        result_holder = []
        error_holder = []

        def _do_grab():
            ensure_desktop_attachment()
            try:
                if bbox:
                    im = ImageGrab.grab(bbox=bbox, all_screens=True)
                else:
                    im = ImageGrab.grab(all_screens=True)

                # Ensure RGB mode
                if im.mode != "RGB":
                    im = im.convert("RGB")
                result_holder.append(im)
            except Exception as e:
                error_holder.append(e)

        thread = threading.Thread(target=_do_grab)
        thread.start()
        thread.join(timeout=10.0)

        if error_holder:
            raise error_holder[0]
        if not result_holder:
            raise TimeoutError("Screen capture operation timed out.")

        image = result_holder[0]
        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=True)
        img_bytes = buf.getvalue()
        sha256_digest = hashlib.sha256(img_bytes).hexdigest()

        shot_id = f"shot_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        effective_bounds = bounds or ScreenRect(left=0, top=0, right=image.width, bottom=image.height)

        captured = CapturedScreenshot(
            screenshot_id=shot_id,
            image=image,
            image_bytes=img_bytes,
            digest_sha256=sha256_digest,
            window_id=window_id,
            bounds=effective_bounds,
            captured_at=now_iso,
        )

        self._cache[shot_id] = captured
        self._prune_cache()
        logger.info(f"Captured screenshot '{shot_id}' for window '{window_id}' ({image.width}x{image.height})")
        return captured

    def _prune_cache(self) -> None:
        """Evicts oldest entries when cache exceeds max capacity."""
        while len(self._cache) > self.max_cache_entries:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k]._created_time)
            del self._cache[oldest_key]

    def get_screenshot(self, screenshot_id: str) -> Optional[CapturedScreenshot]:
        """Retrieves a cached screenshot by ID if not expired."""
        shot = self._cache.get(screenshot_id)
        if shot:
            if shot.is_expired():
                del self._cache[screenshot_id]
                return None
            return shot
        return None

    def clear_cache(self) -> None:
        """Purges all in-memory screenshot captures."""
        self._cache.clear()
