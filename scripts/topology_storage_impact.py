#!/usr/bin/env python3
"""Report managed storage records affected by a proposed topology change."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from model_library.state import Store, validate_home, validate_view
from scripts.terminal_format import TerminalWriter
import topology_manifest


def assess(old: dict | None, new: dict, store: Store) -> dict:
    old_nodes = {row['node_id'] for row in (old or {}).get('nodes', [])}
    new_nodes = {row['node_id'] for row in new['nodes']}
    removed = sorted(old_nodes - new_nodes)
    homes = [validate_home(row) for row in store.records('homes')]
    views = [validate_view(row) for row in store.records('views')]
    topology_changed = bool(old and old.get('topology_id') != new.get('topology_id'))
    affected_homes = sorted(row['node_id'] for row in homes if row['node_id'] in removed)
    stale_views = [
        row for row in views
        if row['node_id'] not in new_nodes
        or row['topology_id'] != new.get('topology_id')
    ]
    return {
        'schema_version': 1,
        'kind': 'pulsar-topology-storage-impact',
        'topology_changed': topology_changed,
        'removed_node_ids': removed,
        'homes_on_removed_nodes': affected_homes,
        'prepared_views_requiring_reconciliation': len(stale_views),
        'pinned_views_requiring_reconciliation': sum(
            1 for row in stale_views if row['pinned']),
    }


def load_topology(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    value = topology_manifest.extract_topology(topology_manifest.load_json(path))
    topology_manifest.validate_manifest(value, require_verified=True)
    return value


def render(document: dict) -> None:
    out = TerminalWriter()
    out.emit('Managed storage impact')
    removed = document['removed_node_ids']
    out.field('Removed nodes', ', '.join(removed) if removed else 'none', indent=2)
    out.field('Homes on removed nodes',
              str(len(document['homes_on_removed_nodes'])), indent=2)
    out.field('Prepared views to reconcile',
              str(document['prepared_views_requiring_reconciliation']), indent=2)
    out.field('Pinned views affected',
              str(document['pinned_views_requiring_reconciliation']), indent=2)
    if document['prepared_views_requiring_reconciliation']:
        out.emit('Prepare records bind the previous topology. Purge or reprepare them explicitly; saving membership does not delete files or records.',
                 initial_indent='  ', subsequent_indent='  ')
    if document['homes_on_removed_nodes']:
        out.emit('Move the home before saving when possible, or plan an explicit archive restore afterward.',
                 initial_indent='  ', subsequent_indent='  ')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old', type=Path)
    parser.add_argument('--new', type=Path, required=True)
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        old = load_topology(args.old)
        new = load_topology(args.new)
        if new is None:
            raise ValueError('proposed topology is missing')
        document = assess(old, new, Store(args.state_root))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'topology storage impact: {exc}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(document, sort_keys=True))
    else:
        render(document)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
