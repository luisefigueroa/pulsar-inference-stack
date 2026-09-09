"""Private immutable launch records, indexed by service for observation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.state import Store
from scripts.container_runtime import validate_plan, PLAN_LABEL
from release_spec.serving import load_json


def save(store, plan):
    validate_plan(plan)
    existing = store.get('service-plans', plan['plan_id'])
    if existing is not None and existing != plan:
        raise ValueError('stored launch plan differs from its content digest')
    if existing is None:
        store.put('service-plans', plan['plan_id'], plan, replace=False)
    # An index locates a service; its actual container labels identify the plan.
    store.put('services', plan['service_id'], {'schema_version': 1,
        'service_id': plan['service_id'], 'selected_spec_id': plan['selected_spec_id'], 'plan_id': plan['plan_id']})


def locate(store, *, service_id=None, selected_spec_id=None):
    if service_id:
        row = store.get('services', service_id)
        rows = [row] if row else []
    else:
        rows = [row for row in store.records('services') if row['selected_spec_id'] == selected_spec_id]
    if len(rows) != 1:
        raise ValueError('service not recorded or selector is ambiguous; select an exact service ID (historical services have no complete launch record)')
    plan = store.get('service-plans', rows[0]['plan_id'])
    validate_plan(plan)
    if plan['service_id'] != rows[0]['service_id']:
        raise ValueError('service index differs from saved plan')
    return plan


def actual_plan(store, locator, containers):
    ids = {(container.get('Config', {}).get('Labels') or {}).get(PLAN_LABEL) for container in containers}
    if len(ids) != 1 or None in ids:
        raise ValueError('serving ranks do not share a recorded launch plan')
    plan = store.get('service-plans', ids.pop())
    validate_plan(plan)
    if plan['service_id'] != locator['service_id'] or plan['ranks'] != locator['ranks']:
        raise ValueError('actual service location differs from the recorded selector')
    if len(containers) != len(plan['ranks']):
        raise ValueError('actual service is missing required ranks')
    return plan


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['save','locate','actual'])
    parser.add_argument('--state-root',required=True)
    parser.add_argument('--plan')
    parser.add_argument('--service-id')
    parser.add_argument('--selected-spec-id')
    parser.add_argument('--observations')
    args=parser.parse_args()
    try:
        store=Store(args.state_root)
        if args.command=='save':
            save(store,load_json(args.plan))
            return 0
        if args.command=='locate':
            result=locate(store,service_id=args.service_id,selected_spec_id=args.selected_spec_id)
        else:
            locator=load_json(args.plan)
            containers=[load_json(Path(args.observations)/f'container-{rank}.json') for rank in range(len(locator['ranks']))]
            result=actual_plan(store,locator,containers)
        print(json.dumps(result,sort_keys=True))
        return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(f'error: {exc}',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
