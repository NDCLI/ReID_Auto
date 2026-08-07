"""
Simple test to check if EasyOCR can read text from an image.
"""

import cv2
import numpy as np

# Create a simple test image
img = np.ones((200, 600, 3), dtype=np.uint8) * 255
font = cv2.FONT_HERSHEY_SIMPLEX
cv2.putText(img, "TIME: 14:30:25", (50, 100), font, 1.5, (0, 0, 0), 3)
cv2.putText(img, "2024-01-15", (50, 150), font, 1.2, (0, 0, 0), 2)

print("Creating EasyOCR reader...")
try:
    import easyocr
    reader = easyocr.Reader(['en'], gpu=False, verbose=False)
    print("✓ Reader created successfully")

    print("\nReading text from image...")
    results = reader.readtext(img)

    print(f"\n✓ Found {len(results)} text regions:")
    for bbox, text, confidence in results:
        print(f"  - Text: '{text}' (confidence: {confidence:.3f})")

    # Test our parse function
    from ocr_utils import parse_timestamp_from_text
    all_text = ' '.join([text for _, text, _ in results])
    print(f"\nCombined text: {all_text}")

    timestamp = parse_timestamp_from_text(all_text)
    print(f"Parsed timestamp: {timestamp}")

except Exception as e:
    print(f"✗ Error: {e}")
    import traceback
    traceback.print_exc()
