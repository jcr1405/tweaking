import sys
import os
import json
import time
import ctypes
from ctypes import wintypes

# Ensure interactive desktop attachment
try:
    h_desk = ctypes.windll.user32.OpenDesktopW("default", 0, False, 0x01FF)
    if h_desk:
        ctypes.windll.user32.SetThreadDesktop(h_desk)
except Exception:
    pass

import win32gui
import win32con
import win32api

state = {
    "button_clicks": 0,
    "feature_toggle": False,
    "text_field_value": "",
    "selected_option": "",
    "list_scroll_pos": 0,
    "custom_canvas_clicks": 0,
    "ready": False,
    "hwnd": 0,
}

state_path = sys.argv[1] if len(sys.argv) > 1 else "harness_state.json"

def save_state():
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

h_edit = 0
h_btn = 0
h_chk = 0
h_combo = 0
h_list = 0
h_custom = 0

def custom_wnd_proc(hwnd, msg, wparam, lparam):
    if msg == win32con.WM_MOUSEACTIVATE:
        return win32con.MA_ACTIVATE
    elif msg in (win32con.WM_LBUTTONDOWN, win32con.WM_LBUTTONUP):
        state["custom_canvas_clicks"] += 1
        save_state()
        return 0
    elif msg == win32con.WM_PAINT:
        hdc, ps = win32gui.BeginPaint(hwnd)
        rect = win32gui.GetClientRect(hwnd)
        brush = win32gui.CreateSolidBrush(win32api.RGB(220, 20, 60))
        win32gui.FillRect(hdc, rect, brush)
        win32gui.DeleteObject(brush)
        win32gui.SetTextColor(hdc, win32api.RGB(255, 255, 255))
        win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
        win32gui.DrawText(
            hdc,
            "CUSTOM VISUAL BUTTON",
            -1,
            rect,
            win32con.DT_CENTER | win32con.DT_VCENTER | win32con.DT_SINGLELINE,
        )
        win32gui.EndPaint(hwnd, ps)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

def wnd_proc(hwnd, msg, wparam, lparam):
    global h_btn, h_chk, h_edit, h_combo, h_list, h_custom
    if msg == win32con.WM_MOUSEACTIVATE:
        return win32con.MA_ACTIVATE
    elif msg == win32con.WM_CREATE:
        return 0
    elif msg == win32con.WM_COMMAND:
        cmd_id = win32api.LOWORD(wparam)
        code = win32api.HIWORD(wparam)
        if cmd_id == 101: # Open Options
            state["button_clicks"] += 1
            save_state()
        elif cmd_id == 102: # Checkbox
            chk_state = win32gui.SendMessage(h_chk, win32con.BM_GETCHECK, 0, 0)
            state["feature_toggle"] = bool(chk_state == win32con.BST_CHECKED)
            save_state()
        elif cmd_id == 103: # Edit
            if code == win32con.EN_CHANGE:
                length = win32gui.GetWindowTextLength(h_edit)
                buf = ctypes.create_unicode_buffer(length + 1)
                win32gui.SendMessage(h_edit, win32con.WM_GETTEXT, length + 1, buf)
                state["text_field_value"] = buf.value
                save_state()
        elif cmd_id == 104: # Combobox
            if code == win32con.CBN_SELCHANGE:
                cur_sel = win32gui.SendMessage(h_combo, win32con.CB_GETCURSEL, 0, 0)
                length = win32gui.SendMessage(h_combo, win32con.CB_GETLBTEXTLEN, cur_sel, 0)
                buf = ctypes.create_unicode_buffer(length + 1)
                win32gui.SendMessage(h_combo, win32con.CB_GETLBTEXT, cur_sel, buf)
                state["selected_option"] = buf.value
                save_state()
        return 0
    elif msg == win32con.WM_VKEYTOITEM:
        return -1
    elif msg == win32con.WM_DESTROY:
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

# Register custom button class
wc_custom = win32gui.WNDCLASS()
wc_custom.lpfnWndProc = custom_wnd_proc
wc_custom.lpszClassName = "PresenceCustomCanvasBtn"
wc_custom.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
try:
    win32gui.RegisterClass(wc_custom)
except Exception:
    pass

# Register main class
wc = win32gui.WNDCLASS()
wc.lpfnWndProc = wnd_proc
wc.lpszClassName = "PresenceAcceptanceHarnessClass"
wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
wc.hbrBackground = win32con.COLOR_WINDOW + 1
try:
    win32gui.RegisterClass(wc)
except Exception:
    pass

hwnd = win32gui.CreateWindowEx(
    win32con.WS_EX_TOPMOST,
    "PresenceAcceptanceHarnessClass",
    "Presence Desktop Acceptance Harness",
    win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
    100, 100, 520, 420,
    0, 0, 0, None
)

state["hwnd"] = hwnd

h_btn = win32gui.CreateWindowEx(
    0, "BUTTON", "Open Options",
    win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
    20, 20, 160, 36, hwnd, 101, 0, None
)

h_chk = win32gui.CreateWindowEx(
    0, "BUTTON", "Feature Toggle",
    win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_AUTOCHECKBOX,
    20, 70, 160, 30, hwnd, 102, 0, None
)

h_edit_lbl = win32gui.CreateWindowEx(
    0, "STATIC", "Harmless Test Field:",
    win32con.WS_CHILD | win32con.WS_VISIBLE,
    20, 110, 180, 20, hwnd, 203, 0, None
)

h_edit = win32gui.CreateWindowEx(
    win32con.WS_EX_CLIENTEDGE, "EDIT", "",
    win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.ES_AUTOHSCROLL,
    20, 135, 200, 28, hwnd, 103, 0, None
)

h_combo = win32gui.CreateWindowEx(
    0, "COMBOBOX", "Select Option",
    win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.CBS_DROPDOWNLIST | win32con.WS_VSCROLL,
    20, 175, 200, 120, hwnd, 104, 0, None
)
for opt in ["Alpha", "Beta", "Gamma"]:
    win32gui.SendMessage(h_combo, win32con.CB_ADDSTRING, 0, opt)
win32gui.SendMessage(h_combo, win32con.CB_SETCURSEL, 0, 0)
state["selected_option"] = "Alpha"

h_list = win32gui.CreateWindowEx(
    win32con.WS_EX_CLIENTEDGE, "LISTBOX", "",
    win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.LBS_NOTIFY | win32con.WS_VSCROLL,
    20, 215, 200, 140, hwnd, 105, 0, None
)
for i in range(1, 21):
    win32gui.SendMessage(h_list, win32con.LB_ADDSTRING, 0, f"Item {i}")

h_custom = win32gui.CreateWindowEx(
    0, "PresenceCustomCanvasBtn", "CustomBtn",
    win32con.WS_CHILD | win32con.WS_VISIBLE,
    250, 20, 230, 60, hwnd, 106, 0, None
)

win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
try:
    win32gui.SetForegroundWindow(hwnd)
except Exception:
    pass
win32gui.UpdateWindow(hwnd)

state["ready"] = True
save_state()

# Run message loop
win32gui.PumpMessages()
