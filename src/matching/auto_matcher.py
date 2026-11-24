"""Gán nhãn heuristic trong phạm vi từng publication, có audit cho ca mơ hồ."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import unicodedata

import pyarrow as pa
import pyarrow.parquet as pq

from standardization.reference_links import normalize_doi_literal
from .candidate_pool import _read_parquet, build_candidate_pool
from .labeling import pool_fingerprint


POLICY = {
    'name': 'doi_or_title_surname_v1',
    'title_threshold': 0.85,
    'title_ratio': 'SequenceMatcher_autojunk_false_on_sorted_normalized_tokens',
    'doi_comparison': 'casefold_after_literal_validation',
    'ambiguity': 'needs_review',
}
AUTO_LABEL_SCHEMA = pa.schema([
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('citation_key', pa.string(), nullable=False),
    pa.field('groundtruth_arxiv_id', pa.string()),
    pa.field('label_status', pa.string(), nullable=False),
    pa.field('reason', pa.string(), nullable=False),
    pa.field('qualifying_candidates', pa.list_(pa.string()), nullable=False),
    pa.field('review_candidates', pa.list_(pa.string()), nullable=False),
])
EVIDENCE_SCHEMA = pa.schema([
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('citation_key', pa.string(), nullable=False),
    pa.field('ref_arxiv_id', pa.string(), nullable=False),
    pa.field('bib_doi_raw', pa.string()),
    pa.field('bib_doi', pa.string()),
    pa.field('ref_doi_raw_values', pa.list_(pa.string()), nullable=False),
    pa.field('ref_doi_values', pa.list_(pa.string()), nullable=False),
    pa.field('ref_doi_sources', pa.list_(pa.string()), nullable=False),
    pa.field('title_similarity', pa.float64(), nullable=False),
    pa.field('shared_surnames', pa.list_(pa.string()), nullable=False),
    pa.field('outcome', pa.string(), nullable=False),
])


def normalize_text(value):
    """Chuẩn hóa Unicode, dấu, chữ hoa và dấu câu cho title/tên."""
    text = unicodedata.normalize('NFKD', value or '').casefold()
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return ' '.join(re.findall(r'[^\W_]+', text, flags=re.UNICODE))


def title_token_sort_ratio(left, right):
    """Tỷ lệ [0, 1], so hai chuỗi token đã sắp xếp; trường thiếu trả 0."""
    left = ' '.join(sorted(normalize_text(left).split()))
    right = ' '.join(sorted(normalize_text(right).split()))
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def author_surnames(authors):
    """Họ trước dấu phẩy hoặc token cuối; bỏ hậu tố và tên chỉ có initial."""
    names = set()
    for author in authors or []:
        surname_part = author.split(',', 1)[0]
        tokens = normalize_text(surname_part).split()
        if tokens and tokens[-1] in {'jr', 'sr', 'ii', 'iii', 'iv'}:
            tokens.pop()
        if tokens and len(tokens[-1]) > 1:
            names.add(tokens[-1])
    return names


def _doi(value):
    if value is None or not value.strip():
        return None, False
    literal = re.sub(r'^doi:\s*', '', value.strip(), flags=re.I)
    normalized = normalize_doi_literal(literal)
    return (normalized.casefold(), False) if normalized else (None, True)


def _paper_dois(papers):
    if papers is None:
        return {}, 'Không cung cấp bảng papers; DOI chỉ lấy từ ref_doi nếu có.'
    if not isinstance(papers, pa.Table):
        papers = _read_parquet(papers)
    if 'arxiv_id' not in papers.column_names:
        raise ValueError('Bảng papers thiếu cột arxiv_id')
    if 'doi' not in papers.column_names:
        return {}, 'Bảng papers thiếu cột doi; chưa bổ sung được DOI từ corpus.'
    indexed = defaultdict(set)
    for row in papers.select(['arxiv_id', 'doi']).to_pylist():
        identifier, doi = row['arxiv_id'], row['doi']
        if not isinstance(identifier, str) or not identifier or identifier != identifier.strip():
            raise ValueError('arxiv_id trong papers phải là chuỗi không rỗng, không có khoảng trắng đầu/cuối')
        if doi is not None and not isinstance(doi, str):
            raise ValueError('doi trong papers phải là chuỗi hoặc null')
        if doi and doi.strip():
            indexed[identifier].add(doi)
    return dict(indexed), None


def _reference_doi(reference, paper_dois):
    """Union bằng chứng trực tiếp và papers; giữ mâu thuẫn để review."""
    sources = []
    raw_values = set()
    direct = reference['ref_doi']
    if direct and direct.strip():
        sources.append('references.ref_doi')
        raw_values.add(direct)
    supplemental = paper_dois.get(reference['ref_arxiv_id'], set())
    if supplemental:
        sources.append('papers.doi')
        raw_values.update(supplemental)
    values = set()
    invalid = False
    for raw in raw_values:
        normalized, malformed = _doi(raw)
        invalid = invalid or malformed
        if normalized:
            values.add(normalized)
    return sorted(raw_values), sorted(values), sources, invalid


@dataclass(frozen=True)
class HeuristicDecision:
    label: dict
    evidence: tuple[dict, ...]


def _iter_decisions(pool, paper_dois, publications, excluded_publications):
    entries = pool.entries.to_pylist()
    available = {entry['arxiv_id'] for entry in entries}
    selected = available if publications is None else set(publications)
    excluded = set(excluded_publications)
    if selected - available or excluded - available:
        raise ValueError('Danh sách publication có ID không xuất hiện trong BibTeX')
    selected = selected - excluded
    references = defaultdict(list)
    for reference in pool.references.to_pylist():
        references[reference['arxiv_id']].append((
            reference, _reference_doi(reference, paper_dois)))
    for entry in entries:
        publication = entry['arxiv_id']
        if publication not in selected:
            continue
        bib_doi, invalid_bib_doi = _doi(entry['doi'])
        surnames = author_surnames(entry['authors'])
        qualifying, review, evidence = [], [], []
        for reference, doi_info in references[publication]:
            raw_values, ref_dois, sources, invalid_ref_doi = doi_info
            ref_id = reference['ref_arxiv_id']
            similarity = title_token_sort_ratio(entry['title'], reference['ref_title'])
            shared = sorted(surnames & author_surnames(reference['ref_authors']))
            if invalid_bib_doi or invalid_ref_doi:
                outcome = 'invalid_doi'
                review.append(ref_id)
            elif len(ref_dois) > 1:
                outcome = 'reference_doi_conflict'
                review.append(ref_id)
            elif bib_doi and ref_dois:
                if bib_doi == ref_dois[0]:
                    outcome = 'doi_match'
                    qualifying.append(ref_id)
                else:
                    outcome = 'doi_mismatch'
            elif similarity >= POLICY['title_threshold'] and shared:
                outcome = 'title_author_match'
                qualifying.append(ref_id)
            else:
                outcome = 'below_threshold_or_no_shared_surname'
            evidence.append({
                'arxiv_id': publication, 'citation_key': entry['citation_key'],
                'ref_arxiv_id': ref_id, 'bib_doi_raw': entry['doi'], 'bib_doi': bib_doi,
                'ref_doi_raw_values': raw_values, 'ref_doi_values': ref_dois,
                'ref_doi_sources': sources, 'title_similarity': similarity,
                'shared_surnames': shared, 'outcome': outcome,
            })
        # Một ca DOI không xác định vẫn có thể cạnh tranh với match đã tìm thấy.
        if len(qualifying) > 1 or review:
            status, reason, target = 'needs_review', 'ambiguous_candidates_or_doi_evidence', None
        elif len(qualifying) == 1:
            status, reason, target = 'matched', 'unique_rule_match', qualifying[0]
        elif any(item['outcome'] == 'doi_mismatch' for item in evidence):
            status, reason, target = 'needs_review', 'doi_mismatch_without_match', None
            review = [item['ref_arxiv_id'] for item in evidence if item['outcome'] == 'doi_mismatch']
        else:
            status, reason, target = 'no_match', 'no_qualifying_candidate', None
        if not evidence:
            reason = 'no_candidates'
        label = dict(arxiv_id=publication, citation_key=entry['citation_key'],
            groundtruth_arxiv_id=target, label_status=status, reason=reason,
            qualifying_candidates=qualifying, review_candidates=review)
        yield HeuristicDecision(label, tuple(evidence))


def iter_heuristic_labels(pool, *, papers=None, publications=None, excluded_publications=()):
    """Trả từng quyết định entry cùng bằng chứng cặp; chưa gán partition."""
    paper_dois, _ = _paper_dois(papers)
    yield from _iter_decisions(pool, paper_dois, publications, excluded_publications)


def write_auto_labels(pool, output, *, papers=None, publications=None, excluded_publications=()):
    """Ghi nhãn/audit vào thư mục mới; chỉ tạo _SUCCESS sau khi ghi xong."""
    paper_dois, warning = _paper_dois(papers)
    publications = None if publications is None else sorted(set(publications))
    excluded_publications = sorted(set(excluded_publications))
    decisions = _iter_decisions(pool, paper_dois, publications, excluded_publications)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    counts = Counter()
    publication_counts = defaultdict(Counter)
    label_batch, evidence_batch = [], []
    with (pq.ParquetWriter(output / 'auto_labels.parquet', AUTO_LABEL_SCHEMA) as labels,
          pq.ParquetWriter(output / 'pair_evidence.parquet', EVIDENCE_SCHEMA) as evidence):
        for decision in decisions:
            label_batch.append(decision.label)
            if len(label_batch) >= 1024:
                labels.write_table(pa.Table.from_pylist(label_batch, schema=AUTO_LABEL_SCHEMA))
                label_batch.clear()
            for row in decision.evidence:
                evidence_batch.append(row)
                if len(evidence_batch) >= 8192:
                    evidence.write_table(pa.Table.from_pylist(evidence_batch, schema=EVIDENCE_SCHEMA))
                    evidence_batch.clear()
            counts[decision.label['label_status']] += 1
            publication_counts[decision.label['arxiv_id']][decision.label['label_status']] += 1
        if label_batch:
            labels.write_table(pa.Table.from_pylist(label_batch, schema=AUTO_LABEL_SCHEMA))
        if evidence_batch:
            evidence.write_table(pa.Table.from_pylist(evidence_batch, schema=EVIDENCE_SCHEMA))
    doi_records = {key: sorted(values) for key, values in sorted(paper_dois.items())}
    doi_hash = hashlib.sha256(json.dumps(doi_records, sort_keys=True).encode('utf-8')).hexdigest()
    report = {
        'policy': POLICY, 'candidate_pool_sha256': pool_fingerprint(pool),
        'papers_doi_sha256': doi_hash, 'publications': publications,
        'excluded_publications': excluded_publications, 'counts': dict(counts),
        'completed_publications': sorted(pub for pub, counts in publication_counts.items()
                                         if not counts['needs_review']),
        'review_publications': sorted(pub for pub, counts in publication_counts.items()
                                      if counts['needs_review']),
        'warnings': [warning] if warning else [],
    }
    (output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                                       encoding='utf-8')
    (output / '_SUCCESS').touch()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bibtex', type=Path, required=True)
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--papers', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--publication', action='append')
    parser.add_argument('--exclude-publication', action='append', default=[])
    args = parser.parse_args()
    pool = build_candidate_pool(args.bibtex, args.references)
    report = write_auto_labels(pool, args.out, papers=args.papers,
        publications=args.publication, excluded_publications=args.exclude_publication)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
