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

    def __call__(self, _image):
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


class TestTimestampMatching(unittest.TestCase):
    def test_card_times_must_match_exactly_by_default(self):
        self.assertTrue(ocr_utils.timestamps_match("12:12 PM", "12:12 PM"))
        self.assertFalse(ocr_utils.timestamps_match("12:12 PM", "12:13 PM"))


if __name__ == "__main__":
    unittest.main()
