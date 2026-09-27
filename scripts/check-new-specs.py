#!/usr/bin/env python3
"""New or edited catalog files use the current spec; unchanged history is readable.

A catalog file may be deleted only for a spec recorded in catalog-removals.json
whose results/ evidence is removed in the same change.
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from release_spec import serving
from release_spec.catalog_removals import LEDGER_PATH, check_append_only, parse_removals
from release_spec.immutable_io import parse_strict_json


def git(root,*args):
    result=subprocess.run(['git','-C',str(root),*args],capture_output=True)
    if result.returncode: raise ValueError('cannot inspect the catalog change range')
    return result.stdout


def commit(root,ref):
    value=git(root,'rev-parse','--verify','--end-of-options',ref+'^{commit}').decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40,64}',value): raise ValueError('invalid Git commit')
    return value


def tree_blob(root,rev,path):
    """Read one regular file from a commit, or from the index when rev is None."""
    listing=git(root,'ls-files','--stage','--',path) if rev is None else git(root,'ls-tree',rev,'--',path)
    if not listing: return None
    if listing.split(b' ',1)[0] not in (b'100644',b'100755'): raise ValueError(f'{path} must be a regular file')
    return git(root,'show',(':' if rev is None else rev+':')+path)


def evidence_spec_ids(root,rev):
    names=git(root,'ls-files','-z','--','results/') if rev is None else git(root,'ls-tree','-r','-z','--name-only',rev,'--','results/')
    return {parts[2] for parts in (name.decode().split('/') for name in names.split(b'\0') if name) if len(parts)>3}


def check_removals(root,removed,*,base_rev,head_rev):
    before=parse_removals(None if base_rev is False else tree_blob(root,base_rev,LEDGER_PATH))
    after=parse_removals(tree_blob(root,head_rev,LEDGER_PATH))
    check_append_only(before,after)
    unrecorded=sorted(spec_id for spec_id in removed if spec_id not in after)
    if unrecorded:
        raise ValueError(f'catalog records must remain readable history; deletion or renaming is not allowed unless the spec is recorded in {LEDGER_PATH}')
    remaining=sorted(removed & evidence_spec_ids(root,head_rev))
    if remaining:
        raise ValueError(f'removed catalog spec still has results/ evidence: {", ".join(remaining)}')


def check(root,*,base=None,head='HEAD',staged=False,merge_base=False):
    root=Path(root).absolute()
    if staged:
        if merge_base: raise ValueError('--merge-base requires a commit range')
        changes=git(root,'diff','--cached','--name-status','--no-renames','-z','--','releases/').split(b'\0')[:-1]
    else:
        if base is None: raise ValueError('select --base or --staged')
        base_commit,head_commit=commit(root,base),commit(root,head)
        if merge_base:
            base_commit=git(root,'merge-base',base_commit,head_commit).decode().strip()
        changes=git(root,'diff','--name-status','--no-renames','-z',base_commit,head_commit,'--','releases/').split(b'\0')[:-1]
    checked=0;removed=set()
    for status,raw_name in zip(changes[::2],changes[1::2]):
        name=raw_name.decode()
        if name=='releases/README.md': continue
        if status==b'D':
            match=re.fullmatch(r'releases/([0-9a-f]{64})\.json',name)
            removed.add(match.group(1) if match else name);continue
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
    if staged:
        has_head=subprocess.run(['git','-C',str(root),'rev-parse','--verify','-q','HEAD^{commit}'],capture_output=True).returncode==0
        check_removals(root,removed,base_rev='HEAD' if has_head else False,head_rev=None)
    else:
        check_removals(root,removed,base_rev=base_commit,head_rev=head_commit)
    return checked


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root',default=ROOT)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--staged',action='store_true');group.add_argument('--base')
    parser.add_argument('--head',default='HEAD')
    parser.add_argument('--merge-base',action='store_true',help='check only topic-branch changes for a pull request')
    args=parser.parse_args()
    try:
        count=check(args.repo_root,base=args.base,head=args.head,staged=args.staged,merge_base=args.merge_base)
        print(f'Current spec format checked: {count} changed catalog file(s).')
        return 0
    except (ValueError,OSError) as exc:
        print(f'error: catalog changes: {exc}',file=sys.stderr);return 2

if __name__=='__main__': raise SystemExit(main())
