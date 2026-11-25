"""Presence Fast Visual Grounder & OCR-Detector Fusion Layer.
Spatially correlates native OCR text tokens with lightweight visual UI element detections
via containment and IoU matching to produce grounded, clickable candidate controls in 10-30 ms.
"""

import io
import time
import uuid
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image

from runtime.desktop.models import (
    OCRResult,
    OCRWord,
    OCRLine,
    UIRegionCandidate,
    UIDetectionResult,
    GroundedCandidate,
    GroundingResolution,
    PerceptionRoute,
    ScreenRect,
)
from runtime.desktop.ocr import WindowsMediaOCREngine, BaseOCREngine
from runtime.desktop.detector import LightweightUIDetector, _compute_iou


def _is_point_inside_box(point: Tuple[float, float], box: List[float], margin: float = 0.02) -> bool:
    """Checks if normalized point (cx, cy) is inside normalized box [ymin, xmin, ymax, xmax]."""
    cx, cy = point
    ymin, xmin, ymax, xmax = box
    return (xmin - margin) <= cx <= (xmax + margin) and (ymin - margin) <= cy <= (ymax + margin)


def _compute_containment(inner_box: List[float], outer_box: List[float], margin: float = 0.02) -> float:
    """Computes the fraction of inner_box that is contained within outer_box."""
    i_ymin, i_xmin, i_ymax, i_xmax = inner_box
    o_ymin, o_xmin, o_ymax, o_xmax = outer_box

    # Apply small margin to outer box for bounding imprecisions
    o_ymin = max(0.0, o_ymin - margin)
    o_xmin = max(0.0, o_xmin - margin)
    o_ymax = min(1.0, o_ymax + margin)
    o_xmax = min(1.0, o_xmax + margin)

    inter_ymin = max(i_ymin, o_ymin)
    inter_xmin = max(i_xmin, o_xmin)
    inter_ymax = min(i_ymax, o_ymax)
    inter_xmax = min(i_xmax, o_xmax)

    if inter_ymax <= inter_ymin or inter_xmax <= inter_xmin:
        return 0.0

    inter_area = (inter_ymax - inter_ymin) * (inter_xmax - inter_xmin)
    inner_area = (i_ymax - i_ymin) * (i_xmax - i_xmin)

    if inner_area <= 0:
        return 0.0
    return inter_area / inner_area


class FastVisualGrounder:
    """High-speed perception fusion engine combining native OCR and lightweight UI detection."""

    def __init__(
        self,
        ocr_engine: Optional[BaseOCREngine] = None,
        ui_detector: Optional[LightweightUIDetector] = None,
    ) -> None:
        self.ocr_engine = ocr_engine or WindowsMediaOCREngine()
        self.ui_detector = ui_detector or LightweightUIDetector()

    def fuse(
        self,
        image_bytes: bytes,
        image_id: str = "",
        window_bounds: Optional[ScreenRect] = None,
        dpi_scale: float = 1.0,
    ) -> Tuple[List[GroundedCandidate], float]:
        """Runs OCR + UI Detector and fuses results into grounded candidates."""
        t0 = time.perf_counter()

        # Run OCR and UI Detection
        ocr_res: OCRResult = self.ocr_engine.recognize(image_bytes, image_id=image_id)
        det_res: UIDetectionResult = self.ui_detector.detect_regions(image_bytes, image_id=image_id)

        candidates: List[GroundedCandidate] = []
        matched_word_indices = set()
        matched_regions = set()

        img_w = ocr_res.image_width or det_res.image_width
        img_h = ocr_res.image_height or det_res.image_height

        # 1. Match words inside detected UI regions
        for reg_idx, reg in enumerate(det_res.regions):
            enclosed_words: List[OCRWord] = []
            for w_idx, word in enumerate(ocr_res.words):
                # Check center point containment or high box containment
                w_center = word.center_point
                if _is_point_inside_box(w_center, reg.bbox_normalized) or _compute_containment(word.bbox_normalized, reg.bbox_normalized) > 0.6:
                    enclosed_words.append(word)
                    matched_word_indices.add(w_idx)

            if enclosed_words:
                matched_regions.add(reg_idx)
                # Sort enclosed words left-to-right, top-to-bottom
                sorted_words = sorted(enclosed_words, key=lambda w: (w.bbox_normalized[0], w.bbox_normalized[1]))
                fused_label = " ".join(w.text for w in sorted_words).strip()

                cand_category = reg.category
                if cand_category in ("clickable", "container") and fused_label:
                    cand_category = "button"

                # Calculate physical desktop coordinates if window bounds provided
                desk_coord = None
                if window_bounds:
                    cx = (reg.bbox_normalized[1] + reg.bbox_normalized[3]) / 2.0
                    cy = (reg.bbox_normalized[0] + reg.bbox_normalized[2]) / 2.0
                    desk_x = window_bounds.left + int(cx * window_bounds.width * dpi_scale)
                    desk_y = window_bounds.top + int(cy * window_bounds.height * dpi_scale)
                    desk_coord = (desk_x, desk_y)

                candidates.append(
                    GroundedCandidate(
                        candidate_id=f"gc_{uuid.uuid4().hex[:8]}",
                        label=fused_label,
                        category=cand_category,
                        bbox_normalized=reg.bbox_normalized,
                        confidence=0.96,
                        grounding_source=["ocr", "ui_detector"],
                        pixel_rect=reg.rect,
                        desktop_coordinate=desk_coord,
                        is_ambiguous=False,
                        actionable=True,
                    )
                )

        # 2. Add remaining unmatched OCR lines or words as text candidates
        for line in ocr_res.lines:
            # Check if all words in this line were matched
            unmatched_in_line = [w for w in line.words if w not in [ocr_res.words[i] for i in matched_word_indices]]
            if unmatched_in_line:
                line_label = " ".join(w.text for w in unmatched_in_line).strip()
                if not line_label:
                    continue

                ymin = min(w.bbox_normalized[0] for w in unmatched_in_line)
                xmin = min(w.bbox_normalized[1] for w in unmatched_in_line)
                ymax = max(w.bbox_normalized[2] for w in unmatched_in_line)
                xmax = max(w.bbox_normalized[3] for w in unmatched_in_line)

                desk_coord = None
                if window_bounds:
                    cx = (xmin + xmax) / 2.0
                    cy = (ymin + ymax) / 2.0
                    desk_x = window_bounds.left + int(cx * window_bounds.width * dpi_scale)
                    desk_y = window_bounds.top + int(cy * window_bounds.height * dpi_scale)
                    desk_coord = (desk_x, desk_y)

                p_left = min(w.rect.left for w in unmatched_in_line if w.rect)
                p_top = min(w.rect.top for w in unmatched_in_line if w.rect)
                p_right = max(w.rect.right for w in unmatched_in_line if w.rect)
                p_bottom = max(w.rect.bottom for w in unmatched_in_line if w.rect)
                pixel_rect = ScreenRect(left=p_left, top=p_top, right=p_right, bottom=p_bottom)

                candidates.append(
                    GroundedCandidate(
                        candidate_id=f"gc_{uuid.uuid4().hex[:8]}",
                        label=line_label,
                        category="text",
                        bbox_normalized=[ymin, xmin, ymax, xmax],
                        confidence=0.88,
                        grounding_source=["ocr"],
                        pixel_rect=pixel_rect,
                        desktop_coordinate=desk_coord,
                        is_ambiguous=False,
                        actionable=True,
                    )
                )

        # 3. Add unmatched UI regions (unlabelled buttons, icons, checkboxes)
        for reg_idx, reg in enumerate(det_res.regions):
            if reg_idx not in matched_regions:
                desk_coord = None
                if window_bounds:
                    cx = (reg.bbox_normalized[1] + reg.bbox_normalized[3]) / 2.0
                    cy = (reg.bbox_normalized[0] + reg.bbox_normalized[2]) / 2.0
                    desk_x = window_bounds.left + int(cx * window_bounds.width * dpi_scale)
                    desk_y = window_bounds.top + int(cy * window_bounds.height * dpi_scale)
                    desk_coord = (desk_x, desk_y)

                candidates.append(
                    GroundedCandidate(
                        candidate_id=f"gc_{uuid.uuid4().hex[:8]}",
                        label=f"{reg.category}_{reg_idx}",
                        category=reg.category,
                        bbox_normalized=reg.bbox_normalized,
                        confidence=0.75,
                        grounding_source=["ui_detector"],
                        pixel_rect=reg.rect,
                        desktop_coordinate=desk_coord,
                        is_ambiguous=False,
                        actionable=True,
                    )
                )

        # 4. Deduplicate overlapping candidates with identical labels (keep outer container)
        deduped: List[GroundedCandidate] = []
        # Sort by area descending so outer containers come first
        sorted_by_area = sorted(
            candidates,
            key=lambda c: (c.bbox_normalized[2] - c.bbox_normalized[0]) * (c.bbox_normalized[3] - c.bbox_normalized[1]),
            reverse=True
        )

        for cand in sorted_by_area:
            is_duplicate = False
            for kept in deduped:
                # If they share the exact label and overlap spatially
                if cand.label.lower() == kept.label.lower():
                    iou = _compute_iou(cand.bbox_normalized, kept.bbox_normalized)
                    cont = _compute_containment(cand.bbox_normalized, kept.bbox_normalized)
                    if iou > 0.25 or cont > 0.6:
                        is_duplicate = True
                        break
            if not is_duplicate:
                deduped.append(cand)

        total_latency_ms = (time.perf_counter() - t0) * 1000.0
        return deduped, round(total_latency_ms, 2)

    def ground_target(
        self,
        image_bytes: bytes,
        query: str,
        screenshot_id: str = "",
        window_bounds: Optional[ScreenRect] = None,
        dpi_scale: float = 1.0,
    ) -> GroundingResolution:
        """Resolves target query against visual scene with ambiguity and fallback detection."""
        candidates, latency_ms = self.fuse(
            image_bytes,
            image_id=screenshot_id,
            window_bounds=window_bounds,
            dpi_scale=dpi_scale,
        )

        q_clean = query.strip().lower()

        # Score candidates
        scored_candidates: List[Tuple[float, GroundedCandidate]] = []
        for cand in candidates:
            c_label = cand.label.lower()
            score = 0.0

            if q_clean == c_label:
                score = 1.0
            elif q_clean in c_label:
                score = 0.90
            elif c_label in q_clean:
                score = 0.85
            else:
                # Check token overlap
                q_words = set(q_clean.split())
                c_words = set(c_label.split())
                overlap = q_words.intersection(c_words)
                if overlap:
                    score = 0.65 + 0.15 * (len(overlap) / max(len(q_words), 1))

            if score > 0.6:
                scored_candidates.append((score, cand))

        # Sort by score descending
        scored_candidates.sort(key=lambda x: x[0], reverse=True)

        # Case 1: No match found -> Escalate to Qwen Vision Reasoning
        if not scored_candidates:
            return GroundingResolution(
                target_query=query,
                selected_candidate=None,
                all_candidates=candidates,
                route=PerceptionRoute.QWEN_VISION,
                is_ambiguous=False,
                ambiguity_reason=f"No matching visual control found via OCR/Detector for query: '{query}'",
                confidence=0.0,
            )

        top_score, top_candidate = scored_candidates[0]

        # Case 2: Ambiguity detection
        # If there are multiple spatially distinct candidates with nearly identical top scores (>0.85)
        ambiguous_matches = [c for s, c in scored_candidates if s >= top_score - 0.05 and s >= 0.85]
        distinct_matches: List[GroundedCandidate] = []
        for m in ambiguous_matches:
            is_overlapping = False
            for d in distinct_matches:
                if (
                    _compute_iou(m.bbox_normalized, d.bbox_normalized) > 0.25
                    or _compute_containment(m.bbox_normalized, d.bbox_normalized) > 0.5
                ):
                    is_overlapping = True
                    break
            if not is_overlapping:
                distinct_matches.append(m)

        if len(distinct_matches) > 1:
            top_candidate.is_ambiguous = True
            top_candidate.ambiguity_reason = (
                f"Found {len(distinct_matches)} spatially distinct matching controls with score {top_score:.2f} for query: '{query}'"
            )
            return GroundingResolution(
                target_query=query,
                selected_candidate=top_candidate,
                all_candidates=[c for _, c in scored_candidates],
                route=PerceptionRoute.AMBIGUOUS,
                is_ambiguous=True,
                ambiguity_reason=top_candidate.ambiguity_reason,
                confidence=top_score,
            )

        # Case 3: Clear unambiguous match -> Fast Path Execution
        # Route is OCR_DETECTOR, OCR, or DETECTOR based on grounding source
        route = PerceptionRoute.OCR_DETECTOR
        if top_candidate.grounding_source == ["ocr"]:
            route = PerceptionRoute.OCR
        elif top_candidate.grounding_source == ["ui_detector"]:
            route = PerceptionRoute.DETECTOR

        return GroundingResolution(
            target_query=query,
            selected_candidate=top_candidate,
            all_candidates=[c for _, c in scored_candidates],
            route=route,
            is_ambiguous=False,
            ambiguity_reason=None,
            confidence=top_score,
        )
