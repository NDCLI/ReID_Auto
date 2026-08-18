"""Unit tests for the standalone pose extractor.

These tests never require MediaPipe: the extraction engine is faked and the
normalization/similarity math is exercised on synthetic keypoints, mirroring
the availability-mocking style of ``test_ocr_utils.py``.
"""

import unittest
import numpy as np

import pose_extractor
from pose_extractor import PoseExtractor, _NUM_LANDMARKS


def make_keypoints(seed=0, offset=(0.0, 0.0), scale=1.0, conf=0.9):
    """Build a plausible (33, 3) [x, y, conf] pose array.

    ``offset`` and ``scale`` let a caller reproduce the SAME pose at a
    different location/size to check descriptor invariance.
    """
    rng = np.random.default_rng(seed)
    base = rng.uniform(50, 150, size=(_NUM_LANDMARKS, 2)).astype(np.float32)
    # Pin the torso landmarks so a valid normalization frame always exists.
    base[pose_extractor._L_SHOULDER] = (80.0, 60.0)
    base[pose_extractor._R_SHOULDER] = (120.0, 60.0)
    base[pose_extractor._L_HIP] = (85.0, 140.0)
    base[pose_extractor._R_HIP] = (115.0, 140.0)

    xy = base * float(scale) + np.asarray(offset, dtype=np.float32)
    pts = np.zeros((_NUM_LANDMARKS, 3), dtype=np.float32)
    pts[:, :2] = xy
    pts[:, 2] = conf
    return pts


class _FakeLandmark:
    def __init__(self, x, y, visibility):
        self.x = x
        self.y = y
        self.visibility = visibility


class _FakeLandmarkList:
    def __init__(self, landmarks):
        self.landmark = landmarks


class _FakeResults:
    def __init__(self, landmark_list):
        self.pose_landmarks = landmark_list


class _FakeEngine:
    """Stand-in for mediapipe Pose that returns preset landmarks."""

    def __init__(self, results):
        self._results = results
        self.calls = 0

    def process(self, _rgb):
        self.calls += 1
        return self._results


class TestPoseNormalization(unittest.TestCase):
    def setUp(self):
        # Bare extractor without touching MediaPipe.
        self.extractor = PoseExtractor.__new__(PoseExtractor)
        self.extractor.model = "mediapipe"
        self.extractor.min_confidence = 0.5
        self.extractor._engine = None

    def test_descriptor_is_translation_invariant(self):
        a = self.extractor._build_payload(make_keypoints(seed=1))
        b = self.extractor._build_payload(
            make_keypoints(seed=1, offset=(500.0, -300.0))
        )
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        sim = PoseExtractor.compute_similarity(a, b)
        self.assertGreater(sim, 0.99)

    def test_descriptor_is_scale_invariant(self):
        a = self.extractor._build_payload(make_keypoints(seed=2))
        b = self.extractor._build_payload(make_keypoints(seed=2, scale=2.5))
        sim = PoseExtractor.compute_similarity(a, b)
        self.assertGreater(sim, 0.99)

    def test_different_poses_score_low(self):
        pts_a = make_keypoints(seed=3)
        # Same torso frame, but every limb joint displaced by ~2 torso lengths:
        # a clearly different posture (different-person scenario).
        pts_b = pts_a.copy()
        torso = [
            pose_extractor._L_SHOULDER, pose_extractor._R_SHOULDER,
            pose_extractor._L_HIP, pose_extractor._R_HIP,
        ]
        limb_mask = np.ones(_NUM_LANDMARKS, dtype=bool)
        limb_mask[torso] = False
        pts_b[limb_mask, :2] += 160.0  # torso length is 80 px
        a = self.extractor._build_payload(pts_a)
        b = self.extractor._build_payload(pts_b)
        sim = PoseExtractor.compute_similarity(a, b)
        import config
        self.assertLess(sim, config.POSE_SIMILARITY_THRESHOLD)
        self.assertGreaterEqual(sim, 0.0)

    def test_missing_torso_landmarks_returns_none(self):
        pts = make_keypoints(seed=4)
        pts[pose_extractor._L_HIP, 2] = 0.0  # hip no longer confident
        pts[pose_extractor._R_HIP, 2] = 0.0
        self.assertIsNone(self.extractor._build_payload(pts))

    def test_low_confidence_joints_are_ignored_without_crash(self):
        pts_a = make_keypoints(seed=5)
        pts_b = make_keypoints(seed=5)
        # Corrupt a non-torso joint in one pose but drop its confidence so it
        # must not affect the score.
        pts_b[5, :2] += 999.0
        pts_b[5, 2] = 0.1
        a = self.extractor._build_payload(pts_a)
        b = self.extractor._build_payload(pts_b)
        sim = PoseExtractor.compute_similarity(a, b)
        self.assertGreater(sim, 0.99)


class TestSimilarityContract(unittest.TestCase):
    def setUp(self):
        self.extractor = PoseExtractor.__new__(PoseExtractor)
        self.extractor.min_confidence = 0.5
        self.extractor._engine = None

    def test_similarity_is_symmetric_and_bounded(self):
        a = self.extractor._build_payload(make_keypoints(seed=7))
        b = self.extractor._build_payload(make_keypoints(seed=8))
        s_ab = PoseExtractor.compute_similarity(a, b)
        s_ba = PoseExtractor.compute_similarity(b, a)
        self.assertAlmostEqual(s_ab, s_ba, places=6)
        self.assertGreaterEqual(s_ab, 0.0)
        self.assertLessEqual(s_ab, 1.0)

    def test_missing_payload_scores_zero(self):
        a = self.extractor._build_payload(make_keypoints(seed=7))
        self.assertEqual(PoseExtractor.compute_similarity(a, None), 0.0)
        self.assertEqual(PoseExtractor.compute_similarity(None, a), 0.0)
        self.assertEqual(PoseExtractor.compute_similarity(None, None), 0.0)


class TestEngineAvailability(unittest.TestCase):
    def test_extract_returns_none_when_engine_absent(self):
        extractor = PoseExtractor.__new__(PoseExtractor)
        extractor._engine = None
        extractor.min_confidence = 0.5
        self.assertFalse(extractor.is_valid)
        image = np.zeros((200, 100, 3), dtype=np.uint8)
        self.assertIsNone(extractor.extract_keypoints(image))

    def test_extract_uses_faked_engine(self):
        extractor = PoseExtractor.__new__(PoseExtractor)
        extractor.min_confidence = 0.5
        # Fake normalized landmarks (0-1) whose torso maps to a valid frame.
        pts = make_keypoints(seed=10)
        norm = [
            _FakeLandmark(pts[i, 0] / 200.0, pts[i, 1] / 200.0, pts[i, 2])
            for i in range(_NUM_LANDMARKS)
        ]
        extractor._engine = _FakeEngine(_FakeResults(_FakeLandmarkList(norm)))

        image = np.zeros((200, 200, 3), dtype=np.uint8)
        payload = extractor.extract_keypoints(image)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["keypoints"].shape, (_NUM_LANDMARKS, 3))
        self.assertEqual(payload["descriptor"].shape, (_NUM_LANDMARKS, 2))
        self.assertTrue(extractor.is_valid)

    def test_extract_returns_none_when_no_person(self):
        extractor = PoseExtractor.__new__(PoseExtractor)
        extractor.min_confidence = 0.5
        extractor._engine = _FakeEngine(_FakeResults(None))
        image = np.zeros((200, 200, 3), dtype=np.uint8)
        self.assertIsNone(extractor.extract_keypoints(image))


if __name__ == "__main__":
    unittest.main()
