import json
import csv
import re
from pathlib import Path
from dataclasses import asdict
import hashlib
import pyarrow as pa
import pyarrow.parquet as pq
from .progress import ProgressStore
from .ids import generate_ids
from .storage import atomic_json,digest

PAPER_SCHEMA=pa.schema([pa.field('arxiv_id',pa.string(),False),pa.field('title',pa.string()),pa.field('authors',pa.list_(pa.string())),pa.field('submission_date',pa.string()),pa.field('revised_dates',pa.list_(pa.string())),pa.field('venue',pa.string()),pa.field('venue_source',pa.string()),pa.field('discovery_status',pa.string())])
REFERENCE_SCHEMA=pa.schema([pa.field('arxiv_id',pa.string(),False),pa.field('ref_arxiv_id',pa.string(),False),pa.field('ref_title',pa.string()),pa.field('ref_authors',pa.list_(pa.string())),pa.field('ref_submission_date',pa.string()),pa.field('ref_s2_id',pa.string())])

VERSION_REFERENCE_SCHEMA=pa.schema(list(REFERENCE_SCHEMA)+[
    pa.field('version',pa.int64(),False),pa.field('snapshot_id',pa.string()),
    pa.field('requested_identifier',pa.string()),pa.field('version_correspondence',pa.string())])
SNAPSHOT_SCHEMA=pa.schema([pa.field('arxiv_id',pa.string(),False),pa.field('version',pa.int64(),False),
    pa.field('scope',pa.string()),pa.field('status',pa.string()),pa.field('snapshot_id',pa.string()),
    pa.field('requested_identifier',pa.string()),pa.field('version_correspondence',pa.string()),
    pa.field('reference_count',pa.int64()),pa.field('fetched_at',pa.string()),
    pa.field('query_fingerprint',pa.string()),pa.field('content_sha256',pa.string())])

def _export_snapshot(config,run_id,*,runtime_root=None,logical_config=None,output_root=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]+',run_id): raise ValueError('Invalid run ID')
    effective_root=Path(runtime_root) if runtime_root is not None else Path(config.paths['persistent_root'])
    target=(Path(output_root) if output_root is not None else effective_root/'exports')/run_id
    if target.exists(): raise FileExistsError(f"Export run ID '{run_id}' already exists")
    target.mkdir(parents=True,exist_ok=False)
    store=ProgressStore(effective_root)
    selected=set(generate_ids(**config.range))
    missing_expected_jobs=sum((paper,0,'discovery',0) not in store.rows for paper in selected)
    for paper in sorted(selected):
        store.add((paper,0,'discovery',0))
    cache={}
    def read_result(key, filename='normalized.json'):
        token=(key,filename)
        if token not in cache:
            cache[token]=None
            try:
                directory=store.storage.result_dir(key)
                marker=json.loads((directory/'complete.json').read_text())
                allowed=('succeeded','not_found') if key[2] in ('discovery','metadata') else ('succeeded','no_tex')
                if marker['key']!=list(key) or marker['status'] not in allowed: return None
                # Verify only the JSON consumed by export, reading file once into bytes to halve FUSE reads.
                payload_bytes=(directory/filename).read_bytes()
                if hashlib.sha256(payload_bytes).hexdigest()!=marker['checksums'][filename]: return None
                cache[token]=json.loads(payload_bytes.decode('utf8'))
            except (OSError,ValueError,KeyError): pass
        return cache[token]
    snapshot_rows=[]; snapshots_by_paper={}
    for key,row in sorted(store.rows.items()):
        if key[0] not in selected or key[2:]!=('references',0): continue
        snapshot=read_result(key)
        if snapshot is not None: snapshots_by_paper.setdefault(key[0],[]).append((key,snapshot))
        data=snapshot or {}
        snapshot_rows.append(dict(arxiv_id=key[0],version=key[1],
            scope='version_query' if key[1] else 'legacy_paper',status=row['status'],
            snapshot_id=data.get('snapshot_id'),requested_identifier=data.get('requested_identifier'),
            version_correspondence=data.get('version_correspondence','unverified'),
            reference_count=len(data['references']) if snapshot is not None else None,
            fetched_at=data.get('fetched_at'),query_fingerprint=data.get('query_fingerprint'),
            content_sha256=data.get('content_sha256')))
    version_references={}
    venue_candidates={}
    for key in store.rows:
        if key[0] not in selected or key[2:]!=('references',0): continue
        snapshot=read_result(key)
        if snapshot:
            for ref in snapshot['references']:
                venue=ref.get('venue')
                if ref.get('ref_arxiv_id') and isinstance(venue,str) and venue.strip():
                    venue_candidates.setdefault(ref['ref_arxiv_id'],set()).add(venue.strip())
    papers=[]; references={}; missing=0; duplicates=0; conflicts=[]; revision_missing=0
    for key in sorted(store.rows):
        if key[2]!='discovery' or key[0] not in selected: continue
        paper=key[0]; meta={}; revisions=[]
        meta=read_result(key) or {}
        if store.rows[key]['status']!='succeeded' or not meta or meta.get('not_found'):
            missing+=1
            continue
        for version in range(2,meta.get('version',0)+1):
            vkey=(paper,version,'metadata',0)
            version_meta=read_result(vkey)
            if version_meta:
                value=version_meta.get('updated')
                if value: revisions.append(value)
                else: revision_missing+=1
            else: revision_missing+=1
        venues=venue_candidates.get(paper,set())
        venue=next(iter(venues)) if len(venues)==1 else None
        papers.append(dict(arxiv_id=paper,title=meta.get('title'),authors=meta.get('authors'),submission_date=meta.get('published'),revised_dates=revisions if meta else None,venue=venue,venue_source='semantic_scholar.citedPaper.venue' if venue else None,discovery_status='succeeded'))
        for rkey,snapshot in snapshots_by_paper.get(paper,[]):
            for ref in snapshot['references']:
                if not ref.get('ref_arxiv_id'): continue
                row={field.name:ref.get(field.name) for field in REFERENCE_SCHEMA}; row['arxiv_id']=paper
                if rkey[1]>0:
                    version_references[(paper,rkey[1],row['ref_arxiv_id'])]=dict(row,version=rkey[1],
                        snapshot_id=snapshot.get('snapshot_id'),requested_identifier=snapshot.get('requested_identifier'),
                        version_correspondence=snapshot.get('version_correspondence','unverified'))
                unique=(paper,row['ref_arxiv_id'])
                if unique in references:
                    duplicates+=1
                    if references[unique]!=row: conflicts.append(dict(arxiv_id=paper,ref_arxiv_id=row['ref_arxiv_id']))
                previous=references.get(unique)
                rank=lambda r:(sum(v is not None for v in r.values()),json.dumps(r,sort_keys=True))
                if previous is None or rank(row)>rank(previous): references[unique]=row
    pq.write_table(pa.Table.from_pylist(papers,schema=PAPER_SCHEMA),target/'papers.parquet')
    pq.write_table(pa.Table.from_pylist(list(references.values()),schema=REFERENCE_SCHEMA),target/'references.parquet')
    pq.write_table(pa.Table.from_pylist(list(version_references.values()),schema=VERSION_REFERENCE_SCHEMA),target/'version_references.parquet')
    pq.write_table(pa.Table.from_pylist(snapshot_rows,schema=SNAPSHOT_SCHEMA),target/'reference_snapshots.parquet')
    exported_ids={p['arxiv_id'] for p in papers}
    paper_metrics=[]; version_metrics=[]
    for key,row in store.rows.items():
        if key[0] not in selected: continue
        if key[2]=='discovery':
            refs=store.rows.get((key[0],0,'references',0),dict(status='not_scheduled'))
            count=None
            snapshots=snapshots_by_paper.get(key[0],[])
            if snapshots: count=len({r.get('ref_arxiv_id') or r.get('ref_s2_id') or json.dumps(r,sort_keys=True) for _,snapshot in snapshots for r in snapshot['references']})
            paper_metrics.append(dict(arxiv_id=key[0],discovery_status=row['status'],metadata_exported=key[0] in exported_ids,references_status=refs['status'],reference_count=count,discovery_attempts=row['total'],references_attempts=sum(r['total'] for k,r in store.rows.items() if k[0]==key[0] and k[2]=='references'),reference_count_scope='paper_union_including_legacy',references_status_scope='legacy_paper',reference_versions_total=sum(k[0]==key[0] and k[1]>0 and k[2:]==('references',0) for k in store.rows),reference_versions_succeeded=sum(k[0]==key[0] and k[1]>0 and k[2:]==('references',0) and r['status']=='succeeded' for k,r in store.rows.items())))
        elif key[2]=='source':
            inventory={}
            inventory=read_result(key,'inventory.json') or {}
            version_metrics.append(dict(arxiv_id=key[0],version=key[1],metadata_status=store.rows.get((key[0],key[1],'metadata',0),{}).get('status','not_scheduled'),references_status=store.rows.get((key[0],key[1],'references',0),{}).get('status','not_scheduled'),source_status=row['status'],source_attempts=row['total'],error_code=row['error'],compressed_bytes=inventory.get('compressed_bytes'),unpacked_bytes=inventory.get('unpacked_bytes'),kept_bytes=inventory.get('kept_bytes'),tex_count=inventory.get('tex_count'),bib_count=inventory.get('bib_count')))
    for name,rows,columns in [
        ('paper_metrics',paper_metrics,['arxiv_id','discovery_status','metadata_exported','references_status','reference_count','discovery_attempts','references_attempts','reference_count_scope','references_status_scope','reference_versions_total','reference_versions_succeeded']),
        ('version_metrics',version_metrics,['arxiv_id','version','metadata_status','references_status','source_status','source_attempts','error_code','compressed_bytes','unpacked_bytes','kept_bytes','tex_count','bib_count'])]:
        with (target/f'{name}.csv').open('w',newline='',encoding='utf8') as f:
            writer=csv.DictWriter(f,fieldnames=columns); writer.writeheader(); writer.writerows(rows)
    run_summaries=[]
    for directory in (store.root/'metrics').glob('*'):
        path=directory/'summary.json'
        if not path.exists(): path=directory/'checkpoint.json'
        if path.exists(): run_summaries.append(json.loads(path.read_text()))
    atomic_json(target/'run_metrics.json',run_summaries)
    atomic_json(target/'reference_conflicts.json',conflicts)
    def mean(rows,field):
        values=[r[field] for r in rows if r.get(field) is not None]
        return dict(mean=sum(values)/len(values) if values else None,sample_count=len(values),missing_count=len(rows)-len(values))
    completed_marker = store.root / 'completed.json'
    completed_data = None
    if completed_marker.exists():
        try: completed_data = json.loads(completed_marker.read_text(encoding='utf8'))
        except (OSError, ValueError): pass

    if completed_data and isinstance(completed_data.get('api_wait_seconds'), dict) and completed_data['api_wait_seconds'].get('arxiv'):
        api_wait_dict = completed_data['api_wait_seconds']
    else:
        latest_cumulative = None
        all_run_ids = {r.get('run_id') for r in run_summaries if r.get('run_id')}
        for r in run_summaries:
            contrib = set(r.get('contributing_run_ids') or [])
            if contrib and all_run_ids.issubset(contrib) and isinstance(r.get('api_wait_seconds'), dict) and r['api_wait_seconds'].get('arxiv'):
                latest_cumulative = r['api_wait_seconds']
                break
        if latest_cumulative:
            api_wait_dict = latest_cumulative
        else:
            total_rl = sum(r.get('api_wait_seconds', {}).get('total_rate_limit_seconds', 0.0) for r in run_summaries)
            ax_pacing = sum(r.get('api_wait_seconds', {}).get('arxiv', {}).get('pacing_seconds', 0.0) for r in run_summaries)
            ax_rl = sum(r.get('api_wait_seconds', {}).get('arxiv', {}).get('rate_limit_seconds', 0.0) for r in run_summaries)
            s2_pacing = sum(r.get('api_wait_seconds', {}).get('s2', {}).get('pacing_seconds', 0.0) for r in run_summaries)
            s2_rl = sum(r.get('api_wait_seconds', {}).get('s2', {}).get('rate_limit_seconds', 0.0) for r in run_summaries)
            total_wall = sum(r.get('wall_seconds', 0.0) for r in run_summaries)
            api_wait_dict = {
                'arxiv': {'pacing_seconds': ax_pacing, 'rate_limit_seconds': ax_rl},
                's2': {'pacing_seconds': s2_pacing, 'rate_limit_seconds': s2_rl},
                'total_rate_limit_seconds': total_rl,
                'effective_crawler_seconds': max(0.0, total_wall - total_rl),
            }
    contrib_runs = (completed_data.get('contributing_run_ids') if completed_data and completed_data.get('contributing_run_ids')
                    else [r['run_id'] for r in run_summaries if r.get('run_id')])
    manifest=dict(schema_version=3,reference_scope='references.parquet is the deduplicated paper union including legacy; version_references.parquet associates only version queries; version correspondence unverified',version_reference_links=len(version_references),version_snapshots=sum(r['version']>0 and r['reference_count'] is not None for r in snapshot_rows),version_snapshots_missing=sum(r['version']>0 and r['reference_count'] is None for r in snapshot_rows),version_snapshots_empty=sum(r['version']>0 and r['reference_count']==0 for r in snapshot_rows),paper_reference_count_scope='union_including_legacy',assigned_ids=len(selected),missing_expected_jobs=sum((paper,0,'discovery',0) not in store.rows for paper in selected),venue_conflicts=sorted(p for p,v in venue_candidates.items() if p in selected and len(v)>1),reference_date_source="semantic_scholar.publicationDate (not verified arXiv submission date)",integrity_scope="checksums of exported JSON only; full source audit is separate",run_id=run_id,papers=len(papers),references=len(references),missing_metadata=missing,missing_reference_snapshots=sum(r['reference_count'] is None for r in paper_metrics),valid_empty_reference_snapshots=sum(r['reference_count']==0 for r in paper_metrics),missing_revision_dates=revision_missing,duplicate_references=duplicates,conflicting_references=len(conflicts),config_fingerprint=hashlib.sha256(json.dumps(asdict(logical_config or config),sort_keys=True).encode()).hexdigest(),input_fingerprint=hashlib.sha256(json.dumps(sorted(selected)).encode()).hexdigest(),schema_fingerprint=hashlib.sha256((str(PAPER_SCHEMA)+str(REFERENCE_SCHEMA)+str(VERSION_REFERENCE_SCHEMA)+str(SNAPSHOT_SCHEMA)).encode()).hexdigest(),contributing_run_ids=contrib_runs,means=dict(reference_count=mean(paper_metrics,'reference_count'),compressed_bytes=mean(version_metrics,'compressed_bytes'),unpacked_bytes=mean(version_metrics,'unpacked_bytes'),kept_bytes=mean(version_metrics,'kept_bytes')),api_wait_seconds=api_wait_dict,checksums={p.name:digest(p) for p in target.iterdir()})
    manifest['missing_expected_jobs']=missing_expected_jobs
    manifest['missing_expected_jobs_scope']='selected discovery jobs only'
    atomic_json(target/'manifest.json',manifest)
    return manifest


def export_dataset_snapshot(config,run_id,*,fast_staging=True,scratch_root=None,takeover_stale=False):
    import copy, shutil, uuid
    from .locking import DatasetLock

    if not re.fullmatch(r'[A-Za-z0-9_-]+',run_id): raise ValueError('Invalid run ID')
    source_root = Path(config.paths['persistent_root'])
    if not source_root.exists(): raise FileNotFoundError(f'Source dataset root does not exist: {source_root}')

    logical_config = copy.deepcopy(config)

    with DatasetLock(source_root, 'export-src-' + run_id, takeover_stale=takeover_stale):
        final_export = source_root / 'exports' / run_id
        if final_export.exists():
            raise FileExistsError(f"Export run ID '{run_id}' already exists")

        base_scratch_str = scratch_root or config.paths.get('scratch_root', 'scratch')
        base_scratch = Path(base_scratch_str)
        staging_dir = base_scratch / f'staging-{run_id}-{uuid.uuid4().hex[:8]}'
        publish_staging = source_root / '.staging_exports' / run_id
        published = False
        owns_scratch = owns_publish = False
        try:
            staging_dir.mkdir(parents=True, exist_ok=False)
            owns_scratch = True
            if publish_staging.exists():
                raise FileExistsError(f"Export run ID '{run_id}' has an unfinished publication")
            runtime_root = source_root
            if fast_staging:
                runtime_root = staging_dir
            # Stage progress
            src_progress = source_root / 'progress'
            if fast_staging and src_progress.is_dir():
                dest_progress = staging_dir / 'progress'; dest_progress.mkdir(parents=True, exist_ok=True)
                for item in src_progress.iterdir():
                    if item.is_file() and item.name not in ('active_run.json', 'writer.lock'):
                        shutil.copyfile(item, dest_progress / item.name)

            # Stage metrics
            src_metrics = source_root / 'metrics'
            if fast_staging and src_metrics.is_dir():
                shutil.copytree(src_metrics, staging_dir / 'metrics', dirs_exist_ok=True)

            # Stage completed.json
            if fast_staging and (source_root / 'completed.json').is_file():
                shutil.copyfile(source_root / 'completed.json', staging_dir / 'completed.json')

            # Job-directed selective data staging: copy only needed JSONs for selected papers
            src_store = ProgressStore(source_root)
            selected = set(generate_ids(**config.range))
            keys_to_stage = set()
            for paper in selected:
                keys_to_stage.add((paper, 0, 'discovery', 0))
            for key in src_store.rows:
                if key[0] in selected:
                    keys_to_stage.add(key)

            # Discovery commits imply version metadata even when an old ledger omitted its job.
            for paper in selected:
                discovery = src_store.storage.result_dir((paper, 0, 'discovery', 0)) / 'normalized.json'
                try:
                    latest = int(json.loads(discovery.read_text()).get('version', 0))
                    keys_to_stage.update((paper, version, 'metadata', 0) for version in range(2, latest + 1))
                except (OSError, ValueError, TypeError):
                    pass

            for key in sorted(keys_to_stage if fast_staging else ()):
                src_res_dir = src_store.storage.result_dir(key)
                if src_res_dir.is_dir():
                    rel = src_res_dir.relative_to(source_root)
                    dest_res_dir = staging_dir / rel
                    for fname in ('complete.json', 'normalized.json', 'inventory.json'):
                        src_file = src_res_dir / fname
                        if src_file.is_file():
                            dest_res_dir.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(src_file, dest_res_dir / fname)

            output_root = staging_dir / 'built_exports'
            manifest = _export_snapshot(config, run_id, runtime_root=runtime_root,
                                        logical_config=logical_config, output_root=output_root)

            # Transactional publication to source
            staged_export = output_root / run_id
            publish_staging.parent.mkdir(parents=True, exist_ok=True)
            publish_staging.mkdir(exist_ok=False)
            owns_publish = True
            shutil.copytree(staged_export, publish_staging, dirs_exist_ok=True)

            # Verify checksummed files
            for filename, expected_hash in manifest['checksums'].items():
                dest_file = publish_staging / filename
                if not dest_file.is_file() or digest(dest_file) != expected_hash:
                    raise OSError(f'Destination checksum verification failed on publish: {filename}')

            # Verify manifest.json
            dest_manifest = publish_staging / 'manifest.json'
            if not dest_manifest.is_file() or digest(dest_manifest) != digest(staged_export / 'manifest.json'):
                raise OSError('Destination checksum verification failed on publish: manifest.json')
            try:
                json.loads(dest_manifest.read_text(encoding='utf8'))
            except (OSError, ValueError) as err:
                raise OSError(f'Destination manifest.json corrupted on publish: {err}')

            final_export.parent.mkdir(parents=True, exist_ok=True)
            publish_staging.replace(final_export)
            published = True
            return manifest

        finally:
            if owns_scratch: shutil.rmtree(staging_dir, ignore_errors=True)
            if owns_publish and not published and publish_staging.exists():
                shutil.rmtree(publish_staging, ignore_errors=True)
            try:
                parent_staging = source_root / '.staging_exports'
                if parent_staging.is_dir() and not any(parent_staging.iterdir()):
                    parent_staging.rmdir()
            except OSError:
                pass


def export_snapshot(config,run_id,takeover_stale=False):
    return export_dataset_snapshot(config,run_id,fast_staging=False,takeover_stale=takeover_stale)
