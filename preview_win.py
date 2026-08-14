import os
import tkinter as tk
import customtkinter as ctk
from PIL import Image, ImageTk
import cv2
import time
from auto_marker import (
    draw_match_boxes,
    save_result_with_metadata,
    get_dominant_query_name,
    copy_image_to_clipboard,
    notify_sound,
    toggle_box_at_point,
)

class PreviewWindow(ctk.CTkToplevel):
    def __init__(
        self,
        master,
        current_bgr,
        matches,
        matcher,
        output_dir,
        on_close_callback=None,
    ):
        super().__init__(master)
        self.title("Duyệt Ảnh - Chỉnh sửa kết quả")

        # CTkToplevel may not inherit the root icon on Windows, so apply it
        # directly after the native window handle has been created.
        assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        self._review_icon = None

        def apply_review_icon():
            try:
                self._review_icon = tk.PhotoImage(
                    file=os.path.join(assets_dir, "app_icon.png")
                )
                self.iconphoto(False, self._review_icon)
                if os.name == "nt":
                    self.iconbitmap(os.path.join(assets_dir, "app_icon.ico"))
            except (tk.TclError, OSError, FileNotFoundError) as exc:
                print(f"Không thể nạp icon cửa sổ Review: {exc}")

        self.after(150, apply_review_icon)
        
        # Set geometry to 1024x768 and center on screen
        w = 1024
        h = 768
        ws = self.winfo_screenwidth()
        hs = self.winfo_screenheight()
        x = (ws - w) // 2
        y = (hs - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        
        # Bring window to front
        self.attributes("-topmost", True)
        self.focus_force()
        # After 500ms, remove topmost so it doesn't block other windows permanently
        self.after(500, lambda: self.attributes("-topmost", False))
        
        self.current_bgr = current_bgr
        self.matches = matches.copy()
        self.matcher = matcher
        self.output_dir = output_dir
        self.on_close_callback = on_close_callback
        self._closed = False
        
        self.configure(fg_color="#15181C")
        self.protocol("WM_DELETE_WINDOW", self.close)

        # Bottom Frame for Save/Cancel
        bot_frame = ctk.CTkFrame(self, fg_color="transparent")
        bot_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=12)
        
        btn_cancel = ctk.CTkButton(
            bot_frame, 
            text="HỦY", 
            width=100,
            height=30,
            corner_radius=8,
            font=("Segoe UI", 11, "bold"),
            command=self.close,
            fg_color="#2A2F37",
            hover_color="#353B45"
        )
        btn_cancel.pack(side=tk.RIGHT, padx=5)
        
        btn_save = ctk.CTkButton(
            bot_frame, 
            text="LƯU & COPY", 
            width=130,
            height=30,
            corner_radius=8,
            command=self.save_and_copy,
            fg_color="#22C55E",
            hover_color="#16A34A",
            font=("Segoe UI", 11, "bold")
        )
        btn_save.pack(side=tk.RIGHT, padx=5)
        
        # Canvas Frame (with dark background and nice styling)
        self.canvas_frame = ctk.CTkFrame(self, fg_color="#12151A", corner_radius=12, border_width=1, border_color="#2A2F37")
        self.canvas_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=6)
        
        self.canvas = tk.Canvas(self.canvas_frame, bg="#12151A", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.canvas.bind("<ButtonPress-1>", self.on_canvas_press)
        
        # Right click to Save & Copy
        self.canvas.bind("<Button-3>", lambda e: self.save_and_copy())
        self.bind("<Button-3>", lambda e: self.save_and_copy())
        
        self.bind("<Configure>", self.on_resize)
        self.bind("<Escape>", lambda e: self.close())
        
        self.photo = None
        self.scale_factor = 1.0

        self.draw_image()

    def on_resize(self, event):
        if str(event.widget) == str(self):
            self.draw_image()

    def draw_image(self):
        marked_bgr = draw_match_boxes(self.current_bgr.copy(), self.matches)
        marked_rgb = cv2.cvtColor(marked_bgr, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(marked_rgb)
        
        self.update_idletasks()
        cw = max(10, self.canvas.winfo_width())
        ch = max(10, self.canvas.winfo_height())
        
        iw, ih = pil_img.size
        scale_w = cw / iw
        scale_h = ch / ih
        self.scale_factor = min(scale_w, scale_h) # Tự động scale vừa khít màn hình
        
        new_w = int(iw * self.scale_factor)
        new_h = int(ih * self.scale_factor)
        
        if new_w > 0 and new_h > 0:
            pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
            self.photo = ImageTk.PhotoImage(pil_img)
            self.canvas.delete("all")
            self.canvas.create_image(cw//2, ch//2, image=self.photo, anchor=tk.CENTER)

    def _get_real_coords(self, event_x, event_y):
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if not self.photo: return None, None
        
        pw = self.photo.width()
        ph = self.photo.height()
        
        ox = (cw - pw) // 2
        oy = (ch - ph) // 2
        
        x_in_photo = event_x - ox
        y_in_photo = event_y - oy
        
        if 0 <= x_in_photo <= pw and 0 <= y_in_photo <= ph:
            real_x = x_in_photo / self.scale_factor
            real_y = y_in_photo / self.scale_factor
            return real_x, real_y
        return None, None

    def on_canvas_press(self, event):
        real_x, real_y = self._get_real_coords(event.x, event.y)
        if real_x is None:
            return

        target_query = self.matches[0]['query'] if self.matches else "Query_Mac_Dinh"
        if toggle_box_at_point(
            self.current_bgr, self.matches, real_x, real_y, target_query
        ):
            self.draw_image()

    def save_and_copy(self):
        marked_bgr = draw_match_boxes(self.current_bgr.copy(), self.matches)

        # Determine query subfolder from dominant query in matches
        query_name = get_dominant_query_name(self.matches)

        # Save with metadata (original + JSON sidecar + marked image)
        filepath = save_result_with_metadata(
            marked_bgr,
            self.current_bgr,
            self.matches,
            self.output_dir,
            query_name=query_name,
        )
        print(f"[{time.strftime('%H:%M:%S')}] [SAVE] Saved: {filepath}")

        marked_pil = Image.fromarray(cv2.cvtColor(marked_bgr, cv2.COLOR_BGR2RGB))
        copy_image_to_clipboard(marked_pil)

        # Tell the background thread to ignore the clipboard change we just caused
        self.matcher.ignore_next_clipboard = True

        notify_sound(success=True)
        self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.on_close_callback:
                self.on_close_callback()
        finally:
            self.destroy()
