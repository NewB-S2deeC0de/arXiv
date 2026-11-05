# Tích hợp phần B vào Milestone 2

C3 chạy A1/A2/A3 cho từng version rồi truyền kết quả theo publication.
`source_dir` là thư mục source đã chọn cho version. `source_texts` là map
đường dẫn tương đối đến văn bản đã decode mà A1 dùng để tính source spans.

```python
from standardization import PublicationVersionInput, standardize_publication
from standardization import b3_audit_payload, b3_cache_fingerprint

versions = [PublicationVersionInput(a3, gathered, source_dir, source_texts)]
result = standardize_publication(arxiv_id, versions, node_factory=TreeNode)

for version in result.versions:
    if version.finalized is not None:
        store.add_tree(version.version, version.finalized.tree)

bibtex_rows = [record.payload.as_kwargs() for record in result.references.merge.records]
audit = b3_audit_payload(result.references)
```

C3 giữ `version.status`, `version.bibliography` và `version.normalized` khi
lưu audit. `version.finalized.nodes` giữ quyết định remap cùng vị trí node.
`audit` có mapping theo publication/version/key gốc, các quyết định gộp và
provenance. Version thiếu TeX được C ghi là `empty` từ trạng thái M1.

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
  staging. `include_partial=True` cho phép version partial có cây tham gia
  gộp và remap. C áp dụng cùng policy khi ghi output và coverage.

Trong hook `cache_fingerprint()` của service C, kết hợp fingerprint parser
với `b3_cache_fingerprint(parser_fingerprint=super().cache_fingerprint())`.
Khi quyết định duyệt, resource selection hoặc chính sách cấu trúc thay đổi,
C cần đưa cấu hình đó vào fingerprint của service để tính lại checkpoint.
