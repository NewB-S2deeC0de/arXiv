"""Searchable notebook labeling table; shares CLI progress without changing the pool."""

from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote, urlsplit

from .auto_matcher import _paper_dois, _reference_doi, normalize_text
from .labeling import _exclusive_progress, _load_labels, _save_labels, pool_fingerprint


class TableSession:
    """Annotation state, with short write locks and conflict checks per entry.

    DOI enrichment is presentation only: it never changes pool fingerprints or
    adds/removes/ranks candidates. Legacy CLI labels remain readable unchanged.
    """

    def __init__(self, pool, progress, *, reviewer, publications=None, entry_keys=None,
                 papers=None):
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError('Điền REVIEWER trước khi mở bảng gán nhãn')
        self.reviewer = reviewer.strip()
        self.progress = Path(progress)
        self.fingerprint = pool_fingerprint(pool)
        self.entries = {(r['arxiv_id'], r['citation_key']): r for r in pool.entries.to_pylist()}
        self.candidates = {}
        paper_dois, warning = _paper_dois(papers)
        self.doi_warning = warning.replace('Bảng papers', 'Bảng DOI bổ sung') if warning and papers is not None else None
        self.dois = {}
        for row in pool.references.to_pylist():
            self.candidates.setdefault(row['arxiv_id'], []).append(row)
            raw, values, sources, invalid = _reference_doi(row, paper_dois)
            self.dois[(row['arxiv_id'], row['ref_arxiv_id'])] = {
                'raw': raw, 'values': values, 'sources': sources, 'invalid': invalid}
        self.candidate_ids = {p: {r['ref_arxiv_id'] for r in rows}
                              for p, rows in self.candidates.items()}
        available = {key[0] for key in self.entries}
        selected = available if publications is None else set(publications)
        if selected - available:
            raise ValueError('Phân công chứa publication không có trong BibTeX')
        keys = [k for k in sorted(self.entries) if k[0] in selected]
        if entry_keys is not None:
            requested = {tuple(k) for k in entry_keys}
            if requested - set(keys):
                raise ValueError('Phân công chứa entry không có trong các publication đã chọn')
            keys = [k for k in keys if k in requested]
        if not keys:
            raise ValueError('Không có entry được chọn; kiểm tra MEMBER hoặc review queue')
        self.keys = keys
        self.labels = {}
        self.refresh()
        self.index = next((i for i, key in enumerate(keys) if key not in self.labels), 0)

    @property
    def key(self):
        return self.keys[self.index]

    def refresh(self):
        with _exclusive_progress(self.progress):
            self.labels = _load_labels(self.progress, self.fingerprint,
                                       self.entries, self.candidate_ids)

    def visible_candidates(self, query=''):
        """All search tokens must occur somewhere in the displayed metadata."""
        tokens = normalize_text(query).split()
        rows = self.candidates.get(self.key[0], [])
        if not tokens:
            return list(rows)
        result = []
        for row in rows:
            doi = self.dois[(self.key[0], row['ref_arxiv_id'])]
            haystack = normalize_text(' '.join([
                row['ref_arxiv_id'], row.get('ref_title') or '',
                ' '.join(row.get('ref_authors') or []),
                row.get('ref_submission_date') or '', *doi['raw'], *doi['values']]))
            if all(token in haystack for token in tokens):
                result.append(row)
        return result

    def save(self, target=None, *, clear=False, expected_key=None):
        key = self.key
        if expected_key is not None and key != expected_key:
            raise RuntimeError('Entry đã đổi; hãy chọn ở bảng hiện tại')
        if target is not None and target not in self.candidate_ids.get(key[0], set()):
            raise ValueError('Ứng viên không thuộc publication hiện tại')
        with _exclusive_progress(self.progress):
            current = _load_labels(self.progress, self.fingerprint,
                                   self.entries, self.candidate_ids)
            if current.get(key) != self.labels.get(key):
                raise RuntimeError('Entry đã được sửa ở phiên khác. Bấm Nạp lại tiến độ trước.')
            if clear:
                current.pop(key, None)
            else:
                current[key] = dict(arxiv_id=key[0], citation_key=key[1],
                    groundtruth_arxiv_id=target,
                    label_status='no_match' if target is None else 'matched',
                    reviewer=self.reviewer, updated_at=datetime.now(timezone.utc).isoformat())
            _save_labels(self.progress, current, self.fingerprint)
            self.labels = current

    def next_pending(self):
        for offset in range(1, len(self.keys) + 1):
            index = (self.index + offset) % len(self.keys)
            if self.keys[index] not in self.labels:
                self.index = index
                return True
        return False

    def progress_rows(self):
        totals = Counter(k[0] for k in self.keys)
        done = Counter(k[0] for k in self.keys if k in self.labels)
        return [dict(arxiv_id=p, selected_entries=totals[p], confirmed=done[p],
                     pending=totals[p] - done[p]) for p in sorted(totals)]


def _text(value):
    return escape(str(value)) if value is not None and value != '' else '[thiếu dữ liệu]'


def _link(url, label):
    try:
        allowed = isinstance(url, str) and urlsplit(url).scheme.lower() in ('https', 'http')
    except ValueError:
        allowed = False
    if not allowed:
        return _text(url)
    return (f'<a href="{escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{escape(label)}</a>')


def _authors(values):
    if not values:
        return '[thiếu dữ liệu]'
    if len(values) <= 3:
        return _text('; '.join(values))
    return (f'<details><summary>{_text("; ".join(values[:3]))} '
            f'(+{len(values) - 3})</summary>{_text("; ".join(values))}</details>')


def entry_html(session):
    row = session.entries[session.key]
    old = session.labels.get(session.key)
    label = ('Chưa gán nhãn' if not old else
             'Không có match' if old['label_status'] == 'no_match' else
             'Match → ' + old['groundtruth_arxiv_id'])
    return f'''<section class="m3-entry" style="border:1px solid #a7b4c8;padding:14px;border-radius:8px">
    <b>ENTRY CẦN GÁN NHÃN · {_text(session.key[0])}</b>
    <h3 style="margin:8px 0">{_text(row['title'])}</h3>
    <div><b>Tác giả:</b> {_authors(row['authors'])}</div>
    <div><b>Năm:</b> {_text(row['year'])} · <b>DOI:</b> {_text(row['doi'])}</div>
    <div><b>Tạp chí:</b> {_text(row['journal'])} · <b>URL:</b> {_link(row['url'], 'Mở nguồn')}</div>
    <details><summary>Khóa trích dẫn</summary>{_text(session.key[1])}</details>
    <div style="margin-top:8px"><b>Nhãn đã lưu:</b> {_text(label)}</div></section>'''


def candidate_cells_html(session, row):
    """One compact HTML table row, escaped before display."""
    identifier = row['ref_arxiv_id']
    info = session.dois[(session.key[0], identifier)]
    doi_text = '<br>'.join(_text(v) for v in info['values']) or '[thiếu dữ liệu]'
    if info['invalid'] or len(info['values']) > 1:
        doi_text += '<br><b>DOI cần kiểm tra</b>'
    if info['raw'] and info['raw'] != info['values']:
        doi_text += ('<details><summary>Giá trị gốc</summary>'
                     + '<br>'.join(_text(v) for v in info['raw']) + '</details>')
    if info['sources']:
        names = {'papers.doi': 'Bảng DOI bổ sung', 'references.ref_doi': 'References gốc'}
        doi_text += '<br><small>Nguồn: ' + _text(', '.join(names.get(v, v) for v in info['sources'])) + '</small>'
    date = row.get('ref_submission_date')
    year = date[:4] if isinstance(date, str) and len(date) >= 4 else None
    arxiv = _link('https://arxiv.org/abs/' + quote(identifier, safe='/'), identifier)
    return f'''<table style="width:100%;table-layout:fixed;border-collapse:collapse"><colgroup>
    <col style="width:13%"><col style="width:33%"><col style="width:24%">
    <col style="width:6%"><col style="width:24%"></colgroup><tbody><tr>
    <td>{arxiv}</td><td><b>{_text(row['ref_title'])}</b></td>
    <td>{_authors(row['ref_authors'])}</td><td>{_text(year)}</td><td>{doi_text}</td>
    </tr></tbody></table>'''


def _close_widgets(roots):
    """Dispose complete replaced subtrees, including layout/style comms."""
    seen = set()
    def close(widget):
        if widget is None or id(widget) in seen:
            return
        seen.add(id(widget))
        for child in getattr(widget, 'children', ()):
            close(child)
        close(getattr(widget, 'layout', None))
        close(getattr(widget, 'style', None))
        widget.close()
    for root in roots:
        close(root)


class LabelingTable:
    """ipywidgets UI, with no predicted labels, score ranking or automatic selection."""

    def __init__(self, session, *, page_size=10):
        import ipywidgets as w
        if type(page_size) is not int or page_size < 1:
            raise ValueError('page_size phải là số nguyên dương')
        self.session, self.page_size = session, page_size
        self.page, self.query, self.closed = 0, '', False
        self._token, self._row_widgets = None, []
        self.status = w.HTML()
        self.header = w.HTML()
        self.entry = w.HTML()
        self.search = w.Text(placeholder='Tìm theo từ trong title, tác giả, arXiv ID, DOI…',
                             layout=w.Layout(width='75%'), continuous_update=False)
        self.search.observe(self._search_changed, names='value')
        self.search_reset = w.Button(description='Xem tất cả')
        self.search_reset.on_click(lambda _: self._reset_search())
        self.table = w.VBox(layout=w.Layout(overflow='auto', width='100%'))
        self.paging = w.HBox()
        self.actions = w.HBox(layout=w.Layout(flex_flow='row wrap'))
        self.navigation = w.VBox()
        self.root = w.VBox([w.HTML('''<style>.m3-labeling td {vertical-align:top;padding:9px 7px;
            overflow-wrap:anywhere;border-bottom:1px solid #b9c3ce}
            .m3-labeling a{color:#1565c0}.m3-labeling small{font-size:11px}</style>'''),
            self.header, self.navigation, self.entry,
            w.HBox([self.search, self.search_reset]), self.table, self.paging,
            self.actions, self.status])
        self.root.add_class('m3-labeling')
        self.render()

    def _search_changed(self, change):
        self.query, self.page = change['new'], 0
        self.render()

    def _reset_search(self):
        if self.search.value:
            self.search.value = ''
        else:
            self.query, self.page = '', 0
            self.render()

    def _run(self, action, token):
        if self.closed or token is not self._token:
            return
        try:
            message = action()
            self.render()
            self.status.value = '<b>' + _text(message or 'Đã cập nhật.') + '</b>'
        except Exception as exc:
            self.status.value = '<b>Lỗi:</b> ' + _text(str(exc))

    def _button(self, description, action, *, disabled=False, style=''):
        import ipywidgets as w
        token = self._token
        button = w.Button(description=description, disabled=disabled, button_style=style,
                          layout=w.Layout(width='auto', min_width='75px'))
        button.on_click(lambda _: self._run(action, token))
        self._row_widgets.append(button)
        return button

    def _decide(self, target, key):
        self.session.save(target, expected_key=key)
        pending = self.session.next_pending()
        self.query, self.page = '', 0
        self.search.value = ''
        return ('Đã lưu và chuyển đến entry chưa gán nhãn.' if pending else
                'Đã lưu. Tất cả entry trong phân công đã có nhãn xác nhận.')

    def _move(self, delta):
        self.session.index = max(0, min(len(self.session.keys) - 1, self.session.index + delta))
        self.query, self.page = '', 0
        self.search.value = ''
        return 'Đã chuyển entry; không thay đổi nhãn.'

    def _pending(self):
        self.session.next_pending()
        self.query, self.page = '', 0
        self.search.value = ''
        return 'Đang hiển thị entry chưa gán nhãn, nếu còn.'

    def _clear(self):
        self.session.save(clear=True, expected_key=self.session.key)
        return 'Đã xóa nhãn; entry trở lại trạng thái chưa gán.'

    def _refresh(self):
        self.session.refresh()
        return 'Đã nạp lại nhãn từ file tiến độ.'

    def _choose_entry(self, change):
        if change['new'] is not None:
            self.session.index = self.session.keys.index(change['new'])
            self.query, self.page = '', 0
            self.search.value = ''
            self.render()

    def _choose_publication(self, change):
        pub = change['new']
        keys = [k for k in self.session.keys if k[0] == pub]
        if keys:
            key = next((k for k in keys if k not in self.session.labels), keys[0])
            self._choose_entry({'new': key})

    def render(self):
        import ipywidgets as w
        if self.closed:
            return
        self._token = object()
        # Old controls no longer act on the next entry; dispose their comms.
        _close_widgets([*self.navigation.children, *self.table.children,
                        *self.paging.children, *self.actions.children])
        self._row_widgets = []
        s = self.session
        progress = s.progress_rows()
        done = sum(r['confirmed'] for r in progress)
        self.header.value = ('<h3>Bảng gán nhãn thủ công</h3>'
            f'<b>{_text(s.reviewer)}</b> · Đã xác nhận {done}/{len(s.keys)} entry · '
            f'Đang xem entry {s.index + 1}/{len(s.keys)}<br>'
            + ' | '.join(f'{_text(r["arxiv_id"])}: {r["confirmed"]}/{r["selected_entries"]}'
                         for r in progress)
            + '<br><small>Năm reference lấy từ ngày phía S2, không khẳng định là năm nộp arXiv.</small>')
        if s.doi_warning:
            self.header.value += '<br><b>Lưu ý DOI:</b> ' + _text(s.doi_warning)
        pubs = w.Dropdown(options=[r['arxiv_id'] for r in progress], value=s.key[0],
                           description='Bài báo:', layout=w.Layout(width='320px'))
        options = []
        for key in s.keys:
            if key[0] == s.key[0]:
                row = s.entries[key]
                state = '✓ ' if key in s.labels else '○ '
                options.append((state + (row['title'] or key[1])[:100], key))
        entries = w.Dropdown(options=options, value=s.key, description='Entry:',
                             layout=w.Layout(width='95%'))
        pubs.observe(self._choose_publication, names='value')
        entries.observe(self._choose_entry, names='value')
        self._row_widgets.extend([pubs, entries])
        self.navigation.children = (pubs, entries)
        self.entry.value = entry_html(s)
        rows = s.visible_candidates(self.query)
        total = len(s.candidates.get(s.key[0], []))
        pages = max(1, (len(rows) + self.page_size - 1) // self.page_size)
        self.page = min(self.page, pages - 1)
        title = w.HTML(f'<b>Ứng viên: {len(rows)}/{total}</b> · '
                      'Lọc theo từ khóa do bạn nhập, không theo dự đoán matcher.')
        header = w.HBox([w.HTML('<b>Chọn</b>', layout=w.Layout(width='85px', flex='0 0 85px')),
            w.HTML('''<table style="width:100%;table-layout:fixed"><colgroup>
            <col style="width:13%"><col style="width:33%"><col style="width:24%">
            <col style="width:6%"><col style="width:24%"></colgroup><tr>
            <th>arXiv</th><th>Tiêu đề</th><th>Tác giả</th><th>Năm</th><th>DOI</th>
            </tr></table>''', layout=w.Layout(flex='1 1 auto'))],
            layout=w.Layout(min_width='900px'))
        items = [title, header]
        for row in rows[self.page * self.page_size:(self.page + 1) * self.page_size]:
            target, key = row['ref_arxiv_id'], s.key
            button = self._button('Chọn', lambda t=target, k=key: self._decide(t, k))
            button.layout.width, button.layout.flex = '85px', '0 0 85px'
            content = w.HTML(candidate_cells_html(s, row), layout=w.Layout(flex='1 1 auto'))
            self._row_widgets.append(content)
            items.append(w.HBox([button, content], layout=w.Layout(min_width='900px')))
        if not rows:
            items.append(w.HTML('Không có kết quả lọc. Bấm Xem tất cả trước khi kết luận không có match.'
                                if self.query.strip() else 'Publication này không có reference ứng viên.'))
        self.table.children = tuple(items)
        self.paging.children = (
            self._button('← Trang', lambda: self._page(-1), disabled=self.page == 0),
            w.HTML(f'Trang {self.page + 1}/{pages}'),
            self._button('Trang →', lambda: self._page(1), disabled=self.page + 1 >= pages))
        self.actions.children = (
            self._button('Không có match', lambda k=s.key: self._decide(None, k),
                         disabled=bool(self.query.strip())),
            self._button('Chưa chắc / Tiếp', lambda: self._move(1)),
            self._button('← Entry', lambda: self._move(-1)),
            self._button('Entry chưa gán', self._pending),
            self._button('Xóa nhãn', self._clear, disabled=s.key not in s.labels),
            self._button('Nạp lại tiến độ', self._refresh))
        self._row_widgets.extend([title, header, *[x for x in items[2:] if isinstance(x, w.HBox)]])
        self.status.value = ('Lọc đang bật: Không có match bị khóa. Bấm Xem tất cả để đối chiếu đầy đủ.'
                             if self.query.strip() else
                             'Chọn một dòng sẽ lưu ngay rồi chuyển entry. Chưa chắc / Tiếp không tạo nhãn.')

    def _page(self, delta):
        self.page += delta
        return 'Đã đổi trang ứng viên; không thay đổi nhãn.'

    def close(self):
        self.closed = True
        _close_widgets([self.root])


def show_labeling_table(pool, progress, *, reviewer, publications=None, entry_keys=None,
                        papers=None, page_size=10):
    """Display a live table and return its controller for progress/export cells."""
    from IPython.display import display
    session = TableSession(pool, progress, reviewer=reviewer, publications=publications,
                           entry_keys=entry_keys, papers=papers)
    table = LabelingTable(session, page_size=page_size)
    display(table.root)
    return table
