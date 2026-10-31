"""Commands run from the selected project root; all mutations share one lock."""
import argparse
import asyncio
import json
import uuid
from pathlib import Path
from .config import load_config
from .ids import generate_ids
from .progress import ProgressStore
from .pipeline import run_pipeline
from .locking import DatasetLock


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['init','run','resume','status','retry-failed','recheck-not-found','refresh-discovery','refresh-references','diagnose','diagnose-references','export'])
    parser.add_argument('--config',type=Path,default=Path('config/crawler.yaml'))
    parser.add_argument('--run-id')
    parser.add_argument('--limit',type=int,default=None)
    parser.add_argument('--stages',nargs='+',choices=['arxiv','s2'],default=['arxiv','s2'])
    parser.add_argument('--takeover-stale',action='store_true')
    args=parser.parse_args(); config=load_config(args.config)
    if args.limit is not None and args.limit<1: parser.error('--limit must be positive')
    if args.command in ('run','resume'):
        result=asyncio.run(run_pipeline(config,set(args.stages),args.command=='resume',limit=args.limit,takeover_stale=args.takeover_stale))
    elif args.command=='diagnose':
        from .diagnostics import diagnose_sources
        result=diagnose_sources(config)
    elif args.command=='diagnose-references':
        from .diagnostics import diagnose_references
        result=diagnose_references(config)
    elif args.command=='status': result=ProgressStore(config.paths['persistent_root']).status_counts()
    else:
        with DatasetLock(config.paths['persistent_root'],'admin-'+uuid.uuid4().hex,args.takeover_stale):
            if args.command=='export':
                from .export import _export_snapshot
                if not args.run_id: parser.error('--run-id required')
                result=_export_snapshot(config,args.run_id)
            else:
                store=ProgressStore(config.paths['persistent_root'])
                ids=generate_ids(**config.range)
                selected=set(ids[:args.limit]) if args.limit else set(ids)
                stages=set()
                if 'arxiv' in args.stages: stages.update(('discovery','metadata','source'))
                if 's2' in args.stages: stages.add('references')
                if args.command=='init': store.initialize(ids,versioned=True)
                elif args.command=='retry-failed': store.reset('failed',stages,selected)
                elif args.command=='recheck-not-found': store.reset('not_found',stages,selected)
                elif args.command=='refresh-discovery': store.refresh('discovery',selected)
                elif args.command=='refresh-references': store.refresh('references',selected)
                result=store.status_counts()
    print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=='__main__': main()
