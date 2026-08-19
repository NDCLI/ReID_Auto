---
name: pose_matching_research_plan
description: SUPERSEDED — pose failed real-data PoC; replaced by LBP appearance signal
metadata:
  type: project
---

> **⚠️ SUPERSEDED (2026-08-18).** Pose matching was built and PoC-tested but did
> NOT separate identities on the real result-card thumbnails (same-person and
> different-person similarity fully overlap — everyone stands the same way, so a
> position/scale-invariant skeleton collapses to a near-identical descriptor).
> The pose module and its tests were removed. The fourth signal is now an **LBP
> clothing-texture descriptor** (`appearance_extractor.py`, PoC in
> `poc_appearance_matching.py`). This document is kept only as the record of the
> negative result. See the project memory `pose-matching-poc-result` for numbers.

## Pose Matching Enhancement Plan

### Phase 1: Research & Evaluation (2-3 tuần)

#### 1.1 Model Selection
- **MediaPipe Pose** (recommended for quick start)
  - 33 keypoints (body + hands + face landmarks)
  - Real-time on CPU, lightweight (~80MB)
  - No training needed, pre-trained on COCO
  - Pros: Fast deployment, high accuracy on frontal poses
  - Cons: Fails on side views, occlusion

- **OpenPose** (alternative, more robust)
  - 17 keypoints (body only)
  - Handles multi-person, side views better
  - Heavier (500MB+), requires GPU for speed
  - Better for VMS multi-angle scenarios

- **YOLOv8-Pose** (modern alternative)
  - 17 keypoints
  - Can fine-tune on custom data
  - Good balance of speed/accuracy
  - Built-in PyTorch training pipeline

#### 1.2 Proof of Concept (PoC)
```
Tasks:
1. Install MediaPipe Pose, run on Query_8 reference + screenshot
2. Extract skeleton from both, visualize keypoints
3. Compute pose similarity (Euclidean distance between normalized keypoints)
4. Measure correlation: high pose_sim when same person, low when different person
5. Test on 5-10 Query folders to validate assumption
```

#### 1.3 Benchmark Current Failures
- Collect cases where ReID currently fails (like Query 8 11:51 AM)
- Check if pose difference explains the rejection
- Identify: is it true negative (good rejection) or false negative (should match)?

---

### Phase 2: Integration Design (1 tuần)

#### 2.1 Architecture
```python
# New class: PoseExtractor
class PoseExtractor:
    def __init__(self, model="mediapipe"):
        self.model = model
    
    def extract_keypoints(self, image_bgr) -> dict:
        # Returns: {"keypoints": [[x,y,conf], ...], "pose_descriptor": [..]}
        pass
    
    def compute_pose_similarity(self, kpt1, kpt2) -> float:
        # Normalized L2 distance or DTW
        # Returns: 0.0-1.0
        pass
```

#### 2.2 Scoring Strategy — Multi-Stage Filtering

**Philosophy:** Lower initial threshold → More candidates → Filter gradually by multiple criteria → Enforce reference count cap

```python
def _rank_features_with_pose_multistage(candidate_features, candidate_keypoints, card_timestamp):
    """
    Multi-stage filtering with progressive elimination:
    Stage 1: ReID score (relaxed threshold)
    Stage 2: OCR timestamp validation
    Stage 3: Pose similarity
    Stage 4: Final ranking + reference count cap
    """
    
    # STAGE 1: ReID Initial Filter (RELAXED)
    # Lower threshold to capture more candidates
    reid_score = existing_ensemble_score()  # 0.0-1.0
    
    if reid_score < 0.50:  # Relaxed from 0.65
        return None  # Too weak, reject immediately
    
    # STAGE 2: OCR Timestamp Filter
    # If timestamp available, validate against reference timestamps
    ocr_match = False
    if card_timestamp and ENABLE_OCR_TIMESTAMP_FILTER:
        ocr_match = card_timestamp in reference_timestamps[query]
        if not ocr_match:
            return None  # Wrong time, reject
    
    # STAGE 3: Pose Similarity
    # If keypoints available, compute pose score
    pose_score = 0.0
    if candidate_keypoints and pose_extractor:
        pose_score = pose_extractor.compute_similarity(
            candidate_keypoints, 
            reference_poses[query][ref_name]
        )
        
        if pose_score < POSE_SIMILARITY_THRESHOLD:  # e.g. 0.25
            return None  # Pose too different, reject
    
    # STAGE 4: Combined Score
    # Weight: 60% ReID + 20% Pose + 20% OCR bonus
    weights = {
        "reid": 0.60,
        "pose": 0.20,
        "ocr_bonus": 0.20 if ocr_match else 0.0
    }
    
    combined = (
        weights["reid"] * reid_score +
        weights["pose"] * pose_score +
        weights["ocr_bonus"]
    )
    
    return {
        "query": query,
        "ref_name": ref_name,
        "combined_score": combined,
        "reid_score": reid_score,
        "pose_score": pose_score,
        "ocr_match": ocr_match,
        "card_timestamp": card_timestamp,
    }

def _apply_reference_count_cap(all_candidates):
    """
    Enforce: max (reference_count - 1) result cards drawn.
    Reference image consumes one slot.
    
    Sort by combined_score DESC, take top N.
    """
    reference_count = len(reference_images[query])
    max_results = reference_count - 1
    
    # Sort by combined score
    sorted_candidates = sorted(
        all_candidates, 
        key=lambda x: x["combined_score"], 
        reverse=True
    )
    
    return sorted_candidates[:max_results]
```

**Key Changes:**
1. **Stage 1 (ReID):** Relaxed threshold 0.50 (from 0.65) → Cast wider net
2. **Stage 2 (OCR):** Hard filter if timestamp available → Eliminate wrong-time cards
3. **Stage 3 (Pose):** Soft filter with minimum threshold → Eliminate wrong-pose cards
4. **Stage 4 (Combined):** Weighted fusion → Rank remaining candidates
5. **Final Cap:** Enforce `max_results = reference_count - 1` → Preserve existing logic

#### 2.3 Config Parameters
```python
# config.py additions:
ENABLE_POSE_MATCHING = True
POSE_MODEL = "mediapipe"  # or "openpose", "yolov8"

# Multi-stage filtering thresholds
REID_INITIAL_THRESHOLD = 0.50  # Stage 1: Relaxed from 0.65 to cast wider net
POSE_SIMILARITY_THRESHOLD = 0.25  # Stage 3: Minimum pose match score
POSE_MIN_CONFIDENCE = 0.5  # Skip if keypoint conf < this

# Combined scoring weights (must sum to 1.0)
SCORE_WEIGHT_REID = 0.60  # ReID ensemble score weight
SCORE_WEIGHT_POSE = 0.20  # Pose similarity weight
SCORE_WEIGHT_OCR_BONUS = 0.20  # OCR exact match bonus (0 if no match)

# Reference count cap (existing logic preserved)
# max_results = len(reference_images[query]) - 1
# This ensures source reference card consumes one slot
```

---

### Phase 3: Implementation (2-3 tuần)

#### 3.1 Step 1: MediaPipe Pose Integration
- Create `pose_extractor.py`
- Extract keypoints from grid cards (during Fast Root pass)
- Cache keypoints alongside ReID features
- Add pose_score to ranking output

#### 3.2 Step 2: Similarity Metrics
```python
def pose_similarity_normalized_keypoints(kpt1, kpt2):
    # Align to body center, normalize scale
    # Compute L2 distance on pose descriptor
    # Handle missing keypoints gracefully
    pass

def pose_similarity_dtw(kpt1, kpt2):
    # Dynamic Time Warping for temporal pose sequences
    # (if we decide to use video frames instead of single images)
    pass
```

#### 3.3 Step 3: Reference Preprocessing
- On load: extract + cache pose keypoints for all references
- Store in `reference_poses` dict: `{query: {ref_name: keypoints}}`
- Re-extract if reference image changes

#### 3.4 Step 4: Candidate Evaluation (Fast Root) — Multi-Stage Pipeline
```python
# In _find_matches_fast_root():

# Step 1: Detect grid
boxes = _detect_result_grid(screenshot)

# Step 2: Extract features + keypoints for ALL candidates (parallel)
all_candidates = []
for bbox in boxes:
    x1, y1, x2, y2 = bbox
    crop = screenshot[y1:y2, x1:x2]
    
    # Extract ReID features
    features = ai_extractor.extract_feature(crop)
    
    # Extract pose keypoints (NEW)
    keypoints = None
    if ENABLE_POSE_MATCHING:
        keypoints = pose_extractor.extract_keypoints(crop)
    
    # Extract OCR timestamp
    card_ts = extract_reference_timestamp(crop) if ENABLE_OCR_TIMESTAMP_FILTER else None
    
    all_candidates.append({
        "bbox": bbox,
        "features": features,
        "keypoints": keypoints,
        "card_timestamp": card_ts,
    })

# Step 3: Multi-stage filtering
filtered_candidates = []
for candidate in all_candidates:
    # STAGE 1: ReID score (relaxed threshold 0.50)
    ranked = _rank_features(candidate["features"])
    if not ranked or ranked[0]["score"] < REID_INITIAL_THRESHOLD:
        continue  # Too weak, skip
    
    query = ranked[0]["query"]
    ref_name = ranked[0]["ref_name"]
    reid_score = ranked[0]["score"]
    
    # STAGE 2: OCR timestamp filter (hard gate)
    card_ts = candidate["card_timestamp"]
    ocr_match = False
    if card_ts and ENABLE_OCR_TIMESTAMP_FILTER:
        if card_ts not in reference_timestamps[query]:
            continue  # Wrong time, skip
        ocr_match = True
    
    # STAGE 3: Pose similarity (soft gate)
    pose_score = 0.0
    if candidate["keypoints"] and ENABLE_POSE_MATCHING:
        pose_score = pose_extractor.compute_similarity(
            candidate["keypoints"],
            reference_poses[query][ref_name]
        )
        if pose_score < POSE_SIMILARITY_THRESHOLD:
            continue  # Pose too different, skip
    
    # STAGE 4: Combined score
    combined = (
        SCORE_WEIGHT_REID * reid_score +
        SCORE_WEIGHT_POSE * pose_score +
        (SCORE_WEIGHT_OCR_BONUS if ocr_match else 0)
    )
    
    filtered_candidates.append({
        "query": query,
        "ref_name": ref_name,
        "bbox": candidate["bbox"],
        "combined_score": combined,
        "reid_score": reid_score,
        "pose_score": pose_score,
        "ocr_match": ocr_match,
        "card_timestamp": card_ts,
    })

# Step 4: Sort by combined score DESC
filtered_candidates.sort(key=lambda x: x["combined_score"], reverse=True)

# Step 5: Apply reference count cap
reference_count = len(reference_images[query])
max_results = reference_count - 1  # Preserve existing logic
final_matches = filtered_candidates[:max_results]

# Step 6: Log and return
log("MULTISTAGE", f"Grid {len(boxes)} → ReID {len([c for c in all_candidates if _rank_features(c['features'])])} → Filtered {len(filtered_candidates)} → Final {len(final_matches)}")

return final_matches
```

**Filtering Flow:**
```
Grid Detection (20 cards)
    ↓
ReID Threshold 0.50 (12 pass)
    ↓
OCR Timestamp Filter (8 pass)
    ↓
Pose Similarity ≥0.25 (5 pass)
    ↓
Combined Score Ranking (5 candidates)
    ↓
Reference Count Cap (max 3 if 4 references)
    ↓
Final Result (3 matches drawn)
```

---

### Phase 4: Testing & Tuning (2-3 tuần)

#### 4.1 Unit Tests
- `test_pose_extractor.py`: keypoint extraction correctness
- `test_pose_similarity.py`: similarity metric validation
- `test_pose_reid_fusion.py`: weighted scoring logic

#### 4.2 Regression Tests
- Ensure all 52 existing tests still pass
- No performance regression (pose extraction time)
- Cache effectiveness (avoid re-extracting same image)

#### 4.3 Accuracy Evaluation
```
Metrics:
1. Same-person pose similarity distribution (should be high)
2. Different-person pose similarity distribution (should be low)
3. Sensitivity: Does pose help rescue correct matches? (FN → TP)
4. Specificity: Does pose prevent false positives? (FP → TN)
5. Inference time: +10ms per card acceptable?
```

#### 4.4 Tuning Hyperparameters
- `REID_INITIAL_THRESHOLD`: Start 0.50 (relaxed), raise to 0.55/0.60 if too many false positives
- `POSE_SIMILARITY_THRESHOLD`: Start 0.25, lower to 0.20 if too many rejections, raise to 0.30 if too many false positives
- `POSE_MIN_CONFIDENCE`: Start 0.5, lower to 0.3 if pose detection fails often
- `SCORE_WEIGHT_REID`: Start 0.60, try 0.70/0.50 to balance precision vs recall
- `SCORE_WEIGHT_POSE`: Start 0.20, adjust inversely with REID weight
- `SCORE_WEIGHT_OCR_BONUS`: Start 0.20, keep fixed (OCR is reliable gate)

**Tuning Strategy:**
1. Measure baseline: FP rate, FN rate, precision, recall
2. Adjust one parameter at a time
3. Re-run on validation set (10-15 Query folders)
4. Track metrics after each change
5. Find optimal balance: high recall (catch true matches) + low FP (reject impostors)

---

### Phase 5: Advanced (Optional, 1-2 tháng)

#### 5.1 Fine-tuning on Domain Data
- If pose accuracy is poor on VMS angles/quality:
  - Collect 100-200 annotated frames from your VMS
  - Fine-tune YOLOv8-Pose on this data (transfer learning)
  - Achieves domain-specific accuracy boost (+5-10%)

#### 5.2 Temporal Pose Consistency
- If processing video sequences:
  - Extract pose trajectory (t-1, t, t+1)
  - Compare motion patterns, not just static pose
  - Reject if movement is unnatural for same person

#### 5.3 Multi-Modal Fusion
- Combine: ReID + Pose + Gait (if walking visible)
- Ensemble voting: if 2/3 agree, accept candidate

---

## Training Strategy (if needed)

### When to Train?
- MediaPipe default: works fine for frontal/semi-profile views
- **Need training if:**
  - Heavy occlusion (arms/backpack obscuring body)
  - Non-standard angles (extreme side view, top-down)
  - Low image quality (VMS compression artifacts)

### How to Train YOLOv8-Pose on Custom Data

#### Step 1: Data Collection & Annotation
```
Collect 200-500 frames from your VMS showing:
- Multiple poses (standing, walking, sitting, bending)
- Various angles (frontal, side, back)
- Different occlusion levels
- Day/night lighting

Tools for annotation:
- CVAT (open-source annotation tool)
- Roboflow (automated labeling + YOLOv8 integration)
- LabelImg (simpler, manual)

Output format: YOLO format (txt files with keypoint coordinates)
```

#### Step 2: Dataset Preparation
```
Directory structure:
dataset/
├── images/
│   ├── train/ (70% of frames)
│   ├── val/   (15%)
│   └── test/  (15%)
└── labels/
    ├── train/ (corresponding .txt files)
    ├── val/
    └── test/

Each .txt line: <x1 y1 conf1 x2 y2 conf2 ... x17 y17 conf17 class>
```

#### Step 3: Training
```python
from ultralytics import YOLO

model = YOLO("yolov8m-pose.pt")  # Medium model, good balance

results = model.train(
    data="dataset.yaml",  # Path to dataset config
    epochs=100,
    imgsz=640,
    device=0,  # GPU device
    patience=20,  # Early stopping
    batch=16,
    workers=4,
)

# Validate
metrics = model.val()

# Export
model.export(format="onnx")  # For inference
```

#### Step 4: Evaluation Metrics
```
Track during training:
- mAP@0.5 (object detection accuracy)
- keypoint OKS (Object Keypoint Similarity) — same as COCO metric
- Training time per epoch
```

#### Step 5: Integration
```python
# After training, use fine-tuned model:
from ultralytics import YOLO

model = YOLO("runs/detect/train/weights/best.pt")
results = model(image)
keypoints = results[0].keypoints.xy  # Extract keypoints
```

---

## Resource Estimates

| Phase | Duration | Effort | Output |
|-------|----------|--------|--------|
| 1: Research | 2-3 wks | Medium | PoC validation, benchmark report |
| 2: Design | 1 wk | Low | Architecture doc, config schema |
| 3: Implementation | 2-3 wks | High | pose_extractor.py, integration |
| 4: Testing | 2-3 wks | Medium | Test suite, tuned hyperparams |
| 5a: Fine-tuning (optional) | 1-2 mo | High | Custom YOLOv8-Pose model |
| **Total** | **8-10 wks** | **High** | **Production-ready pose matching** |

---

## Success Criteria

1. ✅ MediaPipe PoC shows pose similarity correlates with identity (R² > 0.6)
2. ✅ All 52 existing tests pass + 15 new pose tests
3. ✅ Multi-stage filtering reduces false negatives by 15-20% (more true matches rescued)
4. ✅ Reference count cap preserved: max (reference_count - 1) results drawn
5. ✅ Inference time +10-15ms per card (pose extraction overhead acceptable)
6. ✅ No increase in false positives (precision maintained or improved)
7. ✅ Relaxed ReID threshold (0.50) + multi-stage gates = better recall without sacrificing precision

**Key Validation:**
- Test on Query 8: Previously missed 11:51 AM should now be detected (if pose difference was not root cause)
- Test on all 15 Query folders: Measure FN reduction, FP rate unchanged
- Stress test: 50+ cards in grid, ensure performance stays acceptable

---

## Next Step

**Start with Phase 1, Step 1:** Install MediaPipe, test on Query_8 references. If pose_similarity is uncorrelated with identity, consider this not viable. If correlated, proceed to full plan.

```python
# Quick test (30 min):
import mediapipe as mp
from mediapipe.python.solutions import pose

mp_pose = pose.Pose()
# ... extract keypoints from Query_8 ref + screenshot ...
# Compare: same person should have high pose_sim
```

---

## Summary: Multi-Stage Filtering Logic

**Current Problem:** 
- High threshold (0.65) misses valid matches with slight angle/lighting changes
- Single-gate rejection (best_reference_score < 0.62) is too strict

**Solution:**
1. **Lower initial ReID threshold to 0.50** → Cast wider net, capture more candidates
2. **Add OCR timestamp gate** → Hard filter, eliminate wrong-time cards immediately
3. **Add pose similarity gate** → Soft filter, eliminate wrong-pose cards
4. **Combine scores with weights** → 60% ReID + 20% Pose + 20% OCR bonus
5. **Sort by combined score** → Rank remaining candidates
6. **Apply reference count cap** → Take top (reference_count - 1) results

**Benefits:**
- ✅ More recall: Relaxed initial threshold catches borderline valid matches
- ✅ Maintained precision: Multi-stage gates eliminate false positives progressively
- ✅ Preserved logic: Reference count cap unchanged
- ✅ Explainable: Each stage has clear rejection reason (ReID too weak / OCR mismatch / Pose different)

**Example Flow:**
```
20 cards detected
  ↓ Stage 1: ReID ≥0.50
12 candidates remain
  ↓ Stage 2: OCR timestamp match
8 candidates remain
  ↓ Stage 3: Pose similarity ≥0.25
5 candidates remain
  ↓ Stage 4: Sort by combined score
5 ranked candidates
  ↓ Stage 5: Cap at (ref_count - 1) = 3
3 final matches drawn
```
