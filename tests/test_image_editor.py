"""Kiểm thử logic ghép ảnh của cửa sổ chỉnh sửa."""

import os
import tempfile
import time
import unittest

import numpy as np

from image_editor import (
    PendingMergeState,
    ThumbnailDragState,
    calculate_fit_scale,
    list_library_images,
    merge_images_side_by_side,
)


class TestMergeImagesSideBySide(unittest.TestCase):
    def setUp(self):
        self.current = np.full((4, 3, 3), (10, 20, 30), dtype=np.uint8)
        self.added = np.full((4, 2, 3), (100, 110, 120), dtype=np.uint8)

    def test_merge_right_has_expected_size_and_position(self):
        result = merge_images_side_by_side(self.current, self.added, "right")

        self.assertEqual(result.shape, (4, 5, 3))
        np.testing.assert_array_equal(result[:, :3], self.current)
        np.testing.assert_array_equal(result[:, 3:], self.added)

    def test_merge_left_places_added_image_first(self):
        result = merge_images_side_by_side(self.current, self.added, "left")

        self.assertEqual(result.shape, (4, 5, 3))
        np.testing.assert_array_equal(result[:, :2], self.added)
        np.testing.assert_array_equal(result[:, 2:], self.current)

    def test_different_heights_are_centered_on_neutral_background(self):
        short_added = np.full((2, 2, 3), (200, 210, 220), dtype=np.uint8)

        result = merge_images_side_by_side(self.current, short_added, "right")

        self.assertEqual(result.shape, (4, 5, 3))
        np.testing.assert_array_equal(result[:, :3], self.current)
        np.testing.assert_array_equal(result[1:3, 3:], short_added)
        np.testing.assert_array_equal(result[0, 3:], np.full((2, 3), 127, dtype=np.uint8))
        np.testing.assert_array_equal(result[3, 3:], np.full((2, 3), 127, dtype=np.uint8))


class TestCalculateFitScale(unittest.TestCase):
    def test_small_image_stays_at_original_size(self):
        self.assertEqual(calculate_fit_scale(320, 240, 1000, 600), 1.0)

    def test_large_image_is_scaled_down_to_fit(self):
        self.assertAlmostEqual(calculate_fit_scale(2000, 1000, 1000, 800), 0.5)

    def test_fit_down_respects_most_constrained_dimension(self):
        self.assertAlmostEqual(calculate_fit_scale(800, 1200, 1000, 600), 0.5)


class TestLibraryImageLogic(unittest.TestCase):
    def test_list_images_filters_sidecars_and_sorts_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            old_path = os.path.join(directory, "old.jpg")
            new_path = os.path.join(directory, "new.PNG")
            for path in (old_path, new_path, os.path.join(directory, "original_hidden.png"),
                         os.path.join(directory, "notes.txt")):
                with open(path, "wb") as stream:
                    stream.write(b"test")
            now = time.time()
            os.utime(old_path, (now - 10, now - 10))
            os.utime(new_path, (now, now))

            self.assertEqual(list_library_images(directory), [new_path, old_path])

    def test_missing_directory_is_empty(self):
        self.assertEqual(list_library_images("Z:/definitely/missing"), [])

    def test_equal_timestamps_are_sorted_by_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [os.path.join(directory, name) for name in ("b.png", "A.png")]
            for path in paths:
                with open(path, "wb") as stream:
                    stream.write(b"test")
            stamp = time.time()
            for path in paths:
                os.utime(path, (stamp, stamp))

            self.assertEqual(
                [os.path.basename(path) for path in list_library_images(directory)],
                ["A.png", "b.png"],
            )

    def test_pending_merge_state_set_clear_and_consume(self):
        state = PendingMergeState()
        state.set("candidate.png")
        self.assertTrue(state.path.endswith("candidate.png"))
        self.assertEqual(state.consume(), os.path.abspath("candidate.png"))
        self.assertIsNone(state.path)
        state.set("other.png")
        state.clear()
        self.assertIsNone(state.path)


class TestThumbnailDragState(unittest.TestCase):
    bounds = (100, 100, 500, 400)

    def test_motion_below_threshold_remains_click(self):
        state = ThumbnailDragState(threshold=8)
        state.press(10, 10)
        self.assertFalse(state.update(14, 14, self.bounds))
        self.assertEqual(state.release(14, 14, self.bounds), "click")

    def test_valid_drop_sets_highlight_state_and_cleans_up(self):
        state = ThumbnailDragState(threshold=8)
        state.press(10, 10)
        self.assertTrue(state.update(200, 200, self.bounds))
        self.assertTrue(state.over_drop_zone)
        self.assertEqual(state.release(200, 200, self.bounds), "drop")
        self.assertIsNone(state.start)
        self.assertFalse(state.active)
        self.assertFalse(state.over_drop_zone)

    def test_invalid_drop_cancels_and_cleans_up(self):
        state = ThumbnailDragState(threshold=8)
        state.press(10, 10)
        self.assertTrue(state.update(50, 50, self.bounds))
        self.assertFalse(state.over_drop_zone)
        self.assertEqual(state.release(50, 50, self.bounds), "cancel")
        self.assertIsNone(state.start)
        self.assertFalse(state.active)

    def test_reset_cleans_active_drag(self):
        state = ThumbnailDragState()
        state.press(0, 0)
        state.update(200, 200, self.bounds)
        state.reset()
        self.assertIsNone(state.start)
        self.assertFalse(state.active)
        self.assertFalse(state.over_drop_zone)


if __name__ == "__main__":
    unittest.main()
