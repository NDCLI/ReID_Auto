"""Regression tests for preview window close behavior."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import preview_win


class TestPreviewClose(unittest.TestCase):
    def test_close_calls_callback_once_and_destroys_once(self):
        callback = Mock()
        destroyed = []
        window = SimpleNamespace(
            _closed=False,
            on_close_callback=callback,
            destroy=lambda: destroyed.append("destroyed"),
        )

        preview_win.PreviewWindow.close(window)
        preview_win.PreviewWindow.close(window)

        callback.assert_called_once()
        self.assertEqual(destroyed, ["destroyed"])
        self.assertTrue(window._closed)

