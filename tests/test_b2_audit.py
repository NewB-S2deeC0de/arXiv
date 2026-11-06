"""Offline audit integration, locations, decoding and CLI output safety."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from standardization.audit import audit_file, audit_paths, main




class AuditTests(unittest.TestCase):



    def test_source_line_columns_preserve_crlf_unicode_and_cr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.tex'
            raw = ('Nguyễn\r\n\\begin{thebibliography}{1}\r\\bibitem{k}\n'
                   ' DOI: 10.1234/ABC\r\n\\end{thebibliography}')
            path.write_bytes(raw.encode('utf-8'))
            row = audit_file(path)
            candidate = row['entries'][0]['link_candidates'][0]
            loc = candidate['file_location']
            self.assertEqual((loc['start_line'], loc['start_column']), (4, 7))
            self.assertEqual((loc['end_line'], loc['end_column']), (4, 18))
            self.assertEqual(raw[loc['start']:loc['end']], '10.1234/ABC')

    def test_malformed_bibliography_is_needs_review_not_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.tex'
            path.write_text(r'\begin{thebibliography}{1}\bibitem{k}Body.', encoding='utf-8')
            row = audit_file(path)
            self.assertEqual(row['status'], 'needs_review')
            self.assertEqual(row['exclusion_spans'], ())

    def test_link_warning_changes_status_and_maps_to_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.bbl'
            raw = r'\begin{thebibliography}{1}\bibitem{k}10.1234/ABC.\end{thebibliography}'
            path.write_text(raw, encoding='utf-8')
            row = audit_file(path)
            self.assertEqual(row['status'], 'needs_review')
            warning = row['entries'][0]['link_warnings'][0]
            self.assertEqual(warning['code'], 'ambiguous_trailing_punctuation')
            loc = warning['file_location']
            self.assertEqual(raw[loc['start']:loc['end']], '10.1234/ABC.')

    def test_strict_decoding_error_does_not_abort_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bad.tex').write_bytes(b'\xff')
            (root / 'good.tex').write_bytes(b'ordinary text')
            report = audit_paths([root])
            self.assertEqual(report['summary']['statuses'], {'no_literal_bibliography': 1, 'read_error': 1})
            self.assertEqual(audit_file(root / 'bad.tex', encoding='latin-1')['status'], 'no_literal_bibliography')

    def test_size_limit_and_unknown_codec(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.tex'
            path.write_bytes(b'12345')
            self.assertEqual(audit_file(path, max_bytes=4)['error'], 'file_size_limit')
            self.assertEqual(audit_file(path, encoding='not-a-codec')['status'], 'read_error')
            for value in (0, -1, True):
                with self.assertRaises(ValueError):
                    audit_file(path, max_bytes=value)

    def test_missing_and_unsupported_inputs_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / 'missing.tex'
            other = root / 'other.txt'
            other.write_text('text', encoding='utf-8')
            report = audit_paths([missing, other])
            self.assertEqual(report['summary']['statuses'], {'read_error': 1, 'unsupported_input': 1})
            self.assertEqual(audit_file(root / 'missing.bib')['status'], 'read_error')


    def test_cli_stdout_json_and_read_error_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = io.StringIO()
            with redirect_stdout(stream):
                status = main([str(Path(directory) / 'missing.tex')])
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(stream.getvalue())['summary']['statuses'], {'read_error': 1})


if __name__ == '__main__':
    unittest.main()
