"""Every reader of the saved cluster, on a saved ring: one test each.

Synthetic ring fixtures and doubles only; nothing here contacts a node, Docker
or a GPU, and no test writes into the checkout."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'tests/support'), str(ROOT / 'scripts')]
from model_library.catalog import entries as catalog_entries
from model_library.node_names import NodeNames
from model_library.state import Store
from model_library_fixture import Fixture as LibraryFixture
from scripts import cluster_status, models_page, serve_plan, service_status, services_page
from scripts import topology_manifest as tm
from support.topology_fixture import Fixture, as_mesh, ring_probes, shuffled
from tests.test_ring_renumbering import save_ring

LABEL = 'io.pulsar.gb10.'
SINGLE = 'cd' * 32
KEYS = ['head', 'worker', 'rank-2', 'rank-3']


def flat(text):
    return ' '.join(text.split())


def ring(test, enrolled=True):
    return Fixture(Path(test.enterContext(tempfile.TemporaryDirectory())), probes=shuffled(ring_probes(4)),
                   enrolled=enrolled)


def ring_names(fixture):
    nodes = fixture.topology['nodes']
    return NodeNames({node['node_id']: node['hostname'] for node in nodes}, [node['node_id'] for node in nodes],
                     tm.RING_CLASS)


def inventory(fixture, services=()):
    """A saved service inventory of the ring, as ./pulsar inventory --json reports it."""
    nodes = {KEYS[node['rank']]: {'node_id': node['node_id'], 'hostname': node['hostname'],
                                  'control_ip': node['control']['ip'], 'local': node['rank'] == 0,
                                  'confirmed': True, 'probe_status': 'ok', 'topology_index': node['rank'],
                                  'mem_available_gib': 100.0, 'mem_total_gib': 121.69, 'mem_status': 'ok'}
             for node in fixture.topology['nodes']}
    return {'schema_version': 1, 'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'nodes': nodes, 'worker': {'status': 'ok'}, 'unmanaged_gpu_processes': [], 'services': list(services)}


def one_node_service(key, port=8000):
    """A one-node service on the node at KEY, the opposite of this node on the ring."""
    return {'service_id': SINGLE, 'conf': SINGLE, 'state': 'running', 'ownership': 'managed', 'safe_to_stop': True,
            'matches_selected_spec': True, 'served_name': 'served-model', 'api_port': port, 'reasons': [],
            'ranks': [{'rank': 'single', 'node': key, 'observed_api_port': port,
                       'labels': {LABEL + 'managed': 'true'}}]}


class RecordingSshIdentities(unittest.TestCase):
    def test_the_identities_of_a_saved_ring_are_recorded_after_its_save(self):
        fixture = ring(self, enrolled=False)
        result = fixture.run('ssh-trust', 'enroll', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        states = [line.split()[-1] for line in result.stdout.splitlines() if line.strip().startswith('State')]
        self.assertEqual(states, ['new'] * 4)
        saved = json.loads(fixture.path.read_text())
        self.assertEqual((saved['schema_version'], saved['validation']['class']), (2, tm.RING_CLASS))
        tm.validate_manifest(saved, require_verified=True, require_ssh_trust=True)
        # Each node's identity is checked over the links of its own cables.
        self.assertEqual(json.loads(fixture.run('ssh-trust', 'check', '--json').stdout)['state'], 'pass')


class StorageImpact(unittest.TestCase):
    def test_saving_a_ring_over_a_mesh_names_no_removed_node(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        probes = ring_probes(4)
        (directory / 'mesh').mkdir()
        (directory / 'ring').mkdir()
        mesh = Fixture(directory / 'mesh', probes=as_mesh(probes[:3]))
        new = Fixture(directory / 'ring', probes=shuffled(probes))
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/topology_storage_impact.py'), '--old',
                                 str(mesh.path), '--new', str(new.path), '--state-root', str(directory / 'library'),
                                 '--json'], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        impact = json.loads(result.stdout)
        self.assertEqual((impact['topology_changed'], impact['removed_node_ids'], impact['homes_on_removed_nodes']),
                         (True, [], []))


class ForgetAndItsBackup(unittest.TestCase):
    def test_forgetting_a_ring_keeps_a_private_backup(self):
        fixture = ring(self)
        saved = fixture.path.read_bytes()
        result = fixture.run('topology', 'forget', '--yes', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document['result'], 'forgotten')
        self.assertEqual(len(document['nodes']), 4)
        self.assertFalse(fixture.path.exists())
        backups = list((fixture.root / 'state/cluster-backups').rglob('*'))
        self.assertIn(saved, [path.read_bytes() for path in backups if path.is_file()])


class InventoryAndStatus(unittest.TestCase):
    def test_inventory_names_the_node_a_service_runs_on(self):
        fixture = ring(self)
        nodes = fixture.topology['nodes']
        raw = {'profiles': {}, 'topology_id': fixture.topology['topology_id'], 'worker_status': 'ok',
               'gpu_processes': [], 'containers': [],
               'nodes': {KEYS[node['rank']]: dict(node_id=node['node_id'], hostname=node['hostname'], confirmed=True,
                                                  probe_status='ok', mem_available_gib=100, mem_total_gib=121,
                                                  mem_status='ok', topology_index=node['rank']) for node in nodes}}
        path = fixture.root / 'raw.json'
        path.write_text(json.dumps(raw))
        result = fixture.run('inventory', '--from-fixture', str(path), '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual({key: node['node_id'] for key, node in document['nodes'].items()},
                         {KEYS[node['rank']]: node['node_id'] for node in nodes})
        self.assertEqual(document['services'], [])

    def test_status_finds_a_one_node_service_on_the_opposite_node(self):
        fixture = ring(self)
        opposite = fixture.topology['nodes'][2]
        result = service_status.from_inventory(SINGLE, inventory(fixture, [one_node_service('rank-2')]), '')
        self.assertIn(opposite['control']['ip'], json.dumps(result))


class ModelFilesAndBudget(unittest.TestCase):
    def test_model_files_and_the_storage_budget_read_a_saved_ring(self):
        f = LibraryFixture(Path(self.enterContext(tempfile.TemporaryDirectory())), 4)
        self.assertEqual(f.acquire(0).returncode, 0)
        save_ring(f, [0, 2, 1, 3])
        files = f.run('files')
        self.assertEqual(files.returncode, 0, files.stderr)
        self.assertEqual({node['node_id'] for node in json.loads(files.stdout)['nodes']},
                         {node['node_id'] for node in f.topology['nodes']})
        budget = f.run('budget')
        self.assertEqual(budget.returncode, 0, budget.stderr)
        self.assertEqual(len(json.loads(budget.stdout)['nodes']), 4)


class Pages(unittest.TestCase):
    """Home, Cluster…, Serve a model…, Running services… and Models and disk space…,
    with the catalog's three-node recipes present, on a saved ring."""

    def setUp(self):
        self.fixture = ring(self)
        self.names = ring_names(self.fixture)
        self.entries = catalog_entries(ROOT, Store(Path(self.enterContext(tempfile.TemporaryDirectory()))))
        self.sizes = {row['geometry']['nodes'] for row in self.entries}
        self.assertIn(3, self.sizes, 'the catalog has a three-node recipe')

    def records(self):
        environ = {'CLUSTER_TOPOLOGY_FILE': str(self.fixture.path),
                   'PULSAR_CLUSTER_CHANGES_FILE': str(self.fixture.root / 'state/cluster-changes.json'),
                   'PULSAR_SERVICE_INVENTORY_FILE': str(self.fixture.root / 'state/service-inventory.json'),
                   'PULSAR_STORAGE_BUDGET_FILE': str(self.fixture.root / 'state/storage-budget.json')}
        self.enterContext(mock.patch.dict(os.environ, environ))
        return cluster_status.Records(self.fixture.root, environ={'PULSAR_COLD_ROOT': ''})

    def test_home(self):
        text = flat(cluster_status.home_block(self.records(), self.entries, width=80))
        self.assertIn('4 nodes', text)
        self.assertNotIn('full mesh', text)

    def test_cluster(self):
        text = flat(cluster_status.frame(self.records(), width=80))
        self.assertIn('Nodes 4 · ring · ', text)

    def test_serve_a_model(self):
        lines = serve_plan.list_lines(self.entries, width=80, inventory=None, archive_location='disabled',
                                      order=self.names.order, repo=ROOT, ring=self.names.ring)
        text = flat('\n'.join(line.split('\t')[-1] for line in lines))
        for row in self.entries:
            if row.get('historical') or serve_plan.start_unsupported(row):
                continue
            nodes = row['geometry']['nodes']
            phrase = serve_plan.readiness(row, order=self.names.order, archive_location='disabled', sizes={},
                                          service=None, ring=True)
            if nodes >= 3:
                self.assertEqual(phrase, serve_plan.READINESS['ring'], row['spec_id'])
            elif nodes == 2:
                self.assertEqual(phrase, serve_plan.READINESS['ring-pair'], row['spec_id'])
            else:
                self.assertNotIn('ring', phrase)
        self.assertIn(serve_plan.READINESS['ring'], text)
        self.assertNotIn('needs 3 nodes; the cluster has', text)

    def test_running_services(self):
        document = inventory(self.fixture, [one_node_service('rank-2')])
        page = services_page.Page(document, [], names=self.names)
        text = flat(page.screen(SINGLE, width=80))
        self.assertIn(self.fixture.topology['nodes'][2]['hostname'], text)
        for node in self.fixture.topology['nodes'][:2] + self.fixture.topology['nodes'][3:]:
            self.assertNotIn(node['hostname'], text)

    def test_models_and_disk_space(self):
        rows = [row for row in self.entries if row['geometry']['nodes'] == 3][:1]
        cluster = models_page.Cluster(rows, {'schema_version': 1, 'kind': 'pulsar-model-file-inventory',
                                             'observed_at': None, 'nodes': [], 'snapshots': [], 'issues': []},
                                      [], names=self.names, archive_location='disabled')
        self.assertEqual(cluster.readiness(rows[0]['spec_id']), serve_plan.READINESS['ring'])


if __name__ == '__main__':
    unittest.main()
