# Milestone 1 arXiv crawler

## Milestone 3 · E1 candidate pool

Candidate pool tạo tích Cartesian BibTeX × references trong từng publication.
Khóa query là `(arxiv_id, citation_key)`, khóa candidate là
`(arxiv_id, ref_arxiv_id)`. Bước này chưa gán nhãn hoặc chia partition.
Module độc lập với crawler/parser, có thể kiểm tra offline khi dữ liệu thật
đang được chuẩn bị.

Đầu vào là file Parquet hoặc thư mục shards của riêng từng bảng. BibTeX cần
`arxiv_id`, `citation_key`; references cần `arxiv_id`, `ref_arxiv_id`.
Metadata thiếu giữ null. Các trường dùng tiếp gồm title, authors, year, DOI,
journal, URL bên BibTeX và ref_title, ref_authors, ref_submission_date,
ref_s2_id, ref_doi bên references. Schema M1 hiện chưa xuất ref_doi, nên
trường này mặc định null. Các cột khác của đầu vào nằm ngoài projection này.

```powershell
$env:PYTHONPATH = 'src'
python -m matching.candidate_pool --bibtex runtime/parsed/bibtex.parquet --references runtime/exports/EXPORT_ID/references.parquet --out runtime/matching/candidate-pool-001
python -m pytest -q tests/test_matching_candidate_pool.py
```

Thay đường dẫn bằng output M1/M2 thực tế. Thư mục output phải là đường dẫn
mới. Candidate pool gồm các file sau.

| File | Nội dung |
|---|---|
| `entries.parquet` | Các query, gồm entry không có candidate |
| `references.parquet` | References có ID, metadata được giữ theo projection trên |
| `summary.parquet` | Số entry, reference và m×n theo publication |
| `candidate_pairs.parquet` | Các cặp với metadata prefix bib_/ref_ |
| `_SUCCESS` | Chỉ được tạo khi ghi xong mọi output |

Giữ những publication có BibTeX trong summary. `manual_eligible` chỉ xác
nhận có ít nhất 20 entry, cần người gán nhãn kiểm tra chất lượng trước khi
chọn. References không có ref_arxiv_id được loại và đếm trong thông báo CLI.
Row trùng hoàn toàn trong projection được gộp. Row cùng khóa nhưng metadata
mâu thuẫn làm dừng xử lý. Chương trình giữ nguyên identifier, từ chối ID
rỗng hoặc có khoảng trắng đầu/cuối. Không lọc candidate theo độ giống.

Entry không có candidate vẫn được giữ trong entries với số pair bằng 0.
Đây chưa phải nhãn no-match. Luồng gán nhãn cần xác nhận quyết định đó và
giữ riêng trạng thái chưa gán nhãn. Pair batches mặc định 65.536 row, có
thể đổi bằng `--batch-size`. Hai bảng đầu vào được nạp vào RAM, còn ma trận
pair được sinh và ghi từng batch. Output này phục vụ nội bộ E1.

E1 có công cụ manual labeling, heuristic matcher và partition
theo publication. Với nhóm 3 người, manual có ít nhất 15 publication × 20
entry, auto ít nhất max(6, ceil(10% pool ngoài manual)). Valid và test mỗi
tập có 3 manual + 3 auto publication. Các publication còn lại vào train.
Nhãn thủ công cần người thực hiện. Các policy DOI và nhiều ứng viên được
mô tả ở phần heuristic bên dưới; không tự biến entry chưa gán thành nhãn âm.

### Đợt dữ liệu thật ngày 24/11/2026

Dữ liệu được tải bằng GET từ Drive, chỉ xử lý bản sao cục bộ tại
`runtime/matching/real-20261124`. Không sửa dữ liệu trên Drive. Snapshot M1
là `export-8d75eb6e065244d98ac2d45d6c9bdecd`; M2 là bản recovery sau lỗi RAM.
`input/source_files.json` lưu file ID nguồn; `input_audit.json` lưu SHA256
và phạm vi kiểm tra. Các bảng M1 đã đối chiếu checksum với manifest nguồn.

| Kết quả thực tế | Số lượng |
|---|---:|
| Publication M1 | 5.000 |
| Entry BibTeX M2 | 196.178 |
| Publication có entry BibTeX | 2.885 |
| Reference có arXiv ID | 89.961 |
| Cặp ứng viên cùng publication | 5.600.665 |
| Publication đủ điều kiện chọn trong đợt này | 2.584 |
| Phân công manual tạm | 15 bài × 20 entry = 300 entry |
| Auto đã chạy | 499 bài, 31.808 entry, 919.235 cặp |
| Quyết định heuristic | 8.018 matched; 23.753 no_match; 37 needs_review |

`m2_recovery_check.json` ghi kiểm tra đọc toàn bộ bảng theo batch, khóa,
foreign key và số dòng theo checkpoint/coverage: đạt, với 3.176.600 elements,
4.028.754 hierarchy rows và 196.178 BibTeX rows. Đây là kiểm tra toàn vẹn
bản recovery, không phải nghiệm thu độ chính xác parse. Checkpoint vẫn ghi
766 version failed, 575 empty và 6.990 parsed trong tổng 8.357 version.
Không có manifest/errors/bibtex_audit gốc để khẳng định đầy đủ chất lượng.
Hierarchy thiếu position; E1 không dùng bảng này để dựng thứ tự nội dung.

`src/matching/dataset_plan.py` chọn mẫu bằng thứ hạng SHA256 với seed 42,
trước khi chạy matcher. Chỉ chọn publication có version được đối soát đủ,
không có lỗi/pending/conflict B2 trong checkpoint và snapshot S2 thành công
cho mọi version. Manual yêu cầu ít nhất 20 entry và có candidate; lấy ngẫu
nhiên tái lập 20 entry, không lọc theo confidence hay kết quả heuristic.
Auto lấy mọi entry của các bài đã chọn, kể cả candidate pool rỗng hợp lệ.
Các bài bị loại khỏi đợt chọn vẫn nằm trong corpus và mẫu số quota, cùng
lý do trong kế hoạch. Tiêu chí này thu hẹp tập chọn; không coi mẫu là đại
diện ngẫu nhiên không điều kiện của toàn bộ 5.000 bài.

```powershell
$env:PYTHONPATH = 'src'
python -m matching.dataset_plan --input runtime/matching/real-20261124/input --out runtime/matching/real-20261124/dataset_plan-new.json
```

Kế hoạch hiện tại trong `dataset_plan.json` có tên tạm member1–member3,
mỗi người 5 bài. Trong notebook, điền REVIEWER, chọn MEMBER và chạy các
cell lần lượt với `MODE='manual'`. Notebook chỉ mở đúng 100 entry được
phân công; `entry_keys` lọc giao diện và giữ fingerprint toàn snapshot.
Quyết định lưu tại `manual/member1.parquet` tương ứng. Đổi sang
`MODE='review'` để mở `auto/review_queue.parquet`, gồm 37 entry thuộc 16 bài;
tiến độ review lưu riêng. Chưa có nhãn manual nào được tạo thay người dùng.

Trong 300 entry manual dự kiến có 82 entry thiếu title và 99 thiếu authors
(các nhóm có thể chồng lấp). Cần đọc nguồn khi metadata không đủ kết luận;
dùng `s` để bỏ qua khi chưa xác minh, không chọn `n` chỉ vì thiếu trường.
Thiếu title nhận score 0 theo heuristic hiện tại, nên nhãn auto no_match
chưa chứng minh một tài liệu thực sự không có trong candidates. Cần đo
chất lượng bằng nhãn manual độc lập; có thể phải bổ sung ngữ cảnh nguồn
cho các entry khó trước khi đủ 300 nhãn xác nhận.

Kết quả lưu tại `candidate_pool/` và `auto/`. Writer auto gom batch tối đa
1.024 nhãn hoặc 8.192 evidence rows mỗi lần ghi, tránh một row group cho
mỗi entry. Đầu vào vẫn được nạp RAM; không nạp ma trận 5,6 triệu cặp cùng
lúc. Cả hai output có `_SUCCESS` khi ghi xong. 483 bài auto không còn
needs_review; 16 bài còn cần người kiểm tra. Đây chưa là đánh giá accuracy.

`readiness/validation_report.json` và `label_audit.parquet` được xuất thử;
status blocked là đúng vì chưa có manual và còn review. Không có
groundtruth.parquet hay partition cuối. Với 15 bài manual đã đủ nhãn,
quota tạm của dải này là ceil((5000-15)/10)=499 bài auto hoàn tất. Khi chưa
có manual, validator thấy M=0 và tính quota hiện thời là 500; đây không
phải lỗi chọn mẫu. Nhóm còn các dải khác nên phải ghép corpus, tính lại
quota, giữ publication manual/auto tách biệt và kiểm tra fingerprint khi
di chuyển nhãn sang snapshot mới. Chỉ chia train/valid/test và xuất ground
truth sau khi hoàn tất các bước này.

### Sử dụng notebook chung để gán nhãn

Mở `notebooks/labeling_tool.ipynb` trên Colab/Jupyter. Cả bốn người dùng
cùng notebook, **không sửa bất kỳ cell Python nào, không thêm ROOT hay
PROJECT_ROOT**. Cấu hình được nhập qua biểu mẫu trong
`src/matching/notebook_tool.py`; bảng do `labeling_table.py` hiển thị.

| Cell | Việc cần làm |
|---|---|
| 1 | Bấm chạy: mount Drive, chuẩn bị source M3_E1 và thư viện, import |
| 2 | Bấm chạy: mở biểu mẫu; chọn người/công việc/file, bấm Kiểm tra & mở bảng |
| 3 | Khi cần: đọc lại tiến độ đã lưu theo từng bài |
| 4 | Kết thúc: đóng bảng, chỉ ra file nhãn và kế hoạch cần bàn giao |

Người dùng chọn Hiếu/member1, Anh/member2, Kiên/member3 hoặc thành viên
thứ tư/member4. Mã reviewer được điền theo người thực sự chọn nhãn; người
thứ tư tự điền mã. Mọi ô đường dẫn đều để trống. Nút Chọn… duyệt từng
thư mục và phân biệt file/thư mục; có thể dán Copy path từ Files của
Colab. Không nhận link website Drive thay đường dẫn filesystem. Nơi
lưu nhãn cần quyền ghi và có thể tách khỏi dataset chỉ có quyền đọc.

Biểu mẫu có bốn công việc: bắt đầu manual trên dữ liệu riêng, tiếp tục
manual với kế hoạch gốc, review kết quả auto và demo. Công việc mới chỉ
cần BibTeX M2, references M1 và nơi ghi nhãn; tự tạo 5 bài × 20 entry và
lưu plans/memberX.json. Các lần sau dùng lại kế hoạch, không resample.
Có thể chọn 5 ID cụ thể, loại bài auto/đã giao người khác hoặc cung cấp
checkpoint M2/reference snapshots trong mục nâng cao để lọc thiếu/lỗi.
Không có artifact chất lượng sẽ hiện giới hạn kiểm tra; không suy luận
parse đúng chỉ từ số dòng. Chọn manual trước khi chạy auto và loại các
bài manual khỏi auto. Nhóm đối soát trùng publication khi gộp dữ liệu sau.

Người đã có nhãn cũ chọn Tiếp tục manual với kế hoạch gốc, chọn đúng
snapshot, JSON kế hoạch và thư mục cha chứa manual/ trước đây. Không tạo
mẫu mới thay cho phân công cũ. Review chọn đúng thư mục auto hoàn tất;
kiểm marker/schema/fingerprint rồi lấy needs_review từ auto_labels.parquet.
Không dùng nhầm queue cũ; progress tách theo review/<auto-run>/<reviewer>.parquet.

DOI index chỉ bổ sung hiển thị qua ref_arxiv_id, không đổi pool/fingerprint.
Bảng không dùng confidence, không chọn sẵn/rank theo matcher. Có tìm kiếm,
phân trang, mở rộng tác giả và link arXiv. Xem tất cả bỏ lọc; Không có match
bị khóa khi lọc. Chọn dòng lưu ngay rồi chuyển entry pending. Chưa chắc/ Tiếp
không tạo nhãn. Lỗi ghi không chuyển entry; callback bảng cũ không được
label entry mới. File nhãn CLI cũ vẫn đọc được. Một writer/file trên Drive.

Cấu hình được lưu tùy chọn tại notebook_profiles/memberX.json trong thư
mục nhãn. Lần sau chọn file cấu hình, Nạp cấu hình, kiểm tra rồi mở bảng;
không phải sửa cell. Chuyển người/data/công việc sẽ đóng bảng cũ trước khi
áp dụng. Profile là JSON dữ liệu, không chạy code; không chứa nhãn tự động.
Không đưa profile/dataset/labels lên GitHub.

Notebook giữ nhóm 4 người là bối cảnh nhưng chưa nghiệm thu toàn nhóm:
cần đối soát ít nhất 20 bài manual khác nhau và 400 nhãn, auto đủ quota,
review hoàn tất và chia tập members=4 ở bước tổng hợp. Mỗi người gửi file
progress và kế hoạch kèm snapshot dữ liệu. Không nối các snapshot khác nhau
rồi bỏ kiểm tra fingerprint.

CLI vẫn dùng được độc lập nếu muốn nhập số thay vì bảng notebook:

```powershell
$env:PYTHONPATH = 'src'
python -m matching.labeling --bibtex runtime/parsed/bibtex.parquet --references runtime/exports/EXPORT_ID/references.parquet --progress runtime/matching/manual_labels_member1.parquet --reviewer MEMBER1 --publication 2411.18799
```

Nhãn lưu ngay bằng file tạm rồi replace; cùng schema arxiv_id, citation_key,
groundtruth_arxiv_id, label_status, reviewer, updated_at. Match có một ID,
no-match có ID null, chưa gán không có row; chưa có partition ở bước này.

Fingerprint nằm trong metadata Parquet của progress, gắn với cả hai bảng
đầu vào đã chuẩn hóa. Khi input thay đổi, chương trình yêu cầu file progress
mới hoặc kiểm tra nhãn lại có chủ đích. Khóa `.lock` chặn hai phiên cùng ghi
một progress file. Nếu process chết và còn lock, chỉ xóa file lock sau khi
xác nhận phiên cũ đã dừng. Đặt progress trên Drive nếu muốn giữ qua reset
Colab. Atomic replace và khóa phụ thuộc filesystem, cần một writer trên
Drive và kiểm tra lại file sau khi kết nối mới.

M2 A1–A3 implementation: `src/parser/gather.py`, `hierarchy.py`, `leaves.py`.

### Hợp nhất nhãn, chia tập và xuất ground truth

`src/matching/groundtruth.py` nhận file progress manual của các thành viên,
thư mục output của auto matcher và bảng papers của toàn corpus đã chốt.
Fingerprint của mọi nguồn nhãn phải khớp candidate pool. Khóa nhãn là
`(arxiv_id, citation_key)`; ID đích phải nằm trong references của chính
publication đó. no_match được lưu dưới dạng null có trạng thái xác nhận,
còn entry chưa gán nhãn hoặc needs_review không trở thành nhãn âm.

Nhãn trùng và đồng ý được gộp, giữ các nguồn trong audit. Các quyết định
mâu thuẫn chặn xuất toàn bộ groundtruth cho đến khi giải quyết. Manual và
auto là hai pool publication tách biệt. Với ca auto needs_review, dùng công
cụ manual để ghi quyết định vào file review riêng, rồi truyền bằng
`--review`. File này chỉ chứa các key cần review; bỏ qua entry khác khi
gán nhãn. Review giải quyết trạng thái chưa có quyết định của auto, giữ
publication ở pool auto và giữ cả bằng chứng ban đầu. Review không được
ghi đè một quyết định matched/no_match đã có. Nhiều người review bất đồng
vẫn chặn xuất.

Mặc định nhóm có 3 thành viên. Validator kiểm tra ít nhất 15 publication
manual, mỗi publication có ít nhất 20 nhãn hợp lệ, tổng ít nhất 300 nhãn.
Mỗi thành viên phụ trách ít nhất 5 publication. Owner mặc định là reviewer
duy nhất của publication. Khi có nhiều reviewer trên cùng một publication,
truyền `--owners FILE.json` để chỉ định owner trong các reviewer đã tham gia,
ví dụ `{"2411.18799": "member1"}`. Một publication chỉ tính cho một owner.

Pool auto cần ít nhất `max(2*members, ceil((N-M)/10))` publication, với N
là số arxiv_id duy nhất trong toàn bộ bảng papers được truyền vào, M là
số publication manual. Cần dùng papers của toàn corpus, gồm cả các bài
chưa gán nhãn; không dùng riêng bảng papers của pool đã chọn. Mọi entry
của publication auto phải có quyết định cuối cùng. Manual chỉ cần đủ ngưỡng
entry theo đề, các entry chưa gán nhãn không có row groundtruth.

Chia theo publication, dùng seed 42. Mỗi valid/test nhận members publication
manual và members publication auto. Phần còn lại vào train. Thuật toán sắp
publication theo hash của seed/pool/ID, nên đổi thứ tự file đầu vào không
đổi kết quả. Danh sách chia tập được lưu vào publication_split.parquet.
Đây là file chia tập cần bàn giao để các luồng khác sử dụng cùng partition.

```powershell
$env:PYTHONPATH = 'src'
python -m matching.groundtruth --bibtex runtime/parsed/bibtex.parquet --references runtime/exports/EXPORT_ID/references.parquet --papers runtime/exports/EXPORT_ID/papers.parquet --manual runtime/matching/manual_member1.parquet --manual runtime/matching/manual_member2.parquet --manual runtime/matching/manual_member3.parquet --auto runtime/matching/auto-001 --members 3 --seed 42 --out runtime/matching
```

Có thể lặp `--auto` và `--review` để nhận nhiều nguồn. Nếu không còn nguồn
chứa quyết định sai, chạy lại khung trên với output mới để nghiệm thu.
Source nhãn sai cần được sửa tại nơi gán nhãn; công cụ merge giữ nguyên các
quyết định đầu vào để đối chiếu. Một file review không tự giải quyết bất
đồng giữa hai quyết định đã có.

Output khi đạt gồm groundtruth.parquet với đúng 4 cột arxiv_id, citation_key,
groundtruth_arxiv_id và partition; publication_split.parquet; label_audit.parquet;
validation_report.json và _SUCCESS. Partition có kiểu Arrow dictionary.
Các kiểm tra quota hoặc review chưa đạt tạo report và audit với status
blocked, không ghi groundtruth hay _SUCCESS. CLI trả exit code 2 khi blocked.
Input sai snapshot/schema/ID gây exception để sửa nguồn.

Output có thể nằm chung thư mục progress đã tồn tại, nhưng các tên file
kết quả phải chưa có để tránh ghi đè. Khóa ghi dùng chung cơ chế với công
cụ manual. Nếu export gián đoạn, giữ thư mục đó để kiểm tra và chọn output
mới. Trước khi dùng groundtruth cần kiểm tra _SUCCESS. File audit/report là
dữ liệu làm việc nội bộ, không thay thế schema predictions công bố của M3.

API Python gồm `prepare_groundtruth`, `split_publications` và
`write_groundtruth`. API prepare yêu cầu `publication_universe` đầy đủ để
kiểm tra mẫu số 10%. Các test dùng dữ liệu giả lập nhóm 3 người. Đợt dữ liệu
thật bên dưới kiểm tra khả năng xử lý và tính toàn vẹn; chất lượng nhãn vẫn
cần đánh giá bằng nhãn manual độc lập.

### Heuristic matcher E1

`src/matching/auto_matcher.py` gán quyết định cho từng entry trong các
publication được chọn, giữ nguyên candidate scope. Chọn publication bằng
`--publication`, loại pool manual bằng `--exclude-publication`; có thể lặp
các tham số này. Khi bỏ qua cả hai, matcher xử lý toàn bộ publication có
entry. Chọn tập publication để đáp ứng quota và chia partition thực hiện
ở bước riêng.

```powershell
$env:PYTHONPATH = 'src'
python -m matching.auto_matcher --bibtex runtime/parsed/bibtex.parquet --references runtime/exports/EXPORT_ID/references.parquet --out runtime/matching/auto-001
```

Thêm `--papers PATH` khi có bảng corpus chứa `arxiv_id` và `doi`. Matcher
ghép `references.ref_arxiv_id = papers.arxiv_id`, chỉ bổ sung DOI cho
candidate đã có trong cùng publication. Code export M1 hiện không xuất
cột DOI ở cả papers và references, nên chưa đủ nguồn để chạy rule DOI
từ hai bảng chuẩn đó. Bảng papers bổ sung có thể truyền riêng mà không
thay đổi schema công bố M1. Thiếu bảng/cột DOI được ghi vào summary.

Các quy tắc đã chốt được thực hiện như sau.

| Trường hợp | Quyết định |
|---|---|
| Hai DOI hợp lệ khớp | Candidate đạt rule DOI |
| Có đủ hai DOI nhưng khác nhau | Loại candidate khỏi match, ghi doi_mismatch; không fallback title |
| Ít nhất một phía thiếu DOI | Candidate đạt nếu title score ≥0,85 và có họ tác giả chung |
| Nhiều candidate đạt, DOI malformed hoặc nguồn DOI mâu thuẫn | needs_review, groundtruth null |
| Không có match; DOI khác nhưng title ≥0,85 và có họ tác giả chung | needs_review |
| DOI khác, không có cặp title–author đạt và không có bằng chứng cần review khác | no_match |
| Không có candidate đạt và không có bằng chứng DOI cần review | no_match, groundtruth null |

Match duy nhất được chấp nhận nếu không còn candidate có bằng chứng DOI
malformed/mâu thuẫn cần kiểm tra. Các candidate DOI khác nhau thông thường
không cản một match duy nhất hợp lệ. Mọi bằng chứng được lưu để review.
Không chọn tự động giữa một candidate đạt DOI và một candidate khác đạt
title–author; trường hợp này vẫn có nhiều candidate đạt nên cần review.

Policy `doi_or_title_surname_v2` giữ nguyên bằng chứng `doi_mismatch` ở mức
cặp, nhưng không đưa entry sang review chỉ vì DOI khác một candidate bất
kỳ. Khi chưa có match, chỉ review các cặp khác DOI nhưng title đạt 0,85
và có họ tác giả chung. Reason là `doi_mismatch_with_strong_title_author`;
review_candidates chỉ gồm những candidate đạt điều kiện này. DOI khác
không được tự chuyển thành match, kể cả DOI arXiv so với DOI tạp chí.
Malformed DOI, DOI nguồn mâu thuẫn và nhiều candidate đạt vẫn giữ policy
review trước đó. Summary lưu tên policy để phân biệt các lần chạy. Phải
chạy lại vào output mới và dựng review queue từ output mới; không tái sử
dụng file review của lần chạy cũ khi chưa đối soát từng quyết định.

DOI chuẩn hóa bằng bộ nhận diện literal của B2, hỗ trợ prefix doi: và URL
doi.org, sau đó casefold để so sánh. Giữ spelling gốc trong audit. Title
chuẩn hóa Unicode NFKD, bỏ dấu phụ, casefold, tách token chữ/số và sắp token.
Token sort ratio được định nghĩa bằng
`difflib.SequenceMatcher(..., autojunk=False).ratio()` trên hai chuỗi đã
sắp token, trong thang [0,1]. Đây là định nghĩa score cố định của policy này.
Title thiếu nhận score 0. Không cần dependency matching bổ sung.

Họ tác giả lấy từ phần trước dấu phẩy hoặc token cuối trong dạng tên xuôi,
bỏ hậu tố Jr/Sr/II/III/IV và initial một ký tự, rồi áp dụng chuẩn hóa Unicode.
Cách này còn hạn chế với tên tập thể, thứ tự tên theo nhiều ngôn ngữ và
cách viết LaTeX phức tạp. Chất lượng rule sẽ được kiểm chứng trên nhãn manual
khi có dữ liệu thật. Year được giữ trong input, không dùng làm điều kiện
match theo Mission hiện tại.

Output gồm `auto_labels.parquet`, `pair_evidence.parquet`, `summary.json`
và marker `_SUCCESS`. File label có trạng thái matched/no_match/needs_review,
ID được chọn và các candidate cần xem. Audit cặp giữ DOI gốc/chuẩn hóa, nguồn
DOI, title score, họ tác giả chung và lý do. Summary giữ policy, fingerprint,
số quyết định và danh sách publication hoàn tất/cần review. Publication còn
entry needs_review chưa được tính là hoàn tất gán nhãn tự động.

Thư mục output phải mới. Dữ liệu được sinh và ghi theo entry, không giữ toàn
bộ ma trận cặp trong RAM. Source tables và index DOI vẫn nằm trong RAM.
Nhãn auto này chưa có partition và chưa phải groundtruth cuối. Bước hợp nhất
phải giải quyết needs_review trước khi tính quota hoặc đưa vào train/eval.

Python >=3.11, Colab CPU; inclusive range `2504.02523`–`2504.07522`.
Metadata và source arXiv dùng chung scheduler nối tiếp; S2 có limiter riêng.
Mỗi request tối đa ba attempts, gồm cả 429; source validation dùng cùng budget.
Cooldown và attempts được lưu trước khi tiếp tục. Không tự retry failed khi resume.

## Kiến trúc references theo version

References mới truy vấn URL arXiv vN, lưu riêng từng version; snapshot v0 giữ nguyên.
Chưa kiểm chứng rằng S2 trả bibliography lịch sử tương ứng từng version.
S2 chạy bốn lượt `POST /paper/batch`, tối đa 100 version mỗi request. Chỉ
snapshot có `len(references) == referenceCount` mới hoàn tất; mục thiếu chuyển
sang lượt kế tiếp. Sau lượt 4, crawler dùng endpoint references có pagination
riêng cho từng version. Lỗi toàn batch giảm cap `100→75→50→25`; 429 vẫn theo
`Retry-After` và không làm giảm cap.

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
- `data/reference-batches/pass-N/`: raw batch response S2 và receipt checksum;
  snapshot version lưu provenance trỏ về batch/index tương ứng.
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
Notebook chạy toàn range qua quy trình 2 giai đoạn tự động tại cell 5: tự động chạy Discovery trước (~5 phút) rồi cào song song arXiv + S2. CLI hỗ trợ chạy toàn range hoặc từng stage riêng (`discovery`, `arxiv`, `s2`).
Chế độ `discovery` quét nhanh Atom XML toàn bộ ID (50 ID / request, ~5 phút) để xác định phiên bản và khởi tạo version jobs trước khi chạy S2 hoặc Source.
Khi bị ngắt phiên đột ngột trên Drive/Colab gây lệch sequence giữa journal và snapshot (`jobs.csv.json`), dùng tiện ích `repair_journal_sequence` (hoặc cell sửa đồng bộ trong notebook) để tự động cân chỉnh lại sequence an toàn.
`recheck-not-found` riêng cho not_found.
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

Code repo và Drive runtime độc lập. Sau khi export snapshot M1, chuẩn bị số liệu
báo cáo bằng lệnh sau từ repo code cho một range:

```bash
PYTHONPATH=src python -m crawler.m1_report \
  --export runtime/exports/<export_id> --runtime runtime \
  --output ../m1-report/book/data
```

Nếu hai thành viên cào hai range riêng, giữ nguyên hai runtime và export từng
runtime bằng config tương ứng. Ghép hai export bằng:

```bash
PYTHONPATH=src python -m crawler.m1_report \
  --part runtime/exports/m1-own runtime \
  --part runtime_Hiếu/exports/m1-hieu runtime_Hiếu \
  --output ../m1-report/book/data
```

Lệnh kiểm tra checksum, ID được giao không giao nhau và run ID không trùng;
không ghép trực tiếp progress hoặc data. Xem hướng dẫn đầy đủ trong README của
repo báo cáo.

Jupyter Book nằm trong repo báo cáo riêng. Parsing và matching thuộc các
milestone sau.

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

Dừng crawler và đợi worker ghi xong trước khi cập nhật code; giữ backup runtime
và không xóa journal hoặc services.json khi resume.

### Resume và xác minh riêng

Resume chỉ phục hồi job pending/running đã tiêu hao lượt thử trong phạm vi
được chọn; không checksum lại toàn bộ kết quả thành công. Dữ liệu cũ giữ nguyên
trạng thái. Kết quả mới có `verification.json` sau khi
publish thành công, đối chiếu kích thước file và lưu mã băm chuẩn vào manifest.
Quá trình commit không đọc ngược lại toàn bộ file qua Drive FUSE để tối ưu hóa thời gian I/O.

`TAKEOVER_STALE` mặc định False. Chỉ bật sau lỗi dấu khóa cũ khi đã dừng/restart
runtime cũ và chắc chắn không có runtime khác ghi dataset. Đóng tab hay mất
kết nối không đủ xác nhận. Takeover không dừng tiến trình khác; đổi về False
sau lần tiếp quản. Không xóa journal/cooldown để vượt lỗi.

Khởi động đọc snapshot/journal nhưng không quét baseline dung lượng. Các thao
tác đọc/ghi Drive khác vẫn có thể chậm. Các summary cũ có baseline toàn dataset
không được so sánh trực tiếp với disk peak mới có phạm vi session_touched_paths.

### Export M1 schema v3 và định dạng source

`papers.parquet` chỉ chứa publication trong range có discovery succeeded và
normalized metadata kiểm tra được checksum. ID pending/failed/not_found vẫn
có trong `paper_metrics.csv`, cùng cột `metadata_exported`; không đếm số ID
được giao như số publication thành công. Manifest có `assigned_ids`,
`missing_metadata`, `schema_version=3` và phạm vi kiểm tra toàn vẹn. Export cũ
không bị sửa; tạo run-id mới để xuất lại.

Venue được bổ sung từ `citedPaper.venue` trong các snapshot references S2 đã
lưu trong range, ghép bằng đúng ref_arxiv_id. `venue_source` ghi nguồn; giá trị
mâu thuẫn giữ null và được liệt kê trong `venue_conflicts` của manifest. Không
suy đoán venue từ DOI/journal_ref, không gọi thêm API. Vì vậy paper chưa được
nhắc tới trong các snapshot có thể vẫn thiếu venue. Metadata arXiv raw được
giữ nguyên; enrichment chỉ diễn ra lúc export, áp dụng cả dữ liệu cũ.

Export kiểm tra checksum các JSON thực sự sử dụng và cache trong một lần
chạy, không đọc lại mọi file TeX/Bib. `ref_submission_date` có
nguồn S2 publicationDate, không khẳng định là ngày nộp arXiv. Reference snapshot
truy vấn theo URL vN; việc S2 trả đúng bibliography lịch sử từng version chưa
được xác minh. Legacy v0 vẫn giữ riêng.

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


Reference-page recovery and S2 dispatch pacing are implemented in
`src/crawler/reference_worker.py` and `src/crawler/scheduler.py`.
Details of the reference-page recovery and S2 dispatch-pacing update:
[change report](docs/reference-resume-and-s2-pacing.md).

## Milestone 2 · Standardization

Phần B nằm trong `src/standardization`. Dùng Python 3.11 trở lên và cài
`src/requirements.txt`. Chạy bộ test B từ thư mục gốc bằng lệnh sau.

```bash
python -m pytest tests/test_b[123]_*.py tests/test_b_publication.py
```

Trên Windows, truyền danh sách file B trực tiếp để pytest không thu thập
test crawler M1 dùng `fcntl`.

```powershell
$bTests = @(Get-ChildItem tests -File | Where-Object {$_.Name -match '^test_b([123]_|_publication\.py$)'} | ForEach-Object {$_.FullName})
python -m pytest @bTests
```

C3 có thể gọi `standardize_publication` sau A1/A2/A3 để chuẩn hóa cây, đọc
bibliography theo version, hợp nhất reference và remap citation trước C2.
Xem [API và quy tắc tích hợp](docs/b-local-workflow-api.md).
