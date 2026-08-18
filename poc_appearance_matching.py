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

# Ngưỡng false-support tối đa chấp nhận được khi chọn APPEARANCE_RESCUE_MARGIN.
MAX_FALSE_SUPPORT_RATE = 5.0
MARGIN_SWEEP = [round(0.02 + 0.01 * i, 2) for i in range(14)]  # 0.02 … 0.15

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


def identity_scores(descriptor, people, top_k, min_refs, exclude=None):
    """Reproduce the runtime aggregation: one score per identity.

    Mirrors ``TemplateMatcher._appearance_scores_by_query`` — mean of the top-k
    per-reference similarities, and an identity is only scored when it still has
    ``min_refs`` usable descriptors. ``exclude`` is the (person, index) held out
    so a crop is never compared against itself.
    """
    scored = []
    for name, descs in people.items():
        sims = [
            AppearanceExtractor.compute_similarity(descriptor, ref)
            for index, ref in enumerate(descs)
            if exclude != (name, index)
        ]
        if len(sims) < min_refs:
            continue
        k = min(top_k, len(sims))
        scored.append((float(np.mean(sorted(sims, reverse=True)[:k])), name))
    scored.sort(reverse=True)
    return scored


def sweep_rescue_margin(people, floor):
    """Leave-one-out sweep for APPEARANCE_RESCUE_MARGIN at the identity level.

    The pair-level distribution above is NOT the runtime decision: at runtime a
    tie is broken by (top-k mean per identity, then the top1-top2 gap). Picking a
    threshold from pair statistics is exactly how the old
    ``POSE_SIMILARITY_THRESHOLD=0.25`` ended up inert, so measure the real shape.

    Prints, per candidate margin, how often the appearance tie-break would back
    the WRONG identity (false support) versus the right one (true support), and
    returns the smallest margin whose false-support rate is under
    ``MAX_FALSE_SUPPORT_RATE`` — or None when no swept value is safe.
    """
    top_k = getattr(config, "AI_TOP_K_REFERENCES", 2)
    min_refs = getattr(config, "APPEARANCE_MIN_REFERENCES", 2)

    # One ranked identity list per held-out crop, computed once and reused for
    # every margin in the sweep.
    trials = []
    for person, descs in people.items():
        for index, descriptor in enumerate(descs):
            ranked = identity_scores(
                descriptor, people, top_k, min_refs, exclude=(person, index)
            )
            if len(ranked) < 2:
                continue
            (top_score, top_name), (second_score, _) = ranked[0], ranked[1]
            trials.append((person, top_name, top_score, top_score - second_score))

    if not trials:
        print("\n[WARN] not enough crops for a leave-one-out margin sweep "
              f"(need >= {min_refs + 1} per person).")
        return None

    # Baseline before any gate: does the identity-level top-1 even pick the right
    # person, and how big are the gaps? Without this, a 0% false-support row is
    # indistinguishable from a gate that simply never fires.
    correct_top1 = sum(1 for t in trials if t[1] == t[0])
    gaps = np.asarray([t[3] for t in trials])
    print(f"\nIdentity-level leave-one-out sweep (n={len(trials)} held-out crops, "
          f"top_k={top_k}, min_refs={min_refs}, floor={floor:.2f}):")
    print(f"  top-1 identity correct (no gate): {correct_top1}/{len(trials)} "
          f"({100.0 * correct_top1 / len(trials):.1f}%)")
    print(f"  top1-top2 gap: min={gaps.min():.3f}  median={np.median(gaps):.3f}  "
          f"max={gaps.max():.3f}")
    print("  margin  true-support  false-support")

    safe = None
    for margin in MARGIN_SWEEP:
        supported = [t for t in trials if t[2] >= floor and t[3] >= margin]
        true_hits = sum(1 for t in supported if t[1] == t[0])
        false_hits = len(supported) - true_hits
        true_rate = 100.0 * true_hits / len(trials)
        false_rate = 100.0 * false_hits / len(trials)
        flag = ""
        if false_rate <= MAX_FALSE_SUPPORT_RATE and safe is None and true_hits > 0:
            safe = margin
            flag = "  <-- smallest safe margin"
        print(f"  {margin:.2f}    {true_rate:5.1f}%        {false_rate:5.1f}%{flag}")

    if safe is None:
        print(f"  [WARN] no margin in {MARGIN_SWEEP[0]:.2f}–{MARGIN_SWEEP[-1]:.2f} "
              f"keeps false support under {MAX_FALSE_SUPPORT_RATE:.0f}% while still "
              "firing — the tie-break would be either wrong or inert.")
    else:
        print(f"  => set APPEARANCE_RESCUE_MARGIN = {safe:.2f}")

    configured = getattr(config, "APPEARANCE_RESCUE_MARGIN", None)
    if configured is not None and safe is not None and configured < safe:
        print(f"  [WARN] configured APPEARANCE_RESCUE_MARGIN={configured:.2f} is below "
              f"the measured safe value {safe:.2f}.")
    return safe


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
        sweep_rescue_margin(people, floor)

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
