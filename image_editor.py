import tkinter as tk
import customtkinter as ctk
from PIL import Image, ImageTk
import cv2
import numpy as np
import os
from dataclasses import dataclass
from config import RESOURCE_DIR


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff")


def list_library_images(directory: str | None) -> list[str]:
    """Liệt kê ảnh hợp lệ trực tiếp trong thư mục, mới nhất trước.

    Không đọc nội dung/full resolution tại bước này; file lỗi sẽ được bỏ qua khi
    tạo thumbnail hoặc lúc người dùng mở ảnh.
    """
    if not directory or not os.path.isdir(directory):
        return []
    entries = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or not name.lower().endswith(IMAGE_EXTENSIONS):
            continue
        if name.lower().startswith("original_"):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        entries.append((mtime, name.lower(), os.path.abspath(path)))
    entries.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in entries]


@dataclass
class PendingMergeState:
    """Trạng thái thuần Python cho ảnh thumbnail đang chờ ghép."""

    path: str | None = None

    def set(self, path: str | None) -> None:
        self.path = os.path.abspath(path) if path else None

    def clear(self) -> None:
        self.path = None

    def consume(self) -> str | None:
        path, self.path = self.path, None
        return path


@dataclass
class ThumbnailDragState:
    """Logic kéo-thả độc lập UI để có thể kiểm thử không cần display."""

    threshold: int = 8
    start: tuple[int, int] | None = None
    active: bool = False
    over_drop_zone: bool = False

    def press(self, x: int, y: int) -> None:
        self.start = (x, y)
        self.active = False
        self.over_drop_zone = False

    def update(self, x: int, y: int, bounds: tuple[int, int, int, int]) -> bool:
        if self.start is None:
            return False
        if not self.active:
            dx, dy = x - self.start[0], y - self.start[1]
            self.active = dx * dx + dy * dy >= self.threshold * self.threshold
        self.over_drop_zone = self.active and point_in_bounds(x, y, bounds)
        return self.active

    def release(self, x: int, y: int, bounds: tuple[int, int, int, int]) -> str:
        if self.start is None:
            result = "idle"
        else:
            self.update(x, y, bounds)
            result = "drop" if self.active and self.over_drop_zone else (
                "cancel" if self.active else "click"
            )
        self.reset()
        return result

    def reset(self) -> None:
        self.start = None
        self.active = False
        self.over_drop_zone = False


def point_in_bounds(x: int, y: int, bounds: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = bounds
    return left <= x <= right and top <= y <= bottom


def merge_images_side_by_side(
    current_bgr: np.ndarray,
    added_bgr: np.ndarray,
    side: str,
    background_color=(127, 127, 127),
) -> np.ndarray:
    """Ghép hai ảnh BGR cạnh nhau, căn giữa theo chiều dọc.

    Ảnh không bị co giãn nên giữ nguyên tỷ lệ và chất lượng. Phần trống do hai
    ảnh khác chiều cao được tô bằng nền xám trung tính.
    """
    if side not in {"left", "right"}:
        raise ValueError("side phải là 'left' hoặc 'right'")
    if not isinstance(current_bgr, np.ndarray) or not isinstance(added_bgr, np.ndarray):
        raise TypeError("Ảnh ghép phải là numpy.ndarray")
    if current_bgr.ndim != 3 or added_bgr.ndim != 3:
        raise ValueError("Ảnh ghép phải có dạng H x W x C")
    if current_bgr.shape[2] != added_bgr.shape[2]:
        raise ValueError("Hai ảnh phải có cùng số kênh màu")
    if current_bgr.size == 0 or added_bgr.size == 0:
        raise ValueError("Không thể ghép ảnh rỗng")

    height = max(current_bgr.shape[0], added_bgr.shape[0])
    width = current_bgr.shape[1] + added_bgr.shape[1]
    channels = current_bgr.shape[2]
    color = np.asarray(background_color, dtype=current_bgr.dtype).reshape(-1)
    if color.size != channels:
        raise ValueError("Màu nền phải khớp số kênh của ảnh")

    result = np.empty((height, width, channels), dtype=current_bgr.dtype)
    result[...] = color
    left_image, right_image = (
        (added_bgr, current_bgr) if side == "left" else (current_bgr, added_bgr)
    )
    x = 0
    for image in (left_image, right_image):
        y = (height - image.shape[0]) // 2
        result[y:y + image.shape[0], x:x + image.shape[1]] = image
        x += image.shape[1]
    return result


def calculate_fit_scale(image_width: int, image_height: int,
                        canvas_width: int, canvas_height: int) -> float:
    """Tính tỷ lệ fit-down, không bao giờ phóng ảnh vượt quá 100%."""
    if min(image_width, image_height, canvas_width, canvas_height) <= 0:
        raise ValueError("Kích thước ảnh và vùng xem phải lớn hơn 0")
    return min(1.0, canvas_width / image_width, canvas_height / image_height)


class ImageEditorWindow(ctk.CTkToplevel):
    """Lightweight image editor with Crop and Cut-out (horizontal/vertical auto-detection).

    Flow (ShareX-like):
        1. Chọn chế độ (Crop hoặc Cut-out).
        2. Chế độ Cut-out sẽ tự động phát hiện hướng kéo:
           - Kéo ngang rộng hơn dọc (dx > dy) -> Cut-out Dọc.
           - Kéo dọc cao hơn ngang (dy > dx) -> Cut-out Ngang.
        3. Thả chuột -> thực hiện cắt ngay lập tức.
        4. Ctrl+Z Hoàn tác, Đặt lại về ảnh gốc.
        5. Ctrl+S / Enter để Lưu và trả kết quả về callback.
    """

    MODE_CROP = "crop"
    MODE_CUTOUT = "cutout"

    def __init__(self, master, image_bgr: np.ndarray,
                 on_save_callback=None, on_cancel_callback=None, title="Chỉnh sửa ảnh",
                 library_dir=None, current_path=None, on_path_saved_callback=None):
        super().__init__(master)
        self.title(title)

        w, h = 1100, 720
        ws, hs = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(ws-w)//2}+{(hs-h)//2}")
        self.minsize(640, 480)

        # Icon
        assets_dir = os.path.join(RESOURCE_DIR, "assets")
        self._editor_icon = None
        def _apply_icon():
            try:
                self._editor_icon = tk.PhotoImage(
                    file=os.path.join(assets_dir, "app_icon.png"))
                self.iconphoto(False, self._editor_icon)
                if os.name == "nt":
                    self.iconbitmap(os.path.join(assets_dir, "app_icon.ico"))
            except (tk.TclError, OSError, FileNotFoundError):
                pass
        self.after(150, _apply_icon)

        self.attributes("-topmost", True)
        self.focus_force()
        self.after(500, lambda: self.attributes("-topmost", False))

        self.on_save_callback = on_save_callback
        self.on_cancel_callback = on_cancel_callback
        self.on_path_saved_callback = on_path_saved_callback
        self._current_path = os.path.abspath(current_path) if current_path else None
        self._library_dir = os.path.abspath(
            library_dir or (os.path.dirname(self._current_path) if self._current_path else "")
        ) if (library_dir or self._current_path) else None
        self._library_paths: list[str] = []
        self._thumb_cache: dict[tuple[str, float], ImageTk.PhotoImage] = {}
        self._thumb_tiles: dict[str, tk.Label] = {}
        self._thumb_drag_path = None
        self._thumb_drag_state = ThumbnailDragState()
        self._drag_ghost = None
        self._drag_ghost_photo = None
        self._drop_feedback_ids: list[int] = []
        self._drop_highlighted = False
        self._pending_merge = PendingMergeState()
        self.configure(fg_color="#15181C")

        # History stack
        self._history: list[np.ndarray] = [image_bgr.copy()]
        self._current_bgr: np.ndarray   = image_bgr.copy()

        # Canvas / display state
        self._scale    = 1.0
        self._off_x    = 0
        self._off_y    = 0
        self._photo    = None

        # Selection state
        self._mode      = self.MODE_CROP
        self._drag_start = None
        self._drag_end   = None
        self._has_sel    = False
        self._overlay_ids: list[int] = []
        self._closed = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._bind_shortcuts()
        self.bind("<Configure>", self._on_resize)

        self._draw_image()
        self.after_idle(self._draw_image)

    def _build_ui(self):
        # Toolbar (top)
        toolbar = ctk.CTkFrame(self, fg_color="#1E2329", corner_radius=0, height=48)
        toolbar.pack(side=tk.TOP, fill=tk.X)
        toolbar.pack_propagate(False)

        b = dict(height=30, corner_radius=8, font=("Segoe UI", 11, "bold"))

        ctk.CTkLabel(toolbar, text="Chế độ:", text_color="#8B9DC3",
                     font=("Segoe UI", 11)).pack(side=tk.LEFT, padx=(14,6), pady=9)

        self._btn_crop = ctk.CTkButton(
            toolbar, text="✂ Crop (Cắt ảnh)", width=130,
            command=lambda: self._set_mode(self.MODE_CROP),
            fg_color="#1677D2", hover_color="#1265B4", **b)
        self._btn_crop.pack(side=tk.LEFT, padx=(0,4), pady=9)

        self._btn_cutout = ctk.CTkButton(
            toolbar, text="⊟ Cut-out (Cắt bỏ dải)", width=170,
            command=lambda: self._set_mode(self.MODE_CUTOUT),
            fg_color="#2A2F37", hover_color="#353B45", **b)
        self._btn_cutout.pack(side=tk.LEFT, padx=(0,4), pady=9)

        ctk.CTkFrame(toolbar, fg_color="#2A2F37", width=1, height=28).pack(
            side=tk.LEFT, padx=10, pady=9)

        # Indicator / Label
        ctk.CTkLabel(
            toolbar, text="✔ Tự động cắt khi thả", text_color="#22C55E",
            font=("Segoe UI", 10, "bold")
        ).pack(side=tk.LEFT, padx=(0,4), pady=9)

        ctk.CTkFrame(toolbar, fg_color="#2A2F37", width=1, height=28).pack(
            side=tk.LEFT, padx=10, pady=9)

        ctk.CTkButton(
            toolbar, text="↩ Hoàn tác", width=110,
            command=self._undo,
            fg_color="#2A2F37", hover_color="#353B45",
            font=("Segoe UI", 11), height=30, corner_radius=8
        ).pack(side=tk.LEFT, padx=(0,4), pady=9)

        ctk.CTkButton(
            toolbar, text="🔄 Đặt lại", width=96,
            command=self._reset,
            fg_color="#2A2F37", hover_color="#353B45",
            font=("Segoe UI", 11), height=30, corner_radius=8
        ).pack(side=tk.LEFT, padx=(0,4), pady=9)

        ctk.CTkButton(
            toolbar, text="Ghép trái", width=90,
            command=lambda: self._choose_and_merge("left"),
            fg_color="#2A2F37", hover_color="#353B45",
            font=("Segoe UI", 11), height=30, corner_radius=8
        ).pack(side=tk.LEFT, padx=(8,4), pady=9)

        ctk.CTkButton(
            toolbar, text="Ghép phải", width=92,
            command=lambda: self._choose_and_merge("right"),
            fg_color="#2A2F37", hover_color="#353B45",
            font=("Segoe UI", 11), height=30, corner_radius=8
        ).pack(side=tk.LEFT, padx=(0,4), pady=9)

        # hint / size (right side)
        self._hint_var = tk.StringVar(value="")
        ctk.CTkLabel(toolbar, textvariable=self._hint_var,
                     text_color="#8B9DC3", font=("Segoe UI", 10)
                     ).pack(side=tk.RIGHT, padx=14, pady=9)

        self._size_var = tk.StringVar(value="")
        ctk.CTkLabel(toolbar, textvariable=self._size_var,
                     text_color="#8B9DC3", font=("Segoe UI", 10)
                     ).pack(side=tk.RIGHT, padx=(0,6), pady=9)

        # Không gian làm việc: thư viện thumbnail dọc bên trái + canvas bên phải.
        self._library_strip = None
        self._pending_bar = None
        workspace = ctk.CTkFrame(self, fg_color="#12151A", corner_radius=0)
        workspace.pack(fill=tk.BOTH, expand=True)

        if self._library_dir:
            gallery = ctk.CTkFrame(
                workspace, fg_color="#191D23", corner_radius=0, width=190
            )
            gallery.pack(side=tk.LEFT, fill=tk.Y)
            gallery.pack_propagate(False)
            ctk.CTkLabel(
                gallery, text="ẢNH CÙNG THƯ MỤC",
                text_color="#8B9DC3", font=("Segoe UI", 10, "bold")
            ).pack(anchor=tk.W, padx=10, pady=(10, 2))
            ctk.CTkLabel(
                gallery, text="Click để mở\nKéo vào canvas để ghép",
                justify=tk.LEFT, text_color="#68768A", font=("Segoe UI", 9)
            ).pack(anchor=tk.W, padx=10, pady=(0, 6))
            self._library_strip = ctk.CTkScrollableFrame(
                gallery, orientation="vertical", width=170, fg_color="transparent"
            )
            self._library_strip.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
            self.after_idle(self._refresh_library_strip)

        outer = ctk.CTkFrame(workspace, fg_color="#12151A", corner_radius=0)
        outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(outer, bg="#12151A",
                                 highlightthickness=0, cursor="crosshair")
        self._canvas.pack(fill=tk.BOTH, expand=True)

        self._canvas.bind("<ButtonPress-1>",   self._on_press)
        self._canvas.bind("<B1-Motion>",        self._on_drag)
        self._canvas.bind("<ButtonRelease-1>",  self._on_release)
        self._canvas.focus_set()

        self._pending_bar = ctk.CTkFrame(self, fg_color="#243247", corner_radius=0)
        self._pending_label = ctk.CTkLabel(
            self._pending_bar, text="", text_color="#E6EAF0", font=("Segoe UI", 10, "bold")
        )
        self._pending_label.pack(side=tk.LEFT, padx=12, pady=6)
        ctk.CTkButton(self._pending_bar, text="Ghép trái", width=90, height=26,
                      command=lambda: self._merge_pending("left")).pack(side=tk.RIGHT, padx=4, pady=4)
        ctk.CTkButton(self._pending_bar, text="Ghép phải", width=90, height=26,
                      command=lambda: self._merge_pending("right")).pack(side=tk.RIGHT, padx=4, pady=4)
        ctk.CTkButton(self._pending_bar, text="Bỏ", width=55, height=26,
                      fg_color="#2A2F37", command=self._clear_pending_merge).pack(
                          side=tk.RIGHT, padx=(4, 12), pady=4)

        # Bottom bar
        bot = ctk.CTkFrame(self, fg_color="transparent")
        bot.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=10)

        ctk.CTkButton(
            bot, text="HỦY", width=100, height=30, corner_radius=8,
            font=("Segoe UI", 11, "bold"),
            command=self._cancel,
            fg_color="#2A2F37", hover_color="#353B45"
        ).pack(side=tk.RIGHT, padx=5)

        ctk.CTkButton(
            bot, text="💾 LƯU  (Ctrl+S)", width=148, height=30, corner_radius=8,
            font=("Segoe UI", 11, "bold"),
            command=self._save,
            fg_color="#22C55E", hover_color="#16A34A"
        ).pack(side=tk.RIGHT, padx=5)

        self._set_mode(self.MODE_CROP)

    def _refresh_library_strip(self):
        if not self._library_strip:
            return
        self._cleanup_thumbnail_drag()
        for child in self._library_strip.winfo_children():
            child.destroy()
        self._thumb_tiles = {}
        self._library_paths = list_library_images(self._library_dir)
        valid_cache = {}
        for path in self._library_paths:
            try:
                mtime = os.path.getmtime(path)
                key = (path, mtime)
                photo = self._thumb_cache.get(key)
                if photo is None:
                    with Image.open(path) as source:
                        thumb = source.convert("RGB")
                        thumb.thumbnail((92, 58), Image.Resampling.LANCZOS)
                        photo = ImageTk.PhotoImage(thumb.copy())
                valid_cache[key] = photo
            except (OSError, ValueError, tk.TclError):
                continue
            selected = self._current_path and os.path.normcase(path) == os.path.normcase(self._current_path)
            tile = tk.Label(
                self._library_strip, image=photo, text=os.path.basename(path), compound=tk.TOP,
                bg="#214F7A" if selected else "#242A31", fg="white", padx=4, pady=3,
                font=("Segoe UI", 8), cursor="hand2"
            )
            tile.pack(fill=tk.X, padx=3, pady=3)
            self._thumb_tiles[path] = tile
            tile.bind("<ButtonPress-1>", lambda e, p=path: self._thumbnail_press(e, p))
            tile.bind("<B1-Motion>", self._thumbnail_drag)
            tile.bind("<ButtonRelease-1>", lambda e, p=path: self._thumbnail_release(e, p))
        self._thumb_cache = valid_cache

    def _canvas_root_bounds(self):
        x, y = self._canvas.winfo_rootx(), self._canvas.winfo_rooty()
        return x, y, x + self._canvas.winfo_width(), y + self._canvas.winfo_height()

    def _thumbnail_press(self, event, path):
        self._cleanup_thumbnail_drag()
        self._thumb_drag_path = path
        self._thumb_drag_state.press(event.x_root, event.y_root)

    def _thumbnail_drag(self, event):
        path = self._thumb_drag_path
        if not path:
            return
        try:
            was_active = self._thumb_drag_state.active
            active = self._thumb_drag_state.update(
                event.x_root, event.y_root, self._canvas_root_bounds()
            )
            if not active:
                return
            if not was_active:
                self._start_drag_visual(path)
            self._move_drag_ghost(event.x_root, event.y_root)
            self._set_drop_highlight(self._thumb_drag_state.over_drop_zone)
        except (OSError, ValueError, tk.TclError):
            self._cleanup_thumbnail_drag()

    def _start_drag_visual(self, path):
        tile = self._thumb_tiles.get(path)
        if tile:
            tile.configure(bg="#0F6B78", relief=tk.SOLID, bd=2)
        with Image.open(path) as source:
            image = source.convert("RGB")
            image.thumbnail((112, 74), Image.Resampling.LANCZOS)
            self._drag_ghost_photo = ImageTk.PhotoImage(image.copy())
        ghost = tk.Toplevel(self)
        ghost.overrideredirect(True)
        try:
            ghost.attributes("-topmost", True)
            ghost.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        frame = tk.Frame(ghost, bg="#38BDF8", padx=2, pady=2)
        frame.pack()
        tk.Label(
            frame, image=self._drag_ghost_photo, text=os.path.basename(path),
            compound=tk.TOP, bg="#17212B", fg="white", padx=6, pady=5,
            font=("Segoe UI", 9, "bold"),
        ).pack()
        self._drag_ghost = ghost
        self.configure(cursor="hand2")

    def _move_drag_ghost(self, x_root, y_root):
        if self._drag_ghost:
            self._drag_ghost.geometry(f"+{x_root + 16}+{y_root + 18}")

    def _set_drop_highlight(self, enabled):
        if enabled == self._drop_highlighted:
            return
        self._clear_drop_highlight()
        self._drop_highlighted = enabled
        if enabled:
            width, height = self._canvas.winfo_width(), self._canvas.winfo_height()
            self._drop_feedback_ids = [
                self._canvas.create_rectangle(
                    5, 5, width - 6, height - 6, outline="#22C55E", width=4,
                    dash=(10, 5), fill="#12351F", stipple="gray75",
                ),
                self._canvas.create_text(
                    width // 2 + 1, 42 + 1, text="Thả để chọn ảnh ghép",
                    fill="#000000", font=("Segoe UI", 14, "bold"),
                ),
                self._canvas.create_text(
                    width // 2, 42, text="Thả để chọn ảnh ghép",
                    fill="#86EFAC", font=("Segoe UI", 14, "bold"),
                ),
            ]

    def _clear_drop_highlight(self):
        for item in self._drop_feedback_ids:
            try:
                self._canvas.delete(item)
            except tk.TclError:
                pass
        self._drop_feedback_ids = []
        self._drop_highlighted = False

    def _cleanup_thumbnail_drag(self):
        self.configure(cursor="")
        self._clear_drop_highlight()
        if self._drag_ghost:
            try:
                self._drag_ghost.destroy()
            except tk.TclError:
                pass
        self._drag_ghost = None
        self._drag_ghost_photo = None
        for path, tile in self._thumb_tiles.items():
            try:
                selected = self._current_path and os.path.normcase(path) == os.path.normcase(self._current_path)
                tile.configure(bg="#214F7A" if selected else "#242A31", relief=tk.FLAT, bd=0)
            except tk.TclError:
                pass
        self._thumb_drag_path = None
        self._thumb_drag_state.reset()

    def _thumbnail_release(self, event, path):
        try:
            result = self._thumb_drag_state.release(
                event.x_root, event.y_root, self._canvas_root_bounds()
            )
            self._cleanup_thumbnail_drag()
            if result == "drop":
                self._set_pending_merge(path)
                self._flash_drop_success()
            elif result == "click":
                self._open_library_image(path)
            elif result == "cancel":
                self._hint_var.set("Đã hủy kéo ảnh — hãy thả vào vùng canvas")
                self.after(1100, lambda: self._set_mode(self._mode) if not self._closed else None)
        except (OSError, ValueError, tk.TclError):
            self._cleanup_thumbnail_drag()

    def _flash_drop_success(self):
        self._canvas.configure(highlightthickness=3, highlightbackground="#22C55E")
        self.after(450, lambda: self._canvas.configure(highlightthickness=0) if not self._closed else None)

    def _has_unsaved_changes(self):
        return len(self._history) > 1

    def _open_library_image(self, path):
        from tkinter import messagebox
        if self._current_path and os.path.normcase(path) == os.path.normcase(self._current_path):
            return
        if self._has_unsaved_changes():
            answer = messagebox.askyesnocancel(
                "Thay đổi chưa lưu",
                "Lưu thay đổi của ảnh hiện tại trước khi chuyển ảnh?",
                parent=self,
            )
            if answer is None:
                return
            if answer and not self._save_current_path():
                return
        image = cv2.imread(path, cv2.IMREAD_COLOR)
        if image is None:
            messagebox.showerror("Không thể mở ảnh", f"Không đọc được ảnh:\n{path}", parent=self)
            return
        self._current_path = os.path.abspath(path)
        self._current_bgr = image
        self._history = [image.copy()]
        self._clear_pending_merge()
        self._draw_image()
        self._refresh_library_strip()

    def _save_current_path(self):
        from tkinter import messagebox
        if not self._current_path:
            return False
        if not cv2.imwrite(self._current_path, self._current_bgr):
            messagebox.showerror("Không thể lưu", f"Không ghi được ảnh:\n{self._current_path}", parent=self)
            return False
        self._history = [self._current_bgr.copy()]
        self._thumb_cache.clear()
        if self.on_path_saved_callback:
            self.on_path_saved_callback(self._current_path, self._current_bgr.copy())
        self._refresh_library_strip()
        return True

    def _set_pending_merge(self, path):
        self._pending_merge.set(path)
        self._pending_label.configure(text=f"Ảnh chờ ghép: {os.path.basename(path)}")
        if not self._pending_bar.winfo_ismapped():
            self._pending_bar.pack(side=tk.BOTTOM, fill=tk.X, before=self._pending_bar.master.winfo_children()[-1])

    def _clear_pending_merge(self):
        self._pending_merge.clear()
        if self._pending_bar and self._pending_bar.winfo_ismapped():
            self._pending_bar.pack_forget()

    def _merge_pending(self, side):
        path = self._pending_merge.path
        if path:
            self._merge_path(path, side)

    def _merge_path(self, path, side):
        from tkinter import messagebox
        added_bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        if added_bgr is None:
            messagebox.showerror("Không thể ghép ảnh", f"Không đọc được ảnh:\n{path}", parent=self)
            return False
        new_bgr = merge_images_side_by_side(self._current_bgr, added_bgr, side)
        self._history.append(new_bgr.copy())
        self._current_bgr = new_bgr
        self._clear_selection()
        self._clear_pending_merge()
        self._draw_image()
        return True

    def _choose_and_merge(self, side: str):
        """Chọn ảnh ngoài thư viện từ đĩa rồi ghép trái/phải."""
        from tkinter import filedialog, messagebox

        path = filedialog.askopenfilename(
            parent=self,
            title="Chọn ảnh để ghép",
            filetypes=[
                ("Ảnh", "*.png *.jpg *.jpeg *.bmp *.webp *.tiff"),
                ("Tất cả", "*.*"),
            ],
        )
        if not path:
            return
        added_bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        if added_bgr is None:
            messagebox.showerror("Không thể ghép ảnh", f"Không đọc được ảnh:\n{path}", parent=self)
            return

        self._merge_path(path, side)

    def _bind_shortcuts(self):
        """Bind on the Toplevel so shortcuts fire while any child has focus."""
        self.bind("<Escape>",     lambda _e: self._clear_sel_or_cancel())
        self.bind("<Control-z>",  lambda _e: self._undo())
        self.bind("<Control-Z>",  lambda _e: self._undo())
        self.bind("<Control-s>",  lambda _e: self._save())
        self.bind("<Control-S>",  lambda _e: self._save())
        self.bind("<Return>",     lambda _e: self._save())
        self.bind("<KP_Enter>",   lambda _e: self._save())

    def _set_mode(self, mode: str):
        self._mode = mode
        self._clear_selection()
        inactive = dict(fg_color="#2A2F37", hover_color="#353B45")
        active   = dict(fg_color="#1677D2", hover_color="#1265B4")
        for btn, m in (
            (self._btn_crop, self.MODE_CROP),
            (self._btn_cutout, self.MODE_CUTOUT),
        ):
            btn.configure(**(active if m == mode else inactive))

        hints = {
            self.MODE_CROP:   "Kéo chọn vùng giữ lại → Thả chuột để Crop  •  Ctrl+S / Enter để Lưu",
            self.MODE_CUTOUT: "Kéo ngang = cắt dải Dọc • Kéo dọc = cắt dải Ngang  •  Ctrl+S / Enter để Lưu",
        }
        self._hint_var.set(hints.get(mode, ""))

    def _draw_image(self, refit=True):
        self.update_idletasks()
        bgr = self._current_bgr
        ih, iw = bgr.shape[:2]

        cw = max(10, self._canvas.winfo_width())
        ch = max(10, self._canvas.winfo_height())

        if refit:
            self._scale = calculate_fit_scale(iw, ih, cw, ch)
        nw = int(iw * self._scale)
        nh = int(ih * self._scale)
        self._off_x = (cw - nw) // 2
        self._off_y = (ch - nh) // 2

        rgb    = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        pil    = Image.fromarray(rgb).resize((nw, nh), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(pil)

        self._canvas.delete("all")
        self._canvas.create_image(self._off_x, self._off_y,
                                   image=self._photo, anchor=tk.NW)
        self._overlay_ids = []
        self._has_sel = False
        self._drag_start = self._drag_end = None
        self._update_size_label()

    def _update_size_label(self):
        h, w = self._current_bgr.shape[:2]
        self._size_var.set(f"{w} × {h} px")

    def _img_bounds_canvas(self):
        ih, iw = self._current_bgr.shape[:2]
        return (self._off_x,
                self._off_y,
                self._off_x + int(iw * self._scale),
                self._off_y + int(ih * self._scale))

    def _clamp_to_image(self, cx, cy):
        x1, y1, x2, y2 = self._img_bounds_canvas()
        return max(x1, min(cx, x2)), max(y1, min(cy, y2))

    def _canvas_to_image_px(self, cx, cy):
        ih, iw = self._current_bgr.shape[:2]
        x = int((cx - self._off_x) / self._scale)
        y = int((cy - self._off_y) / self._scale)
        return max(0, min(x, iw)), max(0, min(y, ih))

    def _image_to_canvas(self, ix, iy):
        return int(ix * self._scale) + self._off_x, int(iy * self._scale) + self._off_y

    def _on_press(self, event):
        self._canvas.focus_set()
        self._clear_selection()
        self._drag_start = self._clamp_to_image(event.x, event.y)
        self._drag_end   = self._drag_start
        self._has_sel    = False

    def _on_drag(self, event):
        if self._drag_start is None:
            return
        self._drag_end = self._clamp_to_image(event.x, event.y)
        self._redraw_overlay()

    def _on_release(self, event):
        if self._drag_start is None:
            return
        self._drag_end = self._clamp_to_image(event.x, event.y)

        sx, sy = self._drag_start
        ex, ey = self._drag_end
        dx, dy = abs(ex - sx), abs(ey - sy)

        valid = False
        if self._mode == self.MODE_CROP and dx > 5 and dy > 5:
            valid = True
        elif self._mode == self.MODE_CUTOUT:
            # For auto cutout, we just need movement in either axis
            if dx > 5 or dy > 5:
                valid = True

        if valid:
            self._has_sel = True
            self._apply()
        else:
            self._clear_selection()

    def _clear_selection(self):
        for item in self._overlay_ids:
            try:
                self._canvas.delete(item)
            except tk.TclError:
                pass
        self._overlay_ids = []
        self._has_sel     = False
        self._drag_start  = self._drag_end = None

    def _redraw_overlay(self):
        for item in self._overlay_ids:
            try:
                self._canvas.delete(item)
            except tk.TclError:
                pass
        self._overlay_ids = []

        if self._drag_start is None or self._drag_end is None:
            return

        sx, sy = self._drag_start
        ex, ey = self._drag_end
        bx1, by1, bx2, by2 = self._img_bounds_canvas()

        ids = self._overlay_ids

        if self._mode == self.MODE_CROP:
            x1, y1 = min(sx, ex), min(sy, ey)
            x2, y2 = max(sx, ex), max(sy, ey)

            # Dim outside
            MASK = "#000000"
            for coords in [
                (bx1, by1, bx2, y1),
                (bx1, y2,  bx2, by2),
                (bx1, y1,  x1,  y2),
                (x2,  y1,  bx2, y2),
            ]:
                if coords[2] > coords[0] and coords[3] > coords[1]:
                    ids.append(self._canvas.create_rectangle(
                        *coords, fill=MASK, outline="",
                        stipple="gray50"))

            # Dashed border
            ids.append(self._canvas.create_rectangle(
                x1, y1, x2, y2,
                outline="#38BDF8", width=2, dash=(8, 4)))

            # Dimensions
            pw = max(1, round((x2-x1)/self._scale))
            ph = max(1, round((y2-y1)/self._scale))
            label = f"{pw} × {ph}"
            lx = (x1+x2)//2
            ly = y2 + 6 if y2 + 20 < by2 else y1 - 14
            ids.append(self._canvas.create_text(lx+1, ly+1, text=label, fill="#000000", font=("Segoe UI", 9, "bold")))
            ids.append(self._canvas.create_text(lx, ly, text=label, fill="#38BDF8", font=("Segoe UI", 9, "bold")))

        elif self._mode == self.MODE_CUTOUT:
            dx, dy = abs(ex - sx), abs(ey - sy)
            if dx > dy:
                # Kéo ngang rộng hơn dọc -> Cut-out Dọc (Xóa dải dọc)
                x1, x2 = min(sx, ex), max(sx, ex)
                ids.append(self._canvas.create_rectangle(
                    x1, by1, x2, by2,
                    fill="#7F1D1D", outline="", stipple="gray50"))
                ids.append(self._canvas.create_rectangle(
                    x1, by1, x2, by2,
                    outline="#EF4444", width=2, dash=(8, 4)))
                pw = max(1, round((x2-x1)/self._scale))
                label = f"✂ Cắt dọc: {pw} px"
                lx = (x1+x2)//2
                ly = (by1+by2)//2
                ids.append(self._canvas.create_text(lx+1, ly+1, text=label, fill="#000000", font=("Segoe UI", 10, "bold")))
                ids.append(self._canvas.create_text(lx, ly, text=label, fill="#FCA5A5", font=("Segoe UI", 10, "bold")))
            else:
                # Kéo dọc cao hơn ngang -> Cut-out Ngang (Xóa dải ngang)
                y1, y2 = min(sy, ey), max(sy, ey)
                ids.append(self._canvas.create_rectangle(
                    bx1, y1, bx2, y2,
                    fill="#7F1D1D", outline="", stipple="gray50"))
                ids.append(self._canvas.create_rectangle(
                    bx1, y1, bx2, y2,
                    outline="#EF4444", width=2, dash=(8, 4)))
                ph = max(1, round((y2-y1)/self._scale))
                label = f"✂ Cắt ngang: {ph} px"
                lx = (bx1+bx2)//2
                ly = (y1+y2)//2
                ids.append(self._canvas.create_text(lx+1, ly+1, text=label, fill="#000000", font=("Segoe UI", 10, "bold")))
                ids.append(self._canvas.create_text(lx, ly, text=label, fill="#FCA5A5", font=("Segoe UI", 10, "bold")))

    def _apply(self):
        if not self._has_sel or self._drag_start is None or self._drag_end is None:
            return

        sx, sy = self._drag_start
        ex, ey = self._drag_end
        ix1, iy1 = self._canvas_to_image_px(min(sx,ex), min(sy,ey))
        ix2, iy2 = self._canvas_to_image_px(max(sx,ex), max(sy,ey))

        bgr = self._current_bgr
        ih, iw = bgr.shape[:2]

        if self._mode == self.MODE_CROP:
            if ix2 <= ix1 or iy2 <= iy1:
                return
            new_bgr = bgr[iy1:iy2, ix1:ix2]

        elif self._mode == self.MODE_CUTOUT:
            dx, dy = abs(ex - sx), abs(ey - sy)
            if dx > dy:
                # Cut-out Dọc
                if ix2 <= ix1:
                    return
                left  = bgr[:, :ix1]
                right = bgr[:, ix2:]
                parts = [p for p in (left, right) if p.shape[1] > 0]
                if not parts:
                    return
                new_bgr = np.hstack(parts) if len(parts) == 2 else parts[0]
            else:
                # Cut-out Ngang
                if iy2 <= iy1:
                    return
                top    = bgr[:iy1, :]
                bottom = bgr[iy2:, :]
                parts  = [p for p in (top, bottom) if p.shape[0] > 0]
                if not parts:
                    return
                new_bgr = np.vstack(parts) if len(parts) == 2 else parts[0]
        else:
            return

        if new_bgr.size == 0:
            return

        self._history.append(new_bgr.copy())
        self._current_bgr = new_bgr.copy()
        self._draw_image(refit=False)

    def _undo(self):
        if len(self._history) > 1:
            self._history.pop()
            self._current_bgr = self._history[-1].copy()
            self._draw_image()

    def _reset(self):
        if len(self._history) > 1:
            self._current_bgr = self._history[0].copy()
            self._history = [self._current_bgr.copy()]
            self._draw_image()

    def _clear_sel_or_cancel(self):
        if self._thumb_drag_state.start is not None or self._drag_ghost:
            self._cleanup_thumbnail_drag()
        elif self._has_sel:
            self._clear_selection()
            self._redraw_overlay()
        else:
            self._cancel()

    def _on_resize(self, event):
        if str(event.widget) == str(self):
            self._draw_image()

    def _save(self):
        if self._closed:
            return
        self._cleanup_thumbnail_drag()
        # Ảnh duyệt từ dải thư viện được ghi đúng vào path đang chọn. Luồng cũ
        # (không truyền current_path) vẫn trả ndarray qua callback như trước.
        if self._current_path:
            if not self._save_current_path():
                return
        elif self.on_save_callback:
            self.on_save_callback(self._current_bgr.copy())
        self._closed = True
        self.destroy()

    def _cancel(self):
        if self._closed:
            return
        self._cleanup_thumbnail_drag()
        self._closed = True
        if hasattr(self, "on_cancel_callback") and self.on_cancel_callback:
            self.on_cancel_callback()
        self.destroy()
