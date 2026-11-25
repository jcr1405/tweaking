"""Presence Desktop Fast Local OCR Engine.
Utilizes native Windows 11 Windows.Media.Ocr via WinRT/winsdk for hardware-accelerated,
sub-10ms offline text detection and bounding-box extraction.
Guarantees zero network calls and zero cloud data leakage.
"""

import io
import time
import uuid
import asyncio
from typing import List, Optional, Tuple
from PIL import Image

from runtime.desktop.models import (
    OCRWord,
    OCRLine,
    OCRResult,
    ScreenRect,
)

# Detect winsdk availability
WINSDK_AVAILABLE = False
try:
    import winsdk.windows.media.ocr as win_ocr
    import winsdk.windows.graphics.imaging as win_imaging
    import winsdk.windows.storage.streams as win_streams
    WINSDK_AVAILABLE = True
except ImportError:
    WINSDK_AVAILABLE = False


class BaseOCREngine:
    """Abstract base contract for local OCR engines."""

    def recognize(self, image_bytes: bytes, image_id: str = "") -> OCRResult:
        raise NotImplementedError

    def is_available(self) -> bool:
        return True


class WindowsMediaOCREngine(BaseOCREngine):
    """Native Windows 11 OCR engine using Windows.Media.Ocr via winsdk."""

    def __init__(self, language_tag: str = "en-US") -> None:
        self.language_tag = language_tag
        self._engine = None
        self._initialized = False

    def is_available(self) -> bool:
        return WINSDK_AVAILABLE

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        if not WINSDK_AVAILABLE:
            raise RuntimeError("winsdk is not installed or available on this system.")
        
        try:
            self._engine = win_ocr.OcrEngine.try_create_from_user_profile_languages()
            if self._engine is None:
                import winsdk.windows.globalization as glob
                lang = glob.Language(self.language_tag)
                self._engine = win_ocr.OcrEngine.try_create_from_language(lang)
            if self._engine is None:
                raise RuntimeError(f"Could not initialize Windows.Media.Ocr for language {self.language_tag}")
            self._initialized = True
        except Exception as e:
            raise RuntimeError(f"Failed to initialize WindowsMediaOCREngine: {e}") from e

    async def _recognize_async(self, image_bytes: bytes, image_id: str) -> OCRResult:
        self._ensure_initialized()
        t0 = time.perf_counter()

        # Load image via PIL to get dimensions and convert to standard format if needed
        pil_img = Image.open(io.BytesIO(image_bytes))
        width, height = pil_img.size

        # Create WinRT InMemoryRandomAccessStream from bytes
        writer = win_streams.DataWriter()
        writer.write_bytes(image_bytes)
        ibuf = writer.detach_buffer()

        stream = win_streams.InMemoryRandomAccessStream()
        await stream.write_async(ibuf)
        stream.seek(0)

        decoder = await win_imaging.BitmapDecoder.create_async(stream)
        software_bitmap = await decoder.get_software_bitmap_async()

        # Execute OCR recognition
        win_res = await self._engine.recognize_async(software_bitmap)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        lines: List[OCRLine] = []
        all_words: List[OCRWord] = []

        for line_idx, win_line in enumerate(win_res.lines):
            line_words: List[OCRWord] = []
            for w in win_line.words:
                r = w.bounding_rect
                # Normalized bbox [ymin, xmin, ymax, xmax] clamped to [0.0, 1.0]
                xmin = max(0.0, min(1.0, float(r.x) / width))
                ymin = max(0.0, min(1.0, float(r.y) / height))
                xmax = max(0.0, min(1.0, float(r.x + r.width) / width))
                ymax = max(0.0, min(1.0, float(r.y + r.height) / height))

                # Ensure non-inverted
                if xmax < xmin:
                    xmax = xmin
                if ymax < ymin:
                    ymax = ymin

                rect = ScreenRect(
                    left=int(r.x),
                    top=int(r.y),
                    right=int(r.x + r.width),
                    bottom=int(r.y + r.height)
                )

                word_obj = OCRWord(
                    text=w.text.strip(),
                    confidence=1.0,
                    bbox_normalized=[ymin, xmin, ymax, xmax],
                    rect=rect
                )
                line_words.append(word_obj)
                all_words.append(word_obj)

            if line_words:
                # Compute line bounds from constituent words
                line_ymin = min(w.bbox_normalized[0] for w in line_words)
                line_xmin = min(w.bbox_normalized[1] for w in line_words)
                line_ymax = max(w.bbox_normalized[2] for w in line_words)
                line_xmax = max(w.bbox_normalized[3] for w in line_words)

                line_rect = ScreenRect(
                    left=min(w.rect.left for w in line_words if w.rect),
                    top=min(w.rect.top for w in line_words if w.rect),
                    right=max(w.rect.right for w in line_words if w.rect),
                    bottom=max(w.rect.bottom for w in line_words if w.rect),
                )

                line_obj = OCRLine(
                    text=win_line.text.strip(),
                    words=line_words,
                    bbox_normalized=[line_ymin, line_xmin, line_ymax, line_xmax],
                    rect=line_rect
                )
                lines.append(line_obj)

        full_text = " ".join(line.text for line in lines).strip()

        return OCRResult(
            image_id=image_id or f"ocr_{uuid.uuid4().hex[:8]}",
            lines=lines,
            words=all_words,
            full_text=full_text,
            latency_ms=round(latency_ms, 2),
            image_width=width,
            image_height=height
        )

    def recognize(self, image_bytes: bytes, image_id: str = "") -> OCRResult:
        """Synchronously recognize text from raw image bytes."""
        try:
            # If an event loop is already running in this thread, execute in separate thread
            _ = asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(self._recognize_async(image_bytes, image_id))).result()
        except RuntimeError:
            # No running event loop in current thread
            return asyncio.run(self._recognize_async(image_bytes, image_id))


class MockOCREngine(BaseOCREngine):
    """Deterministic OCR engine for testing without OS dependencies."""

    def __init__(self, predefined_results: Optional[List[Tuple[str, List[float]]]] = None) -> None:
        self.predefined_results = predefined_results or []

    def recognize(self, image_bytes: bytes, image_id: str = "") -> OCRResult:
        pil_img = Image.open(io.BytesIO(image_bytes))
        width, height = pil_img.size
        words: List[OCRWord] = []
        lines: List[OCRLine] = []

        for text, bbox in self.predefined_results:
            ymin, xmin, ymax, xmax = bbox
            rect = ScreenRect(
                left=int(xmin * width),
                top=int(ymin * height),
                right=int(xmax * width),
                bottom=int(ymax * height)
            )
            w = OCRWord(
                text=text,
                confidence=0.99,
                bbox_normalized=bbox,
                rect=rect
            )
            words.append(w)
            lines.append(OCRLine(
                text=text,
                words=[w],
                bbox_normalized=bbox,
                rect=rect
            ))

        return OCRResult(
            image_id=image_id or f"mock_ocr_{uuid.uuid4().hex[:8]}",
            lines=lines,
            words=words,
            full_text=" ".join(w.text for w in words),
            latency_ms=1.5,
            image_width=width,
            image_height=height
        )


def find_text_candidates(ocr_result: OCRResult, query: str, case_sensitive: bool = False) -> List[OCRWord]:
    """Finds matching words or word groups in an OCR result."""
    q = query if case_sensitive else query.lower()
    matches: List[OCRWord] = []
    
    # 1. Exact or partial word match
    for w in ocr_result.words:
        w_text = w.text if case_sensitive else w.text.lower()
        if q == w_text or q in w_text:
            matches.append(w)
            
    # 2. Line level match if multi-word query and individual words didn't match
    if not matches and " " in query:
        for line in ocr_result.lines:
            l_text = line.text if case_sensitive else line.text.lower()
            if q in l_text:
                # Return constituent words from matching line
                matches.extend(line.words)
                
    return matches
