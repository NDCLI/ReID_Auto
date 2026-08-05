"""Regression tests for automatic Query assignment."""

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


if __name__ == "__main__":
    unittest.main()
