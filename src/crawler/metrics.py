"""Measured per-session telemetry; monotonic durations and UTC audit timestamps."""
import asyncio
import csv
import importlib.metadata
import json
import platform
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import psutil
from .storage import atomic_json
from .disk_usage import DiskUsage
from .console import log as console_log


def utc_now(): return datetime.now(timezone.utc).isoformat()


def weighted_mean(samples):
    duration=sum(b[0]-a[0] for a,b in zip(samples,samples[1:]))
    return sum(a[1]*(b[0]-a[0]) for a,b in zip(samples,samples[1:]))/duration if duration else None


def unique_roots(paths):
    result=[]
    for path in sorted(set(map(Path,paths)),key=lambda p:(len(p.parts),str(p))):
        if not any(path==root or root in path.parents for root in result): result.append(path)
    return result


class MetricsRecorder:
    def __init__(self,config,auth_mode='anonymous'):
        self.config=config; self.root=Path(config.paths['persistent_root']); self.auth_mode=auth_mode
        self.samples=[]; self.paper_start={}; self.paper_end={}; self.disk_bytes=0; self.last_disk=-float('inf'); self.task=None; self.durations={}
    def start(self,run_id):
        console_log('Metrics: creating log and resource files')
        self.run_id=run_id; self.started=time.monotonic(); self.started_at=utc_now()
        self.directory=self.root/'metrics'/run_id; self.directory.mkdir(parents=True,exist_ok=True)
        log=self.root/'logs'/run_id/'events.jsonl'; log.parent.mkdir(parents=True,exist_ok=True)
        self.log=log.open('a',encoding='utf8')
        self.resources=(self.directory/'resources.csv').open('w',newline='',encoding='utf8')
        self.writer=csv.DictWriter(self.resources,fieldnames=['elapsed_seconds','utc','rss_bytes','disk_bytes']); self.writer.writeheader()
        packages={}
        for name in ('httpx','defusedxml','PyYAML','psutil','pyarrow'):
            try: packages[name]=importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError: packages[name]=None
        console_log('Metrics: collecting environment and writing environment.json')
        atomic_json(self.directory/'environment.json',dict(run_id=run_id,started_at=self.started_at,python=platform.python_version(),packages=packages,cpu_count=psutil.cpu_count(),ram_available_bytes=psutil.virtual_memory().available,disk_free_bytes=shutil.disk_usage(self.root).free,authentication=self.auth_mode,disk_measurement_scope='session_touched_paths',config=asdict(self.config)))
        console_log('Metrics: initializing session path counters without dataset scan')
        self.disk=DiskUsage(self.config.paths.values(),scan_existing=False)
        console_log('Metrics: session counters ready; writing initial sample')
        self.sample(force_disk=True)
    def record(self,event,**fields):
        # Callers pass only identifiers, durations, status codes; never headers/body/exception text.
        for key in ('headers','api_key','authorization','body'): fields.pop(key,None)
        row=dict(event=event,run_id=self.run_id,utc=utc_now(),elapsed_seconds=time.monotonic()-self.started,**fields)
        self.log.write(json.dumps(row,ensure_ascii=False)+'\n'); self.log.flush()
        if 'seconds' in fields: self.durations[event]=self.durations.get(event,0)+fields['seconds']
        jobs=fields.get('jobs') or ([fields['job']] if fields.get('job') else [])
        for job in jobs:
            self.paper_start.setdefault(job[0],row['elapsed_seconds'])
            self.paper_end[job[0]]=row['elapsed_seconds']
    def sample(self,force_disk=False):
        elapsed=time.monotonic()-self.started
        process=psutil.Process(); rss=process.memory_info().rss
        for child in process.children(recursive=True):
            try: rss+=child.memory_info().rss
            except (psutil.NoSuchProcess,psutil.AccessDenied): pass
        # `force_disk` now samples the incremental counter, never a full walk.
        if elapsed-self.last_disk>=self.config.execution['disk_sample_seconds'] or force_disk:
            for name in ('environment.json','checkpoint.json','resources.csv','paper_metrics.csv','summary.json'): self.disk.touch(self.directory/name)
            self.disk.touch(self.root/'logs'/self.run_id/'events.jsonl')
            self.last_disk=elapsed
        self.disk_bytes=self.disk.total
        self.samples.append((elapsed,rss,self.disk_bytes))
        atomic_json(self.directory/'checkpoint.json',dict(run_id=self.run_id,started_at=self.started_at,last_sample_at=utc_now(),wall_seconds=elapsed,incomplete=True,event_seconds=self.durations,disk_measurement_scope='session_touched_paths'))
        self.writer.writerow(dict(elapsed_seconds=elapsed,utc=utc_now(),rss_bytes=rss,disk_bytes=self.disk_bytes)); self.resources.flush()
    def touch(self,*paths):
        for path in paths: self.disk.touch(path)
        self.disk_bytes=self.disk.total

    def reconcile_disk(self):
        """Explicit full measurement, returned separately from session counters."""
        console_log('Metrics: explicit full disk scan started')
        total=DiskUsage(self.config.paths.values()).total
        console_log('Metrics: explicit full disk scan finished')
        return total

    async def monitor(self):
        while True:
            await asyncio.sleep(self.config.execution['ram_sample_seconds'])
            self.sample()
    def stop(self,store=None,incomplete=False):
        self.sample(force_disk=True)
        wall=time.monotonic()-self.started
        paper_rows=[]
        for paper,start in self.paper_start.items():
            rows=[r for k,r in store.rows.items() if k[0]==paper and (k[2]!='references' or k[3]==0)] if store else []
            if any(r['stage']=='references' and r['version']>0 for r in rows):
                rows=[r for r in rows if r['stage']!='references' or r['version']>0]
            complete=bool(rows) and all(r['status'] in ('succeeded','no_tex','not_found','failed') for r in rows)
            paper_rows.append(dict(arxiv_id=paper,run_id=self.run_id,elapsed_seconds=self.paper_end[paper]-start,complete=complete))
        with (self.directory/'paper_metrics.csv').open('w',newline='',encoding='utf8') as f:
            writer=csv.DictWriter(f,fieldnames=['arxiv_id','run_id','elapsed_seconds','complete']); writer.writeheader(); writer.writerows(paper_rows)
        summary=dict(run_id=self.run_id,started_at=self.started_at,finished_at=utc_now(),wall_seconds=wall,rss_peak_bytes=max(s[1] for s in self.samples),rss_time_weighted_mean_bytes=weighted_mean([(s[0],s[1]) for s in self.samples]),disk_observed_peak_bytes=max(s[2] for s in self.samples),ram_sample_seconds=self.config.execution['ram_sample_seconds'],disk_sample_seconds=self.config.execution['disk_sample_seconds'],disk_measurement_scope='session_touched_paths',sampling_note='Logical bytes of paths observed in this session, including existing files in touched subtrees; not total dataset size or downloaded bytes; observed peaks can miss spikes; not filesystem allocation',authentication=self.auth_mode,status_counts=store.status_counts() if store else {})
        previous=[]; interrupted=[]
        for directory in (self.root/'metrics').iterdir():
            if not directory.is_dir() or directory.name==self.run_id: continue
            path=directory/'summary.json'
            if not path.exists(): path=directory/'checkpoint.json'
            if not path.exists(): continue
            try:
                record=json.loads(path.read_text()); previous.append(record)
                if record.get('incomplete'): interrupted.append(record['run_id'])
            except (OSError,ValueError): pass
        summary['incomplete']=incomplete
        summary['event_seconds']=self.durations
        summary['active_wall_seconds']=wall+sum(r.get('wall_seconds',0) for r in previous)
        summary['contributing_run_ids']=[r.get('run_id') for r in previous]+[self.run_id]
        summary['interrupted_run_ids']=interrupted
        starts=[r.get('started_at',self.started_at) for r in previous]+[self.started_at]
        summary['calendar_elapsed_seconds']=(datetime.fromisoformat(summary['finished_at'])-min(datetime.fromisoformat(s) for s in starts)).total_seconds()
        if store:
            counts=store.status_counts()
            def rate(n,d): return dict(numerator=n,denominator=d,value=n/d if d else None)
            papers=sum(counts['discovery'].values()); versions=sum(counts['source'].values())
            versioned='legacy_references' in counts
            queried=sum(1 for k,r in store.rows.items() if k[2]=='references' and k[3]==0 and r['total']>0 and (not versioned or k[1]>0))
            summary['reference_unit']='arxiv_version' if versioned else 'paper_legacy'
            summary['rates']=dict(metadata_success=rate(counts['discovery'].get('succeeded',0),papers),source_coverage=rate(counts['source'].get('succeeded',0)+counts['source'].get('no_tex',0),versions),tex_availability=rate(counts['source'].get('succeeded',0),versions),s2_query_success=rate(counts['references'].get('succeeded',0),queried))
        atomic_json(self.directory/'summary.json',summary)
        self.resources.close(); self.log.close()
        return summary
