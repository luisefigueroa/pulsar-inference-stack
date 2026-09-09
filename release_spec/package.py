"""Closed catalog contribution packages; evidence quality never grants membership."""
from __future__ import annotations
import hashlib
from pathlib import Path, PurePosixPath
import re

from . import serving
from .baseline_evaluate import OPERATION_FILES
from .evidence_v2 import verify_evidence, evidence_summary
from .measurement import read_stable_bytes


def allowed_path(spec_id, name):
    if name == f'releases/{spec_id}.json':
        return True
    parts=PurePosixPath(name).parts
    return (len(parts)==5 and parts[:3]==('results','baseline-v1',spec_id)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}',parts[3]) is not None
            and parts[4] in set(OPERATION_FILES.values())|{'run.json','policy.json','evaluation.json','summary.json'})


def verify_package(root):
    root=Path(root).absolute()
    package=serving.load_json(root/'package.json')
    serving.closed(package,{'schema_version','kind','spec_id','files'},'package')
    if type(package['schema_version']) is not int or package['schema_version']!=2 or package['kind']!='pulsar-contribution-package':
        raise ValueError('new publications require contribution package schema 2')
    spec_path=root/'releases'/f"{package['spec_id']}.json"
    spec=serving.load_spec(spec_path)
    if spec['spec_id']!=package['spec_id'] or not isinstance(package['files'],dict):
        raise ValueError('package identity is invalid')
    if f"releases/{spec['spec_id']}.json" not in package['files']:
        raise ValueError('package does not bind its spec bytes')
    for name,digest in package['files'].items():
        if (not isinstance(name,str) or not allowed_path(spec['spec_id'],name)
                or '..' in PurePosixPath(name).parts or not isinstance(digest,str)
                or not re.fullmatch(r'[0-9a-f]{64}',digest)):
            raise ValueError('package contains an unsupported artifact path or digest')
        if hashlib.sha256(read_stable_bytes(root/name,label='package artifact')).hexdigest()!=digest:
            raise ValueError('package artifact digest differs')
    actual=set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('package may not contain symlinks')
        if path.is_file(): actual.add(path.relative_to(root).as_posix())
    if actual!=set(package['files'])|{'package.json'}:
        raise ValueError('package contains missing or undeclared artifacts')
    runs={PurePosixPath(name).parts[3] for name in package['files'] if name.startswith('results/')}
    for run_id in sorted(runs):
        directory=root/'results'/'baseline-v1'/spec['spec_id']/run_id
        verified=verify_evidence(spec_path,directory/'run.json',root)
        if verified['run_id']!=run_id:
            raise ValueError('run directory differs from recorded run identifier')
        for name in ('evaluation.json','summary.json'):
            path=directory/name
            if path.exists():
                document=serving.load_json(path)
                if not isinstance(document,dict): raise ValueError(name+' must be a structured evidence document')
                expected=verified['evaluation'] if name=='evaluation.json' else evidence_summary(verified,spec,document.get('archive_observation'))
                if document!=expected:
                    raise ValueError(name+' differs from independently verified evidence')
    return package
