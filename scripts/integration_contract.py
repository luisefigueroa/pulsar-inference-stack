#!/usr/bin/env python3
"""Describe the supported public stack/workbench integration surface."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from release_spec.serving import SPEC_SCHEMA_VERSION, DRAFT_SCHEMA_VERSION
from release_spec.contribution import APPROVED_POLICY_DIGEST
from scripts.terminal_format import TerminalWriter


def contract() -> dict:
    return {
        'schema_version': 2,
        'kind': 'pulsar-stack-integration-contract',
        'cli_contract_versions': [1],
        'draft_schema_versions': [DRAFT_SCHEMA_VERSION],
        'spec_schema_versions': [SPEC_SCHEMA_VERSION],
        'historical_spec_schema_versions': [1],
        'observation_schema_versions': [2],
        'measurement_schema_versions': [1],
        'run_record_schema_versions': [3],
        # Only completed, tested operations are advertised during the rollout.
        'operations': ['contract', 'spec.example', 'spec.freeze', 'spec.verify', 'spec.show', 'spec.compare',
                       'policy.show', 'evidence.measurement', 'evidence.evaluate', 'evidence.verify',
                       'evidence.summary', 'contribution.verify', 'privacy.check', 'privacy.commits',
                       'selftest', 'start', 'observe', 'resources', 'status', 'stop',
                       'model.acquire', 'model.prepare', 'model.info', 'model.restore', 'model.archive.verify'],
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
        'diagnostics': ['verify-evidence', 'check-launch-compatibility'],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    document = contract()
    if args.json:
        print(json.dumps({'schema_version': 1, 'ok': True, 'result': document}, sort_keys=True))
        return 0
    out = TerminalWriter()
    out.emit('Stack integration contract')
    out.field('Spec schema', str(document['spec_schema_versions'][0]), indent=2)
    out.field('CLI contract', str(document['cli_contract_versions'][0]), indent=2)
    out.field('Catalog authority', document['catalog']['authority'], indent=2)
    out.emit('State, review, evidence, and launch compatibility do not gate catalog membership.',
             initial_indent='  ', subsequent_indent='  ')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
