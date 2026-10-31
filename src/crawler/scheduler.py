"""Single-flight service schedulers with durable attempt and cooldown accounting."""
import asyncio
import copy
import json
import shutil
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
import httpx
from .workers import join
from .retry import parse_retry_after
from .storage import atomic_json, digest

PAYLOAD_CODES={'Truncated gzip':'payload_gzip_truncated','Unrecognized source payload':'payload_unrecognized','Invalid gzip':'payload_invalid_gzip','Invalid tar':'payload_invalid_tar','Unsafe archive member':'payload_unsafe_member','Non-regular archive member':'payload_nonregular_member','Truncated archive member':'payload_truncated_member'}

class Scheduler:
    def __init__(self,config,store,client,now=time.time,sleep=asyncio.sleep,api_key=None,metrics=None,monotonic=None,progress_worker=None,metrics_worker=None,io_worker=None):
        self.progress_worker=progress_worker; self.metrics_worker=metrics_worker; self.io_worker=io_worker
        self.config=config; self.store=store; self.client=client; self.now=now; self.sleep=sleep; self.api_key=api_key; self.metrics=metrics
        # Injected wall clocks also serve as elapsed clocks in deterministic tests.
        self.monotonic=monotonic or (time.monotonic if now is time.time else now)
        self.s2_next_send=0.
        self.path=store.root/'progress'/'services.json'
        self.state=json.loads(self.path.read_text()) if self.path.exists() else {s:dict(deadline=0,backoff=0,recent=[]) for s in ('arxiv','s2')}
        if self.state['s2'].get('dispatch_pending'):
            # A crash may have occurred after send but before its timestamp was saved.
            gap=config.network['s2_key_gap_seconds' if api_key else 's2_anonymous_gap_seconds']
            self.state['s2']['deadline']=max(self.state['s2']['deadline'],self.now()+gap)
            if self.state['s2']['recent']:
                self.state['s2']['recent'][-1]=self.now()
        self.locks={s:asyncio.Lock() for s in ('arxiv','s2')}; self.stopped=set()
    def event(self,name,**fields):
        if self.metrics: self.metrics.record(name,**fields)
    def persist(self): atomic_json(self.path,self.state)
    async def store_call(self,name,*args):
        method=getattr(self.store,name)
        if self.progress_worker:
            return await self.progress_worker.call(lambda: copy.deepcopy(method(*args)))
        return method(*args)
    async def persist_async(self):
        if self.progress_worker:
            await self.progress_worker.call(atomic_json,self.path,copy.deepcopy(self.state))
        else: self.persist()
    async def event_async(self,name,**fields):
        if self.metrics_worker: await self.metrics_worker.call(self.event,name,**fields)
        else: self.event(name,**fields)
    async def metric(self,name,*args,**kwargs):
        if not self.metrics: return
        if self.metrics_worker: return await self.metrics_worker.call(getattr(self.metrics,name),*args,**kwargs)
        return getattr(self.metrics,name)(*args,**kwargs)
    async def io(self,function,*args,**kwargs):
        if self.io_worker: return await self.io_worker.call(function,*args,**kwargs)
        return function(*args,**kwargs)
    async def wait(self,deadline,job=None,reason='service_cooldown',jobs=None):
        while self.now()<deadline:
            duration=min(30,deadline-self.now())
            await self.event_async('waiting',job=job,jobs=jobs,reason=reason,remaining_seconds=deadline-self.now())
            before=time.monotonic()
            await self.sleep(duration)
            await self.event_async('wait_end',job=job,jobs=jobs,reason=reason,seconds=time.monotonic()-before)
    async def request(self, key, service, url, params=None, target=None, validate=None):
        if service in self.stopped:
            return None
        expected_host = 'export.arxiv.org' if service=='arxiv' else 'api.semanticscholar.org'
        if urlsplit(url).hostname != expected_host or urlsplit(url).scheme!='https': raise ValueError('Disallowed service URL')
        net=self.config.network; state=self.state[service]
        gap = net['arxiv_gap_seconds'] if service == 'arxiv' else net['s2_key_gap_seconds' if self.api_key else 's2_anonymous_gap_seconds']
        queued = time.monotonic()

        batch_keys = key if isinstance(key, list) else [key]
        if not batch_keys or len(set(batch_keys))!=len(batch_keys): raise ValueError('Expected distinct job keys')
        attempts={(await self.store_call('get',k))['attempts'] for k in batch_keys}
        if len(attempts)!=1: raise ValueError('Batch members must have the same attempt count')
        if len(batch_keys)>1 and (service!='arxiv' or len({k[2] for k in batch_keys})!=1 or any(k[2] not in ('discovery','metadata') for k in batch_keys)):
            raise ValueError('Batch requests support homogeneous arXiv metadata stages only')
        primary_key = batch_keys[0]
        async def emit(name,**fields):
            fields.pop('job',None)
            await self.event_async(name,job=primary_key,jobs=batch_keys,service=service,**{k:v for k,v in fields.items() if k!='service'})

        async with self.locks[service]:
            await emit('queue_acquired',job = primary_key,seconds=time.monotonic()-queued)
            while (await self.store_call('get',primary_key))['attempts']<3:
                state['recent'] = [
                    t for t in state['recent']
                    if t > self.now()-net['s2_anonymous_window_seconds']]
                deadline = state['deadline']
                if service == 's2' and not self.api_key and len(state['recent'])>=net['s2_anonymous_window_requests']:
                    deadline = max(
                        deadline, state['recent'][0] + net['s2_anonymous_window_seconds'])
                await self.wait(deadline, primary_key,jobs=batch_keys)
                await self.store_call('start_attempts',batch_keys)
                if service == 's2':
                    state['recent'].append(self.now())
                    # Reserve before send; resume treats an interrupted dispatch conservatively.
                    state['dispatch_pending']=True
                await self.persist_async()
                headers={'User-Agent':net['user_agent']}
                if service=='s2' and self.api_key: headers['x-api-key']=self.api_key
                code=0; error_code='unknown'; received=0; redirected=False
                started=time.monotonic()
                await emit('request_start',job = primary_key, service=service,attempt=(await self.store_call('get',primary_key))['attempts'],journal_directory_sync=self.store.journal.directory_sync_supported)
                try:
                    if service=='s2':
                        # Check again AFTER durable writes/logging; their latency can vary.
                        while self.monotonic()<self.s2_next_send or self.now()<deadline:
                            remaining=max(self.s2_next_send-self.monotonic(),deadline-self.now(),0)
                            await self.wait(self.now()+remaining,primary_key,reason='dispatch_pacing',jobs=batch_keys)
                        sent_at=self.now()
                        self.s2_next_send=self.monotonic()+gap
                        state['deadline']=max(state['deadline'],sent_at+gap)
                        state['recent'][-1]=sent_at
                    async with self.client.stream('GET',url,params=params,headers=headers,follow_redirects=False) as response:
                        code=response.status_code
                        retry_after=parse_retry_after(response.headers.get('Retry-After'),datetime.fromtimestamp(self.now(),timezone.utc))
                        if code==429:
                            state['backoff'] = state['backoff']*2 or 60
                            if service=='s2':
                                state['backoff'] = min(state['backoff'],net['s2_max_backoff_seconds'])
                            state['deadline'] = max(state['deadline'],self.now()+(retry_after if retry_after is not None else state['backoff']))
                        else:
                            state['backoff'] = 0
                            if retry_after is not None:
                                state['deadline'] = max(state['deadline'],self.now()+  retry_after)
                        await self.persist_async()

                        if code in (301, 302, 303, 307, 308):
                            location=response.headers.get('Location','')
                            redirect=urljoin(str(response.url),location)
                            parsed=urlsplit(redirect)
                            if not location or parsed.scheme!='https' or parsed.hostname!=expected_host or parsed.username or parsed.password:
                                await self.store_call('finish_many',[(k,'failed','redirect_disallowed') for k in batch_keys])
                                return None
                            await emit('redirect',job=primary_key,host=parsed.hostname)
                            url=redirect; params=None; redirected=True
                        elif code==200:
                            if target:
                                await self.io(target.parent.mkdir,parents=True,exist_ok=True)
                                metadata = dict(
                                    status_code=code,
                                    content_type=response.headers.get('content-type'),
                                    content_encoding=response.headers.get('content-encoding'),
                                    content_length=response.headers.get('content-length'),
                                    effective_url=str(response.url.copy_with(query=None)),
                                    bytes_received=0,
                                    complete=False)
                                await self.io(atomic_json,target.with_suffix('.response.json'),metadata)
                                f=await self.io(target.open,'wb')
                                try:
                                    # Preserve the actual compressed entity; extraction owns gzip validation.
                                    async for chunk in response.aiter_raw():
                                        if (await self.io(shutil.disk_usage,target.parent)).free < len(chunk) + self.config.execution['min_free_disk_bytes']:
                                            raise OSError('Insufficient disk space during download')
                                        await self.io(f.write,chunk)
                                        received+=len(chunk)
                                finally:
                                    await self.io(f.close)
                                metadata.update(
                                    bytes_received = received,
                                    complete = True,
                                    sha256 = await self.io(digest,target),
                                    download_seconds = time.monotonic()-started
                                )
                                await self.io(atomic_json,target.with_suffix('.response.json'),metadata)
                                result=target
                            else:
                                if service=='s2':
                                    chunks=[]
                                    async for chunk in response.aiter_bytes():
                                        received+=len(chunk)
                                        if received>self.config.execution['s2_response_max_bytes']:
                                            await self.store_call('finish_many',[(k,'failed','resource_limit') for k in batch_keys])
                                            return None
                                        chunks.append(chunk)
                                    result=b''.join(chunks)
                                else:
                                    result = await response.aread()
                                    received=len(result)

                            await emit(
                                'download_end', job = primary_key,
                                bytes_received = received,
                                seconds = time.monotonic()-started
                            )

                            if validate:
                                extract_started = time.monotonic()
                                task = asyncio.create_task(asyncio.to_thread(validate, result))
                                result = await join(task)
                                await emit(
                                    'validation_end', job = key,
                                    seconds = time.monotonic() - extract_started
                                )
                            return result
                        elif code not in (408,429) and code<500:
                            if code in (401,403):
                                self.stopped.add(service)
                            await self.store_call('finish_many',[(k,'not_found' if code==404 else 'failed',f'http_{code}') for k in batch_keys])
                            return None
                except httpx.TransportError as exc:
                    code=0;
                    error_code = 'transport_' + type(exc).__name__
                except httpx.DecodingError:
                    code=0;
                    error_code = 'transport_DecodingError'
                except ValueError as exc:
                    code=0
                    error_code = PAYLOAD_CODES.get(str(exc),'payload_'+type(exc).__name__)
                finally:
                    if service!='s2':
                        state['deadline'] = max(state['deadline'], self.now()+gap)
                    # Cleared only when the actual dispatch timestamp/cooldown is persisted.
                    if service=='s2': state['dispatch_pending']=False
                    await self.persist_async()
                    await emit(
                        'request_end', job=primary_key,
                        service = service, status_code = code,
                        error_code = error_code if code == 0 else None,
                        bytes_received = received,
                        seconds = time.monotonic()-started
                    )
                    if self.metrics:
                        if target: await self.metric('touch',target,target.with_suffix('.response.json'))
                        await self.metric('touch',self.path,self.store.journal.path)
                        await self.metric('sample',force_disk=True)

                attempt=(await self.store_call('get',primary_key))['attempts']
                if attempt<3:
                    if not redirected:
                        delay = net['retry_delays_seconds'][attempt-1]
                        state['deadline'] = max(state['deadline'], self.now() + delay)
                        await self.persist_async()
                        await emit(
                            'retry', job=primary_key,
                            error_code=f'http_{code}' if code else error_code,
                            backoff_seconds = delay)
                else:
                    await self.store_call('finish_many',[(k,'failed',f'http_{code}' if code else error_code) for k in batch_keys])
            unfinished=[(k,'failed','attempts_exhausted') for k in batch_keys if (await self.store_call('get',k))['status'] in ('pending','running')]
            if unfinished: await self.store_call('finish_many',unfinished)
            return None
