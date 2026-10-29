"""Human storage output preserves actionable facts and careful state boundaries."""
import io
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from model_library.render import render
from model_library.planning import storage_budget
from model_library.integrity import StorageError
from model_library.node_names import NodeNames, saved_hostnames, saved_order
from scripts.terminal_format import TerminalWriter

SPEC='a'*64
REVISION='b'*40
SNAPSHOT='c'*64
PATH='/var/tmp/long-operator-selected-storage-location/pulsar-homes/'+SNAPSHOT+'/snapshots/'+REVISION
HOME=dict(model_id='example/model',node_id='node-1',path=PATH,snapshot_manifest_id=SNAPSHOT,snapshot_revision=REVISION)
MANIFEST=dict(model_id='example/model',snapshot_revision=REVISION,manifest_id=SNAPSHOT,file_count=2,total_bytes=1073741824,files=[dict(path='config.json',size=1024),dict(path='weights.safetensors',size=1073740800)])


class StorageRendering(unittest.TestCase):
    def display(self,document,operation,names=None):
        stream=io.StringIO();render(document,operation=operation,writer=TerminalWriter(width=44,stream=stream),names=names)
        output=stream.getvalue()
        self.assertTrue(all(len(line)<=44 for line in output.splitlines()),output)
        self.assertNotIn('"schema_version":',output)
        self.assertNotIn("{'",output)
        return output,' '.join(output.split())

    def test_acquisition_preview_names_exact_source_and_scale(self):
        source=dict(model_id='example/model',snapshot_revision=REVISION,files=MANIFEST['files'])
        output,flat=self.display(dict(kind='pulsar-acquisition-plan',model_id='example/model',snapshot_revision=REVISION,source=source,selected_node='node-1',existing_homes=[]),'acquire')
        self.assertIn('Acquisition preview',flat);self.assertIn('2 files',flat);self.assertIn('1.00 GiB',flat)
        self.assertIn(REVISION,output.replace(' ', '').replace('\n',''))
        self.assertIn('node-1',flat);self.assertIn('--json',flat)
        self.assertNotIn('Acquisition complete',flat)

    def test_human_output_names_nodes_by_saved_hostname(self):
        names=NodeNames({'node-0':'spark-a','node-1':'spark-b'})
        rows=[dict(rank=0,node_id='node-0',path=PATH,pinned=False),dict(rank=1,node_id='node-9',path=PATH,pinned=True)]
        _,flat=self.display(dict(kind='pulsar-prepared-set',spec_id=SPEC,home=HOME,ranks=rows,
                                 blockers=['node-1: disk is full']),'info',names)
        self.assertIn('Home node spark-b',flat);self.assertIn('Rank 0 spark-a',flat)
        # A node missing from the saved topology stays visible as unknown.
        self.assertIn('node-9 (not in saved topology)',flat)
        _,flat=self.display(dict(kind='pulsar-acquisition-plan',model_id='example/model',selected_node='node-1',
                                 blockers=['node-1: disk is full'],existing_homes=[]),'acquire',names)
        self.assertIn('Destination spark-b',flat);self.assertIn('Blocker spark-b: disk is full',flat)

    def test_saved_hostnames_reads_topology_and_tolerates_absence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'topology.json'
            self.assertEqual(saved_hostnames(path),{})
            path.write_text(json.dumps({'nodes':[{'rank':0,'node_id':'node-0','hostname':'spark-a'}]}))
            self.assertEqual(saved_hostnames(path),{'node-0':'spark-a'})

    def test_saved_order_is_topology_order_and_tolerates_absence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'topology.json'
            self.assertEqual(saved_order(path),[])
            path.write_text(json.dumps({'nodes':[{'node_id':'node-1','hostname':'spark-b'},
                                                 {'node_id':'node-0','hostname':'spark-a'}]}))
            self.assertEqual(saved_order(path),['node-1','node-0'])

    def test_verified_reuse_does_not_claim_a_download(self):
        _,flat=self.display(dict(kind='pulsar-acquisition-result',reused=True,manifest=MANIFEST,home=HOME),'acquire')
        self.assertIn('existing files reused',flat);self.assertNotIn('downloaded',flat)
        self.assertIn('home recorded',flat)

    def test_prepared_slots_and_paths_do_not_claim_running_service(self):
        rows=[dict(rank=0,node_id='node-0',path=PATH,pinned=False),dict(rank=1,node_id='node-1',path=PATH+'/other',pinned=True)]
        output,flat=self.display(dict(kind='pulsar-prepared-set',spec_id=SPEC,snapshot_manifest_id=SNAPSHOT,home=HOME,ranks=rows),'info')
        for phrase in ('Rank 0','Rank 1','node-0','node-1','not pinned','pinned','Start is a separate operation'):
            self.assertIn(phrase,flat)
        self.assertIn(SPEC,output.replace(' ','').replace('\n',''))
        self.assertNotIn('server running',flat)
        _,flat=self.display(dict(prepared=2,spec_id=SPEC),'prepare')
        self.assertIn('2 serving ranks',flat);self.assertIn('Start is a separate operation',flat)

    def test_blocked_plan_exposes_reasons_and_dependencies(self):
        reason='A stopped container still references the model home; explicitly remove its container before retrying this operation.'
        _,flat=self.display(dict(kind='pulsar-home-removal-plan',eligible=False,home=HOME,blockers=[reason],dependent_spec_ids=[SPEC]),'remove')
        self.assertIn('blocked',flat);self.assertIn(reason,flat)
        self.assertNotIn('Home removed',flat)
        self.assertIn('Depends on',flat)

    def test_purge_preview_retains_incomplete_preparation_visibility(self):
        plan=dict(kind='pulsar-purge-plan',eligible=False,views=[],blockers=['Prepared files are pinned.'])
        _,flat=self.display(dict(plan=plan,incomplete_preparations=[dict(node_id='node-1',stage=PATH)]),'purge')
        self.assertIn('Incomplete preparations',flat);self.assertIn('Pending path',flat);self.assertIn('pinned',flat)

    def test_archive_presence_is_distinct_from_verified_contents(self):
        observation=dict(local_state='unknown',archive_state='present',prepared=dict(verified=0,required=2),blockers=[])
        _,flat=self.display(dict(spec_id=SPEC,observation=observation),'check')
        self.assertIn('directory present; contents not checked',flat)
        self.assertNotIn('archive verified',flat.lower())
        proof=dict(kind='pulsar-archive-verification',snapshot_manifest_id=SNAPSHOT,verified=True,file_count=2,total_bytes=1024)
        _,flat=self.display(proof,'archive')
        self.assertIn('Archive verified',flat);self.assertIn('expected file hashes',flat)
        proof['verified']=False
        _,flat=self.display(proof,'archive');self.assertNotIn('Archive verified',flat)

    def test_restore_and_movement_show_location_and_planned_route(self):
        _,flat=self.display(dict(home=HOME),'restore')
        self.assertIn('Restoration complete',flat);self.assertIn('Home node',flat);self.assertIn('Prepare',flat)
        _,flat=self.display(dict(kind='pulsar-home-move-plan',snapshot_manifest_id=SNAPSHOT,source_node='node-1',destination_node='node-2',transfer_route='controller-stream-relay'),'move')
        self.assertIn('Home movement preview',flat);self.assertIn('node-1',flat);self.assertIn('node-2',flat)
        self.assertIn('no controller model copy',flat)
        _,flat=self.display(dict(home=HOME,moved=False),'move');self.assertIn('already on the selected node',flat)

    def test_shared_purge_reports_retained_working_files(self):
        _,flat=self.display(dict(spec_id=SPEC,purged=True,shared_copies_retained=2),'purge')
        self.assertIn('Prepared bindings removed',flat)
        self.assertIn('2 retained for other recipes',flat)
        self.assertNotIn('Prepared copies removed',flat)

    def test_retention_success_names_what_was_preserved(self):
        _,flat=self.display(dict(spec_id=SPEC,pinned=False),'unpin');self.assertIn('copies unpinned',flat)
        _,flat=self.display(dict(spec_id=SPEC,purged=True,home_untouched=True,archive_untouched=True),'purge')
        self.assertIn('Home retained',flat);self.assertIn('Archive retained',flat)
        _,flat=self.display(dict(snapshot_manifest_id=SNAPSHOT,removed=True,archive_untouched=True),'remove')
        self.assertIn('Home removed',flat);self.assertIn('Archive retained',flat)

    def test_budget_labels_do_not_treat_missing_observation_as_zero(self):
        _,flat=self.display(dict(kind='pulsar-storage-budget',nodes=[dict(node_id='node-0',used=1073741824,available=20*1024**3,total=30*1024**3),dict(node_id='node-1',available=None)]),'budget')
        # TUI-80: one row per node in human units; what was not observed is unknown, never none.
        self.assertIn('Node Free Pulsar Can copy node-0 20.0 GiB 1.00 GiB unknown node-1 unknown unknown unknown',flat)
        self.assertNotIn('none',flat);self.assertNotIn('bytes',flat)

    def test_budget_table_names_what_each_node_can_still_copy(self):
        G=1024**3
        nodes=[dict(node_id='node-0',available=1900*G,used=450*G,total=4000*G,reserve=100*G,limit=2250*G,path='/fixture/views'),
               dict(node_id='node-1',available=200*G,used=300*G,total=4000*G,reserve=205*G,limit=400*G,path='/fixture/views')]
        names=NodeNames({'node-0':'spark-1','node-1':'spark-2'})
        stream=io.StringIO()
        render(dict(kind='pulsar-storage-budget',nodes=nodes),operation='budget',names=names,
               writer=TerminalWriter(width=80,stream=stream))
        lines=stream.getvalue().splitlines()
        self.assertEqual(lines[1:4],['Node     Disk free  Reserve  Pulsar files  Can still copy',
                                     'spark-1   1.86 TiB  100 GiB       450 GiB        1.76 TiB',
                                     'spark-2    200 GiB  205 GiB       300 GiB            none'])
        self.assertIn('spark-2 is 5.00 GiB past its reserve or copy budget.',lines)
        self.assertIn('Copy root: /fixture/views',lines)
        output,flat=self.display(dict(kind='pulsar-storage-budget',nodes=nodes),'budget',names)
        self.assertIn('Node Free Pulsar Can copy spark-1 1.86 TiB 450 GiB 1.76 TiB',flat)

    def test_restore_preview_distinguishes_payload_capacity_and_unknown_space(self):
        plan = dict(kind='pulsar-restore-plan', snapshot_manifest_id=SNAPSHOT,
                    selected_node='node-1', total_bytes=100, file_count=2,
                    destination_root='/fixture/homes', destination_space={'available': 80})
        before = copy.deepcopy(plan)
        _, flat = self.display(plan, 'restore')
        for phrase in ('2 files; 100 bytes', 'New copy 100 bytes', 'Existing files reused 0 bytes',
                       'Disk free 80 bytes', 'Disk after copy deficit 20 bytes', '/fixture/homes',
                       'space is not reserved'):
            self.assertIn(phrase, flat)
        self.assertEqual(plan, before)
        plan['destination_space'] = None
        _, flat = self.display(plan, 'restore')
        self.assertIn('Disk free not observed', flat)
        self.assertIn('Disk after copy not observed', flat)
        self.assertNotIn('Result blocked', flat)

    def test_preparation_aliases_count_one_payload_and_one_copy_per_node(self):
        budget = dict(available=1000, reserve=100, used=200, limit=1000, path='/fixture/views')
        member = dict(kind='pulsar-preparation-plan', spec_id=SPEC, snapshot_manifest_id=SNAPSHOT,
                      total_bytes=100, file_count=1, eligible=True, blockers=[],
                      actions=[dict(rank=0, node_id='node-0', action='home-view'),
                               dict(rank=1, node_id='node-1', action='copy')],
                      budgets={'node-0': budget, 'node-1': budget})
        plan = dict(kind='pulsar-preparation-set-plan', spec_id=SPEC, eligible=True, blockers=[],
                    snapshots=[dict(member, snapshot='target'), dict(member, snapshot='draft')])
        before = copy.deepcopy(plan)
        _, flat = self.display(plan, 'prepare', NodeNames({'node-0': 'spark-a', 'node-1': 'spark-b'}))
        self.assertIn('Unique snapshots 100 bytes', flat)
        self.assertEqual(flat.count('New copy 100 bytes'), 1)
        self.assertEqual(flat.count('Existing files used 100 bytes'), 1)
        self.assertIn('New copy 0 bytes', flat)
        self.assertIn('After planned copies 700 bytes', flat)
        self.assertEqual(flat.count('Storage estimate'), 1)
        self.assertEqual(plan, before)

    def test_combined_preparation_shows_deficit_and_observation_variation(self):
        first = dict(kind='pulsar-preparation-plan', snapshot='target', snapshot_manifest_id=SNAPSHOT,
                     total_bytes=60, actions=[dict(rank=0, node_id='node-0', action='copy')],
                     budgets={'node-0': dict(available=120, reserve=10, used=10, limit=110)})
        second = copy.deepcopy(first)
        second.update(snapshot='draft', snapshot_manifest_id='d' * 64, total_bytes=50)
        second['budgets']['node-0']['available'] = 110
        plan = dict(kind='pulsar-preparation-set-plan', snapshots=[first, second], eligible=False,
                    blockers=['node-0: insufficient combined snapshot copy budget or disk space'])
        _, flat = self.display(plan, 'prepare')
        self.assertIn('Unique snapshots 110 bytes', flat)
        self.assertIn('New copy 110 bytes', flat)
        self.assertIn('lowest observed headroom', flat)
        self.assertIn('After planned copies deficit 10 bytes', flat)
        self.assertIn('Result blocked', flat)
        self.assertIn('insufficient combined', flat)
        second['budgets']['node-0']['available'] = None
        _, flat = self.display(plan, 'prepare')
        self.assertIn('Disk after reserve not observed', flat)
        self.assertIn('After planned copies not observed', flat)

    def test_legacy_preparation_without_capacity_stays_unknown(self):
        _, flat = self.display(dict(kind='pulsar-preparation-plan', actions=[
            dict(rank=0, node_id='node-0', action='copy')]), 'prepare')
        self.assertIn('Unique snapshots not observed', flat)
        self.assertIn('New copy not observed', flat)
        self.assertIn('After planned copies not observed', flat)

    def test_inspection_uses_the_existing_preparation_budget_policy(self):
        gib = 1024**3
        space = dict(available=200 * gib, used=10 * gib, total=500 * gib)
        budget = storage_budget(space)
        self.assertEqual(budget['reserve'], 64 * gib)
        self.assertEqual(budget['limit'], 146 * gib)
        self.assertEqual(storage_budget({**space, 'total': 2000 * gib})['reserve'], 100 * gib)
        configured = storage_budget(space, '0', '10', path='/fixture/views')
        self.assertEqual((configured['reserve'], configured['limit'], configured['path']), (0, 10, '/fixture/views'))
        with self.assertRaises(StorageError):
            storage_budget(space, '-1')
        with self.assertRaises(StorageError):
            storage_budget({**space, 'available': None})

    def test_cli_rejects_unrecognized_data_without_claiming_success(self):
        result=subprocess.run([sys.executable,'-m','model_library.render','--operation','prepare'],input=json.dumps(dict(prepared=True)),env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','COLUMNS':'44'},cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(result.returncode,2);self.assertNotIn('complete',result.stdout)


    def test_blocker_details_lead_with_the_cause_and_its_fix(self):
        names=NodeNames({'node-1':'spark-b'})
        container=dict(code='container-reference',message='a container references the prepared copy',node_id='node-1',
                       container=dict(id='c'*64,name='vllm',state='running',spec_id=SPEC),mount=PATH,path=PATH,
                       fix='stop the Pulsar service for spec '+SPEC[:12])
        document=dict(kind='pulsar-purge-plan',eligible=False,blockers=['raw planner message'],views=[],actions=[],
                      blocker_details=[container,dict(code='pinned',message='m',node_id='node-1',fix='unpin the prepared copies first')])
        _,flat=self.display(document,'purge',names)
        self.assertIn('Blocker running Pulsar container vllm (spec '+SPEC[:12]+') on spark-b mounts these files',flat)
        self.assertIn('Fix stop the Pulsar service for spec '+SPEC[:12],flat)
        self.assertIn('Blocker the prepared copy on spark-b is pinned Fix unpin the prepared copies first',flat)
        # Details replace the messages they explain.
        self.assertNotIn('raw planner message',flat)
        _,flat=self.display({**document,'blocker_details':[]},'purge',names)
        self.assertIn('Blocker raw planner message',flat)

    def test_container_blockers_say_how_the_mount_covers_the_files(self):
        from model_library.render import blocker_detail_text
        names=NodeNames({'node-1':'spark-b'})
        base=dict(code='container-reference',message='m',node_id='node-1',path='/hub/snapshots/rev')
        for mount,state,expected in (('/hub/snapshots/rev','stopped','stopped container x on spark-b mounts these files'),
                                     ('/hub','running','running container x on spark-b mounts /hub, which contains these files'),
                                     ('/hub/snapshots/rev/sub','other','state-unknown container x on spark-b mounts /hub/snapshots/rev/sub, inside these files')):
            with self.subTest(mount=mount):
                detail=dict(base,mount=mount,container=dict(id='1',name='x',state=state))
                self.assertEqual(blocker_detail_text(detail,names),expected)
        # A set plan's member names its snapshot once; unknown codes fall back to the message.
        detail=dict(code='insufficient-space',message='draft: node-1: insufficient combined snapshot copy budget or disk space',
                    node_id='node-1',snapshot='draft')
        self.assertEqual(blocker_detail_text(detail,names),'draft: not enough combined copy budget or disk space on spark-b')
        self.assertEqual(blocker_detail_text(dict(code='future-code',message='node-1: something new'),names),
                         'spark-b: something new')

if __name__=='__main__':unittest.main()
