# Bàn giao M2 cho repo book

Ngày 2026-11-07. Repo book: <https://github.com/NewB-S2deeC0de/arXiv-report>, bản nền `7777026`. Website đang công bố: <https://newb-s2deec0de.github.io/arXiv-report/>.

`m2-book.patch` bổ sung ba trang methods/validation/results, cập nhật overview và mục lục MyST. Patch là bản bàn giao đầy đủ; không cần thư mục `.work/` của người tạo patch để apply. Patch không chứa corpus, build output hoặc số benchmark giả. Trang results ghi rõ còn thiếu runtime người dùng.

## Apply vào repo report

Trong **repo arXiv-report sạch**, kiểm tra và apply patch bằng đường dẫn thật tới file trong repo code:

```powershell
$bookPatch = 'C:/path/to/arXiv/report/m2-book.patch' # đổi thành đường dẫn trên máy bạn
git apply --check $bookPatch
git apply $bookPatch
```

Nếu report đã có thay đổi mới gây conflict, đối chiếu từng file thay vì force/ghi đè. Patch đã được kiểm tra reverse-apply trên checkout local đã sửa. Bản gốc của M1 được giữ nguyên.

## Build đã kiểm chứng

Jupyter Book `2.1.7`, Node `24.15.0`, lệnh tương đương workflow Pages:

```powershell
$env:BASE_URL='/arXiv-report'
jupyter-book build --html
```

Đã build thành công **6 trang** với base URL đúng, kiểm tra HTML và payload của methods/validation/results. Full suite code M2 trên Linux: **676 passed, 608 subtests passed**, không còn XFAIL. Chưa push hoặc deploy lên website; người giữ repo review rồi đưa thay đổi qua quy trình GitHub của nhóm.

## Dữ liệu và kết quả còn chờ

`drive-preflight.json` lưu kiểm kê tên thư mục công khai của ba runtime ngày 2026-11-07. Tổng 15.000 tên publication không đồng nghĩa 15.000 source thành công; chưa verify từng marker/checksum và chưa đo corpus. Người dùng xác nhận runtime của mình còn thiếu, vì vậy benchmark toàn nhóm chưa thể thực hiện.

Sau khi thêm runtime cuối và chạy notebook Colab CPU trên snapshot đủ nhóm, sinh bảng vào repo report:

```bash
PYTHONPATH=src python -m parser.benchmark_report --run /path/to/official-run --out /path/to/arXiv-report/m2/results.md
```

Review coverage, validation/lỗi, đơn vị metric và nguồn artifact trước khi build lại. Không dùng run `group_data_complete=false` làm kết quả chính thức; không đổi số test thành số benchmark.
