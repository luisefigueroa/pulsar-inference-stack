#!/usr/bin/env python3
"""Verify the schema and filename of a maintainer-selected catalog spec."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from release_spec.contribution import verify_contribution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True)
    # Retained for compatibility with the current workbench publisher. Evidence
    # is assessed separately and never gates catalog membership.
    parser.add_argument('--evidence-root')
    parser.add_argument('--run')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        result = verify_contribution(args.spec, args.evidence_root, args.run)
        expected_name = f"{result['spec_id']}.json"
        if Path(args.spec).name != expected_name:
            raise ValueError(f'catalog filename must be {expected_name}')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print('Catalog spec verified: schema and filename match.')
        print('State, review, evidence, and launch compatibility were not used as catalog gates.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
