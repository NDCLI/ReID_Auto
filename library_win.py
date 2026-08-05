"""Library window for browsing, copying and deleting saved result images."""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

from auto_marker import copy_image_to_clipboard, read_image_file
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

    Single previews save directly into output_dir; batch runs save into
    per-query subfolders. Both are gathered here.
    """
    entries = []
    if not os.path.isdir(output_dir):
        return entries
    for root_dir, _dirs, files in os.walk(output_dir):
        for name in files:
            if name.lower().endswith(VALID_EXTS):
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
    def __init__(self, master, output_dir, on_close=None):
        super().__init__(master)
        self.title("Thư viện ảnh đã lưu")
        self.geometry("1280x820")
        self.minsize(1000, 680)
        self.output_dir = output_dir
        self.on_close_callback = on_close
        self.items = collect_saved_images(output_dir)
        self.current_index = 0
        self.photo = None
        self._library_icon = None

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
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill=tk.X, padx=14, pady=(10, 6))
        ctk.CTkLabel(
            header,
            text="THƯ VIỆN ẢNH ĐÃ LƯU",
            font=("Segoe UI", 16, "bold"),
            text_color="#3498DB",
        ).pack(side=tk.LEFT)
        self.summary_label = ctk.CTkLabel(header, text="", font=("Segoe UI", 11))
        self.summary_label.pack(side=tk.RIGHT)

        body = ctk.CTkFrame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=14, pady=6)

        sidebar = ctk.CTkFrame(body, width=275)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8), pady=8)
        sidebar.pack_propagate(False)
        ctk.CTkLabel(
            sidebar, text="Ảnh đã lưu", font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, padx=10, pady=(10, 5))
        self.listbox = tk.Listbox(
            sidebar,
            bg="#182126",
            fg="#ECF0F1",
            selectbackground="#2471A3",
            selectforeground="white",
            borderwidth=0,
            highlightthickness=0,
            font=("Segoe UI", 10),
        )
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        viewer = ctk.CTkFrame(body, fg_color="#1E272C")
        viewer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8), pady=8)
        self.canvas = tk.Canvas(viewer, bg="#1E272C", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill=tk.X, padx=14, pady=(4, 12))
        ctk.CTkButton(controls, text="← Trước", width=90, command=self.previous).pack(
            side=tk.LEFT, padx=4
        )
        ctk.CTkButton(controls, text="Tiếp →", width=90, command=self.next).pack(
            side=tk.LEFT, padx=4
        )
        self.current_label = ctk.CTkLabel(controls, text="")
        self.current_label.pack(side=tk.LEFT, padx=12)
        ctk.CTkButton(
            controls,
            text="ĐÓNG",
            width=90,
            fg_color="#7F8C8D",
            command=self.close,
        ).pack(side=tk.RIGHT, padx=4)
        ctk.CTkButton(
            controls,
            text="🔄 LÀM MỚI",
            width=110,
            fg_color="#34495E",
            hover_color="#2C3E50",
            command=self.refresh,
        ).pack(side=tk.RIGHT, padx=4)
        ctk.CTkButton(
            controls,
            text="XÓA ẢNH (Del)",
            width=130,
            fg_color="#E74C3C",
            hover_color="#C0392B",
            command=self.delete_current,
        ).pack(side=tk.RIGHT, padx=4)
        ctk.CTkButton(
            controls,
            text="COPY ẢNH (Ctrl+C)",
            width=145,
            fg_color="#2471A3",
            hover_color="#1F618D",
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
        if not self.items:
            self.current_label.configure(text="Chưa có ảnh nào trong thư viện.")
            self.canvas.delete("all")
            self.photo = None
            return
        self.current_index = max(0, min(self.current_index, len(self.items) - 1))
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.current_index)
        self.listbox.see(self.current_index)
        item = self.items[self.current_index]
        self.current_label.configure(
            text=(
                f"{self.current_index + 1}/{len(self.items)} · "
                f"{item['rel']} · ←/→ chuyển ảnh"
            )
        )
        self._draw_current()

    def _draw_current(self):
        if not self.items or not self.canvas.winfo_exists():
            return
        item = self.items[self.current_index]
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
        scale = min(canvas_w / pil_image.width, canvas_h / pil_image.height)
        new_size = (
            max(1, int(pil_image.width * scale)),
            max(1, int(pil_image.height * scale)),
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
        image = read_image_file(item["path"])
        if image is None:
            messagebox.showerror("Lỗi", f"Không đọc được ảnh: {item['rel']}")
            return "break"
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
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
        item = self.items[self.current_index]
        try:
            send_to_recycle_bin(item["path"])
        except OSError as exc:
            messagebox.showerror("Lỗi", f"Không thể xóa: {exc}")
            return
        del self.items[self.current_index]
        self._refresh_list()
        if self.current_index >= len(self.items):
            self.current_index = len(self.items) - 1
        self._show_current()

    def close(self):
        if self.on_close_callback:
            self.on_close_callback()
        self.destroy()
