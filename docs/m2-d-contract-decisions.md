# Quyết định triển khai của luồng D

Trạng thái: quyết định nội bộ D; không thay thế contract B/C hoặc xác nhận toàn bộ M2 đã đạt acceptance.

| Mã | Quyết định | Lý do / giới hạn |
|---|---|---|
| D-01 | Fixture tổng hợp, ghi trong pytest `tmp_path`; không commit corpus | Test offline, tránh sửa dataset thật |
| D-02 | Test C3 với fake services chỉ chứng minh orchestration | Không được dùng làm bằng chứng parser thực chạy đầy đủ |
| D-03 | Các khoảng trống được ghi `xfail(strict=True)` với assertion thật | Khi hành vi được sửa, XPASS buộc cập nhật test và matrix |
| D-04 | Đề xuất hierarchy có `position` | Contract hiện tại mất thứ tự nguồn; cần C1/C2 chốt schema trước khi thay đổi |
| D-05 | Benchmark fresh run, output mới và nằm ngoài cây input M1 | Không trộn timing resume với fresh; không ghi lên dữ liệu nguồn |
| D-06 | Wall-clock bao gồm run_parser đọc/parse/dedup/export/validation; không gồm cài đặt | Thời gian/publication = wall / papers_processed, bao gồm publication đã xử lý thất bại/partial; run bị exception thì chưa báo mean |
| D-07 | RAM là RSS process hiện tại, lấy mẫu 0.25 giây mặc định | Không gồm child processes; peak quan sát có thể bỏ lỡ spike |
| D-08 | Storage là tổng logical bytes trong parsed/, gồm shard/checkpoint | Không phải filesystem allocation, không gồm input hoặc telemetry benchmark |
| D-09 | Unresolved/conflicts chưa có contract metrics thì để null | Không suy ra số 0 từ dữ liệu không được ghi |
| D-10 | Counts theo kind đọc bảng elements sau dedup | Là unique elements, không phải occurrences hoặc counts trước dedup |
| D-11 | Benchmark hoàn tất không đồng nghĩa acceptance pass | Pending/failed ghi blocked; các run khác vẫn cần review D1 |

Không tự định nghĩa lại bibliography merge, canonical key hoặc precedence B2/B3. Các điểm này giữ trạng thái chờ chốt trong `m2-d1-d2-d3-questions.md`. Chưa có số benchmark Colab thật và chưa có nguồn Jupyter Book M1 trong checkout.
