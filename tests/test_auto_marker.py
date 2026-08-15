"""Unit tests for auto_marker.py — geometry helpers, NMS, box drawing logic."""

import unittest
from contextlib import contextmanager
import numpy as np

import auto_marker


@contextmanager
def patch_ocr_enabled():
    """Temporarily force ENABLE_OCR_TIMESTAMP_FILTER on for a test."""
    original = auto_marker.ENABLE_OCR_TIMESTAMP_FILTER
    auto_marker.ENABLE_OCR_TIMESTAMP_FILTER = True
    try:
        yield
    finally:
        auto_marker.ENABLE_OCR_TIMESTAMP_FILTER = original


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
# Click-to-box in the review windows
# ---------------------------------------------------------------------------
class TestBoxAtPoint(unittest.TestCase):
    """A single click resolves the card box using the drawing rules."""

    @staticmethod
    def _ui_card(camera_left, camera_right):
        image = np.full((120, 100, 3), 20, dtype=np.uint8)
        image[10:100, 20:80] = 28
        image[10:100, camera_left:camera_right] = 40
        return image

    def test_click_matches_the_drawn_box(self):
        image = self._ui_card(26, 74)
        matches = [{"bbox": (30, 20, 70, 80), "query": "Query_1"}]
        auto_marker.draw_match_boxes(image, matches)

        self.assertEqual(auto_marker.box_at_point(image, 50, 50), matches[0]["bbox"])

    def test_click_outside_image_returns_none(self):
        image = self._ui_card(26, 74)

        self.assertIsNone(auto_marker.box_at_point(image, 500, 50))
        self.assertIsNone(auto_marker.box_at_point(image, -1, 50))

    def test_click_in_card_gap_returns_none(self):
        image = np.full((120, 100, 3), 20, dtype=np.uint8)

        self.assertIsNone(auto_marker.box_at_point(image, 50, 50))


class TestToggleBoxAtPoint(unittest.TestCase):
    @staticmethod
    def _ui_card():
        image = np.full((120, 100, 3), 20, dtype=np.uint8)
        image[10:100, 20:80] = 28
        image[10:100, 26:74] = 40
        return image

    def test_click_adds_box_on_empty_card(self):
        image = self._ui_card()
        matches = []

        self.assertTrue(auto_marker.toggle_box_at_point(image, matches, 50, 50, "Query_9"))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["query"], "Query_9")
        self.assertEqual(matches[0]["bbox"], auto_marker.box_at_point(image, 50, 50))

    def test_click_inherits_query_from_existing_match(self):
        image = self._ui_card()
        matches = [{"bbox": (0, 0, 5, 5), "score": 1.0, "query": "Query_3"}]

        auto_marker.toggle_box_at_point(image, matches, 50, 50, "Query_Mac_Dinh")

        self.assertEqual(matches[-1]["query"], "Query_3")

    def test_second_click_removes_the_box(self):
        image = self._ui_card()
        matches = []
        auto_marker.toggle_box_at_point(image, matches, 50, 50)

        self.assertTrue(auto_marker.toggle_box_at_point(image, matches, 50, 50))
        self.assertEqual(matches, [])

    def test_click_on_card_padding_removes_that_cards_box(self):
        image = self._ui_card()
        matches = [{"bbox": (30, 20, 70, 80), "query": "Query_1"}]
        auto_marker.draw_match_boxes(image, matches)

        # x=24 is card padding outside the drawn box but still on the same card.
        self.assertTrue(auto_marker.toggle_box_at_point(image, matches, 24, 50))
        self.assertEqual(matches, [])

    def test_click_in_gap_changes_nothing(self):
        image = np.full((120, 100, 3), 20, dtype=np.uint8)
        matches = []

        self.assertFalse(auto_marker.toggle_box_at_point(image, matches, 50, 50))
        self.assertEqual(matches, [])


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
# Exact-time rescue for stable near-threshold body matches
# ---------------------------------------------------------------------------
class TestTimeVerifiedNearThresholdRescue(unittest.TestCase):
    def setUp(self):
        self.matcher = auto_marker.TemplateMatcher.__new__(
            auto_marker.TemplateMatcher
        )
        self.matcher.ai_extractor = _FakeExtractor()
        self.matcher.query_thresholds = {
            "Query_1": 0.65,
            "Query_2": 0.65,
        }
        self.matcher.reference_images = {
            "Query_1": [
                ("target_a.png", None, {"score": 0.63}),
                ("target_b.png", None, {"score": 0.62}),
            ],
            "Query_2": [
                ("other_a.png", None, {"score": 0.40}),
                ("other_b.png", None, {"score": 0.39}),
            ],
        }
        self.matcher.reference_timestamps = {"Query_1": ["12:22 PM"]}
        self.candidate = {"fake_model": np.ones(1)}
        self.crop = np.zeros((190, 80, 3), dtype=np.uint8)

    def _pending(self):
        return self.matcher._classify_features(
            self.candidate,
            allow_time_rescue=True,
        )

    def _confirm_with_time(self, timestamp):
        original = auto_marker.extract_reference_timestamp
        auto_marker.extract_reference_timestamp = lambda _crop: timestamp
        try:
            return self.matcher._confirm_time_rescue(
                self._pending(), self.crop
            )
        finally:
            auto_marker.extract_reference_timestamp = original

    def test_normal_policy_still_rejects_score_below_global_threshold(self):
        result = self.matcher._classify_features(self.candidate)

        self.assertIsNone(result)

    def test_exact_query_time_rescues_stable_near_threshold_match(self):
        result = self._confirm_with_time("12:22 PM")

        self.assertIsNotNone(result)
        self.assertEqual(result["query"], "Query_1")
        self.assertTrue(result["time_rescue"])
        self.assertEqual(result["card_timestamp"], "12:22 PM")
        self.assertAlmostEqual(result["score"], 0.625)

    def test_different_time_does_not_rescue_near_threshold_match(self):
        self.assertIsNone(self._confirm_with_time("1:55 PM"))

    def test_unreadable_time_does_not_rescue_near_threshold_match(self):
        self.assertIsNone(self._confirm_with_time(None))

    def test_score_below_rescue_floor_is_not_pending(self):
        self.matcher.reference_images["Query_1"] = [
            ("target_a.png", None, {"score": 0.61}),
            ("target_b.png", None, {"score": 0.60}),
        ]

        self.assertIsNone(self._pending())


# ---------------------------------------------------------------------------
# Accepted-match alignment and fast-root card retention
# ---------------------------------------------------------------------------
class TestAcceptedMatchRetention(unittest.TestCase):
    def setUp(self):
        self.matcher = auto_marker.TemplateMatcher.__new__(
            auto_marker.TemplateMatcher
        )
        self.matcher.reference_images = {
            "Query_1": [
                (f"ref_{index}.png", None, {}) for index in range(6)
            ]
        }
        # No reference timestamps → the per-card OCR gate keeps every match,
        # so these tests exercise only the cap/alignment behavior.
        self.matcher.reference_timestamps = {}

    def test_reference_count_cap_reserves_source_slot(self):
        matches = [
            {
                "bbox": (index * 100, 10, index * 100 + 80, 196),
                "query": "Query_1",
                "score": 0.9 - index * 0.01,
            }
            for index in range(6)
        ]

        result = self.matcher._limit_and_align_matches(matches)

        self.assertEqual(len(result), 5)
        self.assertEqual({match["query"] for match in result}, {"Query_1"})
        self.assertNotIn((500, 10, 580, 196), [match["bbox"] for match in result])

    def test_fast_root_reserves_source_slot_in_result_cap(self):
        boxes = [
            (index * 100, 10, index * 100 + 80, 196)
            for index in range(20)
        ]

        class FakeFastExtractor:
            models = {auto_marker.FAST_ROOT_PRIMARY_MODEL}
            active_models = (auto_marker.FAST_ROOT_PRIMARY_MODEL,)
            face_model = None

            @staticmethod
            def extract_feature(_crop, model_names=None):
                return {name: np.ones(1) for name in (model_names or ())}

        self.matcher.ai_extractor = FakeFastExtractor()
        self.matcher._detect_result_grid = lambda _image: boxes
        self.matcher._rank_features = lambda _features, _models: [
            {"score": 0.9}
        ]
        self.matcher._classify_features = lambda _features, **_kwargs: {
            "query": "Query_1",
            "ref_name": "ref_0.png",
            "score": 0.9,
        }

        result = self.matcher._find_matches_fast_root(
            np.zeros((220, 2000, 3), dtype=np.uint8)
        )

        self.assertEqual(len(result), 5)
        self.assertNotIn(boxes[0], [match["bbox"] for match in result])
        self.assertTrue(
            {match["bbox"][0] for match in result}.issubset(
                {box[0] for box in boxes[1:]}
            )
        )

    def test_fast_root_enforces_single_query_before_cap(self):
        """A card classified under a secondary identity must not draw a box in
        fast mode either — it would color blue and exceed the folder's N-1 cap.

        Regression for the extra blue frame: the fast root classified each card
        independently without the single-identity policy the full scan applies.
        """
        boxes = [
            (index * 100, 10, index * 100 + 80, 196)
            for index in range(20)
        ]

        class FakeFastExtractor:
            models = {auto_marker.FAST_ROOT_PRIMARY_MODEL}
            active_models = (auto_marker.FAST_ROOT_PRIMARY_MODEL,)
            face_model = None

            @staticmethod
            def extract_feature(_crop, model_names=None):
                return {name: np.ones(1) for name in (model_names or ())}

        self.matcher.ai_extractor = FakeFastExtractor()
        self.matcher._detect_result_grid = lambda _image: boxes
        self.matcher._rank_features = lambda _features, _models: [
            {"score": 0.9}
        ]
        from unittest.mock import patch

        primary = {
            "query": "Query_1",
            "ref_name": "ref_0.png",
            "score": 0.9,
        }
        intruder = {
            "query": "Query_2",
            "ref_name": "ref_0.png",
            "score": 0.95,
        }
        # The intruder scores higher per card, but Query_1 wins by card count
        # (counts decide in dominant_query_only). Both survive the per-card
        # classifier, so a missing dominant_query_only step would keep the
        # Query_2 card and draw it as a blue box past the folder cap.
        self.matcher.reference_images = {
            "Query_1": [(f"ref_{i}.png", None, {}) for i in range(6)],
            "Query_2": [(f"ref_{i}.png", None, {}) for i in range(6)],
        }
        calls = {"n": 0}

        def classify(features, **_kwargs):
            calls["n"] += 1
            result = dict(intruder if calls["n"] == 1 else primary)
            result["bbox"] = (calls["n"] * 100, 10, calls["n"] * 100 + 80, 196)
            return result

        self.matcher._classify_features = classify
        with patch.object(auto_marker, "ENFORCE_SINGLE_QUERY", True):
            result = self.matcher._find_matches_fast_root(
                np.zeros((220, 2000, 3), dtype=np.uint8)
            )

        self.assertTrue(result)
        self.assertTrue(
            all(match["query"] == "Query_1" for match in result),
            "secondary identity card must be dropped before the N-1 cap",
        )
        self.assertEqual(len(result), 5)

    def test_fast_root_drops_secondary_query_before_result_cap(self):
        """A high-scoring card from another Query must not draw a box."""
        boxes = [
            (index * 100, 10, index * 100 + 80, 196)
            for index in range(5)
        ]

        class FakeFastExtractor:
            models = {auto_marker.FAST_ROOT_PRIMARY_MODEL}
            active_models = (auto_marker.FAST_ROOT_PRIMARY_MODEL,)
            face_model = None

            @staticmethod
            def extract_feature(_crop, model_names=None):
                return {name: np.ones(1) for name in (model_names or ())}

        self.matcher.ai_extractor = FakeFastExtractor()
        self.matcher._detect_result_grid = lambda _image: boxes
        self.matcher._rank_features = lambda _features, _models: [
            {"score": 0.9}
        ]
        self.matcher.reference_images = {
            "Query_3": [(f"q3_{i}.png", None, {}) for i in range(4)],
            "Query_13": [(f"q13_{i}.png", None, {}) for i in range(4)],
        }

        calls = {"count": 0}

        def classify(_features, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return {"query": "Query_13", "score": 0.95}
            return {"query": "Query_3", "score": 0.75}

        self.matcher._classify_features = classify
        result = self.matcher._find_matches_fast_root(
            np.zeros((220, 500, 3), dtype=np.uint8)
        )

        self.assertEqual(len(result), 3)
        self.assertEqual({match["query"] for match in result}, {"Query_3"})


class TestFastRootSourceScoping(unittest.TestCase):
    """Fast Root must scope result classification to the source Query."""

    def setUp(self):
        self.matcher = auto_marker.TemplateMatcher.__new__(
            auto_marker.TemplateMatcher
        )
        self.matcher.target_query = None
        self.matcher.reference_images = {
            "Query_1": [("ref_1.png", None, {})],
            "Query_2": [("ref_2.png", None, {})],
        }

    def test_source_query_is_scoped_only_during_fast_pass(self):
        boxes = [(0, 0, 80, 180), (100, 0, 180, 180)]
        seen = {}

        self.matcher._identify_query_by_source_ai = lambda _image: "Query_1"

        def fake_fast_pass(_image):
            seen["queries"] = set(self.matcher.reference_images)
            return [{"query": "Query_1", "bbox": boxes[1], "score": 0.75}]

        self.matcher._find_matches_fast_root = fake_fast_pass

        from unittest.mock import patch

        with patch.object(auto_marker, "FAST_ROOT_MODE", True):
            result = self.matcher._find_matches_inner(
                np.zeros((200, 200, 3), dtype=np.uint8), None
            )

        self.assertEqual(seen["queries"], {"Query_1"})
        self.assertEqual(set(self.matcher.reference_images), {"Query_1", "Query_2"})
        self.assertEqual(result[0]["query"], "Query_1")


# ---------------------------------------------------------------------------
# Per-card OCR timestamp gate
# ---------------------------------------------------------------------------
class TestCardTimestampGate(unittest.TestCase):
    """A card is rejected only when its own printed time is read confidently
    and matches no reference time for that identity. Unreadable times, or
    queries without reference times, are kept so the AI score decides."""

    def setUp(self):
        self.matcher = auto_marker.TemplateMatcher.__new__(
            auto_marker.TemplateMatcher
        )
        self.matcher.reference_timestamps = {
            "Query_1": ["12:12 PM", "11:43 AM", "11:44 AM", "12:15 PM"]
        }
        # This suite exercises only the per-result timestamp gate. Source-card
        # timestamp handling has its own tests and would consume an OCR value.
        self.matcher._detect_result_grid = lambda _image: []
        self.screen = np.zeros((300, 2000, 3), dtype=np.uint8)

    def _run(self, card_times):
        matches = [
            {"bbox": (i * 100, 10, i * 100 + 80, 196), "query": "Query_1",
             "score": 0.9}
            for i in range(len(card_times))
        ]
        # Encode the expected OCR result into each crop so the test remains
        # deterministic while production OCR runs concurrently.
        self.screen.fill(0)
        for i, match in enumerate(matches):
            x1, y1, x2, y2 = match["bbox"]
            self.screen[y1:y2, x1:x2] = i + 1

        import auto_marker as am
        original = am.extract_reference_timestamp

        def fake_extract(crop):
            index = int(crop[0, 0, 0]) - 1
            return card_times[index]

        am.extract_reference_timestamp = fake_extract
        try:
            from unittest.mock import patch

            with patch_ocr_enabled(), patch.object(
                am, "_snap_box_to_card", side_effect=lambda _gray, bbox: bbox
            ):
                return self.matcher._filter_matches_by_card_timestamp(
                    matches, self.screen
                )
        finally:
            am.extract_reference_timestamp = original

    def _run_with_source(self, source_time, card_times, scores, references):
        self.matcher.reference_timestamps = {"Query_1": references}
        source_bbox = (1800, 10, 1880, 196)
        self.matcher._detect_result_grid = lambda _image: [source_bbox]
        matches = [
            {
                "bbox": (i * 100, 10, i * 100 + 80, 196),
                "query": "Query_1",
                "score": scores[i],
            }
            for i in range(len(card_times))
        ]
        self.screen.fill(0)
        for i, match in enumerate(matches):
            x1, y1, x2, y2 = match["bbox"]
            self.screen[y1:y2, x1:x2] = i + 1
        sx1, sy1, sx2, sy2 = source_bbox
        self.screen[sy1:sy2, sx1:sx2] = 250

        import auto_marker as am
        original = am.extract_reference_timestamp

        def fake_extract(crop):
            marker = int(crop[0, 0, 0])
            if marker == 250:
                return source_time
            return card_times[marker - 1]

        am.extract_reference_timestamp = fake_extract
        try:
            from unittest.mock import patch

            with patch_ocr_enabled(), patch.object(
                am, "_snap_box_to_card", side_effect=lambda _gray, bbox: bbox
            ):
                return self.matcher._filter_matches_by_card_timestamp(
                    matches, self.screen
                )
        finally:
            am.extract_reference_timestamp = original

    def test_keeps_matching_and_unreadable_rejects_stranger(self):
        result = self._run(["12:12 PM", "11:43 AM", None, "11:32 AM"])
        kept_x = {m["bbox"][0] for m in result}
        self.assertIn(0, kept_x)     # 12:12 PM matches
        self.assertIn(100, kept_x)   # 11:43 AM matches
        self.assertIn(200, kept_x)   # unreadable → kept
        self.assertNotIn(300, kept_x)  # 11:32 AM stranger → rejected

    def test_query_without_references_keeps_all(self):
        self.matcher.reference_timestamps = {}
        result = self._run(["3:00 PM", "9:99 ZZ"])
        self.assertEqual(len(result), 2)

    def test_different_source_time_still_limits_single_reference_timestamp(self):
        result = self._run_with_source(
            source_time="12:18 PM",
            card_times=["12:05 PM", "12:05 PM", "12:05 PM", "12:20 PM"],
            scores=[0.80, 0.95, 0.85, 0.90],
            references=["12:20 PM", "12:18 PM", "12:05 PM", "12:04 PM", "12:04 PM"],
        )

        kept_x = {match["bbox"][0] for match in result}
        self.assertEqual(kept_x, {100, 300})

    def test_source_consumes_one_slot_from_its_timestamp(self):
        result = self._run_with_source(
            source_time="12:05 PM",
            card_times=["12:05 PM", "12:05 PM"],
            scores=[0.90, 0.95],
            references=["12:05 PM", "12:05 PM"],
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["bbox"][0], 100)


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
        auto_marker.log("WARN", "some warning")


# ---------------------------------------------------------------------------
# Clipboard change token
# ---------------------------------------------------------------------------
class TestClipboardToken(unittest.TestCase):
    """A repeated screenshot still counts as new after ShareX recaptures it."""

    def test_sequence_number_distinguishes_identical_pixels(self):
        from unittest.mock import patch

        payload = b"same screenshot pixels"
        with patch.object(auto_marker, "_get_clipboard_sequence_number", return_value=10):
            first = auto_marker._clipboard_token(payload)
        with patch.object(auto_marker, "_get_clipboard_sequence_number", return_value=11):
            second = auto_marker._clipboard_token(payload)
        self.assertNotEqual(first, second)

    def test_falls_back_to_pixel_hash_without_windows_sequence(self):
        from unittest.mock import patch

        with patch.object(auto_marker, "_get_clipboard_sequence_number", return_value=None):
            first = auto_marker._clipboard_token(b"same pixels")
            second = auto_marker._clipboard_token(b"same pixels")
        self.assertEqual(first, second)


class TestClipboardImageFormats(unittest.TestCase):
    """Companion clipboard formats must not hide a valid ShareX image."""

    def test_reads_image_without_metadata_blacklist(self):
        from unittest.mock import patch
        from PIL import Image

        image = Image.new("RGB", (2, 2), "white")
        with patch.object(auto_marker.ImageGrab, "grabclipboard", return_value=image):
            self.assertIs(auto_marker.get_clipboard_image(), image)

    def test_hashes_image_without_metadata_blacklist(self):
        from unittest.mock import patch
        from PIL import Image

        image = Image.new("RGB", (2, 2), "white")
        with patch.object(auto_marker.ImageGrab, "grabclipboard", return_value=image), patch.object(
            auto_marker, "_get_clipboard_sequence_number", return_value=99
        ):
            self.assertIsNotNone(auto_marker.get_clipboard_image_hash())


# ---------------------------------------------------------------------------
# find_matches fast-root fallback — "select all folders" mode
# ---------------------------------------------------------------------------
class TestFindMatchesFastRootFallback(unittest.TestCase):
    """When the fast grid classifier accepts nothing, find_matches must fall
    back to the reliable template scan instead of returning no boxes.

    Regression for the bug where all-folders mode drew no boxes on screenshots
    that single-folder mode handled correctly: FAST_ROOT_MODE short-circuited
    find_matches on an empty fast result, which simply meant "this heuristic
    rejected every card", not "there is no match here".
    """

    def setUp(self):
        self.matcher = auto_marker.TemplateMatcher.__new__(
            auto_marker.TemplateMatcher
        )
        self.matcher.target_query = None
        self.matcher.reference_images = {"Query_1": [], "Query_2": []}
        self.matcher.query_images = {}
        self.matcher.query_thresholds = {}
        self.matcher.ai_extractor = _FakeExtractor()

    def test_empty_fast_result_falls_back_to_template_scan(self):
        from unittest.mock import patch

        with patch.object(auto_marker, "FAST_ROOT_MODE", True):
            result = self.matcher.find_matches(np.zeros((200, 200, 3), dtype=np.uint8))
        self.assertEqual(result, [])

    def test_nonempty_fast_result_is_used_directly(self):
        from unittest.mock import patch

        accepted = [{"bbox": (0, 0, 50, 50), "score": 0.9, "query": "Query_1"}]
        self.matcher._find_matches_fast_root = lambda _image: accepted
        with patch.object(auto_marker, "FAST_ROOT_MODE", True):
            result = self.matcher.find_matches(
                np.zeros((200, 200, 3), dtype=np.uint8)
            )
        self.assertEqual(result, accepted)


# ---------------------------------------------------------------------------
# dominant_query_only — single-target domain rule
# ---------------------------------------------------------------------------
class TestDominantQueryOnly(unittest.TestCase):
    """auto_marker.dominant_query_only keeps only the query with the most boxes."""

    def test_empty_input(self):
        self.assertEqual(auto_marker.dominant_query_only([]), [])

    def test_keeps_only_dominant_query_by_count(self):
        matches = [
            {"query": "Query_1", "bbox": (0, 0, 10, 10), "score": 0.6},
            {"query": "Query_2", "bbox": (20, 0, 30, 10), "score": 0.9},
            {"query": "Query_1", "bbox": (0, 20, 10, 30), "score": 0.7},
        ]
        result = auto_marker.dominant_query_only(matches)
        self.assertTrue(all(m["query"] == "Query_1" for m in result))
        self.assertEqual(len(result), 2)

    def test_tie_break_by_total_score(self):
        matches = [
            {"query": "Query_1", "bbox": (0, 0, 10, 10), "score": 0.5},
            {"query": "Query_1", "bbox": (0, 20, 10, 30), "score": 0.5},
            {"query": "Query_2", "bbox": (20, 0, 30, 10), "score": 0.8},
            {"query": "Query_2", "bbox": (20, 20, 30, 30), "score": 0.8},
        ]
        result = auto_marker.dominant_query_only(matches)
        self.assertTrue(all(m["query"] == "Query_2" for m in result))
        self.assertEqual(len(result), 2)

    def test_single_query_untouched(self):
        matches = [
            {"query": "Query_1", "bbox": (0, 0, 10, 10), "score": 0.6},
            {"query": "Query_1", "bbox": (0, 20, 10, 30), "score": 0.7},
        ]
        self.assertEqual(auto_marker.dominant_query_only(matches), matches)


if __name__ == "__main__":
    unittest.main()
