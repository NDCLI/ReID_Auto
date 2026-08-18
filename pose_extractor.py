"""
Pose/keypoint extraction and similarity for the Re-ID matcher.

Adds a fourth signal alongside the OSNet body ensemble, the face branch and the
OCR timestamp gate: a body skeleton that is invariant to position and scale.
When the ReID score is undecided because the person changed angle or lighting,
pose similarity helps separate the same identity from a different one.

Design mirrors ``ai_model.AI_FeatureExtractor``: a validity flag, an
``extract_*`` method returning cache-friendly ``np.ndarray`` payloads, and a
static ``compute_similarity``. MediaPipe is imported lazily and optionally
(same pattern as ``ocr_utils``) so the module imports and the app runs even
when MediaPipe is not installed or pose matching is disabled.

This module is intentionally standalone: it is validated on its own before
being wired into ``auto_marker.py`` (reference caching + multi-stage gate).
"""

import os
import numpy as np

try:
    from config import POSE_MIN_CONFIDENCE, POSE_TASK_MODEL
except Exception:  # config should always import, but never fail hard here
    POSE_MIN_CONFIDENCE = 0.5
    POSE_TASK_MODEL = os.path.join("models", "pose_landmarker_lite.task")

# MediaPipe loads TFLite graphs and is not needed at startup when pose matching
# is disabled, so import it lazily behind a flag (mirrors ocr_utils backends).
_MEDIAPIPE_AVAILABLE = None  # None = not probed yet, True/False after first probe


def _probe_mediapipe():
    """Return the mediapipe module if importable, else None. Cached via flag."""
    global _MEDIAPIPE_AVAILABLE
    try:
        import mediapipe as mp  # noqa: F401
        _MEDIAPIPE_AVAILABLE = True
        return mp
    except Exception:
        _MEDIAPIPE_AVAILABLE = False
        return None


# MediaPipe Pose landmark indices used to build the normalization frame.
_L_SHOULDER, _R_SHOULDER = 11, 12
_L_HIP, _R_HIP = 23, 24
_NUM_LANDMARKS = 33


class PoseExtractor:
    """Extract normalized body keypoints and score pose similarity."""

    def __init__(self, model: str = "mediapipe", min_confidence: float = POSE_MIN_CONFIDENCE):
        self.model = model
        self.min_confidence = float(min_confidence)
        self._engine = None
        # "tasks" (new PoseLandmarker API) or "solutions" (legacy mp.solutions.pose).
        # The two return landmarks differently, so extract_keypoints branches on it.
        self._backend = None
        mp = _probe_mediapipe()
        if mp is not None and model == "mediapipe":
            self._init_engine(mp)

    def _init_engine(self, mp):
        """Create a pose engine, preferring the Tasks API, falling back to solutions.

        Recent MediaPipe wheels (Python 3.11+/3.13) ship only the Tasks API and
        drop ``mp.solutions.pose``; older wheels have the reverse. Try Tasks first
        (needs a ``.task`` model file), then the legacy solutions graph.
        """
        # Tasks API: requires a downloaded PoseLandmarker model file.
        try:
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision as mp_vision

            model_path = POSE_TASK_MODEL
            if not os.path.isabs(model_path):
                try:
                    from config import BASE_DIR
                    model_path = os.path.join(BASE_DIR, POSE_TASK_MODEL)
                except Exception:
                    pass
            if os.path.exists(model_path):
                options = mp_vision.PoseLandmarkerOptions(
                    base_options=mp_python.BaseOptions(model_asset_path=model_path),
                    running_mode=mp_vision.RunningMode.IMAGE,
                    min_pose_detection_confidence=self.min_confidence,
                )
                self._engine = mp_vision.PoseLandmarker.create_from_options(options)
                self._backend = "tasks"
                return
        except Exception:
            self._engine = None

        # Legacy solutions API.
        try:
            self._engine = mp.solutions.pose.Pose(
                static_image_mode=True,
                model_complexity=1,
                min_detection_confidence=self.min_confidence,
            )
            self._backend = "solutions"
        except Exception:
            self._engine = None
            self._backend = None

    @property
    def is_valid(self) -> bool:
        """True when a pose engine is loaded and ready to extract keypoints."""
        return self._engine is not None

    def extract_keypoints(self, image_bgr) -> dict | None:
        """Detect a person and return normalized keypoints, or None.

        Returns a dict::

            {
                "keypoints":  np.ndarray (33, 3) of [x, y, conf] in image coords,
                "descriptor": np.ndarray (33, 2) translation/scale invariant,
                "confidence": float mean visibility of confident joints,
            }

        None when the engine is unavailable, no person is detected, or the
        detection is too weak to normalize (missing torso landmarks).
        """
        if self._engine is None or image_bgr is None:
            return None
        if getattr(image_bgr, "size", 0) == 0:
            return None

        # MediaPipe expects RGB; OpenCV frames are BGR.
        import cv2
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)

        landmark_list = self._detect(rgb)
        if landmark_list is None:
            return None

        keypoints = self._landmarks_to_array(landmark_list, image_bgr.shape)
        return self._build_payload(keypoints)

    def _detect(self, rgb):
        """Run the engine and return a flat landmark list, or None. Backend-aware.

        Both backends expose landmarks with ``.x``/``.y`` in [0, 1] and a
        confidence field (``.visibility``), so downstream code is uniform once we
        return a single list. Tasks returns a list of poses; we take the first.
        """
        try:
            if getattr(self, "_backend", None) == "tasks":
                import mediapipe as mp
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = self._engine.detect(image)
                poses = getattr(result, "pose_landmarks", None)
                if not poses:
                    return None
                return poses[0]
            # Legacy solutions backend.
            results = self._engine.process(rgb)
            landmarks = getattr(results, "pose_landmarks", None)
            if landmarks is None:
                return None
            return landmarks.landmark
        except Exception:
            return None

    def _landmarks_to_array(self, landmark_list, image_shape) -> np.ndarray:
        """Convert MediaPipe landmarks to an (N, 3) [x, y, conf] pixel array."""
        h, w = image_shape[0], image_shape[1]
        pts = np.zeros((_NUM_LANDMARKS, 3), dtype=np.float32)
        for i, lm in enumerate(landmark_list):
            if i >= _NUM_LANDMARKS:
                break
            pts[i, 0] = float(getattr(lm, "x", 0.0)) * w
            pts[i, 1] = float(getattr(lm, "y", 0.0)) * h
            pts[i, 2] = float(getattr(lm, "visibility", 0.0))
        return pts

    def _build_payload(self, keypoints: np.ndarray) -> dict | None:
        """Wrap raw keypoints with a normalized descriptor, or None if unusable."""
        descriptor = self._normalize(keypoints)
        if descriptor is None:
            return None
        confident = keypoints[:, 2] >= self.min_confidence
        confidence = float(keypoints[confident, 2].mean()) if confident.any() else 0.0
        return {
            "keypoints": keypoints.astype(np.float32),
            "descriptor": descriptor.astype(np.float32),
            "confidence": confidence,
        }

    def _normalize(self, keypoints: np.ndarray) -> np.ndarray | None:
        """Translate to mid-hip origin and scale by torso length.

        The result is invariant to where the person sits in the crop and to how
        large they are, so the same pose at different positions/sizes yields the
        same descriptor. Returns None when the torso frame cannot be built.
        """
        xy = keypoints[:, :2].astype(np.float32)
        conf = keypoints[:, 2]

        def midpoint(a, b):
            if conf[a] < self.min_confidence or conf[b] < self.min_confidence:
                return None
            return (xy[a] + xy[b]) / 2.0

        mid_hip = midpoint(_L_HIP, _R_HIP)
        mid_shoulder = midpoint(_L_SHOULDER, _R_SHOULDER)
        if mid_hip is None or mid_shoulder is None:
            return None

        torso = float(np.linalg.norm(mid_shoulder - mid_hip))
        if torso <= 1e-3:
            return None

        return (xy - mid_hip) / torso

    @staticmethod
    def compute_similarity(kpt_a: dict | None, kpt_b: dict | None,
                           min_confidence: float = POSE_MIN_CONFIDENCE) -> float:
        """Pose similarity in [0, 1]; 0.0 when either pose is missing/unusable.

        Only joints confident in BOTH poses contribute. The mean per-joint
        Euclidean distance in torso units is mapped to a bounded score via
        exp(-d): identical poses score ~1.0, very different poses approach 0.0.
        Symmetric in its arguments.
        """
        if not kpt_a or not kpt_b:
            return 0.0
        desc_a = np.asarray(kpt_a.get("descriptor"))
        desc_b = np.asarray(kpt_b.get("descriptor"))
        if desc_a.shape != desc_b.shape or desc_a.size == 0:
            return 0.0

        conf_a = kpt_a["keypoints"][:, 2]
        conf_b = kpt_b["keypoints"][:, 2]
        mask = (conf_a >= min_confidence) & (conf_b >= min_confidence)
        if not mask.any():
            return 0.0

        diffs = np.linalg.norm(desc_a[mask] - desc_b[mask], axis=1)
        mean_dist = float(diffs.mean())
        return float(np.exp(-mean_dist))
