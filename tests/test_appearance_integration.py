"""Integration tests: the LBP appearance signal as a tie-breaker only.

Appearance is consulted at exactly one place — the ``margin < AI_MATCH_MARGIN``
gate in ``TemplateMatcher._classify_features`` — and is only allowed to turn a
rejected top1-vs-top2 tie into an acceptance. These tests pin down both halves of
that contract: that it rescues when all three conditions hold, and that it stays
silent everywhere else (including every absolute ReID gate).

Style follows ``TestBestReferenceRejection`` in ``test_auto_marker.py``: a bare
``__new__`` matcher with a score-only fake extractor, so no OpenVINO model loads.
"""

import unittest
from unittest.mock import MagicMock, patch

import numpy as np

import app_gui
import auto_marker
from appearance_extractor import AppearanceExtractor


def textured_crop(h=190, w=80, period=4, seed=0):
    """A standing-card-shaped crop with real texture (passes the std guard)."""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    cols = (np.arange(w) // period) % 2
    img[:, cols == 0] = 40
    img[:, cols == 1] = 210
    img = np.clip(img.astype(np.int16) + rng.integers(-8, 9, img.shape), 0, 255)
    return img.astype(np.uint8)


class _FakeExtractor:
    """Score-only body extractor; face never votes unless a test says so."""

    def __init__(self, face_scores=None):
        self.face_scores = face_scores or {}

    @staticmethod
    def ensemble_similarity(_candidate_features, reference_features):
        score = reference_features["score"]
        return score, {"fake_model": score}

    def face_similarity(self, _candidate_features, reference_features):
        return self.face_scores.get(id(reference_features))


class _AppearanceTestBase(unittest.TestCase):
    """A deliberate tie: two identities whose body scores are 0.01 apart.

    Both clear AI_MATCH_THRESHOLD and AI_BEST_REFERENCE_THRESHOLD, so the only
    reason this is rejected today is the margin gate — exactly the situation
    appearance is allowed to speak to.
    """

    TIE_HIGH = 0.80
    TIE_LOW = 0.79

    def setUp(self):
        self.matcher = auto_marker.TemplateMatcher.__new__(
            auto_marker.TemplateMatcher
        )
        self.matcher.ai_extractor = _FakeExtractor()
        self.matcher.query_thresholds = {}
        self.matcher.reference_timestamps = {}
        self.matcher.reference_images = {
            "Query_1": [
                ("a1.png", None, {"score": self.TIE_HIGH}),
                ("a2.png", None, {"score": self.TIE_HIGH}),
            ],
            "Query_2": [
                ("b1.png", None, {"score": self.TIE_LOW}),
                ("b2.png", None, {"score": self.TIE_LOW}),
            ],
        }
        self.matcher.appearance_extractor = AppearanceExtractor()
        self.candidate_features = {"fake_model": np.ones(1)}
        self.crop = textured_crop(seed=1)

        # Query_1's references look exactly like the candidate crop; Query_2's do
        # not. That is the appearance evidence the tie-break should find.
        # Same stripe pattern as the candidate but different noise, so the score
        # is high without being a degenerate exact 1.0.
        extractor = self.matcher.appearance_extractor
        self.matcher.reference_appearance = {
            ("Query_1", "a1.png"): extractor.extract_descriptor(
                textured_crop(seed=21)
            ),
            ("Query_1", "a2.png"): extractor.extract_descriptor(
                textured_crop(seed=22)
            ),
            ("Query_2", "b1.png"): extractor.extract_descriptor(
                textured_crop(period=17, seed=9)
            ),
            ("Query_2", "b2.png"): extractor.extract_descriptor(
                textured_crop(period=19, seed=10)
            ),
        }

    def classify(self, crop=None, allow_time_rescue=False):
        return self.matcher._classify_features(
            self.candidate_features,
            allow_time_rescue=allow_time_rescue,
            candidate_bgr=self.crop if crop is None else crop,
        )

    def assertTieIsRejected(self, result):
        self.assertIsNone(result)


class TestAppearanceDisabledByDefault(_AppearanceTestBase):
    def test_flag_off_leaves_the_tie_rejected(self):
        """Default config must behave exactly as it did before integration."""
        self.assertIs(auto_marker.ENABLE_APPEARANCE_MATCHING, False)
        self.assertTieIsRejected(self.classify())

    def test_bare_matcher_without_new_attributes_still_classifies(self):
        """~15 existing tests build the matcher with __new__ and never set these."""
        del self.matcher.appearance_extractor
        del self.matcher.reference_appearance
        with patch.object(auto_marker, "ENABLE_APPEARANCE_MATCHING", True):
            self.assertTieIsRejected(self.classify())


class TestAppearanceRescuesTie(_AppearanceTestBase):
    def setUp(self):
        super().setUp()
        self.enabled = patch.object(
            auto_marker, "ENABLE_APPEARANCE_MATCHING", True
        )
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def test_rescues_tie_when_appearance_agrees_clears_floor_and_margin(self):
        result = self.classify()
        self.assertIsNotNone(result)
        self.assertEqual(result["query"], "Query_1")
        self.assertEqual(result["source"], "body+appearance")
        self.assertTrue(result["appearance_rescue"])
        self.assertGreaterEqual(
            result["appearance_score"], auto_marker.APPEARANCE_SIMILARITY_FLOOR
        )
        self.assertGreaterEqual(
            result["appearance_margin"], auto_marker.APPEARANCE_RESCUE_MARGIN
        )

    def test_rescued_score_is_pure_reid_and_not_blended(self):
        """Downstream ranking must stay unaffected by the appearance number."""
        result = self.classify()
        self.assertAlmostEqual(result["score"], self.TIE_HIGH)
        self.assertAlmostEqual(result["best_reference_score"], self.TIE_HIGH)
        self.assertNotAlmostEqual(result["score"], result["appearance_score"])
        # ref_name stays ReID's winner so the OCR-vs-reference check still bites.
        self.assertIn(result["ref_name"], ("a1.png", "a2.png"))

    def test_margin_reported_is_the_reid_margin(self):
        result = self.classify()
        self.assertAlmostEqual(result["margin"], self.TIE_HIGH - self.TIE_LOW)
        self.assertLess(result["margin"], auto_marker.AI_MATCH_MARGIN)

    def test_no_rescue_when_appearance_prefers_a_different_query(self):
        """Appearance must agree with ReID's winner, not overrule it."""
        self.matcher.reference_appearance = {
            key: self.matcher.reference_appearance[
                ("Query_2", "b1.png") if key[0] == "Query_1" else ("Query_1", "a1.png")
            ]
            for key in self.matcher.reference_appearance
        }
        self.assertTieIsRejected(self.classify())

    def test_no_rescue_when_appearance_margin_is_too_small(self):
        """The floor alone admits half of different-person pairs; the gap decides."""
        with patch.object(auto_marker, "APPEARANCE_RESCUE_MARGIN", 0.9):
            self.assertTieIsRejected(self.classify())

    def test_no_rescue_when_appearance_is_below_the_floor(self):
        """Raise the floor just above what this candidate actually scores."""
        achieved = self.classify()["appearance_score"]
        with patch.object(
            auto_marker, "APPEARANCE_SIMILARITY_FLOOR", min(achieved + 0.001, 1.0)
        ):
            self.assertTieIsRejected(self.classify())

    def test_no_rescue_when_candidate_descriptor_is_unusable(self):
        """Flat and tiny crops are rejected by the extractor, not scored 1.0."""
        flat = np.full((190, 80, 3), 128, dtype=np.uint8)
        self.assertTieIsRejected(self.classify(crop=flat))
        tiny = np.zeros((10, 6, 3), dtype=np.uint8)
        self.assertTieIsRejected(self.classify(crop=tiny))

    def test_no_rescue_without_a_candidate_crop(self):
        result = self.matcher._classify_features(
            self.candidate_features, candidate_bgr=None
        )
        self.assertTieIsRejected(result)

    def test_wide_crop_is_ineligible(self):
        """Torso/leg bands are meaningless on a crop that is not a standing card."""
        landscape = textured_crop(h=80, w=190, seed=3)
        self.assertTieIsRejected(self.classify(crop=landscape))

    def test_identity_with_too_few_descriptors_is_not_scored(self):
        """One lucky reference must not be enough to break a tie."""
        with patch.object(auto_marker, "APPEARANCE_MIN_REFERENCES", 3):
            self.assertTieIsRejected(self.classify())

    def test_missing_reference_descriptors_decline_rather_than_raise(self):
        self.matcher.reference_appearance = {}
        self.assertTieIsRejected(self.classify())


class TestAbsoluteGatesAreNeverRescued(_AppearanceTestBase):
    """Appearance may break a tie between people; it may not lower a bar.

    Each of these gates asks "is this person known/unambiguous at all", which a
    clothing-texture histogram has no standing to answer. The best-reference gate
    especially: it exists to reject candidates that merely share a shirt.
    """

    def setUp(self):
        super().setUp()
        self.enabled = patch.object(
            auto_marker, "ENABLE_APPEARANCE_MATCHING", True
        )
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def _retie(self, high, low):
        for name, score in (("Query_1", high), ("Query_2", low)):
            self.matcher.reference_images[name] = [
                (ref[0], None, {"score": score})
                for ref in self.matcher.reference_images[name]
            ]

    def test_below_identity_threshold_is_not_rescued(self):
        gate = auto_marker.AI_MATCH_THRESHOLD
        self._retie(gate - 0.01, gate - 0.02)
        self.assertTieIsRejected(self.classify())

    def test_weak_best_reference_is_not_rescued(self):
        """The one gate appearance must never touch: same-shirt rejection."""
        gate = auto_marker.AI_BEST_REFERENCE_THRESHOLD
        self.matcher.query_thresholds = {"Query_1": 0.30, "Query_2": 0.30}
        self._retie(gate - 0.01, gate - 0.02)
        self.assertTieIsRejected(self.classify())

    def test_model_disagreement_is_not_rescued(self):
        """A second model preferring another identity blocks the tie-break."""
        matcher = self.matcher

        class _SplitExtractor(_FakeExtractor):
            @staticmethod
            def ensemble_similarity(_candidate, reference_features):
                score = reference_features["score"]
                # model_b ranks the identities in the opposite order.
                return score, {"model_a": score, "model_b": 1.0 - score}

        matcher.ai_extractor = _SplitExtractor()
        self.candidate_features = {"model_a": np.ones(1), "model_b": np.ones(1)}
        self.assertTieIsRejected(self.classify())


class TestFacePrecedence(_AppearanceTestBase):
    def test_face_result_wins_over_an_available_appearance_rescue(self):
        """Face is the stronger signal, so it keeps absolute precedence."""
        face_refs = self.matcher.reference_images["Query_1"]
        scores = {id(ref[2]): 0.95 for ref in face_refs}
        for ref in self.matcher.reference_images["Query_2"]:
            scores[id(ref[2])] = 0.10
        self.matcher.ai_extractor = _FakeExtractor(face_scores=scores)

        with patch.object(auto_marker, "ENABLE_APPEARANCE_MATCHING", True):
            result = self.classify()

        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "face")
        self.assertNotIn("appearance_rescue", result)


class TestReferenceDescriptorLoading(unittest.TestCase):
    """_store_reference_appearance is the only place descriptors are built."""

    def setUp(self):
        self.matcher = auto_marker.TemplateMatcher.__new__(
            auto_marker.TemplateMatcher
        )
        self.matcher.reference_appearance = {}
        self.matcher.appearance_extractor = AppearanceExtractor()

    def test_textured_reference_is_described(self):
        self.matcher._store_reference_appearance(
            "Query_1", "ref_1.png", textured_crop(seed=5)
        )
        payload = self.matcher.reference_appearance[("Query_1", "ref_1.png")]
        self.assertEqual(payload["descriptor"].shape, (512,))

    def test_flat_reference_is_skipped_without_raising(self):
        self.matcher._store_reference_appearance(
            "Query_1", "flat.png", np.full((190, 80, 3), 128, dtype=np.uint8)
        )
        self.assertEqual(self.matcher.reference_appearance, {})

    def test_disabled_extractor_stores_nothing(self):
        self.matcher.appearance_extractor = None
        self.matcher._store_reference_appearance(
            "Query_1", "ref_1.png", textured_crop(seed=6)
        )
        self.assertEqual(self.matcher.reference_appearance, {})

    def test_unreadable_image_is_ignored(self):
        self.matcher._store_reference_appearance("Query_1", "gone.png", None)
        self.assertEqual(self.matcher.reference_appearance, {})


class TestModelAgreementHelperIsBehaviourPreserving(unittest.TestCase):
    """_models_agree_on was extracted from _classify_features; same verdicts."""

    def test_single_model_always_agrees(self):
        agree = auto_marker.TemplateMatcher._models_agree_on
        self.assertTrue(agree({"only": [(0.9, "Query_2")]}, "Query_1"))

    def test_unanimous_models_agree(self):
        agree = auto_marker.TemplateMatcher._models_agree_on
        winners = {"a": [(0.9, "Query_1")], "b": [(0.8, "Query_1")]}
        self.assertTrue(agree(winners, "Query_1"))

    def test_split_models_disagree(self):
        agree = auto_marker.TemplateMatcher._models_agree_on
        winners = {"a": [(0.9, "Query_1")], "b": [(0.8, "Query_2")]}
        self.assertFalse(agree(winners, "Query_1"))

    def test_empty_rankings_are_ignored(self):
        agree = auto_marker.TemplateMatcher._models_agree_on
        winners = {"a": [(0.9, "Query_1")], "b": []}
        self.assertTrue(agree(winners, "Query_1"))


class TestDynamicAppearanceToggle(_AppearanceTestBase):
    """Verify runtime toggling on the TemplateMatcher instance."""

    def test_instance_flag_overrides_module_default(self):
        self.matcher.enable_appearance_matching = True
        result = self.classify()
        self.assertIsNotNone(result)
        self.assertTrue(result["appearance_rescue"])

        self.matcher.enable_appearance_matching = False
        self.assertTieIsRejected(self.classify())


class TestAppGuiAppearanceToggle(unittest.TestCase):
    """Verify AutoMarkerApp appearance toggle updates config, module and matcher."""

    def setUp(self):
        self.app = app_gui.AutoMarkerApp.__new__(app_gui.AutoMarkerApp)
        self.app.enable_appearance_matching = MagicMock()
        self.app.enable_appearance_matching.get.return_value = True
        self.app.show_osd = MagicMock()
        self.app.matcher = MagicMock()

    def test_on_toggle_appearance_matching(self):
        self.app.on_toggle_appearance_matching()
        self.assertTrue(self.app.matcher.enable_appearance_matching)
        self.assertTrue(auto_marker.ENABLE_APPEARANCE_MATCHING)
        self.app.show_osd.assert_called_with("👕 Khớp trang phục (LBP): BẬT")

        self.app.enable_appearance_matching.get.return_value = False
        self.app.on_toggle_appearance_matching()
        self.assertFalse(self.app.matcher.enable_appearance_matching)
        self.assertFalse(auto_marker.ENABLE_APPEARANCE_MATCHING)
        self.app.show_osd.assert_called_with("👕 Khớp trang phục (LBP): TẮT")


if __name__ == "__main__":
    unittest.main()
