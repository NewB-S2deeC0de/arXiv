"""Verified JSON publication and checksummed result manifests."""
import hashlib
from datetime import datetime, timezone
import uuid
import json
import os
import shutil
from pathlib import Path
from urllib.parse import quote

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def atomic_json(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf8') as f:
        json.dump(value,f,ensure_ascii=False,sort_keys=True); f.flush(); os.fsync(f.fileno())
    assert json.loads(tmp.read_text())==value
    tmp.replace(path)
    if json.loads(path.read_text())!=value: raise OSError('Publication verification failed')

class Storage:
    def __init__(self,root): self.root=Path(root)
    def paper_dir(self,paper): return self.root/'data'/quote(paper,safe='')
    def result_dir(self,key):
        paper,version,stage,_=key
        base=self.paper_dir(paper)
        return base/'versions'/f'v{version}'/stage if version else base/stage
    def commit(self,key,staging,status='succeeded',*,include=None):
        target=self.result_dir(key)
        if target.exists(): self.archive_result(key)
        target.mkdir(parents=True,exist_ok=True)
        marker=target/'complete.json'
        if marker.exists(): marker.unlink()
        hashes={}
        if include is not None:
            for name in include:
                path=Path(name)
                if path.is_absolute() or '..' in path.parts: raise ValueError('Invalid publication path')
                if not (Path(staging)/path).is_file(): raise OSError('Missing publication input')
        sources=Path(staging).rglob('*') if include is None else (Path(staging)/name for name in include)
        for source in sources:
            if source.is_file() and source.name not in ('complete.json','verification.json'):
                rel=source.relative_to(staging); dest=target/rel
                dest.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(source,dest)
                hashes[rel.as_posix()]=digest(source)
                if digest(dest)!=hashes[rel.as_posix()]: raise OSError('Checksum mismatch')
        atomic_json(marker,dict(schema_version=1,key=list(key),status=status,checksums=hashes,captured_at=datetime.now(timezone.utc).isoformat(),run_id=getattr(self,'run_id',''),bytes=sum((target/name).stat().st_size for name in hashes)))
        self.record_verification(key)

    def record_verification(self,key):
        target=self.result_dir(key)
        atomic_json(target/'verification.json',dict(schema_version=1,
            manifest_digest=digest(target/'complete.json'),
            verified_at=datetime.now(timezone.utc).isoformat()))

    def is_verified(self,key):
        target=self.result_dir(key)
        try:
            receipt=json.loads((target/'verification.json').read_text())
            return receipt.get('schema_version')==1 and receipt['manifest_digest']==digest(target/'complete.json')
        except (OSError,ValueError,KeyError): return False

    def verify(self,key):
        target=self.result_dir(key)
        try:
            marker=json.loads((target/'complete.json').read_text())
            if marker['key']!=list(key): return None
            allowed=('succeeded','not_found') if key[2] in ('discovery','metadata') else ('succeeded','no_tex')
            if marker.get('status') not in allowed or not marker.get('checksums'): return None
            required='inventory.json' if key[2]=='source' else 'normalized.json'
            if required not in marker['checksums']: return None
            for name,checksum in marker['checksums'].items():
                p=Path(name)
                if p.is_absolute() or '..' in p.parts or digest(target/p)!=checksum: return None
            return marker
        except (OSError,ValueError,KeyError): return None

    def archive_result(self,key):
        target=self.result_dir(key)
        if not target.exists(): return
        destination=self.paper_dir(key[0])/'history'/f'{key[2]}-{uuid.uuid4().hex}'
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copytree(target,destination)
        for file in target.rglob('*'):
            if file.is_file() and digest(file)!=digest(destination/file.relative_to(target)): raise OSError('History copy failed')
        shutil.rmtree(target)
