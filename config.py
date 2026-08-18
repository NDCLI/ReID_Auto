"""
Configuration for ReID Auto Draw Tool (OSNet ensemble variant).

This is the TransReID-free variant. It runs three OSNet-family models that all
share the same raw-BGR 256x128 input contract, so no per-model preprocessing is
needed. It is installed alongside the original tool and keeps its own queries,
output, model cache, shortcut, mutex and hotkeys.
"""
import os
import sys

# ============================================================
# VARIANT IDENTITY
# ============================================================
# Everything user-visible or OS-global is namespaced so this build can be
# installed and run next to the original ReID Auto Draw without collisions.
APP_NAME = "AutoMarker Re-ID"
APP_MUTEX_NAME = "Global\\ReID_Auto_Draw_OSNet_Mutex_Unique_5F2B71"
MODEL_CACHE_DIRNAME = "ReIDAutoOSNet"

# ============================================================
# PATHS
# ============================================================
# Resources are inside PyInstaller's runtime folder in a portable build, while
# user-created Queries and results must sit beside the executable so they stay
# writable and travel with the portable folder.
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", SOURCE_DIR)
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else SOURCE_DIR
BASE_DIR = APP_DIR  # Backwards-compatible name used by integrations.
QUERIES_DIR = os.path.join(APP_DIR, "queries")
OUTPUT_DIR = os.path.join(APP_DIR, "output")

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
FACE_MIN_REFERENCES = 1
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

# A body match that narrowly misses the normal identity threshold may be
# recovered only when OCR independently confirms an exact Query timestamp and
# every body model agrees on the same identity. This handles frame-to-frame
# score jitter without lowering the global open-set threshold for strangers.
AI_TIME_RESCUE_SCORE_FLOOR = 0.62
AI_TIME_RESCUE_MARGIN = 0.15

# Automatic mode: pixel matching is deliberately permissive, then the local
# AI ensemble and a per-query calibrated threshold reject uncertain crops.
AUTO_CALIBRATION = True
AUTO_PIXEL_THRESHOLD = 0.86
AUTO_AI_THRESHOLD_FLOOR = 0.65
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

# ============================================================
# CLIPBOARD MONITORING
# ============================================================
# How often to check the lightweight Windows clipboard sequence (seconds).
# The image itself is read only after the sequence changes, so 10 Hz stays
# inexpensive while reducing average capture-detection latency to about 50 ms.
POLL_INTERVAL = 0.1

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
ENABLE_OCR_TIMESTAMP_FILTER = True

# Tolerance in minutes for timestamp matching.
# The Re-ID UI prints only hours and minutes on each card, so a genuine repeat
# of the same reference reads back as the exact same HH:MM. 0 means the card
# time must equal a reference time exactly; any drift is a different sighting.
OCR_TIMESTAMP_TOLERANCE = 0

# OCR method for full screenshots: 'winocr' (recommended) or 'rapidocr'
# Windows OCR (winocr) reads white-on-dark text in Re-ID UI much better.
# Small reference images always use RapidOCR first, then Windows OCR.
# When winocr is available it is always tried first regardless of this setting.
OCR_METHOD = 'winocr'

# ============================================================
# APPEARANCE (CLOTHING TEXTURE) MATCHING — tín hiệu thứ tư
# ============================================================
# Thay cho thử nghiệm pose (đã bỏ): trên các crop thẻ kết quả bé (~82x189 px)
# ai cũng đứng giống nhau nên khung xương không phân biệt được danh tính. Thay
# vào đó mô tả KẾT CẤU quần áo bằng Local Binary Pattern (LBP) histogram —
# đặc trưng phân biệt rẻ nhất còn lại, thuần OpenCV/NumPy, không thêm dependency.
#
# CỜ NÀY MẶC ĐỊNH TẮT: bước hiện tại chỉ xây và kiểm chứng module
# appearance_extractor.py độc lập (unit test + PoC). Phần tích hợp vào
# auto_marker.py (cache .appearance.npz, dùng làm rescue/tie-break như face
# branch — KHÔNG phải hard gate) làm ở bước sau. Khi tắt, hành vi runtime
# không đổi so với trước.
ENABLE_APPEARANCE_MATCHING = False

# Backend mô tả appearance. Hiện hỗ trợ "lbp" (LBP texture histogram).
APPEARANCE_MODEL = "lbp"

# Sàn similarity dùng ở bước tích hợp: ứng viên dưới ngưỡng này không được
# appearance "cứu". Chỉ có tác dụng khi nối dây multi-stage ở bước sau; hiện
# chưa dùng trong flow. Đây là tie-break/rescue, KHÔNG phải hard identity gate.
#
# 0.75 lấy TỪ phân bố đo được trên queries/Query_1..3 (poc_appearance_matching.py):
# giữ 94% cặp cùng người nhưng loại 46% cặp khác người. Đừng hạ xuống ~0.60 —
# ở đó mọi cặp đều lọt (100% same / 100% diff), gate thành vô nghĩa đúng như
# POSE_SIMILARITY_THRESHOLD=0.25 cũ.
APPEARANCE_SIMILARITY_FLOOR = 0.75

# Bỏ qua crop nhỏ hơn kích thước này (dựng histogram từ vài pixel là rác).
APPEARANCE_MIN_CROP_HEIGHT = 20
APPEARANCE_MIN_CROP_WIDTH = 8

# Chuẩn hóa scale (rộng, cao) trước khi tính LBP — LBP KHÔNG bất biến scale,
# cùng một người ở mức zoom UI khác sẽ ra phân bố code khác. Cùng tinh thần
# với ai_model resize mọi crop về input cố định.
APPEARANCE_NORMALIZED_SIZE = (64, 128)

# Bỏ qua crop quá phẳng (std độ xám dưới ngưỡng này): vùng phẳng bật hết 8 bit
# LBP nên hai crop không texture sẽ "giống nhau 1.0" dù thực tế khác hẳn.
# Crop thẻ thật đo được std 38–54, nên 6.0 chỉ chặn ảnh gần như trống.
APPEARANCE_MIN_TEXTURE_STD = 6.0

# ============================================================
# LOGGING
# ============================================================
# Khi False (mặc định): chỉ hiển thị log cần thiết trên giao diện
# (kết quả quét, số khung vẽ được/lý do thiếu, trạng thái OCR, cảnh báo/lỗi).
# Khi True: hiện thêm log chẩn đoán chi tiết (PERF, CALIBRATE, từng ảnh mẫu,
# từng match, nhận diện thẻ nguồn...) để gỡ lỗi.
VERBOSE_LOGGING = False
