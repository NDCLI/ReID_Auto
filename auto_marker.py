"""
ReID Auto Draw Tool
======================
Monitors clipboard for screenshots from Snipping Tool,
finds matching reference images using multi-scale template matching,
draws colored boxes around matches, saves result to file and clipboard.

Usage:
    python auto_marker.py                      # Default settings
    python auto_marker.py --threshold 0.75     # Custom threshold
    python auto_marker.py --query Query_1      # Match only one query
    python auto_marker.py --debug              # Show debug visualization
    python auto_marker.py --single image.png   # Process a single image file
"""

from __future__ import annotations

import os
import sys
from ai_model import AI_FeatureExtractor
import time
import hashlib
import datetime
import io
import json
import ctypes
import argparse
import numpy as np
import cv2
from PIL import Image, ImageGrab
from concurrent.futures import ThreadPoolExecutor, as_completed
import win32clipboard

from config import (
    QUERIES_DIR, OUTPUT_DIR, MATCH_THRESHOLD, MATCH_SCALES,
    BOX_THICKNESS, POLL_INTERVAL, QUERY_IMAGE_PREFIXES,
    CLICK_BOX_MIN_SIZE,
    IGNORE_LEFT_RATIO, IGNORE_BOTTOM_RATIO, AI_MATCH_THRESHOLD,
    AI_MATCH_MARGIN, AI_BEST_REFERENCE_THRESHOLD, AI_TOP_K_REFERENCES,
    AI_REQUIRE_MODEL_AGREEMENT,
    ENFORCE_SINGLE_QUERY, AUTO_CALIBRATION, AUTO_PIXEL_THRESHOLD,
    AUTO_AI_THRESHOLD_FLOOR, AUTO_AI_THRESHOLD_CEILING,
    AUTO_AI_THRESHOLD_TOLERANCE,
    MAX_PIXEL_CANDIDATES, LIMIT_MATCHES_BY_REFERENCE_COUNT,
    FAST_ROOT_MODE, FAST_ROOT_PRIMARY_MODEL, FAST_ROOT_SHORTLIST_THRESHOLD,
    FAST_ROOT_MAX_ROWS, FACE_FEATURE_NAME, FACE_MATCH_THRESHOLD, FACE_MATCH_MARGIN,
    FACE_MIN_REFERENCES, TEMPLATE_REFS_PER_QUERY,
    ENABLE_OCR_TIMESTAMP_FILTER, OCR_TIMESTAMP_TOLERANCE, OCR_METHOD,
)
from ocr_utils import extract_timestamp, extract_reference_timestamp, timestamps_match


# ============================================================
# LOGGING HELPER
# ============================================================
def log(tag: str, message: str) -> None:
    """Print a timestamped log message."""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"  [{timestamp}] [{tag}] {message}")


def read_image_file(path: str, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """Read an image from a Windows path containing Unicode characters."""
    try:
        encoded = np.fromfile(path, dtype=np.uint8)
        if encoded.size:
            image = cv2.imdecode(encoded, flags)
            if image is not None:
                return image
    except (OSError, ValueError):
        pass
    return cv2.imread(path, flags)


def dominant_query_only(all_matches: list[dict]) -> list[dict]:
    """Keep only the query that drew the most boxes.

    Tie-break on highest total score. Used by the single-target domain rule:
    each screenshot is assumed to contain exactly one person.
    """
    if not all_matches:
        return all_matches
    query_counts: dict[str, int] = {}
    query_scores: dict[str, float] = {}
    for m in all_matches:
        q = m["query"]
        query_counts[q] = query_counts.get(q, 0) + 1
        query_scores[q] = query_scores.get(q, 0) + m["score"]
    dominant_query = max(
        query_counts.keys(),
        key=lambda q: (query_counts[q], query_scores[q]),
    )
    return [m for m in all_matches if m["query"] == dominant_query]


def get_dominant_query_name(matches: list[dict]) -> str | None:
    """Return the query name with the most matches, or None if empty.

    Used to determine the subfolder when saving results by query.
    """
    if not matches:
        return None
    query_counts: dict[str, int] = {}
    query_scores: dict[str, float] = {}
    for m in matches:
        q = m.get("query", "")
        if not q:
            continue
        query_counts[q] = query_counts.get(q, 0) + 1
        query_scores[q] = query_scores.get(q, 0) + m.get("score", 0.0)
    if not query_counts:
        return None
    return max(
        query_counts.keys(),
        key=lambda q: (query_counts[q], query_scores[q]),
    )


def write_image_file(path: str, image: np.ndarray) -> bool:
    """Write an image to a Windows path containing Unicode characters."""
    extension = os.path.splitext(path)[1] or ".png"
    success, encoded = cv2.imencode(extension, image)
    if not success:
        return False
    try:
        encoded.tofile(path)
        return True
    except OSError:
        return False


# ============================================================
# TEMPLATE MATCHER
# ============================================================
class TemplateMatcher:
    """Multi-scale template matching engine with NMS."""

    def __init__(self, queries_dir: str, threshold: float = MATCH_THRESHOLD, target_query: str | None = None) -> None:
        self.threshold = threshold
        self.target_query = target_query
        self.reference_images = {}   # {query_name: [(filename, cv2_image, feat), ...]}
        self.query_images = {}       # {query_name: cv2_image} - excluded from matching
        self.reference_timestamps = {}  # {query_name: [timestamp_string, ...]}
        self.ai_extractor = AI_FeatureExtractor()
        self.query_thresholds = {}
        self._load_references(queries_dir)

    def _is_query_image(self, filename: str) -> bool:
        """Check if a file is a query/source image (should be excluded)."""
        name_lower = os.path.splitext(filename)[0].lower()
        return any(name_lower.startswith(prefix) for prefix in QUERY_IMAGE_PREFIXES)

    def _load_references(self, queries_dir):
        """Load all reference images from query folders."""
        if not os.path.exists(queries_dir):
            os.makedirs(queries_dir)
            log("INIT", f"Created queries directory: {queries_dir}")
            return

        print("\n  Loading reference images...")
        print("  " + "-" * 50)

        import re
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

        valid_extensions = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')
        
        items = sorted(os.listdir(queries_dir), key=natural_sort_key)
        has_query_folders = any(
            os.path.isdir(os.path.join(queries_dir, name)) for name in items
        )
        for item_name in items:
            item_path = os.path.join(queries_dir, item_name)
            
            if os.path.isdir(item_path):
                query_name = item_name
                # Filter by target query if specified
                if self.target_query and query_name != self.target_query:
                    continue

                if query_name not in self.reference_images:
                    self.reference_images[query_name] = []

                # Extract timestamps from reference images using RapidOCR
                # and Windows OCR (same engine as Windows 11 Photos).
                ref_timestamps_for_query = []
                cache_dir = os.path.join(item_path, ".cache")
                os.makedirs(cache_dir, exist_ok=True)
                
                for img_file in sorted(os.listdir(item_path), key=natural_sort_key):
                    if not img_file.lower().endswith(valid_extensions):
                        continue

                    img_path = os.path.join(item_path, img_file)
                    
                    if self._is_query_image(img_file):
                        img = cv2.imread(img_path)
                        if img is not None:
                            self.query_images[query_name] = img
                            log("QUERY", f"{query_name}/{img_file} (excluded from matching)")
                        continue

                    # Feature Caching
                    feat_cache_path = os.path.join(cache_dir, f"{img_file}.npz")
                    ocr_cache_path = os.path.join(cache_dir, f"{img_file}.ocr.txt")
                    img_mtime = os.path.getmtime(img_path)
                    
                    feat = None
                    if os.path.exists(feat_cache_path) and os.path.getmtime(feat_cache_path) >= img_mtime:
                        try:
                            with np.load(feat_cache_path) as data:
                                feat = {k: data[k] for k in data.files}
                        except Exception:
                            feat = None

                    ref_ts = None
                    if ENABLE_OCR_TIMESTAMP_FILTER:
                        if os.path.exists(ocr_cache_path) and os.path.getmtime(ocr_cache_path) >= img_mtime:
                            try:
                                with open(ocr_cache_path, 'r', encoding='utf-8') as f:
                                    content = f.read().strip()
                                    ref_ts = content if content else None
                            except Exception:
                                ref_ts = None
                        else:
                            # Need to OCR (handled below)
                            ref_ts = False # Use False to indicate it needs computation

                    if feat is None or (ENABLE_OCR_TIMESTAMP_FILTER and ref_ts is False):
                        img = cv2.imread(img_path)
                        if img is None:
                            log("WARN", f"Cannot read: {img_path}")
                            continue
                            
                        if feat is None:
                            feat = self.ai_extractor.extract_feature(img)
                            try:
                                np.savez(feat_cache_path, **feat)
                            except Exception as e:
                                log("WARN", f"Failed to save feature cache: {e}")
                                
                        if ENABLE_OCR_TIMESTAMP_FILTER and ref_ts is False:
                            ref_ts = extract_reference_timestamp(img)
                            try:
                                with open(ocr_cache_path, 'w', encoding='utf-8') as f:
                                    f.write(ref_ts if ref_ts else "")
                            except Exception as e:
                                log("WARN", f"Failed to save OCR cache: {e}")

                    # Only read image if we didn't already read it and we need it for shape/reference
                    img = cv2.imread(img_path) if 'img' not in locals() or img is None else img
                    if img is None:
                         continue
                         
                    self.reference_images[query_name].append((img_file, img, feat))
                    if ENABLE_OCR_TIMESTAMP_FILTER and ref_ts:
                        ref_timestamps_for_query.append(ref_ts)

                    ts_info = f" | OCR: {ref_ts}" if ref_ts else ""
                    log("REF", f"{query_name}/{img_file} ({img.shape[1]}x{img.shape[0]}){ts_info}")
                    time.sleep(0.005)  # Yield CPU to keep GUI responsive

                # Store ALL timestamps for this query (each ref may be at a different time)
                if ref_timestamps_for_query and ENABLE_OCR_TIMESTAMP_FILTER:
                    self.reference_timestamps[query_name] = ref_timestamps_for_query
                    log("OCR", f"{query_name}: timestamps = {ref_timestamps_for_query}")
            else:
                # Direct files in queries/ folder
                # When named Query folders exist, loose images are test/source
                # screenshots, not identities. Loading them as references would
                # be both incorrect and very expensive.
                if has_query_folders:
                    continue
                if not item_name.lower().endswith(valid_extensions):
                    continue
                    
                query_name = "Query_Mac_Dinh"
                if self.target_query and query_name != self.target_query:
                    continue
                    
                if query_name not in self.reference_images:
                    self.reference_images[query_name] = []
                    
                img = cv2.imread(item_path)
                if img is None:
                    continue
                    
                if self._is_query_image(item_name):
                    self.query_images[query_name] = img
                    log("QUERY", f"Root/{item_name} (excluded from matching)")
                else:
                    cache_dir = os.path.join(queries_dir, ".cache")
                    os.makedirs(cache_dir, exist_ok=True)
                    feat_cache_path = os.path.join(cache_dir, f"{item_name}.npz")
                    img_mtime = os.path.getmtime(item_path)
                    
                    feat = None
                    if os.path.exists(feat_cache_path) and os.path.getmtime(feat_cache_path) >= img_mtime:
                        try:
                            with np.load(feat_cache_path) as data:
                                feat = {k: data[k] for k in data.files}
                        except Exception:
                            feat = None
                            
                    if feat is None:
                        feat = self.ai_extractor.extract_feature(img)
                        try:
                            np.savez(feat_cache_path, **feat)
                        except Exception:
                            pass
                            
                    self.reference_images[query_name].append((item_name, img, feat))
                    log("REF", f"Root/{item_name} ({img.shape[1]}x{img.shape[0]})")
                time.sleep(0.005)  # Yield CPU to keep GUI responsive

        total_refs = sum(len(refs) for refs in self.reference_images.values())
        total_queries = len(self.reference_images)
        print("  " + "-" * 50)
        log("INIT", f"Loaded {total_refs} reference images from {total_queries} queries")
        self._calibrate_query_thresholds()

    def _calibrate_query_thresholds(self):
        """Estimate a conservative acceptance threshold for every identity."""
        for query_name, refs in self.reference_images.items():
            pair_scores = []
            for i in range(len(refs)):
                for j in range(i + 1, len(refs)):
                    score, individual = self.ai_extractor.ensemble_similarity(
                        refs[i][2], refs[j][2]
                    )
                    if individual:
                        pair_scores.append(score)

            if AUTO_CALIBRATION and pair_scores:
                low_intra_score = float(np.percentile(pair_scores, 10))
                threshold = max(
                    AUTO_AI_THRESHOLD_FLOOR,
                    min(
                        AUTO_AI_THRESHOLD_CEILING,
                        low_intra_score - AUTO_AI_THRESHOLD_TOLERANCE,
                    ),
                )
            else:
                threshold = AI_MATCH_THRESHOLD

            self.query_thresholds[query_name] = threshold
            if pair_scores:
                log(
                    "CALIBRATE",
                    f"{query_name}: AI threshold={threshold:.3f}, "
                    f"intra range={min(pair_scores):.3f}-{max(pair_scores):.3f}",
                )
            else:
                log("CALIBRATE", f"{query_name}: AI threshold={threshold:.3f} (need 2+ refs)")

    def _classify_candidate(self, candidate_bgr):
        """Classify a crop against every identity using open-set rejection."""
        candidate_features = self.ai_extractor.extract_feature(candidate_bgr)
        if not candidate_features:
            return None

        return self._classify_features(candidate_features)

    def _classify_face_features(self, candidate_features):
        """Use a confident face match to rescue clothing-change cases."""
        query_results = []
        for query_name, refs in self.reference_images.items():
            scores = []
            best_ref_name = None
            best_ref_score = float("-inf")
            for ref_name, _ref_img, ref_features in refs:
                score = self.ai_extractor.face_similarity(candidate_features, ref_features)
                if score is None:
                    continue
                scores.append(score)
                if score > best_ref_score:
                    best_ref_score = score
                    best_ref_name = ref_name
            if len(scores) >= FACE_MIN_REFERENCES:
                k = min(AI_TOP_K_REFERENCES, len(scores))
                query_results.append({
                    "query": query_name,
                    "ref_name": best_ref_name,
                    "score": float(np.mean(sorted(scores, reverse=True)[:k])),
                    "source": "face",
                })

        if not query_results:
            return None
        query_results.sort(key=lambda item: item["score"], reverse=True)
        best = query_results[0]
        second_score = query_results[1]["score"] if len(query_results) > 1 else -1.0
        margin = best["score"] - second_score
        if best["score"] < FACE_MATCH_THRESHOLD or margin < FACE_MATCH_MARGIN:
            return None
        best["margin"] = float(margin)
        best["threshold"] = float(FACE_MATCH_THRESHOLD)
        return best

    def _classify_features(self, candidate_features):
        """Apply the normal ensemble/open-set policy to existing embeddings."""

        face_result = self._classify_face_features(candidate_features)

        query_results = []
        per_model_winners = {name: [] for name in candidate_features}

        for query_name, refs in self.reference_images.items():
            reference_scores = []
            model_scores = {name: [] for name in candidate_features}
            best_ref_name = None
            best_ref_score = float("-inf")

            for ref_name, _ref_img, ref_features in refs:
                combined, individual = self.ai_extractor.ensemble_similarity(
                    candidate_features, ref_features
                )
                if not individual:
                    continue
                reference_scores.append(combined)
                for model_name, score in individual.items():
                    model_scores[model_name].append(score)
                if combined > best_ref_score:
                    best_ref_score = combined
                    best_ref_name = ref_name

            if not reference_scores:
                continue

            k = min(AI_TOP_K_REFERENCES, len(reference_scores))
            top_scores = sorted(reference_scores, reverse=True)[:k]
            identity_score = float(np.mean(top_scores))
            model_identity_scores = {
                name: float(np.mean(sorted(scores, reverse=True)[:min(k, len(scores))]))
                for name, scores in model_scores.items() if scores
            }
            query_results.append({
                'query': query_name,
                'ref_name': best_ref_name,
                'score': identity_score,
                'best_reference_score': float(best_ref_score),
                'model_scores': model_identity_scores,
            })
            for model_name, score in model_identity_scores.items():
                per_model_winners[model_name].append((score, query_name))

        if not query_results:
            return face_result

        query_results.sort(key=lambda item: item['score'], reverse=True)
        best = query_results[0]
        second_score = query_results[1]['score'] if len(query_results) > 1 else -1.0
        second_query = query_results[1]['query'] if len(query_results) > 1 else "N/A"
        margin = best['score'] - second_score

        identity_threshold = self.query_thresholds.get(best['query'], AI_MATCH_THRESHOLD)

        # Log chi tiết top-2 để debug khi vẽ nhầm/bỏ sót
        log(
            "AI",
            f"Top1: {best['query']} ({best['score']:.3f}) vs "
            f"Top2: {second_query} ({second_score:.3f}), "
            f"margin={margin:.3f} (cần≥{AI_MATCH_MARGIN:.3f}), "
            f"thresh={identity_threshold:.3f}",
        )

        if best['score'] < identity_threshold or margin < AI_MATCH_MARGIN:
            if margin < AI_MATCH_MARGIN and best['score'] >= identity_threshold:
                log(
                    "REJECT",
                    f"Margin quá nhỏ ({margin:.3f}<{AI_MATCH_MARGIN:.3f}): "
                    f"{best['query']} vs {second_query} — bỏ qua để tránh nhầm",
                )
            return face_result

        if best['best_reference_score'] < AI_BEST_REFERENCE_THRESHOLD:
            log(
                "REJECT",
                f"Ảnh tham chiếu tốt nhất {best['best_reference_score']:.3f}"
                f"<{AI_BEST_REFERENCE_THRESHOLD:.3f}; loại ứng viên mơ hồ",
            )
            return face_result

        if AI_REQUIRE_MODEL_AGREEMENT and len(per_model_winners) > 1:
            winners = []
            for ranked in per_model_winners.values():
                if ranked:
                    winners.append(max(ranked)[1])
            if winners and any(winner != best['query'] for winner in winners):
                return face_result

        best['margin'] = float(margin)
        best['threshold'] = float(identity_threshold)
        best['reference_threshold'] = float(AI_BEST_REFERENCE_THRESHOLD)
        best['source'] = "body"
        return best

    def _rank_features(self, candidate_features, model_names=None):
        """Rank identities for a precomputed feature set without rejection."""
        selected = set(model_names) if model_names is not None else None
        ranked = []
        for query_name, refs in self.reference_images.items():
            scores = []
            best_ref_name = None
            best_ref_score = float("-inf")
            for ref_name, _ref_img, ref_features in refs:
                individual = {
                    name: self.ai_extractor.compute_similarity(
                        candidate_features[name], ref_features[name]
                    )
                    for name in candidate_features
                    if name in ref_features and (selected is None or name in selected)
                }
                if not individual:
                    continue
                total_weight = sum(self.ai_extractor.weights[name] for name in individual)
                combined = sum(
                    score * self.ai_extractor.weights[name]
                    for name, score in individual.items()
                ) / total_weight
                scores.append(combined)
                if combined > best_ref_score:
                    best_ref_score = combined
                    best_ref_name = ref_name
            if scores:
                k = min(AI_TOP_K_REFERENCES, len(scores))
                ranked.append({
                    "query": query_name,
                    "ref_name": best_ref_name,
                    "score": float(np.mean(sorted(scores, reverse=True)[:k])),
                })
        return sorted(ranked, key=lambda item: item["score"], reverse=True)

    @staticmethod
    def _detect_result_grid(screenshot_bgr):
        """Detect the regular Re-ID result cards; return [] for unknown layouts."""
        gray = cv2.cvtColor(screenshot_bgr, cv2.COLOR_BGR2GRAY)
        screen_h, screen_w = gray.shape
        content_x = int(screen_w * 0.30)

        # Result-card rows have a large fraction of photographic pixels. This
        # cleanly separates them from headings and the confidence controls.
        row_activity = (gray[:, content_x:] > 45).mean(axis=1)
        row_bands = []
        start = None
        for y, active in enumerate(row_activity > 0.12):
            if active and start is None:
                start = y
            elif not active and start is not None:
                if y - start >= max(80, int(screen_h * 0.15)):
                    row_bands.append((start, y))
                start = None
        if start is not None and screen_h - start >= max(80, int(screen_h * 0.15)):
            row_bands.append((start, screen_h))
        row_bands = row_bands[:FAST_ROOT_MAX_ROWS]
        if not row_bands:
            return []

        # At the top of a card the UI background is uniform, so combining a
        # few scanlines yields exact card edges even when a person is dark.
        y1, y2 = row_bands[0]
        scan = gray[min(y1 + 6, y2 - 1):min(y1 + 28, y2)]
        if scan.size == 0:
            return []
        column_mask = np.max(scan, axis=0) > 30
        segments = []
        start = None
        for x in range(int(screen_w * 0.28), screen_w):
            active = bool(column_mask[x])
            if active and start is None:
                start = x
            elif not active and start is not None:
                width = x - start
                if int(screen_w * 0.04) <= width <= int(screen_w * 0.09):
                    segments.append((start, x))
                start = None
        if len(segments) < 4:
            return []

        widths = [x2 - x1 for x1, x2 in segments]
        card_width = int(round(float(np.median(widths))))
        starts = [x1 for x1, _ in segments]
        gaps = [b - a for a, b in zip(starts, starts[1:]) if b - a < card_width * 1.5]
        pitch = int(round(float(np.median(gaps)))) if gaps else card_width + 4
        first_x = min(starts)
        if card_width < 40 or pitch <= card_width or pitch > card_width * 1.4:
            return []

        columns = []
        x = first_x
        while x + card_width <= screen_w and len(columns) < 20:
            columns.append((x, min(screen_w, x + card_width)))
            x += pitch
        if len(columns) < 4:
            return []

        return [
            (x1, ry1, x2, ry2)
            for ry1, ry2 in row_bands
            for x1, x2 in columns
        ]

    def _find_matches_fast_root(self, screenshot_bgr):
        """Two-stage grid classifier used when the root Query folder is active.

        This is an accelerator, not an authoritative answer. Returns a list of
        accepted matches when the grid is detected and cards survive
        shortlisting, or None when no fast path was possible (grid not detected
        or the primary model is unavailable). The caller treats an empty result
        the same as None and falls back to the full template scan: an empty
        fast result only means "this heuristic accepted nothing", which must
        not be trusted to mean "there is no match here".
        """
        boxes = self._detect_result_grid(screenshot_bgr)
        if not boxes or FAST_ROOT_PRIMARY_MODEL not in self.ai_extractor.models:
            return None

        # The top-left card is the source query shown again by the Re-ID UI.
        boxes = boxes[1:]
        shortlist = []
        for bbox in boxes:
            x1, y1, x2, y2 = bbox
            crop = screenshot_bgr[y1:y2, x1:x2]
            primary = self.ai_extractor.extract_feature(
                crop, model_names=(FAST_ROOT_PRIMARY_MODEL,)
            )
            ranked = self._rank_features(primary, (FAST_ROOT_PRIMARY_MODEL,))
            if ranked and ranked[0]["score"] >= FAST_ROOT_SHORTLIST_THRESHOLD:
                shortlist.append((bbox, crop, primary, ranked[0]["score"]))

        matches = []
        remaining_models = tuple(
            name for name in self.ai_extractor.active_models
            if name != FAST_ROOT_PRIMARY_MODEL
        )
        if self.ai_extractor.face_model is not None:
            remaining_models += (FACE_FEATURE_NAME,)
        for bbox, crop, primary, primary_score in shortlist:
            features = dict(primary)
            features.update(
                self.ai_extractor.extract_feature(crop, model_names=remaining_models)
            )
            # Reuse the normal conservative open-set classifier policy, but
            # avoid recomputing the already extracted primary embedding.
            classification = self._classify_features(features)
            if classification:
                classification.update({
                    "bbox": bbox,
                    "pixel_score": primary_score,
                    "scale": 1.0,
                })
                matches.append(classification)

        matches = self._filter_matches_by_card_timestamp(matches, screenshot_bgr)
        matches = self._limit_and_align_matches(matches)
        log(
            "FAST",
            f"Grid {len(boxes) + 1} cards, shortlisted {len(shortlist)}, "
            f"accepted {len(matches)}",
        )
        return matches

    def _limit_and_align_matches(self, matches):
        """Keep accepted detections within the exemplar-based safety cap.

        The source card occupies one stored exemplar slot, so the cap is
        ``reference_count - 1``. The score ordering keeps the strongest
        independently accepted detections when more candidates are found.
        """
        if LIMIT_MATCHES_BY_REFERENCE_COUNT and matches:
            limited = []
            for query_name in sorted({item["query"] for item in matches}):
                query_matches = [
                    item for item in matches if item["query"] == query_name
                ]
                max_boxes = max(
                    0, len(self.reference_images.get(query_name, [])) - 1
                )
                query_matches.sort(
                    key=lambda item: (
                        item["score"], item.get("pixel_score", 0.0)
                    ),
                    reverse=True,
                )
                limited.extend(query_matches[:max_boxes])
            matches = limited

        if not matches:
            return matches
        ordered = sorted(matches, key=lambda item: item["bbox"][1])
        rows = [[ordered[0]]]
        for item in ordered[1:]:
            if item["bbox"][1] - rows[-1][-1]["bbox"][1] < 50:
                rows[-1].append(item)
            else:
                rows.append([item])
        median_height = float(np.median([
            item["bbox"][3] - item["bbox"][1] for item in matches
        ]))
        for row in rows:
            center = float(np.median([
                (item["bbox"][1] + item["bbox"][3]) / 2.0 for item in row
            ]))
            aligned_y1 = int(center - median_height / 2.0)
            aligned_y2 = int(center + median_height / 2.0)
            for item in row:
                x1, _y1, x2, _y2 = item["bbox"]
                item["bbox"] = (x1, aligned_y1, x2, aligned_y2)
        return matches

    def _filter_matches_by_card_timestamp(self, matches, screenshot_bgr):
        """Reject a match only when its own card time contradicts the query.

        OCR is an extra precision gate, not a requirement: it removes a card
        whose printed time is read confidently and matches no reference time
        for that identity (a different-time stranger the AI mistook for the
        same person). When the card time cannot be read, or the query has no
        reference timestamps, the card is kept and the AI score decides.
        """
        if not (ENABLE_OCR_TIMESTAMP_FILTER and matches):
            return matches

        kept = []
        
        def process_match(match):
            query_name = match.get("query")
            ref_timestamps = self.reference_timestamps.get(query_name)
            if not ref_timestamps:
                return match, True

            x1, y1, x2, y2 = match["bbox"]
            crop = screenshot_bgr[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
            if crop.shape[0] < 10 or crop.shape[1] < 10:
                return match, True

            card_ts = extract_reference_timestamp(crop)
            if not card_ts:
                # Unreadable time — do not reject, trust the AI score.
                return match, True

            if any(
                timestamps_match(card_ts, ref_ts,
                                 tolerance_minutes=OCR_TIMESTAMP_TOLERANCE)
                for ref_ts in ref_timestamps
            ):
                return match, True
            else:
                log(
                    "OCR",
                    f"Rejected {query_name} card at x={x1}: time {card_ts} "
                    f"not in references {sorted(set(ref_timestamps))}",
                )
                return match, False

        with ThreadPoolExecutor() as executor:
            future_to_match = {executor.submit(process_match, m): m for m in matches}
            for future in as_completed(future_to_match):
                match, is_kept = future.result()
                if is_kept:
                    kept.append(match)
                    
        return kept

    def _remove_source_grid_match(self, matches, screenshot_bgr):
        """Remove a match only when it overlaps the detected source card."""
        if not matches:
            return matches

        grid_boxes = self._detect_result_grid(screenshot_bgr)
        if not grid_boxes:
            return matches

        source_bbox = grid_boxes[0]
        overlaps = [
            (self._compute_iou(item["bbox"], source_bbox), item)
            for item in matches
        ]
        if not overlaps:
            return matches

        overlap, source_match = max(overlaps, key=lambda pair: pair[0])
        if overlap <= 0.30:
            return matches

        log(
            "INFO",
            f"Removed source card at ({source_match['bbox'][0]}, {source_match['bbox'][1]})",
        )
        return [item for item in matches if item is not source_match]


    def find_matches(self, screenshot_bgr: np.ndarray, debug: bool = False) -> list[dict]:
        """
        Find all reference images within the screenshot.
        Returns list of match dicts with: query, ref_name, bbox, score
        """
        # Extract timestamp from screenshot for comparison
        screenshot_timestamp = None
        # DISABLED: Tạm tắt việc quét thời gian ở góc trái/phải trên cùng 
        # (tiết kiệm ~0.8 giây) vì user báo không cần lọc thời gian tổng của UI.
        # if ENABLE_OCR_TIMESTAMP_FILTER:
        #     import time as _time_ocr
        #     _t_ocr = _time_ocr.time()
        #     try:
        #         screenshot_timestamp = extract_timestamp(screenshot_bgr, method=OCR_METHOD)
        #         if screenshot_timestamp:
        #             log("OCR", f"Screenshot timestamp: {screenshot_timestamp} "
        #                 f"({_time_ocr.time()-_t_ocr:.1f}s)")
        #         else:
        #             log("OCR", f"No timestamp found in screenshot "
        #                 f"({_time_ocr.time()-_t_ocr:.1f}s)")
        #     except Exception as e:
        #         log("OCR", f"Screenshot timestamp extraction failed: {e}")

        # --- EARLY TIMESTAMP FILTER ---
        # Temporarily hide Query folders whose reference timestamps don't
        # match the screenshot. This speeds up BOTH the fast-root grid path
        # AND the full template scan by avoiding template matching and AI
        # classification for folders whose people are at a different time.
        original_refs = None  # set when we narrow the view
        if (ENABLE_OCR_TIMESTAMP_FILTER and screenshot_timestamp
                and self.reference_timestamps):
            skipped_queries = set()
            for qn, ref_ts_list in self.reference_timestamps.items():
                any_ts_match = any(
                    timestamps_match(ref_ts, screenshot_timestamp,
                                     tolerance_minutes=OCR_TIMESTAMP_TOLERANCE)
                    for ref_ts in ref_ts_list
                )
                if not any_ts_match:
                    skipped_queries.add(qn)
            if skipped_queries:
                log("OCR",
                    f"Early skip {len(skipped_queries)} folder(s) by timestamp: "
                    f"{sorted(skipped_queries)}")
                # Swap in a filtered view; restore in the finally block
                original_refs = self.reference_images
                self.reference_images = {
                    qn: refs for qn, refs in original_refs.items()
                    if qn not in skipped_queries
                }

        try:
            return self._find_matches_inner(screenshot_bgr, screenshot_timestamp)
        finally:
            # Always restore the full reference set
            if original_refs is not None:
                self.reference_images = original_refs

    def _identify_query_folder(self, screenshot_bgr):
        """Identify which query folder the screenshot belongs to.

        Uses the first result card (the source/query image shown by the Re-ID
        VMS UI) and template-matches it against reference images in each folder.
        Returns the folder name with the best match, or None if no grid or no
        confident match is found.
        """
        grid_boxes = self._detect_result_grid(screenshot_bgr)
        if not grid_boxes:
            return None

        # The first card in the grid IS the query/source person image
        sx1, sy1, sx2, sy2 = grid_boxes[0]
        source_crop = screenshot_bgr[sy1:sy2, sx1:sx2]
        if source_crop.shape[0] < 10 or source_crop.shape[1] < 10:
            return None

        source_gray = cv2.cvtColor(source_crop, cv2.COLOR_BGR2GRAY)
        sh, sw = source_gray.shape[:2]

        best_folder = None
        best_score = -1.0

        for query_name, refs in self.reference_images.items():
            folder_best = -1.0
            for _ref_name, ref_img, _ref_feat in refs:
                ref_gray = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY)
                rh, rw = ref_gray.shape[:2]

                # Template-match the source card against this reference image
                # at multiple scales. The source card and reference are both
                # small person crops so we match ref inside source (or source
                # inside ref) depending on relative sizes.
                for scale in [0.9, 0.95, 1.0, 1.05, 1.1]:
                    tw = int(rw * scale)
                    th = int(rh * scale)
                    if tw < 10 or th < 10:
                        continue

                    # Match ref (template) inside source card
                    if tw < sw and th < sh:
                        resized_ref = cv2.resize(ref_gray, (tw, th))
                        result = cv2.matchTemplate(
                            source_gray, resized_ref, cv2.TM_CCOEFF_NORMED
                        )
                        score = float(result.max())
                        if score > folder_best:
                            folder_best = score

                    # Also match source card inside ref (in case ref is bigger)
                    stw = int(sw * scale)
                    sth = int(sh * scale)
                    if stw < rw and sth < rh:
                        resized_src = cv2.resize(source_gray, (stw, sth))
                        result2 = cv2.matchTemplate(
                            ref_gray, resized_src, cv2.TM_CCOEFF_NORMED
                        )
                        score2 = float(result2.max())
                        if score2 > folder_best:
                            folder_best = score2

            if folder_best > best_score:
                best_score = folder_best
                best_folder = query_name

            log("IDENTIFY",
                f"{query_name}: source-card match score = {folder_best:.3f}")

        if best_folder and best_score >= 0.5:
            log("IDENTIFY",
                f"Source card belongs to {best_folder} "
                f"(score={best_score:.3f})")
            return best_folder
        else:
            log("IDENTIFY",
                f"No confident match for source card "
                f"(best={best_folder}, score={best_score:.3f})")
            return None

    def _find_matches_inner(self, screenshot_bgr, screenshot_timestamp):
        """Core matching logic, called after early timestamp filtering.

        In all-folders mode with ENFORCE_SINGLE_QUERY, identifies the correct
        query folder by template-matching the first result card (the source
        query image shown by the VMS UI) against all reference folders. Only
        the identified folder is scanned for result matches.
        """
        import time as _time
        _t0 = _time.time()

        active_count = sum(len(r) for r in self.reference_images.values())
        if self.target_query is None and TEMPLATE_REFS_PER_QUERY > 0:
            probe_count = sum(
                min(len(r), TEMPLATE_REFS_PER_QUERY)
                for r in self.reference_images.values()
            )
        else:
            probe_count = active_count
        log("PERF", f"Matching with {len(self.reference_images)} folder(s), "
            f"{active_count} ref(s), template probes={probe_count}")

        if FAST_ROOT_MODE:
            fast_matches = self._find_matches_fast_root(screenshot_bgr)
            if fast_matches is not None:
                log("PERF", f"Fast grid done in {_time.time()-_t0:.1f}s")
                return fast_matches
            log("PERF", f"Fast grid fallback after {_time.time()-_t0:.1f}s")

        # --- SOURCE-CARD FOLDER IDENTIFICATION ---
        # When ENFORCE_SINGLE_QUERY is on and multiple folders are loaded,
        # identify which folder the screenshot belongs to by matching the
        # first result card (source/query image) against reference images.
        # Then only scan that specific folder instead of all folders.
        identified_folder = None
        scan_refs = self.reference_images  # default: scan all
        if (ENFORCE_SINGLE_QUERY
                and self.target_query is None
                and len(self.reference_images) > 1):
            _t_id = _time.time()
            identified_folder = self._identify_query_folder(screenshot_bgr)
            if identified_folder and identified_folder in self.reference_images:
                scan_refs = {identified_folder: self.reference_images[identified_folder]}
                log("PERF",
                    f"Source-card identification: {identified_folder} "
                    f"in {_time.time()-_t_id:.1f}s — scanning only this folder")
            else:
                log("PERF",
                    f"Source-card identification failed "
                    f"({_time.time()-_t_id:.1f}s) — scanning all folders")

        gray_screen = cv2.cvtColor(screenshot_bgr, cv2.COLOR_BGR2GRAY)
        screen_h, screen_w = gray_screen.shape[:2]
        scales = MATCH_SCALES

        # --- CANDIDATE PROPOSAL ---
        # When the regular result grid is detected, propose every card (minus
        # the source card) and let the AI classifier decide. This removes the
        # dependency on the pixel template gate, which silently dropped true
        # cards whenever a stored reference crop was slightly mis-framed. The
        # multi-scale template scan stays as the fallback for unknown layouts.
        all_matches = []
        grid_boxes = self._detect_result_grid(screenshot_bgr)
        if grid_boxes:
            for x1, y1, x2, y2 in grid_boxes[1:]:
                if x1 < screen_w * IGNORE_LEFT_RATIO:
                    continue
                if y1 > screen_h * (1.0 - IGNORE_BOTTOM_RATIO):
                    continue
                all_matches.append({
                    'bbox': (x1, y1, x2, y2),
                    'pixel_score': 1.0,
                    'score': 1.0,
                    'scale': 1.0,
                })
            log("PERF", f"Grid proposal: {len(all_matches)} card(s) "
                f"in {_time.time()-_t0:.1f}s")

        if not all_matches:
            for query_name, refs in scan_refs.items():
                if self.target_query is None and TEMPLATE_REFS_PER_QUERY > 0:
                    probe_refs = refs[:TEMPLATE_REFS_PER_QUERY]
                else:
                    probe_refs = refs

                for ref_name, ref_img, ref_feat in probe_refs:
                    ref_gray = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY)
                    ref_h, ref_w = ref_gray.shape[:2]

                    for scale in scales:
                        scaled_w = int(ref_w * scale)
                        scaled_h = int(ref_h * scale)

                        if scaled_w < 15 or scaled_h < 15:
                            continue
                        if scaled_w >= screen_w or scaled_h >= screen_h:
                            continue

                        resized_template = cv2.resize(ref_gray, (scaled_w, scaled_h))

                        result = cv2.matchTemplate(
                            gray_screen, resized_template, cv2.TM_CCOEFF_NORMED
                        )

                        local_max = result >= cv2.dilate(result, np.ones((3, 3), np.uint8))
                        pixel_threshold = AUTO_PIXEL_THRESHOLD if AUTO_CALIBRATION else self.threshold
                        loc = np.where((result >= pixel_threshold) & local_max)
                        for pt in zip(*loc[::-1]):
                            x, y = pt
                            pixel_score = float(result[y, x])

                            if x < screen_w * IGNORE_LEFT_RATIO:
                                continue
                            if y > screen_h * (1.0 - IGNORE_BOTTOM_RATIO):
                                continue

                            all_matches.append({
                                'source_query': query_name,
                                'source_ref_name': ref_name,
                                'bbox': (x, y, x + scaled_w, y + scaled_h),
                                'pixel_score': pixel_score,
                                'score': pixel_score,
                                'scale': scale,
                            })

        # --- NMS + AI classification on all candidates ---
        if all_matches:
            all_matches = self._non_max_suppression(
                all_matches, iou_threshold=0.3)
            if len(all_matches) > MAX_PIXEL_CANDIDATES:
                all_matches = sorted(
                    all_matches,
                    key=lambda item: item['pixel_score'], reverse=True
                )[:MAX_PIXEL_CANDIDATES]

            _t1 = _time.time()
            log("PERF", f"Candidates: {len(all_matches)} "
                f"in {_t1-_t0:.1f}s")

            verified_matches = []
            
            def process_candidate(m):
                x, y, x2, y2 = m['bbox']
                candidate_bgr = screenshot_bgr[y:y2, x:x2]
                if candidate_bgr.shape[0] < 10 or candidate_bgr.shape[1] < 10:
                    return None
                classification = self._classify_candidate(candidate_bgr)
                if classification:
                    m_copy = dict(m)
                    m_copy.update(classification)
                    return m_copy
                return None

            with ThreadPoolExecutor() as executor:
                future_to_m = {executor.submit(process_candidate, m): m for m in all_matches}
                for future in as_completed(future_to_m):
                    result = future.result()
                    if result:
                        verified_matches.append(result)

            all_matches = verified_matches
            log("PERF", f"AI classify: {len(all_matches)} verified "
                f"in {_time.time()-_t1:.1f}s")

        if ENFORCE_SINGLE_QUERY:
            all_matches = dominant_query_only(all_matches)

        # VMS Grid Logic: Lọc chỉ giữ lại các match thuộc 2 hàng đầu tiên, và bỏ ảnh gốc ở góc trên trái
        if all_matches:
            y_coords = sorted(m['bbox'][1] for m in all_matches)
            rows = []
            current_row = [y_coords[0]]
            for y in y_coords[1:]:
                if y - current_row[-1] < 50:
                    current_row.append(y)
                else:
                    rows.append(current_row)
                    current_row = [y]
            rows.append(current_row)
            
            allowed_max_y = max(rows[0])
            if len(rows) > 1:
                allowed_max_y = max(rows[1])
            allowed_max_y += 30
            
            all_matches = [m for m in all_matches if m['bbox'][1] <= allowed_max_y]
            
            all_matches = self._remove_source_grid_match(all_matches, screenshot_bgr)

        # Extra precision gate: drop a card whose printed time is read
        # confidently and matches no reference time for that identity.
        all_matches = self._filter_matches_by_card_timestamp(all_matches, screenshot_bgr)

        # The source card has already been removed above. Apply the shared
        # post-source safety cap and align independently accepted result cards.
        all_matches = self._limit_and_align_matches(all_matches)

        # Per-card timestamp filtering is applied above via
        # _filter_matches_by_card_timestamp(); the early folder-level filter in
        # find_matches() only narrows which folders are scanned.

        return all_matches

    def _non_max_suppression(self, matches: list[dict], iou_threshold: float = 0.3) -> list[dict]:
        """Remove overlapping detections, keeping highest score."""
        if not matches:
            return []

        # Sort by score (highest first)
        matches.sort(key=lambda m: m['score'], reverse=True)

        keep = []
        for candidate in matches:
            is_overlapping = False
            for kept in keep:
                if self._compute_iou(candidate['bbox'], kept['bbox']) > iou_threshold:
                    is_overlapping = True
                    break
            if not is_overlapping:
                keep.append(candidate)

        return keep

    @staticmethod
    def _compute_iou(box_a: list | tuple, box_b: list | tuple) -> float:
        """Compute Intersection over Union between two boxes."""
        x1 = max(box_a[0], box_b[0])
        y1 = max(box_a[1], box_b[1])
        x2 = min(box_a[2], box_b[2])
        y2 = min(box_a[3], box_b[3])

        inter_area = max(0, x2 - x1) * max(0, y2 - y1)
        area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
        area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
        union_area = area_a + area_b - inter_area

        return inter_area / union_area if union_area > 0 else 0.0


# ============================================================
# BOX DRAWER
# ============================================================
def _card_inset_x(gray: np.ndarray, mid_x: int, check_y1: int, check_y2: int) -> tuple[int, int]:
    """Return the 6px-inset left/right edges of the card containing mid_x."""
    w_img = gray.shape[1]

    gap_left = mid_x
    while gap_left > 0:
        column = gray[check_y1:check_y2, gap_left]
        if column.mean() < 26 and column.var() < 5:
            break
        gap_left -= 1
    card_start = gap_left + 1 if gap_left > 0 else 0

    image_edge_left = card_start
    for px in range(card_start, mid_x):
        column = gray[check_y1:check_y2, px]
        if column.mean() >= 32 or column.var() >= 5:
            image_edge_left = px
            break

    gap_right = mid_x
    while gap_right < w_img - 1:
        column = gray[check_y1:check_y2, gap_right]
        if column.mean() < 26 and column.var() < 5:
            break
        gap_right += 1
    card_end = gap_right - 1 if gap_right < w_img - 1 else w_img - 1

    image_edge_right = card_end
    for px in range(card_end, mid_x, -1):
        column = gray[check_y1:check_y2, px]
        if column.mean() >= 32 or column.var() >= 5:
            image_edge_right = px
            break

    return min(card_start + 6, image_edge_left), max(card_end - 6, image_edge_right)


def _snap_box_to_card(gray: np.ndarray, bbox: tuple) -> tuple[int, int, int, int]:
    """Snap a box onto its UI card, then apply the required 6px inset.

    If the camera image begins inside that inset, the box is clamped to the
    image edge so the border never cuts into the visible camera area.
    """
    h_img, w_img = gray.shape
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(int(x1), w_img - 1))
    x2 = max(x1 + 1, min(int(x2), w_img))
    y1 = max(0, min(int(y1), h_img - 1))
    y2 = max(y1 + 1, min(int(y2), h_img - 1))

    while y1 > 0 and gray[y1, x1:x2].mean() > 32:
        y1 -= 1
    while y2 < h_img - 1 and gray[y2, x1:x2].mean() > 32:
        y2 += 1

    check_y1 = y1 + (y2 - y1) // 4
    check_y2 = y2 - (y2 - y1) // 4
    if check_y2 <= check_y1:
        check_y1, check_y2 = y1, max(y1 + 1, y2)

    new_x1, new_x2 = _card_inset_x(gray, (x1 + x2) // 2, check_y1, check_y2)
    return new_x1, y1, new_x2, y2


def box_at_point(image_bgr: np.ndarray, x: float, y: float) -> tuple[int, int, int, int] | None:
    """Return the card box under a click, or None when it lands off any card."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    h_img, w_img = gray.shape
    click_x, click_y = int(round(x)), int(round(y))
    if not (0 <= click_x < w_img and 0 <= click_y < h_img):
        return None

    # A short band around the click is enough to locate the card horizontally;
    # _snap_box_to_card then re-measures it with the same rule used when drawing.
    check_y1 = max(0, click_y - 3)
    check_y2 = min(h_img, click_y + 4)
    seed_x1, seed_x2 = _card_inset_x(gray, click_x, check_y1, check_y2)
    if seed_x2 - seed_x1 < CLICK_BOX_MIN_SIZE:
        return None

    x1, y1, x2, y2 = _snap_box_to_card(gray, (seed_x1, click_y, seed_x2, click_y + 1))
    if x2 - x1 < CLICK_BOX_MIN_SIZE or y2 - y1 < CLICK_BOX_MIN_SIZE:
        return None
    return x1, y1, x2, y2


def toggle_box_at_point(
    image_bgr: np.ndarray,
    matches: list[dict],
    x: float,
    y: float,
    default_query: str = "Query_Mac_Dinh",
) -> bool:
    """Add or remove the box for the card under a click; True when it changed."""
    for match in list(matches):
        bx1, by1, bx2, by2 = match["bbox"]
        if bx1 <= x <= bx2 and by1 <= y <= by2:
            matches.remove(match)
            return True

    bbox = box_at_point(image_bgr, x, y)
    if bbox is None:
        return False

    # The click may land on the card padding outside an existing box; treat that
    # as removing that card's box instead of stacking a duplicate on top of it.
    for match in list(matches):
        bx1, by1, bx2, by2 = match["bbox"]
        if bbox[0] <= (bx1 + bx2) // 2 <= bbox[2] and bbox[1] <= (by1 + by2) // 2 <= bbox[3]:
            matches.remove(match)
            return True

    matches.append({
        "bbox": bbox,
        "score": 1.0,
        "query": matches[0]["query"] if matches else default_query,
    })
    return True


def draw_match_boxes(image_bgr: np.ndarray, matches: list[dict]) -> np.ndarray:
    """Draw red bounding boxes on the image."""
    result = image_bgr.copy()

    # Create grayscale for boundary detection
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)

    # Define a palette of vibrant colors for different queries (BGR format)
    palette = [
        (0, 0, 255),    # Red
        (255, 0, 0),    # Blue
        (0, 255, 0),    # Green
        (0, 165, 255),  # Orange
        (255, 0, 255),  # Magenta
        (255, 255, 0),  # Cyan
    ]
    query_colors = {}

    for match in matches:
        match["bbox"] = _snap_box_to_card(gray, match["bbox"])

    for match in matches:
        x1, y1, x2, y2 = match["bbox"]
        query_name = match.get("query", "Query_1")
        if query_name not in query_colors:
            query_colors[query_name] = palette[len(query_colors) % len(palette)]
        cv2.rectangle(result, (x1, y1), (x2, y2), query_colors[query_name], BOX_THICKNESS)

    return result


# ============================================================
# CLIPBOARD UTILITIES
# ============================================================
def is_actual_screenshot() -> bool:
    """Verify if the clipboard data looks like a pure screenshot (no HTML/URL metadata formats)."""
    try:
        blocked_keywords = ("html", "url", "chrome", "link", "mime")
        win32clipboard.OpenClipboard()
        try:
            fmt = 0
            while True:
                fmt = win32clipboard.EnumClipboardFormats(fmt)
                if fmt == 0:
                    break
                try:
                    name = win32clipboard.GetClipboardFormatName(fmt)
                    name_lower = name.lower()
                    if any(kw in name_lower for kw in blocked_keywords):
                        return False
                except Exception:
                    # GetClipboardFormatName raises pywintypes.error for
                    # standard/system format IDs — safe to skip them.
                    pass
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        # OpenClipboard / EnumClipboardFormats can also raise
        # pywintypes.error when the clipboard is locked by another app.
        pass
    return True


def get_clipboard_image_win32() -> Image.Image | list[str] | None:
    try:
        win32clipboard.OpenClipboard()
        try:
            # 1. Try CF_DIB (standard clipboard image format)
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
                data = win32clipboard.GetClipboardData(win32clipboard.CF_DIB)
                if data:
                    header_size = int.from_bytes(data[:4], 'little')
                    bit_count = int.from_bytes(data[14:16], 'little')
                    if len(data) >= 36:
                        colors_used = int.from_bytes(data[32:36], 'little')
                    else:
                        colors_used = 0
                        
                    if colors_used > 0:
                        palette_size = colors_used * 4
                    elif bit_count <= 8:
                        palette_size = (2 ** bit_count) * 4
                    else:
                        palette_size = 0
                        
                    pixel_offset = 14 + header_size + palette_size
                    file_size = 14 + len(data)
                    
                    bmp_header = b'BM' + \
                                 file_size.to_bytes(4, 'little') + \
                                 (0).to_bytes(4, 'little') + \
                                 pixel_offset.to_bytes(4, 'little')
                                 
                    return Image.open(io.BytesIO(bmp_header + data))
                    
            # 2. Try CF_HDROP (copied files list)
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_HDROP):
                files = win32clipboard.GetClipboardData(win32clipboard.CF_HDROP)
                if files:
                    # Only return image files modified in the last 5.0 seconds
                    import time
                    valid_files = []
                    valid_exts = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')
                    for path in files:
                        if isinstance(path, str) and os.path.exists(path) and path.lower().endswith(valid_exts):
                            if time.time() - os.path.getmtime(path) < 5.0:
                                valid_files.append(path)
                    if valid_files:
                        return valid_files
        finally:
            win32clipboard.CloseClipboard()
    except Exception as e:
        print(f"[WIN32 CLIPBOARD FALLBACK ERROR] {e}")
    return None


def get_clipboard_image() -> Image.Image | None:
    """Get current image from clipboard, returns PIL Image or None."""
    if not is_actual_screenshot():
        return None
        
    try:
        img = ImageGrab.grabclipboard()
        if img is not None:
            if isinstance(img, Image.Image):
                return img
            elif isinstance(img, list):
                import time
                valid_exts = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')
                for path in img:
                    if isinstance(path, str) and os.path.exists(path) and path.lower().endswith(valid_exts):
                        if time.time() - os.path.getmtime(path) < 5.0:
                            return Image.open(path)
    except Exception as e:
        print(f"[CLIPBOARD ImageGrab WARNING] {e}")

    try:
        fallback = get_clipboard_image_win32()
        if fallback is not None:
            if isinstance(fallback, Image.Image):
                return fallback
            elif isinstance(fallback, (list, tuple)):
                valid_exts = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')
                for path in fallback:
                    if isinstance(path, str) and os.path.exists(path) and path.lower().endswith(valid_exts):
                        return Image.open(path)
    except Exception as e:
        print(f"[CLIPBOARD Win32 Fallback WARNING] {e}")

    return None


def _get_clipboard_sequence_number() -> int | None:
    """Return Windows' clipboard generation number when available."""
    try:
        user32 = ctypes.windll.user32
        user32.GetClipboardSequenceNumber.restype = ctypes.c_uint32
        return int(user32.GetClipboardSequenceNumber())
    except (AttributeError, OSError):
        return None


def _clipboard_token(payload: bytes) -> str:
    """Hash image bytes and include clipboard generation when available."""
    digest = hashlib.md5(payload).hexdigest()
    sequence = _get_clipboard_sequence_number()
    return f"{sequence}:{digest}" if sequence is not None else digest


def get_clipboard_image_hash() -> str | None:
    """Return a change token for the current clipboard image.

    The Windows sequence number matters because ShareX can recapture the same
    region, producing identical pixels but a new clipboard event.
    """
    if not is_actual_screenshot():
        return None
        
    try:
        img = ImageGrab.grabclipboard()
        if img is not None:
            if isinstance(img, Image.Image):
                small = img.resize((64, 64))
                return _clipboard_token(small.tobytes())
            elif isinstance(img, list):
                import time
                hash_input = ""
                for path in img:
                    if isinstance(path, str) and os.path.exists(path):
                        if time.time() - os.path.getmtime(path) < 5.0:
                            hash_input += f"{path}:{os.path.getmtime(path)}"
                if hash_input:
                    return _clipboard_token(hash_input.encode())
    except Exception as e:
        print(f"[CLIPBOARD ImageGrab Hash WARNING] {e}")
        
    try:
        fallback = get_clipboard_image_win32()
        if fallback is not None:
            if isinstance(fallback, Image.Image):
                small = fallback.resize((64, 64))
                return _clipboard_token(small.tobytes())
            elif isinstance(fallback, (list, tuple)):
                import time
                hash_input = ""
                for path in fallback:
                    if isinstance(path, str) and os.path.exists(path):
                        if time.time() - os.path.getmtime(path) < 5.0:
                            hash_input += f"{path}:{os.path.getmtime(path)}"
                if hash_input:
                    return _clipboard_token(hash_input.encode())
    except Exception as e:
        print(f"[CLIPBOARD Win32 Fallback Hash WARNING] {e}")
        
    return None


def copy_image_to_clipboard(pil_image: Image.Image) -> None:
    """Copy a PIL Image to Windows clipboard as BMP."""
    output = io.BytesIO()
    pil_image.convert('RGB').save(output, 'BMP')
    bmp_data = output.getvalue()[14:]  # Skip BMP file header (14 bytes)
    output.close()

    for i in range(5):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp_data)
            finally:
                win32clipboard.CloseClipboard()
            break
        except Exception as e:
            time.sleep(0.1)
            if i == 4:
                log("ERROR", f"Failed to copy to clipboard: {e}")


# ============================================================
# NOTIFICATION
# ============================================================
def notify_sound(success: bool = True) -> None:
    """Play a system sound notification and show a Windows toast."""
    try:
        from plyer import notification
        
        if success:
            # MB_ICONASTERISK - info sound
            ctypes.windll.user32.MessageBeep(0x00000040)
            # Show toast notification without stealing focus
            notification.notify(
                title='ReID Auto Draw',
                message='Đã vẽ xong và chép vào Clipboard!',
                app_name='ReID Auto Draw',
                timeout=2
            )
        else:
            # MB_ICONHAND - error sound
            ctypes.windll.user32.MessageBeep(0x00000010)
            notification.notify(
                title='ReID Auto Draw',
                message='Không tìm thấy kết quả nào!',
                app_name='ReID Auto Draw',
                timeout=2
            )
    except (ImportError, OSError, RuntimeError) as e:
        log("ERROR", f"Notification failed: {e}")


# ============================================================
# PROCESS SINGLE IMAGE
# ============================================================
def process_image(matcher: TemplateMatcher, image_bgr: np.ndarray, debug: bool = False) -> tuple[np.ndarray | None, list[dict]]:
    """
    Process a single image: find matches, draw boxes, return result.
    Returns (marked_image_bgr, matches_list) or (None, []) if no matches.
    """
    log("SCAN", "Running template matching...")
    start_time = time.time()

    matches = matcher.find_matches(image_bgr, debug=debug)
    elapsed = time.time() - start_time

    if not matches:
        log("RESULT", "No matches found after removing the original query image.")
        return None, []

    log("RESULT", f"Found {len(matches)} match(es) in {elapsed:.1f}s:")
    for m in matches:
        log("MATCH",
            f"  {m['query']}/{m['ref_name']} "
            f"(score: {m['score']:.3f}, scale: {m['scale']:.2f})")

    # Draw boxes
    marked = draw_match_boxes(image_bgr, matches)

    return marked, matches


def save_result(marked_bgr: np.ndarray, output_dir: str) -> str:
    """Save marked image to output directory, returns filepath."""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"marked_{timestamp}.png"
    filepath = os.path.join(output_dir, filename)
    cv2.imwrite(filepath, marked_bgr)
    return filepath


def _serialize_matches(matches: list[dict]) -> list[dict]:
    """Convert match dicts to JSON-safe format (tuples→lists, numpy→float)."""
    result = []
    for m in matches:
        entry = {}
        for k, v in m.items():
            if k == "bbox":
                entry[k] = list(v)
            elif isinstance(v, (np.floating, np.integer)):
                entry[k] = float(v)
            elif isinstance(v, dict):
                entry[k] = {
                    sk: float(sv) if isinstance(sv, (np.floating, np.integer)) else sv
                    for sk, sv in v.items()
                }
            else:
                entry[k] = v
        result.append(entry)
    return result


def save_result_with_metadata(
    marked_bgr: np.ndarray,
    original_bgr: np.ndarray,
    matches: list[dict],
    output_dir: str,
    query_name: str | None = None,
) -> str:
    """Save marked image + original image + JSON sidecar to output directory.

    When query_name is provided, files are saved under output_dir/<query_name>/.
    Returns the filepath of the marked image.
    """
    target_dir = os.path.join(output_dir, query_name) if query_name else output_dir
    os.makedirs(target_dir, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    marked_filename = f"marked_{timestamp}.png"
    marked_filepath = os.path.join(target_dir, marked_filename)
    cv2.imwrite(marked_filepath, marked_bgr)

    original_filename = f"original_{timestamp}.png"
    original_filepath = os.path.join(target_dir, original_filename)
    cv2.imwrite(original_filepath, original_bgr)

    json_filename = f"marked_{timestamp}.json"
    json_filepath = os.path.join(target_dir, json_filename)
    with open(json_filepath, "w", encoding="utf-8") as f:
        json.dump({
            "matches": _serialize_matches(matches),
            "marked_file": marked_filename,
            "original_file": original_filename,
            "timestamp": timestamp,
        }, f, ensure_ascii=False, indent=2)

    return marked_filepath


def load_metadata(marked_filepath: str) -> dict | None:
    """Load the JSON sidecar for a marked image.

    Returns {"original_path": str, "matches": list[dict]} or None if missing.
    """
    base, _ = os.path.splitext(marked_filepath)
    json_path = base + ".json"
    if not os.path.isfile(json_path):
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    directory = os.path.dirname(marked_filepath)
    original_file = data.get("original_file", "")
    original_path = os.path.join(directory, original_file)
    if not os.path.isfile(original_path):
        return None

    matches = data.get("matches", [])
    for m in matches:
        if "bbox" in m and isinstance(m["bbox"], list):
            m["bbox"] = tuple(m["bbox"])

    return {"original_path": original_path, "matches": matches}


def update_metadata(marked_filepath: str, matches: list[dict]) -> None:
    """Update the matches in an existing JSON sidecar."""
    base, _ = os.path.splitext(marked_filepath)
    json_path = base + ".json"
    if not os.path.isfile(json_path):
        return
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    data["matches"] = _serialize_matches(matches)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# CLIPBOARD MONITOR MODE
# ============================================================
def run_clipboard_monitor(matcher, output_dir, debug=False, stop_event=None, preview_callback=None):
    """Main loop: monitor clipboard for new screenshots."""
    print("\n" + "=" * 60)
    log("READY", "Monitoring clipboard for screenshots...")
    log("INFO", f"Output directory: {output_dir}")
    log("INFO", "Press Ctrl+C to stop")
    print("=" * 60 + "\n")

    last_hash = get_clipboard_image_hash()
    consecutive_errors = 0

    try:
        while True:
            if stop_event and stop_event.is_set():
                break
            time.sleep(POLL_INTERVAL)

            try:
                current_hash = get_clipboard_image_hash()
            except Exception:
                consecutive_errors += 1
                if consecutive_errors > 10:
                    log("ERROR", "Too many clipboard read errors. Waiting 5s...")
                    time.sleep(5)
                    consecutive_errors = 0
                continue

            consecutive_errors = 0

            # No image or same image as before
            if current_hash is None or current_hash == last_hash:
                continue

            last_hash = current_hash
            
            # Check if we should ignore this clipboard change (because it came from our own Save & Copy)
            if getattr(matcher, 'ignore_next_clipboard', False):
                matcher.ignore_next_clipboard = False
                log("INFO", "Ignoring clipboard change triggered by Preview Save.")
                continue
                
            # --- NEW IMAGE DETECTED ---
            log("DETECT", "New image found in clipboard.")
            
            pil_img = get_clipboard_image()
            if pil_img is None:
                continue
                
            current_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

            # --- PROCESS SCREENSHOT ---
            # If preview_callback is provided, we don't draw or save here.
            # We just find matches and pass to the callback.
            start_time = time.time()
            matches = matcher.find_matches(current_bgr, debug=debug)
                        
            elapsed = time.time() - start_time
            if not matches:
                log("RESULT", "No matches found.")
                notify_sound(success=False)
                
                if preview_callback:
                    preview_callback(current_bgr, [], matcher, output_dir)
                    time.sleep(0.5)
                    last_hash = get_clipboard_image_hash()
                continue
            log("RESULT", f"Found {len(matches)} match(es) in {elapsed:.1f}s.")
            
            if preview_callback:
                # GUI handles drawing, saving, and putting to clipboard!
                # It will also set matcher.ignore_next_clipboard = True when it saves
                preview_callback(current_bgr, matches, matcher, output_dir)
            else:
                marked_bgr = draw_match_boxes(current_bgr.copy(), matches)
                
                # Save to file
                filepath = save_result(marked_bgr, output_dir)
                log("SAVE", f"Saved: {filepath}")

                # Copy to clipboard
                marked_pil = Image.fromarray(cv2.cvtColor(marked_bgr, cv2.COLOR_BGR2RGB))
                copy_image_to_clipboard(marked_pil)

                # Update hash to avoid re-processing our own clipboard write
                time.sleep(0.2)
                last_hash = get_clipboard_image_hash()

                log("CLIPBOARD", "Marked image copied to clipboard!")
                notify_sound(success=True)

            print("-" * 50)

    except KeyboardInterrupt:
        print(f"\n{'=' * 60}")
        log("STOP", "Clipboard monitoring stopped.")
        print(f"{'=' * 60}")
        if debug:
            cv2.destroyAllWindows()


# ============================================================
# SINGLE FILE MODE
# ============================================================
def run_single_file(matcher, image_path, output_dir, debug=False):
    """Process a single image file."""
    if not os.path.exists(image_path):
        log("ERROR", f"File not found: {image_path}")
        sys.exit(1)

    screenshot_bgr = cv2.imread(image_path)
    if screenshot_bgr is None:
        log("ERROR", f"Cannot read image: {image_path}")
        sys.exit(1)

    log("INFO", f"Processing: {image_path}")
    log("INFO", f"Size: {screenshot_bgr.shape[1]}x{screenshot_bgr.shape[0]}")

    marked_bgr, matches = process_image(matcher, screenshot_bgr, debug=debug)

    if marked_bgr is None:
        log("INFO", "No reference images found in this screenshot.")
        return

    # Save
    filepath = save_result(marked_bgr, output_dir)
    log("SAVE", f"Saved: {filepath}")

    # Copy to clipboard
    marked_pil = Image.fromarray(cv2.cvtColor(marked_bgr, cv2.COLOR_BGR2RGB))
    copy_image_to_clipboard(marked_pil)
    log("CLIPBOARD", "Marked image copied to clipboard!")

    # Show result if debug
    if debug:
        debug_display = cv2.resize(
            marked_bgr, (0, 0),
            fx=min(1.0, 1400 / marked_bgr.shape[1]),
            fy=min(1.0, 800 / marked_bgr.shape[0])
        )
        cv2.imshow("ReID Auto Draw Result", debug_display)
        print("\n  Press any key to close...")
        cv2.waitKey(0)
        cv2.destroyAllWindows()


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="ReID Auto Draw: Auto-detect and highlight matching images in screenshots"
    )
    parser.add_argument(
        '--threshold', '-t', type=float, default=MATCH_THRESHOLD,
        help=f'Match confidence threshold (0.0-1.0, default: {MATCH_THRESHOLD})'
    )
    parser.add_argument(
        '--query', '-q', type=str, default=None,
        help='Match only against a specific query folder (e.g., Query_1)'
    )
    parser.add_argument(
        '--single', '-s', type=str, default=None,
        help='Process a single image file instead of monitoring clipboard'
    )
    parser.add_argument(
        '--debug', '-d', action='store_true',
        help='Show debug visualization window'
    )
    parser.add_argument(
        '--queries-dir', type=str, default=QUERIES_DIR,
        help=f'Path to reference images directory (default: {QUERIES_DIR})'
    )
    parser.add_argument(
        '--output-dir', type=str, default=OUTPUT_DIR,
        help=f'Path to output directory (default: {OUTPUT_DIR})'
    )

    args = parser.parse_args()

    # Banner
    print()
    print("  ╔════════════════════════════════════════════════╗")
    print("  ║          IMAGE AUTO-MARKER v1.0               ║")
    print("  ║  Auto-detect & highlight matching images      ║")
    print("  ╚════════════════════════════════════════════════╝")
    print()

    # Show settings
    log("CONFIG", f"Threshold: {args.threshold}")
    log("CONFIG", f"Queries dir: {args.queries_dir}")
    log("CONFIG", f"Output dir: {args.output_dir}")
    if args.query:
        log("CONFIG", f"Target query: {args.query}")
    if args.debug:
        log("CONFIG", "Debug mode: ON")

    # Initialize matcher
    matcher = TemplateMatcher(
        queries_dir=args.queries_dir,
        threshold=args.threshold,
        target_query=args.query
    )

    total_refs = sum(len(refs) for refs in matcher.reference_images.values())
    if total_refs == 0:
        print()
        log("WARNING", "No reference images found!")
        print()
        print("  Please add reference images to the queries directory:")
        print(f"  {args.queries_dir}")
        print()
        print("  Expected structure:")
        print("    queries/")
        print("    ├── Query_1/")
        print("    │   ├── _query.jpg      <- Query image (EXCLUDED)")
        print("    │   ├── result_1.jpg    <- Will be matched")
        print("    │   ├── result_2.jpg")
        print("    │   └── result_3.jpg")
        print("    ├── Query_2/")
        print("    │   ├── _query.jpg")
        print("    │   └── result_1.jpg")
        print("    └── ...")
        print()
        print("  TIP: Use crop_tool.py to crop images from screenshots")
        print("       python crop_tool.py <image_path> <query_name>")
        print()
        sys.exit(1)

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Run in appropriate mode
    if args.single:
        run_single_file(matcher, args.single, args.output_dir, args.debug)
    else:
        run_clipboard_monitor(matcher, args.output_dir, args.debug)


if __name__ == "__main__":
    main()
