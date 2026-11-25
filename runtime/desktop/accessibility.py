"""Windows UI Automation (UIA) Accessibility Backend
Inspects semantic accessibility trees, extracts filtered control properties,
and executes native control patterns (Invoke, Toggle, Value, Selection, etc.)
with verification and anti-recursion protection.
"""

import ctypes
import logging
import threading
from typing import List, Dict, Optional, Tuple, Any, Callable

try:
    import uiautomation as auto
except ImportError:
    auto = None

from runtime.desktop.models import DesktopElement, DesktopObservation, DesktopWindow, ScreenRect, ObservationSource
from runtime.desktop.windows import ensure_desktop_attachment

logger = logging.getLogger("presence.desktop.accessibility")


class AccessibilityBackend:
    """Provides semantic Windows UI Automation inspection and pattern invocation."""

    def __init__(self, max_depth: int = 5, max_nodes: int = 100, max_text_len: int = 200):
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_text_len = max_text_len
        # Ephemeral element mapping: element_id -> (hwnd, automation_id, name, control_type, bounds)
        self._element_registry: Dict[str, Dict[str, Any]] = {}

    def _run_in_uia_thread(self, target_fn: Callable[[], Any]) -> Any:
        """Executes a callable inside a dedicated thread attached to the interactive desktop."""
        result_holder = []
        error_holder = []

        def _worker():
            ensure_desktop_attachment()
            if auto is None:
                error_holder.append(RuntimeError("uiautomation library is not installed."))
                return
            try:
                with auto.UIAutomationInitializerInThread():
                    res = target_fn()
                    result_holder.append(res)
            except Exception as e:
                error_holder.append(e)

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(timeout=15.0)

        if error_holder:
            raise error_holder[0]
        if not result_holder:
            raise TimeoutError("Accessibility operation timed out.")
        return result_holder[0]

    def inspect_window(self, window: DesktopWindow) -> DesktopObservation:
        """Inspects the accessibility tree of the target window, returning a structured observation."""
        if not window.hwnd:
            raise ValueError(f"Target window '{window.window_id}' has no valid HWND.")

        def _do_inspect() -> DesktopObservation:
            ctrl = auto.ControlFromHandle(window.hwnd)
            if not ctrl or not ctrl.Exists(maxSearchSeconds=0.5):
                raise RuntimeError(f"Failed to bind UIA control from HWND {window.hwnd}.")

            elements: List[DesktopElement] = []
            node_counter = [0]
            truncated = [False]

            def _traverse(node: Any, current_depth: int, parent_el_id: Optional[str]):
                if node_counter[0] >= self.max_nodes:
                    truncated[0] = True
                    return
                if current_depth > self.max_depth:
                    return

                try:
                    children = node.GetChildren()
                except Exception:
                    return

                for child in children:
                    if node_counter[0] >= self.max_nodes:
                        truncated[0] = True
                        break

                    node_counter[0] += 1
                    el_id = f"el_{window.hwnd}_{node_counter[0]}"

                    try:
                        name = (child.Name or "").strip()
                        if len(name) > self.max_text_len:
                            name = name[:self.max_text_len] + "..."

                        auto_id = child.AutomationId or None
                        c_type = child.ControlTypeName or "Control"
                        cls_name = child.ClassName or None
                        enabled = bool(child.IsEnabled)
                        offscreen = bool(child.IsOffscreen)

                        # Bounding rect
                        bounds = None
                        rect = child.BoundingRectangle
                        if rect and (rect.right - rect.left > 0) and (rect.bottom - rect.top > 0):
                            bounds = ScreenRect(left=rect.left, top=rect.top, right=rect.right, bottom=rect.bottom)

                        # Patterns and state
                        patterns: List[str] = []
                        val = None
                        toggle_st = None
                        is_sel = None

                        try:
                            inv_pat = child.GetInvokePattern()
                            if inv_pat:
                                patterns.append("InvokePattern")
                        except Exception:
                            pass

                        try:
                            tog_pat = child.GetTogglePattern()
                            if tog_pat:
                                patterns.append("TogglePattern")
                                # 0=Off, 1=On, 2=Indeterminate
                                st_int = tog_pat.ToggleState
                                toggle_st = "On" if st_int == 1 else ("Off" if st_int == 0 else "Indeterminate")
                        except Exception:
                            pass

                        try:
                            val_pat = child.GetValuePattern()
                            if val_pat:
                                patterns.append("ValuePattern")
                                raw_val = val_pat.Value or ""
                                val = raw_val[:self.max_text_len]
                        except Exception:
                            pass

                        try:
                            sel_pat = child.GetSelectionItemPattern()
                            if sel_pat:
                                patterns.append("SelectionItemPattern")
                                is_sel = bool(sel_pat.IsSelected)
                        except Exception:
                            pass

                        try:
                            exp_pat = child.GetExpandCollapsePattern()
                            if exp_pat:
                                patterns.append("ExpandCollapsePattern")
                        except Exception:
                            pass

                        try:
                            scr_pat = child.GetScrollPattern()
                            if scr_pat:
                                patterns.append("ScrollPattern")
                        except Exception:
                            pass

                        element = DesktopElement(
                            element_id=el_id,
                            name=name,
                            automation_id=auto_id,
                            control_type=c_type,
                            class_name=cls_name,
                            enabled=enabled,
                            offscreen=offscreen,
                            bounds=bounds,
                            supported_patterns=patterns,
                            parent_element_id=parent_el_id,
                            value=val,
                            toggle_state=toggle_st,
                            is_selected=is_sel,
                        )
                        elements.append(element)

                        # Cache element registry metadata for re-resolution
                        self._element_registry[el_id] = {
                            "hwnd": window.hwnd,
                            "automation_id": auto_id,
                            "name": name,
                            "control_type": c_type,
                            "bounds": bounds,
                            "patterns": patterns,
                        }

                        # Recurse
                        _traverse(child, current_depth + 1, el_id)

                    except Exception as err:
                        logger.debug(f"Error reading node properties: {err}")
                        continue

            _traverse(ctrl, 1, None)

            import uuid
            from datetime import datetime, timezone
            obs_id = f"obs_{uuid.uuid4().hex[:8]}"

            return DesktopObservation(
                observation_id=obs_id,
                window_id=window.window_id,
                observed_at=datetime.now(timezone.utc).isoformat(),
                source=ObservationSource.ACCESSIBILITY,
                window=window,
                elements=elements,
                truncated=truncated[0],
                total_nodes_found=node_counter[0],
            )

        return self._run_in_uia_thread(_do_inspect)

    def _resolve_control(self, element_id: str, root_ctrl: Any) -> Optional[Any]:
        """Re-resolves a live UIA control using cached element metadata."""
        meta = self._element_registry.get(element_id)
        if not meta:
            return None

        auto_id = meta.get("automation_id")
        name = meta.get("name")
        c_type = meta.get("control_type")

        # 1. Try matching by AutomationId if available
        if auto_id:
            try:
                candidate = root_ctrl.Control(AutomationId=auto_id, searchDepth=self.max_depth)
                if candidate and candidate.Exists(maxSearchSeconds=0.2):
                    return candidate
            except Exception:
                pass

        # 2. Try matching by Name and ControlType
        if name:
            try:
                candidate = root_ctrl.Control(Name=name, searchDepth=self.max_depth)
                if candidate and candidate.Exists(maxSearchSeconds=0.2):
                    return candidate
            except Exception:
                pass

        # 3. Fallback: traverse and find match
        def _find_match(node: Any, depth: int) -> Optional[Any]:
            if depth > self.max_depth:
                return None
            try:
                for child in node.GetChildren():
                    if auto_id and child.AutomationId == auto_id:
                        return child
                    if name and child.Name == name:
                        if not c_type or child.ControlTypeName == c_type:
                            return child
                    res = _find_match(child, depth + 1)
                    if res:
                        return res
            except Exception:
                pass
            return None

        return _find_match(root_ctrl, 1)

    def invoke_element(self, element_id: str) -> Dict[str, Any]:
        """Invokes a button or command control via InvokePattern."""
        meta = self._element_registry.get(element_id)
        if not meta:
            raise ValueError(f"Unknown or expired element_id '{element_id}'.")

        def _do_invoke():
            root_ctrl = auto.ControlFromHandle(meta["hwnd"])
            target = self._resolve_control(element_id, root_ctrl)
            if not target:
                raise RuntimeError(f"Element '{element_id}' could not be re-resolved in window.")

            inv_pat = None
            if hasattr(target, "GetPattern") and hasattr(auto, "PatternId"):
                inv_pat = target.GetPattern(auto.PatternId.InvokePattern)
            elif hasattr(target, "GetInvokePattern"):
                inv_pat = target.GetInvokePattern()
            if not inv_pat:
                raise RuntimeError(f"Element '{element_id}' ({target.Name}) does not support InvokePattern.")

            inv_pat.Invoke()
            return {"element_id": element_id, "action": "invoke", "status": "invoked", "name": target.Name}

        return self._run_in_uia_thread(_do_invoke)

    def set_toggle(self, element_id: str, desired_state: bool) -> Dict[str, Any]:
        """Idempotently sets a toggle/checkbox to the desired state (True=On, False=Off)."""
        meta = self._element_registry.get(element_id)
        if not meta:
            raise ValueError(f"Unknown or expired element_id '{element_id}'.")

        def _do_toggle():
            root_ctrl = auto.ControlFromHandle(meta["hwnd"])
            target = self._resolve_control(element_id, root_ctrl)
            if not target:
                raise RuntimeError(f"Element '{element_id}' could not be re-resolved in window.")

            tog_pat = None
            if hasattr(target, "GetPattern") and hasattr(auto, "PatternId"):
                tog_pat = target.GetPattern(auto.PatternId.TogglePattern)
            elif hasattr(target, "GetTogglePattern"):
                tog_pat = target.GetTogglePattern()
            if not tog_pat:
                raise RuntimeError(f"Element '{element_id}' ({target.Name}) does not support TogglePattern.")

            current_st = bool(tog_pat.ToggleState == 1)
            before_str = "On" if current_st else "Off"

            if current_st != desired_state:
                tog_pat.Toggle()
                # Verify resulting state
                new_st = bool(tog_pat.ToggleState == 1)
            else:
                new_st = current_st

            meta["toggle_state"] = new_st
            after_str = "On" if new_st else "Off"
            return {
                "element_id": element_id,
                "action": "set_toggle",
                "desired_state": desired_state,
                "before_state": before_str,
                "after_state": after_str,
                "verified": (new_st == desired_state),
            }

        return self._run_in_uia_thread(_do_toggle)

    def set_value(self, element_id: str, text: str) -> Dict[str, Any]:
        """Sets text value into an editable field via ValuePattern."""
        meta = self._element_registry.get(element_id)
        if not meta:
            raise ValueError(f"Unknown or expired element_id '{element_id}'.")

        def _do_set_value():
            root_ctrl = auto.ControlFromHandle(meta["hwnd"])
            target = self._resolve_control(element_id, root_ctrl)
            if not target:
                raise RuntimeError(f"Element '{element_id}' could not be re-resolved in window.")

            val_pat = None
            if hasattr(target, "GetPattern") and hasattr(auto, "PatternId"):
                val_pat = target.GetPattern(auto.PatternId.ValuePattern)
            elif hasattr(target, "GetValuePattern"):
                val_pat = target.GetValuePattern()
            if not val_pat:
                raise RuntimeError(f"Element '{element_id}' ({target.Name}) does not support ValuePattern.")

            before_val = val_pat.Value or ""
            val_pat.SetValue(text)
            after_val = val_pat.Value or ""
            meta["value"] = after_val

            return {
                "element_id": element_id,
                "action": "set_value",
                "before_value": before_val,
                "after_value": after_val,
                "verified": (after_val == text),
            }

        return self._run_in_uia_thread(_do_set_value)

    def select_item(self, element_id: str) -> Dict[str, Any]:
        """Selects an item in a list or combobox via SelectionItemPattern."""
        meta = self._element_registry.get(element_id)
        if not meta:
            raise ValueError(f"Unknown or expired element_id '{element_id}'.")

        def _do_select():
            root_ctrl = auto.ControlFromHandle(meta["hwnd"])
            target = self._resolve_control(element_id, root_ctrl)
            if not target:
                raise RuntimeError(f"Element '{element_id}' could not be re-resolved in window.")

            sel_pat = None
            if hasattr(target, "GetPattern") and hasattr(auto, "PatternId"):
                sel_pat = target.GetPattern(auto.PatternId.SelectionItemPattern)
            elif hasattr(target, "GetSelectionItemPattern"):
                sel_pat = target.GetSelectionItemPattern()
            if not sel_pat:
                raise RuntimeError(f"Element '{element_id}' ({target.Name}) does not support SelectionItemPattern.")

            sel_pat.Select()
            is_sel = bool(sel_pat.IsSelected)
            meta["is_selected"] = is_sel
            return {
                "element_id": element_id,
                "action": "select_item",
                "name": target.Name,
                "is_selected": is_sel,
                "verified": is_sel,
            }

        return self._run_in_uia_thread(_do_select)

    def expand_collapse(self, element_id: str, expand: bool = True) -> Dict[str, Any]:
        """Expands or collapses a dropdown or tree node via ExpandCollapsePattern."""
        meta = self._element_registry.get(element_id)
        if not meta:
            raise ValueError(f"Unknown or expired element_id '{element_id}'.")

        def _do_expand_collapse():
            root_ctrl = auto.ControlFromHandle(meta["hwnd"])
            target = self._resolve_control(element_id, root_ctrl)
            if not target:
                raise RuntimeError(f"Element '{element_id}' could not be re-resolved in window.")

            exp_pat = None
            if hasattr(target, "GetPattern") and hasattr(auto, "PatternId"):
                exp_pat = target.GetPattern(auto.PatternId.ExpandCollapsePattern)
            elif hasattr(target, "GetExpandCollapsePattern"):
                exp_pat = target.GetExpandCollapsePattern()
            if not exp_pat:
                raise RuntimeError(f"Element '{element_id}' ({target.Name}) does not support ExpandCollapsePattern.")

            if expand:
                exp_pat.Expand()
            else:
                exp_pat.Collapse()

            return {
                "element_id": element_id,
                "action": "expand" if expand else "collapse",
                "name": target.Name,
                "verified": True,
            }

        return self._run_in_uia_thread(_do_expand_collapse)

    def scroll(self, element_id: str, direction: str = "down", amount: int = 1) -> Dict[str, Any]:
        """Scrolls a scrollable container via ScrollPattern."""
        meta = self._element_registry.get(element_id)
        if not meta:
            raise ValueError(f"Unknown or expired element_id '{element_id}'.")

        def _do_scroll():
            root_ctrl = auto.ControlFromHandle(meta["hwnd"])
            target = self._resolve_control(element_id, root_ctrl)
            if not target:
                raise RuntimeError(f"Element '{element_id}' could not be re-resolved in window.")

            scr_pat = None
            if hasattr(target, "GetPattern") and hasattr(auto, "PatternId"):
                scr_pat = target.GetPattern(auto.PatternId.ScrollPattern)
            elif hasattr(target, "GetScrollPattern"):
                scr_pat = target.GetScrollPattern()
            if not scr_pat:
                raise RuntimeError(f"Element '{element_id}' ({target.Name}) does not support ScrollPattern.")

            # ScrollAmount: 0=LargeDecrement, 1=SmallDecrement, 2=NoAmount, 3=LargeIncrement, 4=SmallIncrement
            # 2 = NoAmount
            h_amount = 2
            v_amount = 2
            if direction == "down":
                v_amount = 4  # SmallIncrement
            elif direction == "up":
                v_amount = 1  # SmallDecrement
            elif direction == "right":
                h_amount = 4
            elif direction == "left":
                h_amount = 1

            for _ in range(max(1, amount)):
                scr_pat.Scroll(h_amount, v_amount)

            return {
                "element_id": element_id,
                "action": "scroll",
                "direction": direction,
                "amount": amount,
                "verified": True,
            }

        return self._run_in_uia_thread(_do_scroll)

    def get_registered_element(self, element_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves cached metadata for an ephemeral element_id."""
        return self._element_registry.get(element_id)

