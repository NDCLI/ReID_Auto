import os
import re
import shutil
import sys
import datetime
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
import queue
import customtkinter as ctk
import pystray

# Import logic from our existing scripts
from auto_marker import (
    TemplateMatcher,
    get_clipboard_image,
    get_clipboard_image_hash,
    read_image_file,
)
from batch_review import BatchReviewWindow, classify_item_query
from config import APP_MUTEX_NAME, APP_NAME, QUERIES_DIR, OUTPUT_DIR, MATCH_THRESHOLD
from library_win import LibraryWindow
from preview_win import PreviewWindow
from query_organizer import QueryAutoCollector, MAX_QUERY_COUNT

def get_foreground_process_name():
    """Return the lowercase exe name of the foreground window's process.

    Returns an empty string if it can't be determined. Used to skip clipboard
    processing when another app (e.g. Excel running a copy macro) is active.
    """
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

        hwnd = user32.GetForegroundWindow()
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


# Foreground processes whose clipboard activity should not trigger auto-processing.
# Excel copy macros (e.g. CopyAnh) push image shapes onto the clipboard, which the
# monitor would otherwise mistake for new Re-ID screenshots and auto-open Review.
CLIPBOARD_IGNORE_PROCESSES = {"excel.exe"}


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
        HOTKEY_TOGGLE = 102
        HOTKEY_NEW_CAPTURE_QUERY = 103
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

        # Ctrl+Shift+Space (Pause/Resume Toggle)
        register(HOTKEY_TOGGLE, MODS, 0x20, "Ctrl+Shift+Space")
        # MOD_NOREPEAT (0x4000) prevents one long press from skipping through
        # several empty Query slots.
        register(
            HOTKEY_NEW_CAPTURE_QUERY,
            MODS | 0x4000,
            0x4E,
            "Ctrl+Shift+N",
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
            user32.UnregisterHotKey(None, HOTKEY_TOGGLE)
            user32.UnregisterHotKey(None, HOTKEY_NEW_CAPTURE_QUERY)
            for i in range(10):
                user32.UnregisterHotKey(None, HOTKEY_NUM_BASE + i)

    def _handle_hotkey(self, hotkey_id):
        self.app.root.after(0, lambda: self._process_hotkey_on_main_thread(hotkey_id))

    def _process_hotkey_on_main_thread(self, hotkey_id):
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
        elif hotkey_id == 102: # TOGGLE (Pause/Resume)
            if self.app.is_monitoring:
                self.app.stop_marker()
                self.app.show_osd("🔴 ĐÃ TẠM DỪNG VẼ KHUNG")
            else:
                self.app.start_marker()
                if self.app.is_monitoring:
                    self.app.show_osd("🟢 ĐÃ TIẾP TỤC VẼ KHUNG")
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
        w = 700
        h = 700
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws - w) // 2
        y = (hs - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.resizable(False, False)
        
        # Monitoring and Log state
        self.is_monitoring = False
        self.log_queue = queue.Queue()
        self.last_clipboard_hash = None
        self.is_processing = False
        self.active_preview_window = None
        self.left_click_timer = None
        self.last_tray_click_time = 0.0
        self.osd_window = None
        self.osd_timer = None
        self.query_collector = None
        self.auto_query_capture_enabled = True
        self.auto_query_capture = tk.BooleanVar(value=True)
        self.capture_query_target = None
        
        self.current_queries_dir = QUERIES_DIR
        
        self.setup_ui()
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

        # Main frame
        main_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        # TITLE
        lbl_title = ctk.CTkLabel(
            main_frame, 
            text="📸 RE-ID AUTO DRAW",
            font=("Segoe UI", 18, "bold"), 
            text_color="#3498DB"
        )
        lbl_title.pack(pady=(0, 15))

        # --- SECTION 1: DATA SOURCE ---
        frame_data = ctk.CTkFrame(main_frame, corner_radius=10, border_width=1, border_color="#34495E")
        frame_data.pack(fill=tk.X, pady=8, padx=2)

        # Header for Section 1
        lbl_sec1_header = ctk.CTkLabel(
            frame_data, 
            text="📁 BƯỚC 1: QUẢN LÝ DỮ LIỆU MẪU", 
            font=("Segoe UI", 12, "bold"), 
            text_color="#ECF0F1"
        )
        lbl_sec1_header.pack(anchor=tk.W, padx=15, pady=(10, 5))

        # Content container inside frame_data for padding
        content_data = ctk.CTkFrame(frame_data, fg_color="transparent")
        content_data.pack(fill=tk.X, padx=15, pady=(0, 10))

        # 1.1 Chọn thư mục dữ liệu
        lbl_queries_title = ctk.CTkLabel(
            content_data, 
            text="Thư mục chứa ảnh mẫu (Queries):", 
            font=("Segoe UI", 11)
        )
        lbl_queries_title.pack(anchor=tk.W, pady=(5, 2))

        frame_dir = ctk.CTkFrame(content_data, fg_color="transparent")
        frame_dir.pack(fill=tk.X, pady=2)
        
        self.cmb_queries = ctk.CTkComboBox(
            frame_dir, 
            width=360, 
            state="readonly", 
            command=self.on_query_selected
        )
        self.cmb_queries.pack(side=tk.LEFT, padx=(0, 8))
        
        self.update_queries_dropdown()
        
        btn_refresh_dir = ctk.CTkButton(
            frame_dir, 
            text="🔄 Làm Mới", 
            width=90,
            command=self.update_queries_dropdown,
            fg_color="#34495E",
            hover_color="#2C3E50"
        )
        btn_refresh_dir.pack(side=tk.LEFT, padx=3)

        btn_clear_dir = ctk.CTkButton(
            frame_dir, 
            text="🗑 XÓA DỮ LIỆU", 
            width=110,
            command=self.clear_data,
            fg_color="#E74C3C",
            hover_color="#C0392B"
        )
        btn_clear_dir.pack(side=tk.LEFT, padx=3)

        self.switch_auto_query = ctk.CTkSwitch(
            content_data,
            text="Tự nhận ảnh chụp 1 người từ Clipboard và phân vào Query",
            variable=self.auto_query_capture,
            onvalue=True,
            offvalue=False,
            command=self.on_auto_query_toggle,
            font=("Segoe UI", 11, "bold"),
            progress_color="#8E44AD",
        )
        self.switch_auto_query.pack(anchor=tk.W, pady=(9, 0))

        frame_capture_query = ctk.CTkFrame(content_data, fg_color="transparent")
        frame_capture_query.pack(fill=tk.X, pady=(7, 0))

        self.cmb_capture_query = ctk.CTkComboBox(
            frame_capture_query,
            width=360,
            state="readonly",
            command=self.on_capture_query_selected,
        )
        self.cmb_capture_query.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_next_capture_query = ctk.CTkButton(
            frame_capture_query,
            text="Query trống (Space)",
            width=150,
            command=self.select_next_empty_capture_query,
            fg_color="#8E44AD",
            hover_color="#71368A",
        )
        self.btn_next_capture_query.pack(side=tk.LEFT)
        self.update_capture_query_dropdown()

        # --- SECTION 2: AUTO MARKER ---
        frame_marker = ctk.CTkFrame(main_frame, corner_radius=10, border_width=1, border_color="#34495E")
        frame_marker.pack(fill=tk.X, pady=8, padx=2)

        lbl_sec2_header = ctk.CTkLabel(
            frame_marker, 
            text="🎯 BƯỚC 2: CÔNG CỤ VẼ KHUNG (SNIPPING TOOL)", 
            font=("Segoe UI", 12, "bold"), 
            text_color="#ECF0F1"
        )
        lbl_sec2_header.pack(anchor=tk.W, padx=15, pady=(10, 5))

        content_marker = ctk.CTkFrame(frame_marker, fg_color="transparent")
        content_marker.pack(fill=tk.X, padx=15, pady=(0, 10))

        frame_buttons = ctk.CTkFrame(content_marker, fg_color="transparent")
        frame_buttons.pack(fill=tk.X, pady=5)

        self.btn_start = ctk.CTkButton(
            frame_buttons, 
            text="▶ BẬT (START)", 
            font=("Segoe UI", 11, "bold"),
            command=self.start_marker,
            fg_color="#2ECC71",
            hover_color="#27AE60"
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8), expand=True, fill=tk.X)

        self.btn_stop = ctk.CTkButton(
            frame_buttons, 
            text="⏹ TẮT (STOP)", 
            font=("Segoe UI", 11, "bold"),
            state="disabled", 
            command=self.stop_marker,
            fg_color="#E74C3C",
            hover_color="#C0392B"
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(8, 0), expand=True, fill=tk.X)

        self.btn_batch = ctk.CTkButton(
            content_marker,
            text="BATCH: CHỌN NHIỀU ẢNH & MỞ THƯ VIỆN DUYỆT",
            font=("Segoe UI", 11, "bold"),
            command=self.run_batch_dialog,
            fg_color="#D68910",
            hover_color="#B9770E",
            height=32,
        )
        self.btn_batch.pack(fill=tk.X, pady=(6, 0))

        self.btn_library = ctk.CTkButton(
            content_marker,
            text="📚 THƯ VIỆN ẢNH ĐÃ LƯU",
            font=("Segoe UI", 11, "bold"),
            command=self.open_library_window,
            fg_color="#5D6D7E",
            hover_color="#48586A",
            height=32,
        )
        self.btn_library.pack(fill=tk.X, pady=(6, 0))

        self.lbl_status = ctk.CTkLabel(
            content_marker, 
            text="Trạng thái: ĐANG DỪNG 🔴", 
            text_color="#E74C3C", 
            font=("Segoe UI", 12, "bold")
        )
        self.lbl_status.pack(pady=(8, 0))

        # --- SECTION 3: LOGS ---
        frame_logs = ctk.CTkFrame(main_frame, corner_radius=10, border_width=1, border_color="#34495E")
        frame_logs.pack(fill=tk.BOTH, expand=True, pady=8, padx=2)

        lbl_sec3_header = ctk.CTkLabel(
            frame_logs, 
            text="📝 NHẬT KÝ HOẠT ĐỘNG (LOGS)", 
            font=("Segoe UI", 11, "bold"), 
            text_color="#ECF0F1"
        )
        lbl_sec3_header.pack(anchor=tk.W, padx=15, pady=(8, 4))

        content_logs = ctk.CTkFrame(frame_logs, fg_color="transparent")
        content_logs.pack(fill=tk.BOTH, expand=True, padx=15, pady=(0, 10))

        self.txt_logs = ctk.CTkTextbox(
            content_logs, 
            font=("Consolas", 11), 
            fg_color="#1E272C", 
            text_color="#ECF0F1"
        )
        self.txt_logs.pack(fill=tk.BOTH, expand=True)

        # Redirect stdout and stderr
        sys.stdout = RedirectStdout(self.txt_logs, self.log_queue)
        sys.stderr = sys.stdout
        print("Sẵn sàng!")

    def update_queries_dropdown(self):
        """Update the dropdown with subfolders in queries directory."""
        if not os.path.exists(QUERIES_DIR):
            os.makedirs(QUERIES_DIR, exist_ok=True)
            
        folders = [d for d in os.listdir(QUERIES_DIR) if os.path.isdir(os.path.join(QUERIES_DIR, d))]
        
        import re
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
        folders.sort(key=natural_sort_key)
        
        folders.insert(0, "Tất cả (Root queries folder)")
        
        self.cmb_queries.configure(values=folders)
        
        # Determine current selection index
        current_name = os.path.basename(self.current_queries_dir)
        if current_name in folders:
            self.cmb_queries.set(current_name)
        else:
            self.cmb_queries.set(folders[0])

        if hasattr(self, "cmb_capture_query"):
            self.update_capture_query_dropdown()

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
        options = ["Tự phân loại (AI)"] + [f"Query_{number}" for number in range(1, upper + 1)]
        self.cmb_capture_query.configure(values=options)
        selection = self.capture_query_target or options[0]
        self.cmb_capture_query.set(selection)

    def on_capture_query_selected(self, selection):
        if selection == "Tự phân loại (AI)":
            self.capture_query_target = None
            print("[AUTO QUERY] Đích chụp: AI tự phân loại.")
            self.show_osd("📥 Query chụp: AI tự phân loại")
            return

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
                if os.path.isdir(self.current_queries_dir):
                    for root_dir, _dirs, files in os.walk(self.current_queries_dir):
                        for file in files:
                            if file.lower().endswith(valid_exts):
                                os.remove(os.path.join(root_dir, file))
                                deleted_queries += 1

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
                    f"Đã xóa {deleted_queries} ảnh Query và "
                    f"{deleted_outputs} tệp kết quả đã vẽ.",
                )
            except (OSError, PermissionError, IOError) as e:
                messagebox.showerror("Lỗi", f"Lỗi khi xóa: {str(e)}")

    def run_batch_dialog(self):
        paths = filedialog.askopenfilenames(
            title="Chọn nhiều screenshot để xử lý Batch",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.bmp *.webp")],
        )
        if not paths:
            return
        if not getattr(self, "matcher", None):
            messagebox.showwarning(
                "Chưa nạp Query",
                "Hãy bật công cụ vẽ khung để nạp dữ liệu Query trước khi chạy Batch.",
            )
            return

        self.is_processing = True
        self.btn_batch.configure(state="disabled", text="ĐANG XỬ LÝ BATCH...")
        batch_output = os.path.join(
            OUTPUT_DIR, f"batch_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )

        def task():
            items = []
            try:
                for index, path in enumerate(paths, start=1):
                    image = read_image_file(path)
                    if image is None:
                        print(f"[BATCH] Bỏ qua file không đọc được: {path}")
                        continue
                    matches = self.matcher.find_matches(image, debug=False)
                    batch_query = classify_item_query(matches)
                    items.append({
                        "path": path,
                        "image": image,
                        "matches": matches,
                        "query": batch_query,
                    })
                    print(
                        f"[BATCH] {index}/{len(paths)}: {batch_query} · "
                        f"{len(matches)} khung - {path}"
                    )

                if not items:
                    raise ValueError("Không có ảnh hợp lệ để duyệt.")

                def open_library():
                    def on_close():
                        self.active_preview_window = None
                        self.is_processing = False
                        self.btn_batch.configure(
                            state="normal",
                            text="BATCH: CHỌN NHIỀU ẢNH & MỞ THƯ VIỆN DUYỆT",
                        )

                    self.active_preview_window = BatchReviewWindow(
                        self.root, items, batch_output, on_close=on_close
                    )

                self.root.after(0, open_library)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[BATCH ERROR] {exc}")
                error_message = str(exc)

                def show_error(error=error_message):
                    self.is_processing = False
                    self.btn_batch.configure(
                        state="normal",
                        text="BATCH: CHỌN NHIỀU ẢNH & MỞ THƯ VIỆN DUYỆT",
                    )
                    messagebox.showerror("Batch thất bại", error)

                self.root.after(0, show_error)

        threading.Thread(target=task, daemon=True).start()

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

        self.active_preview_window = LibraryWindow(
            self.root, OUTPUT_DIR, on_close=on_close
        )

    def start_marker(self):
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
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="Trạng thái: ĐANG CHẠY 🟢", text_color="#2ECC71")
        
        print("\n" + "=" * 60)
        print(f"  [READY] Monitoring clipboard for screenshots...")
        print(f"  [INFO] Output directory: {OUTPUT_DIR}")
        print("=" * 60 + "\n")
        
        # To prevent Tkinter and OpenVINO threading deadlock, initialize on Main Thread
        self.root.update()
        
        try:
            # Always keep one root-level reference cache. Folder changes can then
            # be applied instantly instead of destroying and rebuilding the AI.
            self.matcher = TemplateMatcher(queries_dir=QUERIES_DIR, threshold=MATCH_THRESHOLD)
            self._matcher_reference_cache = self.matcher.reference_images
            self._matcher_query_image_cache = self.matcher.query_images
            self._apply_matcher_query_selection()
            self.query_collector = QueryAutoCollector(
                QUERIES_DIR, self.matcher.ai_extractor
            )
            self.last_clipboard_hash = get_clipboard_image_hash()
            self.poll_clipboard()
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"\n❌ Lỗi khởi tạo AI: {str(e)}")
            self.stop_marker()

    def stop_marker(self):
        self.is_monitoring = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.lbl_status.configure(text="Trạng thái: ĐANG DỪNG 🔴", text_color="#E74C3C")
        print("\n⏹ Đã dừng tool vẽ khung.")

    def poll_clipboard(self):
        if not getattr(self, 'is_monitoring', False):
            return
            
        # Skip clipboard checking if we are already processing or if a review window is currently open
        if getattr(self, 'is_processing', False):
            self.root.after(500, self.poll_clipboard)
            return
            
        if getattr(self, 'active_preview_window', None) is not None:
            try:
                if self.active_preview_window.winfo_exists():
                    self.root.after(500, self.poll_clipboard)
                    return
            except (tk.TclError, AttributeError):
                self.active_preview_window = None
                
        try:
            current_hash = get_clipboard_image_hash()
            if current_hash is not None and current_hash != self.last_clipboard_hash:
                self.last_clipboard_hash = current_hash
                
                # Check if we should ignore this change (triggered by our own Save & Copy)
                if getattr(self.matcher, 'ignore_next_clipboard', False):
                    self.matcher.ignore_next_clipboard = False
                    print("  [INFO] Ignoring clipboard change triggered by Preview Save.")
                elif get_foreground_process_name() in CLIPBOARD_IGNORE_PROCESSES:
                    # An Excel copy macro (or similar) is pushing images onto the
                    # clipboard. The hash is already recorded above, so this image
                    # won't retrigger later even after Excel loses focus.
                    print("  [INFO] Ignoring clipboard change from Excel (copy macro).")
                else:
                    print("  [DETECT] New image found in clipboard.")
                    pil_img = get_clipboard_image()
                    if pil_img is not None:
                        self.is_processing = True
                        # Process image in a background thread so GUI doesn't freeze during matching
                        threading.Thread(target=self.process_clipboard_image, args=(pil_img,), daemon=True).start()
        except (OSError, ValueError, TypeError) as e:
            print(f"  [CLIPBOARD POLL ERROR] {e}")
            
        # Schedule next check in 500ms
        self.root.after(500, self.poll_clipboard)

    def process_clipboard_image(self, pil_img):
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
                        result = self.query_collector.add_crop(
                            current_bgr, target_query=self.capture_query_target
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
                self.root.after(0, lambda: self.open_preview_window(current_bgr, []))
            else:
                print(f"  [RESULT] Found {len(matches)} match(es) in {elapsed:.1f}s.")
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBeep(0x00000040)  # Asterisk / Info sound
                except (OSError, AttributeError):
                    pass
                self.root.after(0, lambda: self.open_preview_window(current_bgr, matches))
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
                    
            print(f"  [REID DETECT] Best match confidence for Re-ID UI: {best_val:.4f}")
            # A threshold of 0.70 is extremely safe and robust
            return best_val >= 0.70
        except (cv2.error, OSError, ValueError) as e:
            print(f"  [WARN] Error in check_is_reid_interface: {e}")
            return True

    def open_preview_window(self, current_bgr, matches):
        # Open preview window on Main Thread
        self.active_preview_window = PreviewWindow(self.root, current_bgr, matches, self.matcher, OUTPUT_DIR)
        self.is_processing = False

    def process_logs(self):
        """Consume logs from the queue and write to the text widget (thread-safe)."""
        while not self.log_queue.empty():
            msg = self.log_queue.get()
            self.txt_logs.insert(tk.END, msg)
            self.txt_logs.see(tk.END)
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
            pystray.MenuItem(lambda item: "⏹ Tạm dừng vẽ khung" if self.is_monitoring else "▶ Tiếp tục vẽ khung", self.toggle_monitoring_from_menu),
            pystray.MenuItem('Chọn nhanh Folder', pystray.Menu(lambda: self.get_folder_menu_items())),
            pystray.MenuItem('Khởi động lại', self.restart_app),
            pystray.MenuItem('Thoát ứng dụng', self.quit_app)
        )

    def toggle_monitoring_from_menu(self, icon=None, item=None):
        self.root.after(0, self.actual_toggle_monitoring)
        
    def actual_toggle_monitoring(self):
        if self.is_monitoring:
            self.stop_marker()
            self.show_osd("🔴 ĐÃ TẠM DỪNG VẼ KHUNG")
        else:
            self.start_marker()
            if self.is_monitoring:
                self.show_osd("🟢 ĐÃ TIẾP TỤC VẼ KHUNG")

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
            self.osd_window.attributes("-alpha", 0.9)  # Semi-transparent
            self.osd_window.configure(bg="#1E272C")
            
            self.osd_canvas = tk.Canvas(self.osd_window, highlightthickness=0, bg="#1E272C")
            self.osd_canvas.pack(fill="both", expand=True)
        else:
            self.osd_canvas.delete("all")
            
        # Update window geometry
        self.osd_window.geometry(f"{w}x{h}+{x}+{y}")
        
        # Draw mathematically perfect rounded rectangle inside canvas (inset by 2px)
        self.draw_rounded_rect(self.osd_canvas, 2, 2, w - 2, h - 2, radius=12, fill="#1E272C", outline="#3498DB", width=2)
        
        # Draw text in the center
        self.osd_canvas.create_text(
            w // 2,
            h // 2,
            text=text,
            font=("Segoe UI", 13, "bold"),
            fill="#ECF0F1"
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
        fill = kwargs.get('fill', '#1E272C')
        outline = kwargs.get('outline', '#3498DB')
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
