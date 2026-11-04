"""Command-line entry point for the Milestone 2 parser."""
from __future__ import annotations

import argparse
import json

from .pipeline import run_parser


def main() -> None:
    parser = argparse.ArgumentParser(prog='parser', description='Parse Milestone 1 LaTeX sources')
    parser.add_argument('--persistent-root', default='runtime',
                        help='Milestone 1 runtime root containing data/ (read-only)')
    parser.add_argument('--out', default=None,
                        help='Output directory (default: <persistent-root>/parsed)')
    parser.add_argument('--limit', type=int, default=None,
                        help='Process at most N publications')
    parser.add_argument('--no-resume', action='store_true',
                        help='Discard the parser checkpoint and per-publication shards')
    parser.add_argument('--quiet', action='store_true', help='Do not print per-publication progress')
    args = parser.parse_args()

    log = None if args.quiet else (lambda message: print(message, flush=True))
    manifest = run_parser(args.persistent_root, output_root=args.out, limit=args.limit,
                          resume=not args.no_resume, log=log)
    summary = {key: manifest[key] for key in ('papers_processed', 'versions', 'elements', 'bibtex')}
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
