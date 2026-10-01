"""Ordered serving placement is independent of confirmed cluster membership."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.topology_manifest import profile_fabric
from tests.test_transfer import topology

ROOT = Path(__file__).resolve().parents[1]


class ServingPlacement(unittest.TestCase):
    def test_profile_fabric_uses_only_the_ordered_selected_pair(self):
        value = topology(3)
        before = json.dumps(value, sort_keys=True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            profile_fabric(value, 2, ['node-2', 'node-1'])
        self.assertEqual([line.split('\t')[0] for line in output.getvalue().splitlines()], ['2', '1'])
        self.assertEqual(json.dumps(value, sort_keys=True), before)
        for ids in (['node-2'], ['node-2', 'node-2'], ['node-2', 'unknown']):
            with self.assertRaises(ValueError):
                profile_fabric(value, 2, ids)

    def resolve(self, selection, *, single='', count=2):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'topology.json'
            path.write_text(json.dumps(topology(3)))
            code = '''
. "$1/scripts/lib.sh"
NODES="$4"; TOPOLOGY_CLASS=roce-full-mesh; MIN_RAILS_PER_PAIR=1
CLUSTER_TOPOLOGY_FILE="$2"; CLUSTER_TOPOLOGY_COUNT=3; CLUSTER_TOPOLOGY_FULL_MESH=1
CLUSTER_TOPOLOGY_LOADED=1; CLUSTER_TOPOLOGY_SSH_TRUSTED=1; CLUSTER_TOPOLOGY_ID=synthetic
CLUSTER_NODE_IDS=(node-0 node-1 node-2); CLUSTER_NODE_HOSTNAMES=(rank-0 rank-1 rank-2)
CLUSTER_NODE_SSH_HOSTS=(local alias-1 alias-2); CLUSTER_NODE_CONTROL_IPS=(192.0.2.10 192.0.2.11 192.0.2.12)
CLUSTER_NODE_CONTROL_IFS=(mgmt0 mgmt0 mgmt0); CLUSTER_NODE_HCAS=(roce0 roce0 roce0)
declare -A CLUSTER_PAIR_RAILS=([0:1]=1 [0:2]=1 [1:2]=1)
require_cluster_nodes() { [ "$1" -le "$CLUSTER_TOPOLOGY_COUNT" ]; }
load_cluster_topology() { return 0; }
resolve_serving_placement "$5" "$3" || exit 3
printf '%s\n' "${SERVING_NODE_INDEXES[*]}" "${SERVING_NODE_IDS[*]}" "$CLUSTER_TOPOLOGY_COUNT"
'''
            return subprocess.run(['bash', '-c', code, 'placement-test', str(ROOT), str(path),
                                   selection, str(count), single], text=True, capture_output=True)

    def test_ordered_selector_preserves_membership_and_defaults(self):
        selected = self.resolve('node-2,node-1')
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(selected.stdout.splitlines(), ['2 1', 'node-2 node-1', '3'])
        default = self.resolve('')
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(default.stdout.splitlines(), ['0 1', 'node-0 node-1', '3'])
        for value in ('node-2', 'node-2,node-2', 'node-2,missing', ',node-1', 'node-2,node-1,'):
            self.assertNotEqual(self.resolve(value).returncode, 0)
        self.assertNotEqual(self.resolve('node-2,node-1', single='node-0').returncode, 0)


if __name__ == '__main__':
    unittest.main()
