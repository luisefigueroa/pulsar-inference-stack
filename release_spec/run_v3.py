"""Compact measurement runs bind effective specs, not launching commits."""
from __future__ import annotations
import re
from . import serving
from .schema import require_commit, require_sha256_hex
from .run_record import _time

FIELDS={'schema_version','kind','run_id','spec_id','policy_digest','workbench_commit',
        'stack_observers','ranks_before','ranks_after','gates','outcome','observation_complete',
        'same_boot','measurement_sha256','error_codes','input_sha256'}
RANK_FIELDS={'rank','running','owned','spec_id','image_digest','snapshot_manifest_id',
             'boot_witness','files_verified','container_configuration'}
OPERATIONS={'verify-snapshot-manifest','serve-smoke','compare-captures','benchmark-serving',
            'evaluate-gsm8k','validate-soak'}
ERRORS={'observation_failed','producer_failed','interrupted','tooling_changed','evaluation_failed','runtime_changed'}


def verify_run(record, spec, *, policy_digest=None):
    spec=serving.verify_spec(spec)
    serving.closed(record,FIELDS,'run')
    if type(record['schema_version']) is not int or record['schema_version']!=spec['schema_version']+1 or record['kind']!='pulsar-baseline-run':
        serving.invalid('run','run schema differs from effective spec')
    if not isinstance(record['run_id'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}',record['run_id']):
        serving.invalid('run.run_id','invalid run identifier')
    if record['spec_id']!=spec['spec_id']:
        serving.invalid('run.spec_id','differs from effective spec')
    require_sha256_hex(record['policy_digest'],path='run.policy_digest')
    if policy_digest is not None and record['policy_digest']!=policy_digest:
        serving.invalid('run.policy_digest','differs from recorded policy')
    require_commit(record['workbench_commit'],path='run.workbench_commit')
    observers=record['stack_observers']
    if not isinstance(observers,list) or len(observers)>2:
        serving.invalid('run.stack_observers','expected up to two observer provenance records')
    for observer in observers:
        serving.closed(observer,{'stack_commit','working_tree_dirty'},'observer')
        if observer['stack_commit'] is not None:
            require_commit(observer['stack_commit'],path='observer.stack_commit')
        if observer['working_tree_dirty'] is not None and type(observer['working_tree_dirty']) is not bool:
            serving.invalid('observer.working_tree_dirty','expected boolean or null')
    for field in ('measurement_sha256','input_sha256'):
        if not isinstance(record[field],dict):
            serving.invalid('run.'+field,'expected a digest map')
        for name,digest in record[field].items():
            if field=='measurement_sha256' and name not in OPERATIONS:
                serving.invalid('run.measurement_sha256','unknown measurement operation')
            if field=='input_sha256' and name not in {'dataset','policy'}:
                serving.invalid('run.input_sha256','unknown input kind')
            require_sha256_hex(digest,path='run.'+field+'.'+name)
    count=spec['recipe']['geometry']['nodes']
    for side in ('ranks_before','ranks_after'):
        ranks=record[side]
        if not isinstance(ranks,list) or (ranks and len(ranks)!=count):
            serving.invalid('run.'+side,'must be empty or cover every required rank')
        for index,rank in enumerate(ranks):
            serving.closed(rank,(RANK_FIELDS-{'snapshot_manifest_id'}|{'snapshots'}) if spec['schema_version']==3 else RANK_FIELDS,'rank')
            if type(rank['rank']) is not int or rank['rank']!=index:
                serving.invalid('rank.rank','rank order or coverage differs')
            for flag in ('running','owned','files_verified'):
                if rank[flag] is not True:
                    serving.invalid('rank.'+flag,'rank was not fully observed')
            expected={'spec_id':spec['spec_id'],'image_digest':spec['recipe']['image_digest'],
                'snapshot_manifest_id':spec['recipe']['model']['snapshot_manifest']['manifest_id']}
            if spec['schema_version']==3:
                snapshots=rank.get('snapshots')
                if not isinstance(snapshots,dict) or any(not isinstance(v,dict) or v.get('files_verified') is not True for v in snapshots.values()):
                    serving.invalid('rank.snapshots','every snapshot must be fully verified')
                expected.pop('snapshot_manifest_id')
                expected['snapshots'] = {name:{'snapshot_manifest_id':model['snapshot_manifest']['manifest_id'],'files_verified':True}
                    for name,model in serving.required_snapshots(spec).items()}
            for key,value in expected.items():
                if rank[key]!=value:
                    serving.invalid('rank.'+key,'differs from effective spec')
            require_sha256_hex(rank['boot_witness'],path='rank.boot_witness')
            if rank['container_configuration']!=spec['recipe']['container']:
                serving.invalid('rank.container_configuration','differs from effective recipe')
    complete=bool(record['ranks_before']) and bool(record['ranks_after'])
    same=complete and record['ranks_before']==record['ranks_after']
    if type(record['observation_complete']) is not bool or record['observation_complete']!=complete:
        serving.invalid('run.observation_complete','disagrees with rank observations')
    if type(record['same_boot']) is not bool or record['same_boot']!=same:
        serving.invalid('run.same_boot','disagrees with rank observations')
    serving.choice(record['outcome'],('pass','fail','incomplete'),'run.outcome')
    if not isinstance(record['error_codes'],list) or any(code not in ERRORS for code in record['error_codes']):
        serving.invalid('run.error_codes','unknown run error')
    gates=record['gates']
    if not isinstance(gates,list):
        serving.invalid('run.gates','expected an ordered gate list')
    names=[];end=None
    for gate in gates:
        serving.closed(gate,{'name','started_at','ended_at','rc'},'gate')
        if gate['name'] not in ('verify-snapshot-manifest','serve-smoke','run-gates','evaluate-gsm8k','validate-soak'):
            serving.invalid('gate.name','unknown producer')
        start=_time(gate['started_at'],'gate start');finish=_time(gate['ended_at'],'gate end')
        if finish<start or (end is not None and start<end):
            serving.invalid('run.gates','producer intervals overlap or run backwards')
        end=finish;names.append(gate['name'])
        serving.integer(gate['rc'],'gate.rc')
    expected_names=['verify-snapshot-manifest','serve-smoke','run-gates','evaluate-gsm8k','validate-soak']
    if names!=expected_names[:len(names)]:
        serving.invalid('run.gates','producer order differs from baseline-v1')
    if record['outcome']=='pass' and (not same or record['error_codes'] or names!=expected_names
            or any(gate['rc'] for gate in gates) or set(record['measurement_sha256'])!=OPERATIONS
            or set(record['input_sha256'])!={'dataset','policy'}):
        serving.invalid('run.outcome','successful run is missing complete unchanged observations, inputs or producers')
    return record
