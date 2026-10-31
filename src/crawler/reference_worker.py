"""Version-addressed S2 fetching and ordered, bounded page publication."""
import asyncio
import hashlib
import json
import uuid
from urllib.parse import quote
from .console import log
from .metrics import utc_now
from .semantic_scholar import FIELDS, parse_page
from .storage import atomic_json, digest
from .workers import ThreadWorker


def query_identity(paper, version, page_size):
    identifier=f'URL:https://arxiv.org/abs/{paper}v{version}'
    query=dict(requested_identifier=identifier,fields=FIELDS,page_size=page_size,
               requested_version=version,version_correspondence='unverified')
    fingerprint=hashlib.sha256(json.dumps(query,sort_keys=True).encode()).hexdigest()
    return query,fingerprint


class ReferenceWorker:
    def __init__(self,config,store,progress,io,reader,telemetry,metrics,scratch,selected):
        self.config=config; self.store=store; self.progress=progress
        self.io=io; self.reader=reader; self.telemetry=telemetry; self.metrics=metrics
        self.scratch=scratch; self.selected=selected
        self.queue=ThreadWorker(config.execution['s2_publish_queue_capacity'],
            config.execution['s2_publish_queue_bytes'],name='s2-publication',async_handler=True)

    def cached_page(self,base,fingerprint,offset):
        root=self.store.reference_cache_root(*base[:2])
        if root.exists() and any(p.is_dir() and p.name!=fingerprint for p in root.iterdir()):
            raise ValueError('query_fingerprint_changed_refresh_required')
        path=root/fingerprint/f'page-{offset:06d}.json'
        marker=path.with_suffix('.complete.json')
        try:
            receipt=json.loads(marker.read_text())
            if receipt.get('fingerprint')!=fingerprint or receipt.get('sha256')!=digest(path): return None
            raw=path.read_bytes()
            return parse_page(raw,offset),len(raw)
        except (OSError,ValueError,TypeError,AttributeError): return None

    def publish_page(self,base,offset,local,query,fingerprint,final,fetched_at):
        """Filesystem-only work: no mutations of ProgressStore in this thread."""
        directory=self.store.reference_cache_root(*base[:2])/fingerprint
        directory.mkdir(parents=True,exist_ok=True)
        if local is not None:
            raw=local.read_bytes()
            path=directory/f'page-{offset:06d}.json'
            temporary=path.with_suffix('.tmp')
            with temporary.open('wb') as file:
                import os
                file.write(raw); file.flush(); os.fsync(file.fileno())
            checksum=hashlib.sha256(raw).hexdigest()
            if digest(temporary)!=checksum: raise OSError('Reference page checksum mismatch')
            temporary.replace(path)
            atomic_json(path.with_suffix('.complete.json'),dict(sha256=checksum,fingerprint=fingerprint,fetched_at=fetched_at))
            local.unlink()
        if not final: return None
        refs=[]; unresolved=0; cursor=0; byte_count=0; provenance=[]
        while True:
            path=directory/f'page-{cursor:06d}.json'
            raw=path.read_bytes(); byte_count+=len(raw)
            if byte_count>self.config.execution['s2_snapshot_max_bytes']: raise ValueError('Snapshot byte budget exceeded')
            marker=json.loads(path.with_suffix('.complete.json').read_text())
            if marker.get('sha256')!=hashlib.sha256(raw).hexdigest() or marker.get('fingerprint')!=fingerprint:
                raise OSError('Reference cache changed during publication')
            provenance.append(dict(offset=cursor,sha256=marker['sha256'],fetched_at=marker.get('fetched_at')))
            page=parse_page(raw,cursor)
            refs.extend(page['references']); unresolved+=page['unresolved_count']
            if page['next'] is None: break
            cursor=page['next']
        atomic_json(directory/'normalized.json',dict(references=refs,unresolved_count=unresolved,
            fetched_at=utc_now(),query_fingerprint=fingerprint,snapshot_id=uuid.uuid4().hex,
            content_sha256=hashlib.sha256(json.dumps(refs,sort_keys=True).encode()).hexdigest(),
            page_provenance=provenance,**query))
        self.store.storage.commit(base,directory)
        return len(refs)

    async def publish(self,base,offset,local,query,fingerprint,final,fetched_at):
        count=await self.io.call(self.publish_page,base,offset,local,query,fingerprint,final,fetched_at)
        def acknowledge():
            if offset: self.store.finish((*base[:3],offset),'succeeded')
            if final:
                self.store.reference_counts[base[:2]]=count
                self.store.finish(base,'succeeded')
        await self.progress.call(acknowledge)
        directory=self.store.reference_cache_root(*base[:2])/fingerprint
        await self.telemetry.call(self.metrics.touch,directory/f'page-{offset:06d}.json',directory/f'page-{offset:06d}.complete.json')
        if final:
            await self.telemetry.call(self.metrics.record,'reference_snapshot',job=base,service='s2',references=count)
            await self.telemetry.call(self.metrics.touch,self.store.storage.result_dir(base))
            log(f'S2: {base[0]}v{base[1]} complete, {count} references')

    async def fetch_snapshot(self,base,scheduler):
        paper,version=base[:2]
        query,fingerprint=query_identity(paper,version,self.config.network['s2_page_size'])
        offset=0; byte_count=0
        while True:
            key=(*base[:3],offset)
            await self.progress.call(self.store.ensure,key)
            try:
                cached=await self.reader.call(self.cached_page,base,fingerprint,offset)
            except ValueError as exc:
                await self.progress.call(self.store.finish,base,'failed',str(exc)); return
            local=None
            if cached is None:
                if not await self.progress.call(self.store.recover_reference_page,key):
                    await self.progress.call(self.store.finish,base,'failed','integrity_missing_result'); return
                identifier=quote(query['requested_identifier'],safe='')
                result=await scheduler.request(key,'s2',f'https://api.semanticscholar.org/graph/v1/paper/{identifier}/references',
                    params={'fields':FIELDS,'limit':query['page_size'],'offset':offset},
                    validate=lambda raw:(raw,parse_page(raw,offset)))
                if result is None:
                    row=await self.progress.call(lambda:dict(self.store.get(key)))
                    await self.progress.call(self.store.finish,base,
                        'not_found' if row['status']=='not_found' else 'failed',row['error'] or 'missing_page_result')
                    return
                raw,page=result; size=len(raw)
                local=self.scratch/f'{quote(paper,safe="")}-v{version}-page-{offset}.json'
                await self.reader.call(local.write_bytes,raw)
            else:
                page,size=cached
            byte_count+=size
            if byte_count>self.config.execution['s2_snapshot_max_bytes']:
                if local: await self.reader.call(local.unlink,missing_ok=True)
                await self.progress.call(self.store.finish,base,'failed','resource_limit'); return
            # Includes active publication and its durable progress acknowledgement.
            await self.queue.submit(self.publish,base,offset,local,query,fingerprint,
                                    page['next'] is None,utc_now(),weight=size)
            if page['next'] is None: return
            offset=page['next']

    async def run(self,scheduler,discovery_done):
        async with self.queue:
            async with asyncio.TaskGroup() as group:
                producer=group.create_task(self.fetch_loop(scheduler,discovery_done))
                watcher=group.create_task(self.queue.wait_failure())
                try:
                    await producer
                finally:
                    watcher.cancel()
        log(f'S2: publication queue drained; peak {self.queue.peak_items} pages / {self.queue.peak_bytes} bytes')
        await self.telemetry.call(self.metrics.record,'publication_queue',service='s2',
            peak_items=self.queue.peak_items,peak_bytes=self.queue.peak_bytes)

    async def fetch_loop(self,scheduler,discovery_done):
        visited=set()
        while 's2' not in scheduler.stopped:
            keys=await self.progress.call(lambda:sorted(k for k,r in self.store.rows.items()
                if k[0] in self.selected and k[1]>0 and k[2:] == ('references',0)
                and r['status'] in ('pending','running') and k not in visited))
            if not keys:
                if discovery_done.is_set():
                    if not visited: log('S2: no eligible version jobs; discovery must enumerate versions first')
                    break
                await asyncio.sleep(.1)
                continue
            for key in keys:
                if 's2' in scheduler.stopped: break
                visited.add(key)
                await self.fetch_snapshot(key,scheduler)
