# OCR Setup Guide - Hướng dẫn cài đặt OCR

Tính năng OCR đọc thời gian trên ảnh để so sánh và chỉ vẽ khung khi cả tỉ lệ ảnh và thời gian đều khớp.

## Hai engine OCR

Ứng dụng sử dụng **hai** engine OCR, không cần cài thêm phần mềm ngoài:

| Engine | Vai trò | Ghi chú |
|---|---|---|
| **RapidOCR + OpenVINO** | OCR chính cho ảnh crop nhỏ & fallback cho screenshot | Model ONNX cục bộ, không cần PyTorch |
| **Windows OCR (winocr)** | OCR chính cho full screenshot & fallback cho crop nhỏ | Engine WinRT tích hợp sẵn trên Windows 10/11 |

### Ảnh crop nhỏ (thẻ người ~78×187 px)

1. **RapidOCR** — chạy consensus trên nhiều crop phóng to ở đáy thẻ.
2. **Windows OCR** — fallback nếu RapidOCR không đọc được.

### Full screenshot (quét TIME filter ở góc trên-trái)

1. Backend theo `OCR_METHOD` được thử trước.
2. Backend còn lại được dùng làm fallback nếu engine đầu tiên không khả dụng hoặc không đọc được.

## Cài đặt

Tất cả dependency đã nằm trong `requirements.txt`:

```bash
.venv\Scripts\activate
pip install -r requirements.txt
```

Khi chạy lần đầu, RapidOCR có thể tải model OCR vào cache của Python; không thêm cache model vào repository.

## Bật tính năng OCR trong config.py

Mở file `config.py` nếu cần kiểm tra:

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

### OCR không đọc được thời gian
**Giải pháp:**
- Kiểm tra ảnh mẫu có hiển thị thời gian rõ ràng không
- Kiểm tra ảnh mẫu và thẻ kết quả có hiển thị đúng giờ/phút không
- Kiểm tra vùng hiển thị thời gian trên ảnh có bị che khuất không

### RapidOCR không khởi tạo được
**Nguyên nhân:** OpenVINO chưa cài đúng hoặc thiếu DLL runtime

**Giải pháp:**
1. Đảm bảo đã cài `openvino` qua `pip install -r requirements.txt`
2. Khởi động lại terminal sau khi cài

### Windows OCR không hoạt động
**Nguyên nhân:** Package `winocr` chưa cài hoặc hệ điều hành không hỗ trợ

**Giải pháp:**
- `winocr` yêu cầu Windows 10 1809+ hoặc Windows 11
- Cài lại: `pip install winocr`
- Nếu không dùng được, RapidOCR vẫn là engine chính cho crop nhỏ

## Performance

- **RapidOCR/OpenVINO:** model OCR cục bộ, phù hợp hơn với chữ nhỏ trên thẻ.
- **Windows OCR:** đọc rất tốt chữ trắng trên nền tối của giao diện Re-ID.
- **Không ảnh hưởng:** Nếu tắt `ENABLE_OCR_TIMESTAMP_FILTER = False`, không có overhead.

## Lưu ý quan trọng

- Nếu ảnh chụp màn hình có chất lượng thấp hoặc chữ nhỏ, OCR có thể đọc sai
- Trong trường hợp đó, kiểm tra lại OCR hoặc tắt tính năng OCR nếu cần
