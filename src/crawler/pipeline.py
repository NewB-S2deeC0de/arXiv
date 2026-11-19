"""Two independently paced service branches, sharing durable progress."""
import asyncio
from .locking import DatasetLock
from .console import log
from .metrics import MetricsRecorder
from urllib.parse import quote
import shutil
import time
import uuid
from pathlib import Path
import httpx
from .ids import generate_ids
from .progress import ProgressStore
from .storage import atomic_json
from .scheduler import Scheduler
from .secrets import load_semantic_scholar_key
from .arxiv_client import parse_atom_batch, parse_version_batch
from .sources import inspect_and_extract
from .workers import ThreadWorker
from .reference_worker import ReferenceWorker
from contextlib import AsyncExitStack

async def _run_pipeline_inner(config,stages=None,resume=True,*,limit=None,takeover_stale=False,transport=None,run_id=None,now=time.time,sleep=asyncio.sleep,progress=None,telemetry=None,arxiv_io=None,s2_io=None,reader=None):
    log('Startup: validating configuration and selecting IDs')
    config.validate(); stages=stages or {'arxiv','s2'}
    ids=generate_ids(**config.range)
    if limit is not None:
        if limit<1: raise ValueError('limit must be positive')
        selected=set(ids[:limit])
    else: selected=set(ids)
    log('Startup: checking persistent directory')
    root=Path(config.paths['persistent_root']); root.mkdir(parents=True,exist_ok=True)
    log('Startup: querying free disk space on Drive')
    if shutil.disk_usage(root).free<config.execution['min_free_disk_bytes']: raise OSError('Insufficient disk space')
    log('Startup: disk space check complete')
    log('Startup: loading S2 credentials if selected')
    api_key=load_semantic_scholar_key() if 's2' in stages else None
    if 's2' in stages and not api_key and not config.execution['allow_anonymous_s2']:
        raise RuntimeError('Set API key or explicitly allow anonymous S2')
    print('[Startup] Loading progress snapshot and journal',flush=True)
    store=await progress.call(ProgressStore,root); store.run_id=run_id; store.storage.run_id=run_id
    log('Startup: progress loaded; initializing/checkpointing jobs')
    await progress.call(store.initialize,ids,versioned=True)
    print('[Startup] Recovering interrupted jobs in selected scope',flush=True)
    recovery_stages=({'discovery','metadata','source'} if 'arxiv' in stages else ({'discovery'} if 'discovery' in stages else set())) | ({'references'} if 's2' in stages else set())
    await progress.call(store.reconcile,ids=selected,stages=recovery_stages,recovery_only=True,skip_legacy_references=True)
    await progress.call(store.enable_version_references)
    await progress.call(store.save)
    print('[Startup] Recovery finished; initializing metrics',flush=True)
    metrics=MetricsRecorder(config,'key' if api_key else 'anonymous'); await telemetry.call(metrics.start,run_id)
    log('Startup: metrics ready')
    await telemetry.call(metrics.record,'recovery',counts=await progress.call(store.status_counts))
    async def monitor_metrics():
        while True:
            await asyncio.sleep(config.execution['ram_sample_seconds'])
            await telemetry.call(metrics.sample)
    monitor=asyncio.create_task(monitor_metrics())
    scratch=Path(config.paths['scratch_root'])/run_id
    log('Startup: preparing scratch directory')
    scratch.mkdir(parents=True,exist_ok=True)
    async def publish(key, staging, status='succeeded'):
        await arxiv_io.call(store.storage.commit,key,staging,status)

    async def rows_for(stage):
        return await progress.call(lambda:[(k,dict(store.get(k))) for k in store.pending(stage) if k[0] in selected])

    metadata_done_count = 0
    source_done_count = 0

    async def metadata_batch(keys, scheduler, source_queue, metadata_inflight):
        try:
            nonlocal metadata_done_count
            identifiers = [f'{key[0]}v{key[1]}' for key in keys]
            artifact = await scheduler.request(
                keys,
                'arxiv',
                'https://export.arxiv.org/api/query',
                params={'id_list': ','.join(identifiers), 'max_results': len(keys)},
                validate=lambda raw: (raw, parse_version_batch(raw, identifiers)),
            )
            if artifact is None:
                metadata_inflight.difference_update(keys)
                return
            raw, parsed_versions = artifact
            request_data = dict(id_list=identifiers, max_results=len(keys))

            async def publish_metadata():
                try:
                    def write_and_commit_metadata():
                        records = []
                        for key, identifier in zip(keys, identifiers):
                            parsed = parsed_versions[identifier]
                            staging = scratch / f'{quote(key[0], safe="")}-v{key[1]}-metadata'
                            staging.mkdir(parents=True, exist_ok=True)
                            (staging / 'response.xml').write_bytes(raw)
                            atomic_json(staging / 'request.json', request_data)
                            atomic_json(staging / 'normalized.json', parsed)
                            status = 'not_found' if parsed.get('not_found') else 'succeeded'
                            before = time.monotonic()
                            store.storage.commit(key, staging, status)
                            duration = time.monotonic() - before
                            shutil.rmtree(staging)
                            records.append((key, status, duration, store.storage.result_dir(key), staging))
                        return records

                    records = await arxiv_io.call(write_and_commit_metadata)
                    results = []
                    for key, status, duration, result_dir, staging in records:
                        await telemetry.call(metrics.touch, result_dir, staging)
                        await telemetry.call(metrics.record, 'commit', job=key, seconds=duration)
                        results.append((key, status, ''))
                    await progress.call(store.finish_many, results)
                    await telemetry.call(metrics.touch, store.journal.path)
                    nonlocal metadata_done_count
                    metadata_done_count += len(keys)
                    log(f'arXiv Metadata: batch finished ({len(keys)} versions, cumulative: {metadata_done_count})')
                finally:
                    metadata_inflight.difference_update(keys)

            await source_queue.submit(publish_metadata)
        except Exception:
            metadata_inflight.difference_update(keys)
            raise

    async def arxiv(scheduler, source_queue):
        discovery_turn = True
        only_discovery = 'discovery' in stages and 'arxiv' not in stages
        source_inflight = set()
        discovery_inflight = set()
        metadata_inflight = set()
        while 'arxiv' not in scheduler.stopped:
            discoveries = [k for k, r in await rows_for('discovery') if k not in discovery_inflight]
            if not discoveries and not discovery_inflight and not discovery_done.is_set():
                discovery_done.set()
            if only_discovery:
                if not discoveries:
                    if source_queue.used_items or discovery_inflight:
                        await asyncio.sleep(0.01)
                        continue
                    break
                attempts = (await progress.call(lambda: dict(store.get(discoveries[0]))))['attempts']
                size = min(config.network['discovery_batch_size'], config.execution['queue_capacity'])
                batch = [k for k, r in await rows_for('discovery') if k not in discovery_inflight and r['attempts'] == attempts][:size]
                discovery_inflight.update(batch)
                await run_discovery_batch(batch, scheduler, source_queue, discovery_inflight, async_publish=True)
                continue
            version_rows = [(k, r) for k, r in (await rows_for('metadata')) + (await rows_for('source')) if k not in source_inflight and k not in metadata_inflight]
            versions = sorted([k for k, r in version_rows], key=lambda k: (k[0], k[1], k[2] == 'source'))
            if not discoveries and not versions:
                if source_queue.used_items or discovery_inflight or metadata_inflight or source_inflight:
                    await asyncio.sleep(0.01)
                    continue
                break
            if discoveries and (discovery_turn or not versions):
                # Regroup after restart by remaining budget. Never reset attempts
                # to make a new batch: all members must be eligible for every send.
                attempts = (await progress.call(lambda: dict(store.get(discoveries[0]))))['attempts']
                size = min(config.network['discovery_batch_size'], config.execution['queue_capacity'])
                batch = [k for k, r in await rows_for('discovery') if k not in discovery_inflight and r['attempts'] == attempts][:size]
                discovery_inflight.update(batch)
                await run_discovery_batch(batch, scheduler, source_queue, discovery_inflight, async_publish=False)
                discovery_turn = False
                continue
            key = versions[0]
            discovery_turn = True
            if key[2] == 'metadata':
                attempts = (await progress.call(lambda: dict(store.get(key))))['attempts']
                size = min(config.network['metadata_batch_size'], config.execution['queue_capacity'])
                batch = [k for k, r in version_rows if k[2] == 'metadata' and r['attempts'] == attempts][:size]
                metadata_inflight.update(batch)
                await metadata_batch(batch, scheduler, source_queue, metadata_inflight)
                continue
            source_inflight.add(key)
            archive = scratch / f'{quote(key[0], safe="")}v{key[1]}.download'
            staging = scratch / f'{quote(key[0], safe="")}v{key[1]}-source'
            try:
                result = await scheduler.request(key, 'arxiv', f'https://export.arxiv.org/src/{key[0]}v{key[1]}', target=archive, validate=lambda p: inspect_and_extract(p, staging, config))
            except RuntimeError as exc:
                source_inflight.discard(key)
                if str(exc) != 'resource_limit': raise
                await progress.call(store.finish, key, 'failed', 'resource_limit'); continue
            if not result:
                source_inflight.discard(key)
                continue
            def commit_source_files(job_key, archive_path, staging_dir, status):
                response_info = archive_path.with_suffix('.response.json')
                if response_info.exists():
                    shutil.copyfile(response_info, staging_dir / 'response.json')
                before = time.monotonic()
                store.storage.commit(job_key, staging_dir, status)
                duration = time.monotonic() - before
                archive_path.unlink(missing_ok=True)
                shutil.rmtree(staging_dir, ignore_errors=True)
                return duration

            async def publish_source(job_key, archive_path, staging_dir, status):
                try:
                    duration = await arxiv_io.call(commit_source_files, job_key, archive_path, staging_dir, status)
                    await progress.call(store.finish, job_key, status)
                    await telemetry.call(metrics.record, 'commit', job=job_key, seconds=duration)
                    await telemetry.call(metrics.touch, store.storage.result_dir(job_key), archive_path, staging_dir)
                    await telemetry.call(metrics.sample, force_disk=True)
                    nonlocal source_done_count
                    source_done_count += 1
                    if source_done_count % 25 == 0 or source_done_count == 1:
                        log(f'arXiv Source: {source_done_count} sources published (latest: {job_key[0]}v{job_key[1]})')
                finally:
                    source_inflight.discard(job_key)
            await source_queue.submit(publish_source, key, archive, staging, result['status'])

    async def run_discovery_batch(batch_keys, scheduler, source_queue=None, discovery_inflight=None, async_publish=False):
        papers = [k[0] for k in batch_keys]
        id_list = ",".join(papers)
        # Quan trọng: arXiv mặc định max_results=10, nên phải truyền max_results=len(papers)
        try:
            raw = await scheduler.request(
                batch_keys,
                "arxiv",
                "https://export.arxiv.org/api/query",
                params={"id_list": id_list, "max_results": len(papers)},
                validate=lambda raw: (raw, parse_atom_batch(raw, papers)),
            )
            if raw is None:
                if discovery_inflight: discovery_inflight.difference_update(batch_keys)
                return
            raw, batch_parsed = raw
            request_data = dict(id_list=papers, max_results=len(papers))

            async def publish_discovery():
                try:
                    def write_and_commit():
                        records = []
                        for key in batch_keys:
                            paper = key[0]
                            parsed = batch_parsed[paper]
                            staging = scratch / f'{quote(paper, safe="")}-discovery'
                            staging.mkdir(parents=True, exist_ok=True)
                            (staging / 'response.xml').write_bytes(raw)
                            atomic_json(staging / 'request.json', request_data)
                            atomic_json(staging / 'normalized.json', parsed)
                            status = 'not_found' if parsed.get('not_found') else 'succeeded'
                            before = time.monotonic()
                            store.storage.commit(key, staging, status)
                            duration = time.monotonic() - before
                            shutil.rmtree(staging)
                            records.append((key, status, duration, store.storage.result_dir(key), staging))
                        return records

                    records = await arxiv_io.call(write_and_commit)
                    results = []
                    for key, status, duration, result_dir, staging in records:
                        await telemetry.call(metrics.touch, result_dir, staging)
                        await telemetry.call(metrics.record, 'commit', job=key, seconds=duration)
                        results.append((key, status, ''))

                    def add_all_versions():
                        for key in batch_keys:
                            paper = key[0]
                            parsed = batch_parsed[paper]
                            for version in range(1, parsed.get('version', 0) + 1):
                                for stage in ('metadata', 'source', 'references'):
                                    store.add((paper, version, stage, 0))

                    await progress.call(add_all_versions)
                    await progress.call(store.finish_many, results)
                    await telemetry.call(metrics.touch, store.journal.path)
                    await telemetry.call(metrics.sample, force_disk=True)
                    total_succeeded = sum(1 for r in results if r[1] == 'succeeded')
                    log(f'arXiv Discovery: batch finished ({len(batch_keys)} papers, {total_succeeded} succeeded)')
                finally:
                    if discovery_inflight: discovery_inflight.difference_update(batch_keys)

            if async_publish and source_queue:
                await source_queue.submit(publish_discovery)
            else:
                await publish_discovery()
        except Exception:
            if discovery_inflight: discovery_inflight.difference_update(batch_keys)
            raise
    reference_worker=ReferenceWorker(config,store,progress,s2_io,reader,telemetry,metrics,scratch,selected)
    discovery_done=asyncio.Event()
    has_arxiv='arxiv' in stages or 'discovery' in stages
    has_s2='s2' in stages
    if not has_arxiv or not await rows_for('discovery'): discovery_done.set()
    source_queue=ThreadWorker(config.execution['s2_publish_queue_capacity'],name='arxiv-source-publication',async_handler=True)
    async def arxiv_run(scheduler):
        async with source_queue:
            async with asyncio.TaskGroup() as group:
                arxiv_task=group.create_task(arxiv(scheduler,source_queue))
                watcher=group.create_task(source_queue.wait_failure())
                try: await arxiv_task
                finally: watcher.cancel(); discovery_done.set()
    completed=False
    try:
        timeout=httpx.Timeout(config.network['read_timeout_seconds'],connect=config.network['connect_timeout_seconds'])
        async with httpx.AsyncClient(timeout=timeout,transport=transport,follow_redirects=False) as client:
            log('Run: loading service cooldowns and starting selected workers')
            scheduler=await progress.call(Scheduler,config,store,client,api_key=api_key,metrics=metrics,now=now,sleep=sleep,progress_worker=progress,metrics_worker=telemetry)
            async with asyncio.TaskGroup() as group:
                if has_arxiv: group.create_task(arxiv_run(scheduler))
                if has_s2: group.create_task(reference_worker.run(scheduler,discovery_done))
        completed=True
    finally:
        monitor.cancel()
        try: await monitor
        except asyncio.CancelledError: 
            pass
        log('Finalize: saving progress and CSV views')
        if not progress.failed:
            await progress.call(store.save,with_views=True)
        else:
            log('Finalize: progress writer failed; preserving last durable checkpoint')
        log('Finalize: writing metrics summary')
        summary=await telemetry.call(metrics.stop,None if progress.failed else store,incomplete=not completed)
        summary['jobs']=len(store.rows)
    log('Finalize: pipeline complete')
    return summary


async def _run_pipeline(config,*args,**kwargs):
    async with AsyncExitStack() as stack:
        workers={}
        for name in ('progress','telemetry','arxiv_io','s2_io','reader'):
            capacity=config.execution['progress_queue_capacity'] if name=='progress' else 8
            workers[name]=await stack.enter_async_context(ThreadWorker(capacity=capacity,name=name))
        return await _run_pipeline_inner(config,*args,**kwargs,**workers)


async def run_pipeline(config,stages=None,resume=True,*,limit=None,takeover_stale=False,transport=None,now=time.time,sleep=asyncio.sleep):
    log('Startup: pipeline entered; acquiring dataset lock')
    run_id=uuid.uuid4().hex
    with DatasetLock(config.paths['persistent_root'],run_id,takeover_stale):
        return await _run_pipeline(config,stages,resume,limit=limit,takeover_stale=takeover_stale,transport=transport,run_id=run_id,now=now,sleep=sleep)
