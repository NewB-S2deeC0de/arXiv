# Tích hợp phần B vào Milestone 2

C3 giữ kết quả A1 và A3 của từng version, rồi gọi `standardize_publication`
một lần cho cả publication trước khi C2 deduplicate elements. Service C3
chuyển từ giao diện `parser.bibtex` cũ sang API này.
`source_dir` trỏ đến thư mục TeX đã giải nén của version (thường là
`source/tex`). `source_texts` là map đường dẫn tương đối đến đúng văn bản
đã decode mà A1 dùng để tính source spans.

```python
from standardization import PublicationVersionInput, standardize_publication
from standardization import b3_audit_payload, b3_cache_fingerprint

versions = [
    PublicationVersionInput(a3, gathered, source_dir, source_texts, selected_bib_files)
    for a3, gathered, source_dir, source_texts, selected_bib_files in parsed_versions
]
result = standardize_publication(
    arxiv_id, versions, node_factory=TreeNode, include_partial=False,
)

for version in result.versions:
    if version.finalized is not None:
        store.add_tree(version.version, version.finalized.tree)

bibtex_rows = []
for record in result.references.merge.records:
    payload = record.payload.as_kwargs()
    bibtex_rows.append({name: payload.get(name) for name in BIBTEX_SCHEMA.names})
audit = b3_audit_payload(result.references)
mapping = audit["result"]["merge"]["mapping"]
```

C3 giữ `version.status`, `version.bibliography` và `version.normalized` khi
lưu audit. `version.finalized.nodes` giữ quyết định remap cùng vị trí node.
`audit` có mapping theo publication/version/key gốc, các quyết định gộp và
provenance. Version thiếu TeX được C ghi là `empty` từ trạng thái M1.

C3 dùng trực tiếp cây finalized, đã remap citation theo version. C giữ
mapping và diagnostics trong sidecar từng publication
`_parts/<encoded-arxiv-id>/audit.json` và tổng hợp vào
`bibtex_audit.json`. Mapping có thể chứa JSON `null` cho key mơ hồ.
`b3_audit_payload` đóng gói phần B3. Diagnostics B1 nằm ở
`version.normalized.diagnostics`. Diagnostics B2 nằm trong
`version.bibliography.selection_diagnostics`, các block của
`version.bibliography.inspection` và cảnh báo đọc/parse trong
`version.bibliography.resources`. Occurrences của version bị loại vẫn nằm
ở `version.bibliography.occurrences`. Giữ offset basis khi serialize.
Offset của quyết định remap tính trên content trước remap. Nếu manifest
dùng mã `ambiguous_reference_key`, C ánh xạ từ diagnostic B3
`ambiguous_citation_mapping` khi ghi warning và giữ mã gốc trong audit.

- **B1.** Toán inline dùng `$...$`. Display đơn không đánh số dùng
  `equation*`. Các môi trường nhiều dòng như `align`, `gather`, `multline`
  giữ cấu trúc và chế độ đánh số tương ứng. Label, tag và raw được giữ.
  Heading mặc định lấy `attrs.title` và chuẩn hóa prose. Node cha dùng
  chuỗi rỗng khi children mang nội dung. Caller có thể truyền callback
  `structural_content` riêng khi cấu trúc A3 cần chính sách khác.
- **B2.** Mặc định chọn `.bib` từ khai báo tĩnh khi mỗi target có đúng một
  candidate trong inventory. Khi khai báo có điều kiện, có options hoặc
  resource thiếu hay mơ hồ, caller có thể truyền `selected_bib_files` rõ ràng.
  B2 giữ cả bibitem và entries từ file `.bib` cùng provenance. Biểu thức
  BibTeX cần macro expansion hoặc inheritance được lưu kèm diagnostic.
- **B3.** B tự gộp DOI chắc chắn và cấp canonical key theo publication.
  Candidate metadata thiếu DOI cần quyết định duyệt. Lấy `candidate_id` từ
  `result.references.metadata_candidates` rồi chạy lại với
  `approved_metadata_candidates=[candidate_id]`. ID gắn với thành viên và
  evidence của candidate. DOI mâu thuẫn chặn duyệt. Field mâu thuẫn giữ các
  giá trị trong audit. Key gốc mơ hồ giữ nguyên citation trong đúng version.
- **Partial.** A3 partial hoặc lỗi xác định/đọc `.bib` khiến version vào
  staging. Khi caller cung cấp `source_texts` mà B1 không ánh xạ được leaf
  về source, B giữ raw và diagnostic, đồng thời hạ version từ `ready`
  xuống `partial`. `source_texts=None` bỏ qua bước đối chiếu này.
  Mặc định version partial nằm trong audit và không tham gia gộp hay xuất
  cây. `include_partial=True` cho phép version partial có cây tham gia
  gộp và remap, vẫn giữ trạng thái `partial` khi C ghi output và coverage.

C dùng `include_partial=False` trong production và đọc trạng thái từ
`result.versions` để ghi manifest/coverage. C giữ mặc định
`structural_content`. Override .bib thuộc config C theo publication và
version, rồi được truyền trong từng `PublicationVersionInput`.
Production để trống `approved_metadata_candidates`. Nếu bật về sau, C
truyền đúng các `candidate_id` do B cấp từ lần chạy trước.

Trong hook `cache_fingerprint()` của service C, kết hợp fingerprint parser
với `b3_cache_fingerprint(parser_fingerprint=super().cache_fingerprint())`.
C giữ `include_partial` và chính sách cấu trúc trong policy fingerprint.
Override .bib và candidate ID đã duyệt thuộc input fingerprint của
publication liên quan. API B chỉ nhận keyword `parser_fingerprint` cho
`b3_cache_fingerprint`. Code B đổi sẽ làm fingerprint này đổi.

Luồng C phụ trách PR tích hợp và chạy `run_parser` qua service mặc định.
B kiểm tra selection, provenance, mapping, remap và audit. A xác nhận
nội dung đã decode và offsets dùng chung. Mốc nghiệm thu là commit của
nhánh tích hợp có đủ A, B và C.
