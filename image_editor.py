import tkinter as tk
import customtkinter as ctk
from PIL import Image, ImageTk
import cv2
import numpy as np
import os
from config import RESOURCE_DIR


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
                 on_save_callback=None, on_cancel_callback=None, title="Chỉnh sửa ảnh"):
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

        # hint / size (right side)
        self._hint_var = tk.StringVar(value="")
        ctk.CTkLabel(toolbar, textvariable=self._hint_var,
                     text_color="#8B9DC3", font=("Segoe UI", 10)
                     ).pack(side=tk.RIGHT, padx=14, pady=9)

        self._size_var = tk.StringVar(value="")
        ctk.CTkLabel(toolbar, textvariable=self._size_var,
                     text_color="#8B9DC3", font=("Segoe UI", 10)
                     ).pack(side=tk.RIGHT, padx=(0,6), pady=9)

        # Canvas
        outer = ctk.CTkFrame(self, fg_color="#12151A", corner_radius=0)
        outer.pack(fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(outer, bg="#12151A",
                                 highlightthickness=0, cursor="crosshair")
        self._canvas.pack(fill=tk.BOTH, expand=True)

        self._canvas.bind("<ButtonPress-1>",   self._on_press)
        self._canvas.bind("<B1-Motion>",        self._on_drag)
        self._canvas.bind("<ButtonRelease-1>",  self._on_release)
        self._canvas.focus_set()

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
            self._scale = min(cw / iw, ch / ih)
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
        if self._has_sel:
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
        self._closed = True
        if self.on_save_callback:
            self.on_save_callback(self._current_bgr.copy())
        self.destroy()

    def _cancel(self):
        if self._closed:
            return
        self._closed = True
        if hasattr(self, "on_cancel_callback") and self.on_cancel_callback:
            self.on_cancel_callback()
        self.destroy()
