#!/usr/bin/env python3
"""Internal adapter for freezing the public memory-estimate input."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release_spec import memory_estimate, serving


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-file", required=True)
    parser.add_argument("--override-file")
    parser.add_argument("--effective-spec-id", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file")
    source.add_argument("--frozen-json")
    parser.add_argument("--estimate-id")
    args = parser.parse_args(argv)
    try:
        spec = serving.load_spec(args.spec_file)
        if args.override_file:
            spec = serving.apply_overrides(spec, serving.load_json(args.override_file))
        if spec["spec_id"] != args.effective_spec_id:
            raise ValueError("effective spec changed before selecting the memory estimate")
        if args.file:
            frozen = memory_estimate.load(args.file, spec, expected_id=args.estimate_id)
        else:
            frozen = memory_estimate.validate_frozen(
                memory_estimate.parse(args.frozen_json.encode()), spec,
                expected_id=args.estimate_id)
        print(json.dumps(frozen, sort_keys=True, separators=(",", ":")))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(f"memory estimate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
