"""Closed public qualification summaries shared with the private workbench.

These documents bind a compact contribution and the archive proof observed at
export. They do not establish present archive availability or physical execution.
"""
from typing import Any
from .schema import fail
from .run_record import _time


def archive_proof(proof: Any, spec: dict) -> dict:
    manifest = spec['identity']['snapshot_manifest']
    expected = {'schema_version': 1, 'kind': 'pulsar-archive-verification',
                'snapshot_manifest_id': manifest['manifest_id'], 'verified': True,
                'file_count': manifest['file_count'], 'total_bytes': manifest['total_bytes']}
    if (not isinstance(proof, dict) or proof != expected
            or any(type(proof[key]) is not int for key in ('schema_version', 'file_count', 'total_bytes'))
            or proof['verified'] is not True):
        fail('archive verification did not prove the exact candidate snapshot')
    return expected


def summary_document(spec: dict, record: dict, proof: Any, exported_at: str) -> dict:
    _time(exported_at, 'summary export time')
    return {'schema_version': 1, 'kind': 'pulsar-qualification-summary',
            'spec_id': spec['spec_id'], 'suite': 'baseline-v1', 'status': 'pass',
            'exported_at': exported_at,
            'criteria': sorted(row['criterion_id'] for row in spec['measurements']),
            'policy_digest': record['policy_digest'], 'lab_commit': record['lab_commit'],
            'stack_commit': record['stack_commit'],
            'evidence_sha256': {row['id']: row['sha256'] for row in spec['evidence']},
            'archive_verified_at_export': True, 'archive_verification': archive_proof(proof, spec)}


def verify_summary(summary: Any, spec: dict, record: dict) -> dict:
    if not isinstance(summary, dict):
        fail('qualification summary must be a closed document')
    expected = summary_document(spec, record, summary.get('archive_verification'), summary.get('exported_at'))
    if (summary != expected or type(summary.get('schema_version')) is not int
            or summary.get('archive_verified_at_export') is not True):
        fail('qualification summary differs from the exact spec, evidence or archive verification')
    return expected
