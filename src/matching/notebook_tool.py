"""One shared notebook workflow: users fill forms, not Python configuration."""

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import re

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .auto_matcher import AUTO_LABEL_SCHEMA
from .candidate_pool import build_candidate_pool
from .dataset_plan import personal_manual_plan, _write_plan
from .labeling import pool_fingerprint, _exclusive_progress
from .labeling_table import TableSession, LabelingTable, _text, _close_widgets


@dataclass
class AnnotationConfig:
    task: str = 'personal'
    member: str = ''
    reviewer: str = ''
    bibtex: str = ''
    references: str = ''
    output: str = ''
    plan: str = ''
    doi: str = ''
    auto_run: str = ''
    checkpoint: str = ''
    reference_snapshots: str = ''
    publications: str = ''
    excluded: str = ''
    seed: int = 42
    page_size: int = 10


@dataclass
class PreparedAnnotation:
    pool: object
    progress: Path
    reviewer: str
    publications: list | None
    entry_keys: list | None
    doi: object
    plan_path: Path | None
    notes: list
    temporary: object = None


def _ids(value):
    return [item for item in re.split(r'[\s,;]+', value.strip()) if item]


def _file(value, label, *, optional=False, table=False):
    if not value or not str(value).strip():
        if optional:
            return None
        raise ValueError(f'Chưa chọn: {label}')
    if str(value).strip().lower().startswith(('https://', 'http://')):
        raise ValueError(f'{label}: chọn đường dẫn trong Files, không dán link website Drive')
    path = Path(str(value).strip()).expanduser()
    if not (path.is_file() or (table and path.is_dir())):
        raise FileNotFoundError(f'{label}: không tìm thấy {path}')
    return path


def prepare_annotation(config, *, group_members=4):
    """Load and validate a personal/existing/review session without making labels."""
    if config.task == 'demo':
        import tempfile
        temporary = tempfile.TemporaryDirectory(prefix='m3-form-demo-')
        pool = build_candidate_pool(pa.Table.from_pylist([
            dict(arxiv_id='demo-paper', citation_key='a', title='Methods for Reference Matching',
                 authors=['Jane Doe'], year=2024),
            dict(arxiv_id='demo-paper', citation_key='b', title='Another Reference'),
            dict(arxiv_id='demo-empty', citation_key='c', title='No candidates')]),
            pa.Table.from_pylist([
                dict(arxiv_id='demo-paper', ref_arxiv_id='2401.00001',
                     ref_title='Methods for Reference Matching', ref_authors=['Jane Doe'],
                     ref_submission_date='2024-01-02'),
                dict(arxiv_id='demo-paper', ref_arxiv_id='2402.00002',
                     ref_title='Different Subject', ref_authors=['Other Person'])]))
        return PreparedAnnotation(pool, Path(temporary.name) / 'demo.parquet', 'demo', None, None,
            pa.table({'arxiv_id': ['2401.00001'], 'doi': ['10.1234/example']}), None,
            ['Demo dùng dữ liệu giả và thư mục tạm, không ghi vào tiến độ thật.'], temporary)
    if config.task not in ('personal', 'existing', 'review'):
        raise ValueError('Chọn một công việc hợp lệ')
    for label, value in [('Thành viên', config.member), ('Mã người gán nhãn', config.reviewer)]:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise ValueError(f'{label}: chọn/điền mã không dấu, không khoảng trắng')
    if type(config.page_size) is not int or not 1 <= config.page_size <= 50:
        raise ValueError('Số dòng mỗi trang phải từ 1 đến 50')
    if type(config.seed) is not int:
        raise ValueError('Seed phải là số nguyên')
    bib = _file(config.bibtex, 'BibTeX M2', table=True)
    refs = _file(config.references, 'References M1', table=True)
    if not config.output.strip():
        raise ValueError('Chưa chọn thư mục lưu nhãn')
    if config.output.strip().lower().startswith(('http://', 'https://')):
        raise ValueError('Nơi lưu nhãn phải là thư mục, không phải link Drive')
    output = Path(config.output.strip()).expanduser()
    if output.exists() and not output.is_dir():
        raise ValueError('Nơi lưu nhãn phải là thư mục, không phải file')
    doi = _file(config.doi, 'DOI bổ sung', optional=True, table=True)
    plan_path = _file(config.plan, 'Kế hoạch gốc', optional=config.task != 'existing')
    checkpoint_path = _file(config.checkpoint, 'Checkpoint M2', optional=True) if config.task == 'personal' else None
    snapshots_path = _file(config.reference_snapshots, 'Reference snapshots M1', optional=True, table=True) if config.task == 'personal' else None
    auto_path = _file(config.auto_run, 'Thư mục auto', optional=True, table=True) if config.task == 'review' else None
    if config.task == 'personal' and plan_path is not None:
        raise ValueError('Đã chọn kế hoạch gốc: dùng công việc Tiếp tục manual cũ, không tạo mẫu mới')
    pool = build_candidate_pool(bib, refs)
    fingerprint = pool_fingerprint(pool)
    plan = json.loads(plan_path.read_text(encoding='utf-8')) if plan_path else None
    if plan is not None and plan.get('candidate_pool_sha256') != fingerprint:
        raise ValueError('Dữ liệu khác snapshot của kế hoạch. Chọn đúng bảng gốc; không bỏ kiểm tra fingerprint')
    notes = []
    if config.task in ('personal', 'existing'):
        progress = output / 'manual' / f'{config.member}.parquet'
        if config.task == 'personal':
            plan_path = output / 'plans' / f'{config.member}.json'
            plan = personal_manual_plan(pool, plan_path, member=config.member, reviewer=config.reviewer,
                seed=config.seed, group_members=group_members,
                publications=_ids(config.publications) or None,
                excluded_publications=_ids(config.excluded), progress=progress,
                checkpoint=json.loads(checkpoint_path.read_text(encoding='utf-8')) if checkpoint_path else None,
                snapshots=pq.read_table(snapshots_path) if snapshots_path else None)
            notes.extend(plan['warnings'])
        assigned = [a for a in plan['manual_assignments'] if a['owner'] == config.member]
        if not assigned:
            raise ValueError('Thành viên không có phân công trong kế hoạch đã chọn')
        publications = [a['arxiv_id'] for a in assigned]
        keys = [(a['arxiv_id'], k) for a in assigned for k in a['citation_keys']]
    else:
        if auto_path is None or not auto_path.is_dir():
            raise ValueError('Chưa chọn đúng thư mục kết quả auto cần review')
        for name in ('_SUCCESS', 'summary.json', 'auto_labels.parquet'):
            if not (auto_path / name).is_file():
                raise ValueError(f'Thư mục auto thiếu {name}; chưa hoàn tất hoặc chọn sai thư mục')
        report = json.loads((auto_path / 'summary.json').read_text(encoding='utf-8'))
        if report.get('candidate_pool_sha256') != fingerprint:
            raise ValueError('Kết quả auto thuộc snapshot khác dữ liệu đã chọn')
        table = pq.read_table(auto_path / 'auto_labels.parquet')
        if not table.schema.equals(AUTO_LABEL_SCHEMA):
            raise ValueError('Schema auto labels không đúng')
        queue = table.filter(pc.equal(table['label_status'], 'needs_review')).to_pylist()
        if not queue:
            raise ValueError('Lần chạy auto này không còn entry needs_review')
        keys = [(r['arxiv_id'], r['citation_key']) for r in queue]
        publications = sorted({r['arxiv_id'] for r in queue})
        progress = output / 'review' / auto_path.name / f'{config.reviewer}.parquet'
        notes.append('Review lần chạy: ' + auto_path.name)
    # Validate existing progress before a configuration is accepted by the form.
    session = TableSession(pool, progress, reviewer=config.reviewer,
                            publications=publications, entry_keys=keys, papers=doi)
    if any(r['reviewer'] != config.reviewer for r in session.labels.values()):
        notes.append('File có nhãn mang mã reviewer khác trước đây; giữ nguyên nguồn của nhãn cũ. '
                     'Kiểm tra đúng thành viên/nơi lưu trước khi sửa.')
    return PreparedAnnotation(pool, progress, config.reviewer, publications, keys, doi, plan_path, notes)


class PathPicker:
    """Browse one directory at a time; never scan a whole Drive recursively."""

    def __init__(self, field, *, start, kind='file'):
        import ipywidgets as w
        self.field, self.kind = field, kind
        self.location = w.Text(value=str(start), description='Thư mục:', layout=w.Layout(width='95%'))
        self.items = w.Select(options=[], rows=9, layout=w.Layout(width='95%'))
        self.message = w.HTML()
        go, up, open_item, select, cancel = [w.Button(description=x) for x in
            ('Đi đến', 'Lên một cấp', 'Mở thư mục', 'Dùng lựa chọn', 'Đóng')]
        go.on_click(lambda _: self.refresh())
        up.on_click(lambda _: self._go(Path(self.location.value).parent))
        open_item.on_click(lambda _: self._open())
        select.on_click(lambda _: self._select())
        cancel.on_click(lambda _: self.close())
        self.root = w.VBox([w.HTML('Chọn thư mục để xem nội dung, sau đó chọn file/thư mục cần dùng. '
            'Có thể dán đường dẫn trực tiếp vào biểu mẫu, không cần duyệt.'), self.location,
            w.HBox([go, up, open_item, select, cancel]), self.items, self.message])
        self.refresh()

    def _go(self, path):
        self.location.value = str(path)
        self.refresh()

    def refresh(self):
        from itertools import islice
        try:
            path = Path(self.location.value).expanduser()
            if not path.is_dir():
                raise ValueError('Đường dẫn duyệt phải là thư mục')
            children = list(islice(path.iterdir(), 501))
            limited = len(children) > 500
            choices = []
            for child in children[:500]:
                if child.is_dir():
                    choices.append(('📁 ' + child.name, str(child)))
                elif self.kind != 'directory' and child.suffix.lower() in ('.json', '.parquet'):
                    choices.append(('📄 ' + child.name, str(child)))
            self.items.options = sorted(choices, key=lambda item: item[0].casefold())
            self.message.value = ('Chỉ hiện 500 mục đầu. Dán đường dẫn file trực tiếp nếu chưa thấy.'
                                  if limited else 'Chọn mục rồi bấm Dùng lựa chọn; Mở thư mục để đi sâu hơn.')
        except (OSError, ValueError) as exc:
            self.items.options = []
            self.message.value = _text(str(exc))

    def _open(self):
        if self.items.value and Path(self.items.value).is_dir():
            self._go(Path(self.items.value))

    def _select(self):
        path = Path(self.items.value) if self.items.value else Path(self.location.value)
        if self.kind == 'directory' and not path.is_dir():
            self.message.value = 'Mục này yêu cầu chọn thư mục.'
            return
        if self.kind == 'file' and not path.is_file():
            self.message.value = 'Mục này yêu cầu chọn file, không phải thư mục.'
            return
        self.field.value = str(path)
        self.close()

    def close(self):
        _close_widgets([self.root])


class AnnotationNotebook:
    """Configuration form and labeling table owned by one live notebook controller."""

    def __init__(self, *, group_members=4, start_directory=None):
        import ipywidgets as w
        self.group_members = group_members
        self.start_directory = Path(start_directory or Path.cwd())
        self.table = self.prepared = self.config = self.picker = None
        self.temporary = None
        self.closed = False
        self.fields = {}
        self.task = w.Dropdown(description='Công việc:', options=[
            ('Bắt đầu manual trên dữ liệu riêng', 'personal'),
            ('Tiếp tục manual với kế hoạch gốc', 'existing'),
            ('Review kết quả auto', 'review'), ('Thử giao diện bằng dữ liệu giả', 'demo')],
            layout=w.Layout(width='95%'), style={'description_width': '170px'})
        self.member = w.Dropdown(description='Người thực hiện:', options=[('Chọn thành viên…', ''),
            ('Hiếu — member1', 'member1'), ('Anh — member2', 'member2'),
            ('Kiên — member3', 'member3'), ('Thành viên thứ tư — member4', 'member4')],
            layout=w.Layout(width='95%'), style={'description_width': '170px'})
        self.reviewer = w.Text(description='Mã người gán nhãn:', placeholder='hieu / anh / kien / mã người thứ tư',
            layout=w.Layout(width='95%'), style={'description_width': '170px'})
        self.member.observe(self._member_changed, names='value')
        self.task.observe(self._task_changed, names='value')
        self.reviewer.observe(lambda _: self._invalidate(), names='value')
        self.path_rows = {}
        for name, label, kind in [
            ('bibtex', 'BibTeX M2', 'table'), ('references', 'References M1', 'table'),
            ('output', 'Thư mục lưu nhãn', 'directory'), ('plan', 'Kế hoạch gốc JSON', 'file'),
            ('doi', 'DOI bổ sung (tùy chọn)', 'table'), ('auto_run', 'Thư mục kết quả auto', 'directory'),
            ('checkpoint', 'Checkpoint M2 (tùy chọn)', 'file'),
            ('reference_snapshots', 'Reference snapshots (tùy chọn)', 'table')]:
            field = w.Text(description=label + ':', placeholder='Dán Copy path hoặc bấm Chọn…',
                layout=w.Layout(width='85%'), style={'description_width': '220px'})
            field.observe(lambda _: self._invalidate(), names='value')
            button = w.Button(description='Chọn…', layout=w.Layout(width='80px'))
            button.on_click(lambda _, f=field, k=kind: self._browse(f, k))
            self.fields[name] = field
            self.path_rows[name] = w.HBox([field, button])
        self.publications = w.Textarea(description='5 bài tự chọn:', placeholder='Để trống để tự chọn theo seed; hoặc nhập đúng 5 ID',
            layout=w.Layout(width='95%', height='65px'), style={'description_width': '220px'})
        self.excluded = w.Textarea(description='Bài cần loại:', placeholder='ID đã thuộc auto hoặc phần việc khác; cách nhau bằng dấu phẩy/xuống dòng',
            layout=w.Layout(width='95%', height='65px'), style={'description_width': '220px'})
        self.seed = w.IntText(value=42, description='Seed:')
        self.page_size = w.BoundedIntText(value=10, min=1, max=50, description='Dòng/trang:')
        for control in (self.publications, self.excluded, self.seed, self.page_size):
            control.observe(lambda _: self._invalidate(), names='value')
        self.personal_options = w.VBox([self.publications, self.excluded,
            self.path_rows['checkpoint'], self.path_rows['reference_snapshots'], self.seed])
        advanced = w.Accordion(children=[w.VBox([self.path_rows['doi'], self.personal_options, self.page_size])])
        advanced.set_title(0, 'DOI và tùy chọn nâng cao — có thể để trống')
        advanced.selected_index = None
        self.save_profile = w.Checkbox(value=True, description='Lưu cấu hình tại thư mục nhãn để mở lại lần sau')
        self.profile = w.Text(description='File cấu hình đã lưu:', placeholder='Tùy chọn: .../notebook_profiles/memberX.json',
            layout=w.Layout(width='85%'), style={'description_width': '220px'})
        choose_profile = w.Button(description='Chọn…', layout=w.Layout(width='80px'))
        choose_profile.on_click(lambda _: self._browse(self.profile, 'file'))
        load = w.Button(description='Nạp cấu hình')
        load.on_click(lambda _: self._load_profile())
        self.open_button = w.Button(description='Kiểm tra & mở bảng', button_style='primary',
                                     layout=w.Layout(width='210px'))
        self.open_button.on_click(lambda _: self.open())
        self.status = w.HTML()
        self.explanation = w.HTML()
        self.browser_area = w.VBox()
        self.table_area = w.VBox()
        self.report_area = w.VBox()
        self.root = w.VBox([w.HTML('<h3>Thiết lập phiên gán nhãn</h3>'
            'Điền thông tin trong biểu mẫu; không sửa các cell Python. Các ô dữ liệu đều để trống.'),
            w.HBox([self.profile, choose_profile, load]), self.task, self.member, self.reviewer, self.explanation,
            self.path_rows['bibtex'], self.path_rows['references'], self.path_rows['output'],
            self.path_rows['plan'], self.path_rows['auto_run'], advanced, self.save_profile,
            self.open_button, self.status, self.browser_area, self.table_area, self.report_area])
        self._task_changed(None)

    def _invalidate(self):
        if self.table is not None and not self.table.closed:
            self.table.close()
            self.table_area.children = ()
            self.status.value = 'Cấu hình đã đổi. Bấm Kiểm tra & mở bảng để áp dụng; nhãn cũ đã lưu được giữ.'
        self.prepared = None

    def _member_changed(self, change):
        self.reviewer.value = {'member1': 'hieu', 'member2': 'anh', 'member3': 'kien'}.get(change['new'], '')
        self._invalidate()

    def _task_changed(self, _):
        task = self.task.value
        self._invalidate()
        self.path_rows['plan'].layout.display = '' if task == 'existing' else 'none'
        self.path_rows['auto_run'].layout.display = '' if task == 'review' else 'none'
        self.personal_options.layout.display = '' if task == 'personal' else 'none'
        for name in ('bibtex', 'references', 'output'):
            self.path_rows[name].layout.display = 'none' if task == 'demo' else ''
        self.explanation.value = {
            'personal': 'Người mới/data riêng: chọn hai bảng và nơi lưu nhãn. Tạo 5 bài × 20 entry, '
                        'lần sau nạp lại kế hoạch cá nhân. Chọn manual trước khi chạy auto.',
            'existing': 'Đã có nhãn: chọn kế hoạch gốc và thư mục cha chứa manual/ trước đây. '
                        'Giữ đúng mã thành viên. Không tạo lại mẫu 5 bài.',
            'review': 'Chọn đúng dữ liệu của lần chạy auto và thư mục auto có _SUCCESS. '
                      'Không cần kế hoạch manual. Review được lưu riêng theo lần chạy.',
            'demo': 'Không cần điền đường dẫn hoặc tên. Demo không ghi vào tiến độ thật.',
        }[task]

    def _browse(self, field, kind):
        if self.picker:
            self.picker.close()
        entered = Path(field.value).expanduser() if field.value.strip() else self.start_directory
        start = entered if entered.is_dir() else entered.parent
        if not start.is_dir():
            start = self.start_directory
        self.picker = PathPicker(field, start=start, kind=kind)
        self.browser_area.children = (self.picker.root,)

    def current_config(self):
        task = self.task.value
        data = {name: field.value.strip() for name, field in self.fields.items()}
        # Hidden controls from a different task must never affect this one.
        if task != 'existing':
            data['plan'] = ''
        if task != 'review':
            data['auto_run'] = ''
        if task != 'personal':
            data['checkpoint'] = data['reference_snapshots'] = ''
        return AnnotationConfig(task=task, member=self.member.value, reviewer=self.reviewer.value.strip(),
            publications=self.publications.value, excluded=self.excluded.value,
            seed=self.seed.value, page_size=self.page_size.value, **data)

    def _save_profile(self, config):
        path = Path(config.output) / 'notebook_profiles' / f'{config.member}.json'
        with _exclusive_progress(path):
            _write_plan(path, dict(schema_version=1, configuration=asdict(config)))
        self.profile.value = str(path)
        return path

    def _load_profile(self):
        try:
            path = _file(self.profile.value, 'File cấu hình')
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('schema_version') != 1 or not isinstance(data.get('configuration'), dict):
                raise ValueError('File không phải cấu hình của công cụ gán nhãn')
            config = AnnotationConfig(**data['configuration'])
            if config.task not in ('personal', 'existing', 'review', 'demo'):
                raise ValueError('Công việc trong cấu hình không hợp lệ')
            if config.member not in ('', 'member1', 'member2', 'member3', 'member4'):
                raise ValueError('Mã thành viên không thuộc nhóm 4 người')
            self.task.value, self.member.value = config.task, config.member
            self.reviewer.value = config.reviewer
            for name, field in self.fields.items():
                field.value = getattr(config, name)
            self.publications.value, self.excluded.value = config.publications, config.excluded
            self.seed.value, self.page_size.value = config.seed, config.page_size
            self.status.value = 'Đã nạp cấu hình. Kiểm tra tên/đường dẫn rồi bấm Kiểm tra & mở bảng.'
        except Exception as exc:
            self.status.value = '<b>Chưa nạp được cấu hình:</b> ' + _text(str(exc))

    def open(self):
        self._invalidate()
        self.open_button.disabled = True
        self.status.value = 'Đang kiểm tra file và dựng candidate pool…'
        try:
            config = self.current_config()
            prepared = prepare_annotation(config, group_members=self.group_members)
            session = TableSession(prepared.pool, prepared.progress, reviewer=prepared.reviewer,
                publications=prepared.publications, entry_keys=prepared.entry_keys, papers=prepared.doi)
            table = LabelingTable(session, page_size=config.page_size)
            self.table, self.prepared, self.config = table, prepared, config
            self.temporary = prepared.temporary
            self.table_area.children = (table.root,)
            message = '<b>Đã mở bảng.</b> Tiến độ: ' + _text(str(prepared.progress))
            if prepared.plan_path:
                message += '<br>Kế hoạch: ' + _text(str(prepared.plan_path))
            message += ''.join('<br>Lưu ý: ' + _text(note) for note in prepared.notes)
            if self.save_profile.value and config.task != 'demo':
                try:
                    message += '<br>Cấu hình lần sau: ' + _text(str(self._save_profile(config)))
                except Exception as exc:
                    message += '<br>Không lưu được cấu hình phụ: ' + _text(str(exc))
            self.status.value = message
        except Exception as exc:
            self.status.value = '<b>Chưa mở được bảng:</b> ' + _text(str(exc))
        finally:
            self.open_button.disabled = False

    def show_progress(self):
        import ipywidgets as w
        if self.table is None or self.prepared is None:
            self.report_area.children = (w.HTML('Điền biểu mẫu và bấm Kiểm tra & mở bảng trước.'),)
            return
        try:
            self.table.session.refresh()
            rows = self.table.session.progress_rows()
            html = ('<h4>Tiến độ đã lưu</h4><table><tr><th>Bài báo</th><th>Xác nhận</th>'
                    '<th>Được chọn</th><th>Chưa gán</th></tr>' + ''.join(
                f'<tr><td>{_text(r["arxiv_id"])}</td><td>{r["confirmed"]}</td>'
                f'<td>{r["selected_entries"]}</td><td>{r["pending"]}</td></tr>' for r in rows) + '</table>')
            html += f'<p>Tổng xác nhận: {sum(r["confirmed"] for r in rows)}/{len(self.table.session.keys)}</p>'
            if self.config.task in ('personal', 'existing'):
                html += f'<p>Bài đạt ≥20 nhãn: {sum(r["confirmed"] >= 20 for r in rows)}/{len(rows)}</p>'
            self.report_area.children = (w.HTML(html),)
        except Exception as exc:
            self.report_area.children = (w.HTML('<b>Lỗi đọc tiến độ:</b> ' + _text(str(exc))),)

    def finish(self):
        import ipywidgets as w
        if self.table is None or self.prepared is None:
            self.report_area.children = (w.HTML('Chưa mở bảng; không có phiên để kết thúc.'),)
            return
        self.show_progress()
        self.table.close()
        message = '<b>Đã đóng bảng.</b> Nhãn đã chọn được lưu ngay, không cần chạy auto lại.'
        if self.prepared.progress.is_file():
            with pq.ParquetFile(self.prepared.progress) as file:
                message += f'<br>Số nhãn trong file: {file.metadata.num_rows}'
            message += '<br>File gửi người tổng hợp: ' + _text(str(self.prepared.progress))
            if self.prepared.plan_path:
                message += '<br>Kế hoạch gửi kèm: ' + _text(str(self.prepared.plan_path))
        else:
            message += '<br>Chưa có quyết định xác nhận, nên chưa tạo file nhãn.'
        self.status.value = message

    def close(self):
        if self.table:
            self.table.close()
        if self.picker:
            self.picker.close()
        _close_widgets([self.root])
        self.closed = True
