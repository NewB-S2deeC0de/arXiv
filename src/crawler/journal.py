"""Durable hash-chained progress deltas. A partial final line is never committed."""
import hashlib
import errno
import json
import os
from pathlib import Path


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf8')


class ProgressJournal:
    def __init__(self,path):
        self.path=Path(path); self.sequence=0; self.previous=''; self.valid_size=0; self.directory_sync_supported=None

    def replay(self,rows,snapshot_sequence=0):
        if self.path.exists():
            with self.path.open('rb') as stream:
                for line in stream:
                    if not line.endswith(b'\n'): break
                    try:
                        entry=json.loads(line)
                        checksum=entry.pop('checksum')
                        if entry['sequence']!=self.sequence+1 or entry['previous']!=self.previous or hashlib.sha256(encode(entry)).hexdigest()!=checksum:
                            raise ValueError('Hash chain mismatch')
                        for row in entry['rows']:
                            key=(row['paper'],row['version'],row['stage'],row['offset'])
                            if not all(type(row[n]) is int and row[n]>=0 for n in ('version','offset','attempts','total','cycle')): raise ValueError('Invalid progress integers')
                            if entry['sequence']>snapshot_sequence: rows[key]=row
                    except (ValueError,KeyError,TypeError) as exc:
                        raise RuntimeError('Corrupt progress journal; refusing to discard attempt history') from exc
                    self.sequence=entry['sequence']; self.previous=checksum; self.valid_size+=len(line)
        if self.sequence<snapshot_sequence:
            raise RuntimeError('Progress journal missing committed records required by snapshot')

    def append(self,rows):
        payload=dict(sequence=self.sequence+1,previous=self.previous,rows=rows)
        checksum=hashlib.sha256(encode(payload)).hexdigest()
        line=encode(dict(payload,checksum=checksum))+b'\n'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.open('r+b' if self.path.exists() else 'w+b') as stream:
            # Drop only an uncommitted torn tail previously inspected by replay.
            stream.truncate(self.valid_size); stream.seek(self.valid_size)
            stream.write(line); stream.flush(); os.fsync(stream.fileno())
            stream.seek(self.valid_size)
            if stream.read(len(line))!=line: raise OSError('Journal readback failed')
        if self.directory_sync_supported is None:
            descriptor=None
            try:
                descriptor=os.open(self.path.parent,os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
                os.fsync(descriptor)
                self.directory_sync_supported=True
            except OSError as exc:
                if exc.errno not in (errno.EINVAL,errno.ENOTSUP,errno.EBADF,errno.ENOSYS,errno.EACCES,errno.EPERM): raise
                # Drive/FUSE may not expose directory fsync. File fsync/readback
                # still precedes network I/O; record the reduced guarantee.
                self.directory_sync_supported=False
            finally:
                if descriptor is not None: os.close(descriptor)
        self.sequence+=1; self.previous=checksum; self.valid_size+=len(line)
