"""Unit tests for auto_marker.py — geometry helpers, NMS, box drawing logic."""

import unittest
import numpy as np

import auto_marker


# ---------------------------------------------------------------------------
# Geometry helpers on TemplateMatcher
# ---------------------------------------------------------------------------
class TestComputeIoU(unittest.TestCase):
    """TemplateMatcher._compute_iou is a static method."""

    def test_identical_boxes(self):
        box = [0, 0, 100, 100]
        self.assertAlmostEqual(auto_marker.TemplateMatcher._compute_iou(box, box), 1.0)

    def test_no_overlap(self):
        a = [0, 0, 50, 50]
        b = [100, 100, 200, 200]
        self.assertAlmostEqual(auto_marker.TemplateMatcher._compute_iou(a, b), 0.0)

    def test_partial_overlap(self):
        a = [0, 0, 100, 100]
        b = [50, 50, 150, 150]
        iou = auto_marker.TemplateMatcher._compute_iou(a, b)
        # Intersection: 50*50 = 2500, Union: 10000 + 10000 - 2500 = 17500
        self.assertAlmostEqual(iou, 2500 / 17500, places=5)

    def test_contained_box(self):
        outer = [0, 0, 200, 200]
        inner = [50, 50, 100, 100]
        iou = auto_marker.TemplateMatcher._compute_iou(outer, inner)
        # Intersection: 50*50 = 2500, Union: 40000 + 2500 - 2500 = 40000
        self.assertAlmostEqual(iou, 2500 / 40000, places=5)

    def test_zero_area_box(self):
        a = [10, 10, 10, 10]  # zero area
        b = [0, 0, 100, 100]
        self.assertAlmostEqual(auto_marker.TemplateMatcher._compute_iou(a, b), 0.0)


class TestNonMaxSuppression(unittest.TestCase):
    """TemplateMatcher._non_max_suppression removes overlapping detections."""

    def setUp(self):
        # Create a bare matcher instance (skip __init__ which loads files)
        self.matcher = auto_marker.TemplateMatcher.__new__(auto_marker.TemplateMatcher)

    def test_empty_input(self):
        self.assertEqual(self.matcher._non_max_suppression([], 0.3), [])

    def test_single_match(self):
        matches = [{"bbox": [0, 0, 100, 100], "score": 0.9}]
        result = self.matcher._non_max_suppression(matches, 0.3)
        self.assertEqual(len(result), 1)

    def test_suppresses_overlap(self):
        matches = [
            {"bbox": [10, 10, 50, 50], "score": 0.9},
            {"bbox": [12, 12, 52, 52], "score": 0.8},  # overlaps heavily
            {"bbox": [200, 200, 300, 300], "score": 0.85},  # separate
        ]
        result = self.matcher._non_max_suppression(matches, 0.3)
        self.assertEqual(len(result), 2)
        # Highest score kept
        self.assertEqual(result[0]["score"], 0.9)

    def test_keeps_non_overlapping(self):
        matches = [
            {"bbox": [0, 0, 50, 50], "score": 0.7},
            {"bbox": [100, 100, 150, 150], "score": 0.8},
            {"bbox": [200, 200, 250, 250], "score": 0.6},
        ]
        result = self.matcher._non_max_suppression(matches, 0.3)
        self.assertEqual(len(result), 3)


# ---------------------------------------------------------------------------
# UI-anchored box drawing
# ---------------------------------------------------------------------------
class TestDrawMatchBoxes(unittest.TestCase):
    @staticmethod
    def _ui_card(camera_left, camera_right):
        image = np.full((120, 100, 3), 20, dtype=np.uint8)
        image[10:100, 20:80] = 28
        image[10:100, camera_left:camera_right] = 40
        return image

    def test_uses_exact_six_pixel_card_inset(self):
        image = self._ui_card(26, 74)
        matches = [{"bbox": (30, 20, 70, 80), "query": "Query_1"}]

        auto_marker.draw_match_boxes(image, matches)

        self.assertEqual(matches[0]["bbox"], (26, 9, 73, 100))

    def test_clamps_to_camera_image_edge(self):
        image = self._ui_card(23, 77)
        matches = [{"bbox": (30, 20, 70, 80), "query": "Query_1"}]

        auto_marker.draw_match_boxes(image, matches)

        self.assertEqual(matches[0]["bbox"], (23, 9, 76, 100))

# ---------------------------------------------------------------------------
# Open-set body-ReID rejection
# ---------------------------------------------------------------------------
class _FakeExtractor:
    """Minimal score-only extractor for classifier policy tests."""

    @staticmethod
    def ensemble_similarity(_candidate_features, reference_features):
        score = reference_features["score"]
        return score, {"fake_model": score}

    @staticmethod
    def face_similarity(_candidate_features, _reference_features):
        return None


class TestBestReferenceRejection(unittest.TestCase):
    def setUp(self):
        self.matcher = auto_marker.TemplateMatcher.__new__(
            auto_marker.TemplateMatcher
        )
        self.matcher.ai_extractor = _FakeExtractor()
        self.matcher.query_thresholds = {"Query_1": 0.55}

    def _classify(self, scores):
        self.matcher.reference_images = {
            "Query_1": [
                (f"ref_{index}.png", None, {"score": score})
                for index, score in enumerate(scores)
            ]
        }
        return self.matcher._classify_features({"fake_model": np.ones(1)})

    def test_rejects_weak_best_reference_despite_passing_average(self):
        """Scores just under the configured gate must still be rejected.

        Anchored to AI_BEST_REFERENCE_THRESHOLD rather than a literal, because
        this variant calibrates the gate to its own OSNet-only ensemble.
        """
        gate = auto_marker.AI_BEST_REFERENCE_THRESHOLD
        self.assertIsNone(self._classify([gate - 0.01, gate - 0.02]))

    def test_accepts_when_one_reference_is_strong(self):
        gate = auto_marker.AI_BEST_REFERENCE_THRESHOLD
        result = self._classify([gate + 0.05, gate - 0.05])
        self.assertIsNotNone(result)
        self.assertEqual(result["query"], "Query_1")
        self.assertAlmostEqual(result["best_reference_score"], gate + 0.05)


# ---------------------------------------------------------------------------
# Source-card removal
# ---------------------------------------------------------------------------
class TestSourceGridRemoval(unittest.TestCase):
    def setUp(self):
        self.matcher = auto_marker.TemplateMatcher.__new__(auto_marker.TemplateMatcher)
        self.matcher._detect_result_grid = lambda _image: [(0, 0, 80, 180)]

    def test_keeps_first_accepted_match_when_source_card_is_absent(self):
        candidate = {"bbox": (100, 0, 180, 180), "query": "Query_1"}
        result = self.matcher._remove_source_grid_match(
            [candidate],
            np.zeros((200, 200, 3), dtype=np.uint8),
        )
        self.assertEqual(result, [candidate])

    def test_removes_only_match_overlapping_source_card(self):
        source = {"bbox": (0, 0, 80, 180), "query": "Query_1"}
        candidate = {"bbox": (100, 0, 180, 180), "query": "Query_1"}
        result = self.matcher._remove_source_grid_match(
            [source, candidate],
            np.zeros((200, 200, 3), dtype=np.uint8),
        )
        self.assertEqual(result, [candidate])


# ---------------------------------------------------------------------------
# Log helper
# ---------------------------------------------------------------------------
class TestLogFunction(unittest.TestCase):
    """log() should not raise and should print to stdout."""

    def test_log_does_not_raise(self):
        # Just verify it doesn't crash
        auto_marker.log("TEST", "hello world")
        auto_marker.log("WARN", "some warning", color="yellow")


# ---------------------------------------------------------------------------
# classify_item_query from batch_review
# ---------------------------------------------------------------------------
class TestClassifyItemQuery(unittest.TestCase):
    """batch_review.classify_item_query picks the dominant query name."""

    def test_empty_matches(self):
        from batch_review import classify_item_query
        self.assertEqual(classify_item_query([]), "Chua_xac_dinh")

    def test_single_query(self):
        from batch_review import classify_item_query
        matches = [
            {"query": "Query_1", "score": 0.9},
            {"query": "Query_1", "score": 0.8},
        ]
        self.assertEqual(classify_item_query(matches), "Query_1")

    def test_dominant_by_count(self):
        from batch_review import classify_item_query
        matches = [
            {"query": "Query_1", "score": 0.9},
            {"query": "Query_2", "score": 0.95},
            {"query": "Query_1", "score": 0.8},
        ]
        # Query_1 has 2 matches, Query_2 has 1
        self.assertEqual(classify_item_query(matches), "Query_1")

    def test_missing_query_key(self):
        from batch_review import classify_item_query
        matches = [{"score": 0.9}]
        self.assertEqual(classify_item_query(matches), "Chua_xac_dinh")


if __name__ == "__main__":
    unittest.main()
