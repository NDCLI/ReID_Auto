with open('app_gui.pyw', 'r', encoding='utf-8') as f:
    code = f.read()

bad_indent = '''        if is_wide_row:
            def on_editor_saved(new_bgr):
            edited_pil = Image.fromarray(cv2.cvtColor(new_bgr, cv2.COLOR_BGR2RGB))
            
            # Save the edited version to output directories if enabled'''

good_indent = '''        if is_wide_row:
            def on_editor_saved(new_bgr):
                edited_pil = Image.fromarray(cv2.cvtColor(new_bgr, cv2.COLOR_BGR2RGB))
                
                # Save the edited version to output directories if enabled'''

code = code.replace(bad_indent, good_indent)

bad_indent2 = '''            # Save the edited version to output directories if enabled
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
        self.root.attributes("-topmost", False)
        
        def on_editor_cancelled():
            # Restore main window if user cancels
            try:
                self.root.deiconify()
                self.root.after(10, self.root.focus_force)
            except (tk.TclError, AttributeError):
                pass

            # Open editor window
            ImageEditorWindow(self.root, bgr_image, on_save_callback=on_editor_saved, on_cancel_callback=on_editor_cancelled, title="Chỉnh sửa ảnh chụp")'''

good_indent2 = '''                # Save the edited version to output directories if enabled
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
            self.root.attributes("-topmost", False)
            
            def on_editor_cancelled():
                # Restore main window if user cancels
                try:
                    self.root.deiconify()
                    self.root.after(10, self.root.focus_force)
                except (tk.TclError, AttributeError):
                    pass

            # Open editor window
            ImageEditorWindow(self.root, bgr_image, on_save_callback=on_editor_saved, on_cancel_callback=on_editor_cancelled, title="Chỉnh sửa ảnh chụp")'''

code = code.replace(bad_indent2, good_indent2)

with open('app_gui.pyw', 'w', encoding='utf-8') as f:
    f.write(code)
