"""Regression tests for automatic Query assignment."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import query_organizer
from auto_marker import write_image_file


class _RejectingExtractor:
    is_valid = True

    @staticmethod
    def extract_feature(_image):
        return {"fake": np.ones(1, dtype=np.float32)}

    @staticmethod
    def ensemble_similarity(_candidate, _reference):
        return 0.0, {"fake": 0.0}

    @staticmethod
    def face_similarity(_candidate, _reference):
        return None


class TestOrganizeScreenshot(unittest.TestCase):
    def test_new_people_receive_distinct_query_numbers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            queries = root / "queries"
            previews = root / "previews"
            image = np.full((220, 180, 3), 80, dtype=np.uint8)
            self.assertTrue(write_image_file(str(source), image))

            with (
                patch.object(
                    query_organizer,
                    "detect_thumbnail_boxes",
                    return_value=[(10, 10, 70, 200), (100, 10, 160, 200)],
                ),
                patch.object(
                    query_organizer,
                    "AI_FeatureExtractor",
                    return_value=_RejectingExtractor(),
                ),
            ):
                result = query_organizer.organize_screenshot(
                    str(source), str(queries), str(previews)
                )

            assigned = [item["query"] for item in result["assignments"]]
            self.assertEqual(assigned, ["Query_1", "Query_2"])
            self.assertTrue(any((queries / "Query_1").iterdir()))
            self.assertTrue(any((queries / "Query_2").iterdir()))


class TestAddCropWritesOcrCache(unittest.TestCase):
    """A crop added via QueryAutoCollector must leave an OCR cache so the
    running matcher can use its timestamp without a full reload."""

    def test_add_crop_writes_ocr_cache_file(self):
        import ocr_utils

        with tempfile.TemporaryDirectory() as temp_dir:
            queries_dir = os.path.join(temp_dir, "queries")
            collector = query_organizer.QueryAutoCollector(
                queries_dir, _RejectingExtractor()
            )
            rng = np.random.default_rng(42)
            crop = rng.integers(0, 256, (220, 100, 3), dtype=np.uint8)
            with (
                patch.object(
                    query_organizer,
                    "ENABLE_OCR_TIMESTAMP_FILTER",
                    True,
                ),
                patch.object(
                    ocr_utils,
                    "extract_reference_timestamp",
                    return_value="7:42 AM",
                ),
            ):
                result = collector.add_crop(crop, target_query="Query_1")

            query_dir = os.path.join(queries_dir, "Query_1")
            made = os.path.basename(result["path"])
            cache_path = os.path.join(query_dir, ".cache", f"{made}.ocr.txt")
            self.assertTrue(os.path.isfile(cache_path))
            with open(cache_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), "7:42 AM")
            self.assertEqual(result["ocr_timestamp"], "7:42 AM")


if __name__ == "__main__":
    unittest.main()
