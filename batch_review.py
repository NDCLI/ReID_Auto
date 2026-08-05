"""Review library for batch-processed screenshots."""

from __future__ import annotations

import os
import tkinter as tk

import cv2
import customtkinter as ctk
from PIL import Image, ImageTk

from auto_marker import copy_image_to_clipboard, draw_match_boxes, write_image_file


def classify_item_query(matches):
    """Return the dominant Query in a screenshot from its accepted matches."""
    if not matches:
        return "Chua_xac_dinh"

    query_stats = {}
    for match in matches:
        query = match.get("query") or "Chua_xac_dinh"
        count, total_score = query_stats.get(query, (0, 0.0))
        query_stats[query] = (count + 1, total_score + float(match.get("score", 0.0)))

    return max(query_stats, key=lambda query: query_stats[query])


class BatchReviewWindow(ctk.CTkToplevel):
    def __init__(self, master, items, output_dir, on_close=None):
        super().__init__(master)
        self.title("Thư viện duyệt kết quả Batch")
        self.geometry("1280x820")
        self.minsize(1000, 680)
        for item in items:
            item["query"] = classify_item_query(item.get("matches", []))
        self.items = sorted(
            items,
            key=lambda item: (item["query"], os.path.basename(item["path"]).lower()),
        )
        self.output_dir = output_dir
        self.on_close_callback = on_close
        self.current_index = 0
        self.photo = None
        self.scale_factor = 1.0
        self.start_x = None
        self.start_y = None
        self.current_rect = None
        self.is_drawing = False
        self._review_icon = None

        os.makedirs(output_dir, exist_ok=True)
        self._setup_icon()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Control-c>", self.copy_current)
        self.bind("<Control-C>", self.copy_current)
        self.bind("<Left>", lambda _event: self.previous())
        self.bind("<Right>", lambda _event: self.next())
        self.after(100, self._show_current)

    def _setup_icon(self):
        assets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

        def apply_icon():
            try:
                self._review_icon = tk.PhotoImage(file=os.path.join(assets, "app_icon.png"))
                self.iconphoto(False, self._review_icon)
                if os.name == "nt":
                    self.iconbitmap(os.path.join(assets, "app_icon.ico"))
            except (tk.TclError, OSError, FileNotFoundError) as exc:
                print(f"Không thể nạp icon Batch Review: {exc}")

        self.after(150, apply_icon)

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill=tk.X, padx=14, pady=(10, 6))
        ctk.CTkLabel(
            header,
            text="THƯ VIỆN DUYỆT KẾT QUẢ BATCH",
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
            sidebar, text="Ảnh đã xử lý", font=("Segoe UI", 12, "bold")
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
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.bind("<Configure>", lambda _event: self._draw_current())

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
            text="LƯU ẢNH ĐANG DUYỆT",
            width=165,
            fg_color="#16A085",
            hover_color="#117A65",
            command=self.save_current,
        ).pack(side=tk.RIGHT, padx=4)
        ctk.CTkButton(
            controls,
            text="COPY ẢNH (Ctrl+C)",
            width=145,
            fg_color="#2471A3",
            hover_color="#1F618D",
            command=self.copy_current,
        ).pack(side=tk.RIGHT, padx=4)
        ctk.CTkButton(
            controls,
            text="LƯU TẤT CẢ",
            width=120,
            fg_color="#2ECC71",
            hover_color="#27AE60",
            command=self.save_all,
        ).pack(side=tk.RIGHT, padx=4)
        ctk.CTkButton(
            controls,
            text="ĐÓNG",
            width=90,
            fg_color="#7F8C8D",
            command=self.close,
        ).pack(side=tk.RIGHT, padx=4)

        for _ in self.items:
            self.listbox.insert(tk.END, "")
        self._refresh_list()

    def _refresh_list(self):
        saved = 0
        for index, item in enumerate(self.items):
            status = "✓" if item.get("saved") else "○"
            saved += int(bool(item.get("saved")))
            label = (
                f"{status} {index + 1:02d}. [{item['query']}] "
                f"{os.path.basename(item['path'])}  [{len(item['matches'])}]"
            )
            self.listbox.delete(index)
            self.listbox.insert(index, label)
        self.summary_label.configure(text=f"Đã lưu {saved}/{len(self.items)}")

    def _on_select(self, _event=None):
        selection = self.listbox.curselection()
        if selection:
            self.current_index = selection[0]
            self._show_current()

    def _show_current(self):
        if not self.items:
            return
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.current_index)
        self.listbox.see(self.current_index)
        item = self.items[self.current_index]
        item["query"] = classify_item_query(item["matches"])
        self.current_label.configure(
            text=(
                f"{self.current_index + 1}/{len(self.items)} · "
                f"{item['query']} · "
                f"{len(item['matches'])} khung · ←/→ chuyển ảnh · "
                "Click khung để xóa, kéo để thêm"
            )
        )
        self._draw_current()

    def _draw_current(self):
        if not self.items or not self.canvas.winfo_exists():
            return
        item = self.items[self.current_index]
        marked = draw_match_boxes(item["image"].copy(), item["matches"])
        rgb = cv2.cvtColor(marked, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(rgb)
        self.update_idletasks()
        canvas_w = max(10, self.canvas.winfo_width())
        canvas_h = max(10, self.canvas.winfo_height())
        self.scale_factor = min(canvas_w / pil_image.width, canvas_h / pil_image.height)
        new_size = (
            max(1, int(pil_image.width * self.scale_factor)),
            max(1, int(pil_image.height * self.scale_factor)),
        )
        pil_image = pil_image.resize(new_size, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(pil_image)
        self.canvas.delete("all")
        self.canvas.create_image(canvas_w // 2, canvas_h // 2, image=self.photo, anchor=tk.CENTER)

    def _real_coords(self, x, y):
        if not self.photo:
            return None, None
        offset_x = (self.canvas.winfo_width() - self.photo.width()) // 2
        offset_y = (self.canvas.winfo_height() - self.photo.height()) // 2
        px, py = x - offset_x, y - offset_y
        if 0 <= px <= self.photo.width() and 0 <= py <= self.photo.height():
            return px / self.scale_factor, py / self.scale_factor
        return None, None

    def _on_canvas_press(self, event):
        real_x, real_y = self._real_coords(event.x, event.y)
        if real_x is None:
            return
        matches = self.items[self.current_index]["matches"]
        for match in list(matches):
            x1, y1, x2, y2 = match["bbox"]
            if x1 <= real_x <= x2 and y1 <= real_y <= y2:
                matches.remove(match)
                self.items[self.current_index]["query"] = classify_item_query(matches)
                self.items[self.current_index]["saved"] = False
                self._refresh_list()
                self._draw_current()
                return
        self.is_drawing = True
        self.start_x, self.start_y = event.x, event.y
        self.current_rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="red", width=2
        )

    def _on_canvas_drag(self, event):
        if self.is_drawing and self.current_rect:
            self.canvas.coords(self.current_rect, self.start_x, self.start_y, event.x, event.y)

    def _on_canvas_release(self, event):
        if not self.is_drawing:
            return
        self.is_drawing = False
        if self.current_rect:
            self.canvas.delete(self.current_rect)
            self.current_rect = None
        x1, y1 = self._real_coords(self.start_x, self.start_y)
        x2, y2 = self._real_coords(event.x, event.y)
        if None in (x1, y1, x2, y2) or abs(x2 - x1) <= 10 or abs(y2 - y1) <= 10:
            return
        matches = self.items[self.current_index]["matches"]
        query = matches[0]["query"] if matches else "Query_Mac_Dinh"
        matches.append({
            "bbox": (int(min(x1, x2)), int(min(y1, y2)), int(max(x1, x2)), int(max(y1, y2))),
            "score": 1.0,
            "query": query,
        })
        self.items[self.current_index]["query"] = classify_item_query(matches)
        self.items[self.current_index]["saved"] = False
        self._refresh_list()
        self._draw_current()

    def previous(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._show_current()

    def next(self):
        if self.current_index < len(self.items) - 1:
            self.current_index += 1
            self._show_current()

    def _save_item(self, index):
        item = self.items[index]
        marked = draw_match_boxes(item["image"].copy(), item["matches"])
        stem = os.path.splitext(os.path.basename(item["path"]))[0]
        item["query"] = classify_item_query(item["matches"])
        query_output_dir = os.path.join(self.output_dir, item["query"])
        os.makedirs(query_output_dir, exist_ok=True)
        output_path = os.path.join(
            query_output_dir, f"{index + 1:03d}_{stem}_marked.png"
        )
        if not write_image_file(output_path, marked):
            raise IOError(f"Không thể lưu: {output_path}")
        item["saved"] = True
        item["output_path"] = output_path

    def save_current(self):
        self._save_item(self.current_index)
        self._refresh_list()
        if self.current_index < len(self.items) - 1:
            self.current_index += 1
            self._show_current()

    def copy_current(self, _event=None):
        if not self.items:
            return "break"
        item = self.items[self.current_index]
        marked = draw_match_boxes(item["image"].copy(), item["matches"])
        rgb = cv2.cvtColor(marked, cv2.COLOR_BGR2RGB)
        copy_image_to_clipboard(Image.fromarray(rgb))
        self.current_label.configure(
            text=f"Đã copy ảnh {self.current_index + 1}/{len(self.items)} — có thể dán vào Excel"
        )
        try:
            self.bell()
        except (tk.TclError, RuntimeError):
            pass
        return "break"

    def save_all(self):
        for index in range(len(self.items)):
            self._save_item(index)
        self._refresh_list()
        self.current_label.configure(text=f"Đã lưu toàn bộ vào: {self.output_dir}")

    def close(self):
        if self.on_close_callback:
            self.on_close_callback()
        self.destroy()
