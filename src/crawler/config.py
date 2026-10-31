from dataclasses import dataclass, field
from pathlib import Path
import yaml
from .ids import generate_ids

@dataclass
class CrawlerConfig:
    range: dict = field(default_factory=lambda: dict(start_month='2025-04',start_id=2523,end_month='2025-04',end_id=7522))
    paths: dict = field(default_factory=lambda: dict(persistent_root='runtime',scratch_root='runtime/temp'))
    network: dict = field(default_factory=lambda: dict(connect_timeout_seconds=20,read_timeout_seconds=120,max_attempts=3,s2_max_attempts=50,discovery_batch_size=50,metadata_batch_size=50,retry_delays_seconds=[10,30],arxiv_gap_seconds=3.0,s2_key_gap_seconds=1.1,s2_anonymous_gap_seconds=3.1,s2_anonymous_window_seconds=300,s2_anonymous_window_requests=100,s2_page_size=100,user_agent='HCMUS-DS-Course-Crawler/1.0'))
    execution: dict = field(default_factory=lambda: dict(progress_queue_capacity=100,s2_publish_queue_capacity=8,s2_publish_queue_bytes=33554432,s2_response_max_bytes=8388608,s2_snapshot_max_bytes=67108864,queue_capacity=100,ram_sample_seconds=1,disk_sample_seconds=10,allow_anonymous_s2=False,max_member_bytes=268435456,max_unpacked_bytes=1073741824,min_free_disk_bytes=1073741824))
    schema_version: int = 1

    def validate(self):
        for value in self.paths.values():
            p=Path(value)
            if p.is_absolute() or '..' in p.parts: raise ValueError('Project paths must be relative and contained')
        if self.schema_version!=1: raise ValueError('Unsupported config schema')
        for name in ('queue_capacity','max_member_bytes','max_unpacked_bytes','ram_sample_seconds','disk_sample_seconds'):
            if self.execution[name]<=0: raise ValueError(f'{name} must be positive')
        for name in ('progress_queue_capacity','s2_publish_queue_capacity','s2_publish_queue_bytes','s2_response_max_bytes','s2_snapshot_max_bytes'):
            if type(self.execution[name]) is not int or self.execution[name]<1: raise ValueError(f'{name} must be a positive integer')
        if self.execution['s2_response_max_bytes']>self.execution['s2_publish_queue_bytes']: raise ValueError('Response limit exceeds publication queue budget')
        if self.network['s2_key_gap_seconds']<1: raise ValueError('S2 key gap must be >=1 second')
        for name in ('connect_timeout_seconds','read_timeout_seconds','s2_key_gap_seconds','s2_anonymous_gap_seconds','s2_anonymous_window_seconds','s2_anonymous_window_requests'):
            if self.network[name]<=0: raise ValueError(f'{name} must be positive')
        if len(self.network['retry_delays_seconds'])!=2 or any(v<0 for v in self.network['retry_delays_seconds']): raise ValueError('Expected two nonnegative retry delays')
        generate_ids(**self.range)
        if type(self.network['discovery_batch_size']) is not int or not 1<=self.network['discovery_batch_size']<=50: raise ValueError('discovery_batch_size must be 1..50')
        if type(self.network['metadata_batch_size']) is not int or not 1<=self.network['metadata_batch_size']<=50: raise ValueError('metadata_batch_size must be 1..50')
        if type(self.network['s2_max_attempts']) is not int or self.network['s2_max_attempts']<1: raise ValueError('s2_max_attempts must be a positive integer')
        if self.network['max_attempts'] != 3: raise ValueError('max_attempts must be 3')
        if self.network['arxiv_gap_seconds'] < 3: raise ValueError('arxiv gap must be >=3')
        if not 1 <= self.network['s2_page_size'] <= 1000: raise ValueError('Invalid page size')
        return self


def load_config(path: Path) -> CrawlerConfig:
    config=CrawlerConfig()
    raw=yaml.safe_load(Path(path).read_text()) or {}
    for key,value in raw.items():
        if key not in config.__dataclass_fields__: raise ValueError(f'Unknown config field: {key}')
        if isinstance(getattr(config,key),dict): getattr(config,key).update(value)
        else: setattr(config,key,value)
    return config.validate()
