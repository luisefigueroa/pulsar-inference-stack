#!/usr/bin/env python3
"""Verify every public catalog contribution against current stack code.

No private checkout, model bytes, GPU, Docker, or network access is required.
The check proves contribution consistency; repository review establishes trust.
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

from release_spec import load_spec, spec_id_for
from release_spec.baseline_evaluate import OPERATION_FILES
from release_spec.contribution import CLAIM_STATUSES, verify_contribution
from release_spec.measurement import parse_strict_json, read_stable_bytes
from release_spec.summary import verify_summary
from scripts import release_consumer as consumer
from scripts.platform_reference import load_platform_file


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


def current_projection(spec: dict) -> None:
    """Check the current consumer with clean deployment settings, without hardware."""
    identity = spec['identity']
    platform_id = identity['geometry']['platform_id']
    platform_file = STACK_ROOT / 'platforms' / f'{platform_id}.json'
    # Use checked-in platform data directly; ignore operator environment overrides.
    if platform_file.is_symlink() or not platform_file.is_file():
        raise CatalogError(f'no checked-in platform definition for {platform_id}')
    platform = load_platform_file(platform_file)
    if platform['platform_id'] != platform_id or platform['accelerators_per_node'] != 1:
        raise CatalogError('platform definition is incompatible with one accelerator per serving rank')
    variables = consumer.spec_profile_variables(
        spec, dict(port=8000, served_name=identity['model_id'], cache_root=None, placement=None),
        consumer.DEFAULT_IMAGE_REPO, active_platform_id=platform_id)
    actual, gaps = consumer.build_profile_identity(
        model_id=variables['MODEL'], image=variables['IMAGE'], nodes=int(variables['NODES']),
        gpu_mem_util=variables['GPU_MEM_UTIL'], engine_args=variables['ENGINE_ARGS'],
        container_env=variables['CONTAINER_ENV'], spec_decode_args=variables['SPEC_DECODE_ARGS'],
        spec_decode=False, platform_id=variables['SPEC_PLATFORM_ID'],
        snapshot_revision=variables['SNAPSHOT_REVISION'], files=identity['snapshot_manifest']['files'])
    if actual is None or spec_id_for(actual) != spec['spec_id'] or actual != identity:
        raise CatalogError(f'current stack cannot reproduce the frozen recipe: {gaps}')
    consumer.require_exact_contract(consumer.comparable_contract_from_identity(actual),
                                    consumer.comparable_contract_from_spec(spec))
    if identity['geometry']['nodes'] > 1:
        argv = variables['ENGINE_ARGS']
        backends = [argv[index + 1] for index, value in enumerate(argv[:-1])
                    if value == '--distributed-executor-backend']
        if not backends or any(value != 'mp' for value in backends):
            raise CatalogError('current multi-node launcher requires --distributed-executor-backend mp')


def check_catalog(repo_root: str | Path) -> dict:
    root = Path(repo_root).absolute()
    releases = root / 'releases'
    release_files, release_dirs = regular_tree(releases)
    if release_dirs:
        raise CatalogError('releases/ contains unexpected subdirectories')
    release_files.discard('README.md')
    specs = []
    expected_results = set()
    expected_dirs = set()
    for name in sorted(release_files):
        if re.fullmatch(r'[0-9a-f]{64}\.json', name) is None:
            raise CatalogError(f'releases/{name}: filename must be the complete spec id')
        spec = load_spec(releases / name)
        spec_id = spec['spec_id']
        if name != f'{spec_id}.json' or spec['state'] != 'released':
            raise CatalogError(f'releases/{name}: filename or state differs from catalog identity')
        base = Path('baseline-v1') / spec_id
        if spec['review']['status'] in CLAIM_STATUSES:
            expected_paths = {operation: (Path('results') / base / filename).as_posix()
                              for operation, filename in OPERATION_FILES.items()}
            expected_paths['baseline-run'] = (Path('results') / base / 'run.json').as_posix()
            if {row['id']: row['path'] for row in spec['evidence']} != expected_paths:
                raise CatalogError(f'{spec_id}: evidence paths differ from the canonical compact layout')
            paths = {(base / filename).as_posix() for filename in [*OPERATION_FILES.values(), 'run.json', 'summary.json']}
        else:
            paths = set()
            for row in spec['evidence']:
                relative = Path(row['path'])
                if relative.parts[:1] != ('results',) or len(relative.parts) < 3:
                    raise CatalogError(f'{spec_id}: evidence path is not under results/')
                paths.add(Path(*relative.parts[1:]).as_posix())
            summary = (base / 'summary.json').as_posix()
            if (root / 'results' / summary).is_file():
                paths.add(summary)
        expected_results.update(paths)
        if paths:
            expected_dirs.update({'baseline-v1', *(str(Path(path).parent) for path in paths)})
        specs.append(spec)
    actual_results, actual_dirs = regular_tree(root / 'results')
    actual_results.discard('README.md')
    if actual_results != expected_results:
        raise CatalogError('public results contain missing or unreferenced files: '
                           f'missing={sorted(expected_results-actual_results)}, extra={sorted(actual_results-expected_results)}')
    if actual_dirs != expected_dirs:
        raise CatalogError('public results contain unexpected or missing contribution directories')
    for spec in specs:
        path = releases / f'{spec["spec_id"]}.json'
        base = root / 'results' / 'baseline-v1' / spec['spec_id']
        run_file = base / 'run.json'
        verify_contribution(path, root, run_file if run_file.is_file() else path)
        if run_file.is_file() and (base / 'summary.json').is_file():
            run = parse_strict_json(read_stable_bytes(run_file, label='qualification run'), label='qualification run')
            summary = parse_strict_json(read_stable_bytes(base / 'summary.json', label='qualification summary'), label='qualification summary')
            verify_summary(summary, spec, run)
        current_projection(spec)
    return {'schema_version': 1, 'kind': 'pulsar-catalog-verification', 'verified': True,
            'spec_count': len(specs), 'evidence_file_count': len(expected_results)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', default=STACK_ROOT)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = check_catalog(args.repo_root)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'catalog check: {exc}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f'Catalog checked: {result["spec_count"]} specs and {result["evidence_file_count"]} compact evidence files.')
        print('Current stack recipe projection is compatible; physical execution and approval remain maintainer review responsibilities.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
