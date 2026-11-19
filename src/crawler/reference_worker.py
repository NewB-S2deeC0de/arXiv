"""Version-addressed S2 fetching and ordered, bounded page publication."""
import asyncio
import hashlib
import json
import os
import shutil
import uuid
import time
from urllib.parse import quote
from .console import log
from .metrics import utc_now
from .semantic_scholar import FIELDS, parse_batch, parse_page
from .storage import atomic_json, digest
from .workers import ThreadWorker


def query_identity(paper, version, page_size):
    identifier=f'URL:https://arxiv.org/abs/{paper}v{version}'
    query=dict(requested_identifier=identifier,fields=FIELDS,page_size=page_size,
               requested_version=version,version_correspondence='unverified')
    fingerprint=hashlib.sha256(json.dumps(query,sort_keys=True).encode()).hexdigest()
    return query,fingerprint

BATCH_FIELDS='paperId,referenceCount,'+','.join(f'references.{field}' for field in FIELDS.split(','))


class ReferenceWorker:
    def __init__(self,config,store,progress,io,reader,telemetry,metrics,scratch,selected):
        self.config=config; self.store=store; self.progress=progress
        self.io=io; self.reader=reader; self.telemetry=telemetry; self.metrics=metrics
        self.scratch=scratch; self.selected=selected
        self.inflight=set()
        self.queue=ThreadWorker(config.execution['s2_publish_queue_capacity'],
            config.execution['s2_publish_queue_bytes'],name='s2-publication',async_handler=True)

    def cached_page(self,base,fingerprint,offset):
        root=self.store.reference_cache_root(*base[:2])
        if root.exists() and any(p.is_dir() and p.name!=fingerprint for p in root.iterdir()):
            return 'query_fingerprint_changed_refresh_required'
        path=root/fingerprint/f'page-{offset:06d}.json'
        marker=path.with_suffix('.complete.json')
        try:
            size=path.stat().st_size
        except OSError:
            return None
        if size>self.config.execution['s2_response_max_bytes']:
            return 'resource_limit'
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
        included=['normalized.json']
        for item in provenance:
            included.extend([f"page-{item['offset']:06d}.json",f"page-{item['offset']:06d}.complete.json"])
        self.store.storage.commit(base,directory,include=included)
        return len(refs)

    async def publish(self,base,offset,local,query,fingerprint,final,fetched_at):
        started=time.monotonic()
        count=await self.io.call(self.publish_page,base,offset,local,query,fingerprint,final,fetched_at)
        def acknowledge():
            if offset: self.store.finish((*base[:3],offset),'succeeded')
            if final:
                self.store.reference_counts[base[:2]]=count
                self.store.finish(base,'succeeded')
        await self.progress.call(acknowledge)
        if final: self.inflight.discard(base)
        await self.telemetry.call(self.metrics.record,'commit',job=base,service='s2',seconds=time.monotonic()-started)
        directory=self.store.reference_cache_root(*base[:2])/fingerprint
        await self.telemetry.call(self.metrics.touch,directory/f'page-{offset:06d}.json',directory/f'page-{offset:06d}.complete.json')
        if final:
            await self.telemetry.call(self.metrics.record,'reference_snapshot',job=base,service='s2',references=count)
            await self.telemetry.call(self.metrics.touch,self.store.storage.result_dir(base))
            log(f'S2: {base[0]}v{base[1]} complete, {count} references')

    def publish_batch_files(self,batch_id,local,entries,pass_number,fetched_at):
        raw=local.read_bytes(); checksum=hashlib.sha256(raw).hexdigest()
        batch_directory=self.store.root/'data'/'reference-batches'/f'pass-{pass_number}'
        batch_directory.mkdir(parents=True,exist_ok=True)
        batch_path=batch_directory/f'{batch_id}.json'; temporary=batch_path.with_suffix('.tmp')
        with temporary.open('wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        if digest(temporary)!=checksum: raise OSError('Batch response checksum mismatch')
        temporary.replace(batch_path)
        atomic_json(batch_path.with_suffix('.complete.json'),dict(
            sha256=checksum,pass_number=pass_number,item_count=len(entries),fetched_at=fetched_at,
            requested_identifiers=[entry['outcome']['requested_identifier'] for entry in entries]))
        acknowledgements=[]
        for index,entry in enumerate(entries):
            key=entry['key']; outcome=entry['outcome']
            if not outcome['complete']:
                acknowledgements.append((key,None,outcome['error'])); continue
            query,fingerprint=query_identity(key[0],key[1],self.config.network['s2_page_size'])
            directory=self.scratch/f'{quote(key[0],safe="")}-v{key[1]}-ref-{fingerprint[:8]}'
            directory.mkdir(parents=True,exist_ok=True)
            references=outcome['references']
            atomic_json(directory/'normalized.json',dict(references=references,unresolved_count=0,
                reference_count=outcome['reference_count'],fetched_at=fetched_at,
                query_fingerprint=fingerprint,snapshot_id=uuid.uuid4().hex,
                content_sha256=hashlib.sha256(json.dumps(references,sort_keys=True).encode()).hexdigest(),
                batch_provenance={'batch_id':batch_id,'pass':pass_number,'index':index,
                    'sha256':checksum,'returned_paper_id':outcome['returned_paper_id']},**query))
            self.store.storage.commit(key,directory,include=['normalized.json'])
            shutil.rmtree(directory,ignore_errors=True)
            acknowledgements.append((key,len(references),''))
        local.unlink()
        return batch_path,acknowledgements

    async def publish_batch(self,batch_id,local,entries,pass_number,fetched_at):
        started=time.monotonic(); keys=[entry['key'] for entry in entries]
        batch_path,acknowledgements=await self.io.call(
            self.publish_batch_files,batch_id,local,entries,pass_number,fetched_at)
        await self.progress.call(self.store.acknowledge_reference_batch,acknowledgements)
        self.inflight.difference_update(keys)
        await self.telemetry.call(self.metrics.record,'commit',job=keys[0],jobs=keys,
                                  service='s2',seconds=time.monotonic()-started)
        await self.telemetry.call(self.metrics.touch,batch_path,batch_path.with_suffix('.complete.json'))
        complete_paths=[]
        for key,count,_ in acknowledgements:
            if count is None: continue
            await self.telemetry.call(self.metrics.record,'reference_snapshot',job=key,service='s2',references=count)
            complete_paths.append(self.store.storage.result_dir(key))
        if complete_paths: await self.telemetry.call(self.metrics.touch,*complete_paths)
        completed=sum(count is not None for _,count,_ in acknowledgements)
        log(f'S2: batch pass {pass_number} published, {completed} complete, {len(keys)-completed} deferred')

    async def fetch_batch(self,keys,scheduler):
        identifiers=[query_identity(key[0],key[1],self.config.network['s2_page_size'])[0]['requested_identifier'] for key in keys]
        result=await scheduler.request(keys,'s2','https://api.semanticscholar.org/graph/v1/paper/batch',
            params={'fields':BATCH_FIELDS},method='POST',json_body={'ids':identifiers},
            validate=lambda raw:(raw,parse_batch(raw,identifiers)))
        if result is None:
            rows=await self.progress.call(lambda:[dict(self.store.get(key)) for key in keys])
            if 's2' in scheduler.stopped: return
            errors={row['error'] for row in rows}
            shrink=bool(errors) and all(error=='resource_limit' or error.startswith(('transport_','payload_')) for error in errors)
            operation=self.store.reduce_reference_batch if shrink else self.store.advance_reference_pass
            await self.progress.call(operation,keys,'batch_no_response')
            return
        raw,outcomes=result; size=len(raw); pass_number=(await self.progress.call(lambda:self.store.get(keys[0])['reference_pass']))
        batch_id=uuid.uuid4().hex
        local=self.scratch/f's2-batch-{batch_id}.json'
        await self.reader.call(local.write_bytes,raw)
        entries=[dict(key=key,outcome=outcome) for key,outcome in zip(keys,outcomes)]
        self.inflight.update(keys)
        try:
            queued=time.monotonic()
            await self.queue.submit(self.publish_batch,batch_id,local,entries,pass_number,utc_now(),weight=size)
            await self.telemetry.call(self.metrics.record,'publication_queue_wait',job=keys[0],jobs=keys,
                service='s2',seconds=time.monotonic()-queued,items=self.queue.used_items,bytes=self.queue.used_bytes)
        except BaseException:
            self.inflight.difference_update(keys)
            raise

    async def fetch_snapshot(self,base,scheduler):
        paper,version=base[:2]
        query,fingerprint=query_identity(paper,version,self.config.network['s2_page_size'])
        offset=0; byte_count=0
        while True:
            key=(*base[:3],offset)
            await self.progress.call(self.store.ensure,key)
            cached=await self.reader.call(self.cached_page,base,fingerprint,offset)
            if isinstance(cached,str):
                await self.progress.call(self.store.finish,base,'failed',cached); return
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
                del raw, result  # The publication queue retains a path, not response bodies.
            else:
                page,size=cached
            byte_count+=size
            if byte_count>self.config.execution['s2_snapshot_max_bytes']:
                if local: await self.reader.call(local.unlink,missing_ok=True)
                await self.progress.call(self.store.finish,base,'failed','resource_limit'); return
            # Includes active publication and its durable progress acknowledgement.
            queued=time.monotonic()
            await self.queue.submit(self.publish,base,offset,local,query,fingerprint,
                                    page['next'] is None,utc_now(),weight=size)
            await self.telemetry.call(self.metrics.record,'publication_queue_wait',job=key,service='s2',
                seconds=time.monotonic()-queued,items=self.queue.used_items,bytes=self.queue.used_bytes)
            next_offset=page['next']
            page=cached=None
            if next_offset is None: return
            offset=next_offset

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
        if not self.config.network['s2_batch_size']:
            return await self.fetch_loop_individual(scheduler,discovery_done)
        announced=False
        while 's2' not in scheduler.stopped:
            rows=await self.progress.call(lambda:[(k,dict(r)) for k,r in self.store.rows.items()
                if k[0] in self.selected and k[1]>0 and k[2:] == ('references',0)
                and r['status'] in ('pending','running')])
            if not rows:
                if self.queue.used_items: await asyncio.sleep(.01); continue
                if discovery_done.is_set():
                    if not announced: log('S2: no eligible version jobs; discovery must enumerate versions first')
                    break
                await asyncio.sleep(.1); continue
            announced=True
            current_pass=min(row['reference_pass'] for _,row in rows)
            if current_pass>1 and not discovery_done.is_set():
                await asyncio.sleep(.1); continue
            available=[(key,row) for key,row in rows if row['reference_pass']==current_pass and key not in self.inflight]
            if not available:
                await asyncio.sleep(.01); continue
            if current_pass>=5:
                key=available[0][0]; self.inflight.add(key)
                await self.fetch_snapshot(key,scheduler)
                status=await self.progress.call(lambda:self.store.get(key)['status'])
                if status!='running': self.inflight.discard(key)
                continue
            cap=min(row['batch_cap'] for _,row in available)
            attempts=min(row['attempts'] for _,row in available if row['batch_cap']==cap)
            keys=[key for key,row in available if row['batch_cap']==cap and row['attempts']==attempts]
            keys=sorted(keys)[:min(cap,self.config.network['s2_batch_size'])]
            await self.fetch_batch(keys,scheduler)

    async def fetch_loop_individual(self,scheduler,discovery_done):
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
