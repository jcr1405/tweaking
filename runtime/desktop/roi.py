"""Presence Desktop Region of Interest (ROI) & Coordinate Transformations.
Handles high-precision visual cropping, parent screenshot ID provenance tracking,
and multi-tier coordinate mapping (Crop -> Screenshot -> Window -> Desktop Virtual Screen).
"""

import io
import uuid
from typing import List, Tuple, Optional
from PIL import Image

from runtime.desktop.models import (
    CropRegion,
    ScreenRect,
)


def extract_crop(
    image_bytes: bytes,
    bbox_normalized: List[float],
    parent_screenshot_id: str,
    padding_px: int = 12
) -> CropRegion:
    """Extracts a Region-of-Interest crop from full screenshot bytes with padding and provenance."""
    pil_img = Image.open(io.BytesIO(image_bytes))
    img_w, img_h = pil_img.size

    ymin, xmin, ymax, xmax = bbox_normalized

    # Convert to physical pixel bounds with padding
    left = max(0, int(xmin * img_w) - padding_px)
    top = max(0, int(ymin * img_h) - padding_px)
    right = min(img_w, int(xmax * img_w) + padding_px)
    bottom = min(img_h, int(ymax * img_h) + padding_px)

    # Ensure valid crop rectangle
    if right <= left:
        right = min(img_w, left + 1)
    if bottom <= top:
        bottom = min(img_h, top + 1)

    cropped_img = pil_img.crop((left, top, right, bottom))
    crop_w, crop_h = cropped_img.size

    buf = io.BytesIO()
    cropped_img.save(buf, format="PNG")
    crop_bytes = buf.getvalue()

    crop_rect = ScreenRect(left=left, top=top, right=right, bottom=bottom)

    return CropRegion(
        crop_id=f"crop_{uuid.uuid4().hex[:8]}",
        parent_screenshot_id=parent_screenshot_id,
        bbox_normalized=[
            top / img_h,
            left / img_w,
            bottom / img_h,
            right / img_w
        ],
        pixel_bounds=crop_rect,
        crop_bytes=crop_bytes,
        crop_width=crop_w,
        crop_height=crop_h
    )


def transform_crop_to_screenshot(
    crop_normalized_point: Tuple[float, float],
    crop_region: CropRegion
) -> Tuple[int, int]:
    """Transforms a normalized point (nx, ny) inside an ROI crop back to parent screenshot pixel coordinates."""
    nx, ny = crop_normalized_point
    rect = crop_region.pixel_bounds
    shot_x = rect.left + int(nx * rect.width)
    shot_y = rect.top + int(ny * rect.height)
    return (shot_x, shot_y)


def transform_screenshot_to_desktop(
    screenshot_point: Tuple[int, int],
    window_bounds: ScreenRect,
    dpi_scale: float = 1.0
) -> Tuple[int, int]:
    """Transforms screenshot pixel coordinates (sx, sy) to absolute physical desktop coordinates."""
    sx, sy = screenshot_point
    desktop_x = window_bounds.left + int(sx * dpi_scale)
    desktop_y = window_bounds.top + int(sy * dpi_scale)
    return (desktop_x, desktop_y)


def ground_normalized_to_desktop(
    norm_point: Tuple[float, float],
    window_bounds: ScreenRect,
    image_size: Optional[Tuple[int, int]] = None,
    dpi_scale: float = 1.0
) -> Tuple[int, int]:
    """Directly transforms normalized coordinates (cx, cy) [0.0..1.0] to desktop virtual screen coordinates."""
    cx, cy = norm_point
    # In window-relative screenshots, normalized coordinates map directly across window_bounds
    desktop_x = window_bounds.left + int(cx * window_bounds.width)
    desktop_y = window_bounds.top + int(cy * window_bounds.height)
    return (desktop_x, desktop_y)
