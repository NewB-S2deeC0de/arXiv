"""Pairwise feature extraction for Milestone 3 reference matching (E2).

The six features are intentionally cheap enough to compute for the full
publication-scoped candidate matrix.  All numeric outputs are plain floats so
they can be consumed directly by LightGBM.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import math
import re
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from rapidfuzz import fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

FEATURE_NAMES = (
    'feat_title_token_sort',
    'feat_title_char_tfidf',
    'feat_author_jaccard',
    'feat_year_diff',
    'feat_doi_match',
    'feat_hierarchy_cite',
)

#: Missing publication years are treated as five years apart (issue E2).
MISSING_YEAR_DIFF = 5.0

#: The parser stores sentence elements with the full ``ElementKind`` string;
#: the short prefix is accepted as well for hand-made/shard tables.
_SENTENCE_KINDS = frozenset({'sentence', 's'})
_YEAR_RE = re.compile(r'(?<!\d)(\d{4})(?!\d)')
_WORD_RE = re.compile(r'[^\w\s]+', re.UNICODE)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(result) if isinstance(result, (bool, int)) else False


def text_of(value: Any) -> str:
    """Return a stripped string for a scalar value, or ``''`` when missing."""

    if _is_missing(value):
        return ''
    return str(value).strip()


def clean_title(value: Any) -> str:
    """Lowercase and collapse whitespace before similarity scoring."""

    return ' '.join(text_of(value).lower().split())


def normalize_doi(value: Any) -> str:
    """Normalize DOI literals with the lowercase/strip rule from issue E2."""

    return text_of(value).lower()


def authors_of(value: Any) -> tuple[str, ...]:
    """Accept list-like author fields or a comma-separated string."""

    if _is_missing(value):
        return ()
    if isinstance(value, str):
        raw = value.split(',')
    elif isinstance(value, Mapping):
        raw = (value.get('name'),)
    else:
        try:
            raw = tuple(value)
        except TypeError:
            raw = (value,)
    return tuple(item for item in (text_of(part) for part in raw) if item)


def author_surnames(value: Any) -> frozenset[str]:
    """Extract the surname of each author.

    ``Family, Given`` uses the first part; ``Given Family`` uses the last token.
    """

    surnames: set[str] = set()
    for author in authors_of(value):
        if ',' in author:
            surname = author.split(',', 1)[0]
        else:
            parts = clean_title(_WORD_RE.sub(' ', author)).split()
            surname = parts[-1] if parts else ''
        surname = clean_title(surname).strip(' .,-')
        if surname:
            surnames.add(surname)
    return frozenset(surnames)


def parse_year(value: Any) -> int | None:
    """Parse a year from an integer, float or ISO-like string."""

    if _is_missing(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(float(value)):
            return int(value)
    match = _YEAR_RE.search(text_of(value))
    return int(match.group(1)) if match else None


def jaccard(left: Iterable[Any], right: Iterable[Any]) -> float:
    """Jaccard similarity of two token sets; empty sets score zero."""

    left_set = frozenset(text_of(item).lower() for item in left)
    right_set = frozenset(text_of(item).lower() for item in right)
    if not left_set and not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def year_diff(year_bib: Any, year_ref: Any) -> float:
    """Absolute year gap, defaulting to five years when either side is absent."""

    left = parse_year(year_bib)
    right = parse_year(year_ref)
    if left is None or right is None:
        return MISSING_YEAR_DIFF
    return float(abs(left - right))


def doi_match(doi_bib: Any, doi_ref: Any) -> float:
    """Return 1.0 only when both normalized DOIs are present and equal."""

    left = normalize_doi(doi_bib)
    right = normalize_doi(doi_ref)
    return 1.0 if left and right and left == right else 0.0


@dataclass
class TfidfTitleIndex:
    """Corpus-fitted char n-gram TF-IDF index for title cosine similarity."""

    vectorizer: TfidfVectorizer | None = None
    _vectors: dict[str, Any] = field(default_factory=dict, repr=False)

    def fit(self, titles: Iterable[Any]) -> TfidfTitleIndex:
        corpus = [clean_title(title) or 'unknown' for title in titles]
        self.vectorizer = TfidfVectorizer(
            analyzer='char',
            ngram_range=(3, 5),
            lowercase=True,
            dtype=np.float64,
        )
        self.vectorizer.fit(corpus or ['unknown'])
        self._vectors.clear()
        return self

    def _vector(self, title: str) -> Any:
        if self.vectorizer is None:
            raise RuntimeError('TfidfTitleIndex.fit() must be called first')
        if title not in self._vectors:
            self._vectors[title] = self.vectorizer.transform([title])
        return self._vectors[title]

    def similarity(self, title_bib: Any, title_ref: Any) -> float:
        left = clean_title(title_bib)
        right = clean_title(title_ref)
        if not left or not right:
            return 0.0
        score = cosine_similarity(self._vector(left), self._vector(right))
        return float(score[0, 0])


@dataclass
class CitationIndex:
    """Sentence text from ``elements.parquet`` grouped by publication."""

    text_by_arxiv_id: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_elements(cls, elements: str | Any) -> CitationIndex:
        if isinstance(elements, (str, bytes)) or hasattr(elements, '__fspath__'):
            frame = pq.read_table(str(elements)).to_pandas()
        elif isinstance(elements, pd.DataFrame):
            frame = elements
        else:
            frame = pd.DataFrame(elements)
        if frame is None or frame.empty or 'arxiv_id' not in frame.columns:
            return cls()
        if 'kind' not in frame.columns or 'content' not in frame.columns:
            raise ValueError('elements table needs arxiv_id, kind and content columns')

        sentences = frame[frame['kind'].astype(str).isin(_SENTENCE_KINDS)]
        grouped: dict[str, str] = {}
        for arxiv_id, group in sentences.groupby('arxiv_id', sort=False):
            grouped[text_of(arxiv_id)] = '\n'.join(
                text_of(content) for content in group['content']
            )
        return cls(text_by_arxiv_id=grouped)

    def contains(self, arxiv_id: Any, citation_key: Any) -> float:
        key = text_of(citation_key)
        if not key:
            return 0.0
        body = self.text_by_arxiv_id.get(text_of(arxiv_id), '')
        return 1.0 if key in body else 0.0


def _first_value(record: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in record and not _is_missing(record[name]):
            return record[name]
    return None


def _first_text(record: Mapping[str, Any], names: Sequence[str]) -> str:
    value = _first_value(record, names)
    if value is not None:
        text = text_of(value)
        if text:
            return text
    external = record.get('external_ids') or record.get('externalIds')
    if isinstance(external, Mapping):
        for name in ('DOI', 'doi'):
            text = text_of(external.get(name))
            if text:
                return text
    return ''


def _reference_title(reference: Mapping[str, Any]) -> str:
    title = _first_text(reference, ('ref_title', 'title'))
    return title


def collect_titles(bibtex: Any, references: Any) -> list[str]:
    """Collect every known title for fitting the char TF-IDF vocabulary."""

    titles: list[str] = []
    for frame, columns in (
        (bibtex, ('title',)),
        (references, ('ref_title', 'title')),
    ):
        if frame is None:
            continue
        for column in columns:
            if column in getattr(frame, 'columns', ()):
                titles.extend(frame[column].tolist())
    return titles


def extract_features(
    bib_entry: Mapping[str, Any],
    reference: Mapping[str, Any],
    *,
    titles: TfidfTitleIndex,
    citations: CitationIndex,
) -> dict[str, float]:
    """Compute the six E2 features for one ``(bibtex, reference)`` pair."""

    bib_title = clean_title(_first_text(bib_entry, ('title',)))
    ref_title = clean_title(_reference_title(reference))
    surnames_bib = author_surnames(_first_value(bib_entry, ('authors',)))
    surnames_ref = author_surnames(_first_value(reference, ('ref_authors', 'authors')))
    year_bib = parse_year(_first_value(bib_entry, ('year',)))
    year_ref = parse_year(_first_value(reference, ('ref_year', 'year', 'ref_submission_date')))
    doi_bib = _first_text(bib_entry, ('doi',))
    doi_ref = _first_text(reference, ('ref_doi', 'doi'))

    return {
        'feat_title_token_sort': float(fuzz.token_sort_ratio(bib_title, ref_title) / 100.0),
        'feat_title_char_tfidf': titles.similarity(bib_title, ref_title),
        'feat_author_jaccard': float(jaccard(surnames_bib, surnames_ref)),
        'feat_year_diff': year_diff(year_bib, year_ref),
        'feat_doi_match': doi_match(doi_bib, doi_ref),
        'feat_hierarchy_cite': citations.contains(
            bib_entry.get('arxiv_id'), bib_entry.get('citation_key')
        ),
    }
