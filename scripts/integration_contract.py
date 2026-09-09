#!/usr/bin/env python3
"""Describe the supported public stack/workbench integration surface."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from release_spec import SCHEMA_VERSION
from release_spec.contribution import APPROVED_POLICY_DIGEST
from release_spec.run_record import RUN_SCHEMA_VERSION
from scripts.launch_plan import PLAN_SCHEMA_VERSION
from scripts.terminal_format import TerminalWriter


def contract() -> dict:
    return {
        'schema_version': 1,
        'kind': 'pulsar-stack-integration-contract',
        'release_spec_versions': [SCHEMA_VERSION],
        'run_record_versions': [RUN_SCHEMA_VERSION],
        'launch_plan_versions': [PLAN_SCHEMA_VERSION],
        'baseline_policy_digest': APPROVED_POLICY_DIGEST,
        'catalog': {
            'authority': 'workbench-maintainer',
            'required_checks': ['spec-schema', 'filename-spec-id', 'privacy'],
            'state_gate': False,
            'review_gate': False,
            'evidence_gate': False,
            'launch_compatibility_gate': False,
            'nullable_state': True,
            'nullable_review': True,
        },
        'recipe_projector': 'release_spec.build_profile_identity',
        'diagnostics': ['verify-evidence', 'check-launch-compatibility'],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    document = contract()
    if args.json:
        print(json.dumps(document, sort_keys=True))
        return 0
    out = TerminalWriter()
    out.emit('Stack integration contract')
    out.field('Release spec', str(document['release_spec_versions'][0]), indent=2)
    out.field('Run record', str(document['run_record_versions'][0]), indent=2)
    out.field('Launch plan', str(document['launch_plan_versions'][0]), indent=2)
    out.field('Catalog authority', document['catalog']['authority'], indent=2)
    out.emit('State, review, evidence, and launch compatibility do not gate catalog membership.',
             initial_indent='  ', subsequent_indent='  ')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
