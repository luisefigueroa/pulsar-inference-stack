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
from support.topology_fixture import Fixture, ring_probes, shuffled
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


if __name__ == '__main__':
    unittest.main()
