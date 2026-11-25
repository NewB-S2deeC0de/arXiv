import pytest
from pathlib import Path
from parser.gather import gather_version, _resolve_root_candidate
from standardization.scanner import scan_latex
from standardization.citations import _KEY


def test_resolve_root_disambiguates_supplement(tmp_path):
    (tmp_path / 'main.tex').write_text('\\documentclass{article}\\begin{document}Main\\end{document}', encoding='utf8')
    (tmp_path / 'supplement.tex').write_text('\\documentclass{article}\\begin{document}Supp\\end{document}', encoding='utf8')
    
    result = gather_version('2504.02528', 1, tmp_path)
    assert result.status == 'ready'
    assert result.root_file == 'main.tex'
    assert any(d.code == 'resolved_root' for d in result.diagnostics)


def test_resolve_root_disambiguates_rebuttal(tmp_path):
    (tmp_path / 'paper.tex').write_text('\\documentclass{article}\\begin{document}Paper\\end{document}', encoding='utf8')
    (tmp_path / 'rebuttal.tex').write_text('\\documentclass{article}\\begin{document}Rebuttal\\end{document}', encoding='utf8')
    
    result = gather_version('2504.02534', 1, tmp_path)
    assert result.status == 'ready'
    assert result.root_file == 'paper.tex'


def test_resolve_root_disambiguates_subfolder(tmp_path):
    (tmp_path / 'main.tex').write_text('\\documentclass{article}\\begin{document}TopMain\\end{document}', encoding='utf8')
    sub = tmp_path / 'subfolder'
    sub.mkdir()
    (sub / 'main.tex').write_text('\\documentclass{article}\\begin{document}SubMain\\end{document}', encoding='utf8')
    
    result = gather_version('2504.02526', 1, tmp_path)
    assert result.status == 'ready'
    assert result.root_file == 'main.tex'


def test_resolve_root_disambiguates_by_size(tmp_path):
    (tmp_path / 'alpha.tex').write_text('\\documentclass{article}\\begin{document}' + ('Body ' * 500) + '\\end{document}', encoding='utf8')
    (tmp_path / 'beta.tex').write_text('\\documentclass{article}\\begin{document}Short\\end{document}', encoding='utf8')
    
    result = gather_version('2504.09999', 1, tmp_path)
    assert result.status == 'ready'
    assert result.root_file == 'alpha.tex'


def test_resolve_root_preserves_ambiguous_when_undistinguishable(tmp_path):
    (tmp_path / 'a.tex').write_text('\\documentclass{article}\\begin{document}Same\\end{document}', encoding='utf8')
    (tmp_path / 'b.tex').write_text('\\documentclass{article}\\begin{document}Same\\end{document}', encoding='utf8')
    
    result = gather_version('2504.08888', 1, tmp_path)
    assert result.status == 'unparseable'
    assert any(d.code == 'ambiguous_root' for d in result.diagnostics)


def test_gather_recovers_latin1_root(tmp_path):
    # Non-UTF8 byte 0xfc (ü in latin-1) in author name
    content = b'\\documentclass{article}\\begin{document}M\xfcller\\end{document}'
    (tmp_path / 'source.tex').write_bytes(content)
    
    result = gather_version('2504.02661', 1, tmp_path)
    assert result.status == 'ready'
    assert result.root_file == 'source.tex'
    assert 'M' in ''.join(s.text for s in result.segments)


def test_gather_supports_documentstyle_legacy_root(tmp_path):
    content = '\\documentstyle[12pt]{article}\\begin{document}Legacy LaTeX 2.09\\end{document}'
    (tmp_path / 'legacy.tex').write_text(content, encoding='utf8')
    
    result = gather_version('2504.02584', 1, tmp_path)
    assert result.status == 'ready'
    assert result.root_file == 'legacy.tex'


def test_scanner_handles_malformed_url_without_attribute_error():
    # String starting with https:// followed immediately by whitespace
    raw = 'Check this https:// for more details.'
    scanned = scan_latex(raw)
    assert scanned is not None
    assert any('https://' in s.raw for s in scanned.segments)


def test_key_regex_filters_invalid_keys():
    # Keys with spaces or commas should be rejected by _KEY regex
    assert _KEY.fullmatch('valid_key-123') is not None
    assert _KEY.fullmatch('invalid key') is None
    assert _KEY.fullmatch('invalid,key') is None
    # Key validation also rejects '*'
    is_valid_scope = lambda k: _KEY.fullmatch(k) is not None and k != '*'
    assert is_valid_scope('valid_key') is True
    assert is_valid_scope('*') is False
