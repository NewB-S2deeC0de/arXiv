from pathlib import Path
from crawler.config import CrawlerConfig
from crawler.progress import ProgressStore
from crawler.export import export_snapshot, export_dataset_snapshot
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
    assert manifest['missing_expected_jobs']==0
    c.range['end_id']=2524
    assert export_snapshot(c,'missing-job')['missing_expected_jobs']==1
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


def test_export_dataset_snapshot_fast_staging_validates_and_exports(tmp_path, monkeypatch):
    import json
    from crawler.export import export_dataset_snapshot
    from crawler.storage import digest
    monkeypatch.chdir(tmp_path)

    c = CrawlerConfig()
    c.range['start_month'] = '2025-04'
    c.range['start_id'] = 2523
    c.range['end_month'] = '2025-04'
    c.range['end_id'] = 2523
    dataset_folder = 'datasets/2025-04_02523_to_2025-04_02523'
    c.paths['persistent_root'] = dataset_folder
    c.paths['scratch_root'] = 'scratch'
    c.validate()

    store = ProgressStore(dataset_folder)
    paper = '2504.02523'
    store.initialize([paper])

    # Populate discovery and references
    for stage, data in [
        ('discovery', {'version': 1, 'title': 'Test Paper', 'published': '2025-04-01'}),
        ('references', {'references': [{'ref_arxiv_id': '2401.00001', 'ref_title': 'Cited'}]})
    ]:
        key = (paper, 0, stage, 0)
        stg = tmp_path / f'stg_{stage}'; stg.mkdir(parents=True, exist_ok=True)
        (stg / 'normalized.json').write_text(json.dumps(data))
        store.storage.commit(key, stg)
        store.finish(key, 'succeeded')

    # Add a huge fake raw file in data/ that should NOT be staged
    raw_tex = store.storage.paper_dir(paper) / 'source.raw'
    raw_tex.write_text('huge ' * 1000)

    # Run export_dataset_snapshot with fast_staging=True
    manifest = export_dataset_snapshot(c, 'fast-snap-1', fast_staging=True)

    # Verify persistent_root is unchanged in config
    assert c.paths['persistent_root'] == dataset_folder

    final_dir = tmp_path / dataset_folder / 'exports/fast-snap-1'
    assert (final_dir / 'manifest.json').exists()
    assert (final_dir / 'papers.parquet').exists()
    assert (final_dir / 'references.parquet').exists()
    assert manifest['papers'] == 1
    assert manifest['references'] == 1

    # Verify all files in final_dir match manifest checksums
    for name, exp_hash in manifest['checksums'].items():
        assert digest(final_dir / name) == exp_hash

    # Verify staging scratch was cleaned up
    scratch_dir = tmp_path / 'scratch'
    assert not any(scratch_dir.glob('staging-*'))


def test_export_dataset_snapshot_locks_source_before_staging(tmp_path, monkeypatch):
    from crawler.export import export_dataset_snapshot
    from crawler.storage import atomic_json
    import pytest
    monkeypatch.chdir(tmp_path)

    c = CrawlerConfig()
    dataset_folder = 'datasets/2025-04_02523_to_2025-04_02523'
    c.paths['persistent_root'] = dataset_folder
    c.range['end_id'] = 2523
    store = ProgressStore(dataset_folder)
    store.initialize(['2504.02523'])

    # Stale active marker on source
    atomic_json(tmp_path / dataset_folder / 'progress/active_run.json', {'run_id': 'old-stale', 'previous': None})

    # Without takeover_stale=True, must fail before staging
    with pytest.raises(RuntimeError, match='Previous active marker exists'):
        export_dataset_snapshot(c, 'lock-fail', fast_staging=True, takeover_stale=False)

    # With takeover_stale=True, proceeds and cleans up lock
    manifest = export_dataset_snapshot(c, 'lock-ok', fast_staging=True, takeover_stale=True)
    assert manifest['run_id'] == 'lock-ok'


def test_export_dataset_snapshot_parity_direct_vs_fast(tmp_path, monkeypatch):
    import json
    from crawler.export import export_dataset_snapshot
    monkeypatch.chdir(tmp_path)

    c = CrawlerConfig()
    c.range['start_month'] = '2025-04'; c.range['start_id'] = 2523
    c.range['end_month'] = '2025-04'; c.range['end_id'] = 2523
    dataset_folder = 'datasets/2025-04_02523_to_2025-04_02523'
    c.paths['persistent_root'] = dataset_folder
    c.paths['scratch_root'] = 'scratch'
    c.validate()

    store = ProgressStore(dataset_folder); paper = '2504.02523'; store.initialize([paper])
    for stage, data in [
        ('discovery', {'version': 1, 'title': 'Test Paper', 'published': '2025-04-01'}),
        ('references', {'references': [{'ref_arxiv_id': '2401.00001', 'ref_title': 'Cited'}]})
    ]:
        key = (paper, 0, stage, 0)
        stg = tmp_path / f'stg_{stage}'; stg.mkdir(parents=True, exist_ok=True)
        (stg / 'normalized.json').write_text(json.dumps(data))
        store.storage.commit(key, stg); store.finish(key, 'succeeded')

    # Export direct
    manifest_direct = export_dataset_snapshot(c, 'snap-direct', fast_staging=False)
    # Export fast
    manifest_fast = export_dataset_snapshot(c, 'snap-fast', fast_staging=True)

    # Parity check: config_fingerprint, input_fingerprint, papers, references count
    assert manifest_direct['config_fingerprint'] == manifest_fast['config_fingerprint']
    assert manifest_direct['input_fingerprint'] == manifest_fast['input_fingerprint']
    assert manifest_direct['papers'] == manifest_fast['papers'] == 1
    assert manifest_direct['references'] == manifest_fast['references'] == 1

    # Parquet contents parity
    dir_p = pq.read_table(tmp_path / dataset_folder / 'exports/snap-direct/papers.parquet').to_pydict()
    fst_p = pq.read_table(tmp_path / dataset_folder / 'exports/snap-fast/papers.parquet').to_pydict()
    assert dir_p == fst_p

    dir_r = pq.read_table(tmp_path / dataset_folder / 'exports/snap-direct/references.parquet').to_pydict()
    fst_r = pq.read_table(tmp_path / dataset_folder / 'exports/snap-fast/references.parquet').to_pydict()
    assert dir_r == fst_r
    for name in ('version_references.parquet','reference_snapshots.parquet'):
        assert pq.read_table(tmp_path/dataset_folder/'exports/snap-direct'/name).to_pylist()==pq.read_table(tmp_path/dataset_folder/'exports/snap-fast'/name).to_pylist()
    for name in ('paper_metrics.csv','version_metrics.csv'):
        assert (tmp_path/dataset_folder/'exports/snap-direct'/name).read_bytes()==(tmp_path/dataset_folder/'exports/snap-fast'/name).read_bytes()


def test_export_dataset_snapshot_rejects_duplicate_run_id(tmp_path, monkeypatch):
    import pytest
    monkeypatch.chdir(tmp_path)
    c = CrawlerConfig(); c.range['end_id'] = 2523
    dataset_folder = 'datasets/2025-04_02523_to_2025-04_02523'
    c.paths['persistent_root'] = dataset_folder
    store = ProgressStore(dataset_folder); store.initialize(['2504.02523'])

    export_dataset_snapshot(c, 'dup-run', fast_staging=False)
    with pytest.raises(FileExistsError, match="Export run ID 'dup-run' already exists"):
        export_dataset_snapshot(c, 'dup-run', fast_staging=True)


def test_export_dataset_snapshot_absolute_scratch_root_succeeds(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.chdir(tmp_path)
    c = CrawlerConfig(); c.range['end_id'] = 2523
    dataset_folder = 'datasets/2025-04_02523_to_2025-04_02523'
    c.paths['persistent_root'] = dataset_folder
    store = ProgressStore(dataset_folder); store.initialize(['2504.02523'])

    with tempfile.TemporaryDirectory() as external_scratch:
        # Pass absolute external scratch root outside cwd
        manifest = export_dataset_snapshot(c, 'abs-scratch', fast_staging=True, scratch_root=external_scratch)
        assert manifest['run_id'] == 'abs-scratch'
        assert (tmp_path / dataset_folder / 'exports/abs-scratch/manifest.json').exists()


def test_export_dataset_snapshot_publish_staging_outside_exports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = CrawlerConfig(); c.range['end_id'] = 2523
    dataset_folder = 'datasets/2025-04_02523_to_2025-04_02523'
    c.paths['persistent_root'] = dataset_folder
    store = ProgressStore(dataset_folder); store.initialize(['2504.02523'])

    replaced_paths = []
    orig_replace = Path.replace

    def tracked_replace(self, target):
        replaced_paths.append((str(self), str(target)))
        return orig_replace(self, target)

    monkeypatch.setattr(Path, 'replace', tracked_replace)
    manifest = export_dataset_snapshot(c, 'staging-loc', fast_staging=True)
    assert manifest['run_id'] == 'staging-loc'

    # Filter for the export directory promotion replace
    export_replaces = [p for p in replaced_paths if p[1].endswith(f'{dataset_folder}/exports/staging-loc')]
    assert len(export_replaces) == 1
    src_path, dst_path = export_replaces[0]
    assert '.staging_exports' in src_path
    assert f'{dataset_folder}/exports/' not in src_path


def test_export_dataset_snapshot_cleans_drive_staging_on_error(tmp_path, monkeypatch):
    import pytest
    from crawler import export as export_mod
    monkeypatch.chdir(tmp_path)
    c = CrawlerConfig(); c.range['end_id'] = 2523
    dataset_folder = 'datasets/2025-04_02523_to_2025-04_02523'
    c.paths['persistent_root'] = dataset_folder
    store = ProgressStore(dataset_folder); store.initialize(['2504.02523'])

    orig_digest = export_mod.digest
    def failing_digest(path):
        # Fail only when verifying destination files on Drive staging
        if '.staging_exports' in str(path):
            return 'corrupted_hash'
        return orig_digest(path)

    monkeypatch.setattr(export_mod, 'digest', failing_digest)

    with pytest.raises(OSError, match='Destination checksum verification failed'):
        export_dataset_snapshot(c, 'fail-cleanup', fast_staging=True)

    # Verify both final export and Drive publish staging are clean
    assert not (tmp_path / dataset_folder / 'exports/fail-cleanup').exists()
    staging_drive = tmp_path / dataset_folder / '.staging_exports'
    if staging_drive.exists():
        assert not any(staging_drive.iterdir())


def test_export_dataset_snapshot_job_directed_staging_skips_raw_files(tmp_path, monkeypatch):
    import json
    monkeypatch.chdir(tmp_path)
    c = CrawlerConfig(); c.range['end_id'] = 2523
    dataset_folder = 'datasets/2025-04_02523_to_2025-04_02523'
    c.paths['persistent_root'] = dataset_folder
    store = ProgressStore(dataset_folder); store.initialize(['2504.02523'])

    key = ('2504.02523', 0, 'discovery', 0)
    stg = tmp_path / 'stg'; stg.mkdir()
    (stg / 'normalized.json').write_text(json.dumps({'version': 1, 'title': 'T'}))
    store.storage.commit(key, stg); store.finish(key, 'succeeded')

    # Add raw file and an unindexed job directory in data/
    res_dir = store.storage.result_dir(key)
    (res_dir / 'source.tar.gz').write_text('raw archive content')
    unindexed = tmp_path / dataset_folder / 'data/unindexed_garbage'
    unindexed.mkdir(parents=True)
    (unindexed / 'normalized.json').write_text('{}')

    # Intercept staging copy
    copied_sources = []
    orig_copyfile = __import__('shutil').copyfile

    def tracked_copyfile(src, dst, *args, **kwargs):
        copied_sources.append(str(src))
        return orig_copyfile(src, dst, *args, **kwargs)

    monkeypatch.setattr('shutil.copyfile', tracked_copyfile)

    manifest = export_dataset_snapshot(c, 'job-directed', fast_staging=True)
    assert manifest['run_id'] == 'job-directed'

    # Confirm raw archive and unindexed garbage were never copied
    assert not any('source.tar.gz' in s for s in copied_sources)
    assert not any('unindexed_garbage' in s for s in copied_sources)






