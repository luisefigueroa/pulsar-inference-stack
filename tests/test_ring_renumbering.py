"""A ring save that renumbers nodes: prepared copies and the earlier cluster's
containers. Synthetic nodes, files and doubles only; nothing contacts a node."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'tests/support'), str(ROOT / 'scripts')]
from model_library_fixture import Fixture as LibraryFixture
from model_library.state import Store
from scripts import topology_manifest as tm
from support.topology_fixture import Fixture, ring_probes, shuffled
from tests import test_service_logs

SPEC = 'ab' * 32
LABEL = 'io.pulsar.gb10.'
EARLIER = 'e' * 64


def save_ring(fixture, order):
    """Save FIXTURE's nodes as a ring in ORDER (old positions, this node first),
    as a ring save that renumbers them would; the cables follow ORDER."""
    old = fixture.topology['nodes']
    nodes = [dict(old[index], rank=rank) for rank, index in enumerate(order)]
    address = lambda node: node['rdma'][0]['cidrs'][0].split('/')[0]
    count = len(nodes)
    pairs = sorted({tuple(sorted((rank, (rank + 1) % count))) for rank in range(count)})
    links = [dict(ranks=[a, b], rails=[dict(network='198.51.100.0/24',
             a=dict(hca='roce0', netdev='fabric0', ip=address(nodes[a])),
             b=dict(hca='roce0', netdev='fabric0', ip=address(nodes[b])))]) for a, b in pairs]
    topology = {**fixture.topology, 'nodes': nodes, 'links': links,
                'validation': {**fixture.topology['validation'], 'class': tm.RING_CLASS, 'full_mesh': False,
                               'min_rails_per_pair': 1}}
    topology['topology_id'] = tm.topology_digest(topology)
    tm.validate_manifest(topology, require_verified=True)
    fixture.topology = topology
    (fixture.root / 'topology.json').write_text(json.dumps(topology))
    fixture.cfg['nodes'] = [dict(fixture.cfg['nodes'][index], rank=rank) for rank, index in enumerate(order)]
    fixture.save()
    fixture.write_env()


class PreparedCopiesAfterARingSave(unittest.TestCase):
    """Prepare's take-over rewrites rank and topology_id of a copy that still verifies on the same node."""

    def test_a_renumbered_node_keeps_its_copy_under_its_new_rank(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        f = LibraryFixture(root, 4)
        result = f.acquire(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        f.candidate(3)
        self.assertEqual(f.run('prepare', '--yes', spec=True).returncode, 0)
        before = {v['node_id']: (v['rank'], v['topology_id'], v['path'])
                  for v in Store(f.state).views(spec_id=f.spec['spec_id'])}
        old = f.topology['topology_id']
        self.assertEqual({node: rank for node, (rank, _, _) in before.items()}, {'node-0': 0, 'node-1': 1, 'node-2': 2})
        transfers = f.events('transfer')
        # The ring's cables put node-2 at rank 1 and node-1 at rank 2.
        save_ring(f, [0, 2, 1, 3])
        plan = json.loads(f.run('prepare', '--plan', spec=True).stdout)
        self.assertTrue(plan['eligible'], plan['blockers'])
        self.assertEqual([(a['node_id'], a['action'], a['rank'], a['previous']) for a in plan['actions']],
                         [('node-0', 'rebind', 0, {'rank': 0, 'topology_id': old}),
                          ('node-2', 'rebind', 1, {'rank': 2, 'topology_id': old}),
                          ('node-1', 'rebind', 2, {'rank': 1, 'topology_id': old})])
        result = f.run('prepare', '--yes', spec=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {v['node_id']: (v['rank'], v['topology_id'], v['path'])
                 for v in Store(f.state).views(spec_id=f.spec['spec_id'])}
        new = f.topology['topology_id']
        self.assertEqual(after, {'node-0': (0, new, before['node-0'][2]), 'node-2': (1, new, before['node-2'][2]),
                                 'node-1': (2, new, before['node-1'][2])})
        # Records only: nothing was copied again.
        self.assertEqual(f.events('transfer'), transfers)


class EarlierContainersAfterARingSave(unittest.TestCase):
    """Exited containers of a service from the earlier cluster, after a ring
    save renumbered its nodes: each is found on its own node, none is shown as
    running, and nothing is removed without proof of ownership."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ring = ring_probes(4)
        listed = shuffled(self.ring)
        self.fixture = Fixture(self.root, probes=listed)
        docker = test_service_logs.DOCKER.replace('print("unexpected docker operation: "', '''if args[:1] == ["rm"]:
    with open(os.path.join(root, "removed.jsonl"), "a") as stream:
        stream.write(json.dumps([node, args]) + "\\n")
    sys.exit(0)
print("unexpected docker operation: "''')
        for name, body in (('docker', docker), ('ssh', test_service_logs.SSH)):
            (self.root / 'bin' / name).write_text(body)
            (self.root / 'bin' / name).chmod(0o755)
        self.index = {probe['node_id']: position for position, probe in enumerate(listed)}
        # The earlier mesh served on dorado, echo and bravo, in that order; the
        # ring now has bravo at position 1 and echo at position 2.
        self.earlier = [self.ring[0], self.ring[2], self.ring[1]]
        self.containers = []
        for rank, probe in enumerate(self.earlier):
            self.containers.append({'node': self.index[probe['node_id']], 'id': str(rank) * 64,
                                    'name': f'vllm-{rank}', 'state': 'exited', 'logs': [f'rank {rank}'],
                                    'stderr': [], 'labels': {
                                        LABEL + 'managed': 'true', LABEL + 'conf': SPEC, LABEL + 'rank': str(rank),
                                        LABEL + 'world-size': '3', LABEL + 'node-id': probe['node_id'],
                                        LABEL + 'topology': EARLIER}})
        (self.root / 'containers.json').write_text(json.dumps(self.containers))
        self.env = dict(self.fixture.env, LOGS_FIXTURE=str(self.root), COLUMNS='80')

    def run_pulsar(self, *args):
        return subprocess.run([str(ROOT / 'pulsar'), *args], env=self.env, text=True, capture_output=True,
                              timeout=60)

    def test_logs_find_each_container_on_its_own_node(self):
        result = self.run_pulsar('logs', SPEC)
        warnings = [line for line in result.stderr.splitlines() if line.startswith('warning: not reading')]
        self.assertEqual(sorted(warnings), sorted(
            f"warning: not reading container {str(rank) * 12} on {probe['hostname']}: topology label mismatch"
            for rank, probe in enumerate(self.earlier)))
        self.assertNotIn('running', result.stdout + result.stderr)

    def test_stop_removes_nothing_it_cannot_prove_and_names_the_node(self):
        result = self.run_pulsar('stop', SPEC)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"on {self.ring[0]['hostname']}: ownership not proven", result.stderr)
        self.assertFalse((self.root / 'removed.jsonl').exists())

    def test_inventory_shows_each_rank_on_the_node_it_runs_on_and_none_running(self):
        keys = ['head', 'worker', 'rank-2', 'rank-3']
        topology = self.fixture.topology
        position = {node['node_id']: node['rank'] for node in topology['nodes']}
        nodes = {keys[node['rank']]: dict(node_id=node['node_id'], hostname=node['hostname'], confirmed=True,
                                          probe_status='ok', mem_available_gib=120, mem_total_gib=128,
                                          mem_status='ok', topology_index=node['rank'])
                 for node in topology['nodes']}
        containers = [dict(node=keys[position[item['labels'][LABEL + 'node-id']]], id=item['id'], name=item['name'],
                           image='example/image@sha256:' + 'a' * 64, labels=item['labels'], cmd=[], running=False,
                           status='exited', pids=[]) for item in self.containers]
        raw = self.root / 'raw.json'
        raw.write_text(json.dumps(dict(profiles={}, containers=containers, nodes=nodes,
                                       topology_id=topology['topology_id'], worker_status='ok', gpu_processes=[])))
        result = self.run_pulsar('inventory', '--from-fixture', str(raw), '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        (service,) = json.loads(result.stdout)['services']
        self.assertFalse(service['safe_to_stop'])
        found = {rank['rank']: rank['node'] for rank in service['ranks']}
        self.assertEqual(found, {str(rank): keys[position[probe['node_id']]] for rank, probe in enumerate(self.earlier)})
        self.assertTrue(all(rank['status'] == 'exited' for rank in service['ranks']))


if __name__ == '__main__':
    unittest.main()
