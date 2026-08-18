"""Real-data PoC: does the LBP appearance descriptor separate identities?

Runs standalone (no changes to auto_marker.py). Loads the reference crops under
``queries/Query_N`` — each folder is one person — computes LBP descriptors, and
compares the same-person similarity distribution against the different-person
distribution. Prints both distributions and an AUC-like separability score.

This is the go/no-go check the failed pose experiment lacked up front. Pose gave
AUC ~0.50 (no separation, see memory ``pose-matching-poc-result``). This script
exits non-zero when the signal fails ``MIN_ACCEPTABLE_AUC``, so it can actually
report a negative result instead of always passing.

Caveat on the evidence: the crops under one Query folder come from a single
capture session seconds apart, which flatters the same-person distribution. Treat
the AUC as a floor-level sanity check, not a benchmark.

Usage (Windows, Vietnamese-safe stdout):
    set PYTHONIOENCODING=utf-8 && python poc_appearance_matching.py
"""

import os
import sys
import glob
import itertools

import numpy as np
import cv2

import config
from appearance_extractor import AppearanceExtractor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUERIES_DIR = os.path.join(BASE_DIR, "queries")

# Pre-declared go/no-go bar. 0.5 means "no separation at all" (the pose result);
# anything at or below NO_SIGNAL_AUC would be a repeat of that failure.
MIN_ACCEPTABLE_AUC = 0.65
NO_SIGNAL_AUC = 0.55

IMAGE_EXTENSIONS = ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tif", "*.tiff", "*.webp")


def load_person_descriptors(extractor):
    """Return {person_name: [descriptor, ...]} for every Query_N folder."""
    people = {}
    if not os.path.isdir(QUERIES_DIR):
        print(f"[ERROR] queries dir not found: {QUERIES_DIR}")
        return people

    for name in sorted(os.listdir(QUERIES_DIR)):
        folder = os.path.join(QUERIES_DIR, name)
        if not os.path.isdir(folder):
            continue
        paths = []
        for pattern in IMAGE_EXTENSIONS:
            paths.extend(glob.glob(os.path.join(folder, pattern)))
        descriptors = []
        skipped = 0
        for path in sorted(set(paths)):
            img = cv2.imread(path, cv2.IMREAD_COLOR)
            if img is None:
                continue
            payload = extractor.extract_descriptor(img)
            if payload is not None:
                descriptors.append(payload)
            else:
                skipped += 1
        if skipped:
            print(f"  [SKIP] {name}: {skipped} crop(s) too small/flat to describe")
        if len(descriptors) >= 2:
            people[name] = descriptors
    return people


def summarize(label, values):
    if not values:
        print(f"  {label}: (no pairs)")
        return
    arr = np.asarray(values, dtype=np.float64)
    print(
        f"  {label}: n={arr.size}  "
        f"min={arr.min():.3f}  mean={arr.mean():.3f}  max={arr.max():.3f}"
    )


def auc_like(same, diff):
    """Probability a random same-person pair scores above a random diff pair.

    Mann-Whitney U / (n*m). 0.5 = no separation, 1.0 = perfect.
    """
    if not same or not diff:
        return float("nan")
    same = np.asarray(same)
    diff = np.asarray(diff)
    wins = 0.0
    for s in same:
        wins += np.sum(s > diff) + 0.5 * np.sum(s == diff)
    return wins / (same.size * diff.size)


def main():
    extractor = AppearanceExtractor()
    people = load_person_descriptors(extractor)
    if len(people) < 2:
        print("[ERROR] need at least 2 people with >=2 crops each. Found:",
              {k: len(v) for k, v in people.items()})
        return 1

    print(f"Loaded {len(people)} people: "
          + ", ".join(f"{k}={len(v)}" for k, v in people.items()))

    same_scores = []
    for name, descs in people.items():
        for a, b in itertools.combinations(descs, 2):
            same_scores.append(AppearanceExtractor.compute_similarity(a, b))

    diff_scores = []
    names = list(people.keys())
    for na, nb in itertools.combinations(names, 2):
        for a in people[na]:
            for b in people[nb]:
                diff_scores.append(AppearanceExtractor.compute_similarity(a, b))

    print("\nSimilarity distributions:")
    summarize("same-person", same_scores)
    summarize("diff-person", diff_scores)

    auc = auc_like(same_scores, diff_scores)
    print(f"\nSeparability (AUC-like): {auc:.3f}  "
          f"(0.5 = no separation, pose baseline ~0.50)")

    overlap = min(same_scores) < max(diff_scores)
    print(f"Same-min={min(same_scores):.3f}  diff-max={max(diff_scores):.3f}  "
          f"{'OVERLAP' if overlap else 'CLEAN SPLIT'}")

    # How the configured rescue floor would actually behave on this data.
    floor = getattr(config, "APPEARANCE_SIMILARITY_FLOOR", None)
    if floor is not None:
        same_pass = 100.0 * np.mean(np.asarray(same_scores) >= floor)
        diff_pass = 100.0 * np.mean(np.asarray(diff_scores) >= floor)
        print(f"\nAPPEARANCE_SIMILARITY_FLOOR={floor:.2f} would pass "
              f"{same_pass:.1f}% of same-person and {diff_pass:.1f}% of diff-person pairs.")
        if diff_pass > 95.0:
            print("  [WARN] floor passes nearly every different-person pair — "
                  "it is inert, like the old pose soft gate.")

    if auc <= NO_SIGNAL_AUC:
        print(f"\n[FAIL] AUC {auc:.3f} <= {NO_SIGNAL_AUC:.2f}: no usable signal. "
              "Do NOT integrate (same outcome as pose).")
        return 2
    if auc < MIN_ACCEPTABLE_AUC:
        print(f"\n[FAIL] AUC {auc:.3f} below the {MIN_ACCEPTABLE_AUC:.2f} bar "
              "required before integration.")
        return 1
    print(f"\n[PASS] AUC {auc:.3f} >= {MIN_ACCEPTABLE_AUC:.2f}. Signal is usable as a "
          "rescue/tie-breaker (still config-OFF; not wired into auto_marker.py).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
