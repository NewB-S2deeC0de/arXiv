from matching.features import (
    FEATURE_NAMES,
    CitationIndex,
    TfidfTitleIndex,
    author_surnames,
    clean_title,
    doi_match,
    extract_features,
    jaccard,
    normalize_doi,
    year_diff,
)


def test_token_sort_ratio_ignores_word_order():
    titles = TfidfTitleIndex().fit(['deep learning for graphs', 'graphs for deep learning'])
    bib = {'title': 'Deep Learning for Graphs', 'authors': ['Smith'], 'year': 2020}
    ref = {
        'ref_title': 'Graphs for Deep Learning',
        'ref_authors': ['Smith'],
        'ref_year': 2020,
    }
    features = extract_features(bib, ref, titles=titles, citations=CitationIndex())
    assert features['feat_title_token_sort'] == 1.0
    assert 0.0 < features['feat_title_char_tfidf'] < 1.0


def test_tfidf_is_symmetric_and_zero_when_title_missing():
    titles = TfidfTitleIndex().fit(['neural networks', 'neural net'])
    assert titles.similarity('neural networks', 'neural net') == titles.similarity(
        'neural net', 'neural networks'
    )
    assert titles.similarity('', 'neural net') == 0.0
    assert 0.0 < titles.similarity('neural networks', 'networks neural') <= 1.0


def test_author_surname_jaccard_uses_last_or_first_name():
    assert author_surnames(['Nguyen Van A', 'Smith, John']) == {'a', 'smith'}
    assert jaccard({'smith', 'lee'}, {'LEE', 'brown'}) == 1 / 3
    assert jaccard(set(), set()) == 0.0


def test_year_and_doi_rules():
    assert year_diff(2020, 2024) == 4.0
    assert year_diff(None, 2024) == 5.0
    assert year_diff('submitted 2019-04-01', 2020) == 1.0
    assert normalize_doi(' 10.1234/AbC ') == '10.1234/abc'
    assert doi_match(' 10.1/ABC ', '10.1/abc') == 1.0
    assert doi_match(None, '10.1/abc') == 0.0
    assert doi_match('10.1/a', '10.1/b') == 0.0


def test_hierarchy_cite_lookup_uses_sentence_elements():
    index = CitationIndex.from_elements(
        [
            {
                'arxiv_id': 'p1',
                'kind': 'sentence',
                'content': 'We build on ref_key in this sentence.',
            },
            {'arxiv_id': 'p1', 'kind': 'heading', 'content': 'ref_key'},
            {'arxiv_id': 'p2', 'kind': 'sentence', 'content': 'Nothing here.'},
        ]
    )
    assert index.contains('p1', 'ref_key') == 1.0
    assert index.contains('p2', 'ref_key') == 0.0
    assert index.contains('p1', '') == 0.0


def test_extract_features_returns_all_six_float_features():
    titles = TfidfTitleIndex().fit(['attention is all you need', 'the transformer'])
    features = extract_features(
        {
            'arxiv_id': 'p1',
            'citation_key': 'ref_key',
            'title': 'Attention Is All You Need',
            'authors': ['Vaswani'],
            'year': 2017,
            'doi': '10.1/ATT',
        },
        {
            'ref_title': 'The Transformer',
            'ref_authors': ['Vaswani'],
            'ref_submission_date': '2017-06-12',
            'ref_doi': '10.1/att',
        },
        titles=titles,
        citations=CitationIndex.from_elements(
            [
                {
                    'arxiv_id': 'p1',
                    'kind': 'sentence',
                    'content': 'See ref_key for details.',
                }
            ]
        ),
    )
    assert tuple(features) == FEATURE_NAMES
    assert all(isinstance(value, float) for value in features.values())
    assert features['feat_author_jaccard'] == 1.0
    assert features['feat_year_diff'] == 0.0
    assert features['feat_doi_match'] == 1.0
    assert features['feat_hierarchy_cite'] == 1.0
    assert clean_title('  A   B ') == 'a b'
