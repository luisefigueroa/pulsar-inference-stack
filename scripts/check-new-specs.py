#!/usr/bin/env python3
"""New or edited catalog files use the current spec; unchanged history is readable."""
import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from release_spec import serving
from release_spec.immutable_io import parse_strict_json


def git(root,*args):
    result=subprocess.run(['git','-C',str(root),*args],capture_output=True)
    if result.returncode: raise ValueError('cannot inspect the catalog change range')
    return result.stdout


def commit(root,ref):
    value=git(root,'rev-parse','--verify','--end-of-options',ref+'^{commit}').decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40,64}',value): raise ValueError('invalid Git commit')
    return value


def check(root,*,base=None,head='HEAD',staged=False):
    root=Path(root).absolute()
    if staged:
        changes=git(root,'diff','--cached','--name-status','--no-renames','-z','--','releases/').split(b'\0')[:-1]
    else:
        if base is None: raise ValueError('select --base or --staged')
        base_commit,head_commit=commit(root,base),commit(root,head)
        changes=git(root,'diff','--name-status','--no-renames','-z',base_commit,head_commit,'--','releases/').split(b'\0')[:-1]
    checked=0
    for status,raw_name in zip(changes[::2],changes[1::2]):
        name=raw_name.decode()
        if name=='releases/README.md': continue
        if status==b'D':
            raise ValueError('catalog records must remain readable history; deletion or renaming is not allowed')
        if not re.fullmatch(r'releases/[0-9a-f]{64}\.json',name):
            raise ValueError('new catalog entry has an invalid filename')
        if staged:
            mode=git(root,'ls-files','--stage','--',name).split(b' ',1)[0]
            if mode not in (b'100644',b'100755'): raise ValueError('catalog entry must be a regular file')
            spec=serving.verify_spec(parse_strict_json(git(root,'show',':'+name),label='staged spec'))
        else:
            mode=git(root,'ls-tree',head_commit,'--',name).split(b' ',1)[0]
            if mode not in (b'100644',b'100755'): raise ValueError('catalog entry must be a regular file')
            spec=serving.verify_spec(parse_strict_json(git(root,'show',head_commit+':'+name),label='committed spec'))
        if Path(name).stem!=spec['spec_id']: raise ValueError('catalog filename differs from spec identity')
        checked+=1
    return checked


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root',default=ROOT)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--staged',action='store_true');group.add_argument('--base')
    parser.add_argument('--head',default='HEAD')
    args=parser.parse_args()
    try:
        count=check(args.repo_root,base=args.base,head=args.head,staged=args.staged)
        print(f'Current spec format checked: {count} changed catalog file(s).')
        return 0
    except (ValueError,OSError) as exc:
        print(f'catalog changes: {exc}',file=sys.stderr);return 2

if __name__=='__main__': raise SystemExit(main())
