"""Unit tests for the standalone appearance (LBP texture) extractor.

These tests need no external model: LBP is pure OpenCV + NumPy, so they run on
synthetic crops, mirroring the style of ``test_ocr_utils.py``.
"""

import unittest
import numpy as np

import appearance_extractor
from appearance_extractor import AppearanceExtractor, _DESCRIPTOR_SIZE


def striped_crop(h=120, w=48, period=4, seed=0):
    """A crop with a strong vertical-stripe texture (distinct LBP signature)."""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    cols = (np.arange(w) // period) % 2
    img[:, cols == 0] = 40
    img[:, cols == 1] = 210
    # A little noise so histograms are not degenerate single-bin spikes.
    img = np.clip(img.astype(np.int16) + rng.integers(-8, 9, img.shape), 0, 255)
    return img.astype(np.uint8)


def smooth_crop(h=120, w=48, value=128):
    """A flat, textureless crop (very different LBP signature from stripes)."""
    return np.full((h, w, 3), value, dtype=np.uint8)


class TestDescriptor(unittest.TestCase):
    def setUp(self):
        self.extractor = AppearanceExtractor()

    def test_is_valid(self):
        self.assertTrue(self.extractor.is_valid)

    def test_descriptor_shape_and_range(self):
        payload = self.extractor.extract_descriptor(striped_crop())
        self.assertIsNotNone(payload)
        desc = payload["descriptor"]
        self.assertEqual(desc.shape, (_DESCRIPTOR_SIZE,))
        self.assertTrue(np.all(desc >= 0.0))
        self.assertTrue(np.all(desc <= 1.0))
        # Each 256-bin band is L1-normalized (or zero if the band is empty).
        upper_sum = desc[:256].sum()
        lower_sum = desc[256:].sum()
        self.assertAlmostEqual(upper_sum, 1.0, places=4)
        self.assertAlmostEqual(lower_sum, 1.0, places=4)

    def test_identical_crops_score_near_one(self):
        crop = striped_crop(seed=1)
        a = self.extractor.extract_descriptor(crop)
        b = self.extractor.extract_descriptor(crop.copy())
        sim = AppearanceExtractor.compute_similarity(a, b)
        self.assertGreater(sim, 0.99)

    def test_different_textures_score_lower(self):
        a = self.extractor.extract_descriptor(striped_crop(period=3, seed=2))
        b = self.extractor.extract_descriptor(smooth_crop())
        same = self.extractor.extract_descriptor(striped_crop(period=3, seed=2))
        sim_diff = AppearanceExtractor.compute_similarity(a, b)
        sim_same = AppearanceExtractor.compute_similarity(a, same)
        self.assertLess(sim_diff, sim_same)
        self.assertLess(sim_diff, 0.9)

    def test_too_small_crop_returns_none(self):
        tiny = np.zeros((10, 6, 3), dtype=np.uint8)  # below both minimums
        self.assertIsNone(self.extractor.extract_descriptor(tiny))

    def test_flat_crops_are_rejected_instead_of_scoring_one(self):
        """A flat region sets every LBP bit, so two blank crops would score 1.0."""
        dark = self.extractor.extract_descriptor(smooth_crop(value=40))
        bright = self.extractor.extract_descriptor(smooth_crop(value=200))
        self.assertIsNone(dark)
        self.assertIsNone(bright)
        self.assertEqual(AppearanceExtractor.compute_similarity(dark, bright), 0.0)

    def test_descriptor_is_scale_normalized(self):
        """The same crop resized must still look like itself, not a stranger."""
        import cv2
        crop = striped_crop(h=180, w=80, period=6, seed=11)
        a = self.extractor.extract_descriptor(crop)
        b = self.extractor.extract_descriptor(
            cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_LINEAR)
        )
        self.assertIsNotNone(b)
        self.assertGreater(AppearanceExtractor.compute_similarity(a, b), 0.8)

    def test_unsupported_channel_count_returns_none_without_raising(self):
        two_channel = np.zeros((120, 48, 2), dtype=np.uint8)
        self.assertIsNone(self.extractor.extract_descriptor(two_channel))
        self.assertIsNone(
            self.extractor.extract_descriptor(np.zeros((4, 4, 4, 3), dtype=np.uint8))
        )

    def test_bgra_crop_supported(self):
        import cv2
        bgra = cv2.cvtColor(striped_crop(seed=12), cv2.COLOR_BGR2BGRA)
        payload = self.extractor.extract_descriptor(bgra)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["descriptor"].shape, (_DESCRIPTOR_SIZE,))

    def test_unknown_backend_is_invalid_and_extracts_nothing(self):
        other = AppearanceExtractor(model="hog")
        self.assertFalse(other.is_valid)
        self.assertIsNone(other.extract_descriptor(striped_crop(seed=13)))

    def test_descriptor_survives_npz_round_trip(self):
        """Integration will cache descriptors next to the .npz ReID features."""
        import io
        payload = self.extractor.extract_descriptor(striped_crop(seed=14))
        buffer = io.BytesIO()
        np.savez(buffer, descriptor=payload["descriptor"])
        buffer.seek(0)
        restored = {"descriptor": np.load(buffer)["descriptor"]}
        self.assertAlmostEqual(
            AppearanceExtractor.compute_similarity(payload, restored), 1.0, places=5
        )

    def test_none_and_empty_crop_return_none(self):
        self.assertIsNone(self.extractor.extract_descriptor(None))
        self.assertIsNone(
            self.extractor.extract_descriptor(np.zeros((0, 0, 3), dtype=np.uint8))
        )

    def test_grayscale_crop_supported(self):
        gray = striped_crop()[:, :, 0]  # (h, w) single channel
        payload = self.extractor.extract_descriptor(gray)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["descriptor"].shape, (_DESCRIPTOR_SIZE,))


class TestSimilarityContract(unittest.TestCase):
    def setUp(self):
        self.extractor = AppearanceExtractor()

    def test_similarity_is_symmetric_and_bounded(self):
        a = self.extractor.extract_descriptor(striped_crop(seed=3))
        b = self.extractor.extract_descriptor(smooth_crop())
        s_ab = AppearanceExtractor.compute_similarity(a, b)
        s_ba = AppearanceExtractor.compute_similarity(b, a)
        self.assertAlmostEqual(s_ab, s_ba, places=6)
        self.assertGreaterEqual(s_ab, 0.0)
        self.assertLessEqual(s_ab, 1.0)

    def test_missing_payload_scores_zero(self):
        a = self.extractor.extract_descriptor(striped_crop(seed=4))
        self.assertEqual(AppearanceExtractor.compute_similarity(a, None), 0.0)
        self.assertEqual(AppearanceExtractor.compute_similarity(None, a), 0.0)
        self.assertEqual(AppearanceExtractor.compute_similarity(None, None), 0.0)

    def test_mismatched_descriptor_shapes_score_zero(self):
        a = {"descriptor": np.zeros(512, dtype=np.float32)}
        b = {"descriptor": np.zeros(256, dtype=np.float32)}
        self.assertEqual(AppearanceExtractor.compute_similarity(a, b), 0.0)


if __name__ == "__main__":
    unittest.main()
