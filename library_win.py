"""Library window for browsing, copying and deleting saved result images."""

from __future__ import annotations

import os
import json
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

from auto_marker import (
    copy_image_to_clipboard,
    read_image_file,
    draw_match_boxes,
    toggle_box_at_point,
    load_metadata,
    update_metadata,
)
import cv2

VALID_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff")


def send_to_recycle_bin(path):
    """Move a file to the Windows Recycle Bin via the Shell API.

    Uses SHFileOperationW with FOF_ALLOWUNDO so the file can be restored,
    and silent flags so no OS confirmation dialog appears.
    """
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 0x0003
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_SILENT = 0x0004
    FOF_NOERRORUI = 0x0400

    # pFrom must be double-null terminated.
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0\0"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0:
        raise OSError(f"SHFileOperation trả về mã lỗi {result}")


def collect_saved_images(output_dir):
    """Return saved result images under output_dir, newest first.

    Skips original_*.png sidecar files (used internally for box editing).
    """
    entries = []
    if not os.path.isdir(output_dir):
        return entries
    for root_dir, _dirs, files in os.walk(output_dir):
        for name in files:
            if not name.lower().endswith(VALID_EXTS):
                continue
            if name.lower().startswith("original_"):
                continue  # sidecar original image, not user-facing
            full = os.path.join(root_dir, name)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                mtime = 0.0
            rel = os.path.relpath(full, output_dir)
            entries.append({"path": full, "rel": rel, "mtime": mtime})
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    return entries


class LibraryWindow(ctk.CTkToplevel):
    def __init__(self, master, output_dir, on_close=None, matcher=None):
        super().__init__(master)
        self.title("Thư viện ảnh đã lưu")
        self.geometry("1280x820")
        self.minsize(1000, 680)
        self.output_dir = output_dir
        self.on_close_callback = on_close
        self.matcher = matcher
        self.items = collect_saved_images(output_dir)
        self.current_index = 0
        self.photo = None
        self._library_icon = None

        # Edit state — loaded from JSON sidecar when available
        self._edit_original_bgr = None   # clean image for toggle logic
        self._edit_matches = None        # mutable matches list
        self._edit_dirty = False         # True when matches were modified
        self._display_scale = 1.0       # canvas-to-image scale factor

        os.makedirs(output_dir, exist_ok=True)
        self._setup_icon()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Control-c>", self.copy_current)
        self.bind("<Control-C>", self.copy_current)
        self.bind("<Delete>", lambda _event: self.delete_current())
        self.bind("<Left>", lambda _event: self.previous())
        self.bind("<Right>", lambda _event: self.next())
        self.bind("<Configure>", lambda _event: self._draw_current())
        self.after(100, self._show_current)

    def _setup_icon(self):
        assets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

        def apply_icon():
            try:
                self._library_icon = tk.PhotoImage(file=os.path.join(assets, "app_icon.png"))
                self.iconphoto(False, self._library_icon)
                if os.name == "nt":
                    self.iconbitmap(os.path.join(assets, "app_icon.ico"))
            except (tk.TclError, OSError, FileNotFoundError) as exc:
                print(f"Không thể nạp icon Thư viện: {exc}")

        self.after(150, apply_icon)

    def _build_ui(self):
        self.configure(fg_color="#15181C")
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill=tk.X, padx=16, pady=(16, 8))
        ctk.CTkLabel(
            header,
            text="THƯ VIỆN ẢNH ĐÃ LƯU",
            font=("Segoe UI", 16, "bold"),
            text_color="#3B82F6",
        ).pack(side=tk.LEFT)
        self.summary_label = ctk.CTkLabel(header, text="", font=("Segoe UI", 11))
        self.summary_label.pack(side=tk.RIGHT)

        body = ctk.CTkFrame(self, fg_color="#1E2228", corner_radius=12, border_width=1, border_color="#2A2F37")
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

        sidebar = ctk.CTkFrame(body, width=275, fg_color="#1E2228", corner_radius=12)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8), pady=8)
        sidebar.pack_propagate(False)
        ctk.CTkLabel(
            sidebar, text="Ảnh đã lưu", font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, padx=10, pady=(10, 5))
        self.listbox = tk.Listbox(
            sidebar,
            bg="#12151A",
            fg="#E6EAF0",
            selectbackground="#3B82F6",
            selectforeground="white",
            borderwidth=0,
            highlightthickness=0,
            font=("Segoe UI", 10),
        )
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        viewer = ctk.CTkFrame(body, fg_color="#12151A", corner_radius=12, border_width=1, border_color="#2A2F37")
        viewer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8), pady=8)
        self.canvas = tk.Canvas(viewer, bg="#12151A", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_click)

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill=tk.X, padx=16, pady=(6, 16))
        ctk.CTkButton(controls, text="← Trước", width=90, height=30, corner_radius=8, fg_color="#2A2F37", hover_color="#353B45", font=("Segoe UI", 11, "bold"), command=self.previous).pack(
            side=tk.LEFT, padx=4
        )
        ctk.CTkButton(controls, text="Tiếp →", width=90, height=30, corner_radius=8, fg_color="#2A2F37", hover_color="#353B45", font=("Segoe UI", 11, "bold"), command=self.next).pack(
            side=tk.LEFT, padx=4
        )
        self.current_label = ctk.CTkLabel(controls, text="")
        self.current_label.pack(side=tk.LEFT, padx=12)
        ctk.CTkButton(
            controls,
            text="ĐÓNG",
            width=90,
            fg_color="#2A2F37",
            command=self.close,
        ).pack(side=tk.RIGHT, padx=4)
        ctk.CTkButton(
            controls,
            text="🔄 LÀM MỚI",
            width=110,
            fg_color="#2A2F37",
            hover_color="#353B45",
            command=self.refresh,
        ).pack(side=tk.RIGHT, padx=4)
        ctk.CTkButton(
            controls,
            text="XÓA ẢNH (Del)",
            width=130,
            fg_color="#EF4444",
            hover_color="#DC2626",
            command=self.delete_current,
        ).pack(side=tk.RIGHT, padx=4)
        ctk.CTkButton(
            controls,
            text="COPY ẢNH (Ctrl+C)",
            width=145,
            fg_color="#3B82F6",
            hover_color="#2563EB",
            command=self.copy_current,
        ).pack(side=tk.RIGHT, padx=4)

        self._refresh_list()

    def _refresh_list(self):
        self.listbox.delete(0, tk.END)
        for item in self.items:
            self.listbox.insert(tk.END, f"  {item['rel']}")
        self.summary_label.configure(text=f"{len(self.items)} ảnh")

    def refresh(self):
        current_path = (
            self.items[self.current_index]["path"]
            if self.items and 0 <= self.current_index < len(self.items)
            else None
        )
        self.items = collect_saved_images(self.output_dir)
        self._refresh_list()
        if current_path:
            for index, item in enumerate(self.items):
                if item["path"] == current_path:
                    self.current_index = index
                    break
            else:
                self.current_index = 0
        else:
            self.current_index = 0
        self._show_current()

    def _on_select(self, _event=None):
        selection = self.listbox.curselection()
        if selection:
            self.current_index = selection[0]
            self._show_current()

    def _show_current(self):
        # Save any pending edits before switching to a different image
        self._flush_edits()

        if not self.items:
            self.current_label.configure(text="Chưa có ảnh nào trong thư viện.")
            self.canvas.delete("all")
            self.photo = None
            self._edit_original_bgr = None
            self._edit_matches = None
            self._edit_dirty = False
            return
        self.current_index = max(0, min(self.current_index, len(self.items) - 1))
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.current_index)
        self.listbox.see(self.current_index)
        item = self.items[self.current_index]

        # Try to load metadata for edit capability
        meta = load_metadata(item["path"])
        if meta:
            self._edit_original_bgr = read_image_file(meta["original_path"])
            self._edit_matches = meta["matches"]
            self._edit_dirty = False
        else:
            self._edit_original_bgr = None
            self._edit_matches = None
            self._edit_dirty = False

        editable = self._edit_original_bgr is not None
        edit_hint = " · Click thêm/xóa khung" if editable else ""
        self.current_label.configure(
            text=(
                f"{self.current_index + 1}/{len(self.items)} · "
                f"{item['rel']} · ←/→ chuyển ảnh{edit_hint}"
            )
        )
        self._draw_current()

    def _draw_current(self):
        if not self.items or not self.canvas.winfo_exists():
            return
        item = self.items[self.current_index]

        # If editable, re-draw from original + matches (reflects toggled boxes)
        if self._edit_original_bgr is not None and self._edit_matches is not None:
            image = draw_match_boxes(self._edit_original_bgr.copy(), self._edit_matches)
        else:
            image = read_image_file(item["path"])

        if image is None:
            self.canvas.delete("all")
            self.photo = None
            self.current_label.configure(text=f"Không đọc được ảnh: {item['rel']}")
            return
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(rgb)
        self.update_idletasks()
        canvas_w = max(10, self.canvas.winfo_width())
        canvas_h = max(10, self.canvas.winfo_height())
        self._display_scale = min(canvas_w / pil_image.width, canvas_h / pil_image.height)
        new_size = (
            max(1, int(pil_image.width * self._display_scale)),
            max(1, int(pil_image.height * self._display_scale)),
        )
        pil_image = pil_image.resize(new_size, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(pil_image)
        self.canvas.delete("all")
        self.canvas.create_image(
            canvas_w // 2, canvas_h // 2, image=self.photo, anchor=tk.CENTER
        )

    def previous(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._show_current()

    def next(self):
        if self.current_index < len(self.items) - 1:
            self.current_index += 1
            self._show_current()

    def copy_current(self, _event=None):
        if not self.items:
            return "break"
        item = self.items[self.current_index]

        # Use live edited version if available
        if self._edit_original_bgr is not None and self._edit_matches is not None:
            image = draw_match_boxes(self._edit_original_bgr.copy(), self._edit_matches)
        else:
            image = read_image_file(item["path"])

        if image is None:
            messagebox.showerror("Lỗi", f"Không đọc được ảnh: {item['rel']}")
            return "break"
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        if self.matcher is not None:
            self.matcher.ignore_next_clipboard = True
        copy_image_to_clipboard(Image.fromarray(rgb))
        self.current_label.configure(
            text=f"Đã copy ảnh {self.current_index + 1}/{len(self.items)} — có thể dán vào Excel"
        )
        try:
            self.bell()
        except (tk.TclError, RuntimeError):
            pass
        return "break"

    def delete_current(self):
        if not self.items:
            return
        self._edit_dirty = False  # Don't flush — we're deleting
        item = self.items[self.current_index]

        # Determine sidecar file paths before deletion
        base, _ = os.path.splitext(item["path"])
        sidecar_json = base + ".json"
        sidecar_original = None
        if os.path.isfile(sidecar_json):
            try:
                with open(sidecar_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                orig_file = data.get("original_file", "")
                if orig_file:
                    sidecar_original = os.path.join(
                        os.path.dirname(item["path"]), orig_file
                    )
            except Exception:
                pass

        try:
            send_to_recycle_bin(item["path"])
            if os.path.isfile(sidecar_json):
                send_to_recycle_bin(sidecar_json)
            if sidecar_original and os.path.isfile(sidecar_original):
                send_to_recycle_bin(sidecar_original)
        except OSError as exc:
            messagebox.showerror("Lỗi", f"Không thể xóa: {exc}")
            return

        self._edit_original_bgr = None
        self._edit_matches = None

        del self.items[self.current_index]
        self._refresh_list()
        if self.current_index >= len(self.items):
            self.current_index = len(self.items) - 1
        self._show_current()

    def close(self):
        self._flush_edits()
        if self.on_close_callback:
            self.on_close_callback()
        self.destroy()

    # ------------------------------------------------------------------
    # Click-to-toggle box editing (requires JSON sidecar + original image)
    # ------------------------------------------------------------------

    def _on_canvas_click(self, event):
        """Toggle a box at the click location (if metadata is available)."""
        if self._edit_original_bgr is None or self._edit_matches is None:
            return  # not editable (legacy image without sidecar)
        if not self.photo:
            return

        # Translate canvas pixel → real image pixel
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        photo_w = self.photo.width()
        photo_h = self.photo.height()

        offset_x = (canvas_w - photo_w) // 2
        offset_y = (canvas_h - photo_h) // 2

        x_in_photo = event.x - offset_x
        y_in_photo = event.y - offset_y

        if not (0 <= x_in_photo < photo_w and 0 <= y_in_photo < photo_h):
            return

        real_x = x_in_photo / self._display_scale
        real_y = y_in_photo / self._display_scale

        default_query = (
            self._edit_matches[0]["query"] if self._edit_matches else "Query_Mac_Dinh"
        )
        if toggle_box_at_point(
            self._edit_original_bgr, self._edit_matches, real_x, real_y, default_query
        ):
            self._edit_dirty = True
            self._draw_current()

    def _flush_edits(self):
        """Re-save the marked image and update JSON when boxes were toggled."""
        if not self._edit_dirty:
            return
        if self._edit_matches is None or self._edit_original_bgr is None:
            return
        if not self.items or not (0 <= self.current_index < len(self.items)):
            return

        item = self.items[self.current_index]
        filepath = item["path"]

        # Re-draw marked image from clean original + current matches
        marked_bgr = draw_match_boxes(self._edit_original_bgr.copy(), self._edit_matches)

        # Overwrite the marked PNG
        cv2.imwrite(filepath, marked_bgr)

        # Update the JSON sidecar
        update_metadata(filepath, self._edit_matches)

        self._edit_dirty = False
