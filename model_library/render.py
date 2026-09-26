"""Human-readable model storage results. Rendering never performs an operation."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from scripts.terminal_format import TerminalWriter
from .node_names import NodeNames


OPERATIONS = ('acquire', 'prepare', 'info', 'archive', 'restore', 'move', 'pin',
              'unpin', 'purge', 'remove', 'check', 'budget')


def bytes_text(value: Any) -> str:
    if type(value) is not int or value < 0:
        return 'not observed'
    if value < 1024:
        return f'{value:,} bytes'
    for divisor, unit in ((1024**4, 'TiB'), (1024**3, 'GiB'), (1024**2, 'MiB'), (1024, 'KiB')):
        if value >= divisor:
            return f'{value/divisor:.2f} {unit} ({value:,} bytes)'
    return f'{value:,} bytes'


def _identity(out: TerminalWriter, document: dict) -> None:
    for key, label in (('model_id', 'Model'), ('spec_id', 'Spec'),
                       ('snapshot_revision', 'Commit'), ('revision', 'Commit'),
                       ('snapshot_manifest_id', 'Snapshot'), ('manifest_id', 'Snapshot')):
        if document.get(key):
            out.field(label, document[key])


def _files(out: TerminalWriter, document: dict) -> None:
    files = document.get('files')
    count = document.get('file_count')
    total = document.get('total_bytes')
    if isinstance(files, list):
        count = len(files)
        sizes = [item.get('size') for item in files if isinstance(item, dict)]
        if len(sizes) == count and all(type(size) is int and size >= 0 for size in sizes):
            total = sum(sizes)
    if type(count) is int:
        out.field('Files', f'{count:,} files; {bytes_text(total)}')
    elif total is not None:
        out.field('Size', bytes_text(total))


def _home(out: TerminalWriter, home: Any, names: NodeNames) -> None:
    if not isinstance(home, dict):
        out.field('Home', 'no location recorded')
        return
    if home.get('node_id'):
        out.field('Home node', names(home['node_id']))
    if home.get('path') or home.get('hub_path'):
        out.field('Home path', home.get('path') or home['hub_path'])
    if home.get('verified_at'):
        out.field('Recorded', home['verified_at'])


def _copy(out: TerminalWriter, row: dict, names: NodeNames) -> None:
    rank = row.get('rank')
    label = f'Rank {rank}' if type(rank) is int else 'Copy'
    out.field(label, names(row['node_id']) if row.get('node_id') else 'node not recorded')
    if 'pinned' in row:
        out.field('Retention', 'pinned' if row['pinned'] is True else 'not pinned', indent=2)
    if row.get('path') or row.get('hub_path'):
        out.field('Path', row.get('path') or row['hub_path'], indent=2)


def _blockers(out: TerminalWriter, document: dict, names: NodeNames) -> None:
    for blocker in document.get('blockers') or []:
        if isinstance(blocker, dict):
            reason = blocker.get('reason') or blocker.get('message')
            if reason:
                node = blocker.get('node_id')
                blocker = f'{names(node)}: {reason}' if node else reason
            else:
                blocker = 'Additional storage restriction; inspect --json for details.'
        elif isinstance(blocker, str):
            blocker = names.prefixed(blocker)
        out.field('Blocker', blocker)


def _plan(out: TerminalWriter, document: dict, operation: str, names: NodeNames) -> None:
    titles = {'acquire': 'Acquisition preview', 'prepare': 'Preparation preview',
              'archive': 'Recovery archive preview', 'restore': 'Restoration preview',
              'move': 'Home movement preview', 'pin': 'Pin preview', 'unpin': 'Unpin preview',
              'purge': 'Prepared-copy removal preview', 'remove': 'Home removal preview'}
    out.emit(titles.get(operation, 'Storage operation preview'))
    out.emit('Review this plan before confirming the operation.')
    if document.get('eligible') is False:
        out.field('Result', 'blocked')
    elif document.get('eligible') is True:
        out.field('Result', 'prerequisites passed at this check')
    _identity(out, document)
    if isinstance(document.get('manifest'), dict):
        _identity(out, document['manifest'])
        _files(out, document['manifest'])
    elif isinstance(document.get('source'), dict):
        # Top-level acquisition fields already include model/commit.
        source = document['source']
        if not document.get('model_id'):
            _identity(out, source)
        _files(out, source)
    for key, label in (('selected_node', 'Destination'), ('source_node', 'From node'),
                       ('destination_node', 'To node'), ('archive_root', 'Archive')):
        if document.get(key):
            out.field(label, document[key] if key == 'archive_root' else names(document[key]))
    route = document.get('transfer_route')
    routes = {'direct-ssh-roce': 'Direct copy over the confirmed RoCE rail',
              'controller-stream-relay': 'Stream through controller pipes over confirmed RoCE rails; no controller model copy',
              'already-home': 'Home is already on the selected node'}
    if route:
        out.field('Copy route', routes.get(route, route))
    if 'home' in document:
        _home(out, document['home'], names)
    existing = document.get('existing_homes')
    if isinstance(existing, list):
        out.field('Known homes', len(existing))
        for candidate in existing:
            _home(out, candidate.get('home', candidate), names)
    if document.get('action') == 'reuse':
        out.field('Action', 'reuse matching existing files')
    actions = {'reuse': 'reuse verified prepared files', 'home-view': 'use files from the home',
               'copy': 'copy and verify local files', 'bind': 'bind this recipe to existing verified files',
               'release-binding': 'remove this binding; retain shared files',
               'remove-copy': 'remove this binding and its working-copy files'}
    for item in document.get('actions') or []:
        out.field(f'Rank {item.get("rank", "?")}',
                  f'{names(item["node_id"]) if item.get("node_id") else "unknown node"}: {actions.get(item.get("action"), item.get("action", "unknown action"))}')
    views = document.get('views')
    if isinstance(views, list):
        out.field('Copies', len(views))
        for view in views:
            _copy(out, view, names)
    for spec_id in document.get('dependent_spec_ids') or []:
        out.field('Depends on', spec_id)
    _blockers(out, document, names)
    if operation == 'acquire':
        out.emit('The complete source file list is available with --json.')


def _observation(out: TerminalWriter, document: dict, names: NodeNames) -> None:
    out.emit('Storage check recorded')
    _identity(out, document)
    observation = document['observation']
    local_states = {'ready': 'files prepared at this check', 'missing': 'required files missing',
                    'changed': 'files changed; verification is required', 'unknown': 'not established'}
    archive_states = {'verified': 'contents verified against expected hashes in this check',
                      'present': 'directory present; contents not checked', 'missing': 'not found',
                      'unavailable': 'could not be checked', 'unknown': 'not established',
                      'not-configured': 'recovery storage is not configured'}
    out.field('Local files', local_states.get(observation.get('local_state'), 'not established'))
    out.field('Archive', archive_states.get(observation.get('archive_state'), 'not established'))
    prepared = observation.get('prepared') or {}
    if 'verified' in prepared and 'required' in prepared:
        unit="snapshot/rank checks" if "snapshots" in observation else "required ranks"
        out.field('Prepared', f'{prepared["verified"]} of {prepared["required"]} {unit}')
    if observation.get('checked_at'):
        out.field('Checked', observation['checked_at'])
    _blockers(out, observation, names)
    out.emit('Prepared files and a running service are separate states.')


def render(document: Any, *, operation: str, archive_action: str = '',
           writer: TerminalWriter | None = None, names: NodeNames | None = None) -> None:
    if not isinstance(document, dict):
        raise ValueError('storage result must be a JSON object')
    if operation not in OPERATIONS:
        raise ValueError('unsupported storage operation')
    if archive_action not in ('', 'create', 'verify'):
        raise ValueError('unsupported archive action')
    out = writer or TerminalWriter()
    names = names or NodeNames()
    kind = str(document.get('kind') or '')
    members=document.get('snapshots')
    if isinstance(members,dict):
        out.emit('Complete required snapshot set')
        _identity(out,document)
        for name,member in members.items():
            out.field('Snapshot',name)
            render(member,operation=operation,archive_action=archive_action,writer=out,names=names)
        return
    if kind == 'pulsar-preparation-set-plan':
        _plan(out,document,operation,names)
        for member in document['snapshots']:
            out.field('Snapshot',member['snapshot'])
            _plan(out,member,operation,names)
        return

    if isinstance(document.get('plan'), dict):
        _plan(out, document['plan'], operation, names)
        pending = document.get('incomplete_preparations')
        if isinstance(pending, list) and pending:
            out.field('Incomplete preparations', len(pending))
            for row in pending:
                out.field('Pending node', names(row['node_id']) if row.get('node_id') else 'not recorded')
                if row.get('stage'):
                    out.field('Pending path', row['stage'])
        return
    if kind.endswith('-plan') or 'eligible' in document:
        _plan(out, document, operation, names)
        return
    if operation == 'check' and isinstance(document.get('observation'), dict):
        _observation(out, document, names)
        return
    if kind == 'pulsar-prepared-set':
        out.emit('Prepared files checked')
        _identity(out, document)
        _home(out, document.get('home'), names)
        for rank in document.get('ranks') or []:
            _copy(out, rank, names)
        out.emit('Each rank is a serving slot. Start is a separate operation.')
        return
    if 'prepared' in document and type(document['prepared']) is int:
        out.emit('Preparation complete')
        _identity(out, document)
        out.field('Prepared', f'{document["prepared"]} serving ranks')
        out.emit('Start is a separate operation.')
        return
    if kind == 'pulsar-archive-verification' or (operation == 'archive' and 'verified' in document):
        out.emit('Recovery archive verified' if document.get('verified') is True
                 else 'Recovery archive verification not established')
        _identity(out, document)
        _files(out, document)
        if document.get('verified') is True:
            out.emit('Archive contents match the expected file hashes.')
        return
    if isinstance(document.get('home'), dict):
        titles = {'acquire': 'Acquisition complete; home recorded',
                  'restore': 'Restoration complete; home recorded',
                  'move': 'Home location recorded'}
        out.emit(titles.get(operation, 'Home record'))
        if document.get('moved') is False:
            out.field('Movement', 'already on the selected node')
        if document.get('reused') is True:
            out.field('Source', 'verified existing files reused')
        manifest = document.get('manifest')
        _identity(out, manifest if isinstance(manifest, dict) else document['home'])
        if isinstance(manifest, dict):
            _files(out, manifest)
        _home(out, document['home'], names)
        if operation in ('acquire', 'restore'):
            out.emit('Prepare the required local files before starting this recipe.')
        return
    if type(document.get('pinned')) is bool:
        out.emit('Prepared copies pinned' if document['pinned'] else 'Prepared copies unpinned')
        _identity(out, document)
        if 'copies' in document:
            out.field('Copies', document['copies'])
        return
    if document.get('purged') is True or document.get('removed') is True:
        out.emit('Prepared bindings removed' if document.get('purged') is True else 'Home removed')
        _identity(out, document)
        if document.get('shared_copies_retained'):
            out.field('Shared working copies', f"{document['shared_copies_retained']} retained for other recipes")
        if document.get('home_untouched') is True:
            out.field('Home', 'retained')
        if document.get('archive_untouched') is True:
            out.field('Archive', 'retained')
        return
    if operation == 'budget' and isinstance(document.get('nodes'), list):
        out.emit('Managed storage usage')
        for index, node in enumerate(document['nodes']):
            if index:
                out.blank()
            out.field('Node', names(node['node_id']) if node.get('node_id') else 'not recorded')
            for key, label in (('used', 'Managed files'), ('available', 'Disk free'),
                               ('total', 'Disk total'), ('reserve', 'Reserve'), ('limit', 'Copy budget')):
                if key in node:
                    out.field(label, bytes_text(node[key]))
        return
    raise ValueError('unrecognized storage result; inspect the machine-readable output with --json')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', required=True, choices=OPERATIONS)
    parser.add_argument('--archive-action', default='', choices=('', 'create', 'verify'))
    args = parser.parse_args(argv)
    try:
        render(json.load(sys.stdin), operation=args.operation, archive_action=args.archive_action,
               names=NodeNames.saved())
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(f'storage display: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
