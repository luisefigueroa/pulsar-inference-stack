"""End-to-end storage CLI contracts with synthetic nodes and real filesystem state."""
import copy
import json
import hashlib
import base64
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests/support")]
from model_library_fixture import Fixture, contained, node_request
from model_library.integrity import read_json, verify_tree
from model_library.state import Store, view_key
from model_library.local import copy_snapshot, payload
from release_spec import build_snapshot_manifest, pretty_json_bytes, spec_id_for, verify_spec
from release_spec.identity import argv_from_identity


class ModelLibraryCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, nodes=1):
        return Fixture(self.root, nodes)

    def success(self, result):
        self.assertEqual(result.returncode, 0, f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}')
        self.assertTrue(result.stdout.strip(), "successful storage operation returned no result")
        return json.loads(result.stdout)

    def failure(self, result, text=None):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        if text: self.assertIn(text, result.stderr + result.stdout)

    def acquire_candidate(self, fixture, *, nodes=1, home=0):
        result = self.success(fixture.acquire(home))
        fixture.candidate(nodes)
        return result

    def speculative_fixture(self, nodes=2, draft_home=0):
        from release_spec.serving import freeze
        f=self.fixture(nodes)
        self.acquire_candidate(f,nodes=nodes)
        target=copy.deepcopy(f.spec)
        target_manifest=target['recipe']['model']['snapshot_manifest']
        f.cfg['revision']='e'*40;f.save()
        f.manifest_path=f.root/'draft-manifest.json'
        second=self.success(f.acquire(draft_home))['manifest']
        draft={'schema_version':2,'kind':'pulsar-recipe-draft','source':target['source'],'recipe':copy.deepcopy(target['recipe'])}
        draft['recipe']['model'].pop('snapshot_manifest')
        draft['recipe']['required_snapshots']={'draft':{'model_id':second['model_id'],'model_commit':second['snapshot_revision']}}
        draft['recipe']['engine_args'] += ['--speculative_config.model','pulsar-snapshot:draft']
        f.spec=freeze(draft,{'target':target_manifest,'draft':second})
        f.spec_path.write_bytes(pretty_json_bytes(f.spec))
        return f

    def test_speculative_combined_budget_and_interrupted_preparation(self):
        f=self.speculative_fixture(draft_home=0)
        limit=max(m['snapshot_manifest']['total_bytes'] for m in
                  [f.spec['recipe']['model'],*f.spec['recipe']['required_snapshots'].values()])
        f.env['PULSAR_HOT_BUDGET_BYTES']=str(limit)
        before=len([e for e in f.events('node-operation') if e['operation']=='begin-view'])
        self.failure(f.run('prepare','--yes',spec=True),'combined')
        self.assertEqual(len([e for e in f.events('node-operation') if e['operation']=='begin-view']),before)
        self.assertEqual(Store(f.state).views(spec_id=f.spec['spec_id']),[])
        f.env['PULSAR_HOT_BUDGET_BYTES']=str(32*1024**2)
        f.cfg['node_fault']={'operation':'begin-view','rank':1,'after':True,
            'manifest_id':f.spec['recipe']['required_snapshots']['draft']['snapshot_manifest']['manifest_id']};f.save()
        self.failure(f.run('prepare','--yes',spec=True))
        self.assertEqual(len(Store(f.state).views(spec_id=f.spec['spec_id'])),2)
        target_transfers=f.events('transfer')
        self.assertTrue(target_transfers)
        target_sources={event['source'] for event in target_transfers}
        node_state=Store(Path(f.cfg['nodes'][1]['view_root'])/'.pulsar-node')
        pending=node_state.records('transactions')
        self.assertEqual(len(pending),1)
        stage=pending[0]['stage']
        self.failure(f.run('info',spec=True),'snapshot')
        f.cfg.pop('node_fault');f.save()
        self.success(f.run('prepare','--yes',spec=True))
        self.assertEqual(node_state.records('transactions'),[])
        self.assertFalse(Path(stage).exists())
        self.assertEqual([event for event in f.events('transfer') if event['source'] in target_sources],target_transfers)
        self.assertGreater(len(f.events('transfer')),len(target_transfers))
        self.assertEqual(len(Store(f.state).views(spec_id=f.spec['spec_id'])),4)

    def test_speculative_preparation_retention_and_archive_coverage(self):
        f=self.speculative_fixture(draft_home=1)
        self.failure(f.run('archive','create','--yes',spec=True),'--snapshot')
        self.success(f.run('prepare','--yes',spec=True))
        prepared=self.success(f.run('info','--full',spec=True))
        self.assertEqual(set(prepared['snapshots']),{'target','draft'})
        from scripts.container_runtime import prepared_snapshots
        prepared_snapshots(f.spec,prepared,f.topology['topology_id'])
        views=Store(f.state).views(spec_id=f.spec['spec_id'])
        self.assertEqual(len(views),4)
        self.assertEqual(len({(v['node_id'],v['path']) for v in views}),4)
        self.success(f.run('pin','--yes',spec=True))
        self.assertTrue(all(v['pinned'] for v in Store(f.state).views(spec_id=f.spec['spec_id'])))
        self.failure(f.run('purge','--yes',spec=True),'pinned')
        self.success(f.run('unpin','--yes',spec=True))
        draft_view=next(v for v in views if v['snapshot_manifest_id']==f.spec['recipe']['required_snapshots']['draft']['snapshot_manifest']['manifest_id'])
        rank=next(n for n in f.cfg['nodes'] if n['node_id']==draft_view['node_id'])
        rank['containers']=[{'Id':'stopped-draft','State':{'Running':False},'Config':{'Labels':{}},'Mounts':[{'Source':draft_view['path']}]}];f.save()
        self.failure(f.run('purge','--yes',spec=True),'container')
        rank['containers']=[];f.save()
        self.success(f.run('archive','create','--snapshot','target','--yes',spec=True))
        self.failure(f.run('archive','verify',spec=True),'draft')
        self.success(f.run('archive','create','--snapshot','draft','--yes',spec=True))
        proof=self.success(f.run('archive','verify',spec=True))
        self.assertEqual(set(proof['snapshots']),{'target','draft'})
        observed=self.success(f.run('check','--full',spec=True))['observation']
        self.assertEqual(set(observed['snapshots']),{'target','draft'})
        self.assertEqual(observed['local_state'],'ready')
        self.assertEqual(observed['archive_state'],'verified')
        self.success(f.run('purge','--yes',spec=True))
        self.assertEqual(Store(f.state).views(spec_id=f.spec['spec_id']),[])
        self.assertEqual(len(Store(f.state).records('homes')),2)
        self.success(f.run('remove','--snapshot','draft','--yes',spec=True))
        self.assertIsNotNone(Store(f.state).home(f.spec['recipe']['model']['snapshot_manifest']['manifest_id']))
        self.success(f.run('restore','--snapshot','draft','--node','1','--yes',spec=True))
        self.assertEqual(len(Store(f.state).records('homes')),2)

    def test_fixture_refuses_external_data_before_node_execution(self):
        f = self.fixture()
        with patch.dict("os.environ", f.env):
            for path in ("/var/tmp/pulsar-hot", str(ROOT / ".model-library"), "/outside-model-data"):
                with self.assertRaisesRegex(RuntimeError, "outside temporary root"):
                    contained(path)
                with self.assertRaises(RuntimeError):
                    node_request({"path": path}, f.cfg, f.cfg["nodes"][0])

    def catalog(self, fixture):
        # A synthetic catalog fixture exercises retention without metadata gates.
        # This is never published and does not claim physical qualification.
        spec = copy.deepcopy(fixture.spec)
        spec = verify_spec(spec)
        (fixture.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))

    def test_single_node_full_storage_lifecycle(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        home_path = Path(acquired["home"]["path"])
        prepared = self.success(f.run("prepare", "--yes", spec=True))
        self.assertEqual(prepared["prepared"], 1)
        info = self.success(f.run("info", "--full", spec=True))
        self.assertEqual(info["home_node_id"], "node-0")
        self.assertEqual(len(info["ranks"]), 1)
        self.success(f.run("pin", "--yes", spec=True))
        self.failure(f.run("purge", "--yes", spec=True), "pinned")
        self.assertTrue(home_path.is_dir())
        # Preparing identical pinned bytes must preserve the pin and reuse them.
        self.success(f.run("prepare", "--yes", spec=True))
        self.assertTrue(self.success(f.run("info", spec=True))["ranks"][0]["pinned"])
        self.success(f.run("unpin", "--yes", spec=True))
        self.success(f.run("purge", "--yes", spec=True))
        self.assertTrue(home_path.is_dir())
        archive = self.success(f.run("archive", "create", "--yes", spec=True))
        self.assertTrue(archive["verified"])
        self.success(f.run("archive", "verify", spec=True))
        self.catalog(f)
        self.success(f.run("remove", "--yes", spec=True))
        self.assertFalse(home_path.exists())
        self.assertIsNone(Store(f.state).home(f.spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"]))
        # Restoration uses only the frozen spec and archive; HF is unavailable.
        f.cfg["hub_unavailable"] = True; f.save()
        downloads = len(f.events("download"))
        shutil.rmtree(f.state)
        self.success(f.run("restore", f.spec["spec_id"], "--node", "node-0", "--yes"))
        self.assertEqual(len(f.events("download")), downloads)
        self.assertTrue(home_path.is_dir())
        self.success(f.run("prepare", "--yes", spec=True))
        self.success(f.run("info", "--full", spec=True))
        self.assertFalse(any("receipt" in str(p.relative_to(f.state)) for p in f.state.rglob("*")))

    def test_insufficient_copy_budget_reports_blocker_without_publishing(self):
        f=self.fixture(nodes=2)
        self.acquire_candidate(f,nodes=2)
        f.env['PULSAR_HOT_BUDGET_BYTES']='0'
        result=f.run('prepare','--yes',spec=True)
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        plan=json.loads(result.stdout)
        self.assertFalse(plan['eligible'])
        self.assertTrue(any('insufficient copy budget' in item for item in plan['blockers']))
        self.assertEqual(Store(f.state).views(spec_id=f.spec['spec_id']),[])
        self.assertEqual(f.events('transfer'),[])

    def test_two_nodes_with_remote_home(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2, home=1)
        self.assertEqual(acquired["home"]["node_id"], "node-1")
        self.success(f.run("prepare", "--yes", spec=True))
        info = self.success(f.run("info", "--full", spec=True))
        self.assertEqual([r["node_id"] for r in info["ranks"]], ["node-0", "node-1"])
        self.assertTrue(info["ranks"][1]["is_home_view"])
        self.assertNotEqual(info["ranks"][0]["path"], info["ranks"][1]["path"])
        self.assertTrue(f.events("transfer"))
        self.success(f.run("archive", "create", "--yes", spec=True))
        self.success(f.run("archive", "verify", spec=True))
        self.success(f.run("purge", "--yes", spec=True))
        self.success(f.run("remove", "--yes", spec=True))
        f.cfg["hub_unavailable"] = True; f.save()
        self.success(f.run("restore", "--node", "node-1", "--yes", spec=True))
        self.success(f.run("prepare", "--yes", spec=True))

    def test_source_plan_is_not_acquisition_and_execution_requires_exact_commit(self):
        f = self.fixture(nodes=2)
        plan = self.success(f.run("acquire", "--model-id", f.cfg["model_id"], "--revision", "main", "--plan"))
        self.assertEqual(plan["snapshot_revision"], f.cfg["revision"])
        self.assertFalse(f.events("download"))
        self.assertEqual(Store(f.state).records("homes"), [])
        self.failure(f.run("acquire", "--model-id", f.cfg["model_id"], "--revision", "main", "--yes"), "exact commit")
        self.failure(f.run("acquire", "--model-id", f.cfg["model_id"], "--revision", f.cfg["revision"]), "requires --yes")
        self.assertFalse(f.events("download"))

    def test_verified_reuse_does_not_download_again(self):
        f = self.fixture(nodes=2)
        first = self.acquire_candidate(f, nodes=2, home=1)
        self.assertEqual(len(f.events("download")), 1)
        again = self.success(f.run("acquire", "--node", "node-1", "--yes", spec=True))
        self.assertEqual(again["home"]["path"], first["home"]["path"])
        self.assertEqual(len(f.events("download")), 1)
        self.failure(f.run("acquire", "--node", "node-0", "--yes", spec=True), "explicit move")
        self.assertEqual(len(f.events("download")), 1)

    def test_missing_or_changed_bytes_never_trigger_download_fallback(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        self.success(f.run("prepare", "--yes", spec=True))
        path = Path(acquired["home"]["path"])
        (path / "unexpected").write_bytes(b"extra")
        count = len(f.events("download"))
        self.failure(f.run("info", spec=True))
        self.failure(f.run("prepare", "--yes", spec=True))
        self.assertEqual(len(f.events("download")), count)
        (path / "unexpected").unlink()
        (path / "weights.bin").unlink()
        self.failure(f.run("info", "--full", spec=True))
        self.assertEqual(len(f.events("download")), count)

    def test_unreachable_node_blocks_acquisition_and_destructive_actions(self):
        f = self.fixture(nodes=2)
        f.cfg["nodes"][1]["available"] = False; f.save()
        self.failure(f.acquire())
        self.assertFalse(f.events("download"))
        f.cfg["nodes"][1]["available"] = True; f.save()
        acquired = self.acquire_candidate(f, nodes=2)
        self.success(f.run("prepare", "--yes", spec=True))
        f.cfg["nodes"][1]["available"] = False; f.save()
        self.failure(f.run("purge", "--yes", spec=True))
        self.failure(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        self.assertTrue(Path(acquired["home"]["path"]).exists())

    def test_stopped_container_reference_blocks_purge(self):
        f = self.fixture()
        self.acquire_candidate(f)
        self.success(f.run("prepare", "--yes", spec=True))
        info = self.success(f.run("info", spec=True))
        f.cfg["nodes"][0]["containers"] = [{"Id": "synthetic-stopped-container", "State": {"Running": False},
            "Mounts": [{"Source": info["ranks"][0]["hub_path"]}], "Config": {"Labels": {}}}]
        f.save()
        self.failure(f.run("purge", "--yes", spec=True), "container")
        self.assertEqual(len(Store(f.state).views(spec_id=f.spec["spec_id"])), 1)

    def test_catalog_home_cannot_be_discarded_without_archive(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        self.catalog(f)
        self.failure(f.run("remove", "--yes", spec=True), "archive")
        self.failure(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        self.assertTrue(Path(acquired["home"]["path"]).exists())

    def test_archive_corruption_blocks_removal_and_no_delete_interface_exists(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        self.success(f.run("archive", "create", "--yes", spec=True))
        self.catalog(f)
        archive_file = next(f.archive.rglob("weights.bin")); archive_file.write_bytes(b"corrupt")
        self.failure(f.run("archive", "verify", spec=True))
        self.failure(f.run("remove", "--yes", spec=True))
        self.failure(f.run("archive", "delete", "--yes", spec=True))
        self.assertTrue(archive_file.exists())
        self.assertTrue(Path(acquired["home"]["path"]).exists())

    def test_archive_verify_is_read_only(self):
        f = self.fixture()
        self.acquire_candidate(f)
        self.success(f.run("archive", "create", "--yes", spec=True))
        manifest_id = f.spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"]
        store = Store(f.state)
        store.remove("archives", manifest_id)
        before = sorted(path.relative_to(f.state) for path in f.state.rglob("*"))
        verified = self.success(f.run("archive", "verify", spec=True))
        after = sorted(path.relative_to(f.state) for path in f.state.rglob("*"))
        self.assertTrue(verified["verified"])
        self.assertEqual(after, before)
        self.assertIsNone(Store(f.state).get("archives", manifest_id))

    def test_lab_discard_requires_explicit_acknowledgement(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        self.failure(f.run("remove", "--yes", spec=True), "discard")
        self.success(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        self.assertFalse(Path(acquired["home"]["path"]).exists())
        self.assertTrue(f.spec_path.exists())

    def test_unarchived_lab_discard_with_no_archive_configuration(self):
        f = self.fixture()
        acquired = self.acquire_candidate(f)
        f.env["PULSAR_COLD_ROOT"] = ""
        self.success(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        self.assertFalse(Path(acquired["home"]["path"]).exists())

    def test_check_saves_preparation_and_archive_presence_separately(self):
        f = self.fixture()
        self.acquire_candidate(f)
        unprepared = f.run("check", spec=True)
        self.failure(unprepared)
        before = json.loads(unprepared.stdout)["observation"]
        self.assertNotEqual(before["local_state"], "ready")
        self.success(f.run("prepare", "--yes", spec=True))
        self.success(f.run("archive", "create", "--yes", spec=True))
        observed = self.success(f.run("check", spec=True))["observation"]
        self.assertEqual(observed["local_state"], "ready")
        self.assertEqual(observed["archive_state"], "present")
        saved = Store(f.state).get("observations", f.spec["spec_id"])
        self.assertEqual(saved["local_state"], "ready")
        self.assertIn("checked_at", saved)

    def test_movement_requires_dependencies_cleared(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        self.success(f.run("prepare", "--yes", spec=True))
        self.failure(f.run("move", "--node", "node-1", "--yes", spec=True))
        self.success(f.run("purge", "--yes", spec=True))
        moved = self.success(f.run("move", "--node", "node-1", "--yes", spec=True))
        self.assertEqual(moved["home"]["node_id"], "node-1")
        self.assertFalse(Path(acquired["home"]["path"]).exists())
        self.success(f.run("prepare", "--yes", spec=True))

    def test_wrong_expected_git_sha256_never_publishes_a_home(self):
        f = self.fixture()
        files = [{"path": name, "size": len(base64.b64decode(row["data"])),
                  "sha256": hashlib.sha256(base64.b64decode(row["data"])).hexdigest()}
                 for name, row in sorted(f.cfg["files"].items())]
        files[0]["sha256"] = "e" * 64
        manifest = build_snapshot_manifest(model_id=f.cfg["model_id"], snapshot_revision=f.cfg["revision"], files=files)
        f.manifest_path.write_bytes(pretty_json_bytes(manifest))
        f.candidate()
        self.failure(f.run("acquire", "--node", "node-0", "--yes", spec=True))
        self.assertEqual(Store(f.state).records("homes"), [])
        parent = Path(f.cfg["nodes"][0]["home_root"]) / "pulsar-homes"
        published = [p for p in parent.iterdir() if not p.name.startswith(".pending-")] if parent.exists() else []
        self.assertEqual(published, [])

    def test_lost_prepared_publication_reply_blocks_removal_until_explicit_cleanup(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        f.cfg["node_fault"] = {"operation": "publish-view", "rank": 1, "after": True}; f.save()
        self.failure(f.run("prepare", "--yes", spec=True))
        self.failure(f.run("info", spec=True))
        self.failure(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        self.assertTrue(Path(acquired["home"]["path"]).exists())
        f.cfg.pop("node_fault"); f.save()
        self.success(f.run("purge", "--yes", spec=True))
        self.success(f.run("prepare", "--yes", spec=True))
        self.assertEqual(len(self.success(f.run("info", "--full", spec=True))["ranks"]), 2)

    def test_partial_pin_remains_protected_and_explicit_retry_recovers(self):
        f = self.fixture(nodes=2)
        self.acquire_candidate(f, nodes=2)
        self.success(f.run("prepare", "--yes", spec=True))
        f.cfg["node_fault"] = {"operation": "pin-view", "rank": 1, "after": True}; f.save()
        self.failure(f.run("pin", "--yes", spec=True))
        node_state = Store(Path(f.cfg["nodes"][1]["view_root"]) / ".pulsar-node")
        self.assertTrue(any(row["pinned"] for row in node_state.views(spec_id=f.spec["spec_id"])))
        self.failure(f.run("purge", "--yes", spec=True), "pinned")
        f.cfg.pop("node_fault"); f.save()
        self.success(f.run("pin", "--yes", spec=True))
        self.assertTrue(all(row["pinned"] for row in self.success(f.run("info", spec=True))["ranks"]))
        self.success(f.run("unpin", "--yes", spec=True))
        self.success(f.run("purge", "--yes", spec=True))

    def test_overlay_home_root_and_offline_verified_reuse(self):
        f = self.fixture()
        files = [{"path": name, "size": len(base64.b64decode(row["data"])),
                  "sha256": hashlib.sha256(base64.b64decode(row["data"])).hexdigest()}
                 for name, row in sorted(f.cfg["files"].items())]
        manifest = build_snapshot_manifest(model_id=f.cfg["model_id"], snapshot_revision=f.cfg["revision"], files=files)
        f.manifest_path.write_bytes(pretty_json_bytes(manifest)); f.candidate()
        selected = f.root / "selected-model-storage"
        f.cfg["permitted_roots"].append(str(selected)); f.save()
        overlay_path = f.root / "overlay.json"
        overlay = json.loads(overlay_path.read_text()); overlay["defaults"]["cache_root"] = str(selected)
        overlay_path.write_text(json.dumps(overlay))
        result = self.success(f.run("acquire", "--yes", spec=True))
        self.assertTrue(Path(result["home"]["path"]).is_relative_to(selected))
        f.cfg["hub_unavailable"] = True; f.save()
        reused = self.success(f.run("acquire", "--yes", spec=True))
        self.assertEqual(result["home"]["path"], reused["home"]["path"])
        self.assertEqual(len(f.events("download")), 1)

    def test_controller_home_publication_failure_is_not_false_success(self):
        f = self.fixture()
        f.cfg["controller_fault"] = "save-home"; f.save()
        failed = f.acquire()
        self.failure(failed)
        self.assertEqual(Store(f.state).records("homes"), [])
        root = Path(f.cfg["nodes"][0]["home_root"]) / "pulsar-homes"
        manifests = list(root.glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = read_json(manifests[0])
        verify_tree(manifests[0].parent / "snapshots" / manifest["snapshot_revision"], manifest)
        f.cfg.pop("controller_fault"); f.save()
        self.success(f.acquire())
        self.assertEqual(len(f.events("download")), 1)
        self.assertEqual(len(Store(f.state).records("homes")), 1)

    def test_controller_prepared_publication_failure_retains_node_proof(self):
        f = self.fixture(nodes=2)
        self.acquire_candidate(f, nodes=2)
        f.cfg["controller_fault"] = "save-views"; f.save()
        failed = f.run("prepare", "--yes", spec=True)
        self.failure(failed)
        self.assertNotIn('"prepared": 2', failed.stdout)
        self.assertEqual(Store(f.state).views(spec_id=f.spec["spec_id"]), [])
        for node in f.cfg["nodes"]:
            records = Store(Path(node["view_root"]) / ".pulsar-node").views(spec_id=f.spec["spec_id"])
            self.assertEqual(len(records), 1)
            verify_tree(records[0]["path"], f.spec["recipe"]["model"]["snapshot_manifest"])
        self.failure(f.run("info", spec=True))
        self.failure(f.run("remove", "--yes", "--discard-unpromoted", spec=True))
        f.cfg.pop("controller_fault"); f.save()
        transfers = len(f.events("transfer"))
        self.success(f.run("prepare", "--yes", spec=True))
        self.assertEqual(len(f.events("transfer")), transfers)
        self.assertEqual(len(self.success(f.run("info", spec=True))["ranks"]), 2)

    def test_changed_metadata_is_rehashed_once_and_cached_without_transfer(self):
        f = self.fixture(nodes=2)
        self.acquire_candidate(f, nodes=2)
        self.success(f.run("prepare", "--yes", spec=True))
        info = self.success(f.run("info", spec=True))
        transfers = len(f.events("transfer"))
        for record in info["ranks"]:
            path = Path(record["path"]) / "weights.bin"
            observed = path.stat()
            os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))
        refreshed = self.success(f.run("info", spec=True))
        self.assertEqual([row["verification"]["method"] for row in refreshed["ranks"]], ["sha256", "sha256"])
        cached = self.success(f.run("info", spec=True))
        self.assertEqual([row["verification"]["method"] for row in cached["ranks"]], ["metadata", "metadata"])
        self.assertEqual(len(f.events("transfer")), transfers)
        node_store = Store(Path(f.cfg["nodes"][1]["view_root"]) / ".pulsar-node")
        self.assertEqual(node_store.views(spec_id=f.spec["spec_id"])[0]["verification"]["files"],
                         cached["ranks"][1]["verification"]["files"])

    def test_lost_registered_home_restores_on_another_confirmed_node(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        self.success(f.run("archive", "create", "--yes", spec=True))
        previous = Store(f.state).home(f.spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"])
        shutil.rmtree(acquired["home"]["hub_path"])
        f.cfg["hub_unavailable"] = True; f.save()
        restored = self.success(f.run("restore", "--node", "node-1", "--yes", spec=True))
        self.assertEqual(restored["home"]["node_id"], "node-1")
        registered = Store(f.state).home(f.spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"])
        self.assertNotEqual(registered["path"], previous["path"])
        verify_tree(registered["path"], f.spec["recipe"]["model"]["snapshot_manifest"])
        self.assertEqual(len(f.events("download")), 1)

    def test_move_retry_repairs_registration_after_source_retirement(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        f.cfg["controller_fault"] = "save-home"; f.save()
        self.failure(f.run("move", "--node", "node-1", "--yes", spec=True))
        self.assertFalse(Path(acquired["home"]["hub_path"]).exists())
        manifest = f.spec["recipe"]["model"]["snapshot_manifest"]
        destination = Path(f.cfg["nodes"][1]["home_root"]) / "pulsar-homes" / manifest["manifest_id"]
        verify_tree(payload(destination, manifest), manifest)
        self.assertEqual(Store(f.state).home(manifest["manifest_id"])["node_id"], "node-0")
        transfers = len(f.events("transfer"))
        f.cfg.pop("controller_fault"); f.save()
        repaired = self.success(f.run("move", "--node", "node-1", "--yes", spec=True))
        self.assertEqual(repaired["home"]["node_id"], "node-1")
        self.assertEqual(Store(f.state).home(manifest["manifest_id"])["path"], str(payload(destination, manifest)))
        self.assertEqual(len(f.events("transfer")), transfers)

    def test_home_rank_requires_actual_home_and_node_records_cannot_be_transplanted(self):
        f = self.fixture(nodes=2)
        acquired = self.acquire_candidate(f, nodes=2)
        self.success(f.run("prepare", "--yes", spec=True))
        manifest = f.spec["recipe"]["model"]["snapshot_manifest"]
        controller = Store(f.state)
        original = next(row for row in controller.views(spec_id=f.spec["spec_id"]) if row["node_id"] == "node-0")
        duplicate, stamp = copy_snapshot(Path(acquired["home"]["path"]),
            Path(f.cfg["nodes"][0]["view_root"]) / "duplicate", manifest)
        alternate = {**original, "hub_path": str(duplicate), "path": str(payload(duplicate, manifest)), "verification": stamp}
        controller.put("views", view_key(f.spec["spec_id"], "node-0"), alternate)
        self.failure(f.run("info", spec=True), "home directly")
        self.assertTrue(Path(acquired["home"]["path"]).exists())
        controller.put("views", view_key(f.spec["spec_id"], "node-0"), original)
        other_node = Store(Path(f.cfg["nodes"][1]["view_root"]) / ".pulsar-node")
        other_node.put("views", view_key(f.spec["spec_id"], "node-0"), original)
        self.failure(f.run("prepare", "--yes", spec=True))
        self.failure(f.run("purge", "--yes", spec=True))
        self.assertTrue(Path(acquired["home"]["path"]).exists())


if __name__ == "__main__": unittest.main()
