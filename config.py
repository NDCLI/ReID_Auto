"""
Configuration for ReID Auto Draw Tool (OSNet ensemble variant).

This is the TransReID-free variant. It runs three OSNet-family models that all
share the same raw-BGR 256x128 input contract, so no per-model preprocessing is
needed. It is installed alongside the original tool and keeps its own queries,
output, model cache, shortcut, mutex and hotkeys.
"""
import os

# ============================================================
# VARIANT IDENTITY
# ============================================================
# Everything user-visible or OS-global is namespaced so this build can be
# installed and run next to the original ReID Auto Draw without collisions.
APP_NAME = "ReID Auto Draw OSNet"
APP_MUTEX_NAME = "Global\\ReID_Auto_Draw_OSNet_Mutex_Unique_5F2B71"
MODEL_CACHE_DIRNAME = "ReIDAutoOSNet"

# ============================================================
# PATHS
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUERIES_DIR = os.path.join(BASE_DIR, "queries")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# ============================================================
# MATCHING PARAMETERS
# ============================================================
# Matching confidence threshold (0.0 - 1.0)
# Lower = more matches but more false positives
# Tỷ lệ giống nhau để vẽ khung (0.0 đến 1.0)
# Nới lỏng một chút (0.85) để bắt được các ảnh có cùng khung hình nhưng hơi xê dịch pixel, 
# sau đó AI sẽ làm bước cuối để loại trừ ảnh khác người.
MATCH_THRESHOLD = 0.87

# Local embedding models. Optional models are loaded automatically when their
# files are added; missing optional models do not stop the bundled OSNet.
#
# All three take raw BGR uint8-range pixels at 256x128 and emit a 256-D
# embedding, which is exactly what _OpenVINOEmbeddingModel.extract() feeds them.
# Do not add a model here unless its preprocessing is baked into the graph.
REID_MODELS = [
    {
        "name": "osnet_0288",
        "model": "reid.xml",
        "weights": "reid.bin",
        "weight": 0.25,
        "device": "AUTO",
    },
    {
        "name": "osnet_lct_0277",
        "model": "reid_0277.xml",
        "weights": "reid_0277.bin",
        "weight": 0.75,
        "device": "AUTO",
    },
    {
        "name": "osnet_lct_0286",
        "model": "reid_0286.xml",
        "weights": "reid_0286.bin",
        "weight": 1.00,
        "device": "AUTO",
    },
]

# Optional face branch. It rescues the same identity after a clothing change,
# but only when the detected face is both confident and clearly separated from
# every other Query.
FACE_DETECTION_MODEL = os.path.join("models", "face-detection-retail-0005.xml")
FACE_RECOGNITION_MODEL = os.path.join("models", "face-reidentification-retail-0095.xml")
FACE_DETECTION_THRESHOLD = 0.75
FACE_MATCH_THRESHOLD = 0.65
FACE_MATCH_MARGIN = 0.20
FACE_MIN_REFERENCES = 2
FACE_FEATURE_NAME = "face_0095"

# Open-set decision policy. A result must be sufficiently similar and clearly
# better than the runner-up identity. With 2+ models they must also agree.
AI_MATCH_THRESHOLD = 0.68
AI_MATCH_MARGIN = 0.06
# At least one stored reference must strongly support a body-ReID decision.
# This rejects candidates that only share a common shirt color or pose.
#
# Calibrated for THIS ensemble, not the TransReID one. The three OSNet models
# score genuine cross-screenshot pairs lower than TransReID did, so the old
# 0.90 would reject nearly every true match. Measured leave-one-out over the
# reference set: 0.62 gives full recall with no stranger accepted, and recall
# starts dropping above 0.66. Re-measure if the model mix changes.
AI_BEST_REFERENCE_THRESHOLD = 0.62
AI_TOP_K_REFERENCES = 2
AI_REQUIRE_MODEL_AGREEMENT = True

# Automatic mode: pixel matching is deliberately permissive, then the local
# AI ensemble and a per-query calibrated threshold reject uncertain crops.
AUTO_CALIBRATION = True
AUTO_PIXEL_THRESHOLD = 0.86
AUTO_AI_THRESHOLD_FLOOR = 0.55
AUTO_AI_THRESHOLD_CEILING = 0.90
AUTO_AI_THRESHOLD_TOLERANCE = 0.05
MAX_PIXEL_CANDIDATES = 150

# When all Query folders are selected, detect the regular result grid once and
# use the lightweight OSNet model to shortlist cards. The remaining models are
# then run only for plausible cards. If no valid grid is found the matcher falls
# back to the original multi-scale template scan.
FAST_ROOT_MODE = True
FAST_ROOT_PRIMARY_MODEL = "osnet_lct_0277"
FAST_ROOT_SHORTLIST_THRESHOLD = 0.45
FAST_ROOT_MAX_ROWS = 2

# Domain invariant: one image in each query folder is the original/query image,
# therefore at most (reference count - 1) result boxes may be drawn.
LIMIT_MATCHES_BY_REFERENCE_COUNT = True

# Domain rule: each screenshot contains exactly one target person. Only the
# query that draws the most boxes is kept (tie-break: highest total score);
# all other queries' boxes are dropped.
ENFORCE_SINGLE_QUERY = True

# Scales to try for multi-scale template matching
# Covers cases where reference images are at different sizes
# than the thumbnails in the screenshot
MATCH_SCALES = [
    0.9, 0.95, 1.0, 1.05, 1.1
]

# In all-folders mode, limit template matching to at most this many reference
# images per Query folder. Reducing refs cuts template scan time proportionally.
# AI classification still compares candidates against ALL references for accuracy.
# Set to 0 to disable the limit (use all refs — most accurate, slowest).
# With OCR timestamp pre-filtering active, fewer folders reach template scan,
# so a higher value here is affordable.
TEMPLATE_REFS_PER_QUERY = 0

# ============================================================
# DRAWING PARAMETERS
# ============================================================
# Box line thickness in pixels
BOX_THICKNESS = 2

# Review windows add a box by a single click on the card, not by dragging. A
# card resolved from a click must be at least this many pixels wide and tall,
# which rejects clicks that land in the gap between cards.
CLICK_BOX_MIN_SIZE = 20

# Color palette for different queries (BGR format for OpenCV)
QUERY_COLORS = [
    (0, 0, 255),      # Red
    (0, 255, 0),      # Green
    (255, 0, 0),      # Blue
    (0, 255, 255),    # Yellow
    (255, 0, 255),    # Magenta
    (255, 255, 0),    # Cyan
    (0, 165, 255),    # Orange
    (128, 0, 128),    # Purple
    (0, 128, 255),    # Dark Orange
    (203, 192, 255),  # Pink
    (0, 215, 255),    # Gold
    (180, 105, 255),  # Hot Pink
    (144, 238, 144),  # Light Green
    (42, 42, 165),    # Brown
]


def get_query_color(index):
    """Return a BGR color for the given 0-based query index.

    Falls back to generating distinct colors via HSV spacing when the
    index exceeds the predefined palette.
    """
    if index < len(QUERY_COLORS):
        return QUERY_COLORS[index]
    # Generate a visually distinct color using golden-angle HSV spacing
    import colorsys
    golden = 0.618033988749895
    hue = ((index - len(QUERY_COLORS)) * golden) % 1.0
    saturation = 0.7 + (index % 3) * 0.1   # vary saturation slightly
    value = 0.85 + (index % 2) * 0.10       # vary brightness slightly
    r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
    return (int(b * 255), int(g * 255), int(r * 255))  # BGR

# ============================================================
# CLIPBOARD MONITORING
# ============================================================
# How often to check clipboard for new images (seconds)
POLL_INTERVAL = 0.5

# ============================================================
# QUERY IMAGE NAMING CONVENTION
# ============================================================
# Files matching these patterns are treated as query images
# and EXCLUDED from matching
QUERY_IMAGE_PREFIXES = ()

# Ignore matches in the left panel (e.g. SIMILAR TO image)
IGNORE_LEFT_RATIO = 0.25

# Ignore matches in the bottom X portion of the screen (e.g. 0.3 for bottom 30%)
IGNORE_BOTTOM_RATIO = 0.35

# ============================================================
# OCR TIMESTAMP MATCHING
# ============================================================
# Enable OCR-based timestamp verification
# When enabled, matches are filtered by comparing timestamps extracted from
# reference images and clipboard screenshots
ENABLE_OCR_TIMESTAMP_FILTER = True  # Tesseract installed at C:\Program Files\Tesseract-OCR

# Tolerance in minutes for timestamp matching
# Two timestamps are considered matching if they differ by at most this value
# Reference images within this time window of the screenshot are kept
OCR_TIMESTAMP_TOLERANCE = 5

# OCR method for full screenshots: 'winocr' (recommended) or 'tesseract'
# Windows OCR (winocr) reads white-on-dark text in Re-ID UI much better.
# Small reference images always use Windows OCR (winocr) for accuracy.
# When winocr is available it is always tried first regardless of this setting.
OCR_METHOD = 'winocr'
