"""Unit tests for ai_model.py — pure-math helpers that work without GPU/models."""

import unittest
import numpy as np

# ai_model imports cv2 and openvino at module level; cv2 is available but
# openvino may not be.  We only test the *math* helpers that never touch a
# model, so a missing openvino is fine — the module still loads because
# openvino is imported lazily inside __init__.
import ai_model


class TestComputeSimilarity(unittest.TestCase):
    """AI_FeatureExtractor.compute_similarity is a static cosine-similarity."""

    def test_identical_vectors(self):
        v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        self.assertAlmostEqual(ai_model.AI_FeatureExtractor.compute_similarity(v, v), 1.0, places=5)

    def test_orthogonal_vectors(self):
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        self.assertAlmostEqual(ai_model.AI_FeatureExtractor.compute_similarity(a, b), 0.0, places=5)

    def test_opposite_vectors(self):
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([-1.0, 0.0], dtype=np.float32)
        self.assertAlmostEqual(ai_model.AI_FeatureExtractor.compute_similarity(a, b), -1.0, places=5)

    def test_arbitrary_vectors(self):
        a = np.array([0.6, 0.8], dtype=np.float32)
        b = np.array([0.8, 0.6], dtype=np.float32)
        result = ai_model.AI_FeatureExtractor.compute_similarity(a, b)
        expected = float(np.dot(a, b))
        self.assertAlmostEqual(result, expected, places=5)


class TestOptionalModelFailures(unittest.TestCase):
    def test_runtime_error_skips_broken_optional_model(self):
        from unittest.mock import patch

        with (
            patch.object(
                ai_model,
                "_OpenVINOEmbeddingModel",
                side_effect=RuntimeError("unsupported model"),
            ),
            patch.object(ai_model.AI_FeatureExtractor, "_load_face_model"),
        ):
            extractor = ai_model.AI_FeatureExtractor(
                model_specs=[{"name": "broken", "model": "broken.xml"}]
            )

        self.assertFalse(extractor.is_valid)
        self.assertIn("unsupported model", extractor.errors["broken"])

class TestFaceSimilarity(unittest.TestCase):
    """AI_FeatureExtractor.face_similarity — static, no model needed."""

    def test_returns_none_when_missing(self):
        feat_a = {"body_model": np.array([1.0, 0.0])}
        feat_b = {"body_model": np.array([0.0, 1.0])}
        result = ai_model.AI_FeatureExtractor.face_similarity(feat_a, feat_b)
        self.assertIsNone(result)

    def test_returns_score_when_present(self):
        from config import FACE_FEATURE_NAME
        v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        feat_a = {FACE_FEATURE_NAME: v}
        feat_b = {FACE_FEATURE_NAME: v}
        score = ai_model.AI_FeatureExtractor.face_similarity(feat_a, feat_b)
        self.assertIsNotNone(score)
        self.assertAlmostEqual(score, 1.0, places=5)


class TestModelCacheIsolation(unittest.TestCase):
    """_resolve must never fall back to the original app's model cache."""

    def test_cache_fallback_uses_variant_directory(self):
        import os
        import tempfile
        from unittest.mock import patch

        import config

        with tempfile.TemporaryDirectory() as local_app_data:
            cache = os.path.join(local_app_data, config.MODEL_CACHE_DIRNAME, "models")
            os.makedirs(cache)
            cached_model = os.path.join(cache, "reid_0286.xml")
            with open(cached_model, "w", encoding="utf-8") as handle:
                handle.write("<net/>")

            # A sibling directory named after the original app must be ignored.
            original_cache = os.path.join(local_app_data, "ReIDAuto", "models")
            os.makedirs(original_cache)
            with open(os.path.join(original_cache, "reid_0286.xml"), "w", encoding="utf-8") as handle:
                handle.write("<net/>")

            with patch.dict(os.environ, {"LOCALAPPDATA": local_app_data}):
                resolved = ai_model._OpenVINOEmbeddingModel._resolve(
                    tempfile.gettempdir(), "reid_0286.xml"
                )

        self.assertEqual(resolved, cached_model)


class TestModelSpec(unittest.TestCase):
    """ModelSpec dataclass is frozen and has correct defaults."""

    def test_defaults(self):
        spec = ai_model.ModelSpec(name="test", model="test.xml")
        self.assertIsNone(spec.weights)
        self.assertEqual(spec.weight, 1.0)
        self.assertEqual(spec.device, "AUTO")

    def test_frozen(self):
        spec = ai_model.ModelSpec(name="test", model="test.xml")
        with self.assertRaises(AttributeError):
            spec.name = "changed"


if __name__ == "__main__":
    unittest.main()
