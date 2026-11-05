# Các câu hỏi cần trả lời để hoàn tất D1, D2 và D3

## Phạm vi và cách trả lời

Tài liệu phục vụ ba issue của luồng D:

- **D1 #12:** bộ test offline và acceptance matrix cho parser.
- **D2 #18:** benchmark Colab CPU-only và thống kê parsing/deduplication.
- **D3 #19:** chương Milestone 2 trong Jupyter Book hiện hữu và README reproducibility.

Nguồn đối chiếu: nội dung D1–D3 và danh sách issue trong ảnh; code/tài liệu A1–A3 trên local `main`; file `answer.md` mô tả nhánh C `M2_C1_C2_C3`, HEAD `e418a4c`. Chưa có toàn văn issue A/B/C, nên các câu dưới đây không khẳng định yêu cầu ngoài những nguồn đã cung cấp.

Ảnh chưa thể hiện assignee. Vì vậy **người trả lời được chỉ định theo người phụ trách issue**, không suy đoán tên cá nhân. Người phụ trách chính tổng hợp câu trả lời với các bên phối hợp.

Mỗi câu trả lời nên có: quyết định cuối cùng, API/schema hoặc ví dụ input/output, trạng thái `đã triển khai` / `đã chốt, chưa triển khai` / `chưa chốt`, và branch + commit/file liên quan. Với số liệu thực nghiệm, kèm đường dẫn artifact. Nếu khác mô tả trong `answer.md`, ghi rõ quyết định thay thế.

Không cần chờ mọi câu trả lời mới bắt đầu D1. Có thể làm ngay matrix và test A1–A3 theo hành vi đã tài liệu hóa. Các câu về contract B/C chặn việc hoàn tất test tích hợp; các câu về dữ liệu và môi trường chặn benchmark thật; book và artifact benchmark chặn hoàn tất báo cáo.

## 1. Nguồn yêu cầu và phiên bản tích hợp

### Q01 — Đặc tả nào là căn cứ acceptance cuối cùng?

- **Người trả lời:** trưởng nhóm/người tổng hợp yêu cầu đề bài; phối hợp người phụ trách D1 #12.
- **Cần trả lời:** cung cấp bản đặc tả hoặc link + phiên bản, toàn văn các issue A/B/C, các ví dụ bắt buộc và ngoại lệ đã thống nhất. Khi đề, issue và implementation khác nhau thì căn cứ nào được ưu tiên?
- **Phục vụ:** D1 matrix, D2 mẫu số/thống kê, D3 giải thích mức đáp ứng đề.

### Q02 — D dùng branch/commit và entry point nào để kiểm thử toàn bộ M2?

- **Người trả lời:** người phụ trách C3 #17; phối hợp người tích hợp A/B/C.
- **Cần trả lời:** branch tích hợp, commit, cách cài dependency và lệnh/API chạy end-to-end. A1–A3/B1–B3/C1–C3 nào đã được nối vào service thật, phần nào còn fake/stub hoặc file chưa track? Nhánh trong `answer.md` có còn là bản hiện hành không?
- **Phục vụ:** D1 integration; D2 benchmark thực; D3 lệnh tái lập.

## 2. Contract, dữ liệu nguồn và output — luồng C

### Q03 — Contract input/output chính thức của C1/C3 là gì?

- **Người trả lời:** người phụ trách C1 #11; phối hợp C3 #17.
- **Cần trả lời:** module/type/signature chính thức; schema, kiểu dữ liệu, nullable, khóa và schema version của `elements`, `hierarchy`, `bibtex`; danh sách `kind` hợp lệ và root representation. Cung cấp một ví dụ hoàn chỉnh gồm một publication có hai version và ba bảng mong đợi.
- **Phục vụ:** assertion D1, đọc metrics D2 và mô tả output D3.

### Q04 — Điều kiện nhận source M1 và phân loại input không dùng được là gì?

- **Người trả lời:** người phụ trách C1 #11; phối hợp người quản lý dataset M1.
- **Cần trả lời:** layout nguồn và marker bắt buộc; cách phân biệt `no_tex`, source thiếu, marker/checksum sai, version pending/failed và thư mục TeX rỗng. Adapter kiểm gì, bỏ qua gì, và lỗi nào phải chạy audit M1 riêng? Xác nhận mô tả hiện tại: kiểm marker/inventory khác với re-hash từng TeX/Bib, còn input fingerprint phục vụ invalidate checkpoint.
- **Phục vụ:** D1 input fixtures; D2 không quy lỗi input thành lỗi parser; D3 giới hạn toàn vẹn.

### Q05 — Bảng trạng thái cuối cùng và chính sách partial/error là gì?

- **Người trả lời:** người phụ trách C3 #17; phối hợp C1 #11.
- **Cần trả lời:** xác nhận `partial` mặc định không xuất, `include_partial=True` mới xuất; tree chưa finalize là pending; unparseable là failed. Một publication lỗi validation thì dừng cả run hay ghi lỗi publication đó và tiếp tục? Version empty hợp lệ khác pending/failed thế nào? Khi một publication có nhiều trạng thái version thì coverage được tính thế nào?
- **Phục vụ:** D1 test lỗi, D2 success/partial/failure counts, D3 diễn giải coverage.

### Q06 — Raw, provenance và audit sẽ được lưu ở đâu?

- **Người trả lời:** người phụ trách C3 #17; phối hợp C1 #11, B2 #10 và B3 #14.
- **Cần trả lời:** chốt sidecar hay mở rộng schema; tên file/schema/lifecycle của raw, source spans, field evidence, unknown fields, conflicts và entry lỗi/thiếu key. Dữ liệu nào phải ghi trước remap/dedup, và audit có được giữ khi publication bị loại khỏi output không? Cung cấp ví dụ audit có nhiều span.
- **Phục vụ:** D1 bảo toàn provenance; D2 unresolved/conflict counts; D3 khả năng truy vết và failure modes.

### Q07 — Hierarchy lưu thứ tự và các occurrence như thế nào?

- **Người trả lời:** người phụ trách C2 #16; phối hợp C1 #11 và A2 #13.
- **Cần trả lời:** chốt `position`/ordinal, occurrence ID và khóa của cạnh. Nếu một element lặp hai lần dưới cùng parent hoặc xuất hiện dưới hai parent, output biểu diễn từng occurrence thế nào? Có dựng lại được đúng cây và source order sau khi đọc Parquet không? Không chỉ dựa vào thứ tự vật lý của hàng.
- **Phục vụ:** D1 hierarchy/dedup acceptance; D2 số element so với occurrence; D3 mô tả per-version tree.

### Q08 — Quy tắc dedup element và sinh ID cuối cùng là gì?

- **Người trả lời:** người phụ trách C2 #16; phối hợp B1 #9 và B3 #14.
- **Cần trả lời:** khóa nhận diện trùng gồm `kind`, normalized content, cấu trúc con hay trường nào khác; phạm vi trong/cross-version, trong/cross-publication; cách xử lý null/empty content và collision. ID phải ổn định trong những điều kiện nào? Cho ví dụ expected output cho cùng text khác kind, khác parent, cùng parent lặp lại và cùng text qua hai version.
- **Phục vụ:** D1 dedup fixtures; D2 before/after counts; D3 giải thích thuật toán và giới hạn.

### Q09 — Orchestration, checkpoint và resume có contract cuối cùng nào?

- **Người trả lời:** người phụ trách C3 #17; phối hợp B1 #9, B2 #10, B3 #14.
- **Cần trả lời:** xác nhận A1 → A2 → A3 → B1, B2 extraction/unification → B3 remap theo version → C2; xác định method thực tế cho từng bước. Xác nhận fingerprint và hành vi khi source/code/schema/settings đổi, shard mất/hỏng hoặc run bị ngắt. Đường dẫn checkpoint/output nào D có thể dùng trong thư mục tạm mà không sửa runtime thật?
- **Phục vụ:** D1 orchestration/resume tests; D2 fresh/resumed run; D3 cách chạy lại.

## 3. Hành vi parser — luồng A

### Q10 — A1 đã chốt phạm vi hỗ trợ và diagnostics nào?

- **Người trả lời:** người phụ trách A1 #8; phối hợp C1 #11.
- **Cần trả lời:** xác nhận tài liệu `docs/m2-a1-gather.md` là contract hiện hành: root detection, nested/commented/missing/unused includes, cycles, includeonly/conditionals, repeated includes, encoding và giới hạn tài nguyên. Trả về status/diagnostic nào cho mỗi trường hợp? Các trường hợp nào được kỳ vọng partial hoặc ngoài phạm vi?
- **Phục vụ:** D1 fixture matrix và D3 limitations. Không cần thiết kế lại hành vi đã chốt; chỉ nêu khác biệt so với tài liệu.

### Q11 — A2 chốt hierarchy và bibliography boundary thế nào?

- **Người trả lời:** người phụ trách A2 #13; phối hợp B2 #10 và C2 #16.
- **Cần trả lời:** xác nhận `docs/m2-a2-hierarchy.md`; expected tree cho section/subsection/section*, skipped levels, acknowledgments, appendices, paragraph/list/nested item. Heading References không có boundary rõ, bibliography trong preamble/body và cấu trúc không đóng được phân loại thế nào? Node nào được chuyển cho B2, node nào còn trong body?
- **Phục vụ:** D1 hierarchy/bibliography tests; D3 giải thích cấu trúc.

### Q12 — A3 chốt leaf kinds và ranh giới nội dung thế nào?

- **Người trả lời:** người phụ trách A3 #15; phối hợp B1 #9.
- **Cần trả lời:** xác nhận `docs/m2-a3-leaves.md`; expected sentence boundaries cho `i.e.`, `et al.`, `Fig. 1`, decimals và URL; inline math nằm trong sentence hay có leaf riêng? Chốt block formula environments, figure/table/algorithm subtype, caption/label và các trường hợp unsupported/unclosed. Cho ví dụ expected leaves và spans, nhất là khi đoạn đi qua nhiều file include.
- **Phục vụ:** D1 fine-grained acceptance; D2 counts theo kind; D3 phương pháp và giới hạn heuristic.

## 4. Normalization, bibliography và remapping — luồng B

### Q13 — B1 chuẩn hóa content theo quy tắc nào?

- **Người trả lời:** người phụ trách B1 #9; phối hợp A3 #15 và C2 #16.
- **Cần trả lời:** API chính thức; quy tắc comment, whitespace, escaped symbols, formatting commands, Unicode, inline/block math, labels, citations và unknown macros. Những khác biệt raw nào phải cho cùng content, khác biệt nào phải được giữ? Khi nào content được phép null hoặc rỗng? Cung cấp cặp input/output chuẩn, đồng thời xác nhận raw và spans không bị sửa.
- **Phục vụ:** D1 normalization và dedup; D2 empty counts; D3 giải thích cleanup.

### Q14 — Interface bibliography giàu context đã được B/C chốt chưa?

- **Người trả lời:** người phụ trách B2 #10; phối hợp C1 #11 và C3 #17.
- **Cần trả lời:** signature thay thế/bổ sung `extract_bibitems(text, arxiv_id)`; cách truyền version, source_dir, gathered source và bib files; type của entries/diagnostics. Khi `.bib` và `bibitem` cùng tồn tại thì đọc/gộp những nguồn nào, file không được dùng có bị đưa vào bibliography không? Có hỗ trợ nguồn khác như `.bbl` không, hay ghi ngoài phạm vi?
- **Phục vụ:** D1 bibliography integration; D2 entry coverage; D3 input và giới hạn hỗ trợ.

### Q15 — Schema và missing/conflict semantics của BibEntry là gì?

- **Người trả lời:** người phụ trách B2 #10; phối hợp C1 #11 và C3 #17.
- **Cần trả lời:** chốt `authors=None` so với `[]`, `entry_type` parsed/fallback, fields bắt buộc/tùy chọn, unknown fields, raw và evidence. Entry thiếu key, parse lỗi, thiếu title/year/authors hay field mâu thuẫn có được giữ không, với diagnostics nào? Quy tắc ưu tiên khi hợp nhất `.bib` và `bibitem` trong cùng version là gì?
- **Phục vụ:** D1 expected entries; D2 missing/conflict counts; D3 chất lượng trích xuất.

### Q16 — B3 dedup bibliography xuyên version theo khóa và precedence nào?

- **Người trả lời:** người phụ trách B3 #14; phối hợp B2 #10 và C3 #17.
- **Cần trả lời:** nhận diện bằng DOI/title/author/year/key như thế nào; precedence khi non-null fields mâu thuẫn; cách sinh canonical key và xử lý collision. Cùng key nhưng khác reference, khác key nhưng cùng reference và reference đổi title qua version được xử lý ra sao? Chốt interface unification và nơi trả conflicts/evidence. Phân định rõ việc merge trong version của B2 và dedup xuyên version của B3.
- **Phục vụ:** D1 cross-version fixtures; D2 BibTeX before/after; D3 thuật toán và failure modes.

### Q17 — Mapping citation theo version và remap syntax có contract nào?

- **Người trả lời:** người phụ trách B3 #14; phối hợp C3 #17 và B1 #9.
- **Cần trả lời:** có chốt mapping `(version, old_key) → canonical | None` theo từng publication không? `None`, key không tìm thấy hoặc conflict có giữ nguyên text và phát warning không? Những lệnh cite, multiple keys, optional arguments được hỗ trợ; comment/literal/raw có bị sửa không? Cung cấp expected output cho hai version dùng cùng old key để trỏ tới hai reference khác nhau.
- **Phục vụ:** D1 remap-before-dedup acceptance; D2 unresolved/conflicts; D3 mô tả key remapping.

## 5. Bộ test và acceptance — D1

### Q18 — Phạm vi bàn giao D1 và trạng thái chưa triển khai được thống nhất thế nào?

- **Người trả lời:** người phụ trách D1 #12; phối hợp trưởng nhóm và A/B/C.
- **Cần trả lời:** nhóm nào phải pass để bàn giao; mục nào tạm `xfail(strict=True)` có lý do/issue, mục nào chưa chốt contract. Ai xác nhận expected results độc lập với implementation? Khi contract đổi, ai duyệt cập nhật matrix? Unit, integration và live benchmark được phân tách bằng đường dẫn/marker nào?
- **Phục vụ:** tránh pass giả và phân biệt bàn giao nền test với parser đã đạt toàn bộ yêu cầu.

### Q19 — Môi trường offline chính thức để nghiệm thu test là gì?

- **Người trả lời:** người phụ trách D1 #12; phối hợp C3 #17/người quản lý CI.
- **Cần trả lời:** Python/dependency versions, Linux/WSL/Colab nào được hỗ trợ; có CI chạy pytest không; lệnh nghiệm thu. Suite phải chặn mạng và ghi trong tmp_path thế nào? Test symlink/resource limits có điều kiện môi trường gì? Đồng thuận rằng lỗi `fcntl` hoặc quyền symlink trên Windows không được tự động coi là test logic parser đã pass.
- **Phục vụ:** D1 offline/deterministic; D3 hướng dẫn chạy chính xác.

## 6. Dataset, benchmark và metrics — D2

### Q20 — Dataset benchmark nằm ở đâu và mẫu chính thức gồm những gì?

- **Người trả lời:** người quản lý dataset M1; phối hợp C1 #11 và D2 #18.
- **Cần trả lời:** cách truy cập dữ liệu ngoài Git, export/source snapshot dùng, range hoặc danh sách ID/version, checksum/fingerprint và kết quả audit nếu có. Sample nào chạy thử, sample nào báo cáo chính thức? Input có bao phủ multi-version, no_tex, partial và các loại source phổ biến không? D được phép đọc ở đâu và ghi output riêng ở đâu?
- **Phục vụ:** D2 chạy thật, đánh giá thiên lệch mẫu và D3 tái lập. Không gửi credential vào tài liệu.

### Q21 — Phạm vi thời gian và cách chạy benchmark là gì?

- **Người trả lời:** người phụ trách D2 #18; phối hợp C3 #17 và người chạy Colab.
- **Cần trả lời:** fresh run hay resume, cache bật/tắt, số lượt chạy, thứ tự input và CPU-only được xác nhận thế nào? Tổng wall-clock gồm đọc Drive, parse, dedup, export và validation đến đâu; setup/download có tính riêng không? Thời gian trung bình mỗi publication dùng mẫu số nào, xử lý failed/partial và publication nhiều version thế nào?
- **Phục vụ:** D2 phép đo có thể so sánh; D3 không diễn giải sai runtime.

### Q22 — Contract metrics và mẫu số cho từng chỉ số đã chốt chưa?

- **Người trả lời:** người phụ trách C3 #17; phối hợp C2 #16, B3 #14 và D2 #18.
- **Cần trả lời:** tên file/cột, đơn vị và cấp publication/version/run; assigned/available/attempted/success/partial/pending/failed/no_tex/empty; counts theo kind trước/sau dedup; phân biệt unique element với occurrence. BibTeX before/after lấy ở stage nào? Unresolved/conflict đếm theo key, citation occurrence, entry hay field? Null khác zero thế nào? Sửa lệch `coverage.csv` với manifest về partial/pending bằng cách nào?
- **Phục vụ:** D2 tính đúng thống kê; D1 kiểm metrics trên fixture biết trước; D3 render trực tiếp từ artifact.

### Q23 — Đo RAM, storage và giới hạn sampling thế nào?

- **Người trả lời:** người phụ trách D2 #18; phối hợp C3 #17.
- **Cần trả lời:** RSS của process hay gồm child processes, sampling interval, peak observed và thông tin hardware/environment cần lưu. Dung lượng cuối gồm những file nào: ba bảng, audit, checkpoint hay toàn output? Source đầu vào và temp đo riêng thế nào? Xác nhận không dùng disk counter `session_touched_paths` của crawler làm tổng output parser.
- **Phục vụ:** D2 RAM/storage đúng phạm vi; D3 limitations của phép đo.

### Q24 — Run benchmark thật do ai thực hiện và nghiệm thu artifact ở đâu?

- **Người trả lời:** người phụ trách D2 #18; phối hợp người có Colab/dataset và D1 #12.
- **Cần trả lời:** người chạy, nơi lưu CSV/JSON/Parquet + environment/run metadata, logs và kết quả D1 trên commit tích hợp. Khi D1 regression hoặc run bị ngắt thì đánh dấu benchmark thế nào? Sample benchmark tối thiểu nào được chấp nhận và missing data/sampling limitations sẽ được ghi ở đâu?
- **Phục vụ:** điều kiện hoàn tất D2 và nguồn số liệu thật cho D3.

## 7. Jupyter Book, README và bàn giao — D3

### Q25 — Jupyter Book Milestone 1 hiện hữu ở đâu và build thế nào?

- **Người trả lời:** người phụ trách book/report M1; phối hợp D3 #19.
- **Cần trả lời:** repo/branch/path, cấu hình mục lục, phiên bản Jupyter Book, build command và cơ chế hosting/CI. Chương M2 thêm vào vị trí nào, giữ template/cấu trúc nào? Checkout main đã kiểm tra chưa có cấu hình book, nên cần nguồn book chính xác để tránh tạo book thay thế.
- **Phục vụ:** D3 mở rộng đúng artifact hiện hữu và kiểm build/link.

### Q26 — Book lấy metrics từ đâu để build tái lập được?

- **Người trả lời:** người phụ trách D3 #19; phối hợp D2 #18 và người quản lý book.
- **Cần trả lời:** lưu bộ aggregate metrics nhỏ trong repo hay tải artifact bằng manifest/checksum; file nào được phép commit, file nào phải ở ngoài Git. Lệnh chuẩn bị metrics và build có cần credential không? Các bảng/biểu đồ nào bắt buộc, và khi artifact thiếu thì build báo lỗi hay đánh dấu chưa có kết quả?
- **Phục vụ:** D3 biểu đồ sinh từ dữ liệu, không screenshot hoặc điền số giả; README hướng dẫn đầy đủ.

### Q27 — Quy trình README và thông tin bàn giao cuối cùng là gì?

- **Người trả lời:** người phụ trách D3 #19; phối hợp C3 #17, D2 #18 và người nộp bài/trưởng nhóm.
- **Cần trả lời:** chốt lệnh từ cài môi trường → chuẩn bị input → parse → test → benchmark → build book; cấu hình và đường dẫn tương đối. Link GitHub, book/report/artifact chính thức là gì, ai nộp Moodle và cần định dạng nào? Những limitations hoặc kết quả chưa xác minh nào bắt buộc phải ghi rõ?
- **Phục vụ:** D3 reproducibility và bàn giao có thể kiểm tra được.

## Thứ tự thu thập câu trả lời đề xuất

1. **Chốt bản tích hợp và yêu cầu:** Q01–Q03 để mọi người cùng kiểm một contract/commit.
2. **Gỡ khoảng trống B/C:** Q05–Q09 và Q13–Q17, đặc biệt occurrence/order, bibliography conflict và mapping theo version.
3. **Làm D1 song song:** xác nhận Q04, Q10–Q12, Q18–Q19; lập matrix và bổ sung test A1–A3 ngay.
4. **Chuẩn bị D2:** Q20–Q24; chỉ kết luận từ run thật, lưu đầy đủ giới hạn phép đo.
5. **Chuẩn bị D3 sớm:** Q25–Q27; viết phương pháp trước, hoàn thiện kết quả sau artifact D2.

## Mẫu trả lời cho từng câu

```text
Mã câu hỏi:
Người trả lời / issue phụ trách:
Quyết định cuối cùng:
API/schema hoặc ví dụ input → output:
Trạng thái: đã triển khai / đã chốt, chưa triển khai / chưa chốt
Branch + commit + file hoặc artifact:
Điểm còn cần người khác xác nhận:
```
