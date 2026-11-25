"""Presence Desktop Interaction Subsystem Models
Defines typed, bounded data structures for Windows accessibility trees,
window properties, screen captures, visual grounding, desktop actions,
and state verification.
"""

from enum import Enum
from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field, ConfigDict, field_validator


class ObservationSource(str, Enum):
    ACCESSIBILITY = "accessibility"
    VISION = "vision"
    HYBRID = "hybrid"
    OCR = "ocr"
    DETECTOR = "detector"


class PerceptionRoute(str, Enum):
    """Execution route determined by the perception cascading router."""
    UIA = "uia"
    OCR = "ocr"
    DETECTOR = "detector"
    OCR_DETECTOR = "ocr_detector"
    QWEN_VISION = "qwen_vision"
    AMBIGUOUS = "ambiguous"


class ActionStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ActionRiskCategory(str, Enum):
    """Action-level risk classification categories."""
    OBSERVATION = "observation"  # Category A: read-only, L0
    ROUTINE_REVERSIBLE = "routine_reversible"  # Category B: non-destructive UI interaction, L2
    PERSISTENT_DESTRUCTIVE = "persistent_destructive"  # Category C: file delete/overwrite, config changes (Confirmation)
    CONSEQUENTIAL_EXTERNAL = "consequential_external"  # Category D: external messages, payments, transactions (Exact action-bound token)


class ActionType(str, Enum):
    INVOKE_ELEMENT = "invoke_element"
    SET_TOGGLE = "set_toggle"
    SET_VALUE = "set_value"
    SELECT_ITEM = "select_item"
    EXPAND_COLLAPSE = "expand_collapse"
    SCROLL = "scroll"
    CLICK_COORDINATE = "click_coordinate"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    MOVE_MOUSE = "move_mouse"
    DRAG = "drag"
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    PRESS_SHORTCUT = "press_shortcut"
    WAIT_FOR_CONDITION = "wait_for_condition"


class ScreenRect(BaseModel):
    """Represents a bounded screen rectangle in physical desktop pixels."""
    model_config = ConfigDict(extra="ignore")

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom

    def center(self) -> Tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)


class DesktopWindow(BaseModel):
    """Metadata describing a visible top-level Windows application window."""
    model_config = ConfigDict(extra="ignore")

    window_id: str = Field(..., description="Stable ephemeral window identifier (e.g. win_<hwnd>_<pid>)")
    title: str = Field(..., description="Window title text")
    process_id: int = Field(..., description="Operating system process ID")
    process_name: str = Field(..., description="Process executable name (e.g. explorer.exe)")
    bounds: ScreenRect = Field(..., description="Window bounding rectangle")
    is_foreground: bool = Field(False, description="Whether this window currently has foreground focus")
    is_visible: bool = Field(True, description="Whether the window is visible on desktop")
    hwnd: Optional[int] = Field(None, description="Native Win32 window handle")


class DesktopElement(BaseModel):
    """Filtered semantic element extracted from the Windows UI Automation tree."""
    model_config = ConfigDict(extra="ignore")

    element_id: str = Field(..., description="Ephemeral observation-scoped element ID (e.g. el_<token>)")
    name: str = Field(..., description="Accessible element name")
    automation_id: Optional[str] = Field(None, description="Native UIA AutomationId")
    control_type: str = Field(..., description="UIA ControlType (e.g. Button, Edit, CheckBox)")
    class_name: Optional[str] = Field(None, description="Underlying Win32 class name")
    enabled: bool = Field(True, description="Whether the control is enabled")
    offscreen: bool = Field(False, description="Whether the control is scrolled offscreen")
    bounds: Optional[ScreenRect] = Field(None, description="Element bounding rectangle on screen")
    supported_patterns: List[str] = Field(default_factory=list, description="Supported UIA control patterns")
    parent_element_id: Optional[str] = Field(None, description="Parent element identifier in hierarchy")
    value: Optional[str] = Field(None, description="Current text/numeric value if ValuePattern supported")
    toggle_state: Optional[str] = Field(None, description="Current toggle state ('On', 'Off', 'Indeterminate')")
    is_selected: Optional[bool] = Field(None, description="Selection state if SelectionItem supported")


class DesktopObservation(BaseModel):
    """Structured observation snapshot of a window's accessibility or visual state."""
    model_config = ConfigDict(extra="ignore")

    observation_id: str = Field(..., description="Unique observation snapshot identifier")
    window_id: str = Field(..., description="Target window identifier")
    observed_at: str = Field(..., description="ISO 8601 capture timestamp")
    source: ObservationSource = Field(ObservationSource.ACCESSIBILITY, description="Source modality")
    window: DesktopWindow = Field(..., description="Target window metadata snapshot")
    elements: List[DesktopElement] = Field(default_factory=list, description="Filtered accessible controls")
    screenshot_id: Optional[str] = Field(None, description="Associated screenshot ID if captured")
    truncated: bool = Field(False, description="True if accessibility tree exceeded budget bounds")
    total_nodes_found: int = Field(0, description="Total raw nodes enumerated before filtering")


class VisualElementCandidate(BaseModel):
    """Visual perception candidate grounded in a screenshot."""
    model_config = ConfigDict(extra="ignore")

    label: str = Field(..., description="Descriptive label of the detected visual element")
    role: Optional[str] = Field(None, description="Inferred UI role (button, input, toggle, text, icon)")
    bbox_normalized: List[float] = Field(..., description="Normalized bounding box [ymin, xmin, ymax, xmax] in 0.0..1.0")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="Model perception confidence")
    visible_text: Optional[str] = Field(None, description="Visible text inside the element")
    actionable: bool = Field(True, description="Whether the element appears interactive")
    ambiguity: Optional[str] = Field(None, description="Notes on visual ambiguity or clutter")

    @field_validator("bbox_normalized")
    @classmethod
    def validate_bbox(cls, v: List[float]) -> List[float]:
        if len(v) != 4:
            raise ValueError("bbox_normalized must contain exactly 4 floats [ymin, xmin, ymax, xmax]")
        for coord in v:
            if coord < 0.0 or coord > 1.0:
                raise ValueError(f"Normalized coordinate {coord} is out of bounds [0.0, 1.0]")
        ymin, xmin, ymax, xmax = v
        if ymax < ymin or xmax < xmin:
            raise ValueError(f"Invalid bounding box dimensions: min > max in {v}")
        return v

    @property
    def center_point(self) -> Tuple[float, float]:
        """Returns normalized (cx, cy) center coordinate."""
        ymin, xmin, ymax, xmax = self.bbox_normalized
        return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)


class VisualPerceptionResult(BaseModel):
    """Structured response from local Qwen vision inference."""
    model_config = ConfigDict(extra="ignore")

    screenshot_id: str = Field(..., description="Source screenshot identifier")
    window_id: str = Field(..., description="Associated window identifier")
    image_width: int = Field(..., description="Image width in pixels")
    image_height: int = Field(..., description="Image height in pixels")
    observations: List[VisualElementCandidate] = Field(default_factory=list, description="Detected visual elements")
    overall_ambiguity: Optional[str] = Field(None, description="Overall scene ambiguity or warning")


class OCRWord(BaseModel):
    """Single word detected by OCR with normalized and pixel bounds."""
    model_config = ConfigDict(extra="ignore")

    text: str
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    bbox_normalized: List[float] = Field(..., description="[ymin, xmin, ymax, xmax] in 0.0..1.0")
    rect: Optional[ScreenRect] = None

    @field_validator("bbox_normalized")
    @classmethod
    def validate_bbox(cls, v: List[float]) -> List[float]:
        if len(v) != 4:
            raise ValueError("bbox_normalized must contain 4 floats [ymin, xmin, ymax, xmax]")
        for c in v:
            if c < 0.0 or c > 1.0:
                raise ValueError(f"Normalized coordinate {c} out of bounds")
        if v[2] < v[0] or v[3] < v[1]:
            raise ValueError(f"Invalid bounding box dimensions in {v}")
        return v

    @property
    def center_point(self) -> Tuple[float, float]:
        ymin, xmin, ymax, xmax = self.bbox_normalized
        return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)


class OCRLine(BaseModel):
    """Single line of text detected by OCR."""
    model_config = ConfigDict(extra="ignore")

    text: str
    words: List[OCRWord] = Field(default_factory=list)
    bbox_normalized: List[float] = Field(..., description="[ymin, xmin, ymax, xmax]")
    rect: Optional[ScreenRect] = None


class OCRResult(BaseModel):
    """Full OCR recognition result for an image."""
    model_config = ConfigDict(extra="ignore")

    image_id: str
    lines: List[OCRLine] = Field(default_factory=list)
    words: List[OCRWord] = Field(default_factory=list)
    full_text: str = ""
    latency_ms: float = 0.0
    image_width: int = 0
    image_height: int = 0


class UIRegionCandidate(BaseModel):
    """Candidate interactive UI region detected by lightweight UI detector."""
    model_config = ConfigDict(extra="ignore")

    region_id: str
    category: str = Field("clickable", description="Category: button, input, icon, checkbox, card, container")
    bbox_normalized: List[float] = Field(..., description="[ymin, xmin, ymax, xmax]")
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    rect: Optional[ScreenRect] = None
    has_text: bool = False
    associated_text: Optional[str] = None

    @field_validator("bbox_normalized")
    @classmethod
    def validate_bbox(cls, v: List[float]) -> List[float]:
        if len(v) != 4:
            raise ValueError("bbox_normalized must contain 4 floats")
        for c in v:
            if c < 0.0 or c > 1.0:
                raise ValueError(f"Normalized coordinate {c} out of bounds")
        if v[2] < v[0] or v[3] < v[1]:
            raise ValueError(f"Invalid bounding box dimensions in {v}")
        return v

    @property
    def center_point(self) -> Tuple[float, float]:
        ymin, xmin, ymax, xmax = self.bbox_normalized
        return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)


class UIDetectionResult(BaseModel):
    """Output from lightweight UI detector."""
    model_config = ConfigDict(extra="ignore")

    image_id: str
    regions: List[UIRegionCandidate] = Field(default_factory=list)
    latency_ms: float = 0.0
    image_width: int = 0
    image_height: int = 0


class GroundedCandidate(BaseModel):
    """Fused perception candidate grounded via OCR, UI detector, or multimodal vision."""
    model_config = ConfigDict(extra="ignore")

    candidate_id: str
    label: str
    category: str = "control"
    bbox_normalized: List[float] = Field(..., description="[ymin, xmin, ymax, xmax]")
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    grounding_source: List[str] = Field(default_factory=list, description="['ocr', 'ui_detector', 'qwen_vision']")
    pixel_rect: Optional[ScreenRect] = None
    desktop_coordinate: Optional[Tuple[int, int]] = None
    is_ambiguous: bool = False
    ambiguity_reason: Optional[str] = None
    actionable: bool = True

    @property
    def center_point(self) -> Tuple[float, float]:
        ymin, xmin, ymax, xmax = self.bbox_normalized
        return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)


class GroundingResolution(BaseModel):
    """Result of attempting to ground a target query against visual observation."""
    model_config = ConfigDict(extra="ignore")

    target_query: str
    selected_candidate: Optional[GroundedCandidate] = None
    all_candidates: List[GroundedCandidate] = Field(default_factory=list)
    route: PerceptionRoute = PerceptionRoute.OCR_DETECTOR
    is_ambiguous: bool = False
    ambiguity_reason: Optional[str] = None
    confidence: float = 0.0


class CropRegion(BaseModel):
    """Region of Interest (ROI) cropped from a parent screenshot."""
    model_config = ConfigDict(extra="ignore")

    crop_id: str
    parent_screenshot_id: str
    bbox_normalized: List[float]
    pixel_bounds: ScreenRect
    crop_bytes: Optional[bytes] = None
    crop_width: int = 0
    crop_height: int = 0


class DesktopAction(BaseModel):
    """Specification of a single grounded desktop action."""
    model_config = ConfigDict(extra="ignore")

    action_id: str = Field(..., description="Unique action step identifier")
    action_type: ActionType = Field(..., description="Action operation type")
    element_id: Optional[str] = Field(None, description="Target UIA element ID for semantic operations")
    window_id: Optional[str] = Field(None, description="Target window ID")
    coordinate: Optional[List[int]] = Field(None, description="Target physical desktop coordinates [x, y]")
    drag_to: Optional[List[int]] = Field(None, description="Destination coordinates for drag operations [x, y]")
    text: Optional[str] = Field(None, description="Text to type or set into field")
    desired_state: Optional[bool] = Field(None, description="Desired state for toggle/checkbox operations (True=On, False=Off)")
    key: Optional[str] = Field(None, description="Single key name to press (e.g. Enter, Tab, Escape)")
    shortcut: Optional[List[str]] = Field(None, description="Key combination shortcut (e.g. ['ctrl', 's'])")
    scroll_direction: Optional[str] = Field("down", description="Scroll direction ('up', 'down', 'left', 'right')")
    scroll_amount: Optional[int] = Field(1, description="Number of scroll steps")
    screenshot_id: Optional[str] = Field(None, description="Grounded screenshot ID for coordinate actions")
    expected_post_condition: Optional[str] = Field(None, description="Optional assertion condition to verify after action")
    precondition: Optional[str] = Field(None, description="Optional assertion precondition before acting")
    consent_token: Optional[str] = Field(None, description="Single-use action-bound consent token for destructive or consequential actions")
    conversation_id: Optional[str] = Field(None, description="Parent conversation ID binding")
    task_id: Optional[str] = Field(None, description="Delegated task ID binding")
    target_element_name: Optional[str] = Field(None, description="Accessible control name if known")


class DesktopActionResult(BaseModel):
    """Result of an executed desktop action including pre/post observations and verification."""
    model_config = ConfigDict(extra="ignore")

    action_id: str
    status: ActionStatus
    target_window_id: Optional[str] = None
    target_element_id: Optional[str] = None
    action_type: str
    started_at: str
    completed_at: Optional[str] = None
    observed_before: Dict[str, Any] = Field(default_factory=dict)
    observed_after: Dict[str, Any] = Field(default_factory=dict)
    verified: bool = False
    failure_reason: Optional[str] = None


class DesktopBatchResult(BaseModel):
    """Result of executing an ordered batch of desktop actions."""
    model_config = ConfigDict(extra="ignore")

    batch_id: str
    window_id: str
    status: ActionStatus
    total_actions: int
    completed_actions: int
    action_results: List[DesktopActionResult] = Field(default_factory=list)
    stopped_on_failure: bool = False
    summary: str = ""
