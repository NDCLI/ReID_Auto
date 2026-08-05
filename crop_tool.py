"""
Helper tool to crop reference images from a large screenshot.
Usage:
    python crop_tool.py <image_path> <query_name>
"""
import sys
import os
import cv2

# Global variables for drawing
drawing = False
ix, iy = -1, -1
img = None
img_copy = None
current_query = "Query_X"
crop_count = 0

def draw_rect(event, x, y, flags, param):
    global ix, iy, drawing, img, img_copy, crop_count, current_query

    if event == cv2.EVENT_LBUTTONDOWN:
        drawing = True
        ix, iy = x, y

    elif event == cv2.EVENT_MOUSEMOVE:
        if drawing:
            img_copy = img.copy()
            cv2.rectangle(img_copy, (ix, iy), (x, y), (0, 255, 0), 2)
            cv2.imshow('Crop Tool', img_copy)

    elif event == cv2.EVENT_LBUTTONUP:
        drawing = False
        img_copy = img.copy()
        cv2.rectangle(img_copy, (ix, iy), (x, y), (0, 255, 0), 2)
        cv2.imshow('Crop Tool', img_copy)

        # Ensure valid coordinates
        x1, x2 = min(ix, x), max(ix, x)
        y1, y2 = min(iy, y), max(iy, y)
        
        if x2 - x1 > 10 and y2 - y1 > 10:
            crop = img[y1:y2, x1:x2]
            
            # Save the crop
            out_dir = os.path.join("queries", current_query)
            os.makedirs(out_dir, exist_ok=True)
            
            # Is it the first one (query image)?
            is_query = (crop_count == 0)
            
            if is_query:
                filename = "_query.jpg"
                print(f"Saved Query Image: {os.path.join(out_dir, filename)} (This will NOT be matched)")
            else:
                filename = f"result_{crop_count}.jpg"
                print(f"Saved Reference: {os.path.join(out_dir, filename)}")
                
            cv2.imwrite(os.path.join(out_dir, filename), crop)
            crop_count += 1
            
            # Draw persistent box on original image to show what was cropped
            cv2.rectangle(img, (x1, y1), (x2, y2), (255, 0, 0), 2)
            cv2.putText(img, filename, (x1, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)
            cv2.imshow('Crop Tool', img)

def main():
    global img, img_copy, current_query, crop_count
    
    if len(sys.argv) < 3:
        print("Usage: python crop_tool.py <path_to_image> <query_name>")
        print("Example: python crop_tool.py list.png Query_1")
        sys.exit(1)
        
    image_path = sys.argv[1]
    current_query = sys.argv[2]
    
    if not os.path.exists(image_path):
        print(f"Error: File '{image_path}' not found.")
        sys.exit(1)
        
    img = cv2.imread(image_path)
    if img is None:
        print(f"Error: Could not read '{image_path}'")
        sys.exit(1)
        
    # Resize for display if too large
    max_h = 900
    if img.shape[0] > max_h:
        scale = max_h / img.shape[0]
        img = cv2.resize(img, (0, 0), fx=scale, fy=scale)
        print(f"Image scaled down by {scale:.2f} for display.")
        
    img_copy = img.copy()
    
    print("\n--- INSTRUCTIONS ---")
    print("1. Click and drag to crop images.")
    print("2. The FIRST crop will be saved as '_query.jpg' (excluded from matching).")
    print("3. Subsequent crops will be saved as 'result_1.jpg', 'result_2.jpg', etc.")
    print("4. Press 'q' or 'ESC' to quit.\n")
    
    cv2.namedWindow('Crop Tool')
    cv2.setMouseCallback('Crop Tool', draw_rect)
    
    cv2.imshow('Crop Tool', img)
    while True:
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q') or key == 27:
            break
            
    cv2.destroyAllWindows()
    print(f"\nDone! Cropped {crop_count} images to queries/{current_query}/")

if __name__ == "__main__":
    main()
