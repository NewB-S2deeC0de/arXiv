"""B staging/projection behavior, without making pyarrow a core dependency."""
from dataclasses import asdict
from pathlib import Path
import unittest

from standardization import (extract_bibliography, SourceContext,
                             prepare_c_bibentry, to_c_bibentry)


def item(body, key='k'):
    raw = '\\begin{thebibliography}{1}\\bibitem{' + key + '}' + body + '\\end{thebibliography}'
    return extract_bibliography(raw, source=SourceContext('test-publication', 2)).entries[0]


class CBibEntryAdapterTests(unittest.TestCase):
    def test_c_payload_fields_and_missing_values_keep_staging_unknowns(self):
        entry = item('A paper.')
        prepared = prepare_c_bibentry(entry)
        fields = prepared.payload.as_kwargs()
        self.assertEqual(set(fields), {'arxiv_id', 'citation_key', 'entry_type', 'title',
                                     'authors', 'year', 'doi', 'journal', 'url'})
        self.assertEqual(fields['entry_type'], 'misc')
        self.assertEqual(fields['authors'], [])
        self.assertIsNone(fields['title'])
        self.assertIn('authors', prepared.unknown_fields)
        self.assertIn('entry_type', prepared.unknown_fields)
        self.assertIs(prepared.entry, entry)
        self.assertEqual(prepared.entry.source.version, 2)


    def test_supplied_metadata_is_typed_and_not_mutated(self):
        authors = ['A. Author', 'B. Author']
        metadata = dict(title='Approved title', authors=authors, year=2024,
                        journal='Approved journal', entry_type='article')
        prepared = prepare_c_bibentry(item('Body.'), metadata=metadata)
        authors.append('Later edit')
        self.assertEqual(prepared.payload.authors, ('A. Author', 'B. Author'))
        self.assertEqual(dict(prepared.provided_metadata)['authors'], ('A. Author', 'B. Author'))
        self.assertEqual(prepared.payload.year, 2024)
        self.assertNotIn('authors', prepared.unknown_fields)
        self.assertNotIn('c_authors_unknown', [w.code for w in prepared.warnings])

    def test_explicit_empty_authors_differs_from_unknown(self):
        explicit = prepare_c_bibentry(item('Body.'), metadata={'authors': []})
        unknown = prepare_c_bibentry(item('Body.'))
        self.assertEqual(explicit.payload.authors, unknown.payload.authors)
        self.assertNotIn('authors', explicit.unknown_fields)
        self.assertIn('authors', unknown.unknown_fields)

    def test_multiple_and_conflicting_links_not_selected(self):
        for entry, metadata in (
            (item('10.1234/a 10.9999/b'), None),
            (item('10.1234/a'), {'doi': '10.9999/b'}),
            (item(r'\url{https://a.example/x} \url{https://b.example/y}'), None),
        ):
            prepared = prepare_c_bibentry(entry, metadata=metadata)
            field = 'doi' if '10.' in entry.raw_content else 'url'
            self.assertIsNone(getattr(prepared.payload, field))
            self.assertIn('ambiguous_' + field, [w.code for w in prepared.warnings])
            self.assertTrue(prepared.link_evidence)

    def test_punctuation_warning_blocks_automatic_promotion(self):
        prepared = prepare_c_bibentry(item('DOI: 10.1234/a.'))
        self.assertIsNone(prepared.payload.doi)
        self.assertIn('ambiguous_doi', [w.code for w in prepared.warnings])

    def test_repeated_identical_evidence_is_not_a_conflict(self):
        prepared = prepare_c_bibentry(item('10.1234/a 10.1234/a'))
        self.assertEqual(prepared.payload.doi, '10.1234/a')
        self.assertEqual(len(prepared.link_evidence), 2)

    def test_malformed_key_stays_in_staging(self):
        prepared = prepare_c_bibentry(item('Body.', key=''))
        self.assertIsNone(prepared.payload)
        self.assertEqual(prepared.entry.raw_content, 'Body.')
        with self.assertRaises(ValueError):
            to_c_bibentry(prepared, entry_factory=lambda **kw: kw)

    def test_publication_and_metadata_validation(self):
        with self.assertRaises(ValueError):
            prepare_c_bibentry(item('Body.'), arxiv_id='another-publication')
        for metadata, error in (({'year': True}, TypeError), ({'year': '2024'}, TypeError),
                                ({'authors': 'A. Author'}, TypeError), ({'new_field': 1}, ValueError),
                                ({'title': ''}, TypeError)):
            with self.assertRaises(error):
                prepare_c_bibentry(item('Body.'), metadata=metadata)

    def test_factory_receives_exact_c_fields_and_wrapper_is_serializable(self):
        prepared = prepare_c_bibentry(item(r'\url{https://example.org/p}'))
        row = to_c_bibentry(prepared, entry_factory=lambda **kw: kw)
        self.assertEqual(row, prepared.payload.as_kwargs())
        import json
        self.assertEqual(json.loads(json.dumps(asdict(prepared)))['entry']['source']['version'], 2)


if __name__ == '__main__':
    unittest.main()
