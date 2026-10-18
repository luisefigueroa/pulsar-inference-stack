"""A one-node container proves the physical node it was observed on before Pulsar removes it."""
import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = "t" * 64
TOPOLOGY_LABEL = "io.pulsar.gb10.topology"
NODE_LABEL = "io.pulsar.gb10.node-id"


def lib_function(name):
    """A real lib.sh function, so the test exercises the code removal relies on."""
    text = (ROOT / "scripts/lib.sh").read_text()
    return re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M).group(0)


# The real identity functions, with the confirmed topology as a double.
HARNESS = f'''
PULSAR_TOPOLOGY_LABEL={TOPOLOGY_LABEL}
PULSAR_NODE_ID_LABEL={NODE_LABEL}
load_cluster_topology() {{
  CLUSTER_TOPOLOGY_ID={TOPOLOGY}; CLUSTER_TOPOLOGY_COUNT=3; CLUSTER_NODE_IDS=(node-0 node-1 node-2)
}}
''' + "".join(lib_function(name) for name in (
    "container_placement_identity_fields", "placement_index_for_role",
    "container_single_node_identity_is_proven", "container_single_node_identity_refuse_reason")) + '''
if container_single_node_identity_is_proven "$1" "$2"; then echo proven; else echo refused; fi
container_single_node_identity_refuse_reason "$1" "$2"
'''


class SingleNodeIdentity(unittest.TestCase):
    def check(self, labels, placement):
        """(proven or refused, refuse reason) for a container with LABELS at PLACEMENT."""
        result = subprocess.run(["bash", "-c", HARNESS, "identity", json.dumps({"labels": labels}), placement],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        verdict, reason = result.stdout.splitlines()
        return verdict, reason

    def test_both_labels_must_name_the_observed_node_and_topology(self):
        self.assertEqual(self.check({TOPOLOGY_LABEL: TOPOLOGY, NODE_LABEL: "node-1"}, "rank-1")[0], "proven")
        self.assertEqual(self.check({TOPOLOGY_LABEL: TOPOLOGY, NODE_LABEL: "node-2"}, "rank-1"),
                         ("refused", "node-id mismatch (have='node-2' expected='node-1')"))
        self.assertEqual(self.check({TOPOLOGY_LABEL: "u" * 64, NODE_LABEL: "node-1"}, "rank-1"),
                         ("refused", "topology label mismatch"))

    def test_without_a_topology_label_the_node_id_is_still_compared(self):
        for placement, expected in (("head", "node-0"), ("rank-1", "node-1")):
            with self.subTest(placement=placement):
                self.assertEqual(self.check({NODE_LABEL: expected}, placement)[0], "proven")
                self.assertEqual(self.check({NODE_LABEL: "node-2"}, placement),
                                 ("refused", f"node-id mismatch (have='node-2' expected='{expected}')"))

    def test_only_a_local_legacy_container_may_lack_a_node_id(self):
        for labels in ({}, {TOPOLOGY_LABEL: TOPOLOGY}):
            with self.subTest(labels=labels):
                self.assertEqual(self.check(labels, "head")[0], "proven")
                self.assertEqual(self.check(labels, "rank-1"),
                                 ("refused", f"remote single-node container has no {NODE_LABEL}"))


if __name__ == "__main__":
    unittest.main()
