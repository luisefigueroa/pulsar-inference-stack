"""Human storage output preserves actionable facts and careful state boundaries."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from model_library.render import render
from model_library.node_names import NodeNames, saved_hostnames
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
        self.assertIn('Managed files',flat);self.assertIn('20.00 GiB',flat)
        self.assertIn('not observed',flat)

    def test_cli_rejects_unrecognized_data_without_claiming_success(self):
        result=subprocess.run([sys.executable,'-m','model_library.render','--operation','prepare'],input=json.dumps(dict(prepared=True)),env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','COLUMNS':'44'},cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(result.returncode,2);self.assertNotIn('complete',result.stdout)


if __name__=='__main__':unittest.main()
