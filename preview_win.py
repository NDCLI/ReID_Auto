import os
import tkinter as tk
import customtkinter as ctk
from PIL import Image, ImageTk
import cv2
import time
from auto_marker import draw_match_boxes, save_result, copy_image_to_clipboard, notify_sound

class PreviewWindow(ctk.CTkToplevel):
    def __init__(self, master, current_bgr, matches, matcher, output_dir):
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
        self.original_threshold = matcher.threshold
        
        # Top Frame for controls
        top_frame = ctk.CTkFrame(self, fg_color="transparent")
        top_frame.pack(fill=tk.X, padx=15, pady=8)
        
        lbl_sens = ctk.CTkLabel(top_frame, text="Độ nhạy Pixel:", font=("Segoe UI", 11, "bold"))
        lbl_sens.pack(side=tk.LEFT, padx=(0, 5))
        
        self.scale = ctk.CTkSlider(top_frame, from_=0.60, to=0.95, number_of_steps=35, width=150)
        self.scale.set(self.original_threshold)
        self.scale.pack(side=tk.LEFT, padx=5)
        
        self.lbl_thresh = ctk.CTkLabel(top_frame, text=f"{self.original_threshold:.2f}", font=("Segoe UI", 11, "bold"))
        self.lbl_thresh.pack(side=tk.LEFT, padx=5)
        
        self.scale.configure(command=self.on_scale_change)
        
        btn_apply = ctk.CTkButton(
            top_frame, 
            text="Áp dụng lại", 
            width=100, 
            height=28,
            command=self.re_scan,
            fg_color="#34495E",
            hover_color="#2C3E50"
        )
        btn_apply.pack(side=tk.LEFT, padx=10)
        
        lbl_tip = ctk.CTkLabel(
            top_frame, 
            text="(MẸO: Click khung để XÓA | Kéo chuột để VẼ THÊM | Chuột phải để LƯU NHANH)", 
            text_color="#3498DB", 
            font=("Segoe UI", 11, "bold")
        )
        lbl_tip.pack(side=tk.LEFT, padx=20)
        
        # Bottom Frame for Save/Cancel
        bot_frame = ctk.CTkFrame(self, fg_color="transparent")
        bot_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=15, pady=10)
        
        btn_cancel = ctk.CTkButton(
            bot_frame, 
            text="HỦY", 
            width=100,
            command=self.destroy,
            fg_color="#7F8C8D",
            hover_color="#95A5A6"
        )
        btn_cancel.pack(side=tk.RIGHT, padx=5)
        
        btn_save = ctk.CTkButton(
            bot_frame, 
            text="LƯU & COPY", 
            width=130,
            command=self.save_and_copy,
            fg_color="#2ECC71",
            hover_color="#27AE60",
            font=("Segoe UI", 11, "bold")
        )
        btn_save.pack(side=tk.RIGHT, padx=5)
        
        # Canvas Frame (with dark background and nice styling)
        self.canvas_frame = ctk.CTkFrame(self, fg_color="#1E272C", corner_radius=10, border_width=1, border_color="#34495E")
        self.canvas_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)
        
        self.canvas = tk.Canvas(self.canvas_frame, bg="#1E272C", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.canvas.bind("<ButtonPress-1>", self.on_canvas_press)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        
        # Right click to Save & Copy
        self.canvas.bind("<Button-3>", lambda e: self.save_and_copy())
        self.bind("<Button-3>", lambda e: self.save_and_copy())
        
        self.bind("<Configure>", self.on_resize)
        self.bind("<Escape>", lambda e: self.destroy())
        
        self.photo = None
        self.scale_factor = 1.0
        
        # Drawing state
        self.start_x = None
        self.start_y = None
        self.current_rect = None
        self.is_drawing = False
        
        self.draw_image()

    def on_scale_change(self, val):
        self.lbl_thresh.configure(text=f"{float(val):.2f}")

    def re_scan(self):
        new_thresh = float(self.scale.get())
        self.matcher.threshold = new_thresh
        self.matches = self.matcher.find_matches(self.current_bgr, debug=False)
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
        if real_x is None: return
        
        # 1. Check if clicked inside any existing box (for deletion)
        for m in self.matches:
            bx1, by1, bx2, by2 = m['bbox']
            if bx1 <= real_x <= bx2 and by1 <= real_y <= by2:
                self.matches.remove(m)
                self.draw_image()
                return # Deleted, don't start drawing
                
        # 2. Start drawing a new box
        self.is_drawing = True
        self.start_x = event.x
        self.start_y = event.y
        self.current_rect = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline="red", width=2)

    def on_canvas_drag(self, event):
        if self.is_drawing and self.current_rect:
            self.canvas.coords(self.current_rect, self.start_x, self.start_y, event.x, event.y)

    def on_canvas_release(self, event):
        if not self.is_drawing: return
        self.is_drawing = False
        
        if self.current_rect:
            self.canvas.delete(self.current_rect)
            self.current_rect = None
            
        real_x1, real_y1 = self._get_real_coords(self.start_x, self.start_y)
        real_x2, real_y2 = self._get_real_coords(event.x, event.y)
        
        if real_x1 is not None and real_x2 is not None:
            x1 = min(real_x1, real_x2)
            y1 = min(real_y1, real_y2)
            x2 = max(real_x1, real_x2)
            y2 = max(real_y1, real_y2)
            
            width = x2 - x1
            height = y2 - y1
            
            # Only add if it's a reasonably sized box (avoid accidental tiny clicks)
            if width > 10 and height > 10:
                target_query = "Query_Mac_Dinh"
                if self.matches:
                    target_query = self.matches[0]['query']
                    
                new_match = {
                    'bbox': (int(x1), int(y1), int(x2), int(y2)),
                    'score': 1.0,
                    'query': target_query
                }
                self.matches.append(new_match)
                self.draw_image()

    def save_and_copy(self):
        # Restore matcher threshold
        self.matcher.threshold = self.original_threshold
        
        marked_bgr = draw_match_boxes(self.current_bgr.copy(), self.matches)
        filepath = save_result(marked_bgr, self.output_dir)
        print(f"[{time.strftime('%H:%M:%S')}] [SAVE] Saved: {filepath}")
        
        marked_pil = Image.fromarray(cv2.cvtColor(marked_bgr, cv2.COLOR_BGR2RGB))
        copy_image_to_clipboard(marked_pil)
        
        # Tell the background thread to ignore the clipboard change we just caused
        self.matcher.ignore_next_clipboard = True
        
        notify_sound(success=True)
        self.destroy()
