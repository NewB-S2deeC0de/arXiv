# References theo version và các worker I/O

## Phạm vi

Crawler gửi `GET /graph/v1/paper/{identifier}/references`, trong đó identifier là
`URL:https://arxiv.org/abs/<ID>v<N>` được URL-encode. Không thêm bước POST batch
resolve paperId, không fallback sang ID gốc nếu URL version trả 404.
[Schema API chính thức](https://api.semanticscholar.org/graph/v1/swagger.json)
có định danh URL với ví dụ arXiv v1; page size tối đa 1000, mặc định crawler 100.

Snapshot lưu `requested_identifier`, `requested_version`, `query_fingerprint`,
`snapshot_id`, `content_sha256`, `page_provenance` và
`version_correspondence: unverified`. Cùng kết quả giữa v1/v2 vẫn được lưu riêng.
API chấp nhận URL vN chưa chứng minh kết quả là bibliography lịch sử của vN.
Chưa triển khai parsing/matching `.bib` hay kiểm định bibliography.
`fetched_at` của snapshot là thời điểm lắp ráp hoàn tất; từng page có thời điểm
thu nhận trong marker/provenance. Resume có thể ghép các trang từ nhiều phiên.

## Luồng xử lý

```text
arXiv scheduler chung: discovery / metadata / source
  → worker I/O arXiv: staging, copy, checksum, marker + receipt
  → progress writer: ghi journal, trả acknowledgement
  → request arXiv kế tiếp

S2 scheduler riêng: URL vN / pagination / retry
  → staging page trên scratch
  → queue publish có giới hạn số mục và bytes
  → publisher S2: I/O trong thread riêng, lưu page cache lên Drive
  → progress writer: ghi hoàn tất page; trang cuối publish snapshot rồi ghi hoàn tất version

Cả hai scheduler → progress writer → ghi attempt bền vững → mới gửi HTTP
```

Progress có **một** queue FIFO và một thread ghi. Sequence được cấp khi append
journal; không so timestamp giữa hai publisher. Các thao tác đọc trạng thái
trong pipeline cũng đi qua writer, trả bản sao khi cần để không duyệt dictionary
trong lúc thread khác sửa. `services.json` cũng ghi qua writer; HTTP và chờ
cooldown không chiếm worker đó. Telemetry có worker riêng. arXiv chưa có queue
prefetch kết quả; vẫn chờ publish trước request arXiv tiếp theo.

Hai publisher có thể ghi đồng thời ở các đường dẫn khác nhau. S2 không phải đợi
source arXiv copy xong. Tuy nhiên pre-send checkpoint, log/metrics, filesystem
và băng thông chung vẫn có độ trễ; kiến trúc này không bảo đảm đạt 1 request/giây.

## Giới hạn và backpressure

Trong `config/crawler.yaml`, nhóm `execution`:

| Biến | Mặc định | Phạm vi |
|---|---:|---|
| `progress_queue_capacity` | 100 | Cả thao tác progress đang xử lý và đang chờ |
| `s2_publish_queue_capacity` | 8 | Page đang publish và đang chờ acknowledgement |
| `s2_publish_queue_bytes` | 33554432 | Tổng raw bytes đã nhận vào queue, kể cả mục đang xử lý |
| `s2_response_max_bytes` | 8388608 | Raw JSON decoded của một response; quá giới hạn → failed/resource_limit |
| `s2_snapshot_max_bytes` | 67108864 | Tổng raw page bytes của một version |

Ngoài queue có thể tồn tại **một response** đang tải hoặc đang chờ admission.
Giới hạn này đo payload, không phải tổng RSS: Python objects, parsed JSON,
HTTP buffers và lúc lắp ráp snapshot vẫn cần RAM bổ sung. Queue giữ đường dẫn
file scratch cùng thông tin truy vấn, không giữ toàn bộ response của nhiều trang.
Một version quá giới hạn thành failed; không chờ vô hạn cho một mục quá lớn.
Queue đầy thì fetcher chờ; không bỏ dữ liệu hay tạo vô hạn thread.

Scratch mặc định có thể ở Drive; bật cell SSD trong notebook để staging thực sự
ở SSD Colab. Scratch SSD mất khi runtime bị xóa. Dữ liệu đã publish, cache page,
progress và cooldown vẫn nằm trong `persistent_root` trên Drive. Các file local
chưa được nhận vào queue có thể còn sau interruption; chúng không được coi là
kết quả hoàn tất. Không xóa scratch khi crawler đang chạy.

## Job, đường dẫn và chuyển dữ liệu cũ

Job references mới: `(paper, version>0, references, offset)`. Offset là vị trí
đầu trang; dùng đúng `next` API trả về, không tăng journal từng reference.
Discovery tạo version jobs. Với dữ liệu cũ, các version đã có metadata/source
trong ledger được bổ sung reference jobs; không quét toàn bộ source để làm việc này.
S2-only chỉ làm version đã biết; nếu chưa discovery thì chạy arXiv trước.

```text
runtime/data/<paper>/
  references/                          # snapshot cũ, version=0, giữ nguyên
  reference-staging/                   # cache cũ, không dùng cho query version
  versions/vN/
    metadata/
    source/
    reference-staging/<fingerprint>/   # page cache bền vững
    references/                        # raw pages, normalized, complete, verification
```

`status_counts()['references']` đếm **version snapshots**, không đếm page/ref.
`legacy_references` giữ thống kê v0 khi có jobs version mới. Trước lần chạy nâng
cấp đầu tiên, dataset chỉ có v0 vẫn hiển thị kiểu đếm cũ. Không cộng hai đơn vị.
Job v0 không được scheduler mới thực thi hay coi là thành công cho v1…vN.

`versions.csv` schema 2 có trạng thái references, attempts tổng các page,
current offset và reference_count. Count là view phụ; blank sau crash không có
nghĩa snapshot rỗng, payload là nguồn dữ liệu. Các cột references cũ trong
`papers.csv` tiếp tục mô tả **legacy paper**, có `references_scope=legacy_paper`;
`reference_versions_total/succeeded` cho biết tiến độ phiên bản mới.

## Ngắt phiên và phục hồi

- Có marker kết quả hợp lệ nhưng chưa ghi succeeded: recovery xác minh kết quả
  của job đang chạy rồi ghi succeeded; không cần scratch và không tải lại.
- Chỉ publish vài page: resume dùng cache có checksum/fingerprint hợp lệ rồi
  tiếp tục từ trang thiếu. Attempts mỗi page được giữ nguyên, tối đa ba/chu kỳ.
- Response chỉ nằm trên SSD/queue rồi runtime mất: tải lại trang thiếu nếu còn
  budget. Không có cơ chế cứu RAM/SSD đã mất; không reset attempts tự động.
- Publisher lỗi: báo lỗi, đánh thức producer đang bị backpressure; không treo
  chờ queue không bao giờ được giải phóng.
- Stop: ngừng producer, drain các mục đã nhận, chờ thread ghi xong trước khi
  nhả dataset lock. Do đó Stop có thể cần chờ Drive. Thread Python không thể
  cưỡng bức hủy một syscall FUSE bị kẹt; restart runtime khi thật sự cần sẽ dựa
  vào recovery ở lần sau. Không xóa lock/journal để né việc này.

`TAKEOVER_STALE=False` cho resume bình thường. Chỉ True khi có lỗi active marker
và đã xác nhận runtime cũ bị terminate/restart, không còn writer khác. True
không dừng phiên cũ, không đồng bộ Drive, không reset retry/cooldown. Sau lần
chạy tiếp quản đặt lại False. Không chạy nhiều runtime dùng cùng dataset hoặc
cùng API key; limiter ở một process không phối hợp với process khác.

S2 mặc định giãn start request 1.1 giây, cấu hình không cho dưới 1 giây.
Pagination, retries và mọi version đi qua cùng limiter. Fallback backoff giữ
trần 300 giây; `Retry-After` và deadline đã lưu có thể dài hơn và vẫn được tôn trọng.

## Export và kiểm tra Colab

Export schema 3:

- `references.parquet`: union cấp paper của các snapshot version và legacy,
  loại trùng theo `(arxiv_id, ref_arxiv_id)`; không suy đây là bibliography version.
- `version_references.parquet`: liên kết `(paper, version, ref_arxiv_id)` với
  snapshot_id, URL đã hỏi và nhãn unverified. Không gán v0 cho các version.
- `reference_snapshots.parquet`: ledger snapshot v0/vN, status, count và provenance;
  giữ cả pending/missing với count null, phân biệt với snapshot hợp lệ rỗng (0).
- `paper_metrics.csv`: count union (`reference_count_scope`), trạng thái references legacy (`references_status_scope`) và số version
  thành công/tổng; `version_metrics.csv`: thêm references_status cho version.

Dừng runtime cũ → pull → restart Python nếu đã import code → chọn đúng Drive
root → nạp config/key → bật scratch SSD → Status → smoke 10 ID → Status.
Smoke giới hạn **10 paper ID**, gồm mọi version đã discovery của chúng; số
request có thể lớn hơn 10 do version, pagination và retry. Sau nâng cấp,
references mới phải crawl dù 10 snapshot v0 trước đây đã thành công.

Chỉ chuyển toàn range hoặc S2-only sau khi kiểm tra `versions/vN/references/`,
status và log 429. Giữ cùng root/range để resume source/metadata cũ. Không chạy
lại audit toàn bộ chỉ để khởi động crawler. Không tự retry failed.
Bản này được kiểm thử offline, **chưa chạy Colab/API paper thật hoặc benchmark Drive**.

Sự kiện hiệu năng có `commit`, `publication_queue_wait` và `publication_queue` (peak items/bytes). Thời gian các worker chồng lấp, không cộng tất cả event_seconds để suy wall time. Snapshot hoàn chỉnh chỉ copy các page thuộc chuỗi `next` hiện hành; page cache cũ ngoài chuỗi vẫn là lịch sử, không trở thành raw page của snapshot mới.
