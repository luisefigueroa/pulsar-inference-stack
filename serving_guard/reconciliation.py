"""Authenticate completed cleanup before retiring one absent service locator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets

from model_library.integrity import atomic_json, directory
from model_library.state import Store, checked_id, ensure_directory, now
from model_library.verification_process import process_identity
from release_spec import serving
from release_spec.normalize import canonical_json_digest
from scripts.container_runtime import validate_plan
from scripts.service_state import retire_plan
from serving_guard.controller import read_regular


def record(path):
    # Anchor all components as well as the file; historical inputs are data only.
    with directory(path.parent) as parent:
        return json.loads(read_regular(path.name, dir_fd=parent))


def completed_plan(output, run_id):
    checked_id(run_id)
    spec = serving.verify_spec(record(output / 'spec.json'))
    plan = validate_plan(record(output / 'active-plan.json'))
    original = validate_plan(record(output / 'plan.json'))
    expected = {**original, 'lifecycle_action': 'start'}
    expected['plan_id'] = canonical_json_digest({k: v for k, v in expected.items() if k != 'plan_id'})
    if (original['lifecycle_action'] != 'dry-run' or plan != expected or plan.get('guard_run_id') != run_id
            or plan['spec'] != spec or plan['selected_spec'] != spec):
        raise ValueError('saved guarded spec, plan or invocation differs')
    context = record(output / 'context.json')
    if context['plan'] != plan or context['ranks'] != plan['ranks']:
        raise ValueError('saved guarded context differs')
    controller = record(output / 'controller.json')
    owner = controller['owner']
    if (controller['run_id'] != run_id or not isinstance(owner, list) or len(owner) != 2
            or type(owner[0]) is not int or owner[0] <= 0
            or not isinstance(owner[1], str) or not owner[1].isdigit()):
        raise ValueError('saved guarded controller identity differs')
    if process_identity(owner[0]) == owner:
        raise ValueError('guarded controller is still alive; wait for completion')
    result = record(output / 'result.json')
    if (type(result.get('schema_version')) is not int or result['schema_version'] != 1
            or result.get('kind') != 'pulsar-guarded-serving-result'
            or result.get('run_id') != run_id or result.get('service_id') != plan['service_id']
            or result.get('spec_id') != spec['spec_id']
            or result.get('status') not in ('stopped', 'failed')
            or result.get('qualification') is not False):
        raise ValueError('saved guarded result identity differs')
    count = len(plan['ranks'])
    phase = result['phases']['cleanup']
    if (phase != {'complete': True, 'ranks': count} or phase.get('complete') is not True
            or type(phase.get('ranks')) is not int):
        raise ValueError('saved cleanup is incomplete')
    batch = record(output / 'cleanup' / 'batch.json')
    if (batch.get('outcome') != 'complete' or type(batch.get('returncode')) is not int
            or batch['returncode'] != 0 or len(batch['results']) != count):
        raise ValueError('saved cleanup batch is incomplete')
    for rank, row in enumerate(batch['results']):
        value = record(output / 'cleanup' / 'jobs' / f'{rank}.out')
        if (type(row.get('index')) is not int or row['index'] != rank
                or type(row.get('returncode')) is not int or row['returncode'] != 0
                or type(value.get('rank')) is not int or value['rank'] != rank
                or value.get('run_id') != run_id or value.get('spec_id') != spec['spec_id']
                or value.get('cleanup_verified') is not True):
            raise ValueError('saved rank cleanup identity or result differs')
    return plan


def placement(plan, topology_id, members):
    if topology_id != plan['topology_id'] or not members:
        raise ValueError('confirmed guarded topology changed or is missing')
    fields = ('node_id', 'hostname', 'ssh_host', 'control_ip', 'control_if')
    if len(members) % len(fields):
        raise ValueError('confirmed topology mapping is incomplete')
    nodes = [dict(zip(fields, members[i:i + len(fields)]))
             for i in range(0, len(members), len(fields))]
    if len({row['node_id'] for row in nodes}) != len(nodes):
        raise ValueError('confirmed topology repeats a node')
    indexes = []
    for saved in plan['ranks']:
        matches = [index for index, row in enumerate(nodes) if row['node_id'] == saved['node_id']]
        if len(matches) != 1 or any(nodes[matches[0]][field] != saved[field] for field in fields):
            raise ValueError('recorded guarded rank differs from confirmed topology mapping')
        indexes.append(matches[0])
    return indexes


def reconcile(output, run_id, state_root, observed, topology_id, members):
    plan = completed_plan(output, run_id)
    if plan != record(observed / 'plan.json'):
        raise ValueError('saved guarded plan changed during absence probes')
    placement(plan, topology_id, members)
    count = len(plan['ranks'])
    for rank in range(count):
        for selector in ('name', 'run', 'plan', 'spec'):
            path = observed / f'{rank}-{selector}.out'
            with directory(path.parent) as parent:
                if read_regular(path.name, dir_fd=parent).strip():
                    raise ValueError('recorded guarded rank has a container; locator preserved')
    store = Store(state_root)
    saved = store.get('service-plans', plan['plan_id'])
    if saved is not None and saved != plan:
        raise ValueError('stored guarded launch plan differs')
    receipts = ensure_directory(output / 'reconciliations')
    receipt = {'schema_version': 1, 'kind': 'pulsar-guarded-reconciliation',
               'run_id': run_id, 'service_id': plan['service_id'], 'spec_id': plan['spec_id'],
               'plan_id': plan['plan_id'], 'topology_id': topology_id, 'observed_at': now(),
               'ranks': [{'rank': row['rank'], 'node_id': row['node_id'], 'absent': True}
                         for row in plan['ranks']]}
    receipt['service_locator_retired'] = retire_plan(store, plan, require_matching=True)
    receipt['receipt_file'] = str(receipts / (secrets.token_hex(16) + '.json'))
    atomic_json(Path(receipt['receipt_file']), receipt, replace=False)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['validate', 'placement', 'retire'])
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--state-root')
    parser.add_argument('--observed', type=Path)
    parser.add_argument('--topology-id')
    parser.add_argument('--member', action='append', default=[])
    args = parser.parse_args()
    if args.operation == 'validate':
        print(json.dumps(completed_plan(args.output, args.run_id)))
    elif args.operation == 'placement':
        for index in placement(completed_plan(args.output, args.run_id), args.topology_id, args.member):
            print(index)
    else:
        print(json.dumps(reconcile(args.output, args.run_id, args.state_root, args.observed,
                                   args.topology_id, args.member)))


if __name__ == '__main__':
    main()
