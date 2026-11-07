# D — Trạng thái và phần còn lại

Cập nhật 2026-11-07. Đã fetch `main@dbfe53f` và merge vào nhánh D tại `c370c20`, giữ các thay đổi D trước đó. Phản hồi B1 xác nhận commit `2e26c19` bảo toàn thứ tự children/root, sentence lặp và child_path; không cần sửa B1.

## D-01 — Đã xử lý trong code và kiểm chứng

- Thêm `position:int64` không null vào hierarchy, 0-based theo `(arxiv_id, version, parent_id)`, gồm children của root. Ghi khi duyệt cây, không suy từ ID/hàng Parquet.
- Validator kiểm tra sai kiểu/âm/thiếu, vị trí trùng và không liên tục. Repeated content trong version giữ các occurrence theo policy element hiện tại.
- Parser `1.1.0`, checkpoint schema `3`, manifest schema `2`; đổi schema fingerprint và invalidation cache cũ.
- Acceptance hai version đảo root/nested siblings và sentence lặp, trộn thứ tự hàng đọc từ Parquet rồi dựng lại cây giống finalized audit; checkpoint schema cũ/shard thiếu position được parse lại.
- Đã bỏ XFAIL BC-04. Full suite Linux WSL Ubuntu 24.04/Python 3.12.3: **676 passed, 608 subtests passed**, không có XFAIL.

Các thay đổi D mới đang ở working tree để review, chưa push. Không còn yêu cầu chờ B/C bổ sung position; cần review và đưa phần sửa này lên nhánh bàn giao khi nhóm sẵn sàng.

## D-02/D-03 — Còn thiếu runtime của người dùng và run Colab chính thức

[Drive nhóm](https://drive.google.com/drive/folders/1mNYmJH0l19l5bFYddKB-S82CLgtOxe5t) đã truy cập được. Kiểm kê cấp thư mục ngày 2026-11-07:

| Runtime | Số thư mục tên publication | Khoảng ID quan sát |
|---|---:|---|
| runtime | 5.000 | 2504.02523–2504.07522 |
| runtime-hieu | 5.000 | 2411.18799–2412.03847 |
| runtime-kien | 5.000 | 2410.23025–2411.03798 |

Đây là counts thư mục, không phải số source thành công/parsed; chưa kiểm marker/checksum từng source. Người dùng xác nhận **runtime còn thiếu là của mình**. Vì vậy chưa có đầu vào đủ bốn thành viên và chưa có benchmark Colab toàn nhóm; không điền số giả hoặc gọi ba runtime là toàn nhóm.

Notebook `notebooks/benchmark_colab.ipynb` đã cấu hình ba tên runtime, link Drive và chế độ bộ phận. Cần đặt shortcut/đường dẫn MyDrive đúng vị trí thực. `OFFICIAL_RUN=False`, `GROUP_DATA_COMPLETE=False`; run bộ phận có nhãn `blocked_incomplete_group_dataset`. Không cần download toàn corpus về Git/máy Windows.

**Người dùng/người gom dữ liệu cần làm:** tải runtime cuối lên cùng thư mục, xác nhận phạm vi đủ bốn thành viên, crawl dừng và Drive đồng bộ. Không ghi đè source/version khác nhau khi gom.

**D/người có quyền Colab cần làm:** thêm runtime thứ tư vào notebook (hoặc dùng runtime hợp nhất), xác nhận completeness/CPU, chạy suite cùng commit, snapshot mới rồi benchmark toàn bộ input không limit/resume. Chỉ dùng flag complete khi dữ liệu thực đã đủ; không đổi nhãn run cũ.

Giữ `summary.json`, `environment.json`, `resources.csv`, snapshot manifest, log test, manifest/coverage/audit và Parquet. Review failures, pending/partial/missing, validation và quality trước acceptance; completed không phải pass. Thời gian copy/checksum/cài dependency nằm ngoài timing; snapshot trên SSD, output Drive, sampled RSS chỉ process hiện tại.

## D-04 — Book đã có; chương M2 đã bổ sung và build local

- Book đang công bố: https://newb-s2deec0de.github.io/arXiv-report/
- Repo nguồn: https://github.com/NewB-S2deeC0de/arXiv-report, HEAD clone `7777026`.
- Bản checkout local: `.work/arxiv-report`; đã thêm `m2/methods.md`, `m2/validation.md`, `m2/results.md`, cập nhật overview và mục lục `myst.yml`.
- Kết quả build local bằng Jupyter Book `2.1.7`/Node `24.15.0`: thành công 6 trang. Trang results ghi đúng chưa có benchmark đủ nhóm, không dùng số test làm số corpus.
- Patch bàn giao vào repo book: `report/m2-book.patch`; hướng dẫn apply/build ở `report/README.md`. Chưa push repo book hoặc thay đổi website đã công bố.

**Nhóm trưởng/người giữ repo book:** review và apply patch, đưa thay đổi lên repo report qua quy trình nhóm để Pages build/deploy. Đây là xuất bản bản chương hiện có; phần bảng benchmark cuối vẫn phụ thuộc D-02/D-03.

**D sau run chính thức:** sinh `m2/results.md` bằng `parser.benchmark_report`, thêm phân tích lỗi/giới hạn và link artifact, build lại. Chỉ đóng phần thực nghiệm D2/D3 sau khi có run đủ bốn thành viên và book chứa đúng kết quả đó.
