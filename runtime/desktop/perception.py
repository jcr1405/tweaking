"""
Presence Vision Perception Component.

Provides local visual perception and coordinate grounding using Qwen 3.5 9B multimodal vision:
- Formats structured perception prompts for UI element detection
- Invokes OllamaModelProvider.generate_multimodal() under LocalInferenceSemaphore
- Extracts normalized bounding boxes [ymin, xmin, ymax, xmax] in [0.0, 1.0]
- Validates bounding boxes and transforms them to physical screen/window coordinates
- Supports pluggable/mock perception engines for deterministic testing
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from runtime.desktop.capture import CapturedScreenshot
from runtime.desktop.models import (
    CropRegion,
    ScreenRect,
    VisualElementCandidate,
    VisualPerceptionResult,
)
from runtime.desktop.roi import (
    transform_crop_to_screenshot,
    transform_screenshot_to_desktop,
)
from runtime.providers.ollama import OllamaModelProvider

logger = logging.getLogger(__name__)

SYSTEM_VISION_PROMPT = """You are a precise Windows desktop UI perception model.
Your task is to analyze the provided screenshot and locate the requested UI elements, buttons, inputs, icons, or labels.
For each detected UI element, output a JSON object with:
- "label": concise descriptive name
- "role": UI role (e.g. "button", "input", "checkbox", "tab", "link", "icon", "text")
- "bbox_normalized": normalized bounding box as [xmin, ymin, xmax, ymax] where each value is a float between 0.0 and 1.0 (xmin=left, ymin=top, xmax=right, ymax=bottom)
- "confidence": confidence score between 0.0 and 1.0
- "visible_text": visible text on or near the element, if any
- "actionable": boolean true if clickable or editable

Output ONLY valid JSON adhering to this schema:
{
  "elements": [
    {
      "label": "string",
      "role": "string",
      "bbox_normalized": [0.1, 0.2, 0.3, 0.4],
      "confidence": 0.95,
      "visible_text": "string",
      "actionable": true
    }
  ],
  "overall_ambiguity": null
}
Do not include any conversational filler. Return only the JSON object."""


class VisionPerception:
    """
    Coordinates local multimodal vision inference on desktop screenshots.
    """

    def __init__(
        self,
        provider: Optional[OllamaModelProvider] = None,
        default_model: str = "qwen3.5:9b",
    ):
        self.provider = provider
        self.default_model = default_model

    def _extract_json_payload(self, text: str) -> Optional[Dict[str, Any]]:
        """Extract and parse JSON object from model output."""
        if not text:
            return None

        cleaned = text.strip()
        # Strip markdown fences cleanly
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

        # Try direct parse
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"elements": parsed, "overall_ambiguity": None}
        except Exception:
            pass

        # Search for first '{' to last '}'
        start_idx = cleaned.find("{")
        end_idx = cleaned.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            try:
                candidate = cleaned[start_idx : end_idx + 1]
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

        # Repair truncated JSON if output stopped mid-stream
        try:
            trimmed = cleaned.strip()
            trimmed = re.sub(r',\s*"[^"]*$', '', trimmed)
            trimmed = re.sub(r',\s*$', '', trimmed)
            open_curly = trimmed.count("{") - trimmed.count("}")
            open_square = trimmed.count("[") - trimmed.count("]")
            if open_curly > 0 or open_square > 0:
                closing = ("]" * max(0, open_square)) + ("}" * max(0, open_curly))
                parsed = json.loads(trimmed + closing)
                if isinstance(parsed, dict):
                    return parsed
        except Exception:
            pass

        # Regex fallback: extract any completed element objects with "label" and "bbox_normalized"
        try:
            found_elems = []
            for m in re.finditer(r'\{[^{}]*"label"[^{}]*"bbox_normalized"[^{}]*\}', cleaned, re.DOTALL):
                try:
                    obj = json.loads(m.group(0))
                    if isinstance(obj, dict) and "bbox_normalized" in obj:
                        found_elems.append(obj)
                except Exception:
                    pass
            if found_elems:
                return {"elements": found_elems, "overall_ambiguity": None}
        except Exception:
            pass

        return None

    def ground_to_desktop_coordinates(
        self,
        candidate: VisualElementCandidate,
        window_bounds: ScreenRect,
    ) -> Tuple[int, int]:
        """
        Transforms a normalized bounding box [ymin, xmin, ymax, xmax] into absolute
        desktop physical screen coordinates (center_x, center_y).
        """
        ymin, xmin, ymax, xmax = candidate.bbox_normalized
        w = window_bounds.width
        h = window_bounds.height

        center_x_norm = (xmin + xmax) / 2.0
        center_y_norm = (ymin + ymax) / 2.0

        abs_x = window_bounds.left + int(center_x_norm * w)
        abs_y = window_bounds.top + int(center_y_norm * h)

        return abs_x, abs_y

    def analyze_screenshot(
        self,
        screenshot: CapturedScreenshot,
        query: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> VisualPerceptionResult:
        """
        Analyzes a captured screenshot using Qwen multimodal vision.
        """
        if not self.provider:
            return VisualPerceptionResult(
                screenshot_id=screenshot.screenshot_id,
                window_id=screenshot.window_id,
                image_width=screenshot.width,
                image_height=screenshot.height,
                observations=[],
                overall_ambiguity="No vision provider configured",
            )

        prompt = (
            f"Identify the interactive UI elements in this window screenshot.\n"
            f"Focus particularly on: {query}\n"
            if query
            else "Identify all interactive controls, buttons, text fields, and tabs in this window screenshot."
        )

        try:
            resp = self.provider.generate_multimodal(
                prompt=prompt,
                images=[screenshot.base64_png],
                system_prompt=SYSTEM_VISION_PROMPT,
                model=self.default_model,
                task_id=task_id or f"vision_{screenshot.screenshot_id}",
                think=False,  # speed up detection
            )
            content = resp.get("content", "")
            data = self._extract_json_payload(content)
            if not data:
                logger.warning("Failed to extract JSON payload from content:\n%s", content)

            observations: List[VisualElementCandidate] = []
            overall_ambiguity = None

            if data and isinstance(data, dict):
                raw_elements = data.get("elements", [])
                overall_ambiguity = data.get("overall_ambiguity")
                for elem in raw_elements:
                    if not isinstance(elem, dict):
                        continue
                    try:
                        bbox = elem.get("bbox_normalized", [])
                        if not isinstance(bbox, list) or len(bbox) != 4:
                            continue
                        candidate = VisualElementCandidate(
                            label=str(elem.get("label", "element")),
                            role=elem.get("role"),
                            bbox_normalized=[float(c) for c in bbox],
                            confidence=float(elem.get("confidence", 1.0)),
                            visible_text=elem.get("visible_text"),
                            actionable=bool(elem.get("actionable", True)),
                            ambiguity=elem.get("ambiguity"),
                        )
                        observations.append(candidate)
                    except Exception as val_err:
                        logger.warning("Skipping invalid visual element candidate %s: %s", elem, val_err)
            else:
                overall_ambiguity = f"Failed to parse structured perception JSON: {content[:100]}"

            return VisualPerceptionResult(
                screenshot_id=screenshot.screenshot_id,
                window_id=screenshot.window_id,
                image_width=screenshot.width,
                image_height=screenshot.height,
                observations=observations,
                overall_ambiguity=overall_ambiguity,
            )
        except Exception as exc:
            logger.exception("Visual perception failed on screenshot %s: %s", screenshot.screenshot_id, exc)
            return VisualPerceptionResult(
                screenshot_id=screenshot.screenshot_id,
                window_id=screenshot.window_id,
                image_width=screenshot.width,
                image_height=screenshot.height,
                observations=[],
                overall_ambiguity=f"Perception inference error: {str(exc)}",
            )

    def analyze_crop(
        self,
        crop: CropRegion,
        window_bounds: ScreenRect,
        query: str,
        task_id: Optional[str] = None,
    ) -> VisualPerceptionResult:
        """
        Analyzes a targeted Region-of-Interest (ROI) crop using Qwen vision.
        Much faster and token-efficient than full 1080p frame analysis.
        """
        import base64

        if not crop.crop_bytes:
            return VisualPerceptionResult(
                screenshot_id=crop.crop_id,
                window_id="crop",
                image_width=crop.crop_width,
                image_height=crop.crop_height,
                observations=[],
                overall_ambiguity="Crop has no image bytes",
            )

        if not self.provider:
            return VisualPerceptionResult(
                screenshot_id=crop.crop_id,
                window_id="crop",
                image_width=crop.crop_width,
                image_height=crop.crop_height,
                observations=[],
                overall_ambiguity="No vision provider configured",
            )

        b64_img = base64.b64encode(crop.crop_bytes).decode("utf-8")
        prompt = (
            f"Within this targeted UI crop, pinpoint: {query}\n"
            f"Return the exact bounding box [ymin, xmin, ymax, xmax] of the target."
        )

        try:
            resp = self.provider.generate_multimodal(
                prompt=prompt,
                images=[b64_img],
                system_prompt=SYSTEM_VISION_PROMPT,
                model=self.default_model,
                task_id=task_id or f"crop_{crop.crop_id}",
                think=False,
            )
            content = resp.get("content", "")
            data = self._extract_json_payload(content)

            observations: List[VisualElementCandidate] = []
            overall_ambiguity = None

            if data and isinstance(data, dict):
                raw_elements = data.get("elements", [])
                overall_ambiguity = data.get("overall_ambiguity")
                for elem in raw_elements:
                    if not isinstance(elem, dict):
                        continue
                    try:
                        bbox = elem.get("bbox_normalized", [])
                        if not isinstance(bbox, list) or len(bbox) != 4:
                            continue
                        candidate = VisualElementCandidate(
                            label=str(elem.get("label", query)),
                            role=elem.get("role"),
                            bbox_normalized=[float(c) for c in bbox],
                            confidence=float(elem.get("confidence", 1.0)),
                            visible_text=elem.get("visible_text"),
                            actionable=bool(elem.get("actionable", True)),
                            ambiguity=elem.get("ambiguity"),
                        )
                        observations.append(candidate)
                    except Exception as val_err:
                        logger.warning("Skipping invalid visual element crop candidate: %s", val_err)

            return VisualPerceptionResult(
                screenshot_id=crop.crop_id,
                window_id="crop",
                image_width=crop.crop_width,
                image_height=crop.crop_height,
                observations=observations,
                overall_ambiguity=overall_ambiguity,
            )
        except Exception as exc:
            logger.exception("Visual crop perception failed on %s: %s", crop.crop_id, exc)
            return VisualPerceptionResult(
                screenshot_id=crop.crop_id,
                window_id="crop",
                image_width=crop.crop_width,
                image_height=crop.crop_height,
                observations=[],
                overall_ambiguity=f"Crop perception inference error: {str(exc)}",
            )
