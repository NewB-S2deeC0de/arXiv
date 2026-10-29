import gzip
import zlib
import shutil
import tarfile
from pathlib import Path, PurePosixPath
from .storage import atomic_json, digest

def inspect_and_extract(archive,staging,config):
    staging=Path(staging)
    if staging.exists(): shutil.rmtree(staging)
    staging.mkdir(parents=True)
    limits=config.execution; total=0; kept=0; tex=0; bib=0; discarded=0; names=set()
    def save(name,reader,size):
        nonlocal total,kept,tex,bib,discarded
        p=PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or '\\' in name:
            total+=size; discarded+=1; return
        if str(p) in names:
            total+=size; discarded+=1; return
        names.add(str(p)); total+=size
        if p.suffix.lower() not in ('.tex','.bib'):
            discarded+=1; return
        if size>limits['max_member_bytes'] or kept+size>limits['max_unpacked_bytes']:
            raise RuntimeError('resource_limit')
        dest=staging/'tex'/str(p); dest.parent.mkdir(parents=True,exist_ok=True)
        with dest.open('wb') as f: shutil.copyfileobj(reader,f,1024*1024)
        if dest.stat().st_size!=size: raise ValueError('Truncated archive member')
        kept+=size; tex+=p.suffix.lower()=='.tex'; bib+=p.suffix.lower()=='.bib'
    payload=Path(archive)
    with payload.open('rb') as f: signature=f.read(8)
    if signature.startswith(b'\x1f\x8b'):
        inflated=staging/'inflated.tmp'
        try:
            with gzip.open(payload,'rb') as src,inflated.open('wb') as dst:
                count=0
                while chunk:=src.read(1024*1024):
                    count+=len(chunk)
                    if count>limits['max_unpacked_bytes']: raise RuntimeError('resource_limit')
                    dst.write(chunk)
        except EOFError as exc: raise ValueError('Truncated gzip') from exc
        except (gzip.BadGzipFile,zlib.error) as exc: raise ValueError('Invalid gzip') from exc
        payload=inflated
    if tarfile.is_tarfile(payload):
        try:
            with tarfile.open(payload,'r:') as tar:
                for member in tar:
                    if member.isdir(): continue
                    if not member.isfile():
                        total+=max(member.size,0); discarded+=1; continue
                    with tar.extractfile(member) as reader: save(member.name,reader,member.size)
        except tarfile.TarError as exc: raise ValueError('Invalid tar') from exc
    else:
        with payload.open('rb') as f: prefix=f.read(65536)
        if prefix.startswith((b'%PDF-',b'PK\x03\x04',b'%!PS')):
            total=payload.stat().st_size; discarded=1
            if total>min(limits['max_member_bytes'],limits['max_unpacked_bytes']): raise RuntimeError('resource_limit')
        elif any(token in prefix for token in (b'\\documentclass',b'\\documentstyle',b'\\begin{document}',b'\\input',b'\\def')) and b'<html' not in prefix.lower():
            with payload.open('rb') as f: save('source.tex',f,payload.stat().st_size)
        else: raise ValueError('Unrecognized source payload')
    if payload!=Path(archive): payload.unlink()
    result=dict(status='succeeded' if tex else 'no_tex',compressed_bytes=Path(archive).stat().st_size,unpacked_bytes=total,kept_bytes=kept,tex_count=tex,bib_count=bib,discarded_files=discarded,sha256=digest(archive))
    atomic_json(staging/'inventory.json',result)
    return result
