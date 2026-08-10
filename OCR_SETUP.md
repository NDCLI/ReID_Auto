# OCR Setup Guide - Hướng dẫn cài đặt OCR

Tính năng OCR đọc thời gian trên ảnh để so sánh và chỉ vẽ khung khi cả tỉ lệ ảnh và thời gian đều khớp.

## OCR chính cho ảnh crop nhỏ

Ứng dụng dùng **RapidOCR + OpenVINO CPU** làm OCR chính cho thời gian nhỏ ở
đáy mỗi thẻ. RapidOCR chạy model ONNX cục bộ, không cần PyTorch và tránh các
lỗi DLL thường gặp với EasyOCR. Khi chạy lần đầu, RapidOCR có thể tải model OCR
vào cache của Python; không thêm cache model vào repository.

Nếu RapidOCR không cài được hoặc không đọc được ảnh, ứng dụng tự fallback lần
lượt sang Windows OCR rồi Tesseract. Crop không đọc được sẽ không làm hỏng quá
trình matching và vẫn được AI đánh giá.

Cài cùng các dependency của dự án:

```bash
.venv\\Scripts\\activate
pip install -r requirements.txt
```

## Cài đặt Tesseract OCR (Fallback)

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

RapidOCR được dùng tự động nếu đã cài từ `requirements.txt`. Tesseract chỉ là
fallback. Sau khi cài các dependency, mở file `config.py` nếu cần kiểm tra:

```python
# Bật OCR
ENABLE_OCR_TIMESTAMP_FILTER = True  # Đổi từ False sang True

# Dung sai thời gian (phút). 0 = phải khớp đúng HH:MM
OCR_TIMESTAMP_TOLERANCE = 0

# Backend hint cho OCR toàn màn hình; crop nhỏ vẫn dùng RapidOCR trước
OCR_METHOD = 'winocr'
```

## Cách hoạt động

1. **Khi load ảnh mẫu:** RapidOCR đọc thời gian ở từng ảnh crop trong thư mục Query.
2. **Khi nhận ảnh từ clipboard:** App đọc thời gian ở từng thẻ kết quả rồi:
   - So sánh khớp đúng HH:MM với các thời gian mẫu của Query tương ứng.
   - Loại thẻ đọc được giờ khác mẫu; thẻ không đọc được vẫn để AI quyết định.
   - Chỉ vẽ tối đa số mẫu trừ thẻ nguồn.

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
- Kiểm tra ảnh mẫu và thẻ kết quả có hiển thị đúng giờ/phút không
- Kiểm tra vùng hiển thị thời gian trên ảnh có bị che khuất không

### Lỗi DLL với EasyOCR
**Nguyên nhân:** EasyOCR phụ thuộc vào PyTorch có vấn đề DLL trên một số máy Windows

**Giải pháp:** Dùng Tesseract thay vì EasyOCR (đã được cấu hình mặc định)

## Performance

- **RapidOCR/OpenVINO:** model OCR cục bộ, phù hợp hơn với chữ nhỏ trên thẻ.
- **Windows OCR:** fallback cho crop mà RapidOCR không đọc được.
- **Tesseract:** fallback cuối cùng, nhẹ nhưng kém ổn định hơn với chữ rất nhỏ.
- **Không ảnh hưởng:** Nếu tắt `ENABLE_OCR_TIMESTAMP_FILTER = False`, không có overhead.

## Lưu ý quan trọng

- Tesseract cần **ảnh có độ phân giải cao và chữ rõ ràng** để đọc chính xác
- Nếu ảnh chụp màn hình có chất lượng thấp hoặc chữ nhỏ, OCR có thể đọc sai
- Trong trường hợp đó, kiểm tra lại OCR hoặc tắt tính năng OCR nếu cần
