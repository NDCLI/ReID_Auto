"""Regression test: Fast Root fallback recovers OCR-unreadable valid candidates."""

import unittest
from unittest.mock import patch
import numpy as np
import auto_marker


class _FakeExtractor:
    """Minimal fake extractor for testing shortlist and full ensemble paths."""

    def __init__(self):
        self.models = {"osnet_lct_0277": None, "osnet_0288": None, "osnet_lct_0286": None}
        self.active_models = tuple(self.models.keys())
        self.weights = {name: 1.0 for name in self.models}
        self.face_model = None

    def extract_feature(self, _image, model_names=None):
        selected = model_names or self.active_models
        return {name: np.array([0.1, 0.2]) for name in selected if name in self.models}

    def compute_similarity(self, _feat_a, _feat_b):
        return 0.5

    def ensemble_similarity(self, candidate_features, ref_features):
        individual = {
            name: 0.5 for name in candidate_features if name in ref_features
        }
        combined = 0.5
        return combined, individual


class TestFastRootFallback(unittest.TestCase):
    """Fast Root must recover OCR-unreadable cards that fall slightly below
    the primary-model threshold but pass full ensemble classification."""

    def setUp(self):
        self.matcher = auto_marker.TemplateMatcher.__new__(auto_marker.TemplateMatcher)
        self.matcher.target_query = None
        self.matcher.reference_images = {
            "Query_8": [
                ("ref_51.png", np.zeros((100, 80, 3), dtype=np.uint8), {"osnet_lct_0277": np.array([0.3, 0.4]), "osnet_0288": np.array([0.3, 0.4]), "osnet_lct_0286": np.array([0.3, 0.4])}),
            ]
        }
        self.matcher.query_images = {}
        self.matcher.query_thresholds = {}
        self.matcher.reference_timestamps = {}
        self.matcher.reference_timestamps_by_ref = {}
        self.matcher.ai_extractor = _FakeExtractor()

    def test_fallback_candidate_with_unreadable_ocr_reaches_ensemble(self):
        """A card with OCR=None and primary score 0.40 should enter fallback,
        then be evaluated by the full ensemble."""

        with patch.object(auto_marker, "FAST_ROOT_MODE", True), \
             patch.object(auto_marker, "FAST_ROOT_PRIMARY_MODEL", "osnet_lct_0277"), \
             patch.object(auto_marker, "FAST_ROOT_SHORTLIST_THRESHOLD", 0.45), \
             patch.object(auto_marker, "ENABLE_OCR_TIMESTAMP_FILTER", True), \
             patch.object(auto_marker, "extract_reference_timestamp", return_value=None):

            # Simulate a grid with one card
            fake_grid = [(100, 50, 180, 250), (300, 50, 380, 250)]

            with patch.object(self.matcher, "_detect_result_grid", return_value=fake_grid):
                # Primary model returns 0.40 (below 0.45, but within fallback range)
                def mock_rank(features, model_names=None, reference_overrides=None):
                    return [{"query": "Query_8", "ref_name": "ref_51.png", "score": 0.40}]

                with patch.object(self.matcher, "_rank_features", side_effect=mock_rank):
                    # Full ensemble accepts it
                    def mock_classify(features, allow_time_rescue=False, reference_overrides=None,
                                      candidate_bgr=None):
                        return {
                            "query": "Query_8",
                            "ref_name": "ref_51.png",
                            "score": 0.70,
                            "best_reference_score": 0.68,
                            "model_scores": {"osnet_lct_0277": 0.70, "osnet_0288": 0.70, "osnet_lct_0286": 0.70},
                            "margin": 1.70,
                            "threshold": 0.68,
                            "reference_threshold": 0.62,
                            "source": "body",
                        }

                    with patch.object(self.matcher, "_classify_features", side_effect=mock_classify), \
                         patch.object(self.matcher, "_filter_matches_by_card_timestamp", side_effect=lambda m, _: m), \
                         patch.object(self.matcher, "_limit_and_align_matches", side_effect=lambda m: m), \
                         patch.object(auto_marker, "ENFORCE_SINGLE_QUERY", False):

                        result = self.matcher._find_matches_fast_root(
                            np.zeros((300, 500, 3), dtype=np.uint8)
                        )

                        self.assertIsNotNone(result)
                        self.assertEqual(len(result), 1)
                        self.assertEqual(result[0]["query"], "Query_8")
                        self.assertEqual(result[0]["score"], 0.70)

    def test_fallback_rejects_weak_ensemble_score(self):
        """A fallback candidate that fails ensemble gates must still be rejected."""

        with patch.object(auto_marker, "FAST_ROOT_MODE", True), \
             patch.object(auto_marker, "FAST_ROOT_PRIMARY_MODEL", "osnet_lct_0277"), \
             patch.object(auto_marker, "FAST_ROOT_SHORTLIST_THRESHOLD", 0.45), \
             patch.object(auto_marker, "ENABLE_OCR_TIMESTAMP_FILTER", True), \
             patch.object(auto_marker, "extract_reference_timestamp", return_value=None):

            fake_grid = [(100, 50, 180, 250), (300, 50, 380, 250)]

            with patch.object(self.matcher, "_detect_result_grid", return_value=fake_grid):
                def mock_rank(features, model_names=None, reference_overrides=None):
                    return [{"query": "Query_8", "ref_name": "ref_51.png", "score": 0.38}]

                with patch.object(self.matcher, "_rank_features", side_effect=mock_rank):
                    # Ensemble rejects it (too weak)
                    def mock_classify(features, allow_time_rescue=False, reference_overrides=None,
                                      candidate_bgr=None):
                        return None

                    with patch.object(self.matcher, "_classify_features", side_effect=mock_classify), \
                         patch.object(self.matcher, "_filter_matches_by_card_timestamp", side_effect=lambda m, _: m), \
                         patch.object(self.matcher, "_limit_and_align_matches", side_effect=lambda m: m), \
                         patch.object(auto_marker, "ENFORCE_SINGLE_QUERY", False):

                        result = self.matcher._find_matches_fast_root(
                            np.zeros((300, 500, 3), dtype=np.uint8)
                        )

                        self.assertIsNotNone(result)
                        self.assertEqual(len(result), 0)

    def test_fallback_limited_to_five_candidates(self):
        """Fallback queue is capped at 5 to avoid performance issues."""

        with patch.object(auto_marker, "FAST_ROOT_MODE", True), \
             patch.object(auto_marker, "FAST_ROOT_PRIMARY_MODEL", "osnet_lct_0277"), \
             patch.object(auto_marker, "FAST_ROOT_SHORTLIST_THRESHOLD", 0.45), \
             patch.object(auto_marker, "ENABLE_OCR_TIMESTAMP_FILTER", True), \
             patch.object(auto_marker, "extract_reference_timestamp", return_value=None):

            # Simulate 8 cards
            fake_grid = [(100, 50, 180, 250)] + [(i * 100, 300, i * 100 + 80, 500) for i in range(8)]

            with patch.object(self.matcher, "_detect_result_grid", return_value=fake_grid):
                rank_call_count = 0

                def mock_rank(features, model_names=None, reference_overrides=None):
                    nonlocal rank_call_count
                    rank_call_count += 1
                    # All return borderline fallback scores
                    return [{"query": "Query_8", "ref_name": "ref_51.png", "score": 0.38}]

                classify_call_count = 0

                def mock_classify(features, allow_time_rescue=False, reference_overrides=None,
                                      candidate_bgr=None):
                    nonlocal classify_call_count
                    classify_call_count += 1
                    return None

                with patch.object(self.matcher, "_rank_features", side_effect=mock_rank), \
                     patch.object(self.matcher, "_classify_features", side_effect=mock_classify), \
                     patch.object(self.matcher, "_filter_matches_by_card_timestamp", side_effect=lambda m, _: m), \
                     patch.object(self.matcher, "_limit_and_align_matches", side_effect=lambda m: m), \
                     patch.object(auto_marker, "ENFORCE_SINGLE_QUERY", False):

                    result = self.matcher._find_matches_fast_root(
                        np.zeros((600, 900, 3), dtype=np.uint8)
                    )

                    # 8 cards ranked, but only 5 fallback candidates classified
                    self.assertEqual(rank_call_count, 8)
                    self.assertLessEqual(classify_call_count, 5)


if __name__ == "__main__":
    unittest.main()
