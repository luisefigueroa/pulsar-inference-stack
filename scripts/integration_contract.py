#!/usr/bin/env python3
"""Describe the supported public stack/workbench integration surface."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from release_spec.serving import SUPPORTED_SPEC_SCHEMAS
from release_spec.contribution import APPROVED_POLICY_DIGEST
from release_spec.baseline_policy import SUPPORTED_POLICY_DIGESTS
from scripts.document_cli import ERROR_CODES, EXIT_STATUSES
from scripts.start_blockers import BLOCKERS
from scripts.terminal_format import TerminalWriter


def contract() -> dict:
    return {
        'schema_version': 2,
        'kind': 'pulsar-stack-integration-contract',
        'cli_contract_versions': [1],
        'draft_schema_versions': [1, 2],
        'spec_schema_versions': list(SUPPORTED_SPEC_SCHEMAS),
        'historical_spec_schema_versions': [1],
        'observation_schema_versions': [2, 3],
        'measurement_schema_versions': [1, 2],
        'run_record_schema_versions': [3, 4],
        'memory_estimate_schema_versions': [1],
        'serving_guard_schema_versions': [1, 2],
        # Only completed, tested operations are advertised during the rollout.
        'operations': ['contract', 'spec.example', 'spec.freeze', 'spec.verify', 'spec.show', 'spec.compare',
                       'policy.show', 'evidence.measurement', 'evidence.evaluate', 'evidence.verify',
                       'evidence.summary', 'contribution.verify', 'privacy.check', 'privacy.commits',
                       'selftest', 'start', 'observe', 'resources', 'status', 'stop', 'memory.verify',
                       'model.acquire', 'model.prepare', 'model.info', 'model.restore', 'model.archive.verify',
                       'guarded.template', 'guarded.validate', 'guarded.run', 'guarded.stop'],
        'baseline_policy_digest': APPROVED_POLICY_DIGEST,  # Legacy baseline-v1 field.
        'baseline_policies': dict(SUPPORTED_POLICY_DIGESTS),
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
        # Envelope error codes and --json exit statuses. Callers branch on
        # ok and error.code, never on message text; an unknown code is a failure.
        'error_codes': {code: {'exit_status': status, 'meaning': meaning}
                        for code, (status, meaning) in ERROR_CODES.items()},
        'exit_statuses': dict(EXIT_STATUSES),
        # start --json failures list these in error.details as "blocker".
        'start_blocker_codes': sorted(BLOCKERS),
        # Aliases that warn on stderr and are removed in CLI contract 2.
        'deprecated_commands': {
            'gum': {'replacement': 'pulsar', 'removed_in_cli_contract': 2},
            'release list': {'replacement': 'pulsar models list', 'removed_in_cli_contract': 2},
            'wizard': {'replacement': 'pulsar models', 'removed_in_cli_contract': 2},
        },
        # Flags that still parse, warn on stderr, change nothing and are removed
        # in CLI contract 2.
        'deprecated_flags': {
            'observe --spec-file': {'note': 'it is ignored because the recorded spec is authoritative',
                                    'removed_in_cli_contract': 2},
            'stop --retain-weights': {'note': 'stop always retains model files', 'removed_in_cli_contract': 2},
        },
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
    out.field('Spec schema', ', '.join(map(str, document['spec_schema_versions'])), indent=2)
    out.field('CLI contract', str(document['cli_contract_versions'][0]), indent=2)
    out.field('Catalog authority', document['catalog']['authority'], indent=2)
    out.emit('State, review, evidence, and launch compatibility do not gate catalog membership.',
             initial_indent='  ', subsequent_indent='  ')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
