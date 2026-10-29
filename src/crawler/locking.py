"""Local exclusion plus an explicit stale-run takeover marker for Drive datasets."""
import json
import os
from pathlib import Path
try:
    import fcntl
except ImportError:
    fcntl=None
    import msvcrt
from .storage import atomic_json
from .console import log

class DatasetLock:
    def __init__(self,root,run_id,takeover_stale=False):
        self.root=Path(root); self.run_id=run_id; self.takeover=takeover_stale; self.previous=None
    def _lock(self):
        if fcntl: fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        else:
            if os.fstat(self.file.fileno()).st_size==0:
                self.file.write(b'\0'); self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
    def _unlock(self):
        if fcntl: fcntl.flock(self.file,fcntl.LOCK_UN)
        else:
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
    def __enter__(self):
        log('Lock: creating/checking dataset directory')
        self.root.mkdir(parents=True,exist_ok=True)
        log('Lock: opening writer.lock')
        self.file=(self.root/'writer.lock').open('a+b')
        try:
            log('Lock: acquiring local writer lock')
            self._lock()
            self.marker=self.root/'progress'/'active_run.json'
            log('Lock: checking previous active marker')
            if self.marker.exists():
                log('Lock: reading previous active marker')
                self.previous=json.loads(self.marker.read_text())
                if not self.takeover: raise RuntimeError('Previous active marker exists; stop the previous runtime before explicit takeover')
            log('Lock: publishing active marker')
            atomic_json(self.marker,dict(run_id=self.run_id,previous=self.previous))
            if self.previous:
                from datetime import datetime, timezone
                atomic_json(self.root/'progress'/'takeovers'/f'{self.run_id}.json',dict(run_id=self.run_id,previous=self.previous,captured_at=datetime.now(timezone.utc).isoformat()))
            log('Lock: acquired')
            return self
        except BaseException:
            self.file.close(); raise
    def __exit__(self,*args):
        log('Lock: releasing active marker')
        try: self.marker.unlink(missing_ok=True)
        finally:
            try: self._unlock()
            finally: self.file.close()
