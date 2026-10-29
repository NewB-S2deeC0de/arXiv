# Giải thích các tệp trong `runtime/`

`runtime/` chứa dữ liệu crawler đã thu thập, tiến độ để resume và số liệu phục vụ
báo cáo. Thư mục này độc lập với `code/`: cập nhật code không làm mất tiến độ nếu
vẫn chọn đúng project root và dùng lại dataset cũ.

Các đường dẫn dưới đây tương đối với project root đã chọn trong Colab.

## 1. Tổng quan

```text
runtime/
├── progress/       Tiến độ, attempts, cooldown và bản dự phòng
├── data/           Metadata, references và nguồn TeX/Bib
├── temp/           Vùng tải/giải nén tạm nếu scratch đặt trên Drive
├── logs/           Nhật ký từng phiên chạy
├── metrics/        Thời gian, RAM, dung lượng và thống kê
├── exports/        Dataset và thống kê đã xuất
└── writer.lock     File dùng để khóa việc ghi dataset
```

Một số thư mục chỉ xuất hiện sau khi chạy chức năng tương ứng. Khi notebook dùng
SSD tạm, scratch có thể là project path `scratch/` ngoài `runtime/`, trỏ qua
symlink đến đĩa tạm của runtime Colab.

Một **paper** là một arXiv ID, ví dụ `2504.02525`. Một **version** là phiên bản
cụ thể, ví dụ `2504.02525v2`. Một **run** là một lần gọi pipeline, có `run_id`
riêng; nhiều run có thể tiếp tục cùng dataset.

## 2. `progress/`: trạng thái công việc và phục hồi

| File/thư mục | Ý nghĩa |
|---|---|
| `jobs.csv` | Snapshot công việc discovery, metadata phiên bản, source và từng trang references. |
| `jobs.journal.jsonl` | Các cập nhật progress, có sequence và checksum theo chuỗi; được replay khi resume. |
| `jobs.csv.json` | Checksum SHA-256, số dòng và journal sequence tương ứng với snapshot chính. |
| `jobs.bak` | Snapshot dự phòng trước đó của `jobs.csv`. |
| `jobs.bak.json` | Checksum, số dòng và journal sequence của snapshot dự phòng. |
| `papers.csv` | Bảng tổng hợp tiến độ theo bài. |
| `versions.csv` | Bảng tổng hợp tiến độ theo phiên bản. |
| `services.json` | Cooldown từng dịch vụ, backoff 429 và lịch request cho limiter anonymous S2. |
| `active_run.json` | Marker phiên ghi dataset đã bắt đầu; được xóa khi thoát bình thường. |
| `backups/` | Bản trước của các bảng tổng hợp `papers.csv` và `versions.csv`. |
| `takeovers/` | Audit khi chủ động tiếp quản marker phiên cũ bằng `takeover_stale=True`. |

### Snapshot và journal phối hợp thế nào?

Trạng thái mới nhất được dựng từ:

```text
jobs.csv hợp lệ (hoặc jobs.bak)
        + các record journal mới hơn sequence của snapshot
        = progress hiện tại
```

Journal vẫn giữ cả các record lịch sử để backup cũ có thể phục hồi. Không tự xóa
những record tưởng là đã nằm trong CSV.

Trước khi gửi request, crawler tăng attempts và ghi bền vững mutation vào journal.
Khi batch nhiều ID, cả nhóm được checkpoint trong một mutation. Snapshot CSV và
các view được cập nhật theo đợt, khi reconcile và cuối phiên; chúng không nhất
thiết phản ánh ngay mọi thay đổi trong lúc crawler đang chạy.

Mỗi record journal là một dòng JSON. Dòng cuối thiếu newline do crash được coi
là chưa commit; record hoàn chỉnh sai checksum/sequence làm recovery dừng thay
vì âm thầm bỏ mất attempts. Độ bền thực tế còn phụ thuộc Drive/FUSE; fsync và đọc
lại không biến mounted Drive thành một filesystem transaction nhiều file.

### Cách đọc `papers.csv`

Mỗi dòng tương ứng một bài. Các nhóm cột chính:

- `arxiv_id`, `order_index`: ID và vị trí trong range đầu vào.
- `discovery_status`, `latest_version`: kết quả tra cứu arXiv và phiên bản mới
  nhất tại snapshot discovery.
- `references_status`, `reference_count`: trạng thái snapshot references và số
  references đã chuẩn hóa khi có kết quả hoàn chỉnh.
- Các cột attempts/cycle, error, thời gian và run ID: lịch sử xử lý và lỗi.

`reference_count` không phải số citation mà bài nhận được. Các mục
`citedPaper=null` được đếm riêng là unresolved trong snapshot references, nên
không tự diễn giải reference_count là độ đầy đủ bibliography thực tế.

### Cách đọc `versions.csv`

Mỗi dòng có khóa `(arxiv_id, version)`, ví dụ `(2504.02525, 2)`.

| Nhóm cột | Ý nghĩa |
|---|---|
| `metadata_status` | Kết quả lấy metadata của đúng phiên bản đó. |
| `source_status` | Kết quả tải và kiểm tra nguồn của phiên bản đó. |
| `*_attempts_total` | Tổng attempts, không reset khi restart hoặc mở chu kỳ retry. |
| `*_cycle_attempts` | Attempts trong chu kỳ hiện tại; reset khi chủ động mở chu kỳ mới. |
| `*_cycle_id` | Số chu kỳ xử lý. |
| `*_error_code`, `*_error_message` | Thông tin lỗi nếu có. |
| `updated_at`, `last_run_id` | Thông tin cập nhật snapshot. |

Ví dụ smoke đã quan sát có 10 bài và 14 phiên bản: `versions.csv` có 14 dòng dữ
liệu, không phải 10 dòng. Metadata và source là hai trạng thái độc lập: source
thành công không tự chứng minh metadata cũng thành công.

### Các trạng thái

| Trạng thái | Ý nghĩa |
|---|---|
| `pending` | Chưa làm hoặc đủ điều kiện tiếp tục sau recovery. |
| `running` | Đang xử lý, có thể đang đợi retry/cooldown. |
| `succeeded` | Công việc hoàn tất thành công. |
| `no_tex` | Nguồn hợp lệ nhưng không có TeX; chỉ dùng cho source. |
| `not_found` | Không tìm thấy tài nguyên theo kết quả đã xử lý. |
| `failed` | Thất bại trong chu kỳ hiện tại; resume thường không tự thử lại. |

References có cả row cấp bài và row cấp trang trong ledger. Retry được tính
riêng cho từng trang; tổng requests của bài nhiều trang có thể lớn hơn ba.
Dùng `status_counts()` để đếm references theo bài, không đếm trực tiếp mọi row
references trong `jobs.csv`.

Muốn xem trạng thái hiện tại trên Colab:

```python
from crawler.progress import ProgressStore

store = ProgressStore(config.paths['persistent_root'])
print(store.status_counts())
```

## 3. `data/`: dữ liệu đã thu thập

```text
data/2504.02525/
├── discovery/
├── references/
├── reference-staging/
├── versions/
│   ├── v1/
│   │   ├── metadata/
│   │   └── source/
│   └── v2/
│       ├── metadata/
│       └── source/
└── history/
```

Tên thư mục paper được percent-encode nếu ID chứa ký tự đặc biệt như `/` ở dạng
arXiv cũ. Canonical ID trong CSV/JSON không bị đổi thành tên thư mục đó.

### `discovery/`

| File | Nội dung |
|---|---|
| `response.xml` | Response arXiv gốc để đối chiếu. Khi batching, XML có thể chứa nhiều bài. |
| `request.json` | Danh sách ID và max_results của batch đã yêu cầu. |
| `normalized.json` | Metadata chuẩn hóa và số phiên bản mới nhất; hoặc kết quả not_found. |
| `complete.json` | Marker publication và checksums của kết quả discovery. |

Discovery là snapshot đóng băng. Resume thường không tự truy vấn lại latest
version. Refresh discovery là thao tác chủ động và giữ lịch sử cũ.

### `versions/vN/metadata/`

Chứa `response.xml`, `request.json`, `normalized.json` và `complete.json` ở bản
crawler hiện tại. Kết quả từ bản cũ có thể chưa có `request.json`.

Metadata được gắn đúng `IDvN`: title, authors, abstract, categories, published,
updated, journal_ref, DOI và venue nullable. Không sao chép ngày updated của
latest version cho mọi phiên bản. Các trường thiếu giữ null.

### `versions/vN/source/`

| File/thư mục | Nội dung |
|---|---|
| `tex/` | Các file regular `.tex` và `.bib`, giữ byte và relative nesting. |
| `inventory.json` | Số TeX/Bib, bytes nén/giải nén/giữ lại, số file loại bỏ và checksum archive. |
| `response.json` | Thông tin HTTP/download: content type/encoding/length, bytes_received, checksum, URL và thời gian; tùy phiên bản code đã tạo dữ liệu. |
| `complete.json` | Marker của source đã validate và publish. |

Ảnh và binary không được giữ trong `tex/`, nhưng lệnh như `includegraphics`
trong TeX vẫn được giữ nguyên. Không có `.bib` không đồng nghĩa source lỗi.
Có Bib mà không có TeX có thể mang trạng thái `no_tex`.

`compressed_bytes`, `unpacked_bytes` và `kept_bytes` là ba phép đo khác nhau.
Không lấy compressed bytes trừ kept bytes rồi gọi đó là kích thước ảnh đã bỏ.

### `references/` và `reference-staging/`

`reference-staging/<query-fingerprint>/` giữ các file như:

```text
page-000000.json
page-000000.complete.json
page-000100.json
page-000100.complete.json
```

Raw page lưu phản hồi API; marker page lưu checksum và fingerprint. Offset tiếp
theo lấy từ trường `next` của API, không mặc định luôn tăng 100.

Khi toàn bộ chuỗi trang kết thúc hợp lệ, crawler publish snapshot vào
`references/`, gồm raw pages/marker liên quan, `normalized.json` và `complete.json`.

`normalized.json` giữ references đã chuẩn hóa, unresolved_count, fetched_at và
query fingerprint ở bản code hiện tại. Raw pages giữ cả reference thiếu arXiv ID
hoặc `citedPaper=null`. Snapshot references là cấp bài, không tự khẳng định nó
trùng bibliography của từng phiên bản TeX.

Page staging được giữ để resume không phải tải lại page hợp lệ. Không xóa
`reference-staging/` như cách dọn archive tạm nếu vẫn muốn giữ khả năng đó.

### `complete.json` xác nhận điều gì?

Marker chứa job key, trạng thái, checksums, thời gian captured_at và run_id ở
bản hiện tại. Crawler kiểm tra cả marker lẫn file được liệt kê trước khi dùng
kết quả để phục hồi hoặc export.

Có thư mục/file chưa đủ chứng minh thành công. File tải dở hoặc marker không
khớp checksum không được dùng như kết quả hoàn tất.

Cờ `complete` trong `response.json` chỉ mô tả lượt tải đã đọc hết stream, không
thay thế `complete.json`. Ví dụ gzip bị cắt nhưng HTTP stream kết thúc vẫn có
thể qua bước tải, sau đó bị gzip validation từ chối.

### `history/`

Giữ kết quả cũ khi refresh hoặc thay thế kết quả đã tồn tại. Đây là lịch sử dữ
liệu để đối chiếu; không tự coi mọi bản trong history là snapshot đang hoạt động.

## 4. `temp/` hoặc scratch SSD: công việc đang làm dở

Chứa archive `.download`, thông tin response tải xuống và thư mục giải nén trước
khi publish. Ở cấu hình mặc định nó nằm trong `runtime/temp/`; notebook SSD có
thể dùng project path `scratch/` thay thế.

- Archive và staging thành công được dọn sau publish; một số response sidecar
  hoặc dữ liệu từ phiên bản code cũ có thể vẫn còn.
- Staging thất bại có thể được giữ để chẩn đoán, như gzip bị cắt giữa chừng.
- Runtime Colab reset có thể làm mất scratch SSD. Source dở sẽ tải lại từ đầu;
  dữ liệu complete trên Drive và progress vẫn dùng để tiếp tục.
- Scratch không phải nguồn sự thật để kết luận job succeeded.

## 5. `logs/<run_id>/events.jsonl`: nhật ký sự kiện

Mỗi dòng là một JSON event: request start/end, retry, waiting/wait_end,
validation, commit hoặc recovery. Các trường gắn event với job và run.

Dùng log để trả lời: chờ ở dịch vụ nào, bài nào gặp 429, có bao nhiêu attempts,
request chạy lúc nào và publication mất bao lâu. Trong batch, event có danh sách
jobs để gắn request chung cho mọi ID tham gia.

Không cộng duration của hai nhánh đồng thời rồi gọi đó là wall time. Chẳng hạn
tổng `wait_end` có thể lớn hơn wall_seconds vì arXiv và S2 cùng chờ.

Logs được thiết kế không ghi API key, đầy đủ headers hoặc response body nhạy cảm.

## 6. `metrics/<run_id>/`: hiệu năng từng phiên

| File | Ý nghĩa |
|---|---|
| `environment.json` | Cấu hình, Python/packages, CPU, RAM/đĩa khả dụng và chế độ xác thực; không lưu API key. |
| `resources.csv` | Các mẫu elapsed time, UTC, RSS bytes và logical disk bytes. |
| `checkpoint.json` | Thống kê tạm để giữ phần thời gian đã đo nếu phiên chết trước summary. |
| `paper_metrics.csv` | Elapsed quan sát được của từng bài trong phiên và cờ terminal coverage. |
| `summary.json` | Tổng kết thời gian, peak/mean RAM, disk peak, counts, rates và provenance các run. |

Ý nghĩa một số trường summary:

| Trường | Ý nghĩa |
|---|---|
| `wall_seconds` | Thời gian thực tế của riêng phiên này. |
| `active_wall_seconds` | Tổng thời gian phiên hiện tại và phần thời gian đã đo của các phiên trước. |
| `calendar_elapsed_seconds` | Thời gian lịch từ phiên sớm nhất được ghi nhận đến hiện tại, gồm cả khoảng nghỉ. |
| `interrupted_run_ids` | Các phiên trước được ghi nhận là chưa kết thúc đầy đủ. |
| `event_seconds` | Tổng durations theo loại event; có thể chồng lấn giữa hai dịch vụ. |
| `status_counts` | Trạng thái dataset tại lúc tổng kết, có thể gồm kết quả từ các run trước. |
| `rates` | Các tỷ lệ kèm numerator/denominator, phải đọc đúng mẫu số. |
| `incomplete` | Phiên bị ngắt/thoát bất thường hay đã kết thúc bình thường. |

`incomplete=False` không có nghĩa mọi job thành công: phiên có thể kết thúc bình
thường với một số job failed. Cờ complete của paper metrics cũng là terminal
coverage, trong đó failed là một trạng thái terminal.

Peak RAM/disk là đỉnh quan sát được; sampling có thể bỏ lỡ spike. Disk counter
hiện dùng một baseline scan rồi cập nhật vùng thay đổi, đo logical file bytes,
không phải số block vật lý mà filesystem cấp phát.

Nhiều run folders là bình thường: mỗi lần gọi pipeline có một ID mới. Dữ liệu
paper dùng chung trong `data/`, còn logs/metrics giữ riêng lịch sử từng phiên.

## 7. `exports/<export_id>/`: snapshot để phân tích và báo cáo

Chỉ được tạo khi chạy export, không gọi API mạng để thu thập lại dữ liệu.

| File | Nội dung |
|---|---|
| `papers.parquet` | Bảng ID, title, authors, submission_date, revised_dates và venue. |
| `references.parquet` | Liên kết citing/cited có arXiv ID, từ snapshot complete hợp lệ, dedup theo cặp ID. |
| `paper_metrics.csv` | Trạng thái/attempts/reference count cấp bài tại lúc export. |
| `version_metrics.csv` | Trạng thái, attempts, mã lỗi và kích thước nguồn cấp phiên bản. |
| `run_metrics.json` | Summary/checkpoint của các phiên có provenance. |
| `reference_conflicts.json` | Audit reference trùng khóa nhưng khác nội dung. |
| `manifest.json` | Counts, coverage thiếu, mean với sample/missing counts, fingerprints và checksums. |

`papers.parquet` chỉ chứa các ID có discovery thành công và metadata hợp lệ;
ID không tồn tại hoặc discovery thất bại được đếm trong `manifest.json`
(`missing_metadata`) thay vì tạo dòng toàn null. `venue` dùng giá trị `venue`
nếu có, nếu không thì fallback sang `journal_ref`. `input_fingerprint` vẫn tính
trên toàn bộ ID đầu vào.

`references.parquet` chỉ giữ references có ref_arxiv_id; raw JSON vẫn giữ các
reference không có arXiv ID. Bảng export không phản ánh tất cả raw references.

Hai file cùng tên `paper_metrics.csv` có phạm vi khác nhau:

- Trong `metrics/<run_id>/`: elapsed của bài trong một phiên.
- Trong `exports/<export_id>/`: trạng thái/attempts/reference count tại snapshot.

## 8. `writer.lock`: khóa ghi dataset

File này có thể tồn tại và có kích thước 0 byte dù crawler đã dừng. Sự tồn tại
của file không tự chứng minh đang có process giữ lock.

Crawler dùng local filesystem lock kết hợp `active_run.json`. Đây không phải
distributed lock bảo đảm giữa hai máy/Colab runtimes khác nhau. Chỉ chạy một
writer trên cùng dataset. Takeover stale chỉ dùng khi chắc chắn phiên cũ đã dừng.

## 9. Giữ dữ liệu thế nào để resume đúng?

- Sao lưu hoặc chuyển tài khoản Drive bằng cách giữ nguyên cả `runtime/`.
- Đặc biệt giữ snapshot CSV, journal, checksum, backups và services.json cùng nhau.
- Không dùng code cũ không đọc journal trên dataset đã nâng cấp.
- Không sửa tay bảng progress để ép job thành công. Chỉnh view không tạo ra
  marker/source hợp lệ, và view có thể bị crawler ghi lại.
- Không xóa cooldown để gửi lại sớm hơn thời gian máy chủ yêu cầu.
- Không chỉnh tệp trong runtime khi crawler đang ghi cùng dataset.
- Nếu dọn archive tạm, chỉ làm khi phiên đã dừng và không cần file đó để chẩn đoán;
  source chưa complete có thể phải tải lại. Không nhầm với reference-staging.
- Giữ logs/metrics để phục vụ báo cáo; xóa chúng làm mất số liệu lịch sử.

Tóm lại theo mục đích sử dụng:

| Muốn làm gì? | Xem ở đâu? |
|---|---|
| Biết đã crawl được bao nhiêu | `status_counts()`, `papers.csv`, `versions.csv` |
| Tiếp tục sau runtime reset | Giữ nguyên progress + journal + cooldown và data complete |
| Đọc nội dung bài và TeX/Bib | `data/<paper>/` |
| Tìm nguyên nhân 429 hoặc lỗi tải | `logs/<run_id>/events.jsonl`, mã lỗi trong progress, archive tạm |
| Viết báo cáo thời gian/RAM/dung lượng | `metrics/` và CSV metrics trong export |
| Phân tích dataset đã chuẩn hóa | `exports/<export_id>/` |
