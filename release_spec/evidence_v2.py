"""Independent evidence checks for schema-2/3 specs and immutable measurement runs."""
from __future__ import annotations
import hashlib
from pathlib import Path

from . import serving
from .baseline_policy import load_supported_policy, applied_accuracy_floor, copied_thresholds
from .baseline_evaluate import _judge_gate, OPERATION_FILES
from .measurement import load_measurement_bytes, read_stable_bytes
from .run_v3 import verify_run
from .run_record import _time


def evaluate_measurements(spec, policy_path, measurements_dir):
    spec=serving.verify_spec(spec)
    policy,digest=load_supported_policy(policy_path)
    floor=applied_accuracy_floor(policy,spec['recipe']['model']['model_id'])
    outcomes={};measurements=[];hashes={};documents={}
    # Retain all six measurements, including the non-gating v2 diagnostic.
    for operation, filename in OPERATION_FILES.items():
        path=Path(measurements_dir)/filename
        if path.exists() or path.is_symlink():
            raw=read_stable_bytes(path,label='compact measurement')
            value=load_measurement_bytes(raw)
            if value['operation']!=operation:
                raise ValueError('measurement filename and operation differ')
            hashes[operation]=hashlib.sha256(raw).hexdigest()
        else:
            value=None
        documents[operation]=value
    for gate in policy['gates']:
        operation=gate['operation']
        value=documents[operation]
        outcome=_judge_gate(gate,value,spec=spec,accuracy_floor=floor)
        outcomes[gate['criterion_id']]=outcome
        measurements.append(dict(criterion_id=gate['criterion_id'],outcome=outcome,
            thresholds=copied_thresholds(gate,accuracy_floor=floor if operation=='evaluate-gsm8k' else None)))
    result=dict(schema_version=1,kind='pulsar-baseline-evaluation',spec_id=spec['spec_id'],
        policy_digest=digest,outcomes=outcomes,measurements=measurements,measurement_sha256=hashes,
        outcome='pass' if all(v=='pass' for v in outcomes.values()) else 'fail' if 'fail' in outcomes.values() else 'incomplete')
    if policy['suite']=='baseline-v2':
        diagnostic=documents['compare-captures']
        result.update(schema_version=2,suite='baseline-v2',diagnostics={'compare-captures':diagnostic})
        # Differences never grade the recipe; missing/unusable captures cannot
        # establish completion of the prescribed measurement campaign.
        if (diagnostic is None or diagnostic['completion']!='complete') and result['outcome']=='pass':
            result['outcome']='incomplete'
    return result,documents


def verify_evidence(spec_path, run_path, evidence_root):
    spec=serving.load_spec(spec_path)
    run_path=Path(run_path).absolute()
    root=Path(evidence_root).absolute()
    if '..' in run_path.parts or not run_path.is_relative_to(root):
        raise ValueError('run must be below the declared evidence root')
    run=serving.load_json(run_path)
    policy_path=run_path.parent/'policy.json'
    directory=run_path.parent/'measurements'
    if not directory.exists():
        directory=run_path.parent  # Compact publication layout flattens measurements.
    evaluation,documents=evaluate_measurements(spec,policy_path,directory)
    verify_run(run,spec,policy_digest=evaluation['policy_digest'])
    if run['measurement_sha256']!=evaluation['measurement_sha256']:
        raise ValueError('run hashes do not match all supplied measurements')
    if run['input_sha256'].get('policy')!=hashlib.sha256(read_stable_bytes(policy_path,label='recorded policy')).hexdigest():
        raise ValueError('run input policy bytes differ')
    accuracy=documents.get('evaluate-gsm8k')
    if accuracy is not None and run['input_sha256'].get('dataset')!=accuracy['evaluate-gsm8k']['dataset_file_sha256']:
        raise ValueError('run input dataset differs from the measured dataset')
    soak=documents.get('validate-soak')
    if soak is not None:
        gate=next((gate for gate in run['gates'] if gate['name']=='validate-soak'),None)
        if gate is None:
            raise ValueError('soak evidence has no recorded invocation')
        payload=soak['validate-soak']
        if not (_time(gate['started_at'],'gate start') <= _time(payload['started_at'],'soak start')
                <= _time(payload['ended_at'],'soak end') <= _time(gate['ended_at'],'gate end')):
            raise ValueError('soak evidence lies outside its invocation')
    if not run['error_codes'] and run['outcome']!=evaluation['outcome']:
        raise ValueError('run outcome differs from independent policy evaluation')
    if run['outcome']=='pass' and evaluation['outcome']!='pass':
        raise ValueError('run claims passing evidence without the recorded policy criteria and complete measurements')
    return {'schema_version':1,'kind':'pulsar-evidence-verification','spec_id':spec['spec_id'],
            'run_id':run['run_id'],'verified':True,'outcome':run['outcome'],'evaluation':evaluation}


def evidence_summary(verified, spec, archive_observation=None):
    from .summary import archive_proof
    if archive_observation is not None:
        if isinstance(archive_observation,dict) and set(archive_observation)=={'schema_version','ok','result'}:
            if type(archive_observation['schema_version']) is not int or archive_observation['schema_version']!=1 or archive_observation['ok'] is not True:
                raise ValueError('archive command did not return a successful observation')
            archive_observation=archive_observation['result']
        serving.closed(archive_observation,{'schema_version','kind','observed_at','verification'},'archive observation')
        if type(archive_observation['schema_version']) is not int or archive_observation['schema_version']!=2 or archive_observation['kind']!='pulsar-archive-observation':
            raise ValueError('include a timestamped public archive observation; do not invent an observation time')
        _time(archive_observation['observed_at'],'archive observation time')
        archive_proof(archive_observation['verification'],spec)
    return {'schema_version':2,'kind':'pulsar-evidence-summary',
            **{key:verified[key] for key in ('spec_id','run_id','outcome','evaluation')},
            'archive_observation':archive_observation}
