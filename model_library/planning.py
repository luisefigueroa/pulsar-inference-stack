"""Shared action rules for one snapshot and every known prepared view.

All ranks must be observed before destructive action. This planner never
interprets a missing observation as absence and never modifies files.
"""
from __future__ import annotations

from .integrity import StorageError
from .state import checked_id, validate_home, validate_view


from release_spec.serving import identity_fields


def require_observations(node_ids: list[str], observations: list[dict]) -> dict[str, dict]:
    if len(node_ids) != len(set(node_ids)) or not node_ids:
        raise StorageError('operation requires unique confirmed nodes')
    result = {}
    for item in observations:
        key = item.get('node_id')
        if key not in node_ids or key in result:
            raise StorageError('unexpected or duplicate node observation')
        if item.get('observable') is not True or not isinstance(item.get('containers'), list):
            raise StorageError('a required node or its containers are unobservable')
        result[key] = item
    if set(result) != set(node_ids):
        raise StorageError('not every required node was observed')
    return result


def container_references(record: dict, observations: dict[str, dict]) -> list[dict]:
    node = observations.get(record['node_id'])
    if node is None:
        raise StorageError('managed location is outside current observable membership')
    references = []
    for container in node['containers']:
        mounts = container.get('mounts')
        if not isinstance(mounts, list):
            raise StorageError('container mounts are unobservable')
        if any(p == record['hub_path'] or p == record['path'] or
               record['path'].startswith(p.rstrip('/') + '/') or
               p.startswith(record['hub_path'].rstrip('/') + '/') for p in mounts):
            references.append(container)
    return references


def removal_plan(*, home: dict, views: list[dict], node_ids: list[str], observations: list[dict],
                 published: bool, archive_verified: bool, discard_unpromoted: bool = False) -> dict:
    validate_home(home)
    checked = require_observations(node_ids, observations)
    dependencies = [validate_view(v) for v in views if v['snapshot_manifest_id'] == home['snapshot_manifest_id']]
    blockers = []
    if dependencies:
        blockers.append('prepared copies still depend on this home; explicitly purge them first')
    if container_references(home, checked):
        blockers.append('a container still references the home, including stopped containers')
    if published and not archive_verified:
        blockers.append('catalog home removal requires a fully verified recovery archive')
    if not published and not archive_verified and not discard_unpromoted:
        blockers.append('unarchived lab model requires explicit discard-unpromoted')
    if published and discard_unpromoted:
        blockers.append('a catalog snapshot cannot be discarded as an unpromoted experiment')
    return {'kind': 'pulsar-home-removal-plan', 'snapshot_manifest_id': home['snapshot_manifest_id'],
            'eligible': not blockers, 'blockers': blockers, 'home': home,
            'dependent_spec_ids': sorted({v['spec_id'] for v in dependencies})}


def purge_plan(*, views: list[dict], node_ids: list[str], observations: list[dict]) -> dict:
    checked = require_observations(node_ids, observations)
    blockers = []
    for view in views:
        validate_view(view)
        if view['pinned']:
            blockers.append(f"prepared copy for {view['spec_id']} is pinned; explicitly unpin first")
        if container_references(view, checked):
            blockers.append(f"a container references the prepared copy for {view['spec_id']}")
    return {'kind': 'pulsar-purge-plan', 'eligible': not blockers, 'blockers': blockers, 'views': views}


def preparation_plan(*, spec: dict, home: dict, node_ids: list[str], topology_id: str,
                     observations: list[dict], views: list[dict], budgets: dict[str, dict]) -> dict:
    from release_spec import verify_spec
    spec = verify_spec(spec)
    validate_home(home)
    manifest = identity_fields(spec)['snapshot_manifest']
    if home['snapshot_manifest_id'] != manifest['manifest_id']:
        raise StorageError('home and spec manifests differ')
    if len(node_ids) != identity_fields(spec)['geometry']['nodes'] or home['node_id'] not in node_ids:
        raise StorageError('home must be on one of the exact serving nodes')
    checked = require_observations(node_ids, observations)
    blockers = []
    previous = {v['node_id']: validate_view(v) for v in views if v['spec_id'] == spec['spec_id']}
    actions = []
    for rank, node_id in enumerate(node_ids):
        old = previous.get(node_id)
        obs = checked[node_id]
        source_matches = bool(old and (
            (node_id == home['node_id'] and old['is_home_view'] and old['path'] == home['path'] and old['hub_path'] == home['hub_path'])
            or (node_id != home['node_id'] and not old['is_home_view'])))
        view_ready = bool(source_matches and old['topology_id'] == topology_id and
                          old['snapshot_manifest_id'] == manifest['manifest_id'] and
                          obs.get('view_verified') is True)
        if view_ready:
            action = 'reuse'
        elif node_id == home['node_id']:
            action = 'home-view'
        else:
            action = 'copy'
        if old and not view_ready:
            if old['pinned'] or container_references(old, checked):
                blockers.append(f'{node_id}: existing prepared copy is pinned or referenced')
            else:
                blockers.append(f'{node_id}: explicitly purge the changed prepared copy before preparing')
        budget = budgets.get(node_id)
        if action == 'copy':
            if not isinstance(budget, dict) or any(not isinstance(budget.get(k), int) or isinstance(budget[k], bool) or budget[k] < 0 for k in ('available', 'reserve', 'used', 'limit')):
                raise StorageError('every copy target requires an observable storage budget')
            if budget['available'] - budget['reserve'] < manifest['total_bytes'] or budget['used'] + manifest['total_bytes'] > budget['limit']:
                blockers.append(f'{node_id}: insufficient copy budget or disk space')
        actions.append({'rank': rank, 'node_id': node_id, 'action': action})
    return {'kind': 'pulsar-preparation-plan', 'spec_id': spec['spec_id'],
            'snapshot_manifest_id': manifest['manifest_id'], 'topology_id': topology_id,
            'eligible': not blockers, 'blockers': blockers, 'actions': actions}
