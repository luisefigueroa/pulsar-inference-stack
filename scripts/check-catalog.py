#!/usr/bin/env python3
"""Verify every maintainer-published catalog spec's schema and filename.

No private checkout, model bytes, GPU, Docker, or network access is required.
Catalog membership is the maintainer's decision. Evidence and current launch
compatibility are separate diagnostics and do not gate this check. Specs
recorded in catalog-removals.json must have no remaining catalog or results/
files.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

STACK_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STACK_ROOT))

from release_spec import load_spec
from release_spec.catalog_removals import LEDGER_PATH, parse_removals


class CatalogError(ValueError):
    pass


def regular_tree(root: Path) -> tuple[set[str], set[str]]:
    """Inventory a publication tree without following links or opening specials."""
    files, directories = set(), set()
    if root.is_symlink():
        raise CatalogError(f'{root.name}: publication root must not be a symlink')
    if not root.exists():
        return files, directories
    if not root.is_dir():
        raise CatalogError(f'{root.name}: publication root must be a directory')
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in [*dirs, *names]:
            path = Path(base) / name
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise CatalogError(f'{root.name}/{relative}: publication entry must be a regular file or directory')
            (directories if stat.S_ISDIR(mode) else files).add(relative)
    return files, directories


def check_catalog(repo_root: str | Path) -> dict:
    root = Path(repo_root).absolute()
    releases = root / 'releases'
    release_files, release_dirs = regular_tree(releases)
    if release_dirs:
        raise CatalogError('releases/ contains unexpected subdirectories')
    release_files.discard('README.md')
    specs = []
    declared_evidence_count = 0
    for name in sorted(release_files):
        if re.fullmatch(r'[0-9a-f]{64}\.json', name) is None:
            raise CatalogError(f'releases/{name}: filename must be the complete spec id')
        spec = load_spec(releases / name)
        spec_id = spec['spec_id']
        if name != f'{spec_id}.json':
            raise CatalogError(f'releases/{name}: filename differs from catalog identity')
        declared_evidence_count += len(spec.get('evidence', []))
        specs.append(spec)
    ledger = root / LEDGER_PATH
    if ledger.is_symlink() or (ledger.exists() and not ledger.is_file()):
        raise CatalogError(f'{LEDGER_PATH}: removal ledger must be a regular file')
    removals = parse_removals(ledger.read_bytes() if ledger.exists() else None)
    results = root / 'results'
    policies = [path for path in results.iterdir() if path.is_dir() and not path.is_symlink()] if results.is_dir() else []
    for spec_id in sorted(removals):
        if f'{spec_id}.json' in release_files:
            raise CatalogError(f'releases/{spec_id}.json: spec is recorded as removed in {LEDGER_PATH}')
        for policy in policies:
            if (policy / spec_id).exists() or (policy / spec_id).is_symlink():
                raise CatalogError(f'results/{policy.name}/{spec_id}: evidence remains for a removed spec')
    return {'schema_version': 1, 'kind': 'pulsar-catalog-verification', 'verified': True,
            'spec_count': len(specs), 'declared_evidence_count': declared_evidence_count}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', default=STACK_ROOT)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = check_catalog(args.repo_root)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'error: catalog check: {exc}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f'Catalog checked: {result["spec_count"]} schema-valid spec(s).')
        print('State, review, evidence, and launch compatibility are separate and were not used as catalog gates.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
