"""Regression tests for review-window clipboard state handling."""

from types import SimpleNamespace
import queue
import unittest
from unittest.mock import MagicMock, patch

import app_gui
import numpy as np
import cv2


class _FakePreviewWindow:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.closed = False

    def winfo_exists(self):
        return not self.closed


class _FakeRoot:
    def __init__(self):
        self.callbacks = []

    def after(self, delay, callback):
        self.callbacks.append((delay, callback))


class TestReviewClipboardSync(unittest.TestCase):
    def setUp(self):
        self.app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        self.app.root = SimpleNamespace()
        self.app.matcher = object()
        self.app.is_processing = True
        self.app.active_preview_window = None
        self.app.last_clipboard_sequence = 1
        self.app.last_clipboard_hash = "hash-1"

    def test_open_preview_syncs_clipboard_and_close_clears_review_state(self):
        with patch.object(app_gui, "PreviewWindow", _FakePreviewWindow), patch.object(
            app_gui, "get_clipboard_sequence_number", return_value=7
        ), patch.object(app_gui, "get_clipboard_image_hash") as hash_mock:
            app_gui.AutoMarkerApp.open_preview_window(
                self.app,
                current_bgr="current-bgr",
                matches=[{"query": "Query_1"}],
            )

        self.assertFalse(self.app.is_processing)
        self.assertIsInstance(self.app.active_preview_window, _FakePreviewWindow)
        self.assertEqual(self.app.last_clipboard_sequence, 7)
        hash_mock.assert_not_called()

        with patch.object(app_gui, "get_clipboard_sequence_number", return_value=8), patch.object(
            app_gui, "get_clipboard_image_hash"
        ) as hash_mock_close:
            self.app.active_preview_window.kwargs["on_close_callback"]()

        self.assertIsNone(self.app.active_preview_window)
        self.assertEqual(self.app.last_clipboard_sequence, 8)
        hash_mock_close.assert_not_called()

    def test_sync_clipboard_snapshot_uses_hash_fallback(self):
        with patch.object(app_gui, "get_clipboard_sequence_number", return_value=None), patch.object(
            app_gui, "get_clipboard_image_hash", return_value="hash-2"
        ) as hash_mock:
            app_gui.AutoMarkerApp._sync_clipboard_snapshot(self.app)

        self.assertEqual(self.app.last_clipboard_hash, "hash-2")
        hash_mock.assert_called_once()


class TestClipboardCaptureRetry(unittest.TestCase):
    def test_transient_unreadable_sharex_image_is_retried(self):
        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        app.root = _FakeRoot()
        app.matcher = SimpleNamespace(ignore_next_clipboard=False)
        app.is_monitoring = True
        app.is_processing = False
        app.active_preview_window = None
        app.last_clipboard_sequence = 1
        app.last_clipboard_hash = None
        app.pending_clipboard_sequence = None
        app.pending_clipboard_hash = None
        app.pending_clipboard_retries = 0
        app.clipboard_image_retry_limit = 10
        app.clipboard_poll_ms = 100
        app.process_clipboard_image = MagicMock()

        with patch.object(
            app_gui, "get_clipboard_sequence_number", side_effect=[2, 2]
        ), patch.object(
            app_gui, "get_clipboard_image", side_effect=[None, "sharex-image"]
        ), patch.object(
            app_gui, "get_clipboard_owner_process_name", return_value="sharex.exe"
        ), patch.object(app_gui.threading, "Thread") as thread_mock:
            app.poll_clipboard()
            self.assertEqual(app.last_clipboard_sequence, 1)
            self.assertEqual(app.pending_clipboard_sequence, 2)
            self.assertEqual(app.pending_clipboard_retries, 1)
            thread_mock.assert_not_called()

            app.poll_clipboard()

        self.assertEqual(app.last_clipboard_sequence, 2)
        self.assertIsNone(app.pending_clipboard_sequence)
        thread_mock.assert_called_once()
        self.assertEqual(thread_mock.return_value.start.call_count, 1)

    def test_last_region_image_can_arrive_after_one_second(self):
        """ShareX LastRegion must not lose a delayed first clipboard image."""
        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        app.root = _FakeRoot()
        app.matcher = SimpleNamespace(ignore_next_clipboard=False)
        app.is_monitoring = True
        app.is_processing = False
        app.active_preview_window = None
        app.last_clipboard_sequence = 1
        app.last_clipboard_hash = None
        app.pending_clipboard_sequence = None
        app.pending_clipboard_hash = None
        app.pending_clipboard_retries = 0
        app.clipboard_image_retry_limit = 50  # 5 seconds at 100 ms polling
        app.clipboard_poll_ms = 100
        app.process_clipboard_image = MagicMock()

        with patch.object(
            app_gui, "get_clipboard_sequence_number", return_value=2
        ), patch.object(
            app_gui, "get_clipboard_image", side_effect=[None] * 15 + ["last-region-image"]
        ), patch.object(
            app_gui, "should_ignore_clipboard_image", return_value=False
        ), patch.object(app_gui.threading, "Thread") as thread_mock:
            for _ in range(16):
                app.poll_clipboard()

        self.assertEqual(app.last_clipboard_sequence, 2)
        self.assertEqual(app.pending_clipboard_retries, 0)
        thread_mock.assert_called_once()
        self.assertEqual(thread_mock.return_value.start.call_count, 1)

    def test_excel_foreground_does_not_block_sharex_owned_image(self):
        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        app.root = _FakeRoot()
        app.matcher = SimpleNamespace(ignore_next_clipboard=False)
        app.is_monitoring = True
        app.is_processing = False
        app.active_preview_window = None
        app.last_clipboard_sequence = 1
        app.last_clipboard_hash = None
        app.pending_clipboard_sequence = None
        app.pending_clipboard_hash = None
        app.pending_clipboard_retries = 0
        app.clipboard_image_retry_limit = 10
        app.clipboard_poll_ms = 100

        with patch.object(
            app_gui, "get_clipboard_sequence_number", return_value=2
        ), patch.object(
            app_gui, "get_clipboard_image", return_value="sharex-image"
        ), patch.object(
            app_gui, "get_clipboard_owner_process_name", return_value="sharex.exe"
        ), patch.object(app_gui.threading, "Thread") as thread_mock:
            app.poll_clipboard()

        self.assertEqual(app.last_clipboard_sequence, 2)
        thread_mock.assert_called_once()
        self.assertEqual(thread_mock.return_value.start.call_count, 1)

    def test_non_sharex_image_is_ignored_while_excel_is_foreground(self):
        self.assertTrue(
            app_gui.should_ignore_clipboard_image(
                owner_process="", foreground_process="excel.exe"
            )
        )

    def test_sharex_owner_overrides_excel_foreground(self):
        self.assertFalse(
            app_gui.should_ignore_clipboard_image(
                owner_process="sharex.exe", foreground_process="excel.exe"
            )
        )

    def test_excel_owned_image_is_still_ignored(self):
        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        app.root = _FakeRoot()
        app.matcher = SimpleNamespace(ignore_next_clipboard=False)
        app.is_monitoring = True
        app.is_processing = False
        app.active_preview_window = None
        app.last_clipboard_sequence = 1
        app.last_clipboard_hash = None
        app.pending_clipboard_sequence = None
        app.pending_clipboard_hash = None
        app.pending_clipboard_retries = 0
        app.clipboard_image_retry_limit = 10
        app.clipboard_poll_ms = 100

        with patch.object(
            app_gui, "get_clipboard_sequence_number", return_value=2
        ), patch.object(
            app_gui, "get_clipboard_image", return_value="excel-image"
        ), patch.object(
            app_gui, "get_clipboard_owner_process_name", return_value="excel.exe"
        ), patch.object(app_gui.threading, "Thread") as thread_mock:
            app.poll_clipboard()

        self.assertEqual(app.last_clipboard_sequence, 2)
        thread_mock.assert_not_called()


class TestMainWindowControls(unittest.TestCase):
    def setUp(self):
        self.app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)

    def test_clear_logs_removes_pending_and_visible_messages(self):
        self.app.log_queue = queue.Queue()
        self.app.log_queue.put("pending")
        self.app.txt_logs = MagicMock()

        self.app.clear_logs()

        self.assertTrue(self.app.log_queue.empty())
        self.app.txt_logs.delete.assert_called_once_with("1.0", "end")
        self.assertEqual(self.app.txt_logs.configure.call_count, 2)

    def test_tray_menu_has_no_pause_or_resume_control(self):
        captured_items = []

        def fake_menu_item(text, *args, **kwargs):
            captured_items.append(text)
            return text

        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        with patch.object(app_gui.pystray, "MenuItem", side_effect=fake_menu_item), patch.object(
            app_gui.pystray, "Menu", side_effect=lambda *items: items
        ):
            app.create_tray_menu()

        self.assertFalse(any("Tạm dừng" in str(item) for item in captured_items))
        self.assertFalse(any("Tiếp tục" in str(item) for item in captured_items))
        self.assertTrue(any("Chụp vùng" in str(item) for item in captured_items))
        self.assertTrue(any("vùng trước" in str(item) for item in captured_items))
        self.assertTrue(any("Alt+PrintScreen" in str(item) for item in captured_items))
        self.assertTrue(any("Alt+S" in str(item) for item in captured_items))


class TestDirectRegionCapture(unittest.TestCase):
    def test_missing_ui_template_rejects_reid_classification(self):
        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        with patch.object(app_gui.os.path, "exists", return_value=False):
            self.assertFalse(app.check_is_reid_interface(np.zeros((100, 200, 3), dtype=np.uint8)))

    def test_reid_ui_detection_does_not_return_before_template_matching(self):
        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        template = np.zeros((20, 30, 3), dtype=np.uint8)
        image = np.zeros((400, 800, 3), dtype=np.uint8)
        with patch.object(app_gui.os.path, "exists", return_value=True), patch.object(
            cv2, "imread", return_value=template
        ), patch.object(
            cv2, "matchTemplate", return_value=np.array([[0.95]], dtype=np.float32)
        ), patch.object(
            cv2, "minMaxLoc", return_value=(0.0, 0.95, (0, 0), (0, 0))
        ):
            self.assertTrue(app.check_is_reid_interface(image))

    def test_normalize_capture_bounds_orders_drag_coordinates(self):
        self.assertEqual(
            app_gui.normalize_capture_bounds(90, 80, 10, 20), (10, 20, 90, 80)
        )
        self.assertIsNone(app_gui.normalize_capture_bounds(10, 10, 12, 13))

    def test_last_region_crops_fresh_screen_and_bypasses_clipboard(self):
        from PIL import Image

        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        app.region_capture_overlay = None
        app.is_processing = False
        app.active_preview_window = None
        app.matcher = object()
        app.last_capture_region = (1, 2, 5, 7)
        app.show_osd = MagicMock()
        app._grab_virtual_screen = MagicMock(
            return_value=Image.new("RGB", (10, 10), "white")
        )
        app._submit_direct_capture = MagicMock()

        app._capture_last_region_after_hiding_main()

        captured_image, label = app._submit_direct_capture.call_args.args
        self.assertEqual(captured_image.size, (4, 5))
        self.assertEqual(label, "vùng trước")

    def test_visible_main_window_is_hidden_before_capture(self):
        root = MagicMock()
        root.state.return_value = "normal"
        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        app.root = root

        app._hide_main_window_for_capture()

        self.assertTrue(app._restore_main_after_capture_cancel)
        root.withdraw.assert_called_once()
        root.update.assert_called_once()

    def test_direct_capture_starts_normal_reid_pipeline(self):
        from PIL import Image

        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        app.is_processing = False
        app.process_clipboard_image = MagicMock()
        app.show_osd = MagicMock()

        with patch.object(app_gui.threading, "Thread") as thread_mock:
            app._submit_direct_capture(Image.new("RGB", (8, 6), "white"), "vùng đã chọn")

        self.assertTrue(app.is_processing)
        self.assertEqual(thread_mock.call_count, 2)
        self.assertIs(thread_mock.call_args.kwargs["target"], app.process_clipboard_image)
        self.assertEqual(thread_mock.return_value.start.call_count, 2)

    def test_direct_capture_saves_raw_image_when_enabled(self):
        from PIL import Image

        app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        app.is_processing = False
        app.process_clipboard_image = MagicMock()
        app.show_osd = MagicMock()
        app.save_direct_captures = SimpleNamespace(get=lambda: True)
        app._save_direct_capture_image = MagicMock(return_value="saved.png")

        with patch.object(app_gui.threading, "Thread"):
            app._submit_direct_capture(Image.new("RGB", (8, 6), "white"), "vùng đã chọn")

        app._save_direct_capture_image.assert_called_once()

    def test_capture_hotkeys_do_not_depend_on_query_folder(self):
        app = SimpleNamespace(
            root=SimpleNamespace(after=lambda _delay, callback: callback()),
            start_region_capture=MagicMock(),
            capture_last_region=MagicMock(),
        )
        manager = app_gui.GlobalHotkeyManager(app)

        manager._process_hotkey_on_main_thread(104)
        manager._process_hotkey_on_main_thread(105)
        manager._process_hotkey_on_main_thread(106)

        self.assertEqual(app.start_region_capture.call_count, 2)
        app.capture_last_region.assert_called_once()
