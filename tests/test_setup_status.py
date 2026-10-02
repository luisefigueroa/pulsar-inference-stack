"""Local setup status uses saved files only."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.setup_status import build, render_text
from scripts.terminal_format import TerminalWriter
from scripts.topology_manifest import topology_digest, topology_has_ssh_trust


def schema1(nodes):
    rows = []
    for rank in range(nodes):
        rows.append({
            "rank": rank,
            "node_id": f"node-{rank}",
            "hostname": f"rank-{rank}",
            "ssh_host": "local" if rank == 0 else f"host-{rank}",
            "control": {"interface": "mgmt0", "ip": f"192.0.2.{10 + rank}"},
            "gpu": "NVIDIA GB10",
            "rdma": [{"hca": "roce0", "netdev": "fabric0",
                      "cidrs": [f"198.51.100.{10 + rank}/24"]}],
        })
    links = []
    for a in range(nodes):
        for b in range(a + 1, nodes):
            links.append({
                "ranks": [a, b],
                "rails": [{"network": "198.51.100.0/24",
                           "a": {"hca": "roce0", "netdev": "fabric0",
                                 "ip": f"198.51.100.{10 + a}"},
                           "b": {"hca": "roce0", "netdev": "fabric0",
                                 "ip": f"198.51.100.{10 + b}"}}],
            })
    document = {
        "schema_version": 1,
        "nodes": rows,
        "links": links,
        "validation": {
            "class": "roce-full-mesh",
            "full_mesh": True,
            "connectivity_verified": True,
            "min_rails_per_pair": 1 if nodes > 1 else 0,
        },
    }
    document["topology_id"] = topology_digest(document)
    return document


def schema2_enrolled(nodes=2):
    document = schema1(nodes)
    document["schema_version"] = 2
    document["validation"]["ssh_identity_enrolled"] = True
    document["topology_id"] = topology_digest(document)
    return document


class SetupStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "releases").mkdir()

    def write_topology(self, document):
        (self.repo / ".cluster-topology.json").write_text(json.dumps(document))

    def test_missing_topology_asks_to_confirm_membership(self):
        document = build(self.repo, {})
        self.assertFalse(document["complete"])
        self.assertEqual(document["next_action"], "set-up-topology")
        self.assertEqual(document["topology"]["status"], "missing")
        self.assertEqual(document["catalog"]["spec_count"], 0)
        self.assertEqual(document["archives"]["status"], "not-configured")

    def test_without_membership_one_step_sets_up_membership_and_trust(self):
        for contents in (None, "{"):
            if contents is not None:
                (self.repo / ".cluster-topology.json").write_text(contents)
            document = build(self.repo, {})
            self.assertEqual(document["next_action"], "set-up-topology")
            self.assertEqual(document["next_label"], "Set up cluster membership and SSH trust")
        self.write_topology(schema1(2))
        self.assertEqual(build(self.repo, {})["next_label"], "Enroll SSH trust")

    def test_catalog_counts_specs(self):
        for count, expected in ((1, "1 spec"), (2, "2 specs")):
            with self.subTest(count=count):
                (self.repo / "releases" / f"{str(count) * 64}.json").write_text("{}")
                self.assertEqual(build(self.repo, {})["catalog"]["spec_count"], count)
                output = io.StringIO()
                render_text(build(self.repo, {}), writer=TerminalWriter(width=44, stream=output))
                self.assertRegex(output.getvalue(), rf"(?m)^  Catalog +{expected}$")
                self.assertNotIn("recipe", output.getvalue())

    def test_cluster_setup_completes_without_an_archive_choice(self):
        for topology, trust in ((schema1(1), "not-required"), (schema2_enrolled(), "enrolled")):
            with self.subTest(nodes=len(topology["nodes"])):
                self.write_topology(topology)
                document = build(self.repo, {})
                self.assertTrue(document["complete"])
                self.assertNotIn("next_action", document)
                self.assertEqual(document["ssh_trust"]["status"], trust)
                self.assertEqual(document["archives"]["status"], "not-configured")
                self.assertFalse((self.repo / ".env").exists())
                output = io.StringIO()
                render_text(document, writer=TerminalWriter(width=44, stream=output))
                self.assertIn("Cluster\n", output.getvalue())
                self.assertNotIn("not bound", output.getvalue())
                self.assertIn("optional", output.getvalue())
                self.assertIn("Archive storage configuration", " ".join(output.getvalue().split()))
                self.assertTrue(all(len(line) <= 44 for line in output.getvalue().splitlines()))

    def test_two_node_schema1_needs_ssh_trust_even_with_archives_configured(self):
        self.write_topology(schema1(2))
        (self.repo / ".env").write_text("PULSAR_COLD_ROOT=/var/tmp/archives\n")
        document = build(self.repo, {})
        self.assertEqual(document["next_action"], "enroll-ssh-trust")
        self.assertFalse(topology_has_ssh_trust(schema1(2)))

    def test_enrolled_two_node_with_disabled_archives_is_complete(self):
        self.write_topology(schema2_enrolled(2))
        document = build(self.repo, {"PULSAR_COLD_ROOT": ""})
        self.assertTrue(document["complete"])
        self.assertNotIn("next_action", document)
        self.assertEqual(document["ssh_trust"]["status"], "enrolled")
        self.assertEqual(document["archives"]["status"], "disabled")

    def test_unreadable_topology_is_invalid_and_still_confirms_membership(self):
        (self.repo / ".cluster-topology.json").write_text("{")
        document = build(self.repo, {})
        self.assertEqual(document["topology"]["status"], "invalid")
        self.assertEqual(document["next_action"], "set-up-topology")

    def test_process_empty_cold_root_is_disabled_not_a_gap(self):
        self.write_topology(schema1(1))
        document = build(self.repo, {"PULSAR_COLD_ROOT": ""})
        self.assertTrue(document["complete"])
        self.assertEqual(document["archives"]["status"], "disabled")

    def test_complete_status_uses_cluster_and_catalog_sections(self):
        self.write_topology(schema2_enrolled(2))
        output = io.StringIO()
        render_text(
            build(self.repo, {"PULSAR_COLD_ROOT": ""}),
            writer=TerminalWriter(width=44, stream=output),
        )
        text = output.getvalue()
        self.assertIn("Cluster\n", text)
        self.assertIn("Catalog\n", text)
        self.assertIn("Topology", text)
        self.assertIn("SSH trust", text)
        self.assertIn("Archives", text)
        self.assertNotIn(" · ", text)
        self.assertTrue(all(len(line) <= 44 for line in text.splitlines()), text)

    def test_human_text_fits_narrow_terminal(self):
        output = io.StringIO()
        render_text(build(self.repo, {}), writer=TerminalWriter(width=44, stream=output))
        self.assertTrue(all(len(line) <= 44 for line in output.getvalue().splitlines()),
                        output.getvalue())
        self.assertIn("Cluster membership is not configured.", output.getvalue())
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/setup_status.py"),
             "--repo-root", str(self.repo), "--format", "text"],
            env={**os.environ, "COLUMNS": "44", "PYTHONPATH": str(ROOT)},
            text=True, capture_output=True, check=True,
        )
        self.assertTrue(all(len(line) <= 44 for line in result.stdout.splitlines()),
                        result.stdout)

    def test_incomplete_status_names_the_actual_setup_gap(self):
        cases = ((None, "Cluster membership is not configured.", "set-up-topology"),
                 ({}, "Saved cluster membership is invalid.", "set-up-topology"),
                 (schema1(2), "Cluster membership is confirmed; SSH trust is not enrolled.", "enroll-ssh-trust"))
        for topology, explanation, next_action in cases:
            with self.subTest(topology=topology):
                if topology is not None:
                    self.write_topology(topology)
                document = build(self.repo, {})
                self.assertFalse(document["complete"])
                self.assertEqual(document["next_action"], next_action)
                output = io.StringIO()
                render_text(document, writer=TerminalWriter(width=44, stream=output))
                text = " ".join(output.getvalue().split())
                self.assertIn(explanation, text)
                self.assertIn(document["next_label"], text)
                self.assertTrue(all(len(line) <= 44 for line in output.getvalue().splitlines()))


if __name__ == "__main__":
    unittest.main()
