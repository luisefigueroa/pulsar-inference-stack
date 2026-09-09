#!/usr/bin/env python3
"""Assess one catalog spec against the current stack without gating membership."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

STACK_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STACK_ROOT))

from release_spec import (load_spec, spec_id_for, verify_spec,
                          nccl_qps_from_identity)
from scripts import release_consumer as consumer
from scripts.platform_reference import load_platform_file


class CompatibilityError(ValueError):
    pass


def check_launch_compatibility(spec: dict) -> dict:
    """Return a static compatibility observation; never alter catalog state."""
    spec = verify_spec(spec)
    identity = spec['identity']
    recipe_qps = nccl_qps_from_identity(identity)
    platform_id = identity['geometry']['platform_id']
    platform_file = STACK_ROOT / 'platforms' / f'{platform_id}.json'
    if platform_file.is_symlink() or not platform_file.is_file():
        raise CompatibilityError(f'no checked-in platform definition for {platform_id}')
    platform = load_platform_file(platform_file)
    if platform['platform_id'] != platform_id or platform['accelerators_per_node'] != 1:
        raise CompatibilityError(
            'platform definition is incompatible with one accelerator per serving rank')
    variables = consumer.spec_profile_variables(
        spec,
        dict(port=8000, served_name=identity['model_id'], cache_root=None, placement=None),
        consumer.DEFAULT_IMAGE_REPO,
        active_platform_id=platform_id,
    )
    actual, gaps = consumer.build_profile_identity(
        model_id=variables['MODEL'], image=variables['IMAGE'],
        nodes=int(variables['NODES']), gpu_mem_util=variables['GPU_MEM_UTIL'],
        engine_args=variables['ENGINE_ARGS'], container_env=variables['CONTAINER_ENV'],
        spec_decode_args=variables['SPEC_DECODE_ARGS'], spec_decode=False,
        platform_id=variables['SPEC_PLATFORM_ID'],
        snapshot_revision=variables['SNAPSHOT_REVISION'],
        files=identity['snapshot_manifest']['files'],
        freeze_nccl_qps=any(
            item.startswith('NCCL_IB_QPS_PER_CONNECTION=')
            for item in identity['container_env']),
    )
    if actual is None or spec_id_for(actual) != spec['spec_id'] or actual != identity:
        raise CompatibilityError(f'current stack cannot reproduce the frozen recipe: {gaps}')
    consumer.require_exact_contract(
        consumer.comparable_contract_from_identity(actual),
        consumer.comparable_contract_from_spec(spec),
    )
    if identity['geometry']['nodes'] > 1:
        argv = variables['ENGINE_ARGS']
        backends = [
            argv[index + 1]
            for index, value in enumerate(argv[:-1])
            if value == '--distributed-executor-backend'
        ]
        if not backends or any(value != 'mp' for value in backends):
            raise CompatibilityError(
                'current multi-node launcher requires --distributed-executor-backend mp')
    try:
        gpu_mem_util = float(variables['GPU_MEM_UTIL'])
    except ValueError as exc:
        raise CompatibilityError('GPU memory utilization is not numeric') from exc
    if not 0 < gpu_mem_util <= 1:
        raise CompatibilityError('GPU memory utilization must satisfy 0 < value <= 1')
    return {
        'schema_version': 1,
        'kind': 'pulsar-launch-compatibility',
        'spec_id': spec['spec_id'],
        'recipe_nccl_ib_qps': recipe_qps,
        'compatible': True,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = check_launch_compatibility(load_spec(args.spec))
    except (CompatibilityError, ValueError, OSError, KeyError, TypeError) as exc:
        print(f'launch compatibility: {exc}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print('Launch compatibility checked against the current stack.')
        print('This observation does not grant or remove catalog membership.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
