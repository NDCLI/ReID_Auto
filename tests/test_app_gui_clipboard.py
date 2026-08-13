"""Regression tests for review-window clipboard state handling."""

from types import SimpleNamespace
import queue
import unittest
from unittest.mock import MagicMock, patch

import app_gui


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
        thread_mock.return_value.start.assert_called_once()

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
        thread_mock.return_value.start.assert_called_once()

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
