from parser.gather import gather_version


def test_gather_follows_nested_and_repeated_includes_with_file_spans(tmp_path):
    root = tmp_path / 'main.tex'
    root.write_text(
        '\\documentclass{article}\\newcommand{\\name}{Value}\nĐầu\\input{parts/one} giữa '
        '\\include{parts/two.tex} cuối\\input{parts/one}', encoding='utf8'
    )
    (tmp_path / 'parts').mkdir()
    (tmp_path / 'parts/one.tex').write_text('Một\\input{../tail}!', encoding='utf8')
    (tmp_path / 'parts/two.tex').write_text('Hai', encoding='utf8')
    (tmp_path / 'tail.tex').write_text('Đuôi', encoding='utf8')
    (tmp_path / 'unused.tex').write_text('not included', encoding='utf8')
    (tmp_path / 'refs.bib').write_text('@article{x}', encoding='utf8')

    result = gather_version('2504.02523', 2, tmp_path)

    assert result.status == 'ready'
    assert result.root_file == 'main.tex'
    assert result.used_tex_files == ['main.tex', 'parts/one.tex', 'tail.tex', 'parts/two.tex']
    assert result.bib_files == ['refs.bib']
    assert result.diagnostics == []
    assembled = ''.join(part.text for part in result.segments)
    assert assembled == '\\documentclass{article}\\newcommand{\\name}{Value}\nĐầuMộtĐuôi! giữa Hai cuốiMộtĐuôi!'
    for part in result.segments:
        original = (tmp_path / part.origin.path).read_text(encoding='utf8')
        assert original[part.origin.start:part.origin.end] == part.text


def test_gather_ignores_commented_and_literal_commands(tmp_path):
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\n'
        '% \\input{commented}\n'
        '\\% escaped percent \\input{real}\n'
        '\\verb|\\input{inline}|\n'
        '\\begin{verbatim}\\input{literal}\\end{verbatim}\n'
        '\\begin{lstlisting}\\input{listing}\\end{lstlisting}', encoding='utf8'
    )
    (tmp_path / 'real.tex').write_text('REAL', encoding='utf8')
    (tmp_path / 'fake.tex').write_text(
        '% \\documentclass{article}\n'
        '\\begin{verbatim}\\documentclass{article}\\end{verbatim}', encoding='utf8'
    )

    result = gather_version('2504.02523', 1, tmp_path)

    assert result.status == 'ready'
    assert result.used_tex_files == ['main.tex', 'real.tex']
    assert ''.join(s.text for s in result.segments).count('REAL') == 1
    assert result.diagnostics == []


def test_gather_reports_ambiguous_or_missing_root_without_guessing(tmp_path):
    (tmp_path / 'a.tex').write_text('No root here', encoding='utf8')
    missing = gather_version('2504.02523', 1, tmp_path)
    assert (missing.status, missing.root_file, missing.segments) == ('unparseable', None, [])
    assert [d.code for d in missing.diagnostics] == ['no_root']

    (tmp_path / 'a.tex').write_text('\\documentclass{article}', encoding='utf8')
    (tmp_path / 'b.tex').write_text('\\documentclass{book}', encoding='utf8')
    ambiguous = gather_version('2504.02523', 1, tmp_path)
    assert ambiguous.status == 'unparseable'
    assert ambiguous.root_file is None and ambiguous.segments == []
    assert [d.code for d in ambiguous.diagnostics] == ['ambiguous_root']


def test_gather_reports_cycle_missing_and_unsafe_include_without_reading_outside(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'main.tex').write_text(
        '\\documentclass{article}\\input{loop}\\input{absent}'
        '\\input{../outside}\\input{linked}', encoding='utf8'
    )
    (source / 'loop.tex').write_text('Loop\\input{main}', encoding='utf8')
    (tmp_path / 'outside.tex').write_text('SECRET', encoding='utf8')
    (source / 'linked.tex').symlink_to(tmp_path / 'outside.tex')

    result = gather_version('2504.02523', 1, source)

    assert result.status == 'partial'
    assert [d.code for d in result.diagnostics] == [
        'include_cycle', 'missing_include', 'unsafe_include', 'unsafe_include'
    ]
    assert result.used_tex_files == ['main.tex', 'loop.tex']
    assert 'SECRET' not in ''.join(s.text for s in result.segments)
    assert all(d.origin.path in ('main.tex', 'loop.tex') for d in result.diagnostics)


def test_gather_accepts_documentclass_options_and_ignores_external_tex_symlink(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'main.tex').write_text('\\documentclass[12pt]{article}Content', encoding='utf8')
    (tmp_path / 'external.tex').write_text('\\documentclass{book}SECRET', encoding='utf8')
    (source / 'external.tex').symlink_to(tmp_path / 'external.tex')

    result = gather_version('2504.02523', 1, source)

    assert result.status == 'ready'
    assert result.root_file == 'main.tex'
    assert result.used_tex_files == ['main.tex']
    assert 'SECRET' not in ''.join(s.text for s in result.segments)


def test_gather_reports_invalid_utf8_instead_of_crashing(tmp_path):
    (tmp_path / 'main.tex').write_bytes(b'\\documentclass{article}\\input{bad}')
    (tmp_path / 'bad.tex').write_bytes(b'\xff')

    result = gather_version('2504.02523', 1, tmp_path)

    assert result.status == 'partial'
    assert [d.code for d in result.diagnostics] == ['decode_error']
    assert result.used_tex_files == ['main.tex']


def test_gather_does_not_expand_includes_inside_macro_definitions(tmp_path):
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}'
        '\\newcommand{\\unused}{\\input{hidden}}'
        '\\input{real}', encoding='utf8'
    )
    (tmp_path / 'hidden.tex').write_text('HIDDEN', encoding='utf8')
    (tmp_path / 'real.tex').write_text('REAL', encoding='utf8')

    result = gather_version('2504.02523', 1, tmp_path)

    assert result.status == 'partial'  # Macro expansion is not evaluated.
    assert result.used_tex_files == ['main.tex', 'real.tex']
    assert 'HIDDEN' not in ''.join(s.text for s in result.segments)
    assert 'dynamic_macro' in [d.code for d in result.diagnostics]


def test_gather_bounds_recursive_include_depth(tmp_path):
    (tmp_path / 'main.tex').write_text('\\documentclass{article}\\input{part0}', encoding='utf8')
    for n in range(70):
        (tmp_path / f'part{n}.tex').write_text(f'\\input{{part{n + 1}}}', encoding='utf8')

    result = gather_version('2504.02523', 1, tmp_path)

    assert result.status == 'partial'
    assert 'depth_limit' in [d.code for d in result.diagnostics]
    assert len(result.used_tex_files) <= 64


def test_gather_ignores_structural_commands_in_opaque_arguments(tmp_path):
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\\caption{\\input{hidden}}\\input{real}', encoding='utf8')
    (tmp_path / 'fake.tex').write_text('\\title{\\documentclass{book}}', encoding='utf8')
    (tmp_path / 'hidden.tex').write_text('HIDDEN', encoding='utf8')
    (tmp_path / 'real.tex').write_text('REAL', encoding='utf8')

    result = gather_version('2504.02523', 1, tmp_path)

    assert result.root_file == 'main.tex'
    assert result.used_tex_files == ['main.tex', 'real.tex']
    assert result.status == 'partial'
    assert 'opaque_argument' in [d.code for d in result.diagnostics]


def test_gather_ignores_include_in_unbraced_macro_definition(tmp_path):
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\\newcommand\\foo{\\input{hidden}}\\input{real}', encoding='utf8')
    (tmp_path / 'hidden.tex').write_text('HIDDEN', encoding='utf8')
    (tmp_path / 'real.tex').write_text('REAL', encoding='utf8')

    result = gather_version('2504.02523', 1, tmp_path)

    assert result.used_tex_files == ['main.tex', 'real.tex']
    assert 'dynamic_macro' in [d.code for d in result.diagnostics]


def test_gather_bounds_expanded_output_and_input_file_bytes(tmp_path):
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\\input{part}\\input{part}\\input{part}', encoding='utf8')
    (tmp_path / 'part.tex').write_text('x' * 60, encoding='utf8')

    result = gather_version('2504.02523', 1, tmp_path, max_output_bytes=100)

    assert result.status == 'partial'
    assert 'resource_limit' in [d.code for d in result.diagnostics]
    assert len(''.join(s.text for s in result.segments).encode('utf8')) <= 100

    limited = gather_version('2504.02523', 1, tmp_path, max_file_bytes=30)
    assert limited.status == 'unparseable'
    assert 'resource_limit' in [d.code for d in limited.diagnostics]
