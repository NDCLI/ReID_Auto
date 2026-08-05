import cv2
import numpy as np
import os
import sys
import argparse

def auto_extract_grid(image_path, output_dir):
    print(f"Đang xử lý ảnh: {image_path}")
    
    # 1. Đọc ảnh
    img = cv2.imread(image_path)
    if img is None:
        print(f"LỖI: Không thể đọc ảnh '{image_path}'. Vui lòng kiểm tra lại đường dẫn.")
        return

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # 2. Xử lý ảnh để tách phần hình ảnh (tối) ra khỏi nền (trắng/xám nhạt)
    # Ảnh có nền xám (giá trị ~135). Ta dùng threshold 120 để chỉ giữ lại phần ảnh thật sự tối (nhân vật).
    _, thresh = cv2.threshold(gray, 120, 255, cv2.THRESH_BINARY_INV)
    
    # Sử dụng Morphological Open (3x3) để xóa các đường kẻ viền mỏng (grid lines)
    kernel_open = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    opened = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel_open)
    
    # Sử dụng Morphological Close (15x15) để lấp đầy các khoảng trống bên trong từng ảnh (nối chúng thành 1 khối đặc)
    kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel_close)
    
    # 3. Tìm contours (đường viền)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = w * h
        
        # 4. Lọc các contours hợp lệ
        # - Ảnh nhân vật là hình chữ nhật dọc (chiều cao > chiều rộng)
        # - Loại bỏ các đường kẻ viền (chiều rộng > 20)
        # - Loại bỏ các đốm nhiễu, chữ (diện tích > 2000)
        if h > w and w > 20 and area > 2000:
            boxes.append([x, y, w, h])
            
    if not boxes:
        print("Không tìm thấy ảnh nào! Bạn hãy thử kiểm tra lại file đầu vào.")
        return
        
    print(f"Đã tìm thấy {len(boxes)} khung ảnh riêng lẻ. Đang phân loại thành các cột...")

    # 5. Phân nhóm các box thành từng cột (Query) dựa theo tọa độ x
    columns = {}
    for box in boxes:
        x, y, w, h = box
        center_x = x + w // 2
        
        matched_col = None
        for col_x in columns.keys():
            # Nếu tâm của ảnh nằm gần cột đã có (sai số bằng nửa chiều rộng)
            if abs(center_x - col_x) < w: 
                matched_col = col_x
                break
                
        if matched_col is not None:
            columns[matched_col].append(box)
        else:
            columns[center_x] = [box]
            
    # Sắp xếp các cột từ trái sang phải
    sorted_cols = sorted(columns.keys())
    
    print(f"Phát hiện tổng cộng {len(sorted_cols)} cột (Query).")
    
    # 6. Trích xuất và lưu file
    os.makedirs(output_dir, exist_ok=True)
    img_vis = img.copy()
    
    for i, col_x in enumerate(sorted_cols):
        query_idx = i + 1
        query_name = f"Query_{query_idx}"
        query_dir = os.path.join(output_dir, query_name)
        os.makedirs(query_dir, exist_ok=True)
        
        # Sắp xếp các ảnh trong cùng 1 cột từ trên xuống dưới
        col_boxes = sorted(columns[col_x], key=lambda b: b[1])
        
        print(f"  [{query_name}]: Lấy được {len(col_boxes)} ảnh")
        
        for j, box in enumerate(col_boxes):
            x, y, w, h = box
            
            # Cắt ảnh từ ảnh gốc
            crop = img[y:y+h, x:x+w]
            
            # Đặt tên: Tất cả đều là ảnh để đi tìm (template)
            filename = f"template_{j+1}.jpg"
                
            out_path = os.path.join(query_dir, filename)
            cv2.imwrite(out_path, crop)
            
            # Vẽ lên ảnh visualize để review
            cv2.rectangle(img_vis, (x, y), (x+w, y+h), (0, 255, 0), 2)
            cv2.putText(img_vis, f"{query_idx}-{filename}", (x, y-5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
            
    # Lưu ảnh kết quả visualize
    vis_path = os.path.join(output_dir, "auto_crop_preview.jpg")
    cv2.imwrite(vis_path, img_vis)
    print("\n" + "="*50)
    print(f"HOÀN THÀNH! Tất cả ảnh đã được chia vào thư mục: {output_dir}")
    print(f"Bạn có thể xem file '{vis_path}' để kiểm tra xem tool cắt có chuẩn không.")
    print("="*50)

def main():
    parser = argparse.ArgumentParser(description="Tự động cắt ảnh từ bảng danh sách (Excel/Screenshot)")
    parser.add_argument("image_path", help="Đường dẫn đến file ảnh chứa bảng (ví dụ: list.png)")
    parser.add_argument("--out", default="queries", help="Thư mục xuất ra (mặc định: thư mục 'queries')")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.image_path):
        print(f"Lỗi: Không tìm thấy file '{args.image_path}'")
        sys.exit(1)
        
    auto_extract_grid(args.image_path, args.out)

if __name__ == "__main__":
    main()
