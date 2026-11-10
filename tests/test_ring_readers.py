"""Everything that reads a saved ring: Stop, the launch refusal, and each reader.

Synthetic ring fixtures and doubles only; nothing here contacts a node.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tests'), str(ROOT/'scripts')]
from support.topology_fixture import (Fixture, answer_on_fabric_addresses, as_mesh, ring_probes, shuffled,
                                     use_probes)
import topology_actions
import topology_manifest as tm
from tests import test_start_blockers

SPEC = 'a' * 64


def ring_fixture(test, count=4, **options):
    directory = Path(test.enterContext(tempfile.TemporaryDirectory()))
    return Fixture(directory, probes=shuffled(ring_probes(count, **options)))


class StopOnARing(unittest.TestCase):
    """A multi-node service stays stoppable whatever the saved cluster's shape."""

    def stop_cluster(self, fixture, nodes, removal_rc=0):
        marker = fixture.root/'removed'
        stubs = fixture.root/'stop-stubs.sh'
        stubs.write_text(f". '{ROOT}/scripts/lib.sh'\n" + f'''
acquire_model_library_lifecycle_lock() {{ :; }}
load_conf() {{ NODES={nodes}; TOPOLOGY_CLASS=roce-full-mesh; MIN_RAILS_PER_PAIR=2; }}
remove_stack_owned_cluster() {{ printf '%s %s %s\\n' "$1" "$2" "$3" >'{marker}'; return {removal_rc}; }}
container_ownership_inspect_local() {{ return 2; }}
container_ownership_inspect_remote() {{ return 2; }}
retire_stopped_service_indexes() {{ :; }}
''')
        result = subprocess.run([str(ROOT/'cluster/stop-cluster.sh'), SPEC],
                                env=dict(fixture.env, BASH_ENV=str(stubs)),
                                capture_output=True, text=True, timeout=60)
        return result, marker

    def test_a_multi_node_service_on_a_saved_ring_is_stopped(self):
        fixture = ring_fixture(self)
        for nodes in (2, 3, 4):
            with self.subTest(nodes=nodes):
                result, marker = self.stop_cluster(fixture, nodes)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(marker.read_text().startswith(f'{SPEC} '))
                self.assertTrue(marker.read_text().endswith(f' {nodes}\n'))
                self.assertNotIn('full mesh', result.stderr)
                self.assertNotIn('cabled', result.stderr)
                marker.unlink()

    def test_stop_still_refuses_for_reasons_other_than_shape(self):
        fixture = ring_fixture(self)
        result, marker = self.stop_cluster(fixture, 5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('spec requires exactly 5 active node(s), but only 4 confirmed', result.stderr)
        self.assertFalse(marker.exists())
        result, marker = self.stop_cluster(fixture, 3, removal_rc=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refused: ownership not proven for every existing rank', result.stderr)

    def test_pulsar_stop_of_a_multi_node_spec_reaches_the_stop_on_a_ring(self):
        fixture = ring_fixture(self)
        marker = fixture.root/'stopped'
        stubs = fixture.root/'down-stubs.sh'
        stubs.write_text(f". '{ROOT}/scripts/lib.sh'\n" + f'''
acquire_model_library_lifecycle_lock() {{ :; }}
stop_named_service_by_labels() {{ printf '%s\\n' "$CLUSTER_TOPOLOGY_COUNT" >'{marker}'; }}
retire_stopped_service_indexes() {{ :; }}
''')
        result = subprocess.run([str(ROOT/'pulsar'), 'stop', SPEC], env=dict(fixture.env, BASH_ENV=str(stubs)),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(), '4\n')


def gate(fixture, *counts):
    """Each count's exit status and stderr from the launch check, sourced alone."""
    script = '. "$1/cluster/topology.sh"; shift; for n in "$@"; do require_profile_topology "$n"; echo "rc=$?"; done'
    result = subprocess.run(['bash', '-c', script, 'gate', str(ROOT), *map(str, counts)], env=fixture.env,
                            capture_output=True, text=True, timeout=60)
    return result


class RingRefusal(unittest.TestCase):
    """A multi-node Start on a ring stays refused, and every caller says why in the same words."""

    def test_the_launch_check_names_the_cabling(self):
        fixture = ring_fixture(self)
        result = gate(fixture, 1, 2, 3, 4)
        self.assertEqual(result.stdout, 'rc=0\nrc=1\nrc=1\nrc=1\n')
        self.assertEqual(result.stderr.splitlines(), [
            f'topology: {tm.ring_refusal(2)}', f'topology: {tm.ring_refusal(3)}', f'topology: {tm.ring_refusal(4)}'])
        self.assertEqual(tm.ring_refusal(3), 'the cluster is a ring, and this recipe needs every one of its nodes '
                                             'cabled to the others')
        self.assertNotIn('full mesh', tm.ring_refusal(3))

    def test_a_mesh_keeps_its_words(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), nodes=3)
            result = gate(fixture, 1, 3)
        self.assertEqual(result.stdout, 'rc=0\nrc=1\n')
        self.assertEqual(result.stderr, 'topology: ranks 0/1 expose 1 shared RoCE rail(s); the spec requires 2\n')

class RingStart(unittest.TestCase):
    """Start's own refusal carries the launch check's words."""
    setUp = test_start_blockers.StartScenarios.setUp
    start = test_start_blockers.StartScenarios.start

    def test_start_is_refused_with_the_cabling(self):
        result, codes = self.start(nodes=3, fabric=tm.ring_refusal(3))
        self.assertEqual(codes, ['fabric_incomplete'])
        self.assertIn(f'FAIL  fabric    {tm.ring_refusal(3)}', result.stdout)
        self.assertIn(f'({tm.ring_refusal(3)})', self.records[0]['message'])


def discovery(fixture, *extra, json_output=False, columns='80'):
    """`topology detect` as the maintainer saw it: each machine also answers on
    fabric addresses, this machine's loopback is advertised, and one address
    answers with a host key SSH here does not trust."""
    candidates = [alias for alias in json.loads((fixture.root/'map.json').read_text()) if alias != 'local']
    candidates += answer_on_fabric_addresses(fixture)
    fixture.env.update(COLUMNS=columns, TOPOLOGY_UNTRUSTED='127.0.1.1,192.0.2.99',
                       CLUSTER_CANDIDATES=' '.join([*candidates, '127.0.1.1', '192.0.2.99', *extra]))
    return fixture.run('topology', 'detect', *(['--json'] if json_output else []))


def unsaved(test, probes):
    fixture = Fixture(Path(test.enterContext(tempfile.TemporaryDirectory())), probes=shuffled(probes), enrolled=False)
    fixture.path.unlink()
    return fixture


class DiscoveryReport(unittest.TestCase):
    """The discovery report leads with the count and the shape, and keeps its notes short."""

    def test_a_ring_leads_with_its_shape_order_and_what_it_allows(self):
        result = discovery(unsaved(self, ring_probes(4)))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout
        self.assertIn('Found       4 GB10 machines answered · ring · 2 links on every cable\n'
                      'Order       fixture-ring-dorado → fixture-ring-bravo → fixture-ring-echo →\n'
                      '            fixture-ring-alpha → fixture-ring-dorado\n'
                      'Serving     Machines that are not neighbours share no direct link. A ring allows\n', text)
        self.assertIn('Also seen   6 more addresses answered for machines already listed\n', text)
        self.assertEqual(text.count('Also seen'), 1)
        self.assertIn('Untrusted 192.0.2.99 answers', text)
        self.assertNotIn('127.0.1.1', text)
        for retired in ('same system already listed', 'largest directly connected', 'legacy', 'fingerprints not',
                        'Cluster ID', 'full mesh', 'node pair'):
            self.assertNotIn(retired, text)
        self.assertEqual(text.count('Identity    SSH identity not recorded yet'), 4)

    def test_a_dead_cable_names_the_machines_left_out_and_why(self):
        result = discovery(unsaved(self, ring_probes(4, dead_cables={1})))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = ' '.join(result.stdout.split())
        self.assertIn('Found 4 GB10 machines answered · 2 in the cluster · full mesh · 2 links between every pair', text)
        for name in ('fixture-ring-bravo', 'fixture-ring-echo'):
            self.assertIn(f'Left out {name} answered, but is not cabled to every other machine in the cluster', text)
        self.assertIn('Cabling The cabling forms neither a full mesh nor a closed ring of four or more', text)
        self.assertNotIn('largest directly connected', text)
        document = json.loads(discovery(unsaved(self, ring_probes(4, dead_cables={1})), json_output=True).stdout)
        # JSON keeps its stable reasons, and says which machines share links.
        left_out = [item for item in document['rejected'] if item['reasons'] != ['duplicate node identity']]
        self.assertEqual(sorted(item['hostname'] for item in left_out), ['fixture-ring-bravo', 'fixture-ring-echo'])
        self.assertEqual({tuple(item['reasons']) for item in left_out}, {(tm.MESH_LEFT_OUT, tm.FALLBACK_LEFT_OUT)})
        self.assertEqual(len(document['cabling']), 3)

    def test_this_machines_loopback_is_never_listed_as_untrusted(self):
        document = {'topology': {'nodes': []}, 'rejected': []}
        self.assertEqual(topology_actions.untrusted_addresses(document, ['127.0.1.1', '::1', 'localhost', '192.0.2.99']),
                         ['192.0.2.99'])


class MeshBecomesRing(unittest.TestCase):
    """A three-machine mesh recabled as a four-machine ring saves without Forget."""

    def setUp(self):
        self.ring = ring_probes(4)
        self.fixture = Fixture(Path(self.enterContext(tempfile.TemporaryDirectory())), probes=as_mesh(self.ring[:3]))
        use_probes(self.fixture, self.ring)
        self.fixture.env.update(CLUSTER_CANDIDATES='ring-alpha', COLUMNS='80')

    def test_discovery_notices_the_new_shape_cables_and_order(self):
        result = self.fixture.run('topology', 'detect', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        noticed = json.loads(result.stdout)['changes_noticed']
        self.assertEqual(noticed['result'], 'changed')
        self.assertEqual(noticed['shape'], {'old': 'roce-full-mesh', 'new': 'roce-ring'})
        self.assertEqual(noticed['cables'], {'gone': [['fixture-ring-dorado', 'fixture-ring-echo']],
                                             'new': [['fixture-ring-alpha', 'fixture-ring-dorado'],
                                                     ['fixture-ring-alpha', 'fixture-ring-echo']]})
        self.assertEqual([item['hostname'] for item in noticed['order']],
                         ['fixture-ring-dorado', 'fixture-ring-bravo', 'fixture-ring-echo', 'fixture-ring-alpha'])
        self.assertEqual(noticed['missing'], [])
        self.assertEqual([item['hostname'] for item in noticed['found']], ['fixture-ring-alpha'])

    def test_discovery_membership_accepts_all_four_machines(self):
        result = self.fixture.run('topology', 'detect', '--json')
        document = json.loads(result.stdout)
        self.assertEqual(document['result'], 'ok')
        self.assertEqual(document['saved_membership'], {'missing': [], 'issues': []})
        self.assertEqual(document['membership_changes']['added'], ['fixture-ring-alpha'])

    def test_an_idle_cluster_saves_the_ring(self):
        result = self.fixture.run('topology', 'configure', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.fixture.path.read_text())
        self.assertEqual(saved['validation']['class'], 'roce-ring')
        self.assertEqual([node['hostname'] for node in saved['nodes']],
                         ['fixture-ring-dorado', 'fixture-ring-bravo', 'fixture-ring-echo', 'fixture-ring-alpha'])
        tm.validate_manifest(saved, require_verified=True)

    def test_a_running_service_still_refuses_the_save(self):
        self.fixture.env['TOPOLOGY_ACTIVE'] = '1'
        before = self.fixture.path.read_bytes()
        result = self.fixture.run('topology', 'configure', '--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fixture.path.read_bytes(), before)


class DeadCableOnASavedRing(unittest.TestCase):
    """A saved machine that answers but no longer fits the cluster still blocks the save."""

    def test_the_dead_cable_and_the_machines_that_no_longer_fit_are_noticed(self):
        fixture = Fixture(Path(self.enterContext(tempfile.TemporaryDirectory())), probes=shuffled(ring_probes(4)))
        use_probes(fixture, shuffled(ring_probes(4, dead_cables={1})))
        before = fixture.path.read_bytes()
        result = fixture.run('topology', 'detect', '--json')
        self.assertNotEqual(result.returncode, 0)
        document = json.loads(result.stdout)
        noticed = document['changes_noticed']
        self.assertEqual(noticed['cables'], {'gone': [['fixture-ring-bravo', 'fixture-ring-echo']], 'new': []})
        self.assertEqual(noticed['shape'], {'old': 'roce-ring', 'new': 'roce-full-mesh'})
        # A saved ring keeps its order in the fallback: this node and its rank 1.
        self.assertEqual([node['hostname'] for node in document['topology']['nodes']],
                         ['fixture-ring-dorado', 'fixture-ring-bravo'])
        self.assertEqual(sorted((item['hostname'], item['answered']) for item in noticed['missing']),
                         [('fixture-ring-alpha', True), ('fixture-ring-echo', True)])
        result = fixture.run('topology', 'configure', '--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
