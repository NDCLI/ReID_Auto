"""
OCR utilities for extracting time/date info from Re-ID screenshots.

Reads the TIME filter and per-card timestamps from the Re-ID UI to verify
that clipboard images match the reference screenshot's time period.

Strategy:
  - Full screenshots: scan the TOP 20% using Windows OCR (winocr) first,
    falling back to RapidOCR. This reads the TIME filter/date range.
  - Small reference images (~78x187 person crops): use RapidOCR with the
    OpenVINO CPU backend on enlarged bottom crops, then Windows OCR as
    fallback. Multiple RapidOCR variants vote on the timestamp.
  - Compare extracted values between reference and clipboard screenshots.
"""

import asyncio
import os
import re
import threading
import cv2
import numpy as np
from typing import Optional, Tuple

# Check for Windows OCR availability
_WINOCR_AVAILABLE = False
try:
    from winocr import recognize_cv2 as _winocr_recognize
    _WINOCR_AVAILABLE = True
except ImportError:
    pass

# RapidOCR is the preferred backend for tiny card timestamps. Import it lazily
# because the optional package loads ONNX models and is not needed at startup
# when OCR filtering is disabled.
_RAPIDOCR_AVAILABLE = None
_RAPIDOCR_ENGINE = None
_RAPIDOCR_LOCK = threading.Lock()

# Month name mapping
_MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'june': 6, 'july': 7, 'august': 8, 'september': 9,
    'october': 10, 'november': 11, 'december': 12,
}


# ============================================================
# Windows OCR for small reference images
# ============================================================

def _run_async(coro):
    """Run an async coroutine from synchronous code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already inside an event loop — create a new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def _winocr_bottom(image_bgr: np.ndarray, bottom_pct: float = 0.25,
                   scale: int = 8) -> str:
    """Run Windows OCR (WinRT) on the bottom portion of a small image.

    This is the same OCR engine that Windows 11 Photos uses. It reads
    overlay text on small person crops (~78x187px) reliably.

    Args:
        image_bgr: BGR image (small person crop).
        bottom_pct: Fraction of the image height to crop from the bottom.
        scale: Upscaling factor (INTER_LANCZOS4).

    Returns:
        OCR text from the bottom region, or "".
    """
    if not _WINOCR_AVAILABLE:
        return ""

    h, w = image_bgr.shape[:2]
    bottom = image_bgr[int(h * (1 - bottom_pct)):, :]
    bh, bw = bottom.shape[:2]
    if bh < 2 or bw < 2:
        return ""

    scaled = cv2.resize(bottom, (bw * scale, bh * scale),
                        interpolation=cv2.INTER_LANCZOS4)

    async def _ocr():
        result = await _winocr_recognize(scaled, 'en')
        return result.text.strip() if result.text else ""

    try:
        return _run_async(_ocr())
    except Exception:
        return ""


def _get_rapidocr_engine():
    """Load and cache the optional RapidOCR ONNX engine on first use."""
    global _RAPIDOCR_AVAILABLE, _RAPIDOCR_ENGINE
    if _RAPIDOCR_AVAILABLE is False:
        return None
    if _RAPIDOCR_ENGINE is not None:
        return _RAPIDOCR_ENGINE

    try:
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import EngineType
        # Reuse the project's OpenVINO runtime. RapidOCR defaults to
        # onnxruntime, whose Windows DLL can fail to initialize on some Python
        # environments even though OpenVINO is already working for ReID.
        openvino_params = {
            "Global.log_level": "error",
            "Det.engine_type": EngineType.OPENVINO,
            "Cls.engine_type": EngineType.OPENVINO,
            "Rec.engine_type": EngineType.OPENVINO,
        }
        _RAPIDOCR_ENGINE = RapidOCR(params=openvino_params)
        _RAPIDOCR_AVAILABLE = True
    except (ImportError, OSError, RuntimeError, ValueError):
        _RAPIDOCR_AVAILABLE = False
        _RAPIDOCR_ENGINE = None
    return _RAPIDOCR_ENGINE


def warm_up_card_ocr() -> bool:
    global _RAPIDOCR_AVAILABLE, _RAPIDOCR_ENGINE
    """Load RapidOCR and warm its recognition-only path.

    Card crops already isolate a single timestamp line, so the detector and
    orientation classifier are unnecessary. Warming this path while the app is
    initializing prevents the first Review from paying the OpenVINO startup
    cost.
    """
    engine = _get_rapidocr_engine()
    if engine is None:
        return False

    sample = np.zeros((48, 256, 3), dtype=np.uint8)
    with _RAPIDOCR_LOCK:
        try:
            engine(sample, use_det=False, use_cls=False, use_rec=True)
        except Exception as exc:
            # Some RapidOCR/OpenVINO combinations reject ndarray input and
            # attempt to interpret it as a filename (for example image.png).
            # Disable this optional backend instead of allowing the error to
            # affect clipboard, GUI, or mouse-hook processing.
            _RAPIDOCR_AVAILABLE = False
            _RAPIDOCR_ENGINE = None
            print(f"[OCR] RapidOCR disabled after warm-up failure: {exc}")
            return False
    return True


def _rapidocr_result_items(result):
    """Return ``(text, confidence)`` pairs from RapidOCR result variants."""
    if isinstance(result, tuple):
        result = result[0]
    if result is None:
        return []

    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if texts is None and isinstance(result, dict):
        texts = result.get("txts") or result.get("texts")
        scores = result.get("scores")
    if texts is not None:
        if scores is None:
            scores = []
        return [
            (str(text), float(scores[index]) if index < len(scores) else 0.0)
            for index, text in enumerate(texts)
            if text
        ]

    # Older RapidOCR releases return rows like [box, text, score].
    if isinstance(result, (list, tuple)):
        items = []
        for row in result:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            text = row[1]
            score = row[2] if len(row) > 2 else 0.0
            if isinstance(text, str) and text:
                try:
                    score = float(score)
                except (TypeError, ValueError):
                    score = 0.0
                items.append((text, score))
        return items
    return []


def _rapidocr_variants(image_bgr: np.ndarray):
    """Yield enlarged bottom-card crops for RapidOCR."""
    h, w = image_bgr.shape[:2]
    # Start with tight bands around the timestamp baseline. Wider 25-30%
    # crops include too much of the person/background and can collapse
    # repeated digits (for example ``1:55 PM`` -> ``1:5PM``). The 18% and 28%
    # views independently retain that timestamp on the real Re-ID cards.
    for bottom_pct in (0.18, 0.28, 0.20, 0.22, 0.25, 0.30):
        bottom = image_bgr[int(h * (1 - bottom_pct)):, :]
        if bottom.shape[0] < 2 or bottom.shape[1] < 2:
            continue
        scale = 8
        enlarged = cv2.resize(
            bottom,
            (bottom.shape[1] * scale, bottom.shape[0] * scale),
            interpolation=cv2.INTER_CUBIC,
        )
        # The color crop preserves the anti-aliased white glyph edges. Three
        # nearby bottom bands provide consensus without multiplying OCR cost
        # with grayscale/binary variants that can erase tiny characters.
        yield enlarged


def _rapidocr_timestamp(image_bgr: np.ndarray) -> Optional[str]:
    global _RAPIDOCR_AVAILABLE, _RAPIDOCR_ENGINE
    """Extract a timestamp with RapidOCR using consensus across crop variants.

    Uses a lock for thread safety and stops as soon as two crop variants agree.
    Recognition-only inference is cheap enough to preserve this consensus gate
    instead of trusting one high-confidence but potentially truncated reading.
    """
    engine = _get_rapidocr_engine()
    if engine is None:
        return None

    votes = {}
    for variant in _rapidocr_variants(image_bgr):
        with _RAPIDOCR_LOCK:
            try:
                # The crop contains one centered timestamp line already.
                # Recognition-only avoids running text detection and
                # orientation classification for every accepted card.
                result = engine(
                    variant,
                    use_det=False,
                    use_cls=False,
                    use_rec=True,
                )
            except Exception as exc:
                _RAPIDOCR_AVAILABLE = False
                _RAPIDOCR_ENGINE = None
                print(f"[OCR] RapidOCR disabled after recognition failure: {exc}")
                continue
        for text, confidence in _rapidocr_result_items(result):
            timestamp = _parse_time_ampm(text)
            if timestamp:
                votes.setdefault(timestamp, []).append(confidence)
        # Two independent crops agreeing is enough; a single reading still
        # needs another variant because the leading "1" is easily truncated.
        if any(len(confidences) >= 2 for confidences in votes.values()):
            break

    if not votes:
        # Rare fallback: let RapidOCR detect the text line when direct
        # recognition could not parse any tight crop. This costs more, but it
        # runs only for an otherwise-unreadable card and prevents the filter
        # from silently keeping a different-time candidate.
        h, w = image_bgr.shape[:2]
        bottom = image_bgr[int(h * 0.75):, :]
        if bottom.shape[0] >= 2 and bottom.shape[1] >= 2:
            enlarged = cv2.resize(
                bottom,
                (bottom.shape[1] * 8, bottom.shape[0] * 8),
                interpolation=cv2.INTER_CUBIC,
            )
            with _RAPIDOCR_LOCK:
                try:
                    result = engine(
                        enlarged,
                        use_det=True,
                        use_cls=True,
                        use_rec=True,
                    )
                except Exception as exc:
                    _RAPIDOCR_AVAILABLE = False
                    _RAPIDOCR_ENGINE = None
                    print(f"[OCR] RapidOCR disabled after detection failure: {exc}")
                    result = None
            parsed = [
                (timestamp, confidence)
                for text, confidence in _rapidocr_result_items(result)
                if (timestamp := _parse_time_ampm(text))
            ]
            if parsed:
                return max(parsed, key=lambda item: item[1])[0]
        return None
    return max(
        votes,
        key=lambda timestamp: (
            len(votes[timestamp]),
            max(votes[timestamp]),
            sum(votes[timestamp]) / len(votes[timestamp]),
        ),
    )


def extract_reference_timestamp(image_bgr: np.ndarray) -> Optional[str]:
    """Extract a time like '7:42 AM' from a small reference person crop.

    RapidOCR with the OpenVINO CPU backend is preferred for tiny text and uses
    consensus across several enlarged crops. Windows OCR is the fallback so
    OCR stays optional and an unreadable crop never breaks matching.

    Args:
        image_bgr: BGR image (small person crop, ~78x187px).

    Returns:
        Normalized time string like '7:42 AM', or None.
    """
    # --- Strategy 1: RapidOCR (best for tiny overlay text) ---
    rapid_timestamp = _rapidocr_timestamp(image_bgr)
    if rapid_timestamp:
        return rapid_timestamp

    # --- Strategy 2: Windows OCR fallback ---
    if _WINOCR_AVAILABLE:
        for bpct in [0.25, 0.30, 0.20, 0.35]:
            for scale in [8, 10, 12, 6]:
                text = _winocr_bottom(image_bgr, bottom_pct=bpct, scale=scale)
                if text:
                    time_str = _parse_time_ampm(text)
                    if time_str:
                        return time_str

    return None


# ============================================================
# Windows OCR for full screenshots
# ============================================================

def _winocr_region(image_bgr: np.ndarray, y_start: float, y_end: float,
                   x_start: float, x_end: float, scale: int = 3) -> str:
    """Run Windows OCR on a rectangular region of a full screenshot.

    Args:
        image_bgr: BGR image (full screenshot).
        y_start, y_end: Vertical crop as fractions of height (0.0-1.0).
        x_start, x_end: Horizontal crop as fractions of width (0.0-1.0).
        scale: Upscaling factor for better OCR accuracy.

    Returns:
        OCR text from the region, or "".
    """
    if not _WINOCR_AVAILABLE:
        return ""

    h, w = image_bgr.shape[:2]
    y0 = max(int(h * y_start), 0)
    y1 = min(int(h * y_end), h)
    x0 = max(int(w * x_start), 0)
    x1 = min(int(w * x_end), w)
    roi = image_bgr[y0:y1, x0:x1]

    rh, rw = roi.shape[:2]
    if rh < 2 or rw < 2:
        return ""

    scaled = cv2.resize(roi, (rw * scale, rh * scale),
                        interpolation=cv2.INTER_LANCZOS4)

    async def _ocr():
        result = await _winocr_recognize(scaled, 'en')
        return result.text.strip() if result.text else ""

    try:
        return _run_async(_ocr())
    except Exception:
        return ""


def _winocr_screenshot(image_bgr: np.ndarray) -> str:
    """Run Windows OCR on the top portion of a full Re-ID screenshot.

    Scans two regions in parallel using asyncio.gather:
      1. Top-left panel (0-30% width, 0-20% height): TIME filter with
         date range like "Aug 3-4" and time range like "7:00 AM - 12:00 PM"
      2. Top-right area (30-100% width, 0-20% height): Result card
         headers that may show timestamps

    Returns:
        Combined OCR text from both regions.
    """
    if not _WINOCR_AVAILABLE:
        return ""

    h, w = image_bgr.shape[:2]

    # Prepare Region 1: top-left panel (4x scale)
    y1_end = min(int(h * 0.20), h)
    x1_end = min(int(w * 0.30), w)
    roi1 = image_bgr[0:y1_end, 0:x1_end]
    rh1, rw1 = roi1.shape[:2]
    scaled1 = (cv2.resize(roi1, (rw1 * 4, rh1 * 4),
               interpolation=cv2.INTER_LANCZOS4)
               if rh1 >= 2 and rw1 >= 2 else None)

    # Prepare Region 2: top-right header (3x scale)
    roi2 = image_bgr[0:y1_end, x1_end:w]
    rh2, rw2 = roi2.shape[:2]
    scaled2 = (cv2.resize(roi2, (rw2 * 3, rh2 * 3),
               interpolation=cv2.INTER_LANCZOS4)
               if rh2 >= 2 and rw2 >= 2 else None)

    if scaled1 is None and scaled2 is None:
        return ""

    async def _ocr_both():
        tasks = []
        if scaled1 is not None:
            tasks.append(_winocr_recognize(scaled1, 'en'))
        if scaled2 is not None:
            tasks.append(_winocr_recognize(scaled2, 'en'))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        texts = []
        for r in results:
            if isinstance(r, Exception):
                continue
            if r and r.text and r.text.strip():
                texts.append(r.text.strip())
        return "\n".join(texts)

    try:
        return _run_async(_ocr_both())
    except Exception:
        return ""


# ============================================================
# RapidOCR for full screenshots (fallback when WinOCR unavailable)
# ============================================================

def _rapidocr_screenshot(image_bgr: np.ndarray) -> str:
    global _RAPIDOCR_AVAILABLE, _RAPIDOCR_ENGINE
    """Run RapidOCR on the top 20% of a full Re-ID screenshot.

    The Re-ID UI shows the TIME filter in the top-left panel.
    Scale up and invert (dark UI) for better accuracy.

    Returns:
        Combined OCR text from the top-20% region, or "".
    """
    engine = _get_rapidocr_engine()
    if engine is None:
        return ""

    h, w = image_bgr.shape[:2]
    top20_h = max(int(h * 0.20), 1)
    left30_w = max(int(w * 0.30), 1)

    texts = []

    # Region 1: top-left panel (TIME filter + date range)
    roi_left = image_bgr[0:top20_h, 0:left30_w]
    gray = cv2.cvtColor(roi_left, cv2.COLOR_BGR2GRAY)
    scaled = cv2.resize(gray, (gray.shape[1] * 4, gray.shape[0] * 4),
                        interpolation=cv2.INTER_CUBIC)
    inverted = cv2.bitwise_not(scaled)
    with _RAPIDOCR_LOCK:
        try:
            # Full-screen regions can contain multiple text lines, so restore
            # the full OCR pipeline explicitly (RapidOCR options are stateful).
            result = engine(
                inverted,
                use_det=True,
                use_cls=True,
                use_rec=True,
            )
        except Exception as exc:
            _RAPIDOCR_AVAILABLE = False
            _RAPIDOCR_ENGINE = None
            print(f"[OCR] RapidOCR disabled after screenshot failure: {exc}")
            result = None
    if result is not None:
        for text, _conf in _rapidocr_result_items(result):
            if text.strip():
                texts.append(text.strip())

    # Region 2: top-right header area (result cards may show times)
    roi_right = image_bgr[0:top20_h, left30_w:w]
    gray2 = cv2.cvtColor(roi_right, cv2.COLOR_BGR2GRAY)
    scaled2 = cv2.resize(gray2, (gray2.shape[1] * 3, gray2.shape[0] * 3),
                         interpolation=cv2.INTER_CUBIC)
    inv2 = cv2.bitwise_not(scaled2)
    with _RAPIDOCR_LOCK:
        try:
            result2 = engine(
                inv2,
                use_det=True,
                use_cls=True,
                use_rec=True,
            )
        except Exception as exc:
            _RAPIDOCR_AVAILABLE = False
            _RAPIDOCR_ENGINE = None
            print(f"[OCR] RapidOCR disabled after screenshot failure: {exc}")
            result2 = None
    if result2 is not None:
        for text, _conf in _rapidocr_result_items(result2):
            if text.strip():
                texts.append(text.strip())

    return "\n".join(texts)


# ============================================================
# Parsing helpers
# ============================================================

def _parse_date_range(text: str) -> Optional[str]:
    """Extract a date range like 'Aug 3-4' or 'Jul 15' from OCR text.

    Returns:
        Normalized string like 'Aug 3-4' or 'Jul 15', or None.
    """
    # Pattern: Month Day-Day  (e.g. "Aug 3-4", "Jul 15-16", "Sep 1")
    m = re.search(
        r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*'
        r'\s+(\d{1,2})'
        r'(?:\s*-\s*(\d{1,2}))?',
        text, re.IGNORECASE
    )
    if m:
        month = m.group(1).capitalize()[:3]
        day1 = int(m.group(2))
        day2 = int(m.group(3)) if m.group(3) else None
        if 1 <= day1 <= 31:
            if day2 and 1 <= day2 <= 31:
                return f"{month} {day1}-{day2}"
            return f"{month} {day1}"
    return None


def _parse_time_ampm(text: str) -> Optional[str]:
    """Extract times like '7:00 AM', '12:30 PM' from OCR text.

    Also handles common OCR errors:
      - Missing colon: '742 AM' -> '7:42 AM'
      - Space in number: '1 :56AM' -> '1:56 AM'
      - Truncated AM/PM: '7:42A' -> '7:42 AM'

    Returns:
        Normalized time string like '7:00 AM', or None.
    """
    # Clean common OCR artifacts
    cleaned = text.replace('•', ':').replace('·', ':').replace('.', ':')

    # Pattern 1: Standard H:MM AM/PM
    matches = re.findall(
        r'(\d{1,2})\s*:\s*(\d{2})\s*(AM|PM|am|pm|A\.?M\.?|P\.?M\.?|A|P)',
        cleaned
    )
    if matches:
        for h_str, m_str, ampm in matches:
            h = int(h_str)
            m = int(m_str)
            ampm_clean = ampm.replace('.', '').upper()
            # Handle truncated AM/PM: 'A' -> 'AM', 'P' -> 'PM'
            if ampm_clean == 'A':
                ampm_clean = 'AM'
            elif ampm_clean == 'P':
                ampm_clean = 'PM'
            if 1 <= h <= 12 and 0 <= m <= 59 and ampm_clean in ('AM', 'PM'):
                return f"{h}:{m:02d} {ampm_clean}"

    # Pattern 2: Missing colon — '742 AM', '1156 AM'
    matches2 = re.findall(
        r'(\d{1,2})(\d{2})\s*(AM|PM|am|pm|A|P)',
        cleaned
    )
    if matches2:
        for h_str, m_str, ampm in matches2:
            h = int(h_str)
            m = int(m_str)
            ampm_clean = ampm.replace('.', '').upper()
            if ampm_clean == 'A':
                ampm_clean = 'AM'
            elif ampm_clean == 'P':
                ampm_clean = 'PM'
            if 1 <= h <= 12 and 0 <= m <= 59 and ampm_clean in ('AM', 'PM'):
                return f"{h}:{m:02d} {ampm_clean}"

    return None


def _parse_time_24h(text: str) -> Optional[str]:
    """Extract 24h times like '14:30' or '08:15:25' from OCR text.

    Returns:
        Time string like '14:30' or None.
    """
    # Avoid matching things like version numbers by requiring TIME context
    # or standalone HH:MM:SS / HH:MM patterns
    m = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', text)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        s = int(m.group(3)) if m.group(3) else 0
        if 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59:
            if m.group(3):
                return f"{h:02d}:{mi:02d}:{s:02d}"
            return f"{h:02d}:{mi:02d}"
    return None


# ============================================================
# Public API
# ============================================================

def extract_timestamp(image_bgr: np.ndarray, method: str = 'winocr') -> Optional[str]:
    """Extract date/time info from the top 20% of a Re-ID screenshot.

    Looks for:
      1. Date range from TIME filter: "Aug 3-4"
      2. AM/PM times: "7:00 AM", "12:00 PM"
      3. 24h times: "14:30:25"

    Uses the requested backend. When the requested backend is unavailable or
    returns no parseable text, the other backend is used as a fallback.

    Args:
        image_bgr: BGR image (full screenshot)
        method: OCR backend hint ('winocr' or 'rapidocr').
                The selected backend is attempted first.

    Returns:
        Extracted date/time string, or None if nothing found.
    """
    method = (method or "winocr").lower()
    if method not in {"winocr", "rapidocr"}:
        raise ValueError("method must be 'winocr' or 'rapidocr'")
    backends = ("winocr", "rapidocr") if method == "winocr" else ("rapidocr", "winocr")
    for backend in backends:
        if backend == "winocr" and not _WINOCR_AVAILABLE:
            continue
        text = _winocr_screenshot(image_bgr) if backend == "winocr" else _rapidocr_screenshot(image_bgr)
        if text:
            result = _parse_from_text(text)
            if result:
                return result

    return None


def _parse_from_text(text: str) -> Optional[str]:
    """Try to extract a date range or time from OCR text.

    Priority: date range > AM/PM time > 24h time.
    """
    # Priority 1: Date range (most reliable from the Re-ID UI)
    date_range = _parse_date_range(text)
    if date_range:
        return date_range

    # Priority 2: AM/PM time
    time_ampm = _parse_time_ampm(text)
    if time_ampm:
        return time_ampm

    # Priority 3: 24h time
    time_24h = _parse_time_24h(text)
    if time_24h:
        return time_24h

    return None


def _normalize_date_range(s: str) -> Tuple[int, int, int]:
    """Normalize 'Aug 3-4' -> (month, day_start, day_end)."""
    m = re.match(r'(\w+)\s+(\d+)(?:-(\d+))?', s)
    if not m:
        return (0, 0, 0)
    month_str = m.group(1).lower()[:3]
    month = _MONTH_MAP.get(month_str, 0)
    day1 = int(m.group(2))
    day2 = int(m.group(3)) if m.group(3) else day1
    import calendar
    if month == 0 or day1 < 1 or day2 < day1 or day2 > calendar.monthrange(2024, month)[1]:
        return (0, 0, 0)
    return (month, day1, day2)


def _normalize_time_to_minutes(s: str) -> Optional[int]:
    """Convert '7:00 AM' or '14:30' to minutes since midnight."""
    # Try AM/PM
    m = re.match(r'(\d{1,2}):(\d{2})\s*(AM|PM)', s, re.IGNORECASE)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        if not (1 <= h <= 12 and 0 <= mi <= 59):
            return None
        ampm = m.group(3).upper()
        if ampm == 'PM' and h != 12:
            h += 12
        elif ampm == 'AM' and h == 12:
            h = 0
        return h * 60 + mi

    # Try 24h
    m = re.match(r'(\d{1,2}):(\d{2})(?::(\d{2}))?$', s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None
        return h * 60 + mi

    return None


def timestamps_match(timestamp1: Optional[str], timestamp2: Optional[str],
                     tolerance_minutes: int = 0) -> bool:
    """Check if two extracted time/date values match.

    Handles three comparison modes:
      - Date range vs date range: "Aug 3-4" overlaps "Aug 3-4" -> True
      - Time vs time: exact HH:MM matches by default; callers may opt into a
        wider tolerance for broad capture windows.
      - Mixed types or missing: graceful fallback -> True

    Args:
        timestamp1: First extracted string (or None)
        timestamp2: Second extracted string (or None)
        tolerance_minutes: Max difference in minutes for time comparisons.
            Pass 0 to require the exact same HH:MM, which is what the Re-ID UI
            prints on each card. The default stays loose for callers comparing
            broad capture windows rather than individual cards.

    Returns:
        True if they match (or if comparison is impossible -> allow).
    """
    if not timestamp1 or not timestamp2:
        # Can't compare -> allow match (fallback to image similarity)
        return True

    # Determine types
    is_date1 = bool(re.match(r'[A-Za-z]', timestamp1))
    is_date2 = bool(re.match(r'[A-Za-z]', timestamp2))

    # Both are date ranges (e.g. "Aug 3-4" vs "Aug 3-4")
    if is_date1 and is_date2:
        m1, d1s, d1e = _normalize_date_range(timestamp1)
        m2, d2s, d2e = _normalize_date_range(timestamp2)

        if m1 == 0 or m2 == 0:
            return True  # parse failed, allow

        # Same month and overlapping day ranges
        if m1 != m2:
            return False
        # Check overlap: [d1s, d1e] overlaps [d2s, d2e]
        return d1s <= d2e and d2s <= d1e

    # Both are times (e.g. "7:00 AM" vs "7:05 AM")
    if not is_date1 and not is_date2:
        t1 = _normalize_time_to_minutes(timestamp1)
        t2 = _normalize_time_to_minutes(timestamp2)
        if t1 is None or t2 is None:
            return True  # parse failed, allow

        diff = abs(t1 - t2)
        # Handle midnight wrap
        if diff > 720:
            diff = 1440 - diff
        return diff <= tolerance_minutes

    # Mixed types (date vs time) -> can't compare meaningfully, allow
    return True
