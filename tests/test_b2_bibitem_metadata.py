"""Metadata evidence under the narrowly supported IEEE bibliography style."""
from collections import Counter
from pathlib import Path
import unittest

from standardization import extract_bibliography, extract_bibitem_metadata


def entry(body):
    return extract_bibliography(r'\begin{thebibliography}{1}\bibitem{k}' + body +
                                r'\end{thebibliography}').entries[0]


class MetadataTests(unittest.TestCase):
    def check(self, raw):
        item = entry(raw)
        result = extract_bibitem_metadata(item)
        self.assertIs(result.entry, item)
        self.assertEqual(item.raw_content, raw)
        for evidence in result.evidence:
            self.assertEqual(raw[evidence.start:evidence.end], evidence.raw)
        for warning in result.warnings:
            self.assertTrue(0 <= warning.start <= warning.end <= len(raw))
        return result, dict(result.metadata)

    def test_explicit_ieee_metadata_and_title_separator(self):
        r, fields = self.check(r"A.~Author and B.~Writer, ``An explicit title,'' \emph{Journal}, vol. 3, 2024.")
        self.assertEqual(fields, dict(title='An explicit title', authors=('A. Author', 'B. Writer'),
                                      journal='Journal', year=2024))
        self.assertFalse(r.warnings)

    def test_full_author_list_with_and_and_ieee_spacing_macro(self):
        _, fields = self.check(r"\BIBentryALTinterwordspacing P.~Name, S.~Writer, and R.~E. Author, ``Title,'' \emph{Journal}, 2020.")
        self.assertEqual(fields['authors'], ('P. Name', 'S. Writer', 'R. E. Author'))

    def test_truncated_author_list_is_not_invented(self):
        result, fields = self.check(r"A.~Author \emph{et~al.}, ``Title,'' \emph{Journal}, 2024.")
        self.assertNotIn('authors', fields)
        self.assertIn('authors_truncated', [w.code for w in result.warnings])

    def test_unquoted_or_multiple_titles_defer_ambiguous_fields(self):
        for raw in ('Author. Unquoted title. 2024.', r"A. Author, ``First,'' ``Second,'' 2024."):
            result, fields = self.check(raw)
            self.assertNotIn('title', fields)
            self.assertTrue(any(w.code in ('title_style_deferred', 'ambiguous_title') for w in result.warnings))

    def test_title_with_math_keeps_math_and_never_changes_citation_key(self):
        result, fields = self.check(r"A.~Author, ``A $4\pi$ result,'' \emph{Journal}, 2024.")
        self.assertIn(r'$4\pi$', fields['title'])
        self.assertEqual(result.entry.citation_key, 'k')

    def test_book_title_not_assigned_to_journal(self):
        _, fields = self.check(r"A.~Author, ``Chapter,'' in \emph{Book}. Springer, 2020, pp. 1--3.")
        self.assertNotIn('journal', fields)
        self.assertEqual(fields['year'], 2020)

    def test_arxiv_venue_not_assigned_to_journal(self):
        result, fields = self.check(r"A.~Author, ``Paper,'' \emph{arXiv:2406.05200}, 2024.")
        self.assertNotIn('journal', fields)
        self.assertIn('preprint_venue_not_journal', [w.code for w in result.warnings])
        self.assertTrue(any(e.field == 'venue' for e in result.evidence))

    def test_date_and_publisher_clauses(self):
        for raw in (r"A.~Author, ``Paper,'' \emph{Journal}, Apr. 2005, publisher: A.",
                    r"A.~Author, ``Paper,'' \emph{Journal}, Sep 2005."):
            _, fields = self.check(raw)
            self.assertEqual(fields['year'], 2005)

    def test_conflicting_years_not_selected_and_title_year_is_not_publication_year(self):
        r, fields = self.check(r"A.~Author, ``Forecasts, 2020. Results,'' \emph{Journal}, 2023. Revision, 2024.")
        self.assertNotIn('year', fields)
        self.assertEqual({e.value for e in r.evidence if e.field == 'year'}, {2023, 2024})
        self.assertIn('ambiguous_year', [w.code for w in r.warnings])

    def test_protected_comment_url_and_macro_fields_not_parsed(self):
        raw = ("% ``Fake title,'' , 2020.\n" +
               r'\custom{``Hidden title,\textbf{X}'' , 2021.} '
               r'\url{https://example.org/,2022.} Text.')
        _, fields = self.check(raw)
        self.assertNotIn('title', fields)
        self.assertNotIn('year', fields)

    def test_unknown_author_grammar_not_split_at_arbitrary_commas(self):
        result, fields = self.check(r"Smith, John, ``Title,'' \emph{Journal}, 2024.")
        self.assertNotIn('authors', fields)
        self.assertIn('authors_style_deferred', [w.code for w in result.warnings])



if __name__ == '__main__':
    unittest.main()
