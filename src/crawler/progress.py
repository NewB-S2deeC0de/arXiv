"""Single-process progress writer; requests consume attempts before network I/O."""
import csv
import json
import os
import shutil
import threading
from pathlib import Path
from .storage import Storage, atomic_json, digest
from .journal import ProgressJournal

class ProgressStore:
    columns=['paper','version','stage','offset','status','attempts','total','cycle','error']
    def __init__(self,root):
        self.root=Path(root); self.path=self.root/'progress'/'jobs.csv'
        self.lock=threading.RLock(); self.rows={}; self.storage=Storage(root); self.reference_counts={}
        self.dirty_count = 0
        self.journal=ProgressJournal(self.root/'progress/jobs.journal.jsonl')
        self._saved_rows={}
        if self.path.exists() or self.path.with_suffix('.bak').exists() or self.path.with_suffix('.csv.json').exists() or (self.root/'progress/papers.csv').exists() or (self.root/'progress/versions.csv').exists() or (self.root/'progress/backups').exists() or (self.root/'data').exists() or self.journal.path.exists(): self.load()
    def load(self):
        self.journal=ProgressJournal(self.root/'progress/jobs.journal.jsonl')
        for path in (self.path,self.path.with_suffix('.bak')):
            try:
                manifest=json.loads(path.with_suffix(path.suffix+'.json').read_text())
                if digest(path)!=manifest['sha256']: raise ValueError('Invalid checksum')
                with path.open(newline='',encoding='utf8') as f:
                    reader=csv.DictReader(f)
                    if reader.fieldnames!=self.columns: raise ValueError('Invalid schema')
                    rows=list(reader)
                converted={}
                for r in rows:
                    for k in ('version','offset','attempts','total','cycle'): r[k]=int(r[k])
                    key=(r['paper'],r['version'],r['stage'],r['offset'])
                    if key in converted: raise ValueError('Duplicate job')
                    converted[key]=r
                if len(converted)!=manifest['count']: raise ValueError('Invalid row count')
                self.journal.replay(converted,manifest.get('journal_sequence',0))
                self.rows=converted
                self._saved_rows={k:dict(r) for k,r in self.rows.items()}
                view=self.root/'progress/papers.csv'
                if view.exists():
                    try:
                        with view.open(newline='',encoding='utf8') as f:
                            self.reference_counts={r['arxiv_id']:int(r['reference_count']) for r in csv.DictReader(f) if r.get('reference_count','').isdigit()}
                    except (OSError,ValueError,KeyError): pass
                version_view=self.root/'progress/versions.csv'
                if version_view.exists():
                    try:
                        with version_view.open(newline='',encoding='utf8') as f:
                            for r in csv.DictReader(f):
                                if r.get('reference_count','').isdigit():
                                    self.reference_counts[(r['arxiv_id'],int(r['version']))]=int(r['reference_count'])
                    except (OSError,ValueError,KeyError): pass
                return
            except (OSError,ValueError,KeyError): continue
        raise RuntimeError('Progress and backup unavailable or corrupt; refusing empty dataset')
    def save(self, with_views=False):
        changed=[dict(row) for key,row in self.rows.items() if self._saved_rows.get(key)!=row]
        if self.path.exists() or self._saved_rows:
            if changed: self.journal.append(changed)
            if with_views or not self.path.exists(): self._write_snapshot()
        else:
            self._write_snapshot()
        self._saved_rows={key:dict(row) for key,row in self.rows.items()}
        if with_views: self.write_views()

    def _write_snapshot(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.path.with_suffix('.tmp')
        with tmp.open('w',newline='',encoding='utf8') as f:
            w=csv.DictWriter(f,fieldnames=self.columns)
            w.writeheader() 
            w.writerows(self.rows.values())
            f.flush()
            os.fsync(f.fileno())
        valid_current=False
        try:
            valid_current = digest(self.path) == json.loads(self.path.with_suffix('.csv.json').read_text())['sha256']
        except (OSError, ValueError, KeyError): 
            pass
        if valid_current:
            shutil.copyfile(self.path,self.path.with_suffix('.bak'))
            shutil.copyfile(
                self.path.with_suffix('.csv.json'),
                self.path.with_suffix('.bak.json'))
        checksum=digest(tmp)
        tmp.replace(self.path)
        atomic_json(
            self.path.with_suffix('.csv.json'),
            dict(sha256=checksum,count=len(self.rows),journal_sequence=self.journal.sequence))
        if digest(self.path) != checksum: 
            raise OSError('CSV readback failed')
    def add(self,key):
        if key not in self.rows:
            paper,version,stage,offset=key
            self.rows[key]=dict(paper=paper,version=version,stage=stage,offset=offset,status='pending',attempts=0,total=0,cycle=1,error='')
    def initialize(self,ids,*,versioned=False):
        with self.lock:
            for paper in ids:
                for stage in (('discovery',) if versioned else ('discovery','references')): 
                    self.add((paper,0,stage,0))
            if versioned: self.enable_version_references()
            self.save(with_views=True)
    def enable_version_references(self):
        for paper,version in {(k[0],k[1]) for k in self.rows if k[1]>0}:
            self.add((paper,version,'references',0))

    def ensure(self,key):
        self.add(key); self.save()

    def get(self,key): return self.rows[key]
    def pending(self,stage):
        return [k for k,r in self.rows.items() if k[2]==stage and r['status'] in ('pending','running') and r['attempts']<3]
    def start_attempt(self,key):
        self.start_attempts([key])

    def start_attempts(self,keys):
        """Validate all keys before one durable pre-send checkpoint."""
        with self.lock:
            if not keys or len(set(keys))!=len(keys): raise ValueError('Expected distinct job keys')
            for key in keys:
                row=self.rows[key]
                if row['attempts']>=3: raise RuntimeError('Attempt budget exhausted')
                if row['status'] not in ('pending','running'): raise ValueError('Cannot send terminal job')
            for key in keys:
                row=self.rows[key]
                row['attempts']+=1; row['total']+=1; row['status']='running'
                if key[2]=='references': self.rows[(key[0],key[1],'references',0)]['status']='running'
            self.save()

    def finish(self,key,status,error=''):
        self.finish_many([(key,status,error)])

    def finish_many(self,results):
        with self.lock:
            for key,status,error in results:
                if key not in self.rows: raise KeyError(key)
            for key,status,error in results: self.rows[key].update(status=status,error=error)
            self.dirty_count+=len(results)
            with_views=self.dirty_count>=50
            self.save(with_views=with_views)
            if with_views: self.dirty_count=0

    def reconcile(self,recover_running=True,*,ids=None,stages=None,recovery_only=False,skip_legacy_references=False):
        with self.lock:
            for key,row in list(self.rows.items()):
                if ids is not None and key[0] not in ids: continue
                if stages is not None and key[2] not in stages: continue
                if skip_legacy_references and key[1]==0 and key[2]=='references': continue
                if recovery_only and row['status'] not in ('running','pending'): continue
                if recovery_only and row['attempts']==0: continue
                if key[2]=='references' and key[3]!=0:
                    # Page rows are retry history; only base row represents a paper snapshot.
                    continue
                marker=self.storage.verify(key)
                if marker:
                    self.storage.record_verification(key)
                    row['status']=marker['status']
                    if key[2]=='references':
                        self.reference_counts[(key[0],key[1])]=len(json.loads((self.storage.result_dir(key)/'normalized.json').read_text())['references'])
                    if key[2]=='discovery' and marker['status']=='succeeded':
                        meta=json.loads((self.storage.result_dir(key)/'normalized.json').read_text())
                        for v in range(1,meta['version']+1):
                            for stage in ('metadata','source','references'): self.add((key[0],v,stage,0))
                elif row['status'] in ('running','succeeded','no_tex') and (recover_running or row['status']!='running'):
                    attempts=row['attempts']
                    if key[2]=='references':
                        next_offset,finished=self.reference_resume_offset(key[0],key[1])
                        attempts=0 if finished else self.rows.get((key[0],key[1],'references',next_offset),{}).get('attempts',0)
                    row['status']='pending' if attempts<3 else 'failed'
                    row['error']='integrity_missing_result'
            self.save(with_views=True)
    def reset(self,status='failed',stages=None,ids=None):
        with self.lock:
            retry_papers={(k[0],k[1]) for k,r in self.rows.items() if k[2]=='references' and k[3]==0 and r['status']==status}
            for key,row in self.rows.items():
                if stages and key[2] not in stages: continue
                if ids is not None and key[0] not in ids: continue
                if row['status']==status or (key[2]=='references' and (key[0],key[1]) in retry_papers and (row['status']!='succeeded' or not self.valid_reference_page(key[0],key[3],key[1]))):
                    if status=='not_found' and key[2] in ('discovery','metadata'): self.storage.archive_result(key)
                    row.update(status='pending',attempts=0,cycle=row['cycle']+1,error='')
            self.save()

    def status_counts(self):
        from collections import Counter
        counts={stage:Counter() for stage in ('discovery','metadata','source','references')}
        versioned=any(k[1]>0 and k[2]=='references' for k in self.rows)
        if versioned: counts['legacy_references']=Counter()
        for key,row in self.rows.items():
            if key[2]=='references' and key[3]!=0: continue
            stage='legacy_references' if versioned and key[2]=='references' and key[1]==0 else key[2]
            counts[stage][row['status']]+=1
        return {stage:dict(values) for stage,values in counts.items()}

    def write_views(self):
        """Paper/version snapshots; request ledger retains per-page retry history."""
        from datetime import datetime, timezone
        now=datetime.now(timezone.utc).isoformat()
        paper_columns='schema_version,arxiv_id,order_index,discovery_status,latest_version,discovery_attempts_total,discovery_cycle_attempts,discovery_cycle_id,discovery_error_code,discovery_error_message,references_status,references_attempts_total,references_cycle_id,references_current_offset,references_page_cycle_attempts,references_error_code,references_error_message,reference_count,updated_at,last_run_id'.split(',')
        version_columns='schema_version,arxiv_id,version,order_index,metadata_status,metadata_attempts_total,metadata_cycle_attempts,metadata_cycle_id,metadata_error_code,metadata_error_message,source_status,source_attempts_total,source_cycle_attempts,source_cycle_id,source_error_code,source_error_message,updated_at,last_run_id'.split(',')
        version_columns += ['references_'+field for field in ('status','attempts_total','cycle_attempts','cycle_id','error_code','error_message')]
        paper_columns += ['references_scope','reference_versions_total','reference_versions_succeeded']
        version_columns += ['references_current_offset','reference_count']
        ids=list(dict.fromkeys(k[0] for k in self.rows if k[2]=='discovery'))
        order={paper:i for i,paper in enumerate(ids)}
        papers=[]; versions=[]
        grouped={}
        for key,value in self.rows.items(): grouped.setdefault(key[0],[]).append((key,value))
        for paper in ids:
            d=self.rows[(paper,0,'discovery',0)]; r=self.rows.get((paper,0,'references',0),dict(status='not_scheduled',attempts=0,total=0,cycle=1,error=''))
            pages=[v for k,v in grouped[paper] if k[2]=='references' and k[1]==0]
            current=max(pages,key=lambda v:v['offset']) if pages else dict(offset=0,attempts=0)
            latest=max((k[1] for k,v in grouped[paper]),default=0)
            item=dict(schema_version=2,arxiv_id=paper,order_index=order[paper],latest_version=latest or '',updated_at=now,last_run_id=getattr(self,'run_id',''))
            for stage,row in [('discovery',d),('references',r)]:
                item.update({f'{stage}_status':row['status'],f'{stage}_attempts_total':row['total'],f'{stage}_cycle_id':row['cycle'],f'{stage}_error_code':row['error'],f'{stage}_error_message':row['error']})
            item.update(discovery_cycle_attempts=d['attempts'],references_attempts_total=sum(p['total'] for p in pages),references_current_offset=current['offset'],references_page_cycle_attempts=current['attempts'],reference_count='')
            if r['status']=='succeeded': item['reference_count']=self.reference_counts.get((paper,0),self.reference_counts.get(paper,''))
            version_refs=[v for k,v in grouped[paper] if k[1]>0 and k[2:] == ('references',0)]
            item.update(references_scope='legacy_paper',reference_versions_total=len(version_refs),reference_versions_succeeded=sum(v['status']=='succeeded' for v in version_refs))
            papers.append(item)
        for paper,version in sorted({(k[0],k[1]) for k in self.rows if k[1]}):
            item=dict(schema_version=2,arxiv_id=paper,version=version,order_index=order[paper],updated_at=now,last_run_id=getattr(self,'run_id',''))
            for stage in ('metadata','source','references'):
                row=self.rows.get((paper,version,stage,0),dict(status='pending',attempts=0,total=0,cycle=1,error=''))
                for field,value in [('status',row['status']),('attempts_total',row['total']),('cycle_attempts',row['attempts']),('cycle_id',row['cycle']),('error_code',row['error']),('error_message',row['error'])]: item[f'{stage}_{field}']=value
            pages=[r for k,r in grouped[paper] if k[1]==version and k[2]=='references']
            item['references_attempts_total']=sum(r['total'] for r in pages)
            item['references_current_offset']=max((r['offset'] for r in pages),default=0)
            item['reference_count']=self.reference_counts.get((paper,version),'')
            versions.append(item)
        for name,columns,rows in [('papers',paper_columns,papers),('versions',version_columns,versions)]:
            target=self.root/'progress'/f'{name}.csv'; tmp=target.with_suffix('.tmp')
            with tmp.open('w',newline='',encoding='utf8') as f:
                writer=csv.DictWriter(f,fieldnames=columns); writer.writeheader(); writer.writerows(rows); f.flush(); os.fsync(f.fileno())
            with tmp.open(newline='',encoding='utf8') as f:
                if len(list(csv.DictReader(f)))!=len(rows): raise OSError('Snapshot row count mismatch')
            if target.exists():
                backup=self.root/'progress'/'backups'/target.name; backup.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(target,backup)
            checksum=digest(tmp); tmp.replace(target)
            if digest(target)!=checksum: raise OSError('Snapshot readback failed')

    def refresh(self,stage,ids=None):
        if stage not in ('discovery','references'): raise ValueError('Only discovery/references can be refreshed')
        with self.lock:
            for key,row in list(self.rows.items()):
                if ids is not None and key[0] not in ids: continue
                if key[2]!=stage or key[3]!=0 or (ids is not None and key[0] not in ids): continue
                self.storage.archive_result(key)
                if stage=='references':
                    staging=self.reference_cache_root(key[0],key[1])
                    if staging.exists():
                        import uuid
                        history=self.storage.paper_dir(key[0])/'history'/('reference-pages-'+uuid.uuid4().hex)
                        history.parent.mkdir(parents=True,exist_ok=True); shutil.move(staging,history)
                    for pkey,page in self.rows.items():
                        if pkey[:2]==key[:2] and pkey[2]=='references': page.update(status='pending',attempts=0,cycle=page['cycle']+1,error='')
                else: row.update(status='pending',attempts=0,cycle=row['cycle']+1,error='')
            self.save()

    def recover_reference_page(self,key):
        """Reopen a successful page whose cache is invalid, without a new retry cycle."""
        if key[2]!='references': raise ValueError('Expected references job')
        with self.lock:
            row=self.rows[key]
            if row['status']=='succeeded':
                row.update(status='pending' if row['attempts']<3 else 'failed',
                           error='integrity_missing_result')
                self.save()
            return row['status'] in ('pending','running') and row['attempts']<3

    def reference_cache_root(self,paper,version=0):
        base=self.storage.paper_dir(paper)
        if version: base=base/'versions'/f'v{version}'
        return base/'reference-staging'

    def valid_reference_page(self,paper,offset,version=0):
        from .semantic_scholar import parse_page
        root=self.reference_cache_root(paper,version)
        directories=[p for p in root.iterdir() if p.is_dir()] if root.exists() else []
        if len(directories)!=1: return None
        directory=directories[0]
        raw=directory/f'page-{offset:06d}.json'; marker=directory/f'page-{offset:06d}.complete.json'
        try:
            data=json.loads(marker.read_text())
            if data.get('fingerprint')!=directory.name or data.get('sha256')!=digest(raw): return None
            return parse_page(raw.read_bytes(),offset)
        except (OSError,ValueError,TypeError,AttributeError): return None

    def reference_resume_offset(self,paper,version=0):
        offset=0
        while True:
            page=self.valid_reference_page(paper,offset,version)
            if page is None: return offset,False
            if page['next'] is None: return offset,True
            offset=page['next']
