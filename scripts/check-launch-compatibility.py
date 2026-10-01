#!/usr/bin/env python3
"""Static spec support diagnostic, independent of catalog membership."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from release_spec import serving
from scripts.platform_reference import load_platform_file

class CompatibilityError(ValueError): pass

def check_launch_compatibility(spec):
    try: spec=serving.verify_spec(spec)
    except ValueError as exc: raise CompatibilityError(str(exc)) from exc
    if 'guard' in spec['recipe']['container']:
        raise CompatibilityError('guard execution is not supported by ordinary start; use explicitly scoped guarded run')
    serving.snapshot_engine_args(spec['recipe'])
    geometry=spec['recipe']['geometry']
    path=ROOT/'platforms'/(geometry['platform_id']+'.json')
    if path.is_symlink() or not path.is_file():
        raise CompatibilityError('spec platform is not supported by this Stack')
    platform=load_platform_file(path)
    if platform['platform_id']!=geometry['platform_id'] or platform['accelerators_per_node']!=1:
        raise CompatibilityError('platform is incompatible with the serving geometry')
    return {'schema_version':1,'kind':'pulsar-launch-compatibility','spec_id':spec['spec_id'],
            'compatible':True,'availability_checked':False}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec',required=True);parser.add_argument('--json',action='store_true')
    args=parser.parse_args(argv)
    try: result=check_launch_compatibility(serving.load_spec(args.spec))
    except (ValueError,OSError) as exc:
        print(f'error: launch compatibility: {exc}',file=sys.stderr);return 1
    print(json.dumps(result,sort_keys=True) if args.json else 'Spec contract and platform supported; live prerequisites were not checked.')
    return 0
if __name__=='__main__': raise SystemExit(main())
