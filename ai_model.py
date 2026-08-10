"""Local ReID embedding backends with optional multi-model ensembling."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import cv2
import numpy as np


_OV_CORE = None

def _get_ov_core():
    global _OV_CORE
    if _OV_CORE is None:
        import openvino as ov
        _OV_CORE = ov.Core()
    return _OV_CORE


@dataclass(frozen=True)
class ModelSpec:
    name: str
    model: str
    weights: str | None = None
    weight: float = 1.0
    device: str = "AUTO"


class _OpenVINOEmbeddingModel:
    def __init__(self, spec: ModelSpec, base_dir: str):

        self.spec = spec
        model_path = self._resolve(base_dir, spec.model)
        weights_path = self._resolve(base_dir, spec.weights) if spec.weights else None
        if not os.path.isfile(model_path):
            raise FileNotFoundError(model_path)
        self.resolved_path = model_path

        core = _get_ov_core()
        model = core.read_model(model=model_path, weights=weights_path)
        self.compiled_model = core.compile_model(model=model, device_name=spec.device)
        self.infer_request = self.compiled_model.create_infer_request()
        self.input_layer = self.compiled_model.input(0)
        self._lock = threading.Lock()

        shape = list(self.input_layer.shape)
        if len(shape) != 4 or any(int(x) <= 0 for x in shape):
            raise ValueError(f"{spec.name}: expected a static 4D image input, got {shape}")
        self.nchw = shape[1] in (1, 3)
        if self.nchw:
            self.height, self.width = int(shape[2]), int(shape[3])
        else:
            self.height, self.width = int(shape[1]), int(shape[2])

    @staticmethod
    def _resolve(base_dir: str, path: str | None) -> str | None:
        if not path:
            return None
        if os.path.isabs(path):
            return path
        project_path = os.path.join(base_dir, path)
        if os.path.isfile(project_path):
            return project_path
        # Large optional weights live outside Git/OneDrive in a per-user cache.
        # This variant uses its own cache directory so it never picks up models
        # installed by the original ReID Auto Draw.
        from config import MODEL_CACHE_DIRNAME

        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            cached_path = os.path.join(
                local_app_data, MODEL_CACHE_DIRNAME, "models", os.path.basename(path)
            )
            if os.path.isfile(cached_path):
                return cached_path
        return project_path

    def extract(self, img_bgr: np.ndarray) -> np.ndarray:
        resized = cv2.resize(img_bgr, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
        blob = resized.astype(np.float32)
        if self.nchw:
            blob = blob.transpose(2, 0, 1)
        blob = np.expand_dims(blob, axis=0)
        with self._lock:
            self.infer_request.infer([blob])
            feature = self.infer_request.get_output_tensor(0).data.copy().reshape(-1).astype(np.float32)
        return feature / (np.linalg.norm(feature) + 1e-12)


class AI_FeatureExtractor:
    """Loads every available local model and returns one embedding per model.

    A missing optional model does not stop the application. At least one loaded
    model is required for a match to be accepted.
    """

    def __init__(self, model_specs: list[dict] | None = None, base_dir: str | None = None) -> None:
        if model_specs is None:
            from config import REID_MODELS
            model_specs = REID_MODELS

        base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.models = {}
        self.weights = {}
        self.errors = {}
        self.face_model = None

        print("Loading local ReID model(s)...")
        for raw_spec in model_specs:
            spec = raw_spec if isinstance(raw_spec, ModelSpec) else ModelSpec(**raw_spec)
            try:
                self.models[spec.name] = _OpenVINOEmbeddingModel(spec, base_dir)
                self.weights[spec.name] = float(spec.weight)
                loaded = self.models[spec.name]
                print(
                    f"  [OK] {spec.name} ({spec.device}) "
                    f"{loaded.width}x{loaded.height} <- {loaded.resolved_path}"
                )
            except (FileNotFoundError, ValueError, OSError, ImportError, RuntimeError) as exc:
                self.errors[spec.name] = str(exc)
                print(f"  [SKIP] {spec.name}: {exc}")

        self._load_face_model(base_dir)

        self.is_valid = bool(self.models)
        if not self.is_valid:
            print("  [ERROR] No ReID model could be loaded.")

    @property
    def active_models(self) -> tuple[str, ...]:
        return tuple(self.models)

    def _load_face_model(self, base_dir: str) -> None:
        from config import FACE_DETECTION_MODEL, FACE_RECOGNITION_MODEL

        try:
            self.face_model = _OpenVINOFaceModel(
                FACE_DETECTION_MODEL, FACE_RECOGNITION_MODEL, base_dir
            )
            print("  [OK] face_0095 (AUTO)")
        except (FileNotFoundError, ValueError, OSError, ImportError, RuntimeError) as exc:
            self.errors["face_0095"] = str(exc)
            print(f"  [SKIP] face_0095: {exc}")

    def extract_feature(self, img_bgr: np.ndarray, model_names: tuple[str, ...] | None = None) -> dict[str, np.ndarray]:
        if img_bgr is None or img_bgr.size == 0:
            return {}
        selected = set(model_names) if model_names is not None else None
        features = {
            name: model.extract(img_bgr)
            for name, model in self.models.items()
            if selected is None or name in selected
        }
        from config import FACE_FEATURE_NAME
        if self.face_model is not None and (selected is None or FACE_FEATURE_NAME in selected):
            face_feature = self.face_model.extract(img_bgr)
            if face_feature is not None:
                features[FACE_FEATURE_NAME] = face_feature
        return features

    @staticmethod
    def compute_similarity(feat1: np.ndarray, feat2: np.ndarray) -> float:
        """Compatibility helper for a single pair of normalized vectors."""
        return float(np.dot(feat1, feat2))

    def ensemble_similarity(self, features_a: dict[str, np.ndarray], features_b: dict[str, np.ndarray]) -> tuple[float, dict[str, float]]:
        scores = {}
        for name in self.models:
            if name in features_a and name in features_b:
                scores[name] = self.compute_similarity(features_a[name], features_b[name])
        if not scores:
            return float("-inf"), scores
        total_weight = sum(self.weights[name] for name in scores)
        combined = sum(scores[name] * self.weights[name] for name in scores) / total_weight
        return float(combined), scores
    @staticmethod
    def face_similarity(features_a: dict[str, np.ndarray], features_b: dict[str, np.ndarray]) -> float | None:
        from config import FACE_FEATURE_NAME
        if FACE_FEATURE_NAME not in features_a or FACE_FEATURE_NAME not in features_b:
            return None
        return float(np.dot(features_a[FACE_FEATURE_NAME], features_b[FACE_FEATURE_NAME]))


class _OpenVINOFaceModel:
    """Detect the strongest face and return its normalized identity embedding."""

    def __init__(self, detector_path: str, recognizer_path: str, base_dir: str) -> None:

        detector_path = _OpenVINOEmbeddingModel._resolve(base_dir, detector_path)
        recognizer_path = _OpenVINOEmbeddingModel._resolve(base_dir, recognizer_path)
        if not os.path.isfile(detector_path):
            raise FileNotFoundError(detector_path)
        if not os.path.isfile(recognizer_path):
            raise FileNotFoundError(recognizer_path)

        core = _get_ov_core()
        self.detector = core.compile_model(detector_path, "AUTO")
        self.recognizer = core.compile_model(recognizer_path, "AUTO")
        self._det_lock = threading.Lock()
        self._rec_lock = threading.Lock()

    def extract(self, img_bgr: np.ndarray) -> np.ndarray | None:
        from config import FACE_DETECTION_THRESHOLD

        height, width = img_bgr.shape[:2]
        detector_input = cv2.resize(img_bgr, (300, 300), interpolation=cv2.INTER_LINEAR)
        detector_input = detector_input.transpose(2, 0, 1)[None].astype(np.float32)
        with self._det_lock:
            detections = self.detector([detector_input])[self.detector.output(0)].reshape(-1, 7)
        valid = [row for row in detections if float(row[2]) >= FACE_DETECTION_THRESHOLD]
        if not valid:
            return None

        detection = max(valid, key=lambda row: float(row[2]))
        x1 = max(0, min(width - 1, int(float(detection[3]) * width)))
        y1 = max(0, min(height - 1, int(float(detection[4]) * height)))
        x2 = max(x1 + 1, min(width, int(float(detection[5]) * width)))
        y2 = max(y1 + 1, min(height, int(float(detection[6]) * height)))
        face = img_bgr[y1:y2, x1:x2]
        if face.size == 0:
            return None

        recognizer_input = cv2.resize(face, (128, 128), interpolation=cv2.INTER_LINEAR)
        recognizer_input = recognizer_input.transpose(2, 0, 1)[None].astype(np.float32)
        with self._rec_lock:
            feature = self.recognizer([recognizer_input])[self.recognizer.output(0)]
        feature = feature.reshape(-1).astype(np.float32)
        return feature / (np.linalg.norm(feature) + 1e-12)
