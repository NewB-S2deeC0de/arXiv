"""Inspect retained source payloads without network calls or changing progress."""
import gzip
import json
import tarfile
import zlib
from pathlib import Path
from .storage import digest
from .progress import ProgressStore


def inspect_payload(path):
    path=Path(path)
    result={'path':path.as_posix(),'bytes':path.stat().st_size,'sha256':digest(path)}
    try:
        with path.open('rb') as stream: prefix=stream.read(65536)
        if prefix.startswith(b'\x1f\x8b'):
            result['container']='gzip'
            with gzip.open(path,'rb') as stream: prefix=stream.read(65536)
        if prefix.lstrip().lower().startswith((b'<!doctype html',b'<html')):
            result['payload']='html'
        elif prefix.startswith(b'%PDF-'):
            result['payload']='pdf'
        elif tarfile.is_tarfile(path):
            result['payload']='tar'
            with tarfile.open(path,'r:*') as archive:
                members=[]
                for member in archive:
                    if len(members)>=50: break
                    members.append({'name':member.name,'size':member.size,'regular':member.isfile(),'directory':member.isdir()})
                result['members_sample']=members
        elif any(token in prefix for token in (b'\\documentclass',b'\\documentstyle',b'\\begin{document}',b'\\input',b'\\def')):
            result['payload']='tex_candidate'
        else:
            result['payload']='unrecognized'
            result['signature_hex']=prefix[:16].hex()
    except (OSError,EOFError,zlib.error,tarfile.TarError) as exc:
        result['inspection_error']=type(exc).__name__
    return result


def diagnose_sources(config):
    store=ProgressStore(config.paths['persistent_root'])
    scratch=Path(config.paths['scratch_root'])
    reports=[]
    for key,row in store.rows.items():
        if key[2]!='source' or row['status']!='failed': continue
        files=sorted(scratch.glob(f'*/{key[0]}v{key[1]}.download'))
        reports.append({'paper':key[0],'version':key[1],'error':row['error'],'attempts':row['attempts'],'retained_payloads':[inspect_payload(p) for p in files]})
    return reports


def diagnose_references(config):
    """Read S2 request/wait evidence without opening the source corpus or calling APIs."""
    from collections import Counter
    import time
    root=Path(config.paths['persistent_root'])
    runs=[]
    for path in sorted((root/'logs').glob('*/events.jsonl')):
        statuses=Counter(); errors=Counter(); waits=0.; requests=0.; commits=0; references=0
        for line in path.open(encoding='utf-8'):
            try: event=json.loads(line)
            except ValueError: continue  # Interrupted final log line.
            job=event.get('job') or []
            if event.get('service')!='s2' and not (len(job)>2 and job[2]=='references'): continue
            if event['event']=='request_end':
                statuses[str(event.get('status_code'))]+=1
                requests+=event.get('seconds',0)
                if event.get('error_code'): errors[event['error_code']]+=1
            elif event['event']=='wait_end': waits+=event.get('seconds',0)
            elif event['event']=='reference_snapshot':
                commits+=1; references+=event.get('references',0)
        runs.append(dict(run_id=path.parent.name,http_status_counts=dict(statuses),
                         errors=dict(errors),request_seconds=requests,wait_seconds=waits,
                         snapshots_recorded=commits,references_recorded=references))
    services=root/'progress/services.json'
    state=json.loads(services.read_text()).get('s2',{}) if services.exists() else {}
    return dict(runs=runs,remaining_cooldown_seconds=max(0,state.get('deadline',0)-time.time()),
                persisted_backoff_seconds=state.get('backoff',0),
                note='Durations may include event-loop blocking. Snapshot counts require new logs; inspect status for dataset totals.')
