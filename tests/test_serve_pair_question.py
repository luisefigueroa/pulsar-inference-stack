"""Serve a model… on a saved four-node ring: which recipes ask a question
before the plan, and what each choice leads to. Driven with the serve menu's
doubles (tests/test_serve_menu.py); no command here reaches Docker, SSH, a
GPU or the Hugging Face Hub."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from scripts import topology_manifest as tm  # noqa: E402
from support.topology_fixture import ring_probes, shuffled  # noqa: E402
from tests.test_catalog_menu import checked  # noqa: E402
from tests.test_serve_menu import Harness, changes  # noqa: E402
from tests.test_serve_plan import SPEC, envelope, image_plan, prepare_plan  # noqa: E402

# The fixture ring in saved order: this machine first, then cable order.
HOSTS = ["fixture-ring-dorado", "fixture-ring-bravo", "fixture-ring-echo", "fixture-ring-alpha"]
IDS = [f"fixture-ring-node-{index}" for index in range(4)]
OTHER = "cd" * 32


def ring_topology(root: Path) -> dict:
    """A saved ring of four nodes, assembled from the ring fixture's probes."""
    files = []
    for index, probe in enumerate(shuffled(ring_probes(4))):
        path = root / f"ring-probe-{index}.json"
        path.write_text(json.dumps(probe))
        files.append(str(path))
    return tm.mark_verified(tm.assemble(files, None))["topology"]


def ring_inventory(*services):
    """A service inventory of the ring; each service is (spec_id, state, saved positions)."""
    nodes = {f"k{index}": {"node_id": IDS[index], "hostname": HOSTS[index], "confirmed": True,
                           "probe_status": "ok"} for index in range(4)}
    rows = [{"service_id": spec, "conf": spec, "state": state, "ownership": "managed", "safe_to_stop": True,
             "matches_selected_spec": True,
             "ranks": [{"rank": str(rank), "node": f"k{position}"} for rank, position in enumerate(positions)]}
            for spec, state, positions in services]
    return {"schema_version": 1, "generated_at": "2026-09-05T00:08:00Z", "nodes": nodes, "worker": {"status": "ok"},
            "unmanaged_gpu_processes": [], "services": rows}


def recipe(nodes=2, **fields):
    """A ready recipe on NODES nodes whose files have their home on this machine."""
    return checked(geometry={"nodes": nodes, "tp": nodes, "pp": 1},
                   home={"node_id": IDS[0], "path": "/fixture/home", "verified_at": "2026-09-05T00:00:00Z"},
                   local_state="ready", **fields)


class RingHarness(Harness):
    def setUp(self):
        super().setUp()
        self.use_topology(ring_topology(self.root))
        self.inventory(ring_inventory())
        self.fixture("image-plan", envelope(image_plan((0, "ok"), (1, "ok"))))
        self.fixture("start-plan", envelope({"checks": []}))
        self.fixture("prepare-plan", envelope(prepare_plan((IDS[0], "reuse"), (IDS[1], "reuse"))))

    def use_topology(self, topology):
        """Save TOPOLOGY, with a recent cluster check of it that noticed no change."""
        self.topology.write_text(json.dumps(topology))
        record, minutes, kept = changes()
        self.save_changes(dict(record, topology_id=topology["topology_id"]), minutes, kept)


class NoPairQuestion(RingHarness):
    """Where there is no pair question: the menu behaves as it did before the
    question existed, on a ring and on a full mesh."""

    def test_a_one_node_recipe_keeps_its_machine_question(self):
        self.entries(recipe(1))
        self.fixture("image-plan", envelope(image_plan((2, "ok"))))
        result = self.run_menu(["0", "2", "Cancel", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Which machine should serve org/model?",
                                           "fixture-ring-dorado — its model files are here", "fixture-ring-bravo",
                                           "fixture-ring-echo", "fixture-ring-alpha", "Back"])
        self.assertIn("Which machine should serve org/model?\t0", self.default_log.read_text().splitlines())
        self.assertIn(f"start {SPEC} --node fixture-ring-echo --dry-run --json", self.commands())
        self.assertEqual(self.ran(), [])
        self.assert_plain_choices()

    def test_three_nodes_on_a_ring_ask_nothing_and_stay_blocked(self):
        self.entries(recipe(3))
        result = self.run_menu(["0", "Cancel", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Serve a model", "Cancel", "Details"])
        self.assertIn("Start refuses it: the cluster is a ring, and this recipe needs every one of its nodes cabled "
                      "to the others.", " ".join(result.stdout.split()))
        self.assertEqual(self.commands(), ["inventory --json", f"model prepare {SPEC} --plan --json",
                                           f"image stage {SPEC} --plan --json", f"start {SPEC} --dry-run --json"])

    def test_a_two_node_ring_pattern_recipe_asks_nothing(self):
        self.entries(recipe(2, container_env=["NCCL_ALGO=Ring"]))
        result = self.run_menu(["0", "Cancel", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1][0], "Serve a model")
        self.assertEqual(self.commands(), ["inventory --json", f"model prepare {SPEC} --plan --json",
                                           f"image stage {SPEC} --plan --json", f"start {SPEC} --dry-run --json"])
        self.assertIn("Serve org/model on fixture-ring-dorado and fixture-ring-bravo", result.stdout)

    def test_a_two_node_recipe_on_a_full_mesh_asks_nothing(self):
        mesh = json.loads(self.topology.read_text())
        mesh["validation"]["class"] = "roce-full-mesh"
        mesh["topology_id"] = tm.topology_digest(mesh)
        self.use_topology(mesh)
        self.entries(recipe(2))
        result = self.run_menu(["0", "Go ahead", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1][0], "Serve a model")
        self.assertEqual(self.commands(), ["inventory --json", f"model prepare {SPEC} --plan --json",
                                           f"image stage {SPEC} --plan --json", f"start {SPEC} --dry-run --json",
                                           f"start {SPEC}"])
        self.assertIn("Serve org/model on fixture-ring-dorado and fixture-ring-bravo", result.stdout)


if __name__ == "__main__":
    import unittest
    unittest.main()
