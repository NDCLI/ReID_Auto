"""Regression tests for the small-card OCR backend and fallbacks."""

import unittest
import numpy as np

import ocr_utils


class _FakeRapidResult:
    def __init__(self, text, score=0.9):
        self.txts = [text] if text else []
        self.scores = [score] if text else []


class _FakeRapidEngine:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def __call__(self, _image, **kwargs):
        self.calls.append(kwargs)
        return next(self.results, _FakeRapidResult(None))


class TestSmallCardOCR(unittest.TestCase):
    def setUp(self):
        self.image = np.zeros((196, 80, 3), dtype=np.uint8)
        self.original_available = ocr_utils._RAPIDOCR_AVAILABLE
        self.original_engine = ocr_utils._RAPIDOCR_ENGINE
        self.original_winocr = ocr_utils._WINOCR_AVAILABLE
        self.original_bottom = ocr_utils._winocr_bottom

    def tearDown(self):
        ocr_utils._RAPIDOCR_AVAILABLE = self.original_available
        ocr_utils._RAPIDOCR_ENGINE = self.original_engine
        ocr_utils._WINOCR_AVAILABLE = self.original_winocr
        ocr_utils._winocr_bottom = self.original_bottom

    def test_rapidocr_uses_consensus_instead_of_first_bad_read(self):
        engine = _FakeRapidEngine([
            _FakeRapidResult("2:15 PM", 0.99),
            _FakeRapidResult("12:15 PM", 0.88),
            _FakeRapidResult("12:15 PM", 0.87),
        ])
        ocr_utils._RAPIDOCR_AVAILABLE = True
        ocr_utils._RAPIDOCR_ENGINE = engine

        self.assertEqual(
            ocr_utils.extract_reference_timestamp(self.image),
            "12:15 PM",
        )

    def test_windows_ocr_is_used_when_rapidocr_is_unavailable(self):
        ocr_utils._RAPIDOCR_AVAILABLE = False
        ocr_utils._RAPIDOCR_ENGINE = None
        ocr_utils._WINOCR_AVAILABLE = True
        ocr_utils._winocr_bottom = lambda *_args, **_kwargs: "12:12 PM"

        self.assertEqual(
            ocr_utils.extract_reference_timestamp(self.image),
            "12:12 PM",
        )

    def test_empty_rapidocr_result_is_non_fatal(self):
        ocr_utils._RAPIDOCR_AVAILABLE = True
        ocr_utils._RAPIDOCR_ENGINE = _FakeRapidEngine([])
        ocr_utils._WINOCR_AVAILABLE = False

        self.assertIsNone(ocr_utils.extract_reference_timestamp(self.image))

    def test_card_ocr_skips_detection_and_classification(self):
        engine = _FakeRapidEngine([_FakeRapidResult("12:15 PM", 0.95)])
        ocr_utils._RAPIDOCR_AVAILABLE = True
        ocr_utils._RAPIDOCR_ENGINE = engine

        self.assertEqual(
            ocr_utils.extract_reference_timestamp(self.image),
            "12:15 PM",
        )
        self.assertEqual(
            engine.calls[0],
            {"use_det": False, "use_cls": False, "use_rec": True},
        )

    def test_card_ocr_bands_are_ordered_tightest_first(self):
        variants = list(ocr_utils._rapidocr_variants(self.image))
        heights = [v.shape[0] for v in variants]

        # 196px * 18%, 20%, 22%, each enlarged 8x.
        self.assertEqual(heights[:3], [288, 320, 352])
        # Tightest-first is the invariant the consensus vote depends on: the
        # caller stops once two bands agree, so a wide band must never vote
        # before the tight bands that read the timestamp reliably. Measured on
        # real cards, the 18% band was correct 8/8 while 28% was correct 2/8,
        # and the old 18/28/20 order let that band decide a 2-1 vote.
        self.assertEqual(heights, sorted(heights))

    def test_wide_bands_cannot_outvote_tight_bands(self):
        # Two tight bands read the true time, then a wide band misreads it by
        # dropping the leading digit. The consensus must stop at the agreeing
        # tight pair, which is what keeps a real "11:55 AM" card from being
        # filed as "1:55 AM" and dropped from its identity's time bucket.
        engine = _FakeRapidEngine([
            _FakeRapidResult("11:55 AM", 0.94),
            _FakeRapidResult("11:55 AM", 0.90),
            _FakeRapidResult("1:55 AM", 0.99),
        ])
        ocr_utils._RAPIDOCR_AVAILABLE = True
        ocr_utils._RAPIDOCR_ENGINE = engine

        self.assertEqual(
            ocr_utils.extract_reference_timestamp(self.image),
            "11:55 AM",
        )
        # Stopped at the agreeing pair instead of consuming the wide band.
        self.assertEqual(len(engine.calls), 2)


class TestTimestampMatching(unittest.TestCase):
    def test_card_times_must_match_exactly_by_default(self):
        self.assertTrue(ocr_utils.timestamps_match("12:12 PM", "12:12 PM"))
        self.assertFalse(ocr_utils.timestamps_match("12:12 PM", "12:13 PM"))


if __name__ == "__main__":
    unittest.main()
