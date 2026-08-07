"""
Test script for OCR timestamp extraction functionality.

Tests OCR reading from sample images and timestamp matching logic.
"""

import cv2
import numpy as np
from ocr_utils import extract_timestamp, parse_timestamp_from_text, timestamps_match


def test_parse_timestamp():
    """Test timestamp parsing from various text formats."""
    print("\n" + "="*60)
    print("Testing timestamp parsing...")
    print("="*60)

    test_cases = [
        ("2024-01-15 14:30:25", "2024-01-15 14:30:25"),
        ("2024/01/15 14:30:25", "2024-01-15 14:30:25"),
        ("15-01-2024 14:30:25", "2024-01-15 14:30:25"),
        ("TIME: 14:30:25", "14:30:25"),
        ("Some text\n2024-01-15 14:30:25\nMore text", "2024-01-15 14:30:25"),
        ("14:30:25 on 2024-01-15", "2024-01-15 14:30:25"),
        ("No timestamp here", None),
    ]

    passed = 0
    for text, expected in test_cases:
        result = parse_timestamp_from_text(text)
        status = "✓" if result == expected else "✗"
        print(f"{status} Input: {text[:40]:40s} -> {result}")
        if result == expected:
            passed += 1

    print(f"\nPassed: {passed}/{len(test_cases)}")


def test_timestamp_matching():
    """Test timestamp matching logic."""
    print("\n" + "="*60)
    print("Testing timestamp matching...")
    print("="*60)

    test_cases = [
        ("2024-01-15 14:30:25", "2024-01-15 14:30:28", True, "Within 5 seconds"),
        ("2024-01-15 14:30:25", "2024-01-15 14:30:35", False, "More than 5 seconds"),
        ("14:30:25", "14:30:28", True, "Time-only within tolerance"),
        ("14:30:25", "14:30:35", False, "Time-only outside tolerance"),
        ("2024-01-15 14:30:25", None, True, "Missing timestamp allows match"),
        (None, "2024-01-15 14:30:25", True, "Missing timestamp allows match"),
        (None, None, True, "Both missing allows match"),
    ]

    passed = 0
    for ts1, ts2, expected, description in test_cases:
        result = timestamps_match(ts1, ts2, tolerance_seconds=5)
        status = "✓" if result == expected else "✗"
        print(f"{status} {description:40s}: {ts1} vs {ts2} -> {result}")
        if result == expected:
            passed += 1

    print(f"\nPassed: {passed}/{len(test_cases)}")


def test_ocr_extraction():
    """Test OCR extraction from images (requires pytesseract or easyocr)."""
    print("\n" + "="*60)
    print("Testing OCR extraction from images...")
    print("="*60)

    # Create a test image with timestamp text
    test_img = np.ones((200, 600, 3), dtype=np.uint8) * 255

    # Add timestamp text
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(test_img, "TIME: 14:30:25", (50, 100), font, 1.5, (0, 0, 0), 2)
    cv2.putText(test_img, "2024-01-15", (50, 150), font, 1.0, (0, 0, 0), 2)

    print("\nCreated test image with timestamp: TIME: 14:30:25, 2024-01-15")

    # Test with different methods
    methods = ['tesseract', 'easyocr', 'auto']

    for method in methods:
        print(f"\n  Testing with method='{method}'...")
        try:
            result = extract_timestamp(test_img, method=method)
            if result:
                print(f"  ✓ Extracted timestamp: {result}")
            else:
                print(f"  ✗ Could not extract timestamp")
        except ImportError as e:
            print(f"  ⚠ OCR library not available for '{method}': {e}")
        except Exception as e:
            print(f"  ✗ Error during OCR extraction: {e}")
            import traceback
            traceback.print_exc()

    # Final summary
    print("\n" + "-"*60)
    print("Note: If all methods failed, install OCR libraries:")
    print("  pip install pytesseract  # Requires Tesseract-OCR installed")
    print("  pip install easyocr      # Self-contained but slower")


def main():
    """Run all tests."""
    print("\n╔════════════════════════════════════════════════════════════╗")
    print("║         OCR Timestamp Extraction Test Suite               ║")
    print("╚════════════════════════════════════════════════════════════╝")

    test_parse_timestamp()
    test_timestamp_matching()
    test_ocr_extraction()

    print("\n" + "="*60)
    print("Test suite completed!")
    print("="*60)
    print("\nNote: OCR extraction test requires pytesseract or easyocr.")
    print("See OCR_SETUP.md for installation instructions.")
    print()


if __name__ == "__main__":
    main()
