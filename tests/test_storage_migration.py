"""One-time migration verifies bytes; legacy metadata never becomes authority."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release_spec import build_snapshot_manifest, pretty_json_bytes
from model_library import migration
from model_library.integrity import StorageError, verify_tree
from model_library.local import location, payload, verify_archive
from model_library.state import Store


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hub = self.root / 'legacy-hub'
        self.snapshot = self.hub / 'snapshots' / ('a' * 40)
        self.snapshot.mkdir(parents=True)
        (self.hub / 'blobs').mkdir()
        (self.hub / 'blobs' / 'weights').write_bytes(b'weights')
        (self.snapshot / 'model.safetensors').symlink_to('../../blobs/weights')
        (self.snapshot / 'config.json').write_bytes(b'{}')
        self.manifest = build_snapshot_manifest(model_id='example/model', snapshot_revision='a'*40,
            files=[{'path': 'config.json', 'size': 2, 'sha256': hashlib.sha256(b'{}').hexdigest()},
                   {'path': 'model.safetensors', 'size': 7, 'sha256': hashlib.sha256(b'weights').hexdigest()}])
        self.destination_root = self.root / 'new-root'
        self.destination_root.mkdir()
        self.state = self.root / 'new-state'
        self.metadata = self.hub / 'legacy-record.json'
        self.metadata.write_bytes(b'{"pinned":true,"legacy":"retained"}\n')
        self.metadata_bytes = self.metadata.read_bytes()
        self.args = dict(manifest=self.manifest, legacy_hub=self.hub,
            destination_root=self.destination_root, kind='home', node_id='fixture-node', state_root=self.state)

    def plan(self):
        return migration.preview(**self.args)

    def apply(self, plan=None, guard=None):
        return migration.apply(**self.args, expected_plan_digest=(plan or self.plan())['plan_digest'],
                               guard=guard or Mock())

    def test_preview_hashes_without_creating_new_state(self):
        plan = self.plan()
        self.assertEqual(plan['method'], 'hardlink')
        self.assertEqual(plan['bytes_to_copy'], 0)
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.destination_root.iterdir()), [])
        self.assertIn('not imported', plan['prepared_copies'])
        self.assertEqual(self.metadata.read_bytes(), self.metadata_bytes)

    def test_apply_resolves_blob_links_and_creates_verified_regular_home(self):
        guard = Mock()
        result = self.apply(guard=guard)
        self.assertEqual(guard.call_count, 2)
        home = Store(self.state).home(self.manifest['manifest_id'])
        new_payload = Path(home['path'])
        verify_tree(new_payload, self.manifest)
        self.assertFalse((new_payload / 'model.safetensors').is_symlink())
        self.assertEqual((new_payload / 'model.safetensors').stat().st_ino,
                         (self.hub / 'blobs/weights').stat().st_ino)
        self.assertEqual(self.metadata.read_bytes(), self.metadata_bytes)
        self.assertEqual(result['method'], 'hardlink')
        self.assertEqual(Store(self.state).views(), [])

    def test_archive_copies_independent_files_and_keeps_old_archive(self):
        self.args['kind'] = 'archive'
        plan = self.plan()
        self.assertEqual(plan['method'], 'copy')
        self.assertEqual(plan['bytes_to_copy'], 9)
        self.apply(plan)
        archive = payload(location(self.destination_root, self.manifest, archive=True), self.manifest)
        self.assertNotEqual((archive / 'model.safetensors').stat().st_ino,
                            (self.hub / 'blobs/weights').stat().st_ino)
        verify_archive(self.destination_root, self.manifest)
        (self.hub / 'blobs/weights').write_bytes(b'changed')
        verify_archive(self.destination_root, self.manifest)
        self.assertEqual(self.metadata.read_bytes(), self.metadata_bytes)

    def test_source_change_since_preview_requires_another_review(self):
        plan = self.plan()
        # Even identical bytes replaced into a new inode invalidate the reviewed plan.
        old = self.hub / 'blobs/weights'
        old.rename(self.hub / 'blobs/old')
        old.write_bytes(b'weights')
        with self.assertRaisesRegex(StorageError, 'plan changed'):
            self.apply(plan)
        self.assertFalse(location(self.destination_root, self.manifest).exists())

    def test_wrong_bytes_or_file_set_refuse_without_touching_source(self):
        (self.hub / 'blobs/weights').write_bytes(b'WRONG!!')
        with self.assertRaisesRegex(StorageError, 'hash differs'):
            self.plan()
        (self.hub / 'blobs/weights').write_bytes(b'weights')
        (self.snapshot / 'extra').write_bytes(b'x')
        with self.assertRaisesRegex(StorageError, 'file set differs'):
            self.plan()
        (self.snapshot / 'extra').unlink()
        (self.snapshot / 'config.json').unlink()
        with self.assertRaisesRegex(StorageError, 'file set differs'):
            self.plan()
        self.assertFalse(self.state.exists())

    def test_file_symlink_must_stay_within_explicit_hub(self):
        outside = self.root / 'external'
        outside.write_bytes(b'weights')
        (self.snapshot / 'model.safetensors').unlink()
        (self.snapshot / 'model.safetensors').symlink_to(outside)
        with self.assertRaisesRegex(StorageError, 'escapes'):
            self.plan()

    def test_directory_links_and_link_loops_are_refused(self):
        blob = self.hub / 'blobs'
        blob.rename(self.hub / 'real-blobs')
        blob.symlink_to('real-blobs', target_is_directory=True)
        with self.assertRaises(StorageError):
            self.plan()
        blob.unlink()
        (self.hub / 'real-blobs').rename(blob)
        leaf = self.snapshot / 'model.safetensors'
        leaf.unlink()
        leaf.symlink_to('model.safetensors')
        with self.assertRaisesRegex(StorageError, 'loops'):
            self.plan()

    def test_guard_refuses_active_or_stopped_dependency_before_materialization(self):
        guard = Mock(side_effect=StorageError('stopped container still mounts source'))
        with self.assertRaisesRegex(StorageError, 'stopped container'):
            self.apply(guard=guard)
        self.assertFalse(location(self.destination_root, self.manifest).exists())
        self.assertEqual(self.metadata.read_bytes(), self.metadata_bytes)

    def test_second_guard_blocks_publish_and_retains_only_owned_staging(self):
        guard = Mock(side_effect=[None, StorageError('container appeared')])
        with self.assertRaisesRegex(StorageError, 'owned staging retained'):
            self.apply(guard=guard)
        destination = location(self.destination_root, self.manifest)
        self.assertFalse(destination.exists())
        self.assertEqual(len(list(destination.parent.glob('.pending-*'))), 1)
        self.assertEqual(self.metadata.read_bytes(), self.metadata_bytes)
        self.assertIsNone(Store(self.state).home(self.manifest['manifest_id']))

    def test_existing_verified_destination_reuses_without_replacement(self):
        self.apply()
        target = payload(location(self.destination_root, self.manifest), self.manifest)
        inode = target.stat().st_ino
        self.assertEqual(self.plan()['method'], 'reuse-destination')
        self.apply()
        self.assertEqual(target.stat().st_ino, inode)

    def test_cross_filesystem_copy_is_explicit_in_reviewed_plan(self):
        verified = migration.verify_legacy(self.hub, self.snapshot, self.manifest)
        altered = copy.deepcopy(verified)
        for value in altered['files'].values():
            value['metadata'][0] += 1
        with patch.object(migration, 'verify_legacy', return_value=altered):
            plan = self.plan()
            self.assertEqual(plan['method'], 'copy')
            self.assertEqual(plan['bytes_to_copy'], 9)
            self.apply(plan)
        target = payload(location(self.destination_root, self.manifest), self.manifest)
        self.assertNotEqual((target / 'model.safetensors').stat().st_ino,
                            (self.hub / 'blobs/weights').stat().st_ino)

    def test_apply_refuses_legacy_state_location_before_creating_lock(self):
        self.args['state_root'] = self.hub / 'old-state'
        with self.assertRaisesRegex(StorageError, 'must not overlap'):
            migration.apply(**self.args, expected_plan_digest='a'*64, guard=Mock())
        self.assertFalse((self.hub / 'old-state').exists())

    def test_destination_inside_legacy_hub_is_refused(self):
        self.args['destination_root'] = self.hub
        with self.assertRaisesRegex(StorageError, 'overlap'):
            self.plan()


class ViewMigrationTests(unittest.TestCase):
    """Extend tiny snapshot fixture with known schema-3 prepared ownership."""
    def setUp(self):
        MigrationTests.setUp(self)
        from model_library import migration_views
        from release_spec import spec_id_for, verify_spec
        from release_spec.identity import argv_from_identity
        self.views_module = migration_views
        MigrationTests.apply(self)
        self.spec = json.loads((ROOT / 'release_spec/tests/fixtures/golden_measured.json').read_text())
        self.spec['identity']['model_id'] = self.manifest['model_id']
        self.spec['identity']['snapshot_manifest'] = self.manifest
        self.spec['identity']['geometry'].update(nodes=2, tp=2, fabric='roce-v2')
        self.spec['spec_id'] = spec_id_for(self.spec['identity'])
        self.spec['launch_contract']['argv'] = argv_from_identity(self.spec['identity'])
        self.spec['measurements'] = []
        self.spec['evidence'] = []
        self.spec = verify_spec(self.spec)
        self.hot_root = self.root / 'legacy-hot'
        self.old_topology = 'c' * 64
        identity = f"{self.manifest['model_id']}@{self.manifest['snapshot_revision']}"
        content = hashlib.sha256(f'{identity}|validation:receipt-occupancy|{self.manifest["manifest_id"]}'.encode()).hexdigest()[:12]
        self.instance = self.hot_root / f'legacy-profile-{self.old_topology[:12]}' / content
        self.old_view_hub = self.instance / 'hub/models--example--model'
        self.old_view_payload = payload(self.old_view_hub, self.manifest)
        self.old_view_payload.mkdir(parents=True)
        (self.old_view_payload / 'config.json').write_bytes(b'{}')
        (self.old_view_payload / 'model.safetensors').write_bytes(b'weights')
        self.stamp = {'schema_version': 3, 'state': 'pinned', 'profile': 'legacy-profile',
            'model_id': self.manifest['model_id'], 'revision': self.manifest['snapshot_revision'],
            'identity_key': identity, 'home_node_id': 'former-home', 'topology_id': self.old_topology,
            'content_id': content, 'content_digest': self.manifest['manifest_id'],
            'integrity': {'scheme': 'sha256-snapshot-manifest-v1', 'manifest': self.manifest},
            'validation': {'identity_status': 'receipt-occupancy', 'expected_seal': None,
                'observed_seal': {k: self.manifest[k] for k in ('model_id', 'snapshot_revision', 'manifest_id')}},
            'backend': 'rsync', 'bytes_logical': 9, 'activated_at': '2026-01-01T00:00:00Z',
            'pinned': True, 'budget_bytes_accounted': 9}
        self.stamp_file = self.instance / '.pulsar/hot.json'
        self.stamp_file.parent.mkdir()
        self.write_stamp()
        self.view_root = self.root / 'new-views'
        self.view_root.mkdir()
        self.view_args = dict(manifest=self.manifest, spec=self.spec, legacy_instance=self.instance,
            legacy_root=self.hot_root, legacy_topology_id=self.old_topology, topology_id='d'*64,
            node_id='fixture-worker', rank=1, destination_root=self.view_root, state_root=self.state)

    plan = MigrationTests.plan
    apply = MigrationTests.apply

    # Only explicit view scenarios run on this class.
    def write_stamp(self):
        self.stamp_file.write_bytes(pretty_json_bytes(self.stamp))

    def migrate_view(self, guard=None):
        plan = self.views_module.preview_view(**self.view_args)
        return self.views_module.apply_view(**self.view_args, expected_plan_digest=plan['plan_digest'], guard=guard or Mock())

    def test_pinned_view_retains_pin_and_new_purge_refuses_it(self):
        from model_library.planning import purge_plan
        original = self.stamp_file.read_bytes()
        result = self.migrate_view()
        view = Store(self.state).views()[0]
        self.assertTrue(result['pinned'])
        self.assertTrue(view['pinned'])
        self.assertFalse(view['is_home_view'])
        self.assertEqual(self.stamp_file.read_bytes(), original)
        plan = purge_plan(views=[view], node_ids=['fixture-worker'],
            observations=[{'node_id': 'fixture-worker', 'observable': True, 'containers': []}])
        self.assertFalse(plan['eligible'])
        self.assertIn('pinned', plan['blockers'][0])
        self.assertFalse((ROOT / 'releases' / (self.spec['spec_id'] + '.json')).exists())

    def test_unpinned_view_remains_unpinned(self):
        self.stamp.update(pinned=False, state='ready')
        self.write_stamp()
        self.migrate_view()
        self.assertFalse(Store(self.state).views()[0]['pinned'])

    def test_ambiguous_and_unknown_pin_state_refused(self):
        for update in ({'pinned': 'true'}, {'state': 'ready'}, {'schema_version': 2}):
            with self.subTest(update=update):
                original = copy.deepcopy(self.stamp)
                self.stamp.update(update)
                self.write_stamp()
                with self.assertRaises(StorageError):
                    self.views_module.preview_view(**self.view_args)
                self.stamp = original
        self.write_stamp()
        self.assertEqual(Store(self.state).views(), [])

    def test_wrong_legacy_layout_manifest_and_topology_refused(self):
        self.view_args['legacy_topology_id'] = 'e'*64
        with self.assertRaisesRegex(StorageError, 'topology'):
            self.views_module.preview_view(**self.view_args)
        self.view_args['legacy_topology_id'] = self.old_topology
        self.stamp['content_id'] = 'f'*12
        self.write_stamp()
        with self.assertRaisesRegex(StorageError, 'owned instance layout'):
            self.views_module.preview_view(**self.view_args)

    def test_stale_current_topology_node_guard_prevents_new_view(self):
        guard = Mock(side_effect=StorageError('current topology or selected rank/node changed'))
        with self.assertRaisesRegex(StorageError, 'current topology'):
            self.migrate_view(guard)
        self.assertEqual(Store(self.state).views(), [])
        self.assertEqual(list(self.view_root.iterdir()), [])

    def test_home_rank_uses_migrated_home_without_duplicate_payload(self):
        self.view_args.update(node_id='fixture-node', rank=0)
        result = self.migrate_view()
        home = Store(self.state).home(self.manifest['manifest_id'])
        view = Store(self.state).views()[0]
        self.assertEqual(result['method'], 'home-reference')
        self.assertTrue(view['is_home_view'])
        self.assertEqual(view['hub_path'], home['hub_path'])
        self.assertEqual(list(self.view_root.iterdir()), [])

    def test_home_link_requires_explicit_legacy_home_and_never_follows_arbitrary_link(self):
        import shutil
        shutil.rmtree(self.old_view_hub)
        self.old_view_hub.symlink_to(self.hub)
        self.view_args.update(node_id='fixture-node', rank=0)
        with self.assertRaisesRegex(StorageError, 'explicit legacy home'):
            self.views_module.preview_view(**self.view_args)
        self.view_args['legacy_home_hub'] = self.hub
        self.migrate_view()
        self.assertTrue(Store(self.state).views()[0]['is_home_view'])

    def test_view_pin_change_after_preview_requires_new_review(self):
        plan = self.views_module.preview_view(**self.view_args)
        self.stamp.update(pinned=False, state='ready')
        self.write_stamp()
        with self.assertRaisesRegex(StorageError, 'plan changed'):
            self.views_module.apply_view(**self.view_args, expected_plan_digest=plan['plan_digest'], guard=Mock())
        self.assertEqual(Store(self.state).views(), [])

if __name__ == '__main__':
    unittest.main()
