import re

from parser.integration import DefaultParserServices
from parser.pipeline import _policy_fingerprint


def test_default_services_convert_a1_ready_gather(tmp_path):
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\\begin{document}Body.\\end{document}',
        encoding='utf8',
    )
    (tmp_path / 'refs.bib').write_text('@misc{x, title={X}}', encoding='utf8')

    gathered = DefaultParserServices().gather('2504.02523', 1, tmp_path)

    assert gathered.status == 'ready'
    assert gathered.root_file == 'main.tex'
    assert gathered.files == ('main.tex',)
    assert gathered.bib_files == ('refs.bib',)
    assert 'Body.' in gathered.text
    assert gathered.diagnostics == ()


def test_default_services_convert_a1_partial_diagnostics(tmp_path):
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\\begin{document}\\input{missing}\\end{document}',
        encoding='utf8',
    )

    gathered = DefaultParserServices().gather('2504.02523', 1, tmp_path)

    assert gathered.status == 'partial'
    assert [item.code for item in gathered.diagnostics] == ['missing_include']
    assert gathered.diagnostics[0].path == 'main.tex'
    assert gathered.diagnostics[0].start is not None
    assert gathered.diagnostics[0].end is not None


def test_default_services_convert_a1_unparseable_source(tmp_path):
    (tmp_path / 'notes.tex').write_text('No document class here', encoding='utf8')

    gathered = DefaultParserServices().gather('2504.02523', 1, tmp_path)

    assert gathered.status == 'unparseable'
    assert gathered.text == ''
    assert gathered.root_file is None
    assert [item.code for item in gathered.diagnostics] == ['no_root']


def test_default_services_cache_fingerprint_is_hex_and_stable():
    service = DefaultParserServices()

    first = service.cache_fingerprint()
    second = service.cache_fingerprint()

    assert isinstance(first, str)
    assert first == second
    assert re.fullmatch(r'[0-9a-f]{64}', first)


def test_default_services_policy_fingerprint_uses_service():
    fingerprint = _policy_fingerprint(True, DefaultParserServices())

    assert isinstance(fingerprint, str)
    assert re.fullmatch(r'[0-9a-f]{64}', fingerprint)
