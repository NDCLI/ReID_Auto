"""Automatically crop a screenshot and group each person into Query_N folders."""

from __future__ import annotations

import datetime
import hashlib
import os
import re

import cv2
import numpy as np

from ai_model import AI_FeatureExtractor
from auto_marker import read_image_file, write_image_file
from config import (
    ENABLE_OCR_TIMESTAMP_FILTER,
    FACE_MATCH_MARGIN,
    FACE_MATCH_THRESHOLD,
    FACE_MIN_REFERENCES,
)


VALID_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp")
MAX_QUERY_COUNT = 999  # No practical limit; auto-expands with actual Query folders
PERCEPTUAL_HASH_MAX_DISTANCE = 2
DIFFERENCE_HASH_MAX_DISTANCE = 2


class QueryAutoCollector:
    """Classify one clipboard crop at a time and persist it automatically."""

    def __init__(
        self,
        queries_dir,
        extractor,
        similarity_threshold=0.70,
        min_margin=0.03,
    ):
        self.queries_dir = queries_dir
        self.extractor = extractor
        self.similarity_threshold = similarity_threshold
        self.min_margin = min_margin
        os.makedirs(queries_dir, exist_ok=True)
        self.identities = _load_query_features(queries_dir, extractor)
        self.image_fingerprints = _load_image_fingerprints(queries_dir)
        self.next_query = _next_query_number(queries_dir)

    @staticmethod
    def is_person_crop(image_bgr):
        if image_bgr is None or image_bgr.size == 0:
            return False
        height, width = image_bgr.shape[:2]
        if height < 100 or width < 35:
            return False
        aspect = height / max(width, 1)
        return 1.20 <= aspect <= 5.5 and float(np.std(image_bgr)) >= 12.0

    def add_crop(self, image_bgr, target_query=None):
        if not self.is_person_crop(image_bgr):
            raise ValueError(
                "Ảnh clipboard không giống crop dọc của một người; đã bỏ qua để tránh tạo Query rác."
            )

        fingerprint = _image_fingerprint(image_bgr)
        duplicate_path = _find_duplicate_image(fingerprint, self.image_fingerprints)
        if duplicate_path is not None:
            relative_path = os.path.relpath(duplicate_path, self.queries_dir)
            raise ValueError(f"Ảnh đã có trong Query ({relative_path}); không lưu bản lặp.")

        features = self.extractor.extract_feature(image_bgr)
        query_name = target_query
        score = 1.0
        second_score = -1.0
        match_source = "manual"

        if target_query:
            match = re.fullmatch(r"Query_(\d+)", target_query, flags=re.IGNORECASE)
            if not match or not 1 <= int(match.group(1)) <= MAX_QUERY_COUNT:
                raise ValueError(f"Thư mục Query không hợp lệ: {target_query}")
        else:
            raise ValueError("Không có Query đích được chọn.")

        created = query_name not in self.identities
        if created:
            self.identities[query_name] = []

        query_dir = os.path.join(self.queries_dir, query_name)
        os.makedirs(query_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_path = os.path.join(query_dir, f"capture_{timestamp}.png")
        if not write_image_file(output_path, image_bgr):
            raise IOError(f"Không thể lưu ảnh Query: {output_path}")
        ocr_timestamp = _write_ocr_cache(output_path, image_bgr)
        self.identities[query_name].append(features)
        self.image_fingerprints.append((output_path, fingerprint))

        return {
            "query": query_name,
            "score": float(score),
            "second_score": float(second_score),
            "created": created,
            "path": output_path,
            "features": features,
            "match_source": match_source,
            "ocr_timestamp": ocr_timestamp,
        }


def _write_ocr_cache(image_path, image_bgr):
    """OCR a newly added Query image immediately and cache the timestamp.

    auto_marker loads ``.cache/<img>.ocr.txt`` when it scans a query folder, so
    writing the timestamp here means a freshly added image is automatically
    usable on the next reload without having to wait for OCR to run there.
    Feature cache is intentionally left to auto_marker's own load pass.
    """
    if not ENABLE_OCR_TIMESTAMP_FILTER:
        return None
    from ocr_utils import extract_reference_timestamp

    try:
        ts = extract_reference_timestamp(image_bgr)
    except Exception as e:  # OCR is best-effort; never block query capture
        print(f"  [OCR] Lỗi OCR ảnh mới {os.path.basename(image_path)}: {e}")
        return None
    if not ts:
        # Leave no cache file: auto_marker will retry OCR on next reload and
        # could only ever read a stale "no time" from an empty file.
        return None
    cache_dir = os.path.join(os.path.dirname(image_path), ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    ocr_cache_path = os.path.join(
        cache_dir, f"{os.path.basename(image_path)}.ocr.txt"
    )
    try:
        with open(ocr_cache_path, "w", encoding="utf-8") as f:
            f.write(ts)
    except OSError as e:
        print(f"  [OCR] Không ghi được cache {ocr_cache_path}: {e}")
    return ts


def _image_fingerprint(image_bgr):
    """Return exact and resize/compression-tolerant fingerprints."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    exact_input = np.ascontiguousarray(image_bgr)
    exact = hashlib.sha256(
        str(exact_input.shape).encode("ascii") + exact_input.tobytes()
    ).digest()

    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    low_frequency = cv2.dct(np.float32(small))[:8, :8].reshape(-1)
    perceptual = low_frequency > np.median(low_frequency[1:])

    difference_input = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    difference = (difference_input[:, 1:] > difference_input[:, :-1]).reshape(-1)
    return exact, perceptual, difference


def _hash_distance(left, right):
    return int(np.count_nonzero(left != right))


def _find_duplicate_image(candidate, fingerprints):
    candidate_exact, candidate_perceptual, candidate_difference = candidate
    for path, (exact, perceptual, difference) in fingerprints:
        if candidate_exact == exact:
            return path
        if (
            _hash_distance(candidate_perceptual, perceptual)
            <= PERCEPTUAL_HASH_MAX_DISTANCE
            and _hash_distance(candidate_difference, difference)
            <= DIFFERENCE_HASH_MAX_DISTANCE
        ):
            return path
    return None


def _load_image_fingerprints(queries_dir):
    fingerprints = []
    if not os.path.isdir(queries_dir):
        return fingerprints
    for query_name in sorted(os.listdir(queries_dir)):
        query_dir = os.path.join(queries_dir, query_name)
        if not os.path.isdir(query_dir):
            continue
        if not re.fullmatch(r"Query_(\d+)", query_name, flags=re.IGNORECASE):
            continue
        for filename in sorted(os.listdir(query_dir)):
            if not filename.lower().endswith(VALID_EXTENSIONS):
                continue
            path = os.path.join(query_dir, filename)
            image = read_image_file(path)
            if image is not None:
                fingerprints.append((path, _image_fingerprint(image)))
    return fingerprints


def _active_ranges(values, threshold, min_length, merge_gap=10):
    mask = values > threshold
    ranges = []
    start = None
    for index, active in enumerate(mask):
        if active and start is None:
            start = index
        elif not active and start is not None:
            if index - start >= min_length:
                ranges.append([start, index - 1])
            start = None
    if start is not None and len(mask) - start >= min_length:
        ranges.append([start, len(mask) - 1])

    merged = []
    for current in ranges:
        if merged and current[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = current[1]
        else:
            merged.append(current)
    return [tuple(item) for item in merged]


def detect_thumbnail_boxes(image_bgr):
    """Detect portrait thumbnail cells using row/column activity projections."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    background = float(np.percentile(gray, 10))
    foreground = float(np.percentile(gray, 90))
    activity_threshold = background + max(12.0, (foreground - background) * 0.22)

    col_activity = np.mean(gray, axis=0)
    row_activity = np.mean(gray, axis=1)
    col_activity = np.convolve(col_activity, np.ones(7) / 7.0, mode="same")
    row_activity = np.convolve(row_activity, np.ones(7) / 7.0, mode="same")

    x_ranges = _active_ranges(col_activity, activity_threshold, min_length=18, merge_gap=8)
    y_ranges = _active_ranges(row_activity, activity_threshold, min_length=45, merge_gap=12)

    height, width = gray.shape
    boxes = []
    for x1, x2 in x_ranges:
        for y1, y2 in y_ranges:
            # Projection finds the person content; padding restores the card.
            bx1 = max(0, x1 - 14)
            bx2 = min(width, x2 + 15)
            by1 = max(0, y1 - 5)
            by2 = min(height, y2 + 6)
            box_w, box_h = bx2 - bx1, by2 - by1
            if box_w < 35 or box_h < 70:
                continue
            if not 1.35 <= box_h / max(box_w, 1) <= 6.0:
                continue

            crop_gray = gray[by1:by2, bx1:bx2]
            if float(np.std(crop_gray)) < 12.0:
                continue
            boxes.append((bx1, by1, bx2, by2))

    # Remove nearly duplicate boxes.
    unique = []
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        if not any(_iou(box, kept) > 0.75 for kept in unique):
            unique.append(box)
    return unique


def _iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return intersection / max(area_a + area_b - intersection, 1)


def _next_query_number(queries_dir):
    """Return the smallest empty/free Query slot from 1..MAX_QUERY_COUNT."""
    numbers = set()
    if os.path.isdir(queries_dir):
        for name in os.listdir(queries_dir):
            match = re.fullmatch(r"Query_(\d+)", name, flags=re.IGNORECASE)
            if match:
                number = int(match.group(1))
                query_dir = os.path.join(queries_dir, name)
                has_images = os.path.isdir(query_dir) and any(
                    filename.lower().endswith(VALID_EXTENSIONS)
                    and os.path.isfile(os.path.join(query_dir, filename))
                    for filename in os.listdir(query_dir)
                )
                if 1 <= number <= MAX_QUERY_COUNT and has_images:
                    numbers.add(number)
    return next(
        (number for number in range(1, MAX_QUERY_COUNT + 1) if number not in numbers),
        None,
    )


def _load_query_features(queries_dir, extractor):
    identities = {}
    if not os.path.isdir(queries_dir):
        return identities
    for query_name in sorted(os.listdir(queries_dir)):
        query_dir = os.path.join(queries_dir, query_name)
        if not os.path.isdir(query_dir):
            continue
        match = re.fullmatch(r"Query_(\d+)", query_name, flags=re.IGNORECASE)
        if not match or not 1 <= int(match.group(1)) <= MAX_QUERY_COUNT:
            continue
        features = []
        for filename in sorted(os.listdir(query_dir)):
            if not filename.lower().endswith(VALID_EXTENSIONS):
                continue
            image = read_image_file(os.path.join(query_dir, filename))
            if image is not None:
                features.append(extractor.extract_feature(image))
        if features:
            identities[query_name] = features
    return identities


def _identity_scores(candidate_features, identities, extractor, top_k=2):
    ranked = []
    per_model = {name: [] for name in candidate_features}
    for query_name, reference_features in identities.items():
        combined_scores = []
        model_scores = {name: [] for name in candidate_features}
        for reference in reference_features:
            combined, individual = extractor.ensemble_similarity(candidate_features, reference)
            if individual:
                combined_scores.append(combined)
                for name, score in individual.items():
                    model_scores[name].append(score)
        if not combined_scores:
            continue
        k = min(top_k, len(combined_scores))
        score = float(np.mean(sorted(combined_scores, reverse=True)[:k]))
        ranked.append((score, query_name))
        for name, scores in model_scores.items():
            if scores:
                per_model[name].append((max(scores), query_name))
    ranked.sort(reverse=True)
    return ranked, per_model


def _face_identity_scores(candidate_features, identities, extractor, top_k=2):
    ranked = []
    for query_name, reference_features in identities.items():
        scores = []
        for reference in reference_features:
            score = extractor.face_similarity(candidate_features, reference)
            if score is not None:
                scores.append(score)
        if len(scores) >= FACE_MIN_REFERENCES:
            k = min(top_k, len(scores))
            ranked.append((float(np.mean(sorted(scores, reverse=True)[:k])), query_name))
    ranked.sort(reverse=True)
    return ranked


def organize_screenshot(
    image_path,
    queries_dir,
    preview_dir,
    similarity_threshold=0.70,
    min_margin=0.03,
):
    image = read_image_file(image_path)
    if image is None:
        raise ValueError(f"Không thể đọc ảnh: {image_path}")

    boxes = detect_thumbnail_boxes(image)
    if not boxes:
        raise ValueError("Không tìm thấy thumbnail người trong ảnh.")

    os.makedirs(queries_dir, exist_ok=True)
    os.makedirs(preview_dir, exist_ok=True)
    extractor = AI_FeatureExtractor()
    if not extractor.is_valid:
        raise RuntimeError("Không có model ReID khả dụng.")

    identities = _load_query_features(queries_dir, extractor)
    next_query = _next_query_number(queries_dir)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    assignments = []
    preview = image.copy()

    for index, box in enumerate(boxes, start=1):
        x1, y1, x2, y2 = box
        crop = image[y1:y2, x1:x2]
        features = extractor.extract_feature(crop)
        ranked, per_model = _identity_scores(features, identities, extractor)

        query_name = None
        score = 1.0
        if ranked:
            score, winner = ranked[0]
            second = ranked[1][0] if len(ranked) > 1 else -1.0
            model_winners = [max(scores)[1] for scores in per_model.values() if scores]
            models_agree = not model_winners or all(name == winner for name in model_winners)
            if score >= similarity_threshold and score - second >= min_margin and models_agree:
                query_name = winner

        face_ranked = _face_identity_scores(features, identities, extractor)
        if query_name is None and face_ranked:
            face_score, face_winner = face_ranked[0]
            face_second = face_ranked[1][0] if len(face_ranked) > 1 else -1.0
            if (
                face_score >= FACE_MATCH_THRESHOLD
                and face_score - face_second >= FACE_MATCH_MARGIN
            ):
                query_name = face_winner
                score = face_score

        created = query_name is None
        if created:
            if next_query is None:
                raise ValueError(
                    f"Đã đủ {MAX_QUERY_COUNT} Query; không tạo thêm Query mới."
                )
            query_name = f"Query_{next_query}"
            identities[query_name] = []
            score = 1.0

        query_dir = os.path.join(queries_dir, query_name)
        os.makedirs(query_dir, exist_ok=True)
        filename = f"auto_{timestamp}_{index:03d}.png"
        output_path = os.path.join(query_dir, filename)
        if not write_image_file(output_path, crop):
            raise IOError(f"Không thể lưu ảnh Query: {output_path}")
        _write_ocr_cache(output_path, crop)
        identities[query_name].append(features)
        if created:
            # Recompute only after the first image exists; an empty newly-created
            # directory is deliberately considered a reusable Query slot.
            next_query = _next_query_number(queries_dir)
        assignments.append({
            "query": query_name,
            "score": float(score),
            "path": output_path,
            "bbox": box,
        })

        color = (0, 220, 255)
        cv2.rectangle(preview, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            preview,
            f"{query_name} {score:.2f}",
            (x1, max(18, y1 - 5)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            color,
            1,
            cv2.LINE_AA,
        )

    preview_path = os.path.join(preview_dir, f"query_organizer_{timestamp}.png")
    write_image_file(preview_path, preview)
    return {
        "detected": len(boxes),
        "assignments": assignments,
        "preview_path": preview_path,
        "queries": sorted({item["query"] for item in assignments}),
    }
