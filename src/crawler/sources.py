import gzip
import zlib
import shutil
import tarfile
import zipfile
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pathlib import Path, PurePosixPath
from .storage import atomic_json, digest

def is_docx(payload, limits):
    """Recognize a bounded, CRC-valid OOXML Word package without extraction."""
    if not zipfile.is_zipfile(payload): return False
    try:
        with zipfile.ZipFile(payload) as archive:
            entries=archive.infolist()
            names=[entry.filename for entry in entries]
            if len(names)!=len(set(names)): raise ValueError('Duplicate ZIP member')
            for entry in entries:
                path=PurePosixPath(entry.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in entry.filename:
                    raise ValueError('Unsafe archive member')
                if entry.file_size>limits['max_member_bytes']: raise RuntimeError('resource_limit')
            if sum(entry.file_size for entry in entries)>limits['max_unpacked_bytes']:
                raise RuntimeError('resource_limit')
            if not {'[Content_Types].xml','word/document.xml'}.issubset(names): return False
            if any(entry.flag_bits & 1 for entry in entries): raise ValueError('Encrypted DOCX')
            if archive.testzip() is not None: raise ValueError('Invalid DOCX CRC')
            types=ElementTree.fromstring(archive.read('[Content_Types].xml'))
            expected='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
            if not any(e.get('PartName')=='/word/document.xml' and e.get('ContentType')==expected for e in types): return False
            document=ElementTree.fromstring(archive.read('word/document.xml'))
            return sum(entry.file_size for entry in entries) if document.tag=='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document' else False
    except (zipfile.BadZipFile, ElementTree.ParseError, DefusedXmlException) as exc:
        raise ValueError('Invalid DOCX') from exc


def is_html_paper(prefix):
    lowered = prefix.lower()
    if not (b'<html' in lowered or b'<!doctype html' in lowered): return False
    if not (b'<body' in lowered and (b'<head' in lowered or b'<title' in lowered or b'<!doctype' in lowered)): return False
    if any(err in lowered for err in (b'oops', b'access denied', b'attention required', b'cloudflare', b'captcha', b'just a moment')): return False
    return True


def inspect_and_extract(archive,staging,config):
    staging=Path(staging)
    if staging.exists(): shutil.rmtree(staging)
    staging.mkdir(parents=True)
    limits=config.execution; total=0; kept=0; tex=0; bib=0; discarded=0; names=set()
    def save(name,reader,size):
        nonlocal total,kept,tex,bib,discarded
        p=PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or '\\' in name or str(p) in names: raise ValueError('Unsafe archive member')
        names.add(str(p)); total+=size
        if size>limits['max_member_bytes'] or total>limits['max_unpacked_bytes']: raise RuntimeError('resource_limit')
        if p.suffix.lower() not in ('.tex','.bib'): discarded+=1; return
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
                    member_path=PurePosixPath(member.name)
                    if member_path.is_absolute() or '..' in member_path.parts or '\\' in member.name: raise ValueError('Unsafe archive member')
                    if member.isdir(): continue
                    if not member.isfile(): raise ValueError('Non-regular archive member')
                    with tar.extractfile(member) as reader: save(member.name,reader,member.size)
        except tarfile.TarError as exc: raise ValueError('Invalid tar') from exc
    else:
        with payload.open('rb') as f: prefix=f.read(65536)
        if prefix.startswith(b'%PDF-'):
            total=payload.stat().st_size; discarded=1
            if total>min(limits['max_member_bytes'],limits['max_unpacked_bytes']): raise RuntimeError('resource_limit')
        elif any(token in prefix for token in (b'\\documentclass',b'\\documentstyle',b'\\begin{document}',b'\\input',b'\\def')) and b'<html' not in prefix.lower():
            with payload.open('rb') as f: save('source.tex',f,payload.stat().st_size)
        elif docx_size:=is_docx(payload,limits):
            total=docx_size; discarded=1
        elif is_html_paper(prefix):
            total=payload.stat().st_size; discarded=1
            if total>min(limits['max_member_bytes'],limits['max_unpacked_bytes']): raise RuntimeError('resource_limit')
        else: raise ValueError('Unrecognized source payload')
    if payload!=Path(archive): payload.unlink()
    result=dict(no_tex_reason=None if tex else 'valid_source_without_tex',status='succeeded' if tex else 'no_tex',compressed_bytes=Path(archive).stat().st_size,unpacked_bytes=total,kept_bytes=kept,tex_count=tex,bib_count=bib,discarded_files=discarded,sha256=digest(archive))
    atomic_json(staging/'inventory.json',result)
    return result
