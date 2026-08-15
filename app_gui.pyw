import os
import re
import shutil
import sys
import datetime
import time
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import queue
import customtkinter as ctk
import pystray

# Import logic from our existing scripts
from auto_marker import (
    TemplateMatcher,
    get_clipboard_image,
    get_clipboard_image_hash,
    get_clipboard_sequence_number,
    read_image_file,
)
from config import (
    APP_MUTEX_NAME,
    APP_NAME,
    QUERIES_DIR,
    OUTPUT_DIR,
    MATCH_THRESHOLD,
    POLL_INTERVAL,
    ENABLE_OCR_TIMESTAMP_FILTER,
    VERBOSE_LOGGING,
    RESOURCE_DIR,
)
from ocr_utils import warm_up_card_ocr
from library_win import LibraryWindow
from preview_win import PreviewWindow
from query_organizer import QueryAutoCollector, MAX_QUERY_COUNT

def _get_window_process_name(hwnd):
    """Return the lowercase exe name for a Win32 window handle."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # ctypes defaults pointer-returning Win32 calls to 32-bit c_int. Declare
        # the signatures explicitly so HWND/HANDLE values are not truncated on
        # 64-bit Windows when clipboard filtering runs before the hotkey thread.
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        if not hwnd:
            return ""
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        process = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not process:
            return ""
        try:
            path = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(path))
            if not kernel32.QueryFullProcessImageNameW(
                process, 0, path, ctypes.byref(size)
            ):
                return ""
            return os.path.basename(path.value).lower()
        finally:
            kernel32.CloseHandle(process)
    except (OSError, AttributeError):
        return ""


def get_clipboard_owner_process_name():
    """Return the lowercase exe name that owns the current clipboard."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetClipboardOwner.restype = wintypes.HWND
        return _get_window_process_name(user32.GetClipboardOwner())
    except (OSError, AttributeError):
        return ""


def get_foreground_process_name():
    """Return the lowercase exe name for the window receiving user input."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        return _get_window_process_name(user32.GetForegroundWindow())
    except (OSError, AttributeError):
        return ""


# Clipboard owners whose image activity should not trigger auto-processing.
# Excel copy macros (e.g. CopyAnh) push image shapes onto the clipboard, which
# the monitor would otherwise mistake for new Re-ID screenshots and auto-open
# Review.
CLIPBOARD_IGNORE_OWNER_PROCESSES = {"excel.exe"}
# ShareX may capture while Excel remains focused.  Its clipboard ownership is
# definitive, so it must win over the foreground Excel safeguard below.
CLIPBOARD_CAPTURE_OWNER_PROCESSES = {"sharex.exe"}
# LastRegion publishes the clipboard payload later than RectangleRegion on
# some ShareX installations.  Keep the changed clipboard sequence pending for
# this whole window instead of throwing away the first capture after 1 second.
CLIPBOARD_IMAGE_READY_TIMEOUT_SECONDS = 5.0
# Give the desktop compositor a full frame after hiding the Tk window before
# ImageGrab samples the screen.  Without this, the frozen selector image can
# retain a dim, stale copy of the main Re-ID window at the screen edge.
DIRECT_CAPTURE_HIDE_DELAY_MS = 180
def _get_capture_save_dirs():
    """Return a list of unique screenshot directories to save into.

    Always includes ~/Pictures/Screenshots (works on every Windows machine).
    If OneDrive redirects Pictures to a different path, that directory is
    added too — so the image lands in both places.
    """
    home_pics = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
    dirs = [home_pics]
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Pictures {33E28130-4E1E-4676-835A-98395C3BC3BB}
        guid = _GUID()
        guid.Data1 = 0x33E28130
        guid.Data2 = 0x4E1E
        guid.Data3 = 0x4676
        guid.Data4 = (ctypes.c_ubyte * 8)(
            0x83, 0x5A, 0x98, 0x39, 0x5C, 0x3B, 0xC3, 0xBB
        )
        path_ptr = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)
        )
        if hr == 0 and path_ptr.value:
            shell_pics = os.path.join(path_ptr.value, "Screenshots")
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            # Only add if it's genuinely a different directory.
            try:
                if not os.path.exists(home_pics) or not os.path.exists(shell_pics):
                    if shell_pics.lower() != home_pics.lower():
                        dirs.append(shell_pics)
                elif not os.path.samefile(home_pics, shell_pics):
                    dirs.append(shell_pics)
            except (OSError, ValueError):
                if shell_pics.lower() != home_pics.lower():
                    dirs.append(shell_pics)
    except Exception:
        pass
    return dirs

DIRECT_CAPTURE_SAVE_DIRS = _get_capture_save_dirs()

# Windows 11-inspired surface and accent colours.  Keeping the palette in one
# place avoids the mixed gray/emoji appearance that the earlier compact UI had.
UI_COLORS = {
    "window": "#101218",
    "surface": "#191D25",
    "surface_hover": "#222834",
    "border": "#2C3442",
    "text": "#F3F6FC",
    "muted": "#9AA6B7",
    "subtle": "#68768A",
    "primary": "#3B82F6",
    "primary_hover": "#2563EB",
    "secondary": "#2A3342",
    "secondary_hover": "#374357",
    "danger": "#E05263",
    "danger_hover": "#B93A4A",
}

# Segoe MDL2 Assets is bundled with Windows 10/11.  Render the glyphs into
# CTkImage objects so button labels can stay in the normal, highly legible
# Segoe UI font while icons retain the native Windows visual language.
FLUENT_ICON_FONT_PATH = r"C:\Windows\Fonts\segmdl2.ttf"
FLUENT_ICONS = {
    "camera": "\uE722",
    "refresh": "\uE72C",
    "folder": "\uE8B7",
    "clear": "\uE894",
    "delete": "\uE74D",
    "save": "\uE74E",
    "target": "\uE71B",
    "repeat": "\uE8EE",
    "add": "\uE710",
    "activity": "\uE9D9",
}


def create_fluent_icon(icon_name, color, size=18):
    """Return a small Fluent icon for a CTk widget, or ``None`` gracefully."""
    glyph = FLUENT_ICONS.get(icon_name)
    if not glyph or not os.path.isfile(FLUENT_ICON_FONT_PATH):
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont

        pixel_size = max(16, int(size * 2))
        image = Image.new("RGBA", (pixel_size, pixel_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(FLUENT_ICON_FONT_PATH, pixel_size - 2)
        bbox = draw.textbbox((0, 0), glyph, font=font)
        x = (pixel_size - (bbox[2] - bbox[0])) // 2 - bbox[0]
        y = (pixel_size - (bbox[3] - bbox[1])) // 2 - bbox[1]
        draw.text((x, y), glyph, font=font, fill=color)
        return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))
    except (OSError, ValueError, ImportError):
        return None


def should_ignore_clipboard_image(owner_process=None, foreground_process=None):
    """Keep Excel pastes out while allowing an actual ShareX capture.

    A screenshot copied by ShareX can arrive while Excel is foreground, so a
    foreground-only rule loses real captures.  Conversely, a normal image
    paste into Excel should not start a Re-ID review.  Clipboard ownership
    distinguishes the two paths whenever ShareX is the producer.
    """
    owner_process = (
        get_clipboard_owner_process_name()
        if owner_process is None
        else owner_process
    )
    if owner_process in CLIPBOARD_CAPTURE_OWNER_PROCESSES:
        return False
    if owner_process in CLIPBOARD_IGNORE_OWNER_PROCESSES:
        return True

    foreground_process = (
        get_foreground_process_name()
        if foreground_process is None
        else foreground_process
    )
    return foreground_process == "excel.exe"


def normalize_capture_bounds(start_x, start_y, end_x, end_y, minimum_size=5):
    """Return an ordered crop box, or ``None`` for a click/tiny selection."""
    left, right = sorted((int(start_x), int(end_x)))
    top, bottom = sorted((int(start_y), int(end_y)))
    if right - left < minimum_size or bottom - top < minimum_size:
        return None
    return left, top, right, bottom


class RedirectStdout:
    """Redirect stdout to a Tkinter Text widget safely."""
    def __init__(self, text_widget, queue_obj):
        self.text_widget = text_widget
        self.queue_obj = queue_obj

    def write(self, string):
        self.queue_obj.put(string)

    def flush(self):
        pass

class GlobalHotkeyManager:
    def __init__(self, app):
        self.app = app
        self.thread = None
        self.running = False
        self._mouse_hook = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        try:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self.thread.ident, 0, 0, 0)
        except (OSError, AttributeError):
            pass

    def _loop(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        
        # Hotkey IDs
        HOTKEY_PREV = 100
        HOTKEY_NEXT = 101
        HOTKEY_NEW_CAPTURE_QUERY = 103
        HOTKEY_CAPTURE_REGION = 104
        HOTKEY_CAPTURE_LAST_REGION = 105
        HOTKEY_CAPTURE_REGION_FROM_BLAZE = 106
        HOTKEY_NUM_BASE = 200 # 200 to 209 for 0 to 9
        
        # Modifiers: Ctrl (0x0002) + Shift (0x0004) = 0x0006.
        # Only one variant runs at a time, so this matches the original
        # ReID Auto Draw's Ctrl+Shift shortcuts instead of stacking Alt on top.
        MODS = 0x0006

        failed_hotkeys = []

        def register(hotkey_id, modifiers, key, label):
            if not user32.RegisterHotKey(None, hotkey_id, modifiers, key):
                failed_hotkeys.append(label)

        # Ctrl+Shift+A (Previous), +D (Next), +Q (All/Root)
        register(HOTKEY_PREV, MODS, 0x41, "Ctrl+Shift+A")
        register(HOTKEY_NEXT, MODS, 0x44, "Ctrl+Shift+D")
        register(HOTKEY_NUM_BASE + 0, MODS, 0x51, "Ctrl+Shift+Q")

        # MOD_NOREPEAT (0x4000) prevents one long press from skipping through
        # several empty Query slots.
        register(
            HOTKEY_NEW_CAPTURE_QUERY,
            MODS | 0x4000,
            0x4E,
            "Ctrl+Shift+N",
        )

        # The in-app capture path never writes to, or waits on, the clipboard.
        # Alt+PrintScreen selects a new region; Alt+S reuses the prior region.
        capture_modifiers = 0x0001 | 0x4000  # Alt + no repeat
        register(HOTKEY_CAPTURE_REGION, capture_modifiers, 0x2C, "Alt+PrintScreen")
        register(HOTKEY_CAPTURE_LAST_REGION, capture_modifiers, 0x53, "Alt+S")
        # Fallback hotkey for BLAZE integration — the primary trigger is now the
        # low-level mouse hook (right-click in BlazeClient → Region Capture).
        # This hotkey remains registered so AUTO.ahk still works if running.
        register(
            HOTKEY_CAPTURE_REGION_FROM_BLAZE,
            0x0002 | 0x0001 | 0x0004 | 0x4000,
            0x79,
            "Ctrl+Alt+Shift+F10 (BLAZE chuột phải)",
        )

        # The original app installs a low-level hook that swallows plain Space
        # while Blaze is focused. This variant does not install it; use
        # Ctrl+Shift+N instead.
        keyboard_hook = None

        # Register Ctrl+Shift+1 to 9 (top-left number keys)
        for i in range(1, 10):
            register(HOTKEY_NUM_BASE + i, MODS, 0x30 + i, f"Ctrl+Shift+{i}")

        # RegisterHotKey fails silently when another process already owns the
        # combination, which would otherwise look like a dead keyboard.
        if failed_hotkeys:
            print(
                "  [WARN] Windows refused these shortcuts (already in use): "
                + ", ".join(failed_hotkeys)
            )

        # ── Low-level mouse hook ──────────────────────────────────────────────
        # Two features driven by a single WH_MOUSE_LL hook:
        #   • Right-click  in BlazeClient → Region Capture
        #   • Middle-click in BlazeClient / Excel → toggle between the two apps
        #
        # WH_MOUSE_LL callbacks MUST return in < ~200 ms or Windows kills the
        # hook and the entire mouse input chain freezes.  All heavy Win32 work
        # (OpenProcess / QueryFullProcessImageNameW) runs in a 250 ms SetTimer
        # callback that caches:
        #   self._fg_is_blaze   – bool, foreground is BlazeClient?
        #   self._fg_is_excel   – bool, foreground is Excel?
        #   self._blaze_hwnd    – last-seen BlazeClient HWND (for switching)
        #   self._excel_hwnd    – last-seen Excel HWND (for switching)
        # The hook callback only reads these cached values — zero Win32 calls,
        # returns in microseconds.
        BLAZE_EXE = "blazeclient.exe"
        EXCEL_EXE = "excel.exe"
        WH_MOUSE_LL = 14
        WM_RBUTTONDOWN = 0x0204
        WM_RBUTTONUP = 0x0205
        WM_MBUTTONDOWN = 0x0207
        WM_MBUTTONUP = 0x0208
        self._rclick_swallowed = False
        self._mclick_swallowed = False
        self._fg_is_blaze = False
        self._fg_is_excel = False
        self._blaze_hwnd = None
        self._excel_hwnd = None

        # --- Foreground-window polling (runs on the SAME thread) ---------------
        TIMER_ID_FG_POLL = 9901
        TIMER_INTERVAL_MS = 250

        # Declare argtypes once for EnumWindows
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL

        def _hwnd_to_exe(hwnd):
            """Return lowercase exe name for a window handle, or ''."""
            try:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                h = kernel32.OpenProcess(0x1000, False, pid.value)
                if not h:
                    return ""
                try:
                    buf = ctypes.create_unicode_buffer(512)
                    sz = wintypes.DWORD(len(buf))
                    if kernel32.QueryFullProcessImageNameW(
                        h, 0, buf, ctypes.byref(sz)
                    ):
                        return os.path.basename(buf.value).lower()
                    return ""
                finally:
                    kernel32.CloseHandle(h)
            except Exception:
                return ""

        def _poll_foreground(_hwnd_ignored, _msg, _id, _time):
            """TimerProc callback — update cached flags every 250 ms."""
            try:
                fg_hwnd = user32.GetForegroundWindow()
                if not fg_hwnd:
                    self._fg_is_blaze = False
                    self._fg_is_excel = False
                    return
                exe = _hwnd_to_exe(fg_hwnd)
                self._fg_is_blaze = (exe == BLAZE_EXE)
                self._fg_is_excel = (exe == EXCEL_EXE)
                # Remember the most recent visible HWND for each app so the
                # middle-click toggle can SetForegroundWindow to it.
                if self._fg_is_blaze:
                    self._blaze_hwnd = fg_hwnd
                elif self._fg_is_excel:
                    self._excel_hwnd = fg_hwnd

                # If we haven't seen one of the two apps yet, scan all
                # top-level windows once to seed the cache.
                if self._blaze_hwnd is None or self._excel_hwnd is None:
                    def _enum_cb(hwnd, _lparam):
                        if not user32.IsWindowVisible(hwnd):
                            return True
                        name = _hwnd_to_exe(hwnd)
                        if name == BLAZE_EXE and self._blaze_hwnd is None:
                            self._blaze_hwnd = hwnd
                        elif name == EXCEL_EXE and self._excel_hwnd is None:
                            self._excel_hwnd = hwnd
                        # Stop early if both found.
                        if self._blaze_hwnd and self._excel_hwnd:
                            return False
                        return True
                    user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
            except Exception:
                self._fg_is_blaze = False
                self._fg_is_excel = False

        TIMERPROC = ctypes.CFUNCTYPE(
            None, wintypes.HWND, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), wintypes.DWORD
        )
        self._fg_timer_cb = TIMERPROC(_poll_foreground)
        user32.SetTimer(None, TIMER_ID_FG_POLL, TIMER_INTERVAL_MS, self._fg_timer_cb)

        # --- Taskbar/tray detection for mouse hook ----------------------------
        # Cache the taskbar class names so the hook callback can reject clicks
        # whose cursor sits over the taskbar or system-tray overflow, exactly
        # like the AHK MouseIsOverTaskbarOrTray() guard.  The check uses only
        # WindowFromPoint + GetAncestor + GetClassNameW — all lightweight, no
        # process queries.
        _TASKBAR_CLASSES = {
            "Shell_TrayWnd", "SecondaryTrayWnd", "NotifyIconOverflowWindow",
        }
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        _GetAncestor = user32.GetAncestor
        _GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        _GetAncestor.restype = wintypes.HWND
        _GetClassNameW = user32.GetClassNameW
        _GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        _GetClassNameW.restype = ctypes.c_int

        def _mouse_is_over_taskbar(pt_x, pt_y):
            """Return True when the cursor is over the taskbar or tray area."""
            try:
                pt = wintypes.POINT(pt_x, pt_y)
                child = user32.WindowFromPoint(pt)
                if not child:
                    return False
                root = _GetAncestor(child, 2)  # GA_ROOT
                if not root:
                    root = child
                buf = ctypes.create_unicode_buffer(64)
                _GetClassNameW(root, buf, 64)
                return buf.value in _TASKBAR_CLASSES
            except Exception:
                return False

        # --- Mouse hook (ultra-light: reads cached bools/HWNDs) ---------------

        # Windows blocks SetForegroundWindow unless the calling thread owns the
        # foreground lock.  The reliable bypass: simulate an Alt press via
        # keybd_event (this tricks Windows into thinking the user initiated the
        # switch), then call SetForegroundWindow, then release Alt.  Combined
        # with ShowWindow(SW_RESTORE) for minimised windows and
        # BringWindowToTop, this works even across processes.
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.keybd_event.argtypes = [
            ctypes.c_byte, ctypes.c_byte, wintypes.DWORD, ctypes.POINTER(ctypes.c_ulong)
        ]
        user32.keybd_event.restype = None
        SW_RESTORE = 9
        VK_MENU = 0x12          # Alt key
        KEYEVENTF_KEYUP = 0x0002

        def _switch_to(target_hwnd):
            """Force *target_hwnd* to the foreground — works across processes."""
            if not target_hwnd or not user32.IsWindow(target_hwnd):
                return
            # Restore if minimised, otherwise the switch often does nothing.
            if user32.IsIconic(target_hwnd):
                user32.ShowWindow(target_hwnd, SW_RESTORE)
            # Simulate Alt press → unlocks SetForegroundWindow restriction.
            user32.keybd_event(VK_MENU, 0, 0, None)
            user32.SetForegroundWindow(target_hwnd)
            user32.BringWindowToTop(target_hwnd)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)

        # Declare argtypes for CallNextHookEx — LPARAM is pointer-sized
        # and will overflow the default c_int on 64-bit Python without this.
        user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK,        # hhk  (can be None)
            ctypes.c_int,          # nCode
            wintypes.WPARAM,       # wParam
            wintypes.LPARAM,       # lParam — 64-bit on x64!
        ]
        user32.CallNextHookEx.restype = ctypes.c_long

        MOUSEHOOKPROC = ctypes.CFUNCTYPE(
            ctypes.c_long,
            ctypes.c_int,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        )

        def _mouse_hook_proc(nCode, wParam, lParam):
            if nCode < 0:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            # ── Right-click in Blaze → Region Capture ──
            # Skip when the cursor is over the taskbar/tray so the user can
            # still right-click the system tray while Blaze is foreground.
            if wParam in (WM_RBUTTONDOWN, WM_RBUTTONUP):
                if self._fg_is_blaze:
                    # Read cursor position from MSLLHOOKSTRUCT.pt (first 8 bytes)
                    pt_x = ctypes.c_long.from_address(lParam).value
                    pt_y = ctypes.c_long.from_address(lParam + ctypes.sizeof(ctypes.c_long)).value
                    if _mouse_is_over_taskbar(pt_x, pt_y):
                        # Let Windows handle this click normally on the taskbar
                        self._rclick_swallowed = False
                        return user32.CallNextHookEx(None, nCode, wParam, lParam)
                    if wParam == WM_RBUTTONDOWN:
                        self._rclick_swallowed = True
                        self.app.root.after(0, self.app.start_region_capture)
                    elif wParam == WM_RBUTTONUP and self._rclick_swallowed:
                        self._rclick_swallowed = False
                    return 1  # swallow

            # ── Middle-click in Blaze/Excel → toggle between them ──
            if wParam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                if self._fg_is_blaze or self._fg_is_excel:
                    if wParam == WM_MBUTTONDOWN:
                        self._mclick_swallowed = True
                        target = (
                            self._excel_hwnd if self._fg_is_blaze
                            else self._blaze_hwnd
                        )
                        # Defer heavy SetForegroundWindow out of hook proc
                        # to avoid blocking the input chain (>200ms = freeze).
                        threading.Thread(
                            target=_switch_to, args=(target,), daemon=True
                        ).start()
                    elif wParam == WM_MBUTTONUP and self._mclick_swallowed:
                        self._mclick_swallowed = False
                    return 1  # swallow

            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._mouse_hook_cb = MOUSEHOOKPROC(_mouse_hook_proc)
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            MOUSEHOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        self._mouse_hook = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._mouse_hook_cb, None, 0
        )
        if not self._mouse_hook:
            print("  [WARN] Không thể cài đặt mouse hook")

        try:
            msg = wintypes.MSG()
            while self.running:
                if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                    if msg.message == 0x0312:  # WM_HOTKEY
                        hotkey_id = msg.wParam
                        self._handle_hotkey(hotkey_id)
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.KillTimer(None, TIMER_ID_FG_POLL)
            if self._mouse_hook:
                user32.UnhookWindowsHookEx(self._mouse_hook)
                self._mouse_hook = None
            if keyboard_hook:
                user32.UnhookWindowsHookEx(keyboard_hook)
            # Unregister all hotkeys
            user32.UnregisterHotKey(None, HOTKEY_PREV)
            user32.UnregisterHotKey(None, HOTKEY_NEXT)
            user32.UnregisterHotKey(None, HOTKEY_NEW_CAPTURE_QUERY)
            user32.UnregisterHotKey(None, HOTKEY_CAPTURE_REGION)
            user32.UnregisterHotKey(None, HOTKEY_CAPTURE_LAST_REGION)
            user32.UnregisterHotKey(None, HOTKEY_CAPTURE_REGION_FROM_BLAZE)
            for i in range(10):
                user32.UnregisterHotKey(None, HOTKEY_NUM_BASE + i)

    def _handle_hotkey(self, hotkey_id):
        self.app.root.after(0, lambda: self._process_hotkey_on_main_thread(hotkey_id))

    def _process_hotkey_on_main_thread(self, hotkey_id):
        if hotkey_id in (104, 106):
            self.app.start_region_capture()
            return
        if hotkey_id == 105:
            self.app.capture_last_region()
            return
        if not os.path.exists(QUERIES_DIR):
            return
        folders = [d for d in os.listdir(QUERIES_DIR) if os.path.isdir(os.path.join(QUERIES_DIR, d))]
        
        import re
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
        folders.sort(key=natural_sort_key)
        
        all_options = ["Tất cả (Root queries folder)"] + folders
        
        current_name = os.path.basename(self.app.current_queries_dir)
        if self.app.current_queries_dir == QUERIES_DIR:
            current_index = 0
        elif current_name in folders:
            current_index = folders.index(current_name) + 1
        else:
            current_index = 0
            
        if hotkey_id == 100: # PREV
            new_index = (current_index - 1) % len(all_options)
            self._select_index(all_options, new_index)
        elif hotkey_id == 101: # NEXT
            new_index = (current_index + 1) % len(all_options)
            self._select_index(all_options, new_index)
        elif hotkey_id == 103: # Space in Blaze or global Shift+Space
            self.app.select_next_empty_capture_query()
        elif 200 <= hotkey_id <= 209: # Ctrl+Shift+Q or 1 to 9
            digit = hotkey_id - 200
            if digit == 0:
                self._select_index(all_options, 0)
            elif digit < len(all_options):
                self._select_index(all_options, digit)
                
    def _select_index(self, all_options, index):
        selection = all_options[index]
        self.app.cmb_queries.set(selection)
        self.app.on_query_selected(selection)
        
        display_name = selection
        if selection == "Tất cả (Root queries folder)":
            display_name = "Thư mục gốc (Tất cả)"
            
        # Display the custom fast-refreshing OSD overlay
        self.app.show_osd(f"📁 Folder: {display_name}")

class AutoMarkerApp:
    def __init__(self, root, mutex=None):
        self.root = root
        self.mutex = mutex
        self.root.title(APP_NAME)
        self.assets_dir = os.path.join(RESOURCE_DIR, "assets")
        self.app_icon_png = os.path.join(self.assets_dir, "app_icon.png")
        self.app_icon_ico = os.path.join(self.assets_dir, "app_icon.ico")
        self._window_icon = None
        try:
            # `default=True` propagates the icon to Preview/OSD Toplevel windows.
            self._window_icon = tk.PhotoImage(file=self.app_icon_png)
            self.root.iconphoto(True, self._window_icon)
            if os.name == "nt":
                self.root.iconbitmap(self.app_icon_ico)
        except (tk.TclError, OSError, FileNotFoundError) as e:
            print(f"Không thể nạp icon ứng dụng: {e}")
        # Center the window on screen
        # Keep enough vertical room for the log header and the capture
        # destination card at the bottom.  A shorter window clips the
        # "Chọn Query trống" button when the app is restored from the tray.
        w = 900
        h = 700
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws - w) // 2
        y = (hs - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.resizable(True, True)
        self.root.minsize(760, 640)
        self._ui_icon_images = []
        
        # Monitoring and Log state
        self.is_monitoring = False
        self.log_queue = queue.Queue()
        self.last_clipboard_hash = None
        self.last_clipboard_sequence = None
        # ShareX can publish the clipboard sequence before the image payload
        # is readable. Keep that event pending and retry briefly instead of
        # consuming it permanently on the first failed read.
        self.pending_clipboard_sequence = None
        self.pending_clipboard_hash = None
        self.pending_clipboard_retries = 0
        self.clipboard_poll_ms = max(50, int(round(POLL_INTERVAL * 1000)))
        self.clipboard_image_retry_limit = max(
            10,
            int(round(CLIPBOARD_IMAGE_READY_TIMEOUT_SECONDS * 1000 / self.clipboard_poll_ms)),
        )
        self.is_processing = False
        self.active_preview_window = None
        self.left_click_timer = None
        self.last_tray_click_time = 0.0
        self.last_capture_region = None
        self.region_capture_overlay = None
        self.region_capture_pending = False
        self._restore_main_after_capture_cancel = False
        self._region_capture_image = None
        self._region_capture_start = None
        self._region_capture_rect = None
        self.osd_window = None
        self.osd_timer = None
        self.query_collector = None
        self.auto_query_capture_enabled = True  # luôn bật, không có nút tắt
        self.auto_query_capture = tk.BooleanVar(value=True)
        self.capture_query_target = "Query_1"
        # Direct screen captures are useful as an audit trail, but can be
        # disabled per session without affecting matching or Query collection.
        self.save_direct_captures = tk.BooleanVar(value=True)
        
        self.current_queries_dir = QUERIES_DIR
        
        self.setup_ui()
        # Derive the minimum from the actual widget request after CTk has
        # applied the current DPI/font scaling.  A hard-coded minimum can be
        # too short on another display and let the bottom controls get clipped.
        self.root.update_idletasks()
        self.root.minsize(
            max(760, self.root.winfo_reqwidth()),
            max(640, self.root.winfo_reqheight() + 8),
        )
        self.process_logs()
        
        # Tray Icon setup - dời lịch khởi chạy sau 1 giây
        self.tray_icon = None
        self.root.after(1000, self.setup_tray)
        
        # Tự động BẬT ngay khi mở app
        self.root.after(500, self.start_marker)
        
        # Initialize Global Hotkey Manager
        self.hotkey_manager = GlobalHotkeyManager(self)
        self.hotkey_manager.start()
        
        # Khởi động ẩn dưới khay hệ thống (Tray Icon)
        self.root.withdraw()

    def _ui_icon(self, icon_name, color, size=18):
        """Create and retain a Fluent icon while this window is alive."""
        icon = create_fluent_icon(icon_name, color, size)
        if icon is not None:
            self._ui_icon_images.append(icon)
        return icon

    def setup_ui(self):
        """Build a compact, Windows 11-style control surface."""
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.root.configure(fg_color=UI_COLORS["window"])

        # Header: product identity at left, a quiet monitoring status chip at right.
        header = ctk.CTkFrame(
            self.root,
            fg_color=UI_COLORS["surface"],
            border_width=1,
            border_color=UI_COLORS["border"],
            corner_radius=16,
        )
        header.pack(fill=tk.X, padx=20, pady=(20, 10))

        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.pack(side=tk.LEFT, padx=16, pady=13)
        app_mark = ctk.CTkFrame(
            title_group,
            width=40,
            height=40,
            corner_radius=12,
            fg_color="#17345F",
        )
        app_mark.pack(side=tk.LEFT, padx=(0, 11))
        app_mark.pack_propagate(False)
        ctk.CTkLabel(
            app_mark,
            text="",
            image=self._ui_icon("camera", "#B9DCFF", 21),
        ).pack(expand=True)

        title_copy = ctk.CTkFrame(title_group, fg_color="transparent")
        title_copy.pack(side=tk.LEFT)
        ctk.CTkLabel(
            title_copy,
            text="AutoMarker Re-ID",
            font=("Segoe UI", 17, "bold"),
            text_color=UI_COLORS["text"],
        ).pack(anchor=tk.W)
        ctk.CTkLabel(
            title_copy,
            text="Nhận ảnh Clipboard và đánh dấu kết quả tự động",
            font=("Segoe UI", 10),
            text_color=UI_COLORS["muted"],
        ).pack(anchor=tk.W, pady=(1, 0))

        status_chip = ctk.CTkFrame(
            header,
            fg_color="#14251F",
            corner_radius=12,
        )
        status_chip.pack(side=tk.RIGHT, padx=16, pady=15)
        self.lbl_status = ctk.CTkLabel(
            status_chip,
            text="●",
            text_color="#4ADE80",
            fg_color="transparent",
            font=("Segoe UI", 15, "bold"),
            width=16,
            height=28,
        )
        self.lbl_status.pack(side=tk.LEFT, padx=(10, 3))
        ctk.CTkLabel(
            status_chip,
            text="Đang theo dõi",
            font=("Segoe UI", 10, "bold"),
            text_color="#AAF0C8",
        ).pack(side=tk.LEFT, padx=(0, 11))

        # ── Toolbar: compact two-row card ──────────────────────────────
        toolbar_card = ctk.CTkFrame(
            self.root,
            fg_color=UI_COLORS["surface"],
            border_width=1,
            border_color=UI_COLORS["border"],
            corner_radius=12,
        )
        toolbar_card.pack(fill=tk.X, padx=20, pady=(0, 10))

        # — Row 1: management actions + capture buttons —
        row1 = ctk.CTkFrame(toolbar_card, fg_color="transparent")
        row1.pack(fill=tk.X, padx=10, pady=(8, 0))

        _btn_h = 28
        _btn_r = 8
        _btn_font = ("Segoe UI", 9, "bold")
        _btn_font_light = ("Segoe UI", 9)
        _icon_sz = 14

        ctk.CTkButton(
            row1,
            text="Làm mới OCR",
            image=self._ui_icon("refresh", "#EAF4FF", _icon_sz),
            compound="left",
            width=105,
            height=_btn_h,
            corner_radius=_btn_r,
            font=_btn_font,
            command=self.refresh_and_rebuild_cache,
            fg_color="#1677D2",
            hover_color="#1265B4",
        ).pack(side=tk.LEFT, padx=(0, 4))

        ctk.CTkButton(
            row1,
            text="Thư viện",
            image=self._ui_icon("folder", "#DCE8F8", _icon_sz),
            compound="left",
            width=80,
            height=_btn_h,
            corner_radius=_btn_r,
            command=self.open_library_window,
            fg_color=UI_COLORS["secondary"],
            hover_color=UI_COLORS["secondary_hover"],
            font=_btn_font,
        ).pack(side=tk.LEFT, padx=(0, 4))

        ctk.CTkButton(
            row1,
            text="Xóa log",
            image=self._ui_icon("clear", "#B8C4D4", _icon_sz),
            compound="left",
            width=76,
            height=_btn_h,
            corner_radius=_btn_r,
            command=self.clear_logs,
            fg_color="transparent",
            border_width=1,
            border_color=UI_COLORS["border"],
            hover_color=UI_COLORS["surface_hover"],
            font=_btn_font_light,
        ).pack(side=tk.LEFT, padx=(0, 4))

        ctk.CTkButton(
            row1,
            text="Xóa Query",
            image=self._ui_icon("delete", "#FFC2C9", _icon_sz),
            compound="left",
            width=88,
            height=_btn_h,
            corner_radius=_btn_r,
            command=self.clear_data,
            fg_color="transparent",
            border_width=1,
            border_color="#7A3A47",
            text_color="#FFC2C9",
            hover_color="#3A2029",
            font=_btn_font,
        ).pack(side=tk.LEFT, padx=(0, 10))

        # Separator
        ctk.CTkFrame(
            row1, fg_color=UI_COLORS["border"], width=1, height=18,
        ).pack(side=tk.LEFT, padx=(0, 10))

        ctk.CTkButton(
            row1,
            text="Chụp vùng",
            image=self._ui_icon("target", "#F4F8FF", 15),
            compound="left",
            width=100,
            height=_btn_h,
            corner_radius=_btn_r,
            command=self.start_region_capture,
            fg_color=UI_COLORS["primary"],
            hover_color=UI_COLORS["primary_hover"],
            font=_btn_font,
        ).pack(side=tk.LEFT, padx=(0, 4))

        ctk.CTkButton(
            row1,
            text="Chụp lại",
            image=self._ui_icon("repeat", "#D7E6F8", _icon_sz),
            compound="left",
            width=86,
            height=_btn_h,
            corner_radius=_btn_r,
            command=self.capture_last_region,
            fg_color="#2B405C",
            hover_color="#385474",
            font=_btn_font,
        ).pack(side=tk.LEFT, padx=(0, 6))

        ctk.CTkCheckBox(
            row1,
            text="Lưu ảnh",
            variable=self.save_direct_captures,
            onvalue=True,
            offvalue=False,
            checkbox_width=17,
            checkbox_height=17,
            font=("Segoe UI", 9),
            text_color="#D6E2F1",
            fg_color=UI_COLORS["primary"],
            hover_color=UI_COLORS["primary_hover"],
            border_color="#70839C",
        ).pack(side=tk.LEFT, padx=(0, 6))

        # Separator before capture tools
        ctk.CTkFrame(
            row1, fg_color=UI_COLORS["border"], width=1, height=18,
        ).pack(side=tk.LEFT, padx=(0, 8))

        ctk.CTkButton(
            row1,
            text="📂 Ảnh chụp",
            width=94,
            height=_btn_h,
            corner_radius=_btn_r,
            command=self.open_capture_library,
            fg_color="#2A2F37",
            hover_color="#353B45",
            font=_btn_font,
        ).pack(side=tk.LEFT, padx=(0, 4))

        ctk.CTkButton(
            row1,
            text="✂ Sửa ảnh",
            width=90,
            height=_btn_h,
            corner_radius=_btn_r,
            command=self.open_image_editor_standalone,
            fg_color="#2B405C",
            hover_color="#385474",
            font=_btn_font,
        ).pack(side=tk.LEFT, padx=(0, 4))

        # — Row 2: destination selector —
        row2 = ctk.CTkFrame(toolbar_card, fg_color="transparent")
        row2.pack(fill=tk.X, padx=10, pady=(4, 8))

        ctk.CTkLabel(
            row2,
            text="LƯU VÀO",
            font=("Segoe UI", 9, "bold"),
            text_color=UI_COLORS["subtle"],
        ).pack(side=tk.LEFT, padx=(0, 6))

        self.cmb_capture_query = ctk.CTkOptionMenu(
            row2,
            width=140,
            height=_btn_h,
            dynamic_resizing=False,
            command=self.on_capture_query_selected,
            corner_radius=_btn_r,
            fg_color=UI_COLORS["secondary"],
            button_color="#3C4A60",
            button_hover_color="#4A5A72",
            text_color=UI_COLORS["text"],
            font=_btn_font_light,
        )
        self.cmb_capture_query.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_next_capture_query = ctk.CTkButton(
            row2,
            text="Query trống",
            image=self._ui_icon("add", "#EFE7FF", _icon_sz),
            compound="left",
            height=_btn_h,
            corner_radius=_btn_r,
            width=100,
            command=self.select_next_empty_capture_query,
            fg_color="#7057C9",
            hover_color="#5E46B1",
            font=_btn_font,
        )
        self.btn_next_capture_query.pack(side=tk.LEFT)

        # Body: sidebar (left) + content (right)
        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 20))

        # -------- SIDEBAR (left) --------
        sidebar = ctk.CTkFrame(
            body,
            width=224,
            fg_color=UI_COLORS["surface"],
            corner_radius=14,
            border_width=1,
            border_color=UI_COLORS["border"],
        )
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        lbl_side_folder = ctk.CTkLabel(
            sidebar,
            text="THƯ MỤC MẪU",
            image=self._ui_icon("folder", "#8FC5FF", 15),
            compound="left",
            font=("Segoe UI", 10, "bold"),
            text_color=UI_COLORS["text"],
        )
        lbl_side_folder.pack(anchor=tk.W, padx=14, pady=(14, 6))

        self.lbl_folder_summary = ctk.CTkLabel(
            sidebar,
            text="Đang tải danh sách...",
            font=("Segoe UI", 9),
            text_color=UI_COLORS["muted"],
        )
        self.lbl_folder_summary.pack(anchor=tk.W, padx=14, pady=(0, 9))

        # The folder tree below is the primary picker. The OptionMenu is kept
        # unpacked only because the hotkey/tray code paths still call `.set()`
        # on a widget tagged `cmb_queries` to stay in sync.
        self.cmb_queries = ctk.CTkOptionMenu(
            sidebar,
            width=196,
            height=34,
            dynamic_resizing=False,
            command=self.on_query_selected,
        )

        # Plain tk.Canvas + ttk.Scrollbar instead of CTkScrollableFrame. CTk
        # widgets redraw their canvas on every <Configure> (window resize), so a
        # tree holding many CTkButton items makes dragging the window laggy.
        # Native tk items have no canvas redraw cost on resize.
        # Use standard dark colors that match CTk's default dark theme
        tree_bg = UI_COLORS["surface"]
        tree_wrap = tk.Frame(sidebar, bg=tree_bg, bd=0, highlightthickness=0)
        tree_wrap.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.folder_tree = tk.Canvas(
            tree_wrap, bg=tree_bg, bd=0, highlightthickness=0
        )
        tree_sb = ctk.CTkScrollbar(
            tree_wrap, orientation="vertical", command=self.folder_tree.yview
        )
        self.folder_tree.configure(yscrollcommand=tree_sb.set)
        tree_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.folder_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._tree_inner = tk.Frame(self.folder_tree, bg=tree_bg)
        self._tree_window = self.folder_tree.create_window(
            (0, 0), window=self._tree_inner, anchor="nw"
        )
        self._tree_inner.bind(
            "<Configure>",
            lambda e: self.folder_tree.configure(
                scrollregion=self.folder_tree.bbox("all")
            ),
        )
        self.folder_tree.bind(
            "<Configure>",
            lambda e: self.folder_tree.itemconfigure(self._tree_window, width=e.width),
        )

        # -------- CONTENT (right) --------
        content = ctk.CTkFrame(
            body,
            fg_color=UI_COLORS["surface"],
            corner_radius=14,
            border_width=1,
            border_color=UI_COLORS["border"],
        )
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(14, 0))

        log_header = ctk.CTkFrame(content, fg_color="transparent")
        log_header.pack(fill=tk.X, padx=14, pady=(13, 7))
        ctk.CTkLabel(
            log_header,
            text="NHẬT KÝ HOẠT ĐỘNG",
            image=self._ui_icon("activity", "#8FC5FF", 15),
            compound="left",
            font=("Segoe UI", 10, "bold"),
            text_color=UI_COLORS["text"],
        ).pack(side=tk.LEFT)
        ctk.CTkLabel(
            log_header,
            text="Tự cuộn theo sự kiện mới",
            font=("Segoe UI", 9),
            text_color=UI_COLORS["subtle"],
        ).pack(side=tk.RIGHT)

        log_frame = tk.Frame(
            content,
            bg="#10141B",
            highlightthickness=1,
            highlightbackground=UI_COLORS["border"],
        )
        log_frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 14))
        
        scrollbar = ctk.CTkScrollbar(log_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.txt_logs = tk.Text(
            log_frame,
            bg="#10141B",
            fg=UI_COLORS["text"],
            insertbackground=UI_COLORS["text"],
            font=("Consolas", 10),
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            wrap="word",
            yscrollcommand=scrollbar.set,
            state="disabled"
        )
        self.txt_logs.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.configure(command=self.txt_logs.yview)

        self.update_queries_dropdown()
        self.update_capture_query_dropdown()

        # Redirect stdout and stderr
        sys.stdout = RedirectStdout(self.txt_logs, self.log_queue)
        sys.stderr = sys.stdout
        print("Sẵn sàng!")

        # Debounce resize: CTk redraws every canvas on each <Configure> event.
        # Coalescing rapid resize events into one deferred call cuts CPU usage
        # during window dragging and eliminates the stutter.
        self._resize_job = None

        def _on_root_configure(event):
            if event.widget is not self.root:
                return
            if self._resize_job is not None:
                self.root.after_cancel(self._resize_job)
            self._resize_job = self.root.after(80, self.root.update_idletasks)

        self.root.bind("<Configure>", _on_root_configure, add="+")

    def clear_logs(self):
        """Clear the visible activity log and any messages waiting to render."""
        while not self.log_queue.empty():
            try:
                self.log_queue.get_nowait()
            except queue.Empty:
                break
        self.txt_logs.configure(state="normal")
        self.txt_logs.delete("1.0", tk.END)
        self.txt_logs.configure(state="disabled")

    def _set_status_dot(self, color):
        """Show only the color status dot; keep state details in the log."""
        self.lbl_status.configure(
            text="●",
            text_color=color,
            fg_color="transparent",
        )

    def refresh_and_rebuild_cache(self):
        """Refresh Query folders, then offer to rebuild cache/OCR."""
        self.update_queries_dropdown()
        print("[UI] Đã làm mới danh sách Query. Chuẩn bị kiểm tra cache/OCR...")
        self.rebuild_cache_and_reocr()

    def update_queries_dropdown(self):
        """Update the dropdown with subfolders in queries directory."""
        # Drop cache left behind by images that were deleted or moved out of a
        # Query (e.g. a screenshot saved into the wrong folder). This keeps the
        # OCR/feature cache aligned with the images actually present.
        pruned = self._prune_orphaned_cache(QUERIES_DIR)
        if pruned:
            print(f"[CACHE] Đã dọn {pruned} tệp cache mồ côi (ảnh nguồn đã bị xóa).")

        if not os.path.exists(QUERIES_DIR):
            os.makedirs(QUERIES_DIR, exist_ok=True)

        folders = [d for d in os.listdir(QUERIES_DIR) if os.path.isdir(os.path.join(QUERIES_DIR, d))]

        import re
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
        folders.sort(key=natural_sort_key)

        all_options = ["Tất cả (Root queries folder)"] + folders
        self.cmb_queries.configure(values=all_options)

        # Determine current selection index
        current_name = os.path.basename(self.current_queries_dir)
        if current_name in all_options:
            self.cmb_queries.set(current_name)
        else:
            self.cmb_queries.set(all_options[0])

        # Rebuild the clickable folder tree in the sidebar.
        self.rebuild_folder_tree(folders)

        if hasattr(self, "cmb_capture_query"):
            self.update_capture_query_dropdown()

    def rebuild_folder_tree(self, folders):
        """Rebuild the sidebar folder list. Clicking an item selects that folder.

        Plain tk.Label items: native widgets have no per-resize canvas redraw,
        which keeps window resizing smooth even with many folders.
        """
        tree = getattr(self, "folder_tree", None)
        if tree is None:
            return

        valid_exts = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')
        image_total = 0
        try:
            for root_dir, dirs, files in os.walk(QUERIES_DIR):
                dirs[:] = [name for name in dirs if name != ".cache"]
                image_total += sum(name.lower().endswith(valid_exts) for name in files)
        except OSError:
            image_total = 0
        if hasattr(self, "lbl_folder_summary"):
            self.lbl_folder_summary.configure(
                text=f"{len(folders)} thư mục  •  {image_total} ảnh mẫu"
            )

        for widget in self._tree_inner.winfo_children():
            widget.destroy()

        active = os.path.basename(self.current_queries_dir)
        root_selected = self.current_queries_dir == QUERIES_DIR
        hover_bg = UI_COLORS["surface_hover"]
        normal_bg = UI_COLORS["surface"]
        selected_bg = "#245FAD"
        normal_fg = "#DCE5F1"
        selected_fg = "#FFFFFF"

        def make_item(text, selected, command):
            label = tk.Label(
                self._tree_inner,
                text=text,
                anchor="w",
                font=("Segoe UI", 10),
                bg=selected_bg if selected else normal_bg,
                fg=selected_fg if selected else normal_fg,
                padx=8,
                pady=2,
                cursor="hand2",
            )
            label.pack(fill=tk.X)
            label.bind("<Button-1>", lambda e: command())
            label.bind(
                "<Enter>",
                lambda e, bg=label.cget("bg"): label.configure(
                    bg=hover_bg, fg=selected_fg if selected else "#E6EAF0"
                ),
            )
            label.bind(
                "<Leave>",
                lambda e: label.configure(
                    bg=selected_bg if selected else normal_bg,
                    fg=selected_fg if selected else normal_fg,
                ),
            )
            return label

        make_item(
            "● Tất cả (Root)",
            root_selected,
            lambda: self.select_folder_from_sidebar(QUERIES_DIR),
        )
        for folder in folders:
            selected = not root_selected and folder == active
            make_item(
                folder,
                selected,
                lambda f=folder: self.select_folder_from_sidebar(
                    os.path.join(QUERIES_DIR, f)
                ),
            )

    def select_folder_from_sidebar(self, path):
        """Sidebar tree click → set OptionMenu text + apply selection."""
        self.current_queries_dir = path
        self.on_query_selected(os.path.basename(path) if path != QUERIES_DIR else "Tất cả (Root queries folder)")
        self.update_queries_dropdown()

    def update_capture_query_dropdown(self):
        """Refresh the independent destination used for clipboard Query captures."""
        # Scan actual Query_N folders to determine the range dynamically
        max_existing = 0
        if os.path.isdir(QUERIES_DIR):
            for name in os.listdir(QUERIES_DIR):
                match = re.fullmatch(r"Query_(\d+)", name, flags=re.IGNORECASE)
                if match:
                    max_existing = max(max_existing, int(match.group(1)))
        # Always show at least up to max_existing + 1 so the user can create the next one
        upper = max(max_existing + 1, 14)
        options = [f"Query_{number}" for number in range(1, upper + 1)]
        self.cmb_capture_query.configure(values=options)
        selection = self.capture_query_target or options[0]
        self.cmb_capture_query.set(selection)

    def on_capture_query_selected(self, selection):
        self.capture_query_target = selection
        os.makedirs(os.path.join(QUERIES_DIR, selection), exist_ok=True)
        print(f"[AUTO QUERY] Đã khóa ảnh chụp vào {selection}.")
        self.show_osd(f"📥 Ảnh chụp → {selection}")
        self.update_queries_dropdown()

    def select_next_empty_capture_query(self):
        """Select the first empty/missing Query slot, scanning existing folders dynamically."""
        valid_exts = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')
        # Determine the range by scanning existing Query_N folders
        max_existing = 0
        if os.path.isdir(QUERIES_DIR):
            for name in os.listdir(QUERIES_DIR):
                match = re.fullmatch(r"Query_(\d+)", name, flags=re.IGNORECASE)
                if match:
                    max_existing = max(max_existing, int(match.group(1)))
        upper = max(max_existing + 1, 14)
        for number in range(1, upper + 1):
            query_name = f"Query_{number}"
            query_dir = os.path.join(QUERIES_DIR, query_name)
            has_images = os.path.isdir(query_dir) and any(
                filename.lower().endswith(valid_exts)
                and os.path.isfile(os.path.join(query_dir, filename))
                for filename in os.listdir(query_dir)
            )
            if not has_images:
                self.cmb_capture_query.set(query_name)
                self.on_capture_query_selected(query_name)
                return

        print(f"[AUTO QUERY] Query_1 đến Query_{max_existing} đều đã có ảnh.")
        self.show_osd(f"⚠️ Không còn Query trống (1–{max_existing})")

    def on_auto_query_toggle(self):
        self.auto_query_capture_enabled = bool(self.auto_query_capture.get())
        state = "BẬT" if self.auto_query_capture_enabled else "TẮT"
        print(f"[AUTO QUERY] Tự thu thập ảnh người từ Clipboard: {state}")
            
    def on_query_selected(self, selection):
        if selection == "Tất cả (Root queries folder)":
            self.current_queries_dir = QUERIES_DIR
        else:
            self.current_queries_dir = os.path.join(QUERIES_DIR, selection)
            
        print(f"Đã chuyển thư mục: {self.current_queries_dir}")
        
        # The matcher always loads the root Query set once. Switching the active
        # folder only swaps a small in-memory view, so the Tk event loop is not
        # blocked by model construction and feature extraction on every click.
        if getattr(self, 'is_monitoring', False):
            self._apply_matcher_query_selection()

    def _apply_matcher_query_selection(self):
        """Select the active Query data without rebuilding the AI models."""
        matcher = getattr(self, "matcher", None)
        if matcher is None:
            return

        all_references = getattr(self, "_matcher_reference_cache", None)
        all_query_images = getattr(self, "_matcher_query_image_cache", None)
        if all_references is None or all_query_images is None:
            all_references = matcher.reference_images
            all_query_images = matcher.query_images
            self._matcher_reference_cache = all_references
            self._matcher_query_image_cache = all_query_images

        if self.current_queries_dir == QUERIES_DIR:
            matcher.reference_images = all_references
            matcher.query_images = all_query_images
            matcher.target_query = None
            active_name = "tất cả Query"
        else:
            query_name = os.path.basename(self.current_queries_dir)
            matcher.reference_images = {
                query_name: all_references.get(query_name, [])
            }
            matcher.query_images = (
                {query_name: all_query_images[query_name]}
                if query_name in all_query_images
                else {}
            )
            matcher.target_query = query_name
            active_name = query_name

        print(f"Đã cập nhật AI tức thì: {active_name}.")

    def _prune_orphaned_cache(self, base_dir=None):
        """Remove .cache entries whose source image no longer exists.

        Each Query folder keeps a .cache subfolder with '<image>.npz'
        (ReID feature) and '<image>.ocr.txt' (OCR timestamp) files, keyed by
        the exact image filename. When an image is captured into the wrong
        Query and later deleted, its cache is orphaned and could still be read
        back. Deleting orphaned cache files keeps the cache in sync with the
        images actually present. Returns the number of files removed.
        """
        base_dir = base_dir or QUERIES_DIR
        valid_exts = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')
        removed = 0
        if not os.path.isdir(base_dir):
            return 0
        for root_dir, _dirs, files in os.walk(base_dir):
            if os.path.basename(root_dir) != ".cache":
                continue
            parent = os.path.dirname(root_dir)
            try:
                existing_images = {
                    name for name in os.listdir(parent)
                    if name.lower().endswith(valid_exts)
                    and os.path.isfile(os.path.join(parent, name))
                }
            except OSError:
                continue
            for cache_file in files:
                if cache_file.endswith(".ocr.txt"):
                    source_name = cache_file[:-len(".ocr.txt")]
                elif cache_file.endswith(".npz"):
                    source_name = cache_file[:-len(".npz")]
                else:
                    continue
                if source_name not in existing_images:
                    try:
                        os.remove(os.path.join(root_dir, cache_file))
                        removed += 1
                    except OSError:
                        pass
        return removed

    def rebuild_cache_and_reocr(self):
        """Wipe every .cache subtree, then re-extract features and re-run OCR.

        Use this when caches might be stale or mismatched — e.g. an image was
        saved into the wrong Query and then moved/deleted. Query images are NOT
        touched; only the derived cache is rebuilt from scratch. The heavy
        recompute runs on a background thread so the UI stays responsive.
        """
        if not os.path.isdir(QUERIES_DIR):
            messagebox.showinfo("Thông báo", "Chưa có thư mục queries nào.")
            return

        if not messagebox.askyesno(
            "Xóa cache & OCR lại",
            "Sẽ xóa toàn bộ cache OCR/feature trong thư mục queries và tính "
            "lại từ đầu cho MỌI ảnh.\n\n"
            "• Ảnh Query của bạn KHÔNG bị xóa.\n"
            "• Lần xử lý này có thể chậm hơn bình thường.\n\n"
            "Tiếp tục?",
        ):
            return

        deleted_cache = 0
        for root_dir, _dirs, files in os.walk(QUERIES_DIR, topdown=False):
            if os.path.basename(root_dir) == ".cache":
                deleted_cache += len(files)
                try:
                    shutil.rmtree(root_dir)
                except OSError:
                    pass
        print(f"[CACHE] Đã xóa {deleted_cache} tệp cache. Đang OCR lại toàn bộ ảnh...")

        matcher = getattr(self, "matcher", None)
        if matcher is None:
            # No AI matcher in memory yet: caches will be rebuilt automatically
            # the next time monitoring starts.
            self.update_queries_dropdown()
            messagebox.showinfo(
                "Hoàn tất",
                f"Đã xóa {deleted_cache} tệp cache cũ.\n"
                "Toàn bộ ảnh sẽ được OCR lại khi bạn bấm ▶ BẬT.",
            )
            return

        self._set_status_dot(
            "#22C55E" if getattr(self, "is_monitoring", False) else "#EF4444"
        )

        def _reocr_thread():
            try:
                matcher.reference_images.clear()
                matcher.query_images.clear()
                matcher.reference_timestamps.clear()
                matcher.query_thresholds.clear()
                if ENABLE_OCR_TIMESTAMP_FILTER:
                    warm_up_card_ocr()
                matcher._load_references(QUERIES_DIR)

                def _done():
                    self._matcher_reference_cache = matcher.reference_images
                    self._matcher_query_image_cache = matcher.query_images
                    self._apply_matcher_query_selection()
                    running = getattr(self, "is_monitoring", False)
                    self._set_status_dot("#22C55E" if running else "#EF4444")
                    self.update_queries_dropdown()
                    messagebox.showinfo(
                        "Hoàn tất",
                        f"Đã xóa {deleted_cache} tệp cache cũ và OCR lại "
                        "toàn bộ ảnh Query.",
                    )

                self.root.after(0, _done)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.root.after(
                    0,
                    lambda: (
                        self._set_status_dot("#EF4444"),
                        messagebox.showerror("Lỗi", f"Lỗi khi OCR lại: {e}"),
                    ),
                )

        threading.Thread(target=_reocr_thread, daemon=True).start()

    def clear_data(self):
        query_has_data = os.path.isdir(self.current_queries_dir) and any(
            True for _ in os.scandir(self.current_queries_dir)
        )
        output_has_data = os.path.isdir(OUTPUT_DIR) and any(
            True for _ in os.scandir(OUTPUT_DIR)
        )
        if not query_has_data and not output_has_data:
            messagebox.showinfo("Thông báo", "Dữ liệu Query và ảnh đã vẽ đều đang trống.")
            return

        confirmation = (
            "Bạn có chắc chắn muốn xóa:\n\n"
            f"• Dữ liệu Query trong: {self.current_queries_dir}\n"
            f"• Toàn bộ ảnh đã vẽ trong: {OUTPUT_DIR}\n\n"
            "Thao tác này không thể hoàn tác."
        )
        if messagebox.askyesno("Xác nhận xóa toàn bộ dữ liệu", confirmation):
            try:
                valid_exts = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')
                deleted_queries = 0
                deleted_cache = 0
                if os.path.isdir(self.current_queries_dir):
                    for root_dir, _dirs, files in os.walk(self.current_queries_dir):
                        for file in files:
                            if file.lower().endswith(valid_exts):
                                os.remove(os.path.join(root_dir, file))
                                deleted_queries += 1

                    # Query folders recreate .cache/*.npz and *.ocr.txt for every
                    # reference image. Deleting the images orphans those caches,
                    # so remove every .cache subtree left behind.
                    for root_dir, _dirs, files in os.walk(
                        self.current_queries_dir, topdown=False
                    ):
                        if os.path.basename(root_dir) == ".cache":
                            deleted_cache += len(files)
                            shutil.rmtree(root_dir)

                deleted_outputs = 0
                os.makedirs(OUTPUT_DIR, exist_ok=True)
                for entry in os.scandir(OUTPUT_DIR):
                    if entry.is_dir(follow_symlinks=False):
                        for _root, _dirs, files in os.walk(entry.path):
                            deleted_outputs += len(files)
                        shutil.rmtree(entry.path)
                    else:
                        os.remove(entry.path)
                        deleted_outputs += 1

                messagebox.showinfo(
                    "Thành công",
                    f"Đã xóa {deleted_queries} ảnh Query, "
                    f"{deleted_cache} tệp cache OCR/feature và "
                    f"{deleted_outputs} tệp kết quả đã vẽ.",
                )
                self.update_queries_dropdown()
            except (OSError, PermissionError, IOError) as e:
                messagebox.showerror("Lỗi", f"Lỗi khi xóa: {str(e)}")

    def open_capture_library(self):
        """Open a library window browsing the region-capture screenshots."""
        # Use the first configured capture directory.
        capture_dir = DIRECT_CAPTURE_SAVE_DIRS[0] if DIRECT_CAPTURE_SAVE_DIRS else None
        if not capture_dir:
            return
        from library_win import LibraryWindow
        existing = getattr(self, "capture_library_window", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.lift()
                existing.focus_force()
                return
        except (tk.TclError, AttributeError):
            pass

        def on_close():
            self.capture_library_window = None
            # Keep the capture library consistent with the saved-results
            # library: the main configuration window stays hidden until the
            # library is closed.
            try:
                self.root.deiconify()
                self.root.after(10, self.root.focus_force)
            except (tk.TclError, AttributeError):
                pass

        self.root.withdraw()
        try:
            self.capture_library_window = LibraryWindow(
                self.root,
                capture_dir,
                on_close=on_close,
                matcher=self.matcher,
                enable_editor=True,
            )
        except Exception:
            # Do not leave the application hidden if the library could not be
            # created (for example, when the capture directory is unavailable).
            self.capture_library_window = None
            self.root.deiconify()
            raise

    def open_image_editor_standalone(self):
        """Open a file dialog and edit the selected image."""
        from tkinter import filedialog
        from image_editor import ImageEditorWindow
        import cv2
        path = filedialog.askopenfilename(
            title="Chọn ảnh để chỉnh sửa",
            filetypes=[
                ("Ảnh", "*.png *.jpg *.jpeg *.bmp *.webp *.tiff"),
                ("Tất cả", "*.*"),
            ],
        )
        if not path:
            return
        bgr = cv2.imread(path)
        if bgr is None:
            from tkinter import messagebox
            messagebox.showerror("Lỗi", f"Không thể đọc ảnh:\n{path}")
            return

        def on_saved(new_bgr):
            from PIL import Image
            from auto_marker import copy_image_to_clipboard
            import numpy as np
            rgb = cv2.cvtColor(new_bgr, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            # Ask whether to overwrite or save as new
            from tkinter import messagebox
            save_path = filedialog.asksaveasfilename(
                title="Lưu ảnh",
                initialfile=os.path.basename(path),
                defaultextension=".png",
                filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"), ("Tất cả", "*.*")],
            )
            if save_path:
                cv2.imwrite(save_path, new_bgr)
            copy_image_to_clipboard(pil_img)
            self.matcher.ignore_next_clipboard = True
            self.show_osd("✔ Đã lưu & copy ảnh")

        ImageEditorWindow(self.root, bgr, on_save_callback=on_saved, title="Chỉnh sửa ảnh")

    def open_library_window(self):

        existing = getattr(self, "active_preview_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.focus_force()
                    return
            except (tk.TclError, AttributeError):
                self.active_preview_window = None

        def on_close():
            self.active_preview_window = None
            self.root.deiconify()

        self.root.withdraw()
        self.active_preview_window = LibraryWindow(
            self.root, OUTPUT_DIR, on_close=on_close, matcher=self.matcher
        )

    def start_marker(self):
        # A second call while the first background initialization is running
        # would compile every OpenVINO model and create another poll loop.
        if self.is_monitoring:
            return

        # Check if queries dir exists and has files (either subfolders or direct images)
        valid_exts = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')
        has_data = False
        if os.path.exists(self.current_queries_dir):
            for item in os.listdir(self.current_queries_dir):
                item_path = os.path.join(self.current_queries_dir, item)
                if os.path.isdir(item_path):
                    has_data = True
                    break
                elif item.lower().endswith(valid_exts):
                    has_data = True
                    break

        if not has_data:
            print(
                "[AUTO QUERY] Chưa có dữ liệu mẫu. App vẫn theo dõi Clipboard; "
                "ảnh chụp người đầu tiên sẽ tự tạo Query_1."
            )

        self.is_monitoring = True
        self._set_status_dot("#EF4444")
        
        def _init_ai_thread():
            try:
                # Reuse existing matcher to avoid recompiling OpenVINO models
                existing_matcher = getattr(self, "matcher", None)
                if existing_matcher is None:
                    matcher = TemplateMatcher(queries_dir=QUERIES_DIR, threshold=MATCH_THRESHOLD)
                else:
                    matcher = existing_matcher
                    matcher.reference_images.clear()
                    matcher.query_images.clear()
                    matcher.reference_timestamps.clear()
                    matcher.query_thresholds.clear()
                    matcher._load_references(QUERIES_DIR)

                if ENABLE_OCR_TIMESTAMP_FILTER:
                    ocr_started = time.perf_counter()
                    if warm_up_card_ocr():
                        print(
                            "  [OCR] Sẵn sàng đọc thời gian trên thẻ sau "
                            f"{time.perf_counter() - ocr_started:.2f}s"
                        )
                
                def _on_init_complete():
                    if not getattr(self, 'is_monitoring', False):
                        return  # Application is shutting down during init.
                    
                    self.matcher = matcher
                    self._matcher_reference_cache = self.matcher.reference_images
                    self._matcher_query_image_cache = self.matcher.query_images
                    self._apply_matcher_query_selection()
                    
                    if getattr(self, "query_collector", None) is None:
                        self.query_collector = QueryAutoCollector(
                            QUERIES_DIR, self.matcher.ai_extractor
                        )
                    self.last_clipboard_sequence = get_clipboard_sequence_number()
                    # Retain the image hash only as a fallback for platforms
                    # where a clipboard sequence number is unavailable.
                    self.last_clipboard_hash = get_clipboard_image_hash()
                    
                    self._set_status_dot("#22C55E")
                    self.poll_clipboard()
                    
                self.root.after(0, _on_init_complete)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"\n❌ Lỗi khởi tạo AI: {str(e)}")
                self.root.after(0, self._handle_monitoring_startup_error)

        import threading
        threading.Thread(target=_init_ai_thread, daemon=True).start()

    def _handle_monitoring_startup_error(self):
        """Show failed startup without exposing a manual pause control."""
        self.is_monitoring = False
        self._set_status_dot("#EF4444")

    def _sync_clipboard_snapshot(self):
        """Consume the current clipboard state without triggering processing."""
        sequence = get_clipboard_sequence_number()
        if sequence is not None:
            self.last_clipboard_sequence = sequence
            return

        current_hash = get_clipboard_image_hash()
        if current_hash is not None:
            self.last_clipboard_hash = current_hash

    def _on_preview_window_closed(self):
        """Clear review state and remember the latest clipboard snapshot."""
        self.active_preview_window = None
        self._sync_clipboard_snapshot()

    def _capture_is_available(self):
        """Reject a new direct capture while the app cannot process it safely."""
        if (
            getattr(self, "region_capture_pending", False)
            or getattr(self, "region_capture_overlay", None) is not None
        ):
            self.show_osd("⚠️ Đang chọn vùng chụp")
            return False
        if getattr(self, "is_processing", False):
            self.show_osd("⏳ Ảnh trước đang được xử lý")
            return False
        preview = getattr(self, "active_preview_window", None)
        if preview is not None:
            try:
                if preview.winfo_exists():
                    self.show_osd("⚠️ Hãy đóng cửa sổ Duyệt ảnh trước")
                    return False
            except (tk.TclError, AttributeError):
                pass
            self.active_preview_window = None
        if not getattr(self, "matcher", None):
            self.show_osd("⏳ Re-ID đang khởi tạo")
            return False
        return True

    def _grab_virtual_screen(self):
        """Take one frozen desktop image for selection or last-region capture."""
        from PIL import ImageGrab

        try:
            return ImageGrab.grab(all_screens=True)
        except TypeError:
            # Older Pillow builds do not support ``all_screens``.
            return ImageGrab.grab()

    def _virtual_screen_origin(self):
        """Return the virtual desktop origin used by ImageGrab on Windows."""
        try:
            import ctypes

            user32 = ctypes.windll.user32
            return user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        except (OSError, AttributeError):
            return 0, 0

    def _hide_main_window_for_capture(self):
        """Hide the main window before sampling pixels from the desktop."""
        try:
            self._restore_main_after_capture_cancel = self.root.state() != "withdrawn"
        except (tk.TclError, AttributeError):
            self._restore_main_after_capture_cancel = False
            return
        if self._restore_main_after_capture_cancel:
            self.root.withdraw()
            # ``withdraw`` changes Tk state immediately, but the last frame can
            # still be present in DWM when ImageGrab runs.  Flush it before the
            # scheduled capture, then allow one additional desktop frame.
            self.root.update()
            try:
                import ctypes

                ctypes.windll.dwmapi.DwmFlush()
            except (OSError, AttributeError):
                pass

    def _restore_main_window_after_capture_cancel(self):
        if not getattr(self, "_restore_main_after_capture_cancel", False):
            return
        self._restore_main_after_capture_cancel = False
        try:
            self.root.deiconify()
            self.root.after(10, self.root.focus_force)
        except (tk.TclError, AttributeError):
            pass

    def start_region_capture(self):
        """Show a lightweight, ShareX-independent region selector."""
        if not self._capture_is_available():
            return
        # A button press originates from the visible main window.  Give Windows
        # one event-loop turn to withdraw it so it cannot appear in the frozen
        # desktop image used by the selector.
        self._hide_main_window_for_capture()
        self.region_capture_pending = True
        self.root.after(DIRECT_CAPTURE_HIDE_DELAY_MS, self._open_region_capture_selector)

    def _open_region_capture_selector(self):
        self.region_capture_pending = False
        try:
            screenshot = self._grab_virtual_screen()
        except (OSError, ValueError) as exc:
            print(f"  [CAPTURE] Không thể chụp màn hình: {exc}")
            self._restore_main_window_after_capture_cancel()
            self.show_osd("⚠️ Không thể chụp màn hình")
            return

        origin_x, origin_y = self._virtual_screen_origin()
        overlay = tk.Toplevel(self.root)
        self.region_capture_overlay = overlay
        self._region_capture_image = screenshot
        self._region_capture_start = None
        self._region_capture_rect = None

        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.geometry(
            f"{screenshot.width}x{screenshot.height}{origin_x:+d}{origin_y:+d}"
        )
        overlay.configure(bg="#101318")

        from PIL import ImageTk

        canvas = tk.Canvas(
            overlay,
            width=screenshot.width,
            height=screenshot.height,
            highlightthickness=0,
            cursor="crosshair",
        )
        canvas.pack(fill=tk.BOTH, expand=True)
        # Keep the PhotoImage on the app so Tk does not garbage-collect it.
        self._region_capture_photo = ImageTk.PhotoImage(screenshot)
        canvas.create_image(0, 0, anchor=tk.NW, image=self._region_capture_photo)
        canvas.bind("<ButtonPress-1>", self._on_region_capture_press)
        canvas.bind("<B1-Motion>", self._on_region_capture_drag)
        canvas.bind("<ButtonRelease-1>", self._on_region_capture_release)
        canvas.bind("<Escape>", self.cancel_region_capture)
        overlay.bind("<Escape>", self.cancel_region_capture)
        overlay.protocol("WM_DELETE_WINDOW", self.cancel_region_capture)
        overlay.focus_force()
        canvas.focus_set()
        try:
            overlay.grab_set()
        except tk.TclError:
            pass
        # The overlay is borderless and another app (Blaze) may still own the
        # foreground, so Tk keyboard bindings alone cannot catch Escape.  Poll
        # the physical key state with GetAsyncKeyState every 50 ms as a
        # reliable fallback — works regardless of which window has focus.
        self._esc_poll_timer = self.root.after(50, self._poll_escape_key)
        # Safety net for an interrupted drag, focus loss, or Tk exception.
        self._capture_watchdog_timer = self.root.after(
            30000, self._capture_watchdog
        )

    def _capture_watchdog(self):
        """Release a forgotten region overlay/grab after a bounded timeout."""
        if getattr(self, "region_capture_overlay", None) is not None:
            print("  [CAPTURE] Watchdog giải phóng overlay vùng chụp bị treo.")
            self.cancel_region_capture()

    def _poll_escape_key(self):
        """Cancel region capture when Escape is pressed, even without Tk focus."""
        if getattr(self, "region_capture_overlay", None) is None:
            return
        try:
            import ctypes
            # GetAsyncKeyState returns the physical key state regardless of
            # which window has focus.  Bit 0x8000 = currently pressed.
            VK_ESCAPE = 0x1B
            if ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
                self.cancel_region_capture()
                return
        except (OSError, AttributeError):
            pass
        self._esc_poll_timer = self.root.after(50, self._poll_escape_key)

    def _on_region_capture_press(self, event):
        self._region_capture_start = (event.x, event.y)
        canvas = event.widget
        if self._region_capture_rect is not None:
            canvas.delete(self._region_capture_rect)
        self._region_capture_rect = canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="#38BDF8",
            width=2,
            dash=(5, 3),
        )

    def _on_region_capture_drag(self, event):
        if self._region_capture_start is None or self._region_capture_rect is None:
            return
        start_x, start_y = self._region_capture_start
        event.widget.coords(
            self._region_capture_rect, start_x, start_y, event.x, event.y
        )

    def _on_region_capture_release(self, event):
        if self._region_capture_start is None:
            self.cancel_region_capture()
            return
        start_x, start_y = self._region_capture_start
        bounds = normalize_capture_bounds(start_x, start_y, event.x, event.y)
        screenshot = self._region_capture_image
        self._destroy_region_capture_overlay()
        if bounds is None or screenshot is None:
            self._restore_main_window_after_capture_cancel()
            self.show_osd("⚠️ Vùng chụp quá nhỏ")
            return
        self.last_capture_region = bounds
        # A successful capture returns the app to its normal tray workflow.
        self._restore_main_after_capture_cancel = False
        self._submit_direct_capture(screenshot.crop(bounds).copy(), "vùng đã chọn")

    def _destroy_region_capture_overlay(self):
        overlay = getattr(self, "region_capture_overlay", None)
        self.region_capture_overlay = None
        self.region_capture_pending = False
        self._region_capture_start = None
        self._region_capture_rect = None
        self._region_capture_image = None
        self._region_capture_photo = None
        watchdog_timer = getattr(self, "_capture_watchdog_timer", None)
        self._capture_watchdog_timer = None
        if watchdog_timer is not None:
            try:
                self.root.after_cancel(watchdog_timer)
            except (tk.TclError, ValueError):
                pass
        # Stop the Escape-key polling timer
        esc_timer = getattr(self, "_esc_poll_timer", None)
        if esc_timer is not None:
            try:
                self.root.after_cancel(esc_timer)
            except (tk.TclError, ValueError):
                pass
            self._esc_poll_timer = None
        if overlay is not None:
            try:
                overlay.grab_release()
            except tk.TclError:
                pass
            try:
                overlay.destroy()
            except tk.TclError:
                pass

    def cancel_region_capture(self, event=None):
        self._destroy_region_capture_overlay()
        self._restore_main_window_after_capture_cancel()
        self.show_osd("Đã hủy chụp vùng")

    def capture_last_region(self):
        """Capture the most recently selected region without the selector."""
        if not self._capture_is_available():
            return
        bounds = getattr(self, "last_capture_region", None)
        if bounds is None:
            self.show_osd("⚠️ Chưa có vùng trước đó")
            return
        self._hide_main_window_for_capture()
        self.root.after(
            DIRECT_CAPTURE_HIDE_DELAY_MS,
            self._capture_last_region_after_hiding_main,
        )

    def _capture_last_region_after_hiding_main(self):
        bounds = getattr(self, "last_capture_region", None)
        if bounds is None:
            self._restore_main_window_after_capture_cancel()
            return
        try:
            screenshot = self._grab_virtual_screen()
        except (OSError, ValueError) as exc:
            print(f"  [CAPTURE] Không thể chụp lại vùng trước: {exc}")
            self._restore_main_window_after_capture_cancel()
            self.show_osd("⚠️ Không thể chụp màn hình")
            return

        left, top, right, bottom = bounds
        if left < 0 or top < 0 or right > screenshot.width or bottom > screenshot.height:
            self._restore_main_window_after_capture_cancel()
            self.show_osd("⚠️ Vùng trước không còn hợp lệ")
            return
        self._restore_main_after_capture_cancel = False
        self._submit_direct_capture(screenshot.crop(bounds).copy(), "vùng trước")

    def _submit_direct_capture(self, pil_img, capture_label):
        """Run an in-app capture through the normal Re-ID/Review pipeline after optional editing."""
        if pil_img is None:
            return
        try:
            image = pil_img.convert("RGB").copy()
        except (AttributeError, ValueError) as exc:
            print(f"  [CAPTURE] Ảnh chụp không hợp lệ: {exc}")
            self.show_osd("⚠️ Ảnh chụp không hợp lệ")
            return

        if getattr(self, "matcher", None) is not None:
            self.matcher.ignore_next_clipboard = True

        # Copy captured image to clipboard in background (avoid blocking UI)
        try:
            from auto_marker import copy_image_to_clipboard
            def _bg_copy(img, app):
                try:
                    copy_image_to_clipboard(img)
                    app.root.after(0, lambda: setattr(app, 'last_clipboard_sequence', get_clipboard_sequence_number()))
                    app.root.after(0, lambda: setattr(app, 'pending_clipboard_sequence', None))
                    print(f"  [CAPTURE] Đã copy ảnh {capture_label} vào clipboard")
                except Exception as exc:
                    if getattr(app, "matcher", None) is not None:
                        app.matcher.ignore_next_clipboard = False
                    print(f"  [CAPTURE] Không thể copy vào clipboard: {exc}")
            threading.Thread(target=_bg_copy, args=(image.copy(), self), daemon=True).start()
        except Exception as exc:
            print(f"  [CAPTURE] Không thể copy vào clipboard: {exc}")

        # Open the image editor immediately for the captured region
        import cv2
        import numpy as np
        from image_editor import ImageEditorWindow
        from PIL import Image

        bgr_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        h, w = bgr_image.shape[:2]

        is_reid_ui = self.check_is_reid_interface(bgr_image)
        is_wide_row = (w > h) and not is_reid_ui

        # Keep the processing path usable during startup/tests where a Tk root
        # is not available yet. In the real application root is always set,
        # so wide captures still open the editor normally.
        if is_wide_row and getattr(self, "root", None) is None:
            if self._should_save_direct_capture():
                self._save_direct_capture_image(image)
            self.is_processing = True
            threading.Thread(
                target=self.process_clipboard_image,
                args=(image, time.perf_counter()),
                daemon=True,
            ).start()
            return

        if is_wide_row:
            def on_editor_saved(new_bgr):
                edited_pil = Image.fromarray(cv2.cvtColor(new_bgr, cv2.COLOR_BGR2RGB))
                # Update clipboard with edited version in background
                try:
                    from auto_marker import copy_image_to_clipboard
                    def _bg_copy_edited(img, app):
                        try:
                            copy_image_to_clipboard(img)
                            app.root.after(0, lambda: setattr(app, 'last_clipboard_sequence', get_clipboard_sequence_number()))
                            app.root.after(0, lambda: setattr(app, 'pending_clipboard_sequence', None))
                        except Exception:
                            pass
                    threading.Thread(target=_bg_copy_edited, args=(edited_pil.copy(), self), daemon=True).start()
                except Exception:
                    pass
                    # Save the edited version to output directories if enabled
                if self._should_save_direct_capture():
                    saved_path = self._save_direct_capture_image(edited_pil)
                    if saved_path:
                        print(f"  [CAPTURE] Đã lưu ảnh gốc (sau chỉnh sửa): {saved_path}")

                detected_at = time.perf_counter()
                self.is_processing = True
                print(f"  [CAPTURE] Đã chỉnh sửa {capture_label}; đang nhận diện...")
                
                threading.Thread(
                    target=self.process_clipboard_image,
                    args=(edited_pil, detected_at),
                    daemon=True,
                ).start()

            # Disable topmost attributes on main root if it exists
            root = getattr(self, "root", None)
            if root is not None:
                root.attributes("-topmost", False)
            
            def on_editor_cancelled():
                # Restore main window if user cancels
                try:
                    self.root.deiconify()
                    self.root.after(10, self.root.focus_force)
                except (tk.TclError, AttributeError):
                    pass

            # Open editor window
            ImageEditorWindow(self.root, bgr_image, on_save_callback=on_editor_saved, on_cancel_callback=on_editor_cancelled, title="Chỉnh sửa ảnh chụp")
        else:
            if self._should_save_direct_capture():
                saved_path = self._save_direct_capture_image(pil_img)
            detected_at = time.perf_counter()
            self.is_processing = True
            threading.Thread(target=self.process_clipboard_image, args=(pil_img, detected_at), daemon=True).start()

    def _should_save_direct_capture(self):
        save_toggle = getattr(self, "save_direct_captures", None)
        try:
            return bool(save_toggle.get())
        except (AttributeError, tk.TclError):
            return False

    def _save_direct_capture_image(self, image):
        """Persist the unmodified direct capture to all configured directories."""
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"ReID_{stamp}.png"
        first_saved = None
        for save_dir in DIRECT_CAPTURE_SAVE_DIRS:
            try:
                os.makedirs(save_dir, exist_ok=True)
                path = os.path.join(save_dir, filename)
                image.save(path, "PNG")
                if first_saved is None:
                    first_saved = path
            except (OSError, ValueError) as exc:
                print(f"  [CAPTURE] Không thể lưu vào {save_dir}: {exc}")
        return first_saved

    def request_region_capture_from_tray(self, icon=None, item=None):
        self.root.after(0, self.start_region_capture)

    def request_last_region_capture_from_tray(self, icon=None, item=None):
        self.root.after(0, self.capture_last_region)

    def poll_clipboard(self):
        if not getattr(self, 'is_monitoring', False):
            return
            
        # Skip clipboard checking if we are already processing or if a review window is currently open
        if getattr(self, 'is_processing', False):
            self.root.after(self.clipboard_poll_ms, self.poll_clipboard)
            return
            
        if getattr(self, 'active_preview_window', None) is not None:
            try:
                if self.active_preview_window.winfo_exists():
                    self.root.after(self.clipboard_poll_ms, self.poll_clipboard)
                    return
                self.active_preview_window = None
            except (tk.TclError, AttributeError):
                self.active_preview_window = None
                
        try:
            sequence = get_clipboard_sequence_number()
            clipboard_changed = False
            current_hash = None
            if sequence is not None:
                if sequence != self.last_clipboard_sequence:
                    if self.pending_clipboard_sequence != sequence:
                        self.pending_clipboard_sequence = sequence
                        self.pending_clipboard_retries = 0
                    clipboard_changed = True
                else:
                    self.pending_clipboard_sequence = None
                    self.pending_clipboard_retries = 0
            else:
                # Compatibility fallback: hashing is more expensive, but is
                # only needed when Windows cannot provide a sequence number.
                current_hash = get_clipboard_image_hash()
                if current_hash is not None and current_hash != self.last_clipboard_hash:
                    if self.pending_clipboard_hash != current_hash:
                        self.pending_clipboard_hash = current_hash
                        self.pending_clipboard_retries = 0
                    clipboard_changed = True
                else:
                    self.pending_clipboard_hash = None
                    self.pending_clipboard_retries = 0

            if clipboard_changed:
                # Check if we should ignore this change (triggered by our own Save & Copy)
                if getattr(self.matcher, 'ignore_next_clipboard', False):
                    self.matcher.ignore_next_clipboard = False
                    if sequence is not None:
                        self.last_clipboard_sequence = sequence
                        self.pending_clipboard_sequence = None
                    elif current_hash is not None:
                        self.last_clipboard_hash = current_hash
                        self.pending_clipboard_hash = None
                    self.pending_clipboard_retries = 0
                    print("  [INFO] Ignoring clipboard change triggered by Preview Save.")
                else:
                    pil_img = get_clipboard_image()
                    if pil_img is not None:
                        # Commit the clipboard token only after the payload
                        # is readable. This prevents a transient ShareX
                        # clipboard lock from losing the first capture.
                        if sequence is not None:
                            self.last_clipboard_sequence = sequence
                            self.pending_clipboard_sequence = None
                        elif current_hash is not None:
                            self.last_clipboard_hash = current_hash
                            self.pending_clipboard_hash = None
                        self.pending_clipboard_retries = 0
                        if should_ignore_clipboard_image():
                            # An Excel copy/paste flow owns the image, or Excel
                            # is receiving a non-ShareX image paste.
                            # The sequence is recorded above, so it will not
                            # retrigger after the user leaves Excel.
                            print("  [INFO] Ignoring clipboard image during Excel copy/paste.")
                        else:
                            detected_at = time.perf_counter()
                            print("  [DETECT] New image found in clipboard.")
                            self.is_processing = True
                            # Process image in a background thread so GUI doesn't freeze during matching
                            threading.Thread(
                                target=self.process_clipboard_image,
                                args=(pil_img, detected_at),
                                daemon=True,
                            ).start()
                    else:
                        # The sequence changed, but ShareX may still be
                        # publishing CF_DIB/PNG. Retry on the next poll before
                        # deciding that this clipboard change is non-image.
                        self.pending_clipboard_retries = getattr(
                            self, "pending_clipboard_retries", 0
                        ) + 1
                        if self.pending_clipboard_retries == 1:
                            print(
                                "  [CLIPBOARD] Ảnh chưa sẵn sàng; đang thử đọc lại..."
                            )
                        if self.pending_clipboard_retries >= getattr(
                            self, "clipboard_image_retry_limit", 10
                        ):
                            if sequence is not None:
                                self.last_clipboard_sequence = sequence
                                self.pending_clipboard_sequence = None
                            elif current_hash is not None:
                                self.last_clipboard_hash = current_hash
                                self.pending_clipboard_hash = None
                            self.pending_clipboard_retries = 0
                            print(
                                "  [CLIPBOARD] Bỏ qua thay đổi clipboard sau khi "
                                "retry nhưng không đọc được ảnh."
                            )
        except (OSError, ValueError, TypeError) as e:
            print(f"  [CLIPBOARD POLL ERROR] {e}")
            
        self.root.after(self.clipboard_poll_ms, self.poll_clipboard)

    def process_clipboard_image(self, pil_img, detected_at=None):
        try:
            import cv2
            import numpy as np
            import time
            
            # Convert PIL to BGR for OpenCV
            current_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
            
            # A portrait crop is a Query sample; a Re-ID UI screenshot is a
            # search result that needs boxes. Both arrive through Clipboard.
            # NOTE: The editor is intentionally NOT opened here — clipboard images
            # come from external tools (ShareX, Snipping Tool, etc.) and should
            # be processed directly. The editor only opens for in-app captures
            # (Alt+PrintScreen / Alt+S) via _submit_direct_capture.
            if not self.check_is_reid_interface(current_bgr):
                h, w = current_bgr.shape[:2]
                if self.auto_query_capture_enabled and self.query_collector:
                    try:
                        # Always use the selected target query
                        target = self.capture_query_target or "Query_1"
                        result = self.query_collector.add_crop(
                            current_bgr, target_query=target
                        )
                        action = "tạo mới" if result["created"] else "thêm vào"
                        source = " qua khuôn mặt" if result.get("match_source") == "face" else ""
                        print(
                            f"  [AUTO QUERY] Đã {action} {result['query']}{source} "
                            f"(score={result['score']:.3f}): {result['path']}"
                        )
                        self._add_collected_reference_to_matcher(current_bgr, result)
                        self.root.after(0, self.update_queries_dropdown)
                        try:
                            import ctypes
                            ctypes.windll.user32.MessageBeep(0x00000040)
                        except (OSError, AttributeError):
                            pass
                    except ValueError as exc:
                        print(f"  [AUTO QUERY] {exc}")
                    finally:
                        self.is_processing = False
                    return
                print("  [INFO] Clipboard không phải giao diện Re-ID; đã bỏ qua.")
                self.is_processing = False
                return
                
            start_time = time.time()
            matches = self.matcher.find_matches(current_bgr, debug=False)
            elapsed = time.time() - start_time
            
            if not matches:
                print("  [RESULT] No matches found.")
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBeep(0x00000010)  # Hand / Error sound
                except (OSError, AttributeError):
                    pass
                self.root.after(
                    0,
                    lambda: self.open_preview_window(
                        current_bgr, [], detected_at, elapsed
                    ),
                )
            else:
                print(f"  [RESULT] Found {len(matches)} match(es) in {elapsed:.1f}s.")
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBeep(0x00000040)  # Asterisk / Info sound
                except (OSError, AttributeError):
                    pass
                self.root.after(
                    0,
                    lambda: self.open_preview_window(
                        current_bgr, matches, detected_at, elapsed
                    ),
                )
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  [PROCESSING ERROR] {e}")
            self.is_processing = False  # Reset state on exception

    def _add_collected_reference_to_matcher(self, image_bgr, result):
        """Keep the active matcher in sync without reloading all three models."""
        if not getattr(self, "matcher", None):
            return
        query_name = result["query"]
        filename = os.path.basename(result["path"])
        all_references = getattr(
            self, "_matcher_reference_cache", self.matcher.reference_images
        )
        all_references.setdefault(query_name, []).append(
            (filename, image_bgr.copy(), result["features"])
        )
        # Keep the OCR timestamp cache in sync too, so the running matcher sees
        # the new image without reloading all three models. Non-path files are
        # not refs: auto_marker treats missing/empty cache as "no timestamp".
        ref_timestamps = self.matcher.reference_timestamps.setdefault(
            query_name, []
        )
        ts = result.get("ocr_timestamp")
        if ts:
            ref_timestamps.append(ts)
        active_query = getattr(self.matcher, "target_query", None)
        if active_query is None or active_query == query_name:
            self.matcher._calibrate_query_thresholds()

    def check_is_reid_interface(self, img_bgr):
        try:
            import cv2
            import os
            template_path = os.path.join(RESOURCE_DIR, "ui_template.png")
            if not os.path.exists(template_path):
                print(f"  [WARN] UI template not found at {template_path}. Skipping validation.")
                return False
                
            template = cv2.imread(template_path)
            if template is None:
                print("  [WARN] Failed to load UI template. Skipping validation.")
                return False
                
            gray_img = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            gray_temp = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

            th, tw = gray_temp.shape
            h, w = gray_img.shape

            # A Re-ID interface screenshot is always a wide, landscape capture of
            # the whole search window (large and clearly wider than tall). A Query
            # sample is a single portrait person crop — taller than wide, and small.
            # Reject those outright so a copied crop is never mistaken for the UI.
            if w <= h:
                return False
            if w < 600:
                return False

            if h < th or w < tw:
                return False
                
            # Match at multiple scales to handle resolution changes
            scales = [0.8, 0.9, 1.0, 1.1, 1.2]
            best_val = 0.0
            for scale in scales:
                nw, nh = int(tw * scale), int(th * scale)
                if nh >= h or nw >= w or nh <= 5 or nw <= 5:
                    continue
                resized_temp = cv2.resize(gray_temp, (nw, nh))
                res = cv2.matchTemplate(gray_img, resized_temp, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(res)
                if max_val > best_val:
                    best_val = max_val
                    
            if VERBOSE_LOGGING:
                print(f"  [REID DETECT] Best match confidence for Re-ID UI: {best_val:.4f}")
            # A threshold of 0.70 is extremely safe and robust
            return best_val >= 0.70
        except (cv2.error, OSError, ValueError) as e:
            print(f"  [WARN] Error in check_is_reid_interface: {e}")
            return False

    def open_preview_window(
        self, current_bgr, matches, detected_at=None, matching_elapsed=None
    ):
        # Open preview window on Main Thread
        ui_started = time.perf_counter()
        try:
            self.active_preview_window = PreviewWindow(
                self.root,
                current_bgr,
                matches,
                self.matcher,
                OUTPUT_DIR,
                on_close_callback=self._on_preview_window_closed,
            )
        except Exception as exc:
            print(f"  [PREVIEW ERROR] {exc}")
            self.active_preview_window = None
        finally:
            self.is_processing = False
        if self.active_preview_window is None:
            self._sync_clipboard_snapshot()
            return
        self._sync_clipboard_snapshot()
        if detected_at is not None:
            total = time.perf_counter() - detected_at
            ui_elapsed = time.perf_counter() - ui_started
            match_info = (
                f", matching={matching_elapsed:.3f}s"
                if matching_elapsed is not None else ""
            )
            print(
                f"  [PERF] Clipboard detect -> Review ready: {total:.3f}s"
                f"{match_info}, review_ui={ui_elapsed:.3f}s"
            )

    def process_logs(self):
        """Consume logs from the queue and write to the text widget (thread-safe)."""
        if not self.log_queue.empty():
            self.txt_logs.configure(state="normal")
            while not self.log_queue.empty():
                msg = self.log_queue.get()
                self.txt_logs.insert(tk.END, msg)
            self.txt_logs.see(tk.END)
            self.txt_logs.configure(state="disabled")
        self.root.after(100, self.process_logs)

    def setup_tray(self):
        # Bind closing protocol to hide_window instead of on_closing
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        
        # Create tray icon
        try:
            self.tray_icon = pystray.Icon(
                "reid_auto_draw_osnet",
                self.create_tray_image(),
                APP_NAME,
                menu=self.create_tray_menu()
            )
            
            # Start tray icon in a separate thread
            self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
            self.tray_thread.start()
            
            # Show a startup notification letting the user know it is running in the tray
            self.root.after(500, lambda: self.tray_icon.notify(
                "Ứng dụng đã khởi động ngầm ở khay hệ thống.",
                title=APP_NAME
            ))
        except (OSError, ImportError, RuntimeError) as e:
            print(f"Không thể khởi tạo Tray Icon: {e}")

    def on_tray_left_click(self, icon):
        # Determine double click manually to bypass pystray's native default mapping
        import time
        now = time.time()
        last_click = getattr(self, 'last_tray_click_time', 0.0)
        self.last_tray_click_time = now
        
        if now - last_click < 0.35:  # Double click threshold is 350ms
            if getattr(self, 'left_click_timer', None) is not None:
                try:
                    self.root.after_cancel(self.left_click_timer)
                except (tk.TclError, ValueError):
                    pass
                self.left_click_timer = None
            self.show_window()
        else:
            if getattr(self, 'left_click_timer', None) is not None:
                try:
                    self.root.after_cancel(self.left_click_timer)
                except (tk.TclError, ValueError):
                    pass
            self.left_click_timer = self.root.after(300, self.execute_single_left_click)

    def execute_single_left_click(self):
        self.left_click_timer = None
        try:
            import ctypes
            # MOUSEEVENTF_RIGHTDOWN = 0x0008, MOUSEEVENTF_RIGHTUP = 0x0010
            ctypes.windll.user32.mouse_event(0x0008, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0)
        except (OSError, AttributeError) as e:
            print(f"Lỗi mô phỏng chuột: {e}")
            self.show_window()

    def create_tray_image(self):
        from PIL import Image
        with Image.open(self.app_icon_png) as source:
            icon = source.convert("RGBA")
            # Keep a high-resolution tray source; Windows selects/downscales it.
            icon.thumbnail((256, 256), Image.Resampling.LANCZOS)
            return icon.copy()

    def create_tray_menu(self):
        return pystray.Menu(
            pystray.MenuItem('DefaultAction', self.on_tray_left_click, default=True, visible=False),
            pystray.MenuItem('Hiện ứng dụng', self.show_window),
            pystray.MenuItem('Chụp vùng  (Alt+PrintScreen)', self.request_region_capture_from_tray),
            pystray.MenuItem('Chụp lại vùng trước  (Alt+S)', self.request_last_region_capture_from_tray),
            pystray.MenuItem('Chọn nhanh Folder', pystray.Menu(lambda: self.get_folder_menu_items())),
            pystray.MenuItem('Khởi động lại', self.restart_app),
            pystray.MenuItem('Thoát ứng dụng', self.quit_app)
        )

    def get_folder_menu_items(self):
        if not os.path.exists(QUERIES_DIR):
            os.makedirs(QUERIES_DIR, exist_ok=True)
        folders = [d for d in os.listdir(QUERIES_DIR) if os.path.isdir(os.path.join(QUERIES_DIR, d))]
        
        import re
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
        folders.sort(key=natural_sort_key)
        
        menu_items = []
        
        # Root option
        menu_items.append(
            pystray.MenuItem(
                "Tất cả (Root queries folder)",
                self.make_tray_select_handler("Tất cả (Root queries folder)"),
                checked=self.make_tray_checked_handler(QUERIES_DIR)
            )
        )
        
        for f in folders:
            folder_path = os.path.join(QUERIES_DIR, f)
            menu_items.append(
                pystray.MenuItem(
                    f,
                    self.make_tray_select_handler(f),
                    checked=self.make_tray_checked_handler(folder_path)
                )
            )
            
        return menu_items

    def make_tray_select_handler(self, folder_name):
        return lambda: self.select_folder_from_tray(folder_name)

    def make_tray_checked_handler(self, folder_path):
        return lambda _: self.current_queries_dir == folder_path

    def select_folder_from_tray(self, folder_name):
        self.root.after(0, lambda: self.apply_folder_selection(folder_name))

    def apply_folder_selection(self, folder_name):
        if folder_name == "Tất cả (Root queries folder)":
            self.cmb_queries.set("Tất cả (Root queries folder)")
            self.on_query_selected("Tất cả (Root queries folder)")
        else:
            self.cmb_queries.set(folder_name)
            self.on_query_selected(folder_name)

    def show_window(self, icon=None, item=None):
        if getattr(self, 'left_click_timer', None) is not None:
            try:
                self.root.after_cancel(self.left_click_timer)
            except (tk.TclError, ValueError):
                pass
            self.left_click_timer = None
        self.root.after(0, self.root.deiconify)
        self.root.after(10, self.root.focus_force)

    def hide_window(self):
        self.root.withdraw()
        try:
            self.tray_icon.notify(
                "Ứng dụng vẫn đang hoạt động dưới khay hệ thống.",
                title=APP_NAME
            )
        except (OSError, RuntimeError, AttributeError):
            pass

    def show_osd(self, text):
        import tkinter.font as tkfont
        font = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        text_width = font.measure(text)
        text_height = font.metrics("linespace")
        
        # Calculate window dimensions with padding
        padx = 25
        pady = 10
        w = text_width + padx * 2
        h = text_height + pady * 2
        
        # Center in the upper part of the screen
        ws = self.root.winfo_screenwidth()
        x = (ws - w) // 2
        y = 80  # 80px from top
        
        # Create OSD window if not exists
        if not getattr(self, 'osd_window', None) or not self.osd_window.winfo_exists():
            self.osd_window = tk.Toplevel(self.root)
            self.osd_window.overrideredirect(True)  # Borderless
            self.osd_window.attributes("-topmost", True)  # Always on top
            self.osd_window.attributes("-alpha", 0.92)  # Semi-transparent
            self.osd_window.configure(bg="#1E2228")
            
            self.osd_canvas = tk.Canvas(self.osd_window, highlightthickness=0, bg="#1E2228")
            self.osd_canvas.pack(fill="both", expand=True)
        else:
            self.osd_canvas.delete("all")
            
        # Update window geometry
        self.osd_window.geometry(f"{w}x{h}+{x}+{y}")
        
        # Draw mathematically perfect rounded rectangle inside canvas (inset by 2px)
        self.draw_rounded_rect(self.osd_canvas, 2, 2, w - 2, h - 2, radius=12, fill="#1E2228", outline="#3B82F6", width=2)
        
        # Draw text in the center
        self.osd_canvas.create_text(
            w // 2,
            h // 2,
            text=text,
            font=("Segoe UI", 13, "bold"),
            fill="#FFFFFF"
        )
        
        # Apply rounded corners to HWND (use same radius 12)
        try:
            hwnd = self.osd_window.winfo_id()
            import ctypes
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w, h, 12, 12)
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
        except (OSError, AttributeError, ctypes.ArgumentError) as e:
            print(f"Lỗi bo góc OSD: {e}")
            
        # Make sure the window is visible (deiconified)
        self.osd_window.deiconify()
        
        # Reset hide timer
        if getattr(self, 'osd_timer', None) is not None:
            try:
                self.root.after_cancel(self.osd_timer)
            except (tk.TclError, ValueError):
                pass
        self.osd_timer = self.root.after(1000, self.hide_osd)

    def draw_rounded_rect(self, canvas, x1, y1, x2, y2, radius=12, **kwargs):
        fill = kwargs.get('fill', '#12151A')
        outline = kwargs.get('outline', '#3B82F6')
        width = kwargs.get('width', 2)
        
        # Draw fill parts (no outlines)
        canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, outline="")
        canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=fill, outline="")
        
        # Draw 4 corner fill circles (arcs)
        canvas.create_arc(x1, y1, x1 + radius * 2, y1 + radius * 2, start=90, extent=90, fill=fill, outline="")
        canvas.create_arc(x2 - radius * 2, y1, x2, y1 + radius * 2, start=0, extent=90, fill=fill, outline="")
        canvas.create_arc(x1, y2 - radius * 2, x1 + radius * 2, y2, start=180, extent=90, fill=fill, outline="")
        canvas.create_arc(x2 - radius * 2, y2 - radius * 2, x2, y2, start=270, extent=90, fill=fill, outline="")
        
        # Draw outlines (lines and arcs)
        canvas.create_line(x1 + radius, y1, x2 - radius, y1, fill=outline, width=width)
        canvas.create_line(x1 + radius, y2, x2 - radius, y2, fill=outline, width=width)
        canvas.create_line(x1, y1 + radius, x1, y2 - radius, fill=outline, width=width)
        canvas.create_line(x2, y1 + radius, x2, y2 - radius, fill=outline, width=width)
        
        canvas.create_arc(x1, y1, x1 + radius * 2, y1 + radius * 2, start=90, extent=90, style="arc", outline=outline, width=width)
        canvas.create_arc(x2 - radius * 2, y1, x2, y1 + radius * 2, start=0, extent=90, style="arc", outline=outline, width=width)
        canvas.create_arc(x1, y2 - radius * 2, x1 + radius * 2, y2, start=180, extent=90, style="arc", outline=outline, width=width)
        canvas.create_arc(x2 - radius * 2, y2 - radius * 2, x2, y2, start=270, extent=90, style="arc", outline=outline, width=width)

    def hide_osd(self):
        self.osd_timer = None
        if getattr(self, 'osd_window', None) and self.osd_window.winfo_exists():
            self.osd_window.withdraw()

    def restart_app(self, icon=None, item=None):
        if hasattr(self, 'hotkey_manager'):
            self.hotkey_manager.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        if getattr(self, 'mutex', None) is not None:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self.mutex)
            self.mutex = None
        self.root.after(0, self.actual_restart)

    def actual_restart(self):
        import subprocess
        self.is_monitoring = False
        self.root.destroy()
        subprocess.Popen([sys.executable] + sys.argv, creationflags=subprocess.CREATE_NO_WINDOW if sys.executable.endswith('w.exe') else 0)
        sys.exit(0)

    def quit_app(self, icon=None, item=None):
        if hasattr(self, 'hotkey_manager'):
            self.hotkey_manager.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.after(0, self.actual_quit)

    def actual_quit(self):
        self.is_monitoring = False
        self.root.destroy()
        sys.exit(0)

if __name__ == "__main__":
    import ctypes
    from tkinter import messagebox
    
    # Single instance check. The variant owns a different mutex than the
    # original app, so both can be running at the same time while each still
    # refuses to start twice.
    mutex_name = APP_MUTEX_NAME
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, True, mutex_name)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Thông báo", f"Ứng dụng {APP_NAME} đã đang chạy ở khay hệ thống (Tray Icon)!\nHãy kiểm tra ở góc dưới bên phải màn hình.")
        root.destroy()
        sys.exit(0)

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    app = AutoMarkerApp(root, mutex=mutex)
    root.mainloop()
