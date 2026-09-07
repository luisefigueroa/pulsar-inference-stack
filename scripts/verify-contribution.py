#!/usr/bin/env python3
"""Verify a compact catalog contribution independently of the workbench."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from release_spec.contribution import CLAIM_STATUSES, verify_contribution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True)
    parser.add_argument('--evidence-root', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        result = verify_contribution(args.spec, args.evidence_root, args.run)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        from release_spec import load_spec
        spec = load_spec(args.spec)
        if spec['review']['status'] in CLAIM_STATUSES:
            print('Contribution verified: all six baseline-v1 criteria pass.')
        else:
            print('Contribution verified: released spec and declared evidence bind.')
        print('Review status does not authorize or block serving. Physical execution still requires maintainer review.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
