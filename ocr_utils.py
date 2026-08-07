"""
OCR utilities for extracting time/date info from Re-ID screenshots.

Reads the TIME filter and per-card timestamps from the Re-ID UI to verify
that clipboard images match the reference screenshot's time period.

Strategy:
  - Full screenshots: scan the TOP 20% using Windows OCR (winocr) first,
    falling back to Tesseract.  winocr handles white-on-dark UI text that
    Tesseract cannot read.  Looks for date ranges like "Aug 3-4" from the
    TIME filter (left panel) and card timestamps.
  - Small reference images (~78x187 person crops): use Windows OCR (WinRT)
    on the BOTTOM 25%, scaled up 8x.  This is the same engine Windows 11
    Photos uses and it reads overlay text that Tesseract cannot handle.
  - Compare extracted values between reference and clipboard screenshots.
"""

import asyncio
import os
import re
import cv2
import numpy as np
from datetime import datetime
from typing import Optional, Tuple

# Auto-configure Tesseract path on Windows
_TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.exists(_TESSERACT_PATH):
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_PATH
    except ImportError:
        pass

# Check for Windows OCR availability
_WINOCR_AVAILABLE = False
try:
    from winocr import recognize_cv2 as _winocr_recognize
    _WINOCR_AVAILABLE = True
except ImportError:
    pass

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
    overlay text on small person crops (~78x187px) that Tesseract cannot.

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


def extract_reference_timestamp(image_bgr: np.ndarray) -> Optional[str]:
    """Extract a time like '7:42 AM' from a small reference person crop.

    Uses Windows OCR (WinRT) — the same engine as Windows 11 Photos.
    Falls back to Tesseract if WinRT is unavailable.

    Args:
        image_bgr: BGR image (small person crop, ~78x187px).

    Returns:
        Normalized time string like '7:42 AM', or None.
    """
    # --- Strategy 1: Windows OCR (best for small overlay text) ---
    # Try multiple crop/scale combos for maximum accuracy (~98%).
    if _WINOCR_AVAILABLE:
        for bpct in [0.25, 0.30, 0.20, 0.35]:
            for scale in [8, 10, 12, 6]:
                text = _winocr_bottom(image_bgr, bottom_pct=bpct, scale=scale)
                if text:
                    time_str = _parse_time_ampm(text)
                    if time_str:
                        return time_str

    # --- Strategy 2: Tesseract fallback (rarely works on small crops) ---
    try:
        import pytesseract
    except ImportError:
        return None

    h, w = image_bgr.shape[:2]
    bottom = image_bgr[int(h * 0.75):, :]
    bh, bw = bottom.shape[:2]
    if bh < 2 or bw < 2:
        return None

    for scale in [12, 16]:
        scaled = cv2.resize(bottom, (bw * scale, bh * scale),
                            interpolation=cv2.INTER_LANCZOS4)
        gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
        for processed in [gray, cv2.bitwise_not(gray)]:
            for psm in [6, 7]:
                try:
                    text = pytesseract.image_to_string(
                        processed, config=f'--psm {psm} --oem 3').strip()
                    if text:
                        ts = _parse_time_ampm(text)
                        if ts:
                            return ts
                except Exception:
                    pass
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

    Scans two regions where timestamps appear in the Re-ID UI:
      1. Top-left panel (0-30% width, 0-20% height): TIME filter with
         date range like "Aug 3-4" and time range like "7:00 AM - 12:00 PM"
      2. Top-right area (30-100% width, 0-20% height): Result card
         headers that may show timestamps

    Windows OCR handles white text on dark background much better than
    Tesseract, which is critical for the Re-ID UI's dark theme.

    Returns:
        Combined OCR text from both regions.
    """
    if not _WINOCR_AVAILABLE:
        return ""

    texts = []

    # Region 1: top-left panel (TIME filter + date range)
    # Use higher scale (4x) for this smaller region with critical data
    t1 = _winocr_region(image_bgr, 0.0, 0.20, 0.0, 0.30, scale=4)
    if t1:
        texts.append(t1)

    # Region 2: top-right header area (result cards may show times)
    t2 = _winocr_region(image_bgr, 0.0, 0.20, 0.30, 1.0, scale=3)
    if t2:
        texts.append(t2)

    return "\n".join(texts)


# ============================================================
# Tesseract OCR for full screenshots
# ============================================================

def _ocr_top20(image_bgr: np.ndarray) -> str:
    """Run OCR on the top 20% of the image (left 30% panel area).

    The Re-ID UI shows the TIME filter in the top-left panel.
    Scale up 4x and invert (dark UI) for better Tesseract accuracy.

    Returns:
        Combined OCR text from the top-20% region.
    """
    try:
        import pytesseract
    except ImportError:
        return ""

    h, w = image_bgr.shape[:2]
    top20_h = max(int(h * 0.20), 1)
    left30_w = max(int(w * 0.30), 1)

    # Region 1: top-left panel (TIME filter + date range)
    roi_left = image_bgr[0:top20_h, 0:left30_w]
    gray = cv2.cvtColor(roi_left, cv2.COLOR_BGR2GRAY)
    scaled = cv2.resize(gray, (gray.shape[1] * 4, gray.shape[0] * 4),
                        interpolation=cv2.INTER_CUBIC)
    inverted = cv2.bitwise_not(scaled)

    texts = []
    try:
        t = pytesseract.image_to_string(inverted, config='--psm 6 --oem 3')
        if t.strip():
            texts.append(t.strip())
    except Exception:
        pass

    # Region 2: top-right header area (result cards may show times)
    roi_right = image_bgr[0:top20_h, left30_w:w]
    gray2 = cv2.cvtColor(roi_right, cv2.COLOR_BGR2GRAY)
    scaled2 = cv2.resize(gray2, (gray2.shape[1] * 3, gray2.shape[0] * 3),
                         interpolation=cv2.INTER_CUBIC)
    inv2 = cv2.bitwise_not(scaled2)
    try:
        t2 = pytesseract.image_to_string(inv2, config='--psm 6 --oem 3')
        if t2.strip():
            texts.append(t2.strip())
    except Exception:
        pass

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

def extract_timestamp(image_bgr: np.ndarray, method: str = 'tesseract') -> Optional[str]:
    """Extract date/time info from the top 20% of a Re-ID screenshot.

    Looks for:
      1. Date range from TIME filter: "Aug 3-4"
      2. AM/PM times: "7:00 AM", "12:00 PM"
      3. 24h times: "14:30:25"

    Uses Windows OCR (winocr) first when available — it handles the
    dark-themed Re-ID UI (white text on dark background) much better
    than Tesseract.  Falls back to Tesseract if winocr is unavailable
    or returns nothing.

    Args:
        image_bgr: BGR image (full screenshot)
        method: OCR backend hint ('tesseract' or 'winocr').
                When winocr is available it is always tried first
                regardless of this setting.

    Returns:
        Extracted date/time string, or None if nothing found.
    """
    # --- Strategy 1: Windows OCR (best for dark UI) ---
    if _WINOCR_AVAILABLE:
        text = _winocr_screenshot(image_bgr)
        if text:
            result = _parse_from_text(text)
            if result:
                return result

    # --- Strategy 2: Tesseract fallback ---
    text = _ocr_top20(image_bgr)
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
    return (month, day1, day2)


def _normalize_time_to_minutes(s: str) -> Optional[int]:
    """Convert '7:00 AM' or '14:30' to minutes since midnight."""
    # Try AM/PM
    m = re.match(r'(\d{1,2}):(\d{2})\s*(AM|PM)', s, re.IGNORECASE)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
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
        return h * 60 + mi

    return None


def timestamps_match(timestamp1: Optional[str], timestamp2: Optional[str],
                     tolerance_minutes: int = 30) -> bool:
    """Check if two extracted time/date values match.

    Handles three comparison modes:
      - Date range vs date range: "Aug 3-4" overlaps "Aug 3-4" -> True
      - Time vs time: "7:00 AM" vs "7:05 AM" within tolerance -> True
      - Mixed types or missing: graceful fallback -> True

    Args:
        timestamp1: First extracted string (or None)
        timestamp2: Second extracted string (or None)
        tolerance_minutes: Max difference in minutes for time comparisons.
            Default 30 minutes because reference images may span a range
            of capture times.

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
