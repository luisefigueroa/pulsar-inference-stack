"""Stable public result envelopes around Stack-owned actions and documents."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from release_spec import serving
from scripts.document_cli import CommandParser, emit, failure


def redact_diagnostic(value):
    from scripts.check_publishable_privacy import SECRET_PATTERNS
    for _,pattern in SECRET_PATTERNS:
        value=pattern.sub('<credential>',value)
    value=re.sub(r'(?i)(bearer\s+|--api-key(?:=|\s+)|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s\"\x27]+',r'\1<credential>',value)
    for name,secret in os.environ.items():
        if len(secret)>=4 and re.search(r'(?i)(?:^|_)(?:TOKEN|PASSWORD|SECRET|CREDENTIAL|API_KEY)(?:_|$)',name):
            value=value.replace(secret,'<credential>')
    return value


def producer_provenance():
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--verify', 'HEAD'],
                            text=True, capture_output=True)
    dirty = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True, capture_output=True)
    return {'stack_commit': commit.stdout.strip() if commit.returncode == 0 else None,
            'working_tree_dirty': bool(dirty.stdout) if dirty.returncode == 0 else None}


def execute(script, args, *, json_result=False, env=None):
    result = subprocess.run(['bash', str(ROOT / script), *map(str, args)], env=env,
                            cwd=ROOT,text=True, capture_output=True)
    diagnostic=redact_diagnostic(result.stderr)
    if diagnostic:
        print(diagnostic, file=sys.stderr, end='')
    if result.returncode:
        if result.stdout:
            print(redact_diagnostic(result.stdout), file=sys.stderr, end='')
        raise RuntimeError((diagnostic.strip() or redact_diagnostic(result.stdout).strip() or 'Stack action failed')[-4000:])
    if json_result:
        return json.loads(result.stdout)
    if result.stdout:
        print(result.stdout, file=sys.stderr, end='')
    return {'completed': True}


def measurement(args):
    from release_spec import measurement as m
    parser = CommandParser()
    parser.add_argument('--operation', required=True, choices=['compare-captures', 'benchmark-serving',
        'evaluate-gsm8k', 'validate-soak', 'verify-snapshot-manifest', 'serve-smoke', 'observe-resources'])
    parser.add_argument('--input', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--validate', action='store_true', help='validate a complete measurement document')
    options = parser.parse_args(args)
    value = serving.load_json(options.input)
    if options.validate:
        document=m.validate_measurement(value)
        if document['operation'] != options.operation:
            raise ValueError('measurement operation differs from request')
        m.write_measurement(Path(options.out).absolute(),document)
        return document
    serving.closed(value, {'completion', 'reason', 'payload'}, 'measurement input')
    builders = {'compare-captures': m.build_compare_measurement, 'evaluate-gsm8k': m.build_accuracy_measurement,
        'validate-soak': m.build_soak_measurement, 'verify-snapshot-manifest': m.build_identity_measurement,
        'serve-smoke': m.build_serve_smoke_measurement, 'observe-resources': m.build_resource_measurement}
    if options.operation == 'benchmark-serving':
        document = m.build_benchmark_measurement(completion=value['completion'], reason=value['reason'], **value['payload'])
    else:
        document = builders[options.operation](**value)
    m.write_measurement(Path(options.out).absolute(), document)
    return document


def policy(args):
    from release_spec.baseline_policy import load_policy
    if args != ['show', 'baseline-v1']:
        raise ValueError('usage: pulsar policy show baseline-v1 --json')
    document, digest = load_policy(ROOT / 'policy/baseline-v1.json')
    return {'policy': document, 'policy_digest': digest}


def evaluate(args):
    from release_spec.measurement import atomic_write_json
    from release_spec.evidence_v2 import evaluate_measurements
    parser=CommandParser()
    parser.add_argument('--spec-file', required=True)
    parser.add_argument('--policy-file', required=True)
    parser.add_argument('--measurements-dir', required=True)
    parser.add_argument('--out', required=True)
    options=parser.parse_args(args)
    spec=serving.load_spec(options.spec_file)
    result,_=evaluate_measurements(spec,options.policy_file,options.measurements_dir)
    target=Path(options.out).absolute()
    if target.exists():
        raise ValueError('evaluation output already exists; choose a new run directory')
    target.mkdir(mode=0o700,parents=True)
    atomic_write_json(target/'evaluation.json',result)
    return result


def dispatch(command, args):
    if command == 'observe':
        result = execute('scripts/observe-serving.sh', args, json_result=True)
        result['producer'] = producer_provenance()
        return result
    if command == 'resources':
        os.chdir(ROOT)
        os.execvp('bash',['bash',str(ROOT/'scripts/resources.sh'),*args])
    if command == 'start':
        with tempfile.TemporaryDirectory(prefix='pulsar-start-result.') as temp:
            path=Path(temp)/'result.json'
            execute('scripts/up.sh',args,env={**os.environ,'PULSAR_LAUNCH_RESULT_FILE':str(path)})
            return serving.load_json(path)
    if command == 'model':
        result=execute('scripts/model-library.sh', [*args, '--json'], json_result=True)
        if isinstance(result,dict) and result.get('kind')=='pulsar-archive-verification':
            from datetime import datetime,timezone
            return {'schema_version':2,'kind':'pulsar-archive-observation',
                    'observed_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),'verification':result}
        return result
    if command == 'status':
        return execute('scripts/status.sh', [*args, '--json'], json_result=True)
    if command == 'stop':
        return execute('scripts/down.sh', args)
    if command == 'policy':
        return policy(args)
    if command == 'contribution' and args[:1] == ['verify']:
        from release_spec.package import verify_package
        parser=CommandParser()
        parser.add_argument('--package',required=True)
        options=parser.parse_args(args[1:])
        result=verify_package(options.package)
        from scripts.check_publishable_privacy import scan_files
        from release_spec.measurement import read_stable_bytes
        _, findings=scan_files((name,read_stable_bytes(Path(options.package)/name,label='package privacy'))
                              for name in [*result['files'],'package.json'])
        if findings:
            raise ValueError('package privacy check failed: '+', '.join(sorted({finding.rule for finding in findings})))
        return result
    if command == 'evidence' and args:
        if args[0] == 'measurement':
            return measurement(args[1:])
        if args[0] == 'evaluate':
            return evaluate(args[1:])
        if args[0] in ('verify', 'summary'):
            from release_spec.evidence_v2 import verify_evidence
            parser=CommandParser()
            parser.add_argument('--spec-file',required=True)
            parser.add_argument('--run',required=True)
            parser.add_argument('--evidence-root')
            if args[0]=='summary': parser.add_argument('--archive-observation')
            options=parser.parse_args(args[1:])
            result=verify_evidence(options.spec_file,options.run,options.evidence_root or Path(options.run).parent)
            if args[0]=='summary':
                from release_spec.evidence_v2 import evidence_summary
                archive=serving.load_json(options.archive_observation) if options.archive_observation else None
                return evidence_summary(result,serving.load_spec(options.spec_file),archive)
            return result
    if command == 'selftest':
        return execute('scripts/selftest.sh', args)
    if command == 'privacy' and args[:1] == ['check']:
        parser=CommandParser()
        parser.add_argument('--root',required=True)
        parser.add_argument('--staged',action='store_true')
        options=parser.parse_args(args[1:])
        root=Path(options.root).absolute()
        if not (root/'.git').exists():
            if options.staged: raise ValueError('staged privacy checks require a Git checkout')
            if root.is_symlink() or not root.is_dir(): raise ValueError('privacy root must be a regular directory')
            from scripts.check_publishable_privacy import scan_files
            from release_spec.measurement import read_stable_bytes
            files=[]
            for path in root.rglob('*'):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise ValueError('privacy input contains a symlink or special file')
                if path.is_file(): files.append((path.relative_to(root).as_posix(),read_stable_bytes(path,label='privacy input')))
            count,findings=scan_files(files)
            if findings: raise ValueError('publication privacy check failed: '+', '.join(sorted({finding.rule for finding in findings})))
            return {'checked':True,'file_count':count}
        result=subprocess.run([sys.executable,str(ROOT/'scripts/check_publishable_privacy.py'),
            '--repo-root',options.root,*(['--staged'] if options.staged else [])],text=True,capture_output=True)
        print(result.stdout+result.stderr,file=sys.stderr,end='')
        if result.returncode:
            raise ValueError('publication privacy check failed')
        return {'checked':True}
    if command == 'privacy' and args[:1] == ['commits']:
        parser=CommandParser()
        parser.add_argument('--root',required=True)
        parser.add_argument('--range',required=True)
        options=parser.parse_args(args[1:])
        result=subprocess.run([sys.executable,str(ROOT/'scripts/check_commit_privacy.py'),
            '--repo-root',options.root,'--range',options.range],text=True,capture_output=True)
        print(result.stdout+result.stderr,file=sys.stderr,end='')
        if result.returncode: raise ValueError('commit metadata privacy check failed')
        return {'checked':True}
    raise ValueError('unsupported public command')


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    json_output='--json' in argv
    argv=[arg for arg in argv if arg!='--json']
    try:
        if not argv:
            raise ValueError('a public command is required')
        # Interpret artifact paths at the user's working directory before
        # entering Stack's implementation directory.
        path_flags={'--spec-file','--manifest','--manifest-out','--override-file','--overlay'}
        for index in range(1,len(argv)):
            if argv[index-1] in path_flags:
                argv[index]=str(Path(argv[index]).absolute())
        human_scripts={'start':'scripts/up.sh','stop':'scripts/down.sh',
                       'status':'scripts/status.sh','model':'scripts/model-library.sh'}
        if argv[0] in human_scripts and (not json_output or any(a in ('--help','-h') for a in argv[1:])):
            command=['bash',str(ROOT/human_scripts[argv[0]]),*argv[1:]]
            os.chdir(ROOT)
            os.execvp('bash',command)
        result=dispatch(argv[0],argv[1:])
        emit(result,json_output=json_output)
        return 0
    except (ValueError,OSError,TypeError,KeyError) as exc:
        return failure(exc,json_output=json_output)
    except RuntimeError as exc:
        return failure(exc,json_output=json_output,code='prerequisite_failed',exit_code=3)


if __name__=='__main__':
    raise SystemExit(main())
