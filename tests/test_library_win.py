"""Regression tests for LibraryWindow clipboard behavior."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

import library_win


class _Label:
    def __init__(self):
        self.text = ""

    def configure(self, **kwargs):
        self.text = kwargs.get("text", self.text)


class TestLibraryCopy(unittest.TestCase):
    def test_copy_current_copies_selected_image_and_ignores_own_clipboard_event(self):
        image_bgr = np.full((12, 18, 3), (10, 20, 30), dtype=np.uint8)
        matcher = SimpleNamespace(ignore_next_clipboard=False)
        label = _Label()
        window = SimpleNamespace(
            items=[{"path": "selected.png", "rel": "Query_1/selected.png"}],
            current_index=0,
            _edit_original_bgr=None,
            _edit_matches=None,
            matcher=matcher,
            current_label=label,
            bell=lambda: None,
        )
        copied = []

        with patch.object(library_win, "read_image_file", return_value=image_bgr), patch.object(
            library_win, "copy_image_to_clipboard", side_effect=copied.append
        ):
            result = library_win.LibraryWindow.copy_current(window)

        self.assertEqual(result, "break")
        self.assertTrue(matcher.ignore_next_clipboard)
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0].size, (18, 12))
        self.assertIn("Đã copy ảnh", label.text)
