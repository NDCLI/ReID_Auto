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
DIRECT_CAPTURE_SAVE_DIR = r"C:\Users\HVV-AI33\Pictures\Screenshots"


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
        # Reserved for AUTO.ahk's right-click handler in BLAZE.  It is not a
        # ShareX shortcut, so the two capture systems remain independent.
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
        self.assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
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
        w = 760
        h = 640
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws - w) // 2
        y = (hs - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.resizable(True, True)
        self.root.minsize(680, 620)
        
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
            max(680, self.root.winfo_reqwidth()),
            max(620, self.root.winfo_reqheight() + 8),
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

    def setup_ui(self):
        # Configure appearance
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.root.configure(fg_color="#15181C")

        # Header bar (product identity + live state)
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill=tk.X, padx=16, pady=(16, 8))

        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.pack(side=tk.LEFT)
        ctk.CTkLabel(
            title_group,
            text="📸 AUTOMARKER RE-ID",
            font=("Segoe UI", 16, "bold"),
            text_color="#E6EAF0",
        ).pack(anchor=tk.W)
        ctk.CTkLabel(
            title_group,
            text="Theo dõi Clipboard • Nhận diện và vẽ khung tự động",
            font=("Segoe UI", 10),
            text_color="#8E98A8",
        ).pack(anchor=tk.W, pady=(1, 0))

        self.lbl_status = ctk.CTkLabel(
            header,
            text="●",
            text_color="#EF4444",
            fg_color="transparent",
            corner_radius=10,
            font=("Segoe UI", 18, "bold"),
            width=24,
            height=24,
        )
        self.lbl_status.pack(side=tk.RIGHT)

        # Compact single-row action bar.  Keeping all actions above the log
        # prevents the last control from being clipped when the window gets
        # shorter.
        action_bar = ctk.CTkFrame(self.root, fg_color="transparent")
        action_bar.pack(fill=tk.X, padx=16, pady=(0, 8))

        ctk.CTkButton(
            action_bar,
            text="🔄 Làm mới + OCR",
            width=130,
            height=28,
            corner_radius=7,
            font=("Segoe UI", 9, "bold"),
            command=self.refresh_and_rebuild_cache,
            fg_color="#0EA5A3",
            hover_color="#0C8886",
        ).pack(side=tk.LEFT, padx=(0, 5))

        ctk.CTkButton(
            action_bar,
            text="📚 Ảnh đã lưu",
            width=100,
            height=28,
            corner_radius=7,
            command=self.open_library_window,
            fg_color="#2A2F37",
            hover_color="#353B45",
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 5))

        ctk.CTkButton(
            action_bar,
            text="Xóa log",
            width=65,
            height=28,
            corner_radius=7,
            command=self.clear_logs,
            fg_color="transparent",
            border_width=1,
            border_color="#3A414C",
            hover_color="#2A2F37",
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=(0, 5))

        ctk.CTkButton(
            action_bar,
            text="🗑 Xóa Query",
            width=100,
            height=28,
            corner_radius=7,
            command=self.clear_data,
            fg_color="transparent",
            border_width=1,
            border_color="#7F3540",
            text_color="#FCA5A5",
            hover_color="#3A2025",
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 8))

        ctk.CTkLabel(
            action_bar,
            text="LƯU:",
            font=("Segoe UI", 9, "bold"),
            text_color="#8E98A8",
        ).pack(side=tk.LEFT, padx=(0, 4))

        self.cmb_capture_query = ctk.CTkOptionMenu(
            action_bar,
            width=105,
            height=28,
            dynamic_resizing=False,
            command=self.on_capture_query_selected,
        )
        self.cmb_capture_query.pack(side=tk.LEFT, padx=(0, 5))

        self.btn_next_capture_query = ctk.CTkButton(
            action_bar,
            text="Query trống",
            height=28,
            corner_radius=7,
            width=95,
            command=self.select_next_empty_capture_query,
            fg_color="#8B5CF6",
            hover_color="#7C3AED",
            font=("Segoe UI", 9, "bold"),
        )
        self.btn_next_capture_query.pack(side=tk.LEFT)

        capture_bar = ctk.CTkFrame(self.root, fg_color="transparent")
        capture_bar.pack(fill=tk.X, padx=16, pady=(0, 8))

        ctk.CTkButton(
            capture_bar,
            text="✂ Chụp vùng",
            width=138,
            height=30,
            corner_radius=7,
            command=self.start_region_capture,
            fg_color="#2563EB",
            hover_color="#1D4ED8",
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 6))

        ctk.CTkButton(
            capture_bar,
            text="↻ Chụp lại vùng trước",
            width=172,
            height=30,
            corner_radius=7,
            command=self.capture_last_region,
            fg_color="#334155",
            hover_color="#475569",
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 10))

        ctk.CTkCheckBox(
            capture_bar,
            text="Lưu ảnh chụp",
            variable=self.save_direct_captures,
            onvalue=True,
            offvalue=False,
            checkbox_width=18,
            checkbox_height=18,
            font=("Segoe UI", 10),
            text_color="#CBD5E1",
            fg_color="#2563EB",
            hover_color="#1D4ED8",
        ).pack(side=tk.LEFT)

        # Body: sidebar (left) + content (right)
        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 16))

        # -------- SIDEBAR (left) --------
        sidebar = ctk.CTkFrame(body, width=205, fg_color="#1E2228", corner_radius=12, border_width=1, border_color="#2A2F37")
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        lbl_side_folder = ctk.CTkLabel(
            sidebar,
            text="📁 THƯ MỤC MẪU",
            font=("Segoe UI", 10, "bold"),
            text_color="#E6EAF0",
        )
        lbl_side_folder.pack(anchor=tk.W, padx=12, pady=(12, 6))

        self.lbl_folder_summary = ctk.CTkLabel(
            sidebar,
            text="Đang tải danh sách...",
            font=("Segoe UI", 9),
            text_color="#8E98A8",
        )
        self.lbl_folder_summary.pack(anchor=tk.W, padx=12, pady=(0, 7))

        # The folder tree below is the primary picker. The OptionMenu is kept
        # unpacked only because the hotkey/tray code paths still call `.set()`
        # on a widget tagged `cmb_queries` to stay in sync.
        self.cmb_queries = ctk.CTkOptionMenu(
            sidebar,
            width=180,
            height=30,
            dynamic_resizing=False,
            command=self.on_query_selected,
        )

        # Plain tk.Canvas + ttk.Scrollbar instead of CTkScrollableFrame. CTk
        # widgets redraw their canvas on every <Configure> (window resize), so a
        # tree holding many CTkButton items makes dragging the window laggy.
        # Native tk items have no canvas redraw cost on resize.
        # Use standard dark colors that match CTk's default dark theme
        tree_bg = "#1E2228"
        tree_wrap = tk.Frame(sidebar, bg=tree_bg, bd=0, highlightthickness=0)
        tree_wrap.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

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
        content = ctk.CTkFrame(body, fg_color="transparent")
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        log_header = ctk.CTkFrame(content, fg_color="transparent")
        log_header.pack(fill=tk.X, pady=(1, 5))
        ctk.CTkLabel(
            log_header,
            text="NHẬT KÝ HOẠT ĐỘNG",
            font=("Segoe UI", 10, "bold"),
            text_color="#CBD2DC",
        ).pack(side=tk.LEFT)
        ctk.CTkLabel(
            log_header,
            text="Tự cuộn theo sự kiện mới",
            font=("Segoe UI", 9),
            text_color="#687384",
        ).pack(side=tk.RIGHT)

        log_frame = tk.Frame(
            content,
            bg="#12151A",
            highlightthickness=1,
            highlightbackground="#2A2F37",
        )
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        
        scrollbar = ctk.CTkScrollbar(log_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.txt_logs = tk.Text(
            log_frame,
            bg="#12151A",
            fg="#E6EAF0",
            insertbackground="#E6EAF0",
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
        hover_bg = "#262B33"
        normal_bg = "#1E2228"
        selected_bg = "#3B82F6"
        normal_fg = "#E6EAF0"
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
        overlay.bind("<Escape>", self.cancel_region_capture)
        overlay.protocol("WM_DELETE_WINDOW", self.cancel_region_capture)
        overlay.focus_force()
        try:
            overlay.grab_set()
        except tk.TclError:
            pass

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
        """Run an in-app capture through the normal Re-ID/Review pipeline."""
        if pil_img is None:
            return
        try:
            image = pil_img.convert("RGB").copy()
        except (AttributeError, ValueError) as exc:
            print(f"  [CAPTURE] Ảnh chụp không hợp lệ: {exc}")
            self.show_osd("⚠️ Ảnh chụp không hợp lệ")
            return
        if self._should_save_direct_capture():
            saved_path = self._save_direct_capture_image(image)
            if saved_path:
                print(f"  [CAPTURE] Đã lưu ảnh gốc: {saved_path}")
        detected_at = time.perf_counter()
        self.is_processing = True
        print(f"  [CAPTURE] Đã chụp {capture_label}; đang nhận diện...")
        threading.Thread(
            target=self.process_clipboard_image,
            args=(image, detected_at),
            daemon=True,
        ).start()

    def _should_save_direct_capture(self):
        save_toggle = getattr(self, "save_direct_captures", None)
        try:
            return bool(save_toggle.get())
        except (AttributeError, tk.TclError):
            return False

    def _save_direct_capture_image(self, image):
        """Persist the unmodified direct capture before recognition begins."""
        try:
            os.makedirs(DIRECT_CAPTURE_SAVE_DIR, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = os.path.join(DIRECT_CAPTURE_SAVE_DIR, f"ReID_{stamp}.png")
            image.save(path, "PNG")
            return path
        except (OSError, ValueError) as exc:
            print(f"  [CAPTURE] Không thể lưu ảnh gốc: {exc}")
            return None

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
            if not self.check_is_reid_interface(current_bgr):
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
            base_dir = os.path.dirname(os.path.abspath(__file__))
            template_path = os.path.join(base_dir, "ui_template.png")
            if not os.path.exists(template_path):
                print(f"  [WARN] UI template not found at {template_path}. Skipping validation.")
                return True
                
            template = cv2.imread(template_path)
            if template is None:
                print("  [WARN] Failed to load UI template. Skipping validation.")
                return True
                
            gray_img = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            gray_temp = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

            th, tw = gray_temp.shape
            h, w = gray_img.shape

            # A Re-ID interface screenshot is always a wide, landscape capture of
            # the whole search window (large and clearly wider than tall). A Query
            # sample is a single portrait person crop — taller than wide, and small.
            # Reject those outright so a copied crop is never mistaken for the UI.
            if w <= h:
                print(f"  [REID DETECT] Portrait image ({w}x{h}) → treated as Query crop, not interface.")
                return False
            if w < 900:
                print(f"  [REID DETECT] Image too small ({w}x{h}) → treated as Query crop, not interface.")
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
            return True

    def open_preview_window(
        self, current_bgr, matches, detected_at=None, matching_elapsed=None
    ):
        # Open preview window on Main Thread
        ui_started = time.perf_counter()
        self.active_preview_window = PreviewWindow(
            self.root,
            current_bgr,
            matches,
            self.matcher,
            OUTPUT_DIR,
            on_close_callback=self._on_preview_window_closed,
        )
        self.is_processing = False
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
