# AutoMarker Re-ID

Công cụ tự động hóa nhận diện nhân vật Re-ID và tự động vẽ khung đỏ từ ảnh chụp màn hình (Clipboard). Ứng dụng chạy ngầm ở khay hệ thống (System Tray), hoạt động mượt mà và tối ưu hóa cho thao tác bằng một tay.

---

## 🌟 Tính năng nổi bật

1. **Giám sát Clipboard thông minh**:
   * Tự động nhận diện khi bạn vừa chụp ảnh màn hình mới (hỗ trợ cả **Snipping Tool** và **ShareX**).
   * **Chống bắt nhầm ảnh rác**: Chỉ kích hoạt khi ảnh chụp màn hình chứa giao diện ứng dụng Re-ID (quét nhanh nhãn định vị `TIME` đa tỷ lệ). Tự động bỏ qua các ảnh copy từ trình duyệt web, Office, tệp tin cũ hoặc ứng dụng chat (Zalo, Messenger...).
2. **OCR Timestamp Matching (MỚI)**:
   * Trích xuất thời gian trên các ảnh thẻ kết quả nhỏ bằng RapidOCR.
   * Chỉ vẽ khung khi **CẢ** tỉ lệ ảnh **VÀ** thời gian đều khớp.
   * Tránh vẽ nhầm khung lên ảnh cùng người nhưng khác thời điểm.
   * Xem [OCR_SETUP.md](OCR_SETUP.md) để cài đặt và cấu hình.
3. **Khớp nhân vật AI cục bộ & Tối ưu siêu tốc**:
   * Trích xuất đặc trưng (Feature Extraction) bằng mô hình AI (ONNX/OpenVINO).
   * **Đa luồng (Multi-threading)**: Quét OCR và AI đồng thời giúp giảm thời gian xuống dưới 1 giây.
   * **Fast-root**: Chế độ nhận diện lưới kết quả thông minh giúp quét siêu tốc ngay cả khi chọn "Tất cả" hay 1 thư mục riêng lẻ.
   * **Caching & Background Init**: Tự động nạp model chạy ngầm, không làm đơ giao diện; sử dụng cache `.npz` và `.txt` để chuyển thư mục tức thì.
4. **Vẽ khung thông minh (Dynamic Insets)**:
   * Tự động thụt lề `2px` ở các ảnh sát cạnh nhau để tránh dính khung đỏ (tạo khoảng hở `4px` trực quan).
   * Đối với ảnh to hoặc đơn lẻ, khung đỏ tự động bám khít `0px` vào mép ngoài của ảnh.
5. **Bảng hiển thị nhanh OSD (On-Screen Display)**:
   * Hiển thị trạng thái chuyển thư mục và thông báo lỗi.
   * Ghi đè tức thời không trễ khi chuyển đổi nhanh liên tục.
6. **Hệ thống phím tắt bên tay trái cực kỳ tiện lợi**:
   * `Ctrl + Shift + A`: Quay lại thư mục trước (Lùi).
   * `Ctrl + Shift + D`: Sang thư mục tiếp theo (Tiến).
   * `Ctrl + Shift + Q`: Trở về thư mục gốc (Tất cả).
   * `Ctrl + Shift + 1` đến `9`: Chọn nhanh thư mục con từ 1 đến 9.
   * `Ctrl + Shift + Space` (Phím cách): **Tạm dừng / Tiếp tục** quá trình tự động quét ảnh màn hình.
   * `Ctrl + Shift + N`: Chuyển nhanh sang Query trống kế tiếp khi tự thu thập ảnh mẫu.
7. **Tối ưu hóa khay hệ thống (System Tray) & Tự động chạy**:
   * Ứng dụng **tự động chạy và quét ngay lập tức** khi mở lên.
   * Chạy ẩn gọn gàng dưới System Tray. Click đúp chuột trái để mở cấu hình chính.
   * Click chuột trái 1 lần hoặc click phải để chọn nhanh thư mục mẫu.
8. **Cửa sổ Review tiện dụng**:
   * Cho phép nhấn `Esc` để đóng/tắt nhanh cửa sổ Review kết quả.

---

## 🛠 Hướng dẫn cài đặt & Khởi chạy

### Yêu cầu hệ thống
* Hệ điều hành: Windows (7/10/11)
* Python 3.8 trở lên

### Các bước cài đặt:
1. Double-click vào file **`install.bat`** để tự động tạo môi trường ảo Python và cài đặt đầy đủ các thư viện phụ thuộc cần thiết (`opencv`, `pillow`, `pystray`, `customtkinter`, `onnxruntime`, ...).
2. Chuẩn bị các thư mục ảnh nhân vật mục tiêu cần đối sánh bên trong thư mục `queries/`.

### Khởi chạy:
* Chạy file **`Run.bat`** để khởi động ứng dụng chạy ngầm ở khay hệ thống.

### Bản Portable (không cần cài Python)
* Mở `dist/AutoMarker-ReID-Portable/AutoMarker-ReID.exe` để chạy trực tiếp.
* Luôn giữ nguyên cả thư mục `AutoMarker-ReID-Portable` khi sao chép sang máy
  khác: EXE cần thư mục `_internal` đi kèm.
* `queries/` và `output/` nằm cạnh EXE. Có thể chép Query hiện có vào đây để
  mang theo dữ liệu nhận diện; kết quả mới sẽ được ghi vào `output/`.
* Để tạo lại bản portable từ mã nguồn, chạy
  `python -m PyInstaller --noconfirm --clean ReID_Auto_Draw_OSNet.spec`.

---

## 📁 Cấu trúc thư mục mã nguồn sạch
* `app_gui.pyw`: Mã nguồn giao diện Tkinter (CustomTkinter) và quản lý phím tắt hệ thống, khay hệ thống.
* `create_shortcut.ps1`: Tạo shortcut **RE-ID Auto Draw** ngoài Desktop bằng icon của app.
* `auto_marker.py`: Chứa các hàm giám sát clipboard, bộ lọc screenshot nâng cao, và thuật toán vẽ khung.
* `preview_win.py`: Giao diện cửa sổ xem trước (Review) kết quả khớp khung đỏ.
* `ai_model.py`: Trích xuất đặc trưng ảnh sử dụng mô hình MobileNetV2 ReID.
* `ui_template.png`: Ảnh mẫu giao diện dùng để xác thực nhanh screenshot Re-ID.
* `.gitignore`: Cấu hình loại bỏ hoàn toàn dữ liệu tạm, file rác cá nhân khi đẩy lên GitHub.

---

## AI ReID nhiều model và cơ chế chống nhận nhầm

Pipeline mới phân loại mỗi ảnh ứng viên với **toàn bộ danh tính** trong `queries/`,
thay vì chỉ xác minh với template đã tìm ra vị trí. Một kết quả chỉ được nhận khi:

* Điểm ensemble đạt `AI_MATCH_THRESHOLD`.
* Điểm hạng nhất hơn hạng nhì ít nhất `AI_MATCH_MARGIN`.
* Ít nhất một ảnh tham chiếu phải đạt `AI_BEST_REFERENCE_THRESHOLD` để loại ứng viên chỉ giống màu áo hoặc dáng.
* Khi có từ hai model, các model phải đồng ý về danh tính thắng cuộc.

Model `person-reidentification-retail-0288` đi kèm luôn hoạt động. Cấu hình cũng
hỗ trợ model mạnh hơn `person-reidentification-retail-0277` và TransReID
ViT-Base; `install.bat` tải các model một lần vào
`%LOCALAPPDATA%\ReIDAuto\models`. Backend tự phát hiện model trong project hoặc
cache. Nhánh khuôn mặt dùng `face-detection-retail-0005` và
`face-reidentification-retail-0095` để cứu các trường hợp cùng người đổi trang phục;
chỉ chấp nhận khi điểm khuôn mặt và khoảng cách với Query hạng nhì đều đủ an toàn.
Nếu model tùy chọn không tồn tại, chương trình bỏ qua model đó và tiếp tục
chạy bằng các model còn lại. Nguồn và checksum model được ghi trong
`MODEL_SOURCES.md`.

Các ngưỡng trong `config.py` cần được hiệu chỉnh bằng ảnh đúng/sai của môi trường
thực tế. Không model nào bảo đảm đúng 100% với dữ liệu chưa từng thấy; cơ chế
margin và từ chối giúp chương trình không tự đoán khi bằng chứng chưa đủ mạnh.

---

## Tự phân nhóm Query và xử lý Batch

### Tự thu thập và phân nhóm Query từ Clipboard

Bật công tắc **Tự nhận ảnh chụp 1 người từ Clipboard và lưu vào Query**, sau đó
dùng Snipping Tool, ShareX hoặc công cụ chụp màn hình bất kỳ để chụp từng người
một. Ngay khi ảnh vào Clipboard, công cụ sẽ:

1. Kiểm tra ảnh có phải crop dọc của một người hay không.
2. So sánh bằng ensemble OSNet + OSNet-LCT + TransReID.
3. Lưu người giống nhau vào cùng `Query_N`.
4. Tự tạo `Query_N` tiếp theo khi gặp người khác.
5. Cập nhật matcher đang chạy mà không cần khởi động lại model.

Nếu thư mục `queries/` đang trống, ảnh người đầu tiên tự tạo `Query_1`. Ảnh ngang,
ảnh quá nhỏ hoặc ảnh không giống crop người sẽ bị bỏ qua để tránh tạo Query rác.
Screenshot giao diện kết quả Re-ID vẫn đi vào luồng vẽ khung bình thường.

### Batch và thư viện duyệt

Bấm **BATCH: CHỌN NHIỀU ẢNH & MỞ THƯ VIỆN DUYỆT** để chọn nhiều screenshot.
Sau khi xử lý, cửa sổ thư viện cho phép:

* Tự nhận diện, hiển thị và sắp xếp từng screenshot theo `Query_N`.
* Chuyển qua lại giữa các ảnh.
* Click vào ảnh để thêm hoặc xóa khung (tự động snap theo quy tắc vẽ khung).
* Lưu riêng ảnh đang duyệt hoặc lưu toàn bộ batch.

Kết quả được lưu theo từng Query trong
`output/batch_YYYYMMDD_HHMMSS/Query_N/`. Ảnh không có khung khớp được đưa vào
`Chua_xac_dinh/` để tiện kiểm tra lại.

### Chế độ lưới siêu tốc (Fast-Grid Mode)

Khi ứng dụng phát hiện màn hình là dạng lưới (Grid) thẻ kết quả, tính năng tăng tốc sẽ tự động kích hoạt dù bạn chọn **Tất cả (Root)** hay **Từng Query riêng lẻ**. OSNet-LCT sẽ quét sơ bộ qua toàn bộ các thẻ, sau đó ensemble đầy đủ (bao gồm TransReID và FaceModel) chỉ kiểm tra lại những thẻ có khả năng khớp cao nhất. Điều này giúp giảm số lần chạy AI từ hàng chục lần xuống chỉ còn 1-3 lần, giúp vẽ khung gần như tức thời. Nếu ảnh không có bố cục lưới hợp lệ, app tự quay về cách dò tự do (quét toàn bộ).

Ảnh đặt trực tiếp tại gốc `queries/` được coi là ảnh mẫu/test khi đã có các thư
mục `Query_N`, nên không bị nạp nhầm thành một người truy vấn mới. Giới hạn số
khung vẫn được giữ nguyên: Query có 4 ảnh chỉ được vẽ tối đa 3 khung.
