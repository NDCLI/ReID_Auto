---
name: pose_matching_research_plan
description: Plan for adding pose/keypoint matching to improve ReID robustness
metadata:
  type: project
---

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

#### 2.2 Scoring Strategy
```python
def _rank_features_with_pose(candidate_features, candidate_keypoints):
    # Existing ReID rank: osnet ensemble
    reid_score = existing_ensemble_score()  # 0.0-1.0
    
    # Pose matching: skip if keypoints incomplete
    pose_score = compute_pose_similarity(candidate_keypoints, ref_keypoints)
    
    # Weighted fusion (tunable)
    combined = 0.75 * reid_score + 0.25 * pose_score
    
    return combined
```

#### 2.3 Config Parameters
```python
# config.py additions:
ENABLE_POSE_MATCHING = True
POSE_MODEL = "mediapipe"  # or "openpose", "yolov8"
POSE_REID_WEIGHT = 0.75  # vs 0.25 for pose
POSE_MIN_CONFIDENCE = 0.5  # Skip if keypoint conf < this
POSE_SIMILARITY_THRESHOLD = 0.3  # Reject if pose_score < this
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

#### 3.4 Step 4: Candidate Evaluation (Fast Root)
```python
# In _find_matches_fast_root():
for bbox, crop, primary_score in candidates:
    # Existing ReID
    features = ai_extractor.extract_feature(crop)
    ranked = _rank_features(features)
    
    # NEW: Extract pose
    keypoints = pose_extractor.extract_keypoints(crop)
    
    # NEW: Compute pose score vs winning reference
    if keypoints and ranked:
        pose_score = pose_extractor.compute_similarity(
            keypoints, 
            reference_poses[query][ref_name]
        )
        
        # Adjust ranking
        adjusted_score = 0.75 * ranked[0]["score"] + 0.25 * pose_score
        ranked[0]["pose_score"] = pose_score
        ranked[0]["adjusted_score"] = adjusted_score
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
- `POSE_REID_WEIGHT`: Start 0.75, try 0.80, 0.70
- `POSE_MIN_CONFIDENCE`: Start 0.5, lower to 0.3 if too many rejections
- `POSE_SIMILARITY_THRESHOLD`: Start 0.3, calibrate on false positives

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
3. ✅ Query 8 11:51 AM issue resolved (if pose difference was root cause)
4. ✅ Inference time +5-10ms per card (acceptable)
5. ✅ FN → TP recovery: 10-15% reduction in false negatives
6. ✅ FP → TN prevention: no increase in false positives

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
