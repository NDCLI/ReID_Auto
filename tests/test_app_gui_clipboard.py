"""Regression tests for review-window clipboard state handling."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app_gui


class _FakePreviewWindow:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.closed = False

    def winfo_exists(self):
        return not self.closed


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
