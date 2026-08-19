"""
Appearance (clothing texture) extraction and similarity for the Re-ID matcher.

This is the fourth signal alongside the OSNet body ensemble, the face branch and
the OCR timestamp gate. It replaces the earlier pose experiment, which did not
separate identities on the tiny standing-pose result-card thumbnails (see the
project memory ``pose-matching-poc-result``): a translation/scale-invariant
skeleton collapses onto a near-identical descriptor regardless of who it is.

Instead we describe the *texture* of the clothing with a Local Binary Pattern
(LBP) histogram. Fabric weave / print pattern is the cheapest remaining
discriminator on these desaturated crops, and it needs no extra dependency:
pure OpenCV + NumPy.

Design mirrors ``ai_model.AI_FeatureExtractor``: a validity flag, an
``extract_*`` method returning a cache-friendly ``np.ndarray`` payload, and a
static ``compute_similarity`` with the "higher = more similar" convention shared
by the other signals.

This module is intentionally standalone: it is validated on its own (unit tests
+ real-data PoC) before being wired into ``auto_marker.py``. Nothing here reads
or mutates the matching pipeline.
"""

import numpy as np

try:
    from config import (
        APPEARANCE_MIN_CROP_HEIGHT,
        APPEARANCE_MIN_CROP_WIDTH,
        APPEARANCE_MIN_TEXTURE_STD,
        APPEARANCE_MODEL,
        APPEARANCE_NORMALIZED_SIZE,
    )
except Exception:  # config should always import, but never fail hard here
    APPEARANCE_MIN_CROP_HEIGHT = 20
    APPEARANCE_MIN_CROP_WIDTH = 8
    APPEARANCE_MIN_TEXTURE_STD = 6.0
    APPEARANCE_MODEL = "lbp"
    APPEARANCE_NORMALIZED_SIZE = (64, 128)

# Vertical bands (as a fraction of crop height) describing the torso and legs.
# The head/feet edges are skipped so background above the shoulders and the
# floor below the feet do not pollute the clothing texture.
_UPPER_BAND = (0.15, 0.50)   # shirt / jacket
_LOWER_BAND = (0.55, 0.95)   # trousers / skirt

_LBP_BINS = 256              # 8-neighbor LBP codes: 0..255
_DESCRIPTOR_SIZE = _LBP_BINS * 2  # upper band + lower band


class AppearanceExtractor:
    """Extract a clothing-texture descriptor and score appearance similarity."""

    def __init__(self, model: str = APPEARANCE_MODEL):
        self.model = model

    @property
    def is_valid(self) -> bool:
        """True for a backend we implement. LBP needs no external model file.

        Only "lbp" exists today, so an unknown ``APPEARANCE_MODEL`` reports
        invalid rather than silently returning LBP descriptors under a name that
        promises something else.
        """
        return self.model == "lbp"

    def extract_descriptor(self, crop_bgr) -> dict | None:
        """Return an LBP texture descriptor for a person crop, or None.

        Returns a dict::

            {"descriptor": np.ndarray (512,) float32}

        The descriptor is two 256-bin LBP histograms (upper body, lower body),
        each L1-normalized, concatenated.

        Returns None — never raises — when the crop cannot be described:
        missing/empty, an unsupported channel layout, smaller than
        ``APPEARANCE_MIN_CROP_HEIGHT`` x ``APPEARANCE_MIN_CROP_WIDTH``, or too
        flat to carry texture (see ``APPEARANCE_MIN_TEXTURE_STD``). Callers can
        treat None as "no appearance evidence" instead of guarding for errors.
        """
        if not self.is_valid:
            return None
        if crop_bgr is None or getattr(crop_bgr, "size", 0) == 0:
            return None
        if crop_bgr.ndim not in (2, 3):
            return None
        if crop_bgr.ndim == 3 and crop_bgr.shape[2] not in (3, 4):
            return None

        h, w = crop_bgr.shape[0], crop_bgr.shape[1]
        if h < APPEARANCE_MIN_CROP_HEIGHT or w < APPEARANCE_MIN_CROP_WIDTH:
            return None

        import cv2
        if crop_bgr.ndim == 3:
            code = cv2.COLOR_BGRA2GRAY if crop_bgr.shape[2] == 4 else cv2.COLOR_BGR2GRAY
            gray = cv2.cvtColor(crop_bgr, code)
        else:
            gray = crop_bgr
        gray = np.ascontiguousarray(gray, dtype=np.uint8)

        # Normalize scale before describing texture, the same way the ReID models
        # resize every crop to a fixed input (ai_model uses 256x128). LBP is not
        # scale invariant: the same person at a different UI zoom produces a
        # different code distribution, which would look like a different person.
        target_w, target_h = APPEARANCE_NORMALIZED_SIZE
        interp = cv2.INTER_AREA if (w > target_w or h > target_h) else cv2.INTER_LINEAR
        gray = cv2.resize(gray, (int(target_w), int(target_h)), interpolation=interp)

        # A flat region sets every LBP bit (all neighbors >= center), so any two
        # textureless crops collapse onto the same single-spike histogram and
        # would score a perfectly confident 1.0 against each other. Reject them
        # instead of reporting maximum similarity on no evidence.
        if float(gray.std()) < APPEARANCE_MIN_TEXTURE_STD:
            return None

        lbp = self._lbp_codes(gray)

        upper = self._band_histogram(lbp, gray.shape[0], _UPPER_BAND)
        lower = self._band_histogram(lbp, gray.shape[0], _LOWER_BAND)
        descriptor = np.concatenate([upper, lower]).astype(np.float32)
        return {"descriptor": descriptor}

    @staticmethod
    def _lbp_codes(gray: np.ndarray) -> np.ndarray:
        """8-neighbor LBP code image, computed with np.roll (no skimage needed).

        Each pixel is compared to its 8 neighbors; each neighbor that is >= the
        center contributes a bit, yielding a code in 0..255. Border pixels wrap
        via np.roll, which is a negligible edge effect on these crops and keeps
        the code shape equal to the input.
        """
        center = gray.astype(np.int16)
        codes = np.zeros(gray.shape, dtype=np.uint8)
        # (dy, dx) for the 8 neighbors, each mapped to one bit 0..7.
        neighbors = [
            (-1, -1), (-1, 0), (-1, 1),
            (0, 1), (1, 1), (1, 0),
            (1, -1), (0, -1),
        ]
        for bit, (dy, dx) in enumerate(neighbors):
            shifted = np.roll(np.roll(center, -dy, axis=0), -dx, axis=1)
            codes |= ((shifted >= center).astype(np.uint8) << bit)
        return codes

    @staticmethod
    def _band_histogram(lbp: np.ndarray, height: int, band: tuple) -> np.ndarray:
        """L1-normalized 256-bin LBP histogram over a horizontal band."""
        y0 = int(round(height * band[0]))
        y1 = int(round(height * band[1]))
        y0 = max(0, min(y0, height))
        y1 = max(0, min(y1, height))
        if y1 <= y0:
            return np.zeros(_LBP_BINS, dtype=np.float32)
        region = lbp[y0:y1, :]
        hist = np.bincount(region.reshape(-1), minlength=_LBP_BINS)[:_LBP_BINS]
        hist = hist.astype(np.float32)
        total = hist.sum()
        if total > 0:
            hist /= total
        return hist

    @staticmethod
    def compute_similarity(a: dict | None, b: dict | None) -> float:
        """Appearance similarity in [0, 1]; 0.0 when either descriptor is missing.

        Uses Bhattacharyya distance between the two texture histograms, mapped
        to ``max(0, 1 - distance)`` so higher means more similar (same
        convention as the ReID/face signals). Symmetric in its arguments.
        """
        if not a or not b:
            return 0.0
        desc_a = np.asarray(a.get("descriptor"), dtype=np.float32)
        desc_b = np.asarray(b.get("descriptor"), dtype=np.float32)
        if desc_a.shape != desc_b.shape or desc_a.size == 0:
            return 0.0

        import cv2
        distance = float(cv2.compareHist(desc_a, desc_b, cv2.HISTCMP_BHATTACHARYYA))
        if not np.isfinite(distance):
            return 0.0
        return float(max(0.0, min(1.0, 1.0 - distance)))
