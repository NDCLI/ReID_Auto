"""Unit tests for config.py — verify all configuration constants exist and have valid types."""

import unittest
import config


class TestConfigConstants(unittest.TestCase):
    """Ensure every config constant exists with the expected type and range."""

    def test_paths_are_strings(self):
        self.assertIsInstance(config.BASE_DIR, str)
        self.assertIsInstance(config.QUERIES_DIR, str)
        self.assertIsInstance(config.OUTPUT_DIR, str)

    def test_match_threshold_range(self):
        self.assertIsInstance(config.MATCH_THRESHOLD, float)
        self.assertTrue(0.0 <= config.MATCH_THRESHOLD <= 1.0)

    def test_reid_models_is_list(self):
        self.assertIsInstance(config.REID_MODELS, list)
        self.assertGreater(len(config.REID_MODELS), 0)
        for model in config.REID_MODELS:
            self.assertIn("name", model)
            self.assertIn("model", model)
            self.assertIn("weight", model)

    def test_variant_excludes_transreid(self):
        """This build is the TransReID-free OSNet ensemble."""
        names = [model["name"] for model in config.REID_MODELS]
        self.assertNotIn("transreid", names)
        self.assertEqual(names, ["osnet_0288", "osnet_lct_0277", "osnet_lct_0286"])

    def test_fast_root_primary_is_a_configured_model(self):
        names = [model["name"] for model in config.REID_MODELS]
        self.assertIn(config.FAST_ROOT_PRIMARY_MODEL, names)

    def test_variant_identity_is_namespaced(self):
        """Must not collide with the original app's mutex or model cache."""
        self.assertNotEqual(config.MODEL_CACHE_DIRNAME, "ReIDAuto")
        self.assertIn("OSNet", config.MODEL_CACHE_DIRNAME)
        self.assertTrue(config.APP_MUTEX_NAME.startswith("Global\\"))
        self.assertNotIn("ReID_Auto_Draw_Mutex_Unique_998877", config.APP_MUTEX_NAME)

    def test_best_reference_threshold_calibrated_for_this_ensemble(self):
        """0.90 was tuned for TransReID and rejects almost every true match here."""
        self.assertLessEqual(config.AI_BEST_REFERENCE_THRESHOLD, 0.70)

    def test_single_query_rule_enabled(self):
        """Each screenshot is one target person, so only the dominant query is drawn."""
        self.assertIs(config.ENFORCE_SINGLE_QUERY, True)

    def test_ai_thresholds(self):
        self.assertTrue(0.0 <= config.AI_MATCH_THRESHOLD <= 1.0)
        self.assertTrue(0.0 <= config.AI_MATCH_MARGIN <= 1.0)
        self.assertTrue(0.0 <= config.AI_BEST_REFERENCE_THRESHOLD <= 1.0)
        self.assertIsInstance(config.AI_TOP_K_REFERENCES, int)
        self.assertGreater(config.AI_TOP_K_REFERENCES, 0)

    def test_auto_calibration_thresholds(self):
        self.assertIsInstance(config.AUTO_CALIBRATION, bool)
        self.assertTrue(config.AUTO_AI_THRESHOLD_FLOOR <= config.AUTO_AI_THRESHOLD_CEILING)

    def test_face_config(self):
        self.assertIsInstance(config.FACE_DETECTION_THRESHOLD, float)
        self.assertIsInstance(config.FACE_MATCH_THRESHOLD, float)
        self.assertIsInstance(config.FACE_MATCH_MARGIN, float)
        self.assertIsInstance(config.FACE_MIN_REFERENCES, int)
        self.assertIsInstance(config.FACE_FEATURE_NAME, str)

    def test_match_scales(self):
        self.assertIsInstance(config.MATCH_SCALES, list)
        for scale in config.MATCH_SCALES:
            self.assertIsInstance(scale, float)
            self.assertGreater(scale, 0)

    def test_drawing_parameters(self):
        self.assertIsInstance(config.BOX_THICKNESS, int)
        self.assertGreater(config.BOX_THICKNESS, 0)

    def test_poll_interval(self):
        self.assertIsInstance(config.POLL_INTERVAL, float)
        self.assertGreater(config.POLL_INTERVAL, 0)

    def test_ignore_ratios(self):
        self.assertTrue(0.0 <= config.IGNORE_LEFT_RATIO <= 1.0)
        self.assertTrue(0.0 <= config.IGNORE_BOTTOM_RATIO <= 1.0)

    def test_fast_root_config(self):
        self.assertIsInstance(config.FAST_ROOT_MODE, bool)
        self.assertIsInstance(config.FAST_ROOT_PRIMARY_MODEL, str)
        self.assertTrue(0.0 <= config.FAST_ROOT_SHORTLIST_THRESHOLD <= 1.0)
        self.assertIsInstance(config.FAST_ROOT_MAX_ROWS, int)


if __name__ == "__main__":
    unittest.main()
