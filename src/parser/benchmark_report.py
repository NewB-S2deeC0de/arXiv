"""Render a portable Markdown results section from measured D2 artifacts."""
import argparse
import hashlib
import json
from pathlib import Path


def write_report(run_root, output):
    root, output = Path(run_root), Path(output)
    summary_path, environment_path = root / 'summary.json', root / 'environment.json'
    summary = json.loads(summary_path.read_text(encoding='utf8'))
    environment = json.loads(environment_path.read_text(encoding='utf8'))

    def value(item):
        if item is None:
            return 'null (không có số đo)'
        if isinstance(item, float):
            return f'{item:.6g}'
        return str(item).replace('|', '\\|').replace('\n', ' ')

    rows = [(key, summary.get(key)) for key in (
        'status', 'acceptance', 'group_data_complete', 'papers_processed', 'wall_seconds',
        'seconds_per_publication', 'observed_peak_rss_bytes', 'parsed_output_bytes',
        'unresolved_citations', 'bibliography_conflicts', 'bibliography_conflict_fields')]
    for group in ('versions', 'elements', 'bibtex', 'unique_elements_by_kind'):
        rows.extend((f'{group}.{key}', item) for key, item in summary.get(group, {}).items())
    lines = ['# M2 — Kết quả benchmark', '',
             'Bảng dưới được sinh từ artifact của run; completed không đồng nghĩa acceptance pass.', '',
             f"Commit đo: `{environment.get('commit')}`; dirty: `{environment.get('dirty')}`.",
             f"Bắt đầu (UTC): `{environment.get('started_at')}`.",
             f"Platform: {value(environment.get('platform'))}; Python: {environment.get('python')}.",
             f"Giới hạn publication: `{environment.get('limit')}`; include_partial: `{environment.get('include_partial')}`.",
             '', '| Chỉ số | Giá trị |', '|---|---:|']
    lines.extend(f'| {key} | {value(item)} |' for key, item in rows)
    lines.extend(['', '## Đơn vị và giới hạn', '', summary.get('measurement_note', '')])
    lines.extend(f'- `{key}`: {unit}.' for key, unit in summary.get('quality_units', {}).items())
    lines.extend(['', 'Các metric citation này là distinct key theo publication, không phải số lần cite. '
                  'Không cộng RSS peak giữa các run; không lấy trung bình các mean không có trọng số. '
                  'Run blocked/failed cần được báo cùng lỗi và coverage.', '',
                  '## Định danh artifact', ''])
    for path in (summary_path, environment_path):
        lines.append(f'- `{path.name}` SHA-256: `{hashlib.sha256(path.read_bytes()).hexdigest()}`.')
    if summary.get('dataset_snapshot'):
        lines.append(f"- Snapshot fingerprint: `{summary['dataset_snapshot']['fingerprint']}`.")
        lines.append(f"- Số thư mục runtime đầu vào: {len(summary['dataset_snapshot']['members'])}.")
    lines.extend(['', 'Bảo quản cùng run: `resources.csv`, `parsed/manifest.json`, '
                  '`parsed/coverage.csv`, `parsed/bibtex_audit.json` và `snapshot.json` của input.', ''])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('\n'.join(lines), encoding='utf8')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    write_report(args.run, args.out)


if __name__ == '__main__':
    main()
