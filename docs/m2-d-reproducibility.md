# M2 — Quy trình test, benchmark và báo cáo

## Cài trong WSL/Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r src/requirements.txt
python -m pytest -q
```

Không dùng virtualenv Windows trong WSL. Bộ dependencies dùng ranges; benchmark lưu phiên bản thực tế. Xem `m2-d1-acceptance-matrix.md` và `m2-d-contract-decisions.md` trước khi diễn giải kết quả.

## Benchmark Colab CPU-only

Chọn CPU trong Colab, cài dependency của đúng commit và xác nhận dataset M1. Dữ liệu phải nằm ngoài Git; mọi path được truyền qua tham số. Chạy từ project root:

```bash
PYTHONPATH=src python -m parser.benchmark --persistent-root /path/to/m1-runtime --out /path/to/new-benchmark-run --limit 10
```

Đổi hai đường dẫn theo môi trường. Không cần sửa code. Không truyền limit khi chạy toàn bộ input. `--include-partial` thay đổi policy và phải ghi nhận khi so sánh. Mỗi run dùng output mới. Đo sample trước, sau đó sample chính thức; không tự tải dataset trong benchmark.

Output: environment.json (commit, dirty status, Python/packages/hardware/config), resources.csv (RSS samples), summary.json và parsed/ (output C3 với fingerprint/coverage). Khi exception, lưu telemetry và trả exit code lỗi; không tạo kết quả thành công giả. Kiểm tra hardware trong environment và xác nhận thủ công runtime CPU-only. Chạy suite D1 trên cùng commit, lưu log riêng bên cạnh benchmark. Pending/failed hoặc D1 regression phải được báo blocker.

Hiện default services chưa có B đầy đủ; benchmark dữ liệu TeX thật có thể thất bại. Đây là blocker tích hợp, không phải số đo chất lượng parser. Không dùng fake services làm benchmark thật.

## Nội dung chương M2 cần đưa vào book M1

Tài liệu này là hướng dẫn nguồn, không tạo book thay thế. Khi có repo/path book M1, thêm chương vào mục lục hiện hữu và dùng build command của book đó.

1. Mô tả A1 root/includes và giới hạn static TeX; A2 hierarchy; A3 sentence/formula/figure/table.
2. Mô tả B1 normalization, B2 bibliography, B3 remap và C2 dedup theo contract được chốt; phân biệt raw với content, element với occurrence.
3. Đọc trực tiếp JSON/CSV benchmark để render bảng và biểu đồ lúc build. Không dán screenshot hoặc điền số ví dụ như kết quả thực nghiệm.
4. Báo publication/version coverage, no_tex/partial/pending/failed, counts trước/sau dedup, runtime/RAM/storage; giải thích mẫu số và null.
5. Nêu sampling limitations, missing data, static parsing limits và failures thực tế.
6. Build book, kiểm tra link/artifact và ghi commit + checksum input/metrics. Dataset/output lớn không commit.

Chưa có run Colab thật, dữ liệu benchmark hoặc nguồn book M1 trong checkout. D2/D3 chỉ có khung triển khai, chưa đạt acceptance cuối.

## Git sau khi kiểm tra code

Trên nhánh D, kiểm tra file trước khi commit; không add dataset/runtime/benchmark output:

```bash
git status -sb
git diff --check
git diff
git add src/parser/benchmark.py tests/test_d1_acceptance.py tests/test_d2_benchmark.py docs/m2-d-contract-decisions.md docs/m2-d1-acceptance-matrix.md docs/m2-d-reproducibility.md README.md
git commit -m "test: add M2 acceptance checks and benchmark scaffold"
git push -u origin M2-D1-D2-D3
```

Mở PR từ M2-D1-D2-D3 vào main, ghi rõ test chạy ở đâu, strict xfail, blocker B/Colab/book và giới hạn scope. Không đóng D1–D3 trước khi các acceptance tương ứng đạt. `-u` thiết lập upstream riêng của nhánh D; sau đó dùng `git push`. Trước mỗi đợt cập nhật, kiểm tra working tree sạch, `git fetch origin`, rồi `git merge origin/main` nếu cần đồng bộ; giải quyết conflict, chạy test lại rồi commit/push. Không push thẳng main.
