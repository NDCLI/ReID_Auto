# OCR Setup Guide - Hướng dẫn cài đặt OCR

Tính năng OCR đọc thời gian trên ảnh để so sánh và chỉ vẽ khung khi cả tỉ lệ ảnh và thời gian đều khớp.

## Cài đặt Tesseract OCR (Khuyên dùng)

### Bước 1: Tải và cài Tesseract cho Windows

1. **Tải Tesseract:**
   - Truy cập: https://github.com/UB-Mannheim/tesseract/wiki
   - Chọn phiên bản mới nhất: `tesseract-ocr-w64-setup-5.x.x.exe`
   - Chạy file cài đặt và làm theo hướng dẫn

2. **Thêm Tesseract vào PATH:**
   - Mặc định cài tại: `C:\Program Files\Tesseract-OCR`
   - Thêm đường dẫn này vào biến môi trường PATH:
     * Bấm `Windows + R`, gõ `sysdm.cpl` và Enter
     * Chọn tab "Advanced" → "Environment Variables"
     * Trong "System variables", tìm `Path` và bấm "Edit"
     * Bấm "New" và thêm: `C:\Program Files\Tesseract-OCR`
     * Bấm OK để lưu

3. **Khởi động lại Command Prompt hoặc PowerShell** để áp dụng PATH mới

### Bước 2: Cài thư viện Python

```bash
.venv\Scripts\activate
pip install pytesseract
```

### Bước 3: Kiểm tra cài đặt

```bash
tesseract --version
```

Nếu thấy phiên bản Tesseract (ví dụ: `tesseract 5.3.0`) thì đã cài thành công!

## Bật tính năng OCR trong config.py

Sau khi cài Tesseract xong, mở file `config.py` và thay đổi:

```python
# Bật OCR
ENABLE_OCR_TIMESTAMP_FILTER = True  # Đổi từ False sang True

# Độ dung sai thời gian (giây)
OCR_TIMESTAMP_TOLERANCE = 5  # Cho phép chênh lệch tối đa 5 giây

# Phương pháp OCR
OCR_METHOD = 'tesseract'
```

## Cách hoạt động

1. **Khi load ảnh mẫu:** App sẽ đọc thời gian từ ảnh đầu tiên trong mỗi thư mục Query
2. **Khi nhận ảnh từ clipboard:** App sẽ:
   - Đọc thời gian trên ảnh mới
   - So sánh với thời gian của ảnh mẫu
   - Chỉ vẽ khung nếu:
     * Tỉ lệ ảnh giống nhau (như cũ)
     * **VÀ** thời gian khớp nhau (trong khoảng dung sai 5 giây)

## Định dạng thời gian hỗ trợ

OCR nhận diện được nhiều định dạng:
- `2024-01-15 14:30:25`
- `2024/01/15 14:30:25`
- `15-01-2024 14:30:25`
- `TIME: 14:30:25`
- `14:30:25 2024-01-15`

## Kiểm tra hoạt động

Chạy test để kiểm tra:

```bash
.venv\Scripts\python test_ocr.py
```

Khi chạy app, bạn sẽ thấy log:
```
[OCR] Query_1: detected timestamp = 2024-01-15 14:30:25
[OCR] Screenshot timestamp: 2024-01-15 14:30:28
[OCR] Timestamp filter: 5 -> 3 matches
```

## Tắt tính năng OCR

Nếu không cần OCR hoặc gặp lỗi, tắt trong `config.py`:
```python
ENABLE_OCR_TIMESTAMP_FILTER = False
```

App sẽ hoạt động bình thường chỉ dựa vào tỉ lệ ảnh như trước.

## Xử lý lỗi thường gặp

### Lỗi: "pytesseract.pytesseract.TesseractNotFoundError"
**Nguyên nhân:** Tesseract chưa được cài đặt hoặc chưa thêm vào PATH

**Giải pháp:**
1. Kiểm tra Tesseract đã cài chưa: mở CMD và gõ `tesseract --version`
2. Nếu chưa cài, tải và cài từ link ở trên
3. Nếu đã cài nhưng vẫn lỗi, thêm vào PATH theo hướng dẫn Bước 1.2
4. Khởi động lại terminal/CMD sau khi thêm PATH
5. Nếu vẫn không được, chỉ định đường dẫn trực tiếp trong code:
   ```python
   # Thêm vào đầu file ocr_utils.py
   import pytesseract
   pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
   ```

### OCR không đọc được thời gian
**Giải pháp:** 
- Kiểm tra ảnh mẫu có hiển thị thời gian rõ ràng không
- Tăng `OCR_TIMESTAMP_TOLERANCE` lên 10-15 giây nếu thời gian chênh lệch nhiều
- Kiểm tra vùng hiển thị thời gian trên ảnh có bị che khuất không

### Lỗi DLL với EasyOCR
**Nguyên nhân:** EasyOCR phụ thuộc vào PyTorch có vấn đề DLL trên một số máy Windows

**Giải pháp:** Dùng Tesseract thay vì EasyOCR (đã được cấu hình mặc định)

## Performance

- **Tesseract:** ~50-100ms mỗi ảnh, nhẹ, ổn định
- **EasyOCR:** ~200-500ms mỗi ảnh, chính xác hơn nhưng có vấn đề DLL trên Windows
- **Không ảnh hưởng:** Nếu tắt `ENABLE_OCR_TIMESTAMP_FILTER = False`, không có overhead

## Lưu ý quan trọng

- Tesseract cần **ảnh có độ phân giải cao và chữ rõ ràng** để đọc chính xác
- Nếu ảnh chụp màn hình có chất lượng thấp hoặc chữ nhỏ, OCR có thể đọc sai
- Trong trường hợp đó, tăng `OCR_TIMESTAMP_TOLERANCE` hoặc tắt tính năng OCR
