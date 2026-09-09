#!/usr/bin/env python3
"""Verify optional compact evidence without deciding catalog membership."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

STACK_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STACK_ROOT))

from release_spec.contribution import verify_compact_evidence
from release_spec.measurement import parse_strict_json, read_stable_bytes
from release_spec.summary import verify_summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True)
    parser.add_argument('--evidence-root', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--summary')
    parser.add_argument('--require-pass', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = verify_compact_evidence(
            args.spec, args.evidence_root, args.run,
            require_pass=args.require_pass,
        )
        if args.summary:
            from release_spec import load_spec
            spec = load_spec(args.spec)
            run = parse_strict_json(
                read_stable_bytes(args.run, label='qualification run'),
                label='qualification run',
            )
            summary = parse_strict_json(
                read_stable_bytes(args.summary, label='qualification summary'),
                label='qualification summary',
            )
            verify_summary(summary, spec, run)
            result['summary_verified'] = True
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'evidence verification: {exc}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print('Compact evidence verified independently of catalog membership.')
        if result.get('unverified_suites'):
            print('Not assessed by this command: ' + ', '.join(result['unverified_suites']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
