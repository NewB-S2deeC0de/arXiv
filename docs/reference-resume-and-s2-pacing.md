# Báo cáo chỉnh sửa references và giãn cách request S2

## Phạm vi

Báo cáo trình bày hai mục đã được yêu cầu sửa: phục hồi references khi cache trang bị mất/hỏng và giãn cách request Semantic Scholar (S2) theo thời điểm gửi thực tế. Mốc so sánh là code ngay trước lần sửa này, đã có bản tối ưu backoff S2 trước đó.

## 1. Phục hồi references khi cache trang bị mất hoặc hỏng

### Vấn đề của code cũ

Khi crawl references nhiều trang, một trang có thể đã mang trạng thái `succeeded` nhưng cache bị mất, hỏng hoặc chưa đồng bộ đầy đủ trên Drive.

Khi resume, pipeline cố tải lại trang đó nhưng scheduler từ chối job đã kết thúc, gây lỗi `Cannot send terminal job`. Lỗi này có thể làm dừng toàn bộ nhóm tác vụ (`TaskGroup`). Cache đúng checksum nhưng chứa JSON hoặc thông tin phân trang không hợp lệ cũng có thể gây lỗi chưa được xử lý.

### Thay đổi đã thực hiện

- Kiểm tra đồng thời marker, fingerprint, checksum và cấu trúc dữ liệu trang trước khi dùng cache.
- Dùng lại trang cache hợp lệ mà không gửi request tải lại.
- Chuyển trang `succeeded` có cache không hợp lệ về `pending` nếu còn lượt thử; lưu thay đổi trước khi gửi request.
- Giữ nguyên `attempts`, tổng lượt thử `total` và mã chu kỳ `cycle`. Phục hồi không cấp thêm ba lượt thử mới; request tiếp theo tiêu hao một lượt như bình thường.
- Khi không thể tiếp tục tải trang, ghi paper là `failed` và chuyển sang paper khác thay vì phát sinh lỗi trạng thái làm dừng pipeline.
- Xử lý marker sai cấu trúc như cache không hợp lệ, kể cả trong bước phục hồi lúc khởi động.

| File thay đổi | Nội dung |
|---|---|
| `src/crawler/pipeline.py` | Kiểm tra cache đầy đủ, phục hồi trạng thái trước khi tải lại và xử lý thất bại ở cấp paper. |
| `src/crawler/progress.py` | Thêm `recover_reference_page()` để mở lại trang bằng số lượt còn lại; xử lý marker sai cấu trúc. |

### Vấn đề được giải quyết

Crawler có thể tiếp tục paper đang crawl dở dù trạng thái trang và cache không khớp, đồng thời giữ lịch sử retry. Paper không phục hồi được không làm các paper còn lại dừng theo vì lỗi `Cannot send terminal job`.

Phần sửa này áp dụng cho dữ liệu tạm theo trang của snapshot references chưa hoàn tất. Paper đã thành công vẫn được bỏ qua khi resume; kiểm tra toàn vẹn kết quả đã hoàn tất thuộc chức năng audit riêng. Nếu trang tải lại trả về `next` khác trước, pipeline đi theo chuỗi phân trang mới.

## 2. Giãn cách request S2 theo thời điểm gửi thực tế

### Vấn đề của code cũ

Timestamp và khoảng giãn cách S2 được tính trước khi hoàn thành thao tác lưu trạng thái lên Drive và ghi log. Khi thời gian ghi biến động, timestamp đã lưu có thể sớm hơn đáng kể so với thời điểm gửi HTTP. Request tiếp theo có thể được gửi quá gần request trước, làm tăng khả năng gặp HTTP 429.

### Thay đổi đã thực hiện

- Tiếp tục checkpoint số lượt thử trước khi gửi request.
- Kiểm tra lại điều kiện chờ sau thao tác ghi trạng thái dịch vụ và log.
- Ghi timestamp ngay trước khi gọi HTTP request; tính deadline tiếp theo từ thời điểm này.
- Dùng đồng hồ monotonic để kiểm soát khoảng cách trong phiên, kết hợp deadline theo thời gian thực đã lưu để phục hồi qua restart. Thời gian nhận response được tính vào khoảng giãn cách.
- Cập nhật timestamp trong cửa sổ giới hạn request anonymous theo thời điểm gửi.
- Lưu thêm marker `dispatch_pending` trước khi gửi. Nếu restart thấy marker này, scheduler chờ ít nhất một khoảng giãn cách cấu hình từ lúc khởi tạo scheduler vì timestamp gửi cuối có thể chưa được lưu. Deadline dài hơn đã tồn tại vẫn được giữ; timestamp anonymous cuối cũng được điều chỉnh theo hướng thận trọng.
- Khi request kết thúc hoặc thoát do lỗi/ngắt, lưu timestamp/cooldown hiện có và xóa cờ `dispatch_pending`. File trạng thái cũ chưa có trường này vẫn đọc được.

File thay đổi: `src/crawler/scheduler.py`.

### Vấn đề được giải quyết

Thời gian ghi Drive/log biến động không còn khiến timestamp trước thao tác ghi bị dùng làm mốc gửi request. Khoảng giãn cách được kiểm soát sát thời điểm gọi HTTP hơn, đồng thời có cơ chế chờ thận trọng khi phiên trước bị ngắt giữa lúc gửi và lưu timestamp.

Timestamp được đo tại thời điểm gọi vào `httpx`; thời gian request đến server còn phụ thuộc mạng. Bản sửa xử lý một nguyên nhân gây request quá gần nhau trong code, không bảo đảm loại bỏ mọi lỗi 429 từ server.

Chính sách `Retry-After`, cooldown đã lưu, trần backoff S2 tự tính và cách giãn request arXiv được giữ như trước lần sửa này.

## 3. Kết quả kiểm thử

Môi trường: Linux, Python 3.14.

| Kiểm tra | Kết quả |
|---|---|
| Toàn bộ bộ kiểm thử: `python3 -m pytest -q` | **120 tests pass** |
| `git diff --check` | Không phát hiện lỗi |

Bổ sung `tests/test_reference_resume_dispatch.py` với **9 trường hợp kiểm thử mới**:

1. Cache trang bị mất.
2. Checksum không khớp.
3. JSON không hợp lệ dù checksum khớp.
4. Thông tin phân trang không hợp lệ dù checksum khớp.
5. Marker sai cấu trúc.
6. Trang thiếu cache đã hết lượt thử: paper đó thất bại nhưng paper tiếp theo vẫn hoàn tất.
7. Thời gian ghi Drive/log biến động: khoảng cách gửi S2 vẫn đạt cấu hình; timestamp lưu khớp với lần gửi mô phỏng.
8. Restart khi còn `dispatch_pending`: scheduler chờ theo chính sách thận trọng.
9. Đồng hồ thời gian thực nhảy về phía trước không thể bỏ qua khoảng giãn cách monotonic.

Các kiểm thử phục hồi còn xác nhận trang cache hợp lệ phía trước được dùng lại, trang sửa chỉ tiêu hao lượt tiếp theo, chu kỳ không bị reset và resume sau khi thành công không phát sinh thêm request.

Kiểm thử dùng HTTP mô phỏng và dataset tạm. Chưa gọi API arXiv/Semantic Scholar thật hoặc benchmark tốc độ trực tiếp trên Colab cho bản sửa này.

## 4. Áp dụng với dữ liệu đã crawl trên Colab

1. Dừng phiên crawler đang ghi dữ liệu trước khi cập nhật code.
2. Pull bản cập nhật rồi restart Python để nạp module mới.
3. Nạp lại cấu hình, dùng đúng thư mục Drive, `persistent_root` và range của dataset đang tiếp tục.
4. Chạy resume. Không cần reset progress/journal hoặc chuyển đổi dataset.
5. Paper đã `failed` vẫn cần retry riêng; resume không tự reset chúng. Giữ nguyên cooldown đã lưu.
