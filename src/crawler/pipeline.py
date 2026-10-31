"""Two independently paced service branches, sharing durable progress."""
import asyncio
from .locking import DatasetLock
from .console import log
from .metrics import MetricsRecorder, utc_now
from urllib.parse import quote
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
import httpx
from .ids import generate_ids
from .progress import ProgressStore
from .storage import atomic_json, digest
from .scheduler import Scheduler
from .secrets import load_semantic_scholar_key
from .arxiv_client import parse_atom_batch, parse_version_batch
from .semantic_scholar import parse_page,FIELDS
from .sources import inspect_and_extract

async def _run_pipeline(config,stages=None,resume=True,*,limit=None,takeover_stale=False,transport=None,run_id=None,now=time.time,sleep=asyncio.sleep):
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
    started=time.monotonic()
    log('Startup: loading S2 credentials if selected')
    api_key=load_semantic_scholar_key() if 's2' in stages else None
    if 's2' in stages and not api_key and not config.execution['allow_anonymous_s2']:
        raise RuntimeError('Set API key or explicitly allow anonymous S2')
    print('[Startup] Loading progress snapshot and journal',flush=True)
    store=ProgressStore(root); store.run_id=run_id; store.storage.run_id=run_id
    log('Startup: progress loaded; initializing/checkpointing jobs')
    store.initialize(ids)
    print('[Startup] Recovering interrupted jobs in selected scope',flush=True)
    recovery_stages=({'discovery','metadata','source'} if 'arxiv' in stages else set()) | ({'references'} if 's2' in stages else set())
    store.reconcile(ids=selected,stages=recovery_stages,recovery_only=True)
    print('[Startup] Recovery finished; initializing metrics',flush=True)
    metrics=MetricsRecorder(config,'key' if api_key else 'anonymous'); metrics.start(run_id)
    log('Startup: metrics ready')
    metrics.record('recovery',counts=store.status_counts())
    monitor=asyncio.create_task(metrics.monitor())
    scratch=Path(config.paths['scratch_root'])/run_id
    log('Startup: preparing scratch directory')
    scratch.mkdir(parents=True,exist_ok=True)
    async def publish(key, staging, status='succeeded'):
        # Slow Drive copies/checksums must not block the other service or RAM sampler.
        # Join the writer on cancellation before releasing the dataset lock.
        task=asyncio.create_task(asyncio.to_thread(store.storage.commit,key,staging,status))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def metadata_batch(keys,scheduler):
        identifiers=[f'{key[0]}v{key[1]}' for key in keys]
        artifact=await scheduler.request(keys,'arxiv','https://export.arxiv.org/api/query',params={'id_list':','.join(identifiers),'max_results':len(keys)},validate=lambda raw:(raw,parse_version_batch(raw,identifiers)))
        if artifact is None: return
        raw,parsed_versions=artifact; results=[]
        for key,identifier in zip(keys,identifiers):
            parsed=parsed_versions[identifier]
            staging=scratch/f'{quote(key[0],safe="")}-v{key[1]}-metadata'
            staging.mkdir(parents=True,exist_ok=True)
            (staging/'response.xml').write_bytes(raw)
            atomic_json(staging/'request.json',dict(id_list=identifiers,max_results=len(keys)))
            atomic_json(staging/'normalized.json',parsed)
            before=time.monotonic()
            status='not_found' if parsed.get('not_found') else 'succeeded'
            await publish(key,staging,status)
            metrics.touch(store.storage.paper_dir(key[0]),staging)
            metrics.record('commit',job=key,seconds=time.monotonic()-before)
            shutil.rmtree(staging); metrics.touch(staging)
            results.append((key,status,''))
        store.finish_many(results)
        metrics.touch(root/'progress')

    async def arxiv(scheduler):
        discovery_turn = True
        while 'arxiv' not in scheduler.stopped:
            discoveries=[k for k in store.pending('discovery') if k[0] in selected]
            versions=sorted([k for stage in ('metadata','source') for k in store.pending(stage) if k[0] in selected],key=lambda k:(k[0],k[1],k[2]=='source'))
            if not discoveries and not versions: break
            if discoveries and (discovery_turn or not versions):
                # Regroup after restart by remaining budget. Never reset attempts
                # to make a new batch: all members must be eligible for every send.
                attempts=store.get(discoveries[0])['attempts']
                size=min(config.network['discovery_batch_size'],config.execution['queue_capacity'])
                batch=[k for k in discoveries if store.get(k)['attempts']==attempts][:size]
                await run_discovery_batch(batch,scheduler)
                discovery_turn=False
                continue
            key=versions[0]
            discovery_turn=True
            if key[2]=='metadata':
                attempts=store.get(key)['attempts']
                size=min(config.network['metadata_batch_size'],config.execution['queue_capacity'])
                batch=[k for k in versions if k[2]=='metadata' and store.get(k)['attempts']==attempts][:size]
                await metadata_batch(batch,scheduler)
                continue
            archive=scratch/f'{quote(key[0],safe="")}v{key[1]}.download'
            staging=scratch/f'{quote(key[0],safe="")}v{key[1]}-source'
            try:
                result=await scheduler.request(key,'arxiv',f'https://export.arxiv.org/src/{key[0]}v{key[1]}',target=archive,validate=lambda p:inspect_and_extract(p,staging,config))
            except RuntimeError as exc:
                if str(exc)!='resource_limit': raise
                store.finish(key,'failed','resource_limit'); continue
            if result:
                response_info=archive.with_suffix('.response.json')
                if response_info.exists(): shutil.copyfile(response_info,staging/'response.json')
                before=time.monotonic(); await publish(key,staging,result['status']); store.finish(key,result['status']); archive.unlink(missing_ok=True)
                metrics.record('commit',job=key,seconds=time.monotonic()-before)
                metrics.touch(store.storage.paper_dir(key[0]),archive,staging,root/'progress')
                shutil.rmtree(staging); metrics.touch(staging)
                metrics.sample(force_disk=True)
    async def references(scheduler):
        for paper in ids:
            if paper not in selected or 's2' in scheduler.stopped: continue
            base=(paper,0,'references',0)
            if store.get(base)['status'] in ('succeeded','failed','not_found'): continue
            offset=0; refs=[]; unresolved=0
            fingerprint=hashlib.sha256(f'{FIELDS}:{config.network["s2_page_size"]}'.encode()).hexdigest()
            cache_root=store.storage.paper_dir(paper)/'reference-staging'
            if cache_root.exists() and any(d.is_dir() and d.name!=fingerprint for d in cache_root.iterdir()):
                store.finish(base,'failed','query_fingerprint_changed_refresh_required'); continue
            staging=cache_root/fingerprint
            staging.mkdir(parents=True,exist_ok=True)
            while True:
                key=(paper,0,'references',offset)
                store.add(key); store.save()
                page_path=staging/f'page-{offset:06d}.json'
                marker_path=staging/f'page-{offset:06d}.complete.json'
                page=None
                if marker_path.exists() and page_path.exists():
                    try:
                        marker=json.loads(marker_path.read_text())
                        raw=page_path.read_bytes()
                        if marker.get('sha256')==hashlib.sha256(raw).hexdigest() and marker.get('fingerprint')==fingerprint:
                            page=parse_page(raw,offset)
                    except (OSError,ValueError,TypeError,AttributeError): page=None
                if page is None:
                    if not store.recover_reference_page(key):
                        store.finish(base,'failed','integrity_missing_result')
                        break
                    raw=await scheduler.request(key,'s2',f'https://api.semanticscholar.org/graph/v1/paper/ARXIV:{paper}/references',params={'fields':FIELDS,'limit':config.network['s2_page_size'],'offset':offset},validate=lambda raw:(raw,parse_page(raw,offset)))
                    if raw is None:
                        store.finish(base,'not_found' if store.get(key)['status']=='not_found' else 'failed',store.get(key)['error'] or 'missing_page_result'); break
                    raw,page=raw
                    page_path.write_bytes(raw)
                    atomic_json(marker_path,dict(sha256=digest(page_path),fingerprint=fingerprint))
                    metrics.touch(page_path,marker_path)
                if offset:
                    store.finish(key,'succeeded')
                refs.extend(page['references']); unresolved+=page['unresolved_count']
                if page['next'] is None:
                    atomic_json(staging/'normalized.json',dict(references=refs,unresolved_count=unresolved,fetched_at=utc_now(),query_fingerprint=fingerprint))
                    before=time.monotonic(); await publish(base,staging); store.reference_counts[paper]=len(refs); store.finish(base,'succeeded')
                    metrics.touch(store.storage.result_dir(base),root/'progress')
                    metrics.record('commit',job=base,seconds=time.monotonic()-before)
                    metrics.record('reference_snapshot',job=base,service='s2',references=len(refs))
                    log(f'S2: {paper} complete, {len(refs)} references')
                    break
                offset=page['next']
                
    async def run_discovery_batch(batch_keys, scheduler):
        papers = [k[0] for k in batch_keys]
        id_list = ",".join(papers)
        # Quan trọng: arXiv mặc định max_results=10, nên phải truyền max_results=len(papers)
        raw = await scheduler.request(
            batch_keys,
            "arxiv",
            "https://export.arxiv.org/api/query",
            params={"id_list": id_list, "max_results": len(papers)},
            validate=lambda raw: (raw, parse_atom_batch(raw, papers)),
        )
        if raw is None:
            return
        raw, batch_parsed = raw
        results=[]
        for key in batch_keys:
            paper=key[0]; parsed=batch_parsed[paper]
            staging=scratch/f'{quote(paper,safe="")}-discovery'
            staging.mkdir(parents=True,exist_ok=True)
            (staging/'response.xml').write_bytes(raw)
            atomic_json(staging/'request.json',dict(id_list=papers,max_results=len(papers)))
            atomic_json(staging/'normalized.json',parsed)
            before=time.monotonic()
            status='not_found' if parsed.get('not_found') else 'succeeded'
            await publish(key,staging,status)
            # Marker first, then version rows, then terminal CSV state. Recovery
            # reconstructs missing rows from any valid discovery marker.
            for version in range(1,parsed.get('version',0)+1):
                for stage in ('metadata','source'): store.add((paper,version,stage,0))
            metrics.touch(store.storage.paper_dir(key[0]),staging)
            metrics.record('commit',job=key,seconds=time.monotonic()-before)
            shutil.rmtree(staging); metrics.touch(staging)
            results.append((key,status,''))
        store.finish_many(results)
        metrics.touch(root/'progress')
    completed=False
    try:
        timeout=httpx.Timeout(config.network['read_timeout_seconds'],connect=config.network['connect_timeout_seconds'])
        async with httpx.AsyncClient(timeout=timeout,transport=transport,follow_redirects=False) as client:
            log('Run: loading service cooldowns and starting selected workers')
            scheduler=Scheduler(config,store,client,api_key=api_key,metrics=metrics,now=now,sleep=sleep)
            async with asyncio.TaskGroup() as group:
                if 'arxiv' in stages: group.create_task(arxiv(scheduler))
                if 's2' in stages: group.create_task(references(scheduler))
        completed=True
    finally:
        monitor.cancel()
        try: await monitor
        except asyncio.CancelledError: 
            pass
        log('Finalize: saving progress and CSV views')
        store.save(with_views=True)
        log('Finalize: writing metrics summary')
        summary=metrics.stop(store,incomplete=not completed)
        summary['jobs']=len(store.rows)
    log('Finalize: pipeline complete')
    return summary


async def run_pipeline(config,stages=None,resume=True,*,limit=None,takeover_stale=False,transport=None,now=time.time,sleep=asyncio.sleep):
    log('Startup: pipeline entered; acquiring dataset lock')
    run_id=uuid.uuid4().hex
    with DatasetLock(config.paths['persistent_root'],run_id,takeover_stale):
        return await _run_pipeline(config,stages,resume,limit=limit,takeover_stale=takeover_stale,transport=transport,run_id=run_id,now=now,sleep=sleep)
