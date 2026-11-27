"""LightGBM training and Top-5 inference for reference matching (issue E2).

The module is deliberately self-contained: it consumes the E1 ground truth and
the M1/M2 published tables, trains on ``partition == 'train'`` and writes a
publication-scoped ``predictions_raw.parquet`` together with the model and the
feature-importance report.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .features import (
    FEATURE_NAMES,
    CitationIndex,
    TfidfTitleIndex,
    collect_titles,
    extract_features,
    text_of,
)

PARTITIONS = ('train', 'valid', 'test')
CANDIDATE_COLUMNS = tuple(f'cand_{index}' for index in range(1, 6))
PREDICTION_COLUMNS = (
    'arxiv_id',
    'partition',
    'citation_key',
    'groundtruth_arxiv_id',
    *CANDIDATE_COLUMNS,
)
PREDICTION_SCHEMA = pa.schema(
    [pa.field(name, pa.string(), nullable=True) for name in PREDICTION_COLUMNS]
)

_GROUNDTRUTH_COLUMNS = ('arxiv_id', 'citation_key', 'groundtruth_arxiv_id', 'partition')
_PAIR_COLUMNS = ('arxiv_id', 'citation_key', 'ref_arxiv_id')


def _read_parquet(path: str | Path) -> pd.DataFrame:
    return pq.read_table(str(path)).to_pandas()


def _require_columns(frame: pd.DataFrame, columns: Sequence[str], label: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f'{label} is missing columns: {", ".join(missing)}')


def _string_or_none(value: Any) -> str | None:
    text = text_of(value)
    return text or None


def discover_reference_path(root: str | Path) -> Path:
    """Find the most recently written valid ``references.parquet`` under ``root``."""

    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f'reference search root does not exist: {root}')
    root_parts = [part.lower() for part in root.parts]
    if any(part.startswith(('staging-', 'staging_'))
           or part in ('staging', 'staging_exports', '.staging_exports') for part in root_parts):
        raise FileNotFoundError(f'no valid references.parquet with manifest found under {root}')
    candidates = []
    for path in sorted(root.rglob('references.parquet')):
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        if any(part.startswith('.') or part.lower().startswith(('staging-', 'staging_'))
               or part.lower() in ('staging', 'staging_exports') for part in rel.parts):
            continue
        manifest_file = path.parent / 'manifest.json'
        if not manifest_file.is_file():
            continue
        try:
            manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
            if not isinstance(manifest, dict) or manifest.get('schema_version') != 3:
                continue
            expected = manifest.get('checksums', {}).get('references.parquet')
            if not isinstance(expected, str) or len(expected) != 64:
                continue
            int(expected, 16)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected.lower():
                continue
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        candidates.append(path)
    if not candidates:
        raise FileNotFoundError(f'no valid references.parquet with manifest found under {root}')
    return max(candidates, key=lambda path: (path.stat().st_mtime, str(path)))


def resolve_input_path(value: str | Path | None, default: str | Path) -> Path:
    """Resolve an optional CLI path, allowing a directory for references."""

    path = Path(value) if value is not None else Path(default)
    for candidate in (path.absolute(), path.resolve()):
        if any(part.lower().startswith(('staging-', 'staging_')) or part.lower() in
               ('staging', 'staging_exports', '.staging_exports') for part in candidate.parts):
            raise ValueError(f'staging input is not published: {path}')
    if path.is_dir():
        return discover_reference_path(path)
    if not path.exists():
        raise FileNotFoundError(f'input file does not exist: {path}')
    # A Parquet file inside a published snapshot is not a standalone input.
    if path.name == 'references.parquet' and ((path.parent / 'manifest.json').exists()
            or 'exports' in path.parts or 'exports' in path.resolve().parts):
        found = discover_reference_path(path.parent)
        if found != path:
            raise ValueError(f'invalid snapshot input: {path}')
    return path


def load_groundtruth(path: str | Path) -> pd.DataFrame:
    """Read and validate the four-column E1 ground-truth table."""

    frame = _read_parquet(path)
    _require_columns(frame, _GROUNDTRUTH_COLUMNS, 'groundtruth')
    frame = frame.copy()
    frame['arxiv_id'] = frame['arxiv_id'].map(text_of)
    frame['citation_key'] = frame['citation_key'].map(text_of)
    frame['partition'] = frame['partition'].map(lambda value: text_of(value).lower())
    invalid = sorted(set(frame['partition']) - set(PARTITIONS))
    if invalid:
        raise ValueError(f'invalid groundtruth partitions: {", ".join(invalid)}')
    if frame['arxiv_id'].eq('').any() or frame['citation_key'].eq('').any():
        raise ValueError('groundtruth arxiv_id and citation_key must be non-empty')
    return frame


def partition_by_publication(groundtruth: pd.DataFrame) -> dict[str, str]:
    """Return one partition per publication, rejecting conflicting labels."""

    result: dict[str, str] = {}
    for arxiv_id, group in groundtruth.groupby('arxiv_id', sort=False):
        values = set(group['partition'])
        if len(values) != 1:
            raise ValueError(f'publication {arxiv_id} has conflicting partitions: {sorted(values)}')
        result[arxiv_id] = next(iter(values))
    return result


def build_candidate_pairs(
    bibtex: pd.DataFrame,
    references: pd.DataFrame,
    arxiv_ids: set[str],
) -> tuple[list[tuple[str, str, str]], dict[tuple[str, str], Mapping[str, Any]], dict[tuple[str, str], Mapping[str, Any]]]:
    """Build the deterministic publication-scoped Cartesian candidate space."""

    _require_columns(bibtex, ('arxiv_id', 'citation_key'), 'bibtex')
    _require_columns(references, ('arxiv_id', 'ref_arxiv_id'), 'references')

    bib_selected = bibtex[bibtex['arxiv_id'].map(text_of).isin(arxiv_ids)].copy()
    ref_selected = references[references['arxiv_id'].map(text_of).isin(arxiv_ids)].copy()
    bib_selected['arxiv_id'] = bib_selected['arxiv_id'].map(text_of)
    bib_selected['citation_key'] = bib_selected['citation_key'].map(text_of)
    ref_selected['arxiv_id'] = ref_selected['arxiv_id'].map(text_of)
    ref_selected['ref_arxiv_id'] = ref_selected['ref_arxiv_id'].map(text_of)

    bib_selected = bib_selected.drop_duplicates(subset=['arxiv_id', 'citation_key'], keep='first')
    ref_selected = ref_selected[
        ref_selected['ref_arxiv_id'].ne('') & ref_selected['ref_arxiv_id'].notna()
    ].drop_duplicates(subset=['arxiv_id', 'ref_arxiv_id'], keep='first')

    bib_index: dict[tuple[str, str], Mapping[str, Any]] = {
        (row['arxiv_id'], row['citation_key']): row
        for row in bib_selected.sort_values(['arxiv_id', 'citation_key']).to_dict('records')
    }
    ref_index: dict[tuple[str, str], Mapping[str, Any]] = {
        (row['arxiv_id'], row['ref_arxiv_id']): row
        for row in ref_selected.sort_values(['arxiv_id', 'ref_arxiv_id']).to_dict('records')
    }
    refs_by_publication: dict[str, list[str]] = defaultdict(list)
    for arxiv_id, ref_id in sorted(ref_index):
        refs_by_publication[arxiv_id].append(ref_id)

    pairs = [
        (arxiv_id, citation_key, ref_id)
        for arxiv_id, citation_key in sorted(bib_index)
        for ref_id in refs_by_publication.get(arxiv_id, ())
    ]
    return pairs, bib_index, ref_index


def build_feature_frame(
    pairs: Sequence[tuple[str, str, str]],
    bib_index: Mapping[tuple[str, str], Mapping[str, Any]],
    ref_index: Mapping[tuple[str, str], Mapping[str, Any]],
    bibtex: pd.DataFrame,
    references: pd.DataFrame,
    elements: pd.DataFrame,
) -> tuple[pd.DataFrame, TfidfTitleIndex, CitationIndex]:
    """Compute the six E2 features for every candidate pair."""

    titles = TfidfTitleIndex().fit(collect_titles(bibtex, references))
    citations = CitationIndex.from_elements(elements)
    rows = []
    for arxiv_id, citation_key, ref_id in pairs:
        features = extract_features(
            bib_index[(arxiv_id, citation_key)],
            ref_index[(arxiv_id, ref_id)],
            titles=titles,
            citations=citations,
        )
        rows.append(
            {
                'arxiv_id': arxiv_id,
                'citation_key': citation_key,
                'ref_arxiv_id': ref_id,
                **features,
            }
        )
    frame = pd.DataFrame(rows, columns=[*_PAIR_COLUMNS, *FEATURE_NAMES])
    return frame, titles, citations


def training_labels(
    pairs: Sequence[tuple[str, str, str]],
    groundtruth: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray]:
    """Return a train-only pair mask and its binary labels."""

    truth: dict[tuple[str, str], str | None] = {
        (row.arxiv_id, row.citation_key): _string_or_none(row.groundtruth_arxiv_id)
        for row in groundtruth.loc[groundtruth['partition'].eq('train')].itertuples(index=False)
    }
    mask = np.array([(arxiv_id, citation_key) in truth for arxiv_id, citation_key, _ in pairs])
    labels = np.array(
        [
            1 if truth.get((arxiv_id, citation_key)) == ref_id else 0
            for arxiv_id, citation_key, ref_id in pairs
        ],
        dtype=np.int8,
    )
    return mask, labels


def train_model(features: pd.DataFrame, labels: np.ndarray) -> Any:
    """Fit the requested LightGBM binary classifier."""

    from lightgbm import LGBMClassifier

    model = LGBMClassifier(
        n_estimators=150,
        max_depth=5,
        learning_rate=0.05,
        random_state=42,
        n_jobs=1,
        verbosity=-1,
    )
    model.fit(features[list(FEATURE_NAMES)], labels)
    return model


def rank_predictions(
    pairs: pd.DataFrame,
    probabilities: np.ndarray,
    bib_index: Mapping[tuple[str, str], Mapping[str, Any]],
    groundtruth: pd.DataFrame,
    partitions: Mapping[str, str],
) -> pd.DataFrame:
    """Select the five highest-probability candidates per BibTeX entry."""

    scored = pairs[list(_PAIR_COLUMNS)].copy()
    scored['score'] = np.asarray(probabilities, dtype=float)
    scored = scored.sort_values(
        ['arxiv_id', 'citation_key', 'score', 'ref_arxiv_id'],
        ascending=[True, True, False, True],
        kind='mergesort',
    ).reset_index(drop=True)
    scored['rank'] = scored.groupby(['arxiv_id', 'citation_key'], sort=False).cumcount() + 1
    top = scored[scored['rank'] <= len(CANDIDATE_COLUMNS)]
    wide = top.pivot(index=['arxiv_id', 'citation_key'], columns='rank', values='ref_arxiv_id')

    truth: dict[tuple[str, str], str | None] = {
        (row.arxiv_id, row.citation_key): _string_or_none(row.groundtruth_arxiv_id)
        for row in groundtruth.itertuples(index=False)
    }
    base = [
        {
            'arxiv_id': arxiv_id,
            'citation_key': citation_key,
            'partition': partitions[arxiv_id],
            'groundtruth_arxiv_id': truth.get((arxiv_id, citation_key)),
        }
        for arxiv_id, citation_key in bib_index
        if arxiv_id in partitions
    ]
    predictions = pd.DataFrame(base)
    if predictions.empty:
        return pd.DataFrame(columns=list(PREDICTION_COLUMNS))

    predictions = predictions.merge(
        wide.reset_index().rename(columns={rank: CANDIDATE_COLUMNS[rank - 1] for rank in wide.columns}),
        on=['arxiv_id', 'citation_key'],
        how='left',
    ).reindex(columns=PREDICTION_COLUMNS)
    partition_order = {name: index for index, name in enumerate(PARTITIONS)}
    predictions['_order'] = predictions['partition'].map(partition_order).fillna(len(PARTITIONS))
    predictions = predictions.sort_values(
        ['_order', 'arxiv_id', 'citation_key'], kind='mergesort'
    ).drop(columns='_order')
    for column in PREDICTION_COLUMNS:
        predictions[column] = predictions[column].map(_string_or_none)
    return predictions[list(PREDICTION_COLUMNS)].reset_index(drop=True)


def feature_importance_frame(model: Any) -> pd.DataFrame:
    """Tabulate split and gain importances for all six features."""

    gains = list(model.booster_.feature_importance(importance_type='gain'))
    splits = list(model.feature_importances_)
    frame = pd.DataFrame(
        {
            'feature': list(FEATURE_NAMES[: len(splits)]),
            'split': splits,
            'gain': gains[: len(splits)],
        }
    )
    return frame.sort_values(['gain', 'split'], ascending=False).reset_index(drop=True)


def write_outputs(
    output_dir: str | Path,
    model: Any,
    importances: pd.DataFrame,
    predictions: pd.DataFrame,
    scored: pd.DataFrame,
) -> dict[str, str]:
    """Persist predictions, scores, checkpoint and the importance report."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    prediction_path = output_dir / 'predictions_raw.parquet'
    table = pa.Table.from_pandas(predictions, schema=PREDICTION_SCHEMA, preserve_index=False)
    pq.write_table(table, str(prediction_path))

    score_path = output_dir / 'candidate_scores.parquet'
    pq.write_table(pa.Table.from_pandas(scored, preserve_index=False), str(score_path))

    model_path = output_dir / 'lgbm_model.txt'
    model.booster_.save_model(str(model_path))

    importance_path = output_dir / 'feature_importances.csv'
    importances.to_csv(importance_path, index=False)

    chart_path: str | None = None
    try:
        import matplotlib

        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        figure, axis = plt.subplots(figsize=(8, 4))
        ordered = importances.sort_values('gain')
        axis.barh(ordered['feature'], ordered['gain'], color='#4c78a8')
        axis.set_xlabel('LightGBM gain')
        axis.set_title('Feature importances (gain)')
        figure.tight_layout()
        png_path = output_dir / 'feature_importances.png'
        figure.savefig(png_path, dpi=150)
        plt.close(figure)
        chart_path = str(png_path)
    except ImportError:
        pass

    outputs = {
        'predictions_raw': str(prediction_path),
        'candidate_scores': str(score_path),
        'model': str(model_path),
        'feature_importances': str(importance_path),
    }
    if chart_path is not None:
        outputs['feature_importances_chart'] = chart_path
    return outputs


def run(
    *,
    bibtex_path: str | Path,
    references_path: str | Path,
    elements_path: str | Path,
    groundtruth_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Run the full train-and-rank pipeline and return a JSON-safe summary."""

    bibtex = _read_parquet(bibtex_path)
    references = _read_parquet(references_path)
    elements = _read_parquet(elements_path) if Path(elements_path).exists() else pd.DataFrame()
    groundtruth = load_groundtruth(groundtruth_path)
    partitions = partition_by_publication(groundtruth)

    pairs, bib_index, ref_index = build_candidate_pairs(
        bibtex, references, set(partitions)
    )
    features, _, _ = build_feature_frame(
        pairs, bib_index, ref_index, bibtex, references, elements
    )
    train_mask, labels = training_labels(pairs, groundtruth)
    if not train_mask.any():
        raise ValueError('groundtruth produced no training pairs')
    model = train_model(features.loc[train_mask].reset_index(drop=True), labels[train_mask])

    probabilities = model.predict_proba(features[list(FEATURE_NAMES)])[:, 1]
    scored = features.copy()
    scored['score'] = probabilities
    predictions = rank_predictions(features, probabilities, bib_index, groundtruth, partitions)
    importances = feature_importance_frame(model)
    outputs = write_outputs(output_dir, model, importances, predictions, scored)

    return {
        'pairs_total': len(pairs),
        'training_pairs': int(train_mask.sum()),
        'positive_training_pairs': int(labels[train_mask].sum()),
        'predictions': len(predictions),
        'partitions': {
            partition: int(predictions['partition'].eq(partition).sum())
            for partition in PARTITIONS
        },
        'outputs': outputs,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bibtex', default='runtime/parsed/bibtex.parquet')
    parser.add_argument(
        '--references',
        default=None,
        help='references.parquet path, or a directory to search recursively',
    )
    parser.add_argument('--elements', default='runtime/parsed/elements.parquet')
    parser.add_argument('--groundtruth', default='runtime/matching/groundtruth.parquet')
    parser.add_argument('--output-dir', default='runtime/matching')
    parser.add_argument('--quiet', action='store_true')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    references_path = resolve_input_path(args.references, 'runtime/exports')
    summary = run(
        bibtex_path=Path(args.bibtex),
        references_path=references_path,
        elements_path=Path(args.elements),
        groundtruth_path=Path(args.groundtruth),
        output_dir=Path(args.output_dir),
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
