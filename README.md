# Milestone 1 arXiv crawler

Python >=3.11, Colab CPU; inclusive range `2504.02523`–`2504.07522`.
Metadata và source arXiv dùng chung scheduler nối tiếp; S2 có limiter riêng.
Mỗi request tối đa ba attempts, gồm cả 429; source validation dùng cùng budget.
Cooldown và attempts được lưu trước khi tiếp tục. Không tự retry failed khi resume.

## Kiến trúc references theo version

Xem [luồng worker, giới hạn queue, chuyển dữ liệu cũ và cách smoke](docs/versioned-reference-workers.md).
References mới truy vấn URL arXiv vN, lưu riêng từng version; snapshot v0 giữ nguyên.
Chưa kiểm chứng rằng S2 trả bibliography lịch sử tương ứng từng version.

## Kết quả smoke do người dùng cung cấp (bản paper-level cũ)

Bản đầu đã chạy 10 ID trên Colab: discovery 10, version metadata 14,
reference snapshots 10, source thành công 7 và thất bại 7.
Hai source thất bại do HTTP 429. Năm archive còn lại được người dùng kiểm tra:
`gzip.open()` đọc đến cuối đều báo EOFError (thiếu end-of-stream marker).
Đã xác nhận archive bị cắt; chưa xác định tầng gây cắt (server/network/Colab).
API key Semantic Scholar không thay đổi rate limit arXiv.

Bản cập nhật phân biệt `payload_gzip_truncated`, `payload_invalid_gzip`,
`payload_invalid_tar`, `transport_ReadTimeout` và các mã lỗi khác; lưu response
metadata cùng số bytes, checksum, content type/encoding/length. Không đánh dấu
archive hỏng là complete/no_tex. Chưa chạy live smoke cho bản cập nhật.

## Cài và kiểm thử offline

```bash
python -m pip install -r src/requirements.txt
PYTHONPATH=src python -m pytest -q
```

Dependency ranges chưa khóa theo Colab vì chưa thu thập package snapshot từ
smoke bản cập nhật. Mỗi run tự lưu phiên bản package trong environment.json.

## Dữ liệu và tương thích với bản đầu

Giải thích chi tiết từng file: [docs/runtime-files.md](docs/runtime-files.md).

- `runtime/progress/jobs.csv`: snapshot progress; `jobs.journal.jsonl` giữ các
  cập nhật mới hơn snapshot. Resume dùng cả hai, giữ tương thích dữ liệu cũ.
  Không xóa/sửa tay journal hoặc hạ code về bản không đọc journal.
- `papers.csv`, `versions.csv`: snapshots theo schema đặc tả, tạo tự động từ
  ledger khi checkpoint. `status_counts()` đếm references theo version, không cộng
  page rows; `legacy_references` tách các snapshot v0 cũ. Đây là lựa chọn tương thích: ledger giữ lịch sử retry chi tiết,
  hai snapshots là view tái tạo được, không phải ba nguồn trạng thái độc lập.
- `services.json`: cooldown và rolling-window S2 tồn tại qua restart.
- Kết quả chỉ hợp lệ khi marker/checksum khớp. Legacy marker vẫn được đọc;
  marker mới thêm UTC captured_at, run_id và số byte.
- `reference-staging/`: raw pages có checksum/fingerprint để resume giữa trang.
- `history/`: kết quả cũ được giữ khi chủ động refresh.
- `metrics/<run_id>/`: environment, samples RAM/disk, paper elapsed và summary.
- `logs/<run_id>/events.jsonl`: request, retry, wait heartbeat, commit, recovery.
- `exports/<export_id>/`: Parquet, paper/version metrics CSV, run metrics,
  reference conflict audit và manifest checksums/fingerprints.

Mọi project path là tương đối với root đang làm việc. CSV UTF-8 dùng quoting;
ô trống biểu thị thiếu dữ liệu. Dates thiếu giữ null, không suy ngày từ year.
RAM trung bình có trọng số thời gian; peak RAM/disk là peak quan sát được,
sampling có thể bỏ lỡ spike. Các metrics cũ chưa đo không được dựng lại giả.

## Cập nhật Colab đang có dữ liệu

1. Push bản code cập nhật lên GitHub, rồi mở `notebooks/crawl_colab.ipynb`.
2. Chọn CPU, mount Drive bằng giao diện Files → Mount Drive.
3. Chọn đúng project root đã dùng ở smoke cũ. Không tạo runtime mới.
4. Chạy cell clone/pull và cài dependency. Nếu đã import crawler trước khi pull,
   restart runtime, mount Drive lại rồi chạy lại các cell để dùng code mới.
5. Giữ secret `SEMANTIC_SCHOLAR_API_KEY` trong Colab Secrets, bật Notebook access.
   Cell authentication chỉ in `key` hoặc `anonymous`. Không dán key vào code.
6. Chạy status/diagnostics. `diagnose_sources(config)` chỉ đọc file tạm, không gọi API.
7. Bật cell scratch SSD, rồi bật `RUN_SMOKE=True` tại cell smoke 10 ID.
   Smoke resume các version của 10 ID đầu; references v0 cũ không thay thế vN.
8. Đặt lại toggle False, chạy Status; kiểm tra references version và lỗi 429.
9. Stop một lần, chờ drain worker rồi resume smoke; chỉ bật takeover khi runtime
   cũ đã dừng và còn active marker. Không reset attempts/cooldown.
10. Khi smoke đạt, chọn một cell toàn range, arXiv-only hoặc S2-only. Failed jobs
    cần chủ động retry bằng CLI, không tự được chạy lại khi resume.

Config mẫu yêu cầu key (`allow_anonymous_s2: false`). Chỉ thử anonymous khi chủ động
bật `execution.allow_anonymous_s2: true`; không dùng cấu hình đó cho full crawl.
Env `SEMANTIC_SCHOLAR_API_KEY` ưu tiên hơn Secrets. 401/403 dừng dịch vụ tương ứng,
không tự hạ xuống anonymous. Anonymous pacing là chính sách bảo thủ của dự án.

## CLI từ project root

```bash
PYTHONPATH=src python -m crawler.cli init
PYTHONPATH=src python -m crawler.cli run --limit 10
PYTHONPATH=src python -m crawler.cli status
PYTHONPATH=src python -m crawler.cli diagnose
PYTHONPATH=src python -m crawler.cli retry-failed --limit 10 --stages arxiv
PYTHONPATH=src python -m crawler.cli resume --limit 10 --stages arxiv
PYTHONPATH=src python -m crawler.cli export --run-id export-001
```

CLI `retry-failed` chỉ reset failed stages của service được chọn, không gọi mạng.
Notebook có chế độ chỉ arXiv hoặc chỉ S2. `recheck-not-found` riêng cho not_found.
`refresh-discovery --limit 10` và `refresh-references --limit 10` mở snapshot mới,
giữ history; không tự refresh khi resume. `run`/`resume` không có `--limit` chọn
cả 5.000 ID. Notebook smoke luôn truyền 10; không auto chạy full range.

Các lệnh mutation/export dùng chung local lock và active marker. Local lock
không phải distributed lock trên Drive: chỉ một notebook/process ghi dataset.
Redirect chỉ được theo trên cùng HTTPS service host và vẫn đi qua pacing/budget;
redirect sang host khác ghi `redirect_disallowed`, không âm thầm dùng main site.

## API và phạm vi

- https://info.arxiv.org/help/api/user-manual.html
- https://info.arxiv.org/help/api/tou.html
- https://info.arxiv.org/help/mimetypes.html
- https://api.semanticscholar.org/api-docs/graphs
- https://www.python-httpx.org/quickstart/

Code repo và Drive runtime độc lập; parsing, matching, Jupyter Book, GitHub Pages,
upload dataset và report repo thuộc milestone sau. Chưa chạy toàn bộ 5.000 ID.

## Discovery batching

`network.discovery_batch_size` mặc định 50 (1–50), giới hạn thêm bởi
`execution.queue_capacity`. Pipeline luân phiên một batch discovery và một
version stage; metadata riêng từng version và source vẫn qua cùng scheduler.
Batch chỉ gom các ID đang pending/running có cùng cycle-attempt count. Mỗi
request tiêu thụ một attempt của từng ID tham gia, checkpoint cả nhóm một lần
trước khi gửi; không reset attempts khi chia/gom batch hoặc restart. Nhóm có
attempts khác nhau được xử lý riêng để mỗi ID giữ tối đa ba attempts/chu kỳ.

Atom error, ID ngoài batch, duplicate hoặc response có totalResults không khớp
không được biến thành not_found. Raw response và request ID list được lưu với
mỗi discovery result. Recovery tái tạo version rows từ marker nếu runtime chết
trước terminal checkpoint. Metrics ghi danh sách jobs của batch, chỉ cộng thời
gian request một lần.

`papers.csv`/`versions.csv` là views có thể trễ tối đa 49 terminal mutations;
Progress journal vẫn checkpoint mỗi request hoặc terminal batch; `jobs.csv`
được compact cùng các view theo đợt và lúc kết thúc/reconcile. Dùng CLI status hoặc
`status_counts()` khi đang chạy. Views được flush ở cuối phiên và reconcile.

Notebook duy nhất `crawl_colab.ipynb` có smoke 10 ID và cell toàn range,
mặc định `RUN_FULL_RANGE=False`. Muốn dùng SSD, notebook tạo
project path tương đối `scratch` là symlink đến thư mục tạm do hệ điều hành cấp;
config/log không lưu mount path hoặc đường dẫn SSD tuyệt đối. Khi runtime reset,
link hỏng được tạo lại, staging dở tải lại từ đầu, kết quả complete nằm trên Drive.
Sau cập nhật code cần restart kernel trước khi import lại crawler. Chỉ bật
`RUN_FULL_RANGE=True` sau khi smoke mới và interrupt/resume đã đạt.

## Tối ưu thời gian và I/O

- `network.metadata_batch_size: 50` gom các versioned ID còn cùng budget. Mapping
  theo chính xác `IDvN`, không dùng base ID làm khóa metadata phiên bản. Giá trị
  có thể hạ xuống 1 để kiểm chứng endpoint khi smoke. Source vẫn tải riêng.
- Full reconcile/checksum chỉ chạy lúc bắt đầu/resume. Trong phiên, mỗi result
  vừa publish được checksum/readback và cập nhật progress riêng; không quét lại
  toàn dataset sau mỗi metadata request.
- `jobs.journal.jsonl` append các row thay đổi, hash chain và fsync/readback trước
  request. CSV đầy đủ được compact mỗi đợt view (50 terminal mutations) và cuối
  phiên. Dòng cuối journal thiếu newline do crash bị bỏ qua; checksum/sequence
  sai ở record hoàn chỉnh làm dừng recovery. Backup CSV replay journal để giữ
  attempts mới hơn. Journal được giữ nguyên để backup cũ còn phục hồi được.
- Migrate tự động: snapshot jobs.csv cũ vẫn đọc được; journal mới được tạo khi
  có mutation. Khi sao lưu/chuyển dataset, phải mang cả thư mục progress. Không
  chạy bản crawler cũ trên dataset đã có journal vì bản cũ không đọc delta.
- Disk metrics khởi tạo không quét dataset, chỉ cập nhật file/subtree được quan sát
  (paper, progress và staging), không rglob toàn Drive ở mỗi request/sample.
  `disk_measurement_scope=session_touched_paths`: có thể gồm file cũ trong subtree
  được chạm tới; không phải tổng dataset hay số byte vừa tải.
  `metrics.reconcile_disk()` trả riêng tổng đo toàn bộ, không đổi bộ đếm phiên.
  Staging thành công được xóa sau publish, staging thất bại giữ để chẩn đoán.
- RAM/disk metrics vẫn là mẫu quan sát, không bảo đảm bắt được spike giữa hai
  mẫu. Các file nguồn cuối vẫn giữ cấu trúc TeX/Bib trên Drive; chưa đổi sang
  định dạng archive để tránh thay đổi hợp đồng dữ liệu. Vì vậy I/O nhiều file
  nhỏ và quota Drive vẫn cần được quan sát trong smoke dài hơn.

Ước lượng: nếu có 7.000 phiên bản và hai nhánh metadata gom được đầy batch,
khoảng nghỉ arXiv riêng cho 100 discovery + 140 metadata + 7.000 source requests
là khoảng 6 giờ. Đây chỉ là phép tính theo request count, chưa gồm tải/giải nén,
Drive publish, retry/cooldown hoặc thời gian mất do runtime reset. Không cam kết
xong trong một phiên Colab. Chưa có benchmark Colab thật cho bản tối ưu này.

Journal fsync file và đọc lại trước request; khi tạo journal còn thử fsync directory.
Nếu Drive/FUSE báo không hỗ trợ directory fsync, event `request_start` ghi
`journal_directory_sync=false`. Đây là checkpoint best-effort theo filesystem,
không cam kết độ bền như đĩa local khi mất điện/hỏng mount.

## Đổi range đầu vào trong Colab

Sửa `config/crawler.yaml` trong repo; bản clone trên Drive là
`code/config/crawler.yaml`. Ví dụ range hiện tại:

```yaml
range:
  start_month: '2025-04'
  start_id: 2523
  end_month: '2025-04'
  end_id: 7522
```

Nó sinh `2504.02523` đến `2504.07522`, inclusive: 5.000 ID. Thay tháng bằng
chuỗi `YYYY-MM` và thay số thứ tự bằng integer, không cần tự thêm số 0 đầu.
Hỗ trợ cùng tháng hoặc xuyên tháng/năm. Đây là tháng của arXiv ID, không phải
bộ lọc ngày/giờ published hoặc updated. Với range xuyên tháng, phải cung cấp
`monthly_end_ids` cho tháng đầu và từng tháng trung gian vì số thứ tự reset mỗi
tháng. Không mặc định tháng nào cũng có 99.999 bài.

Ví dụ minh họa cơ chế (giả sử tháng đầu đã được xác nhận kết thúc ở serial 10;
đây không phải số bài thật của tháng 2025-04):

```yaml
range:
  start_month: '2025-04'
  start_id: 8
  end_month: '2025-05'
  end_id: 3
  monthly_end_ids:
    '2025-04': 10
```

Kết quả: `2504.00008`–`2504.00010`, rồi `2505.00001`–`2505.00003` (6 ID).
Nếu có tháng trung gian, thêm mỗi tháng với serial cuối đã xác nhận. Tháng cuối
không cần mapping: nó kết thúc tại end_id. Mapping chỉ khai báo giới hạn; code
không tự tra API để chứng minh ID tồn tại. Dùng giới hạn theo đề/phân công hoặc
nguồn đã xác nhận của nhóm, không chép số minh họa vào range thực tế.

Range cùng tháng hiện tại không cần monthly_end_ids và giữ nguyên 5.000 ID.

Sau khi sửa YAML, chạy lại cell `load_config` và cell scratch nếu dùng SSD.
Notebook in ID đầu/cuối và tổng số để kiểm tra. Smoke vẫn giới hạn 10 ID đầu;
cell toàn range dùng `limit=None` theo range mới, không hardcode 5.000.

Nếu là dataset/range mới độc lập, dùng persistent root tương đối khác, ví dụ:

```yaml
paths:
  persistent_root: runtime_2505_sample
  scratch_root: runtime_2505_sample/temp
```

Không dùng cùng progress cho hai dataset độc lập: code hiện upsert ID mới,
không tự xóa ID cũ, nên status/export sẽ gồm cả các ID đã tồn tại trong root đó.
Nếu đang resume dataset hiện tại, giữ nguyên persistent_root và range.
Nên sửa cấu hình trong repo rồi commit/push để lần git pull sau không xung đột
với YAML sửa riêng trong bản clone trên Drive.

`notebooks/5000_crawl.ipynb` đã được thay bằng cell toàn range trong
`notebooks/crawl_colab.ipynb`; chỉ cần duy trì một notebook.

Hướng dẫn dừng, sao lưu và nâng cấp khi Drive chưa đồng bộ: [docs/safe-stop-and-upgrade.md](docs/safe-stop-and-upgrade.md).

### Resume và xác minh riêng

Resume chỉ phục hồi job pending/running đã tiêu hao lượt thử trong phạm vi
được chọn; không checksum lại toàn bộ kết quả thành công. Dữ liệu cũ giữ nguyên
trạng thái và được audit riêng. Kết quả mới có `verification.json` sau khi
publish và đối chiếu nội dung thành công. Receipt gắn với SHA-256 của manifest;
receipt không bảo đảm phát hiện sửa đổi âm thầm của nội dung sau lần xác minh.

Notebook có cell `RUN_AUDIT`: mặc định bỏ qua receipt hợp lệ, kiểm tra phần còn
lại và lưu sau từng kết quả. `FULL_AUDIT=True` bắt đầu/tiếp tục một lượt kiểm tra
lại toàn phạm vi, checkpoint tại `progress/audit.json`; sau khi hoàn tất, gọi
full lần nữa tạo lượt mới. Giữ nguyên phạm vi khi tiếp tục lượt bị ngắt. Audit
không gọi API, không reset trạng thái crawl, và dùng cùng DatasetLock nên phải
chạy khi crawler dừng. Invalid cần được xem xét riêng (kể cả not_found không
có payload/marker). File hỏng không được cấp dấu xác minh mới.

`TAKEOVER_STALE` mặc định False. Chỉ bật sau lỗi dấu khóa cũ khi đã dừng/restart
runtime cũ và chắc chắn không có runtime khác ghi dataset. Đóng tab hay mất
kết nối không đủ xác nhận. Takeover không dừng tiến trình khác; đổi về False
sau lần tiếp quản. Không xóa journal/cooldown để vượt lỗi.

Khởi động đọc snapshot/journal nhưng không quét baseline dung lượng. Các thao
tác đọc/ghi Drive khác vẫn có thể chậm. Các summary cũ có baseline toàn dataset
không được so sánh trực tiếp với disk peak mới có phạm vi session_touched_paths.

### Export M1 schema v2 và định dạng source

`papers.parquet` chỉ chứa publication trong range có discovery succeeded và
normalized metadata kiểm tra được checksum. ID pending/failed/not_found vẫn
có trong `paper_metrics.csv`, cùng cột `metadata_exported`; không đếm số ID
được giao như số publication thành công. Manifest có `assigned_ids`,
`missing_metadata`, `schema_version=2` và phạm vi kiểm tra toàn vẹn. Export cũ
không bị sửa; tạo run-id mới để xuất lại.

Venue được bổ sung từ `citedPaper.venue` trong các snapshot references S2 đã
lưu trong range, ghép bằng đúng ref_arxiv_id. `venue_source` ghi nguồn; giá trị
mâu thuẫn giữ null và được liệt kê trong `venue_conflicts` của manifest. Không
suy đoán venue từ DOI/journal_ref, không gọi thêm API. Vì vậy paper chưa được
nhắc tới trong các snapshot có thể vẫn thiếu venue. Metadata arXiv raw được
giữ nguyên; enrichment chỉ diễn ra lúc export, áp dụng cả dữ liệu cũ.

Export kiểm tra checksum các JSON thực sự sử dụng và cache trong một lần
chạy, không đọc lại mọi file TeX/Bib. Đây không phải full source audit; dùng
cell audit riêng nếu cần kiểm tra toàn bộ source. `ref_submission_date` có
nguồn S2 publicationDate, không khẳng định là ngày nộp arXiv. References là
snapshot cấp paper, không phải bibliography từng version.

DOCX độc lập được nhận diện qua cấu trúc OOXML, XML và CRC của ZIP, giữ các
giới hạn kích thước; hợp lệ thì no_tex với inventory. HTML/ZIP không nhận diện
được và payload hỏng vẫn là lỗi, không tự biến thành no_tex. Chỉ giữ .tex/.bib;
không convert Word, .bbl hay parsing bibliography ở M1. Các archive chứa
member nguy hiểm, tên trùng, link hoặc vượt giới hạn vẫn bị từ chối theo đặc tả.


## S2 throughput investigation and update

S2 429 fallback backoff previously doubled across papers without a ceiling:
60, 120, 240, 480, 960, 1920, 3840, 7680 seconds. This can make a long
session mostly wait, even though each page has only three attempts. The new
`network.s2_max_backoff_seconds` defaults to 300 and bounds only the computed
fallback; server Retry-After and already persisted cooldown deadlines remain
respected. Updating code does not erase an existing long cooldown.

S2 now paces request starts (1.1 seconds with key by default), so response
latency does not get an additional full gap appended. Drive result publication
(copy/checksum/readback) runs in joined worker threads, allowing the other
service and RAM sampler to run while a large source is published. Cancellation
waits for publication before releasing the dataset lock. Reference disk accounting
only scans the references result, rather than rescanning the paper's TeX tree.
Request attempts are still durably checkpointed before network I/O.

After stopping the previous crawler, pull the update and restart Python before
importing it. Keep the existing persistent root, range, journal and services.json.
Resume skips terminal jobs; explicitly retry failed S2 jobs when ready:

```bash
PYTHONPATH=src python -m crawler.cli diagnose-references
PYTHONPATH=src python -m crawler.cli retry-failed --stages s2
PYTHONPATH=src python -m crawler.cli resume --stages s2
```

In Colab, after loading config, diagnostics can be called directly:

```python
from crawler.diagnostics import diagnose_references
print(diagnose_references(config))
```

Diagnostics read existing event logs and report HTTP counts (especially 429),
request time, wait time and remaining persisted cooldown. New logs also record
completed version snapshots and the actual number of reference entries. A
references succeeded count in status means versions with a complete snapshot,
not the number of referenced publications. These counters must not be confused.
No local dataset/run logs were available during this investigation, so the
specific cause of an existing Colab session's slowdown requires its diagnostics.
This patch has no live Colab throughput benchmark.


Details of the reference-page recovery and S2 dispatch-pacing update:
[change report](docs/reference-resume-and-s2-pacing.md).
