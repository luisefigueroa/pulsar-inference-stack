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
from scripts.start_checks import CHECK_IDS
from scripts.terminal_format import TerminalWriter

# The operations that do not print the result envelope, each with its output
# form and kind. An operation not named here prints the envelope under --json.
# With the form document, a command prints its own JSON document under --json;
# the kind here is the kind that document carries, or None when it carries
# none. The tests that run each of these commands on fixtures compare what it
# prints with this table.
NON_ENVELOPE_OUTPUTS = {
    'logs': ('text', None),  # Text for people; it has no --json.
    'resources': ('stream', None),  # A stream of JSON lines (--jsonl).
    'models.list': ('document', 'pulsar-model-catalog'),
    'models.show': ('document', 'pulsar-model-catalog'),
    'inventory': ('document', None),
    'doctor': ('document', 'pulsar-doctor'),
    'topology.show': ('document', 'pulsar-topology-observation'),
    'topology.check': ('document', 'pulsar-topology-observation'),
    'topology.detect': ('document', None),
    'ssh-trust.check': ('document', 'topology-ssh-trust-check'),
    'configure.archive-root.show': ('document', 'pulsar-archive-root-configuration'),
}


def operation_outputs(operations) -> dict:
    """The output form of every operation, with the kind of the document it prints.

    Built from the operation list, so the two cannot differ: every operation
    gets an entry, and a form given to a name that is not an operation fails.
    """
    stray = sorted(set(NON_ENVELOPE_OUTPUTS) - set(operations))
    if stray:
        raise ValueError(f"an output form is given to {', '.join(stray)}, which is not an operation")
    return {operation: dict(zip(('form', 'kind'), NON_ENVELOPE_OUTPUTS.get(operation, ('envelope', None))))
            for operation in operations}


def contract() -> dict:
    document = {
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
                       'selftest', 'start', 'observe', 'resources', 'status', 'stop', 'logs', 'memory.verify',
                       'model.acquire', 'model.prepare', 'model.info', 'model.restore', 'model.archive.verify',
                       'guarded.template', 'guarded.validate', 'guarded.run', 'guarded.stop', 'guarded.reconcile',
                       'image.check', 'image.stage', 'models.list', 'models.show', 'model.check', 'model.budget',
                       'model.files', 'inventory', 'doctor', 'topology.show', 'topology.check', 'topology.detect',
                       'ssh-trust.check', 'configure.archive-root.show'],
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
        # start --json lists its checks with these ids, in this order.
        'start_check_ids': list(CHECK_IDS),
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
            'start --yes': {'note': 'start asks no confirmation; --replace, --pull-image and '
                                    '--accept-memory-warn grant its permissions',
                            'removed_in_cli_contract': 2},
            'stop --retain-weights': {'note': 'stop always retains model files', 'removed_in_cli_contract': 2},
        },
    }
    document['operation_outputs'] = operation_outputs(document['operations'])
    return document


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='pulsar contract', description=__doc__)
    parser.add_argument('--json', action='store_true', help='print the contract document as JSON')
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
