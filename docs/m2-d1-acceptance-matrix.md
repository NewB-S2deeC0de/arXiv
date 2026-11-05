# D1 — Acceptance matrix

Mỗi test dưới đây dùng dữ liệu tổng hợp. `covered` có nghĩa đã có assertion, không có nghĩa đã xác nhận toàn bộ parser M2 đáp ứng đề. Kết quả Linux đầy đủ cần chạy theo hướng dẫn reproducibility. Test C3 dùng fake services không chứng minh B1/B2/B3 thực.

| Yêu cầu | Test (trong tests/) | Expected result | Phạm vi |
|---|---|---|---|
| Root, nested/repeated includes | test_a1_gather.py::test_gather_follows_nested_and_repeated_includes_with_file_spans | Đúng thứ tự và nguồn đoạn | covered A1 |
| Comment/literal include | test_a1_gather.py::test_gather_ignores_commented_and_literal_commands | Không mở rộng include giả | covered A1 |
| Missing/ambiguous root | test_a1_gather.py::test_gather_reports_ambiguous_or_missing_root_without_guessing | Không đoán root | covered A1 |
| Cycle, missing, unsafe include | test_a1_gather.py::test_gather_reports_cycle_missing_and_unsafe_include_without_reading_outside | Diagnostics, không đọc ngoài source | covered A1; cần symlink |
| Unused input, deterministic, read-only | test_d1_acceptance.py::test_d1_real_a_stages_are_deterministic_and_read_only | Hai kết quả bằng nhau; input giữ nguyên; đúng câu và spans | covered A1–A3 thật |
| No TeX/missing/source lỗi | test_parser_adapter.py::test_adapter_reads_success_empty_and_missing | Phân loại riêng | covered adapter |
| Heading/starred/appendix | test_a2_hierarchy.py::test_headings_skip_levels_without_synthetic_nodes_and_keep_appendix | Không tạo cấp giả; giữ thuộc tính | covered A2 |
| List/item lồng nhau | test_a2_hierarchy.py::test_nested_lists_have_item_parents_and_preserve_order | Đúng parent/thứ tự | covered A2 |
| Bibliography boundary | test_a2_hierarchy.py::test_bibliography_is_separate_but_uncertain_references_heading_is_kept | Tách khi rõ; giữ uncertain | covered A2 |
| Acknowledgments | Chưa có test chuyên biệt được xác nhận | Giữ trong body | cần bổ sung |
| Sentence viết tắt/decimal/inline math | test_a3_leaves.py::test_protected_inline_content_and_abbreviations | Không cắt bên trong vùng bảo vệ | covered A3 |
| URL | test_a3_leaves.py::test_url_terminal_period_still_ends_sentence | Dấu kết câu ngoài URL vẫn tách | covered A3 |
| Formula, figure/table | test_a3_leaves.py::test_block_formula_and_figure_order_and_caption | Đúng leaf/order; caption không lặp sentence | covered A3 |
| Source spans xuyên file | test_a3_leaves.py::test_block_spans_across_input_files_and_fake_delimiter_in_command | Giữ provenance và delimiter thật | covered A3 |
| Dedup xuyên version/khác parent | test_parser_dedup.py::test_dedup_shares_content_under_different_parents | Share element, giữ cạnh | covered C2 với tree tổng hợp |
| Repeated content trong version | test_parser_dedup.py::test_dedup_reuses_content_across_versions_but_not_within_one | Không collapse occurrence trong version | covered C2 |
| Remap trước dedup | test_parser_pipeline.py::test_pipeline_dedups_elements_after_citation_remap | Nội dung canonical trước C2 | orchestration với fake B |
| Partial/pending | test_parser_pipeline.py::test_ready_tree_none_bibtex_not_exported | Không xuất bibliography của tree chưa finalize | covered C3 |
| Resume/input thay đổi | test_parser_pipeline.py::test_input_fingerprint_changes_when_content_changes | Reprocess khi nội dung đổi | covered C3 |
| B1 finalized tree thật | test_d1_acceptance.py::test_d1_real_pipeline_can_finalize_content | Tree khác None | strict xfail; B chưa tích hợp |
| Thứ tự cây published | test_d1_acceptance.py::test_d1_published_hierarchy_can_preserve_source_order | Có position | strict xfail; đề xuất schema chờ C |
| Bibitem/.bib thực, normalization, version-aware key remap/conflicts | Chưa có implementation B trong checkout | Chốt Q13–Q17, bổ sung fixture/expected entries | blocked; không coi fake test là pass |

## Chạy và nghiệm thu

Trong Linux/WSL, từ project root với dependencies đã cài:

```bash
python -m pytest -q tests/test_a1_gather.py tests/test_a2_hierarchy.py tests/test_a3_leaves.py tests/test_d1_acceptance.py tests/test_parser_contract.py tests/test_parser_adapter.py tests/test_parser_dedup.py tests/test_parser_integration.py tests/test_parser_pipeline.py tests/test_parser_validation.py
```

Chạy lại lệnh để kiểm tra độc lập giữa các lần chạy. Test D1 mới chặn socket network trong phạm vi file; các test khác dùng input tổng hợp hoặc fake services, không cần Drive. Full regression: `python -m pytest -q`. Windows cần quyền symlink; crawler dùng fcntl nên môi trường nghiệm thu chính là Linux. Không skip lỗi môi trường rồi báo full suite pass.

D1 chưa hoàn tất: cần fixture acknowledgments, acceptance B thật, test pipeline thật và kiểm nghiệm Linux đầy đủ. Khi tích hợp B, strict xfail có thể chuyển thành failure/XPASS: cập nhật theo contract, không xóa assertion chỉ để suite xanh.
