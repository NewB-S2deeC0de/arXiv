import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from matching.auto_matcher import (
    author_surnames, iter_heuristic_labels, title_token_sort_ratio, write_auto_labels,
)
from matching.candidate_pool import build_candidate_pool


def make_pool(*, bib_doi=None, ref_doi=None, reference_id='2001.00001',
              bib_title='Reference matching methods', ref_title='Methods reference matching',
              bib_authors=None, ref_authors=None, extra_references=()):
    entry = dict(arxiv_id='p', citation_key='key', title=bib_title,
                 authors=['Doe, Jane'] if bib_authors is None else bib_authors, doi=bib_doi)
    reference = dict(arxiv_id='p', ref_arxiv_id=reference_id, ref_title=ref_title,
                     ref_authors=['Jane Doe'] if ref_authors is None else ref_authors, ref_doi=ref_doi)
    return build_candidate_pool(pa.Table.from_pylist([entry]),
        pa.Table.from_pylist([reference, *extra_references]))


def decision(pool, **kwargs):
    return next(iter_heuristic_labels(pool, **kwargs))


def test_doi_url_prefix_case_and_papers_join_by_reference_id():
    pool = make_pool(bib_doi='DOI: 10.1234/ABC', bib_title='Different', bib_authors=[])
    papers = pa.Table.from_pylist([
        {'arxiv_id': 'p', 'doi': '10.1234/wrong'},
        {'arxiv_id': '2001.00001', 'doi': 'https://doi.org/10.1234/abc'},
    ])
    result = decision(pool, papers=papers)
    assert result.label['groundtruth_arxiv_id'] == '2001.00001'
    assert result.evidence[0]['outcome'] == 'doi_match'
    assert result.evidence[0]['ref_doi_sources'] == ['papers.doi']


@pytest.mark.parametrize('bib_doi,ref_doi', [
    (None, None), ('10.1234/a', None), (None, '10.1234/a'),
])
def test_title_author_fallback_when_at_least_one_side_has_no_doi(bib_doi, ref_doi):
    result = decision(make_pool(bib_doi=bib_doi, ref_doi=ref_doi))
    assert result.label['label_status'] == 'matched'
    assert result.evidence[0]['title_similarity'] == 1.0
    assert result.evidence[0]['shared_surnames'] == ['doe']


def test_mismatching_dois_cannot_fall_back_to_identical_title():
    result = decision(make_pool(bib_doi='10.1234/a', ref_doi='10.1234/b'))
    assert result.label['label_status'] == 'needs_review'
    assert result.label['groundtruth_arxiv_id'] is None
    assert result.evidence[0]['outcome'] == 'doi_mismatch'


def test_unrelated_doi_mismatch_does_not_block_a_unique_valid_match():
    pool = make_pool(bib_doi='10.1234/a', ref_doi='10.1234/a',
        extra_references=[dict(arxiv_id='p', ref_arxiv_id='2002.00002', ref_doi='10.1234/b')])
    assert decision(pool).label['groundtruth_arxiv_id'] == '2001.00001'


def test_multiple_qualifying_references_need_review_even_when_scores_differ():
    pool = make_pool(extra_references=[dict(arxiv_id='p', ref_arxiv_id='2002.00002',
        ref_title='Reference matching method', ref_authors=['J. Doe'])])
    result = decision(pool)
    assert result.label['label_status'] == 'needs_review'
    assert result.label['qualifying_candidates'] == ['2001.00001', '2002.00002']


@pytest.mark.parametrize('kwargs', [
    {'bib_title': None},
    {'bib_title': 'Completely unrelated topic'},
    {'ref_authors': []},
    {'ref_authors': ['Other Person']},
])
def test_missing_title_or_author_or_low_similarity_yields_no_match(kwargs):
    result = decision(make_pool(**kwargs))
    assert result.label['label_status'] == 'no_match'
    assert result.label['groundtruth_arxiv_id'] is None


@pytest.mark.parametrize('doi', ['invalid identifier', '10.1234/a', ''])
def test_conflicting_or_malformed_reference_doi_evidence_is_not_silently_overwritten(doi):
    papers = pa.Table.from_pylist([
        {'arxiv_id': '2001.00001', 'doi': '10.1234/b'},
    ])
    result = decision(make_pool(ref_doi=doi), papers=papers)
    if doi:
        assert result.label['label_status'] == 'needs_review'
        assert result.label['review_candidates'] == ['2001.00001']
    else:
        assert result.label['label_status'] == 'matched'


def test_duplicate_papers_rows_with_same_doi_do_not_create_false_ambiguity():
    papers = pa.Table.from_pylist([
        {'arxiv_id': '2001.00001', 'doi': '10.1234/a'},
        {'arxiv_id': '2001.00001', 'doi': 'https://doi.org/10.1234/A'},
    ])
    assert decision(make_pool(bib_doi='10.1234/a'), papers=papers).label['label_status'] == 'matched'


def test_all_entries_in_selected_publications_are_visited_and_manual_pool_excluded():
    entries = pa.Table.from_pylist([
        {'arxiv_id': 'auto', 'citation_key': 'a'},
        {'arxiv_id': 'auto', 'citation_key': 'b'},
        {'arxiv_id': 'manual', 'citation_key': 'a'},
    ])
    refs = pa.table({'arxiv_id': pa.array([], pa.string()),
                     'ref_arxiv_id': pa.array([], pa.string())})
    pool = build_candidate_pool(entries, refs)
    results = list(iter_heuristic_labels(pool, excluded_publications=['manual']))
    assert [result.label['citation_key'] for result in results] == ['a', 'b']
    assert all(result.label['reason'] == 'no_candidates' for result in results)


def test_export_roundtrip_and_missing_papers_doi_is_reported(tmp_path):
    pool = make_pool()
    output = tmp_path / 'auto'
    report = write_auto_labels(pool, output, papers=pa.table({'arxiv_id': ['2001.00001']}))
    assert report['warnings']
    assert report['counts'] == {'matched': 1}
    labels = pq.read_table(output / 'auto_labels.parquet').to_pylist()
    assert labels[0]['groundtruth_arxiv_id'] == '2001.00001'
    assert pq.read_table(output / 'pair_evidence.parquet').num_rows == 1
    assert json.loads((output / 'summary.json').read_text(encoding='utf-8')) == report
    assert (output / '_SUCCESS').exists()
    with pytest.raises(FileExistsError):
        write_auto_labels(pool, output)


def test_unknown_publications_are_rejected():
    with pytest.raises(ValueError, match='publication'):
        list(iter_heuristic_labels(make_pool(), publications=['unknown']))


def test_normalization_and_known_surname_formats():
    assert title_token_sort_ratio('Méthods, Reference!', 'reference methods') == 1.0
    assert title_token_sort_ratio(None, None) == 0
    assert author_surnames(['Doe, Jane', 'Jane Doe', 'A. Smith Jr.', 'L. Li', 'A.']) == {
        'doe', 'smith', 'li',
    }


@pytest.mark.parametrize('score,expected', [(0.85, 'matched'), (0.849999, 'no_match')])
def test_title_threshold_boundary(monkeypatch, score, expected):
    monkeypatch.setattr('matching.auto_matcher.title_token_sort_ratio', lambda *_: score)
    assert decision(make_pool()).label['label_status'] == expected


def test_review_publication_is_not_counted_as_completed(tmp_path):
    report = write_auto_labels(make_pool(bib_doi='10.1234/a', ref_doi='10.1234/b'),
                               tmp_path / 'review')
    assert report['completed_publications'] == []
    assert report['review_publications'] == ['p']


def test_missing_papers_doi_does_not_override_a_direct_reference_doi():
    papers = pa.table({'arxiv_id': ['2001.00001']})
    result = decision(make_pool(bib_doi='10.1234/a', ref_doi='10.1234/a'), papers=papers)
    assert result.evidence[0]['outcome'] == 'doi_match'


def test_reference_from_another_publication_cannot_match():
    pool = make_pool(bib_title=None, extra_references=[
        dict(arxiv_id='other', ref_arxiv_id='2002.00002', ref_doi='10.1234/a')])
    result = decision(pool)
    assert result.label['label_status'] == 'no_match'
    assert len(result.evidence) == 1


def test_large_export_keeps_every_label_and_pair_across_buffer_boundaries(tmp_path):
    pool = build_candidate_pool(pa.Table.from_pylist([
        dict(arxiv_id='p', citation_key=f'key{i:04}', title='Unrelated') for i in range(1100)]),
        pa.Table.from_pylist([dict(arxiv_id='p', ref_arxiv_id=f'r{i}',
                                 ref_title='Candidate') for i in range(8)]))
    report = write_auto_labels(pool, tmp_path / 'large')
    labels = pq.read_table(tmp_path / 'large/auto_labels.parquet').to_pylist()
    assert len({r['citation_key'] for r in labels}) == 1100
    assert report['counts'] == {'no_match': 1100}
    assert pq.ParquetFile(tmp_path / 'large/pair_evidence.parquet').metadata.num_rows == 8800
