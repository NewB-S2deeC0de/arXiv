from crawler.config import CrawlerConfig
from crawler.progress import ProgressStore
from crawler.export import export_snapshot
import pyarrow.parquet as pq

def test_missing_metadata_stays_in_ledger_only(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.range['end_id']=2523
    ProgressStore('runtime').initialize(['2504.02523'])
    manifest=export_snapshot(c,'test-run')
    table=pq.read_table('runtime/exports/test-run/papers.parquet')
    assert table.num_rows==0
    assert manifest['papers']==0
    assert manifest['assigned_ids']==1
    assert manifest['missing_metadata']==1
    assert (tmp_path/'runtime/exports/test-run/paper_metrics.csv').exists()
    assert (tmp_path/'runtime/exports/test-run/version_metrics.csv').exists()
    assert manifest['input_fingerprint']


def test_version_metrics_exposes_metadata_status(tmp_path, monkeypatch):
    import csv
    monkeypatch.chdir(tmp_path)
    config = CrawlerConfig(); config.range['end_id'] = 2523
    store = ProgressStore('runtime'); store.initialize(['2504.02523'])
    key = ('2504.02523', 1, 'source', 0)
    store.add(key)
    store.finish(key, 'failed', 'test')
    metadata_key = ('2504.02523', 1, 'metadata', 0)
    store.add(metadata_key)
    store.finish(metadata_key, 'failed', 'test')
    export_snapshot(config, 'metadata-status')
    with (tmp_path/'runtime/exports/metadata-status/version_metrics.csv').open() as file:
        rows = list(csv.DictReader(file))
    assert rows[0]['metadata_status'] == 'failed'


def test_export_venue_and_no_full_source_hash(tmp_path,monkeypatch):
    import json
    from crawler.storage import Storage
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.range['end_id']=2523
    store=ProgressStore('runtime'); store.initialize(['2504.02523'])
    paper='2504.02523'
    for stage,data in [('discovery',dict(version=1,title='Title',published='2025-04-01')),
                       ('references',dict(references=[dict(ref_arxiv_id=paper,venue='Journal A')]))]:
        key=(paper,0,stage,0); staging=tmp_path/stage; staging.mkdir()
        (staging/'normalized.json').write_text(json.dumps(data))
        store.storage.commit(key,staging); store.finish(key,'succeeded')
    def forbidden(*a): raise AssertionError('full verification during export')
    monkeypatch.setattr(Storage,'verify',forbidden)
    manifest=export_snapshot(c,'venue')
    row=pq.read_table('runtime/exports/venue/papers.parquet').to_pylist()[0]
    assert row['venue']=='Journal A'
    assert row['venue_source']=='semantic_scholar.citedPaper.venue'
    assert manifest['schema_version']==3


def test_export_filters_not_found_and_outside_range(tmp_path,monkeypatch):
    import json,csv
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.range['end_id']=2523
    store=ProgressStore('runtime'); store.initialize(['2504.02523','2504.02524'])
    for paper,status,data in [('2504.02523','not_found',{'not_found':True}),
                              ('2504.02524','succeeded',{'version':1})]:
        staging=tmp_path/paper; staging.mkdir()
        (staging/'normalized.json').write_text(json.dumps(data))
        key=(paper,0,'discovery',0)
        store.storage.commit(key,staging,status); store.finish(key,status)
    manifest=export_snapshot(c,'filtered')
    assert manifest['papers']==0 and manifest['assigned_ids']==1
    with open('runtime/exports/filtered/paper_metrics.csv') as f: rows=list(csv.DictReader(f))
    assert len(rows)==1 and rows[0]['discovery_status']=='not_found'
    assert rows[0]['metadata_exported']=='False'


def test_conflicting_venues_stay_null_and_corrupt_json_excluded(tmp_path,monkeypatch):
    import json
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.range['end_id']=2523
    store=ProgressStore('runtime'); paper='2504.02523'; store.initialize([paper])
    for stage,data in [('discovery',dict(version=1,title='Title')),
                       ('references',dict(references=[dict(ref_arxiv_id=paper,venue='A'),dict(ref_arxiv_id=paper,venue='B')]))]:
        key=(paper,0,stage,0); staging=tmp_path/stage; staging.mkdir()
        (staging/'normalized.json').write_text(json.dumps(data))
        store.storage.commit(key,staging); store.finish(key,'succeeded')
    manifest=export_snapshot(c,'conflict')
    assert manifest['venue_conflicts']==[paper]
    assert pq.read_table('runtime/exports/conflict/papers.parquet').to_pylist()[0]['venue'] is None
    (store.storage.result_dir((paper,0,'discovery',0))/'normalized.json').write_text('{"version":1,"title":"modified"}')
    manifest=export_snapshot(c,'corrupt')
    assert manifest['papers']==0 and manifest['missing_metadata']==1


def test_version_exports_preserve_provenance_and_dedupe_paper_pairs(tmp_path,monkeypatch):
    import json
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.range['end_id']=2523
    paper='2504.02523'; store=ProgressStore('runtime'); store.initialize([paper])
    for key,data in [((paper,0,'discovery',0),dict(version=2,title='T'))]+[
        ((paper,v,'references',0),dict(references=[dict(ref_arxiv_id='2401.00001',ref_title='R')],
            snapshot_id=f'snapshot-{v}',requested_identifier=f'URL:https://arxiv.org/abs/{paper}v{v}',
            version_correspondence='unverified')) for v in (1,2)]:
        store.add(key); staging=tmp_path/f'{key[1]}-{key[2]}'; staging.mkdir()
        (staging/'normalized.json').write_text(json.dumps(data))
        store.storage.commit(key,staging); store.finish(key,'succeeded')
    export_snapshot(c,'versions')
    assert pq.read_table('runtime/exports/versions/references.parquet').num_rows==1
    rows=pq.read_table('runtime/exports/versions/version_references.parquet').to_pylist()
    assert {r['version'] for r in rows}=={1,2}
    assert {r['snapshot_id'] for r in rows}=={'snapshot-1','snapshot-2'}
    assert all(r['version_correspondence']=='unverified' for r in rows)
    snapshots=pq.read_table('runtime/exports/versions/reference_snapshots.parquet').to_pylist()
    assert len(snapshots)==3  # Includes old v0 ledger, without assigning it to any version.


def test_export_snapshot_takeover_stale(tmp_path, monkeypatch):
    import json
    from crawler.storage import atomic_json
    monkeypatch.chdir(tmp_path)
    c = CrawlerConfig(); c.range['end_id'] = 2523
    ProgressStore('runtime').initialize(['2504.02523'])
    # Simulate a stale active run marker left by a killed process
    active_marker = tmp_path / 'runtime/progress/active_run.json'
    atomic_json(active_marker, {'run_id': 'stale-run-123', 'previous': None})

    import pytest
    with pytest.raises(RuntimeError, match='Previous active marker exists'):
        export_snapshot(c, 'takeover-fail', takeover_stale=False)

    # With takeover_stale=True, export should proceed successfully
    manifest = export_snapshot(c, 'takeover-ok', takeover_stale=True)
    assert manifest['run_id'] == 'takeover-ok'


def test_export_snapshot_telemetry_from_completed_json(tmp_path, monkeypatch):
    import json
    from crawler.storage import atomic_json
    monkeypatch.chdir(tmp_path)
    c = CrawlerConfig(); c.range['end_id'] = 2523
    store = ProgressStore('runtime'); store.initialize(['2504.02523'])
    key = ('2504.02523', 0, 'discovery', 0)
    staging = tmp_path / 'disc'; staging.mkdir()
    (staging / 'normalized.json').write_text(json.dumps({'version': 1, 'title': 'T'}))
    store.storage.commit(key, staging); store.finish(key, 'succeeded')

    # Add two run summaries in metrics/ (simulating cumulative summaries)
    m1 = tmp_path / 'runtime/metrics/run-1'; m1.mkdir(parents=True)
    atomic_json(m1 / 'summary.json', {
        'run_id': 'run-1', 'wall_seconds': 100.0,
        'api_wait_seconds': {'arxiv': {'pacing_seconds': 50.0, 'rate_limit_seconds': 10.0},
                             's2': {'pacing_seconds': 0.0, 'rate_limit_seconds': 0.0},
                             'total_rate_limit_seconds': 10.0, 'effective_crawler_seconds': 90.0}
    })
    m2 = tmp_path / 'runtime/metrics/run-2'; m2.mkdir(parents=True)
    atomic_json(m2 / 'summary.json', {
        'run_id': 'run-2', 'wall_seconds': 20.0,
        'contributing_run_ids': ['run-1', 'run-2'],
        'api_wait_seconds': {'arxiv': {'pacing_seconds': 100.0, 'rate_limit_seconds': 20.0},
                             's2': {'pacing_seconds': 0.0, 'rate_limit_seconds': 0.0},
                             'total_rate_limit_seconds': 20.0, 'effective_crawler_seconds': 100.0}
    })
    # Add completed.json at root with authoritative cumulative metrics
    atomic_json(tmp_path / 'runtime/completed.json', {
        'run_id': 'run-2',
        'active_wall_seconds': 120.0,
        'wall_seconds': 20.0,
        'contributing_run_ids': ['run-1', 'run-2'],
        'api_wait_seconds': {'arxiv': {'pacing_seconds': 100.0, 'rate_limit_seconds': 20.0},
                             's2': {'pacing_seconds': 0.0, 'rate_limit_seconds': 0.0},
                             'total_rate_limit_seconds': 20.0, 'effective_crawler_seconds': 100.0}
    })

    manifest = export_snapshot(c, 'telemetry-test')
    # Must match the authoritative 100s pacing, NOT sum 50 + 100 = 150
    assert manifest['api_wait_seconds']['arxiv']['pacing_seconds'] == 100.0
    assert manifest['api_wait_seconds']['total_rate_limit_seconds'] == 20.0
    assert manifest['api_wait_seconds']['effective_crawler_seconds'] == 100.0

    # Also test case where completed.json is missing, but latest summary covers previous run_ids
    (tmp_path / 'runtime/completed.json').unlink()
    manifest_no_marker = export_snapshot(c, 'telemetry-no-marker')
    assert manifest_no_marker['api_wait_seconds']['arxiv']['pacing_seconds'] == 100.0


def test_cli_export_with_folder(tmp_path, monkeypatch):
    import sys
    from crawler.cli import main
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/crawler.yaml').write_text(
        "schema_version: 1\n"
        "range:\n"
        "  start_month: '2025-04'\n"
        "  start_id: 1\n"
        "  end_month: '2025-04'\n"
        "  end_id: 10\n"
        "paths:\n"
        "  persistent_root: runtime\n"
        "  scratch_root: runtime/temp\n"
    )
    custom_folder = tmp_path / 'datasets/2025-04_02523_to_2025-04_02523'
    store = ProgressStore(custom_folder)
    store.initialize(['2504.02523'])

    monkeypatch.setattr(sys, 'argv', [
        'crawler.cli', 'export',
        '--run-id', 'cli-run-1',
        '--folder', str(custom_folder),
        '--takeover-stale'
    ])
    main()

    export_target = custom_folder / 'exports/cli-run-1'
    assert (export_target / 'manifest.json').exists()
    assert (export_target / 'papers.parquet').exists()



