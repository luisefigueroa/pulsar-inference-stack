"""Private immutable launch records, indexed by service for observation."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.state import Store, checked_id
from scripts.container_runtime import validate_plan, PLAN_LABEL, PREFIX, SELECTED_SPEC_LABEL, SPEC_LABEL
from release_spec.serving import load_json


class RecordedElsewhere(ValueError):
    """Saving an ordinary multi-node record while another service of the same
    spec is recorded: a spec runs as one service. HOSTS lists each other
    service's nodes, in role order."""

    def __init__(self, hosts):
        super().__init__('a service of this spec is recorded on other nodes')
        self.hosts = hosts


def save(store, plan):
    # Callers may share the lifecycle lock; serialize index compare/write/remove
    # separately, without upgrading the lifecycle lock held by a foreground run.
    with store.lock(name='services.lock'):
        validate_plan(plan)
        # One service per spec, decided here, under the lock every save and
        # retire takes, so two Starts of one spec at once cannot both record
        # one. A guarded trial and a one-node service keep their own rules.
        if not plan.get('guard_run_id') and len(plan['ranks']) > 1:
            others = []
            for row in store.records('services'):
                if row['selected_spec_id'] != plan['selected_spec_id'] or row['service_id'] == plan['service_id']:
                    continue
                other = store.get('service-plans', row['plan_id']) or {}
                others.append([rank.get('hostname') or rank.get('node_id') for rank in other.get('ranks') or []])
            if others:
                raise RecordedElsewhere(others)
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
    if len(rows) > 1:
        raise ValueError(f'several services are recorded for spec {selected_spec_id[:12]}; '
                         'select one with ./pulsar observe --service-id ID')
    if not rows:
        subject = 'with this service ID' if service_id else f'for spec {str(selected_spec_id)[:12]}'
        raise ValueError(f'no running service is recorded {subject} '
                         '(services started before launch records existed are found only by inventory)')
    plan = store.get('service-plans', rows[0]['plan_id'])
    validate_plan(plan)
    if plan['service_id'] != rows[0]['service_id']:
        raise ValueError('service index differs from saved plan')
    return plan


def retire(store, *, topology_id, node_ids, selected_spec_id=None):
    """Retire active locators after proven stop; caller holds the lifecycle lock.

    NODE_IDS are the nodes the stop proved, in saved rank order. The stop
    paths that call this find containers by saved position, so a multi-node
    service recorded off the default node order is never retired here: only
    a stop on its recorded nodes (retire_plan) proves its containers gone."""
    # Callers may share the lifecycle lock; serialize index compare/write/remove
    # separately, without upgrading the lifecycle lock held by a foreground run.
    with store.lock(name='services.lock'):
        checked_id(topology_id)
        if selected_spec_id is not None: checked_id(selected_spec_id)
        if not node_ids: raise ValueError('stop must identify its confirmed nodes')
        retired=[]
        for row in store.records('services'):
            if selected_spec_id is not None and row['selected_spec_id']!=selected_spec_id:
                continue
            plan=store.get('service-plans',row['plan_id'])
            validate_plan(plan)
            if plan['service_id']!=row['service_id']:
                raise ValueError('service index differs from saved plan')
            recorded=[rank['node_id'] for rank in plan['ranks']]
            if (plan['topology_id']==topology_id and set(recorded)<=set(node_ids)
                    and not off_default_order(recorded,node_ids)):
                store.remove('services',row['service_id'])
                retired.append(row['service_id'])
        return retired


def retire_plan(store, plan, *, require_matching=False, retirement_record=None):
    """Retire this plan after cleanup; reconciliation refuses a different locator."""
    validate_plan(plan)
    with store.lock(name='services.lock'):
        row = store.get('services', plan['service_id'])
        if row is None:
            if retirement_record is not None:
                with retirement_record(False):
                    pass
            return False
        if row['plan_id'] != plan['plan_id']:
            if require_matching:
                raise ValueError('active service locator selects a different launch plan; preserved')
            return False
        if require_matching and (type(row.get('schema_version')) is not int or row != {
                'schema_version': 1, 'service_id': plan['service_id'],
                'selected_spec_id': plan['selected_spec_id'], 'plan_id': plan['plan_id']}):
            raise ValueError('active service locator differs from saved guarded plan; preserved')
        saved = store.get('service-plans', plan['plan_id'])
        if saved != plan or row['service_id'] != plan['service_id']:
            raise ValueError('service index differs from saved plan')
        # A reconciliation receipt must be durable before removal, and remain
        # truthful if removal or its final record fails. Keep the caller's
        # receipt context inside the same compare/remove lock.
        with retirement_record(True) if retirement_record is not None else nullcontext():
            store.remove('services', plan['service_id'])
        return True


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


def binding(plan):
    """What the ownership test reads from a saved launch plan: its identity and
    the node each role rank runs on."""
    return {'plan_id': plan['plan_id'], 'service_id': plan['service_id'],
            'selected_spec_id': plan['selected_spec_id'], 'spec_id': plan['spec_id'],
            'container_name': plan['container_name'], 'topology_id': plan['topology_id'],
            'guarded': bool(plan.get('guard_run_id')), 'node_ids': [rank['node_id'] for rank in plan['ranks']]}


def _recorded(store, row):
    plan = store.get('service-plans', row['plan_id'])
    validate_plan(plan)
    if plan['service_id'] != row['service_id']:
        raise ValueError('service index differs from saved plan')
    return binding(plan)


def recorded_bindings(store, *, skip_invalid=False):
    """The binding of every active service record. A retired record's plan is
    kept as history and proves nothing about a container. SKIP_INVALID leaves
    out a record whose plan does not validate, which then proves nothing."""
    result = []
    for row in store.records('services'):
        try:
            result.append(_recorded(store, row))
        except (ValueError, KeyError, TypeError):
            if not skip_invalid:
                raise
    return result


def recorded_binding(store, plan_id):
    """The binding of the active record of launch plan PLAN_ID, or None. Only
    that record's plan is read, so another spec's record cannot block it."""
    for row in store.records('services'):
        if plan_id and row.get('plan_id') == plan_id:
            return _recorded(store, row)
    return None


def off_default_order(node_ids, confirmed):
    """Whether a multi-node service runs on other nodes than the first
    len(node_ids) saved ranks in order, where an ordinary start puts it."""
    return len(node_ids) > 1 and list(node_ids) != list(confirmed[:len(node_ids)])


def recorded_ownership(container, *, position, topology_id, node_ids, bindings):
    """May this container be removed as part of an ordinary service recorded
    off the default node order?

    CONTAINER has `name` and `labels`; POSITION is the saved rank of the node
    it was observed on; TOPOLOGY_ID and NODE_IDS (in saved rank order) are the
    saved cluster; BINDINGS are the active records (recorded_bindings).

    Returns None when no such record names the container's launch plan: the
    caller then judges it by saved position, as for every other container.
    Otherwise (True, '') when it is that service's own container, or
    (False, REASON). Guarded trials keep their own stop path and are None.
    """
    labels = container.get('labels') or {}
    plan_id = labels.get(PLAN_LABEL)
    record = next((item for item in bindings if plan_id and item['plan_id'] == plan_id), None)
    if record is None or record['guarded'] or not off_default_order(record['node_ids'], node_ids):
        return None
    def refused(detail):
        return False, f'does not match its saved service record: {detail}'
    if labels.get(PREFIX + 'managed') != 'true':
        return refused("its managed label is not exactly 'true'")
    if record['topology_id'] != topology_id:
        return refused('the record was saved under another saved cluster')
    if labels.get(PREFIX + 'topology') != topology_id:
        return refused('its topology label names another saved cluster')
    if str(container.get('name') or '').lstrip('/') != record['container_name']:
        return refused(f"its name is not {record['container_name']}")
    if (labels.get(PREFIX + 'conf'), labels.get(SELECTED_SPEC_LABEL), labels.get(SPEC_LABEL)) != (
            record['selected_spec_id'], record['selected_spec_id'], record['spec_id']):
        return refused('its spec labels name another spec')
    rank = labels.get(PREFIX + 'rank')
    roles = [str(role) for role in range(len(record['node_ids']))]
    if rank not in roles:
        return refused(f"its rank label '{rank}' is not a rank of the record")
    node = record['node_ids'][int(rank)]
    if labels.get(PREFIX + 'node-id') != node:
        return refused(f'its node-id label is not the node the record names for rank {rank}')
    if type(position) is not int or not 0 <= position < len(node_ids) or node_ids[position] != node:
        return refused(f'it runs on another node than the record names for rank {rank}')
    return True, ''


def recorded_elsewhere(store, *, selected_spec_id, topology_id, node_ids):
    """The other services of SELECTED_SPEC_ID that active records hold: each
    as the hostnames of its nodes in role order. A Start on NODE_IDS (in role
    order, under TOPOLOGY_ID) would be its own service, so a record of the
    same service is not listed; one of an earlier saved cluster is."""
    from scripts.container_runtime import service_identifier
    own = service_identifier(selected_spec_id, topology_id, list(node_ids))
    result = []
    for row in store.records('services'):
        if row['selected_spec_id'] != selected_spec_id or row['service_id'] == own:
            continue
        plan = store.get('service-plans', row['plan_id'])
        validate_plan(plan)
        result.append([rank.get('hostname') or rank['node_id'] for rank in plan['ranks']])
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['save','locate','actual','retire','bindings','ownership','elsewhere'])
    parser.add_argument('--state-root',required=True)
    parser.add_argument('--plan')
    parser.add_argument('--service-id')
    parser.add_argument('--selected-spec-id')
    parser.add_argument('--observations')
    parser.add_argument('--topology-id')
    parser.add_argument('--node-id',action='append',default=[])
    parser.add_argument('--position',type=int)
    args=parser.parse_args()
    try:
        store=Store(args.state_root)
        if args.command=='save':
            try:
                save(store,load_json(args.plan))
            except RecordedElsewhere as exc:
                # Exit 3, one line per other service of the spec: its nodes, comma-separated.
                for hosts in exc.hosts:
                    print(', '.join(str(host) for host in hosts))
                print(f'error: {exc}',file=sys.stderr)
                return 3
            return 0
        if args.command=='ownership':
            # The container's ownership inspect JSON on stdin: one line out,
            # "none", "owned", or "refused<TAB>REASON".
            container=json.load(sys.stdin)
            record=recorded_binding(store,(container.get('labels') or {}).get(PLAN_LABEL))
            verdict=recorded_ownership(container,position=args.position,topology_id=args.topology_id,
                                       node_ids=args.node_id,bindings=[record] if record else [])
            print('none' if verdict is None else 'owned' if verdict[0] else f'refused\t{verdict[1]}')
            return 0
        if args.command=='retire':
            result={'retired_service_ids':retire(store,topology_id=args.topology_id,
                node_ids=args.node_id,selected_spec_id=args.selected_spec_id)}
        elif args.command=='elsewhere':
            # One line per other service of the spec: its nodes, comma-separated.
            for hosts in recorded_elsewhere(store,selected_spec_id=args.selected_spec_id,
                                            topology_id=args.topology_id,node_ids=args.node_id):
                print(', '.join(hosts))
            return 0
        elif args.command=='bindings':
            # For inventory: a record that does not validate proves nothing,
            # so its containers are judged by saved position.
            result=recorded_bindings(store,skip_invalid=True)
        elif args.command=='locate':
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
