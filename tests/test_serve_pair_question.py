"""Serve a model… on a saved four-node ring: which recipes ask a question
before the plan, and what each choice leads to. Driven with the serve menu's
doubles (tests/test_serve_menu.py); no command here reaches Docker, SSH, a
GPU or the Hugging Face Hub."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from scripts import serve_plan as serve  # noqa: E402
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
    return checked(**{"geometry": {"nodes": nodes, "tp": nodes, "pp": 1},
                      "home": {"node_id": IDS[0], "path": "/fixture/home", "verified_at": "2026-09-05T00:00:00Z"},
                      "local_state": "ready", **fields})


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


QUESTION = "Which two neighbouring machines should serve org/model?"


class PairQuestion(RingHarness):
    """A two-node recipe on the ring asks which pair, then plans on it."""

    def setUp(self):
        super().setUp()
        self.entries(recipe(2, local_state="missing"),
                     recipe(1, spec_id=OTHER, model_id="org/other"))

    def test_the_pairs_in_order_with_the_default_first(self):
        result = self.run_menu(["0", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        # At 80 columns a choice too wide goes on under its first line.
        self.assertEqual(self.menus()[1], [
            QUESTION,
            f"{HOSTS[0]} and {HOSTS[1]}, API on {HOSTS[0]}; what",
            "  Start uses when no machines are named; its model files are on",
            f"  {HOSTS[0]}",
            f"{HOSTS[0]} and {HOSTS[3]}, API on {HOSTS[0]}; its",
            f"  model files are on {HOSTS[0]}",
            f"{HOSTS[1]} and {HOSTS[2]}, API on {HOSTS[1]}",
            f"{HOSTS[2]} and {HOSTS[3]}, API on {HOSTS[2]}",
            "Back"])
        self.assertIn(f"{QUESTION}\t0", self.default_log.read_text().splitlines())
        self.assert_plain_choices()

    def test_backing_out_changes_nothing(self):
        for answer in ("Back", "esc"):
            with self.subTest(answer=answer):
                for path in (self.log, self.choose_log, self.answers.with_suffix(".n")):
                    path.unlink(missing_ok=True)
                result = self.run_menu(["0", answer, "Back"])
                self.assertEqual(result.returncode, 0, result.stderr)
                # Back to the list, and nothing planned, prepared or started.
                self.assertEqual(self.menus()[2][0], "Serve a model")
                self.assertEqual(self.commands(), ["inventory --json"])

    def test_the_default_pair_plans_as_the_menu_did_without_a_question(self):
        self.fixture("prepare-plan", envelope(prepare_plan((IDS[0], "home-view"), (IDS[1], "copy"))))
        self.fixture("prepare-plan", envelope(prepare_plan((IDS[0], "home-view"), (IDS[1], "copy"))), nth=2)
        result = self.run_menu(["0", "0", "Go ahead", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["inventory --json", f"model prepare {SPEC} --plan --json",
                                           f"image stage {SPEC} --plan --json", f"start {SPEC} --dry-run --json",
                                           f"model prepare {SPEC} --plan --json", f"model prepare {SPEC} --yes",
                                           f"start {SPEC}"])
        self.assertIn(f"Serve org/model on {HOSTS[0]} and {HOSTS[1]}", result.stdout)

    def test_the_pair_on_saved_ranks_2_and_3_reaches_prepare_and_start_in_that_order(self):
        copy = prepare_plan((IDS[2], "copy"), (IDS[3], "copy"))
        copy["actions"][0]["passes_through"] = [{"copies": "to", "node_id": IDS[2], "through": [IDS[1]]}]
        self.fixture("prepare-plan", envelope(copy))
        self.fixture("image-plan", envelope(image_plan((2, "ok"), (3, "missing"))))
        # This machine does not serve, so its image is not observed: the image is pulled.
        self.fixture("image-plan", envelope(image_plan((2, "ok"), (3, "missing"), mode="pull-exact-digest")), nth=2)
        result = self.run_menu(["0", "3", "Go ahead", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        placement = f"--placement-nodes {HOSTS[2]},{HOSTS[3]}"
        self.assertEqual(self.commands(), [
            "inventory --json", f"model prepare {SPEC} {placement} --plan --json",
            f"image stage {SPEC} {placement} --plan --json", f"start {SPEC} {placement} --dry-run --json",
            f"model prepare {SPEC} {placement} --plan --json", f"model prepare {SPEC} {placement} --yes",
            f"image stage {SPEC} {placement} --pull --plan --json", f"image stage {SPEC} {placement} --pull --yes",
            f"start {SPEC} {placement}"])
        text = " ".join(result.stdout.split())
        # The choice again, in the plan's first line; the copy's route; the image on the pair's node.
        self.assertIn(f"Serve org/model on {HOSTS[2]} and {HOSTS[3]} Pulsar will:", text)
        self.assertIn(f"1. Copy the model files from {HOSTS[0]} to {HOSTS[2]} and {HOSTS[3]}. "
                      f"Copies to {HOSTS[2]} pass through {HOSTS[1]}.", text)
        self.assertIn(f"2. Pull the serving image on {HOSTS[3]} (exact digest, from its registry).", text)
        self.assertIn(f"✓ Serving: org/model started on {HOSTS[2]} and {HOSTS[3]}.", text)
        # A command too long for the line breaks between arguments, each line but
        # the last ending in " \"; joined again, the commands are whole.
        self.assertIn(f"Same as: ./pulsar model prepare abababababab {placement} --yes ./pulsar image stage "
                      f"{SPEC} {placement} --pull --yes ./pulsar start abababababab {placement}",
                      text.replace(" \\ ", " "))
        self.assert_no_unapproved_authority()

    def test_a_cable_recorded_only_in_reverse_is_offered_and_started_in_that_order(self):
        # Prepare recorded rank 0 on saved rank 3 and rank 1 on saved rank 2, under this cluster.
        topology_id = json.loads(self.topology.read_text())["topology_id"]
        copies = [{"rank": 0, "node_id": IDS[3], "topology_id": topology_id},
                  {"rank": 1, "node_id": IDS[2], "topology_id": topology_id}]
        self.entries(recipe(2, local_state="missing", prepared_copies=copies),
                     recipe(1, spec_id=OTHER, model_id="org/other"))
        self.fixture("prepare-plan", envelope(prepare_plan((IDS[3], "reuse"), (IDS[2], "reuse"))))
        self.fixture("image-plan", envelope(image_plan((3, "ok"), (2, "ok"))))
        result = self.run_menu(["0", "3", "Go ahead", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        question = " ".join(" ".join(self.menus()[1]).split())
        self.assertIn(f"{HOSTS[3]} and {HOSTS[2]}, API on {HOSTS[3]}; its copies are recorded here", question)
        self.assertNotIn(f"{HOSTS[2]} and {HOSTS[3]}", question)
        placement = f"--placement-nodes {HOSTS[3]},{HOSTS[2]}"
        commands = self.commands()
        self.assertIn(f"model prepare {SPEC} {placement} --plan --json", commands)
        self.assertIn(f"start {SPEC} {placement} --dry-run --json", commands)
        self.assertEqual(commands[-1], f"start {SPEC} {placement}")
        text = " ".join(result.stdout.split())
        self.assertIn(f"Serve org/model on {HOSTS[3]} and {HOSTS[2]}", text)
        self.assertIn(f"./pulsar start abababababab {placement}", text.replace(" \\ ", " "))
        self.assert_no_unapproved_authority()

    def test_change_never_offers_another_machine_for_a_pair(self):
        self.fixture("image-plan", envelope(image_plan((0, "ok"), (3, "missing"))))
        result = self.run_menu(["0", "1", "Change…", "Back", "Cancel", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[3], ["Change the plan", "Pull the serving image from its registry instead",
                                           "Back"])
        self.assertEqual(self.ran(), [])


def card_inventory(*services):
    """A ring inventory as Running services… reads it; each service is (spec_id, saved positions)."""
    from datetime import datetime, timezone
    from tests.test_services_menu import service, utc
    document = dict(ring_inventory(), generated_at=utc(datetime.now(timezone.utc)))
    for index, node in document["nodes"].items():
        node.update(control_ip=f"192.0.2.{30 + int(index[1:])}", local=index == "k0")
    document["services"] = [service(spec, keys=[f"k{position}" for position in positions])
                            for spec, positions in services]
    return document


class RunningServices(RingHarness):
    """Restart and Switch from Running services…, through the real serve-menu.sh."""
    MENUS = ("services-menu.sh", "serve-menu.sh", "cluster-review.sh", "connect_card.py")
    MENU = "services-menu.sh"

    def setUp(self):
        super().setUp()
        self.entries(recipe(2), recipe(2, spec_id=OTHER, model_id="org/other"))
        self.inventory(card_inventory((SPEC, [2, 3])))
        (self.fixtures / "stop.inventory").write_text(json.dumps(card_inventory()))

    def test_restart_starts_it_again_on_the_same_pair_in_the_same_order(self):
        result = self.run_menu(["Restart…", "Go ahead", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Restart…", self.menus()[0])
        self.assertEqual(self.menus()[1], ["Restart a service", "Cancel", "Go ahead", "Details"])
        text = " ".join(result.stdout.split())
        self.assertIn(f"Restart org/model on {HOSTS[2]} and {HOSTS[3]}", text)
        self.assertIn(f"1. Stop org/model on {HOSTS[2]} and {HOSTS[3]}. Its model files are kept.", text)
        # No pair question: Restart uses the nodes it runs on.
        self.assertNotIn(QUESTION, " ".join(" ".join(menu) for menu in self.menus()))
        self.assertEqual(self.ran(), [f"stop {SPEC}", f"start {SPEC} --placement-nodes {HOSTS[2]},{HOSTS[3]}"])
        self.assertIn(f"Same as: ./pulsar stop abababababab ./pulsar start abababababab --placement-nodes "
                      f"{HOSTS[2]},{HOSTS[3]}", text.replace(" \\ ", " "))
        self.assert_no_unapproved_authority()
        self.assert_plain_choices()

    def test_restart_keeps_a_reversed_pair_in_its_order(self):
        self.inventory(card_inventory((SPEC, [3, 2])))
        result = self.run_menu(["Restart…", "Go ahead", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Restart org/model on {HOSTS[3]} and {HOSTS[2]}", " ".join(result.stdout.split()))
        self.assertEqual(self.ran(), [f"stop {SPEC}", f"start {SPEC} --placement-nodes {HOSTS[3]},{HOSTS[2]}"])

    def test_switch_stops_it_then_asks_its_own_question_for_the_new_recipe(self):
        self.fixture("prepare-plan", envelope(prepare_plan((IDS[0], "reuse"), (IDS[3], "copy"))))
        self.fixture("image-plan", envelope(image_plan((0, "ok"), (3, "ok"))))
        result = self.run_menu(["Switch to another model…", "1", "1", "Go ahead", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1][0], "Switch to another model")
        self.assertEqual(self.menus()[2][0], "Which two neighbouring machines should serve org/other?")
        # Each pair says where the service being switched from runs.
        self.assertEqual(self.menus()[2][-3:], [f"{HOSTS[2]} and {HOSTS[3]}, API on {HOSTS[2]}; org/model",
                                                f"  is running on {HOSTS[2]} and {HOSTS[3]}", "Back"])
        text = " ".join(result.stdout.split())
        self.assertIn(f"Stop org/model on {HOSTS[2]} and {HOSTS[3]}; you are switching from it.", text)
        placement = f"--placement-nodes {HOSTS[0]},{HOSTS[3]}"
        self.assertEqual(self.ran(), [f"model prepare {OTHER} {placement} --yes", f"stop {SPEC}",
                                      f"start {OTHER} {placement}"])
        self.assert_no_unapproved_authority()
        self.assert_plain_choices()


def names_of(topology):
    from model_library.node_names import NodeNames
    nodes = sorted(topology["nodes"], key=lambda node: node["rank"])
    return NodeNames({node["node_id"]: node["hostname"] for node in nodes}, [node["node_id"] for node in nodes],
                     topology["validation"]["class"])


class Pairs(unittest.TestCase):
    """The pairs, their order and the commands each leads to, from scripts/serve_plan.py."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.topology = ring_topology(self.root)
        self.names = names_of(self.topology)

    def test_this_machines_pairs_first_then_cable_order(self):
        self.assertEqual(serve.ring_pairs(self.topology),
                         [(IDS[0], IDS[1]), (IDS[0], IDS[3]), (IDS[1], IDS[2]), (IDS[2], IDS[3])])

    def test_files_without_a_home_go_where_acquire_and_restore_take_a_home(self):
        # Acquire and Restore take a two-node recipe's home only on saved rank 0 or 1. A pair at
        # ranks 2 and 3 gets its files there, and Prepare copies them to the pair.
        order = [node["node_id"] for node in self.topology["nodes"]]
        bare = recipe(2, home=None, local_state="missing", archive_state="verified")
        for serving, expected in (([IDS[2], IDS[3]], IDS[0]), ([IDS[1], IDS[2]], IDS[1]),
                                  ([IDS[0], IDS[1]], IDS[0]), ([IDS[3], IDS[0]], IDS[0])):
            with self.subTest(serving=serving):
                for archive in ("configured", "disabled"):
                    sources = serve.file_sources(bare, serving=serving, order=order, archive_location=archive)
                    self.assertEqual({item["node"] for item in sources}, {expected})
                self.assertIn(expected, order[:2])
        self.assertEqual(serve.home_nodes(2, [IDS[2], IDS[3]], order), order[:2])
        self.assertEqual(serve.home_nodes(1, [IDS[3]], order), order)
        # Files found on a pair's node cannot be registered as its home there.
        manifest = next(iter(serve.members(bare).values())).get("snapshot_manifest_id")
        found = serve.file_sources(bare, serving=[IDS[2], IDS[3]], order=order, archive_location="disabled",
                                   files={str(manifest): [IDS[2]]})
        self.assertEqual([(item["kind"], item["node"]) for item in found], [("download", IDS[0])])

    def test_start_accepts_every_pair_in_the_order_offered(self):
        from scripts.start_placement import ordinary_start_refusal
        for pair in serve.ring_pairs(self.topology):
            self.assertIsNone(ordinary_start_refusal(self.topology, list(pair), guarded=False), pair)
            # A cable is offered in reverse when only that order has its copies recorded.
            self.assertIsNone(ordinary_start_refusal(self.topology, list(reversed(pair)), guarded=False), pair)

    def test_no_pairs_without_saved_links(self):
        self.assertEqual(serve.ring_pairs(dict(self.topology, links=[])), [])
        self.assertEqual(serve.ring_pairs(None), [])

    def test_who_is_asked(self):
        mesh = names_of(dict(self.topology, validation={"class": "roce-full-mesh"}))
        cases = [(recipe(2), self.names, True), (recipe(1), self.names, False), (recipe(3), self.names, False),
                 (recipe(2, container_env=["NCCL_ALGO=Ring"]), self.names, False), (recipe(2), mesh, False),
                 (recipe(2, start_supported=False, start_unsupported_reason="guard_unsupported"), self.names, False)]
        for row, names, asked in cases:
            with self.subTest(nodes=row["geometry"]["nodes"], env=row.get("container_env"), shape=names.shape):
                self.assertEqual(serve.asks_for_a_pair(row, names), asked)

    def test_only_serve_a_model_asks_for_the_pairs(self):
        # Why will a model not start? reads the same command without --pairs and asks nothing for a pair.
        import contextlib
        import io
        import os
        from unittest import mock
        path = self.root / "topology.json"
        path.write_text(json.dumps(self.topology))
        printed = {}
        for flags in ((), ("--pairs",)):
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"CLUSTER_TOPOLOGY_FILE": str(path)}), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps({"entries": [recipe(2)]}))), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(serve.main(["nodes", "--spec-id", SPEC, *flags, "--width", "200",
                                             "--repo-root", str(self.root)]), 0)
            printed[flags] = output.getvalue().splitlines()
        self.assertEqual(printed[()], [])
        self.assertEqual(printed[("--pairs",)][0], f"title\t{QUESTION}")

    def test_each_choice_names_its_pair_in_that_order_and_the_default_leads(self):
        lines = serve.pair_lines(recipe(2), serve.ring_pairs(self.topology), self.names, width=200)
        self.assertEqual(lines, [
            "title\tWhich two neighbouring machines should serve org/model?",
            f"default\t{IDS[0]},{IDS[1]}",
            f"node\t{IDS[0]},{IDS[1]}\t{HOSTS[0]} and {HOSTS[1]}, API on {HOSTS[0]}; what Start uses when no "
            f"machines are named; its model files are on {HOSTS[0]}",
            f"node\t{IDS[0]},{IDS[3]}\t{HOSTS[0]} and {HOSTS[3]}, API on {HOSTS[0]}; its model files are on "
            f"{HOSTS[0]}",
            f"node\t{IDS[1]},{IDS[2]}\t{HOSTS[1]} and {HOSTS[2]}, API on {HOSTS[1]}",
            f"node\t{IDS[2]},{IDS[3]}\t{HOSTS[2]} and {HOSTS[3]}, API on {HOSTS[2]}"])

    def test_copies_recorded_only_in_the_order_they_were_prepared_and_services_from_the_inventory(self):
        def view(rank, node, topology_id=self.topology["topology_id"]):
            return {"rank": rank, "node_id": node, "topology_id": topology_id}
        row = recipe(2, prepared_copies=[view(0, IDS[1]), view(1, IDS[2]),
                                         # The other order of 2 and 3, and a copy of an earlier saved cluster.
                                         view(0, IDS[3]), view(1, IDS[2]), view(0, IDS[0], "e" * 64)])
        everything = [row, recipe(1, spec_id=OTHER, model_id="org/other")]
        lines = serve.pair_lines(row, serve.ring_pairs(self.topology), self.names, width=200, entries=everything,
                                 inventory=ring_inventory((OTHER, "running", [3])),
                                 topology_id=self.topology["topology_id"])
        self.assertEqual([line.split("\t")[-1] for line in lines[2:]], [
            f"{HOSTS[0]} and {HOSTS[1]}, API on {HOSTS[0]}; what Start uses when no machines are named; its model "
            f"files are on {HOSTS[0]}",
            f"{HOSTS[0]} and {HOSTS[3]}, API on {HOSTS[0]}; its model files are on {HOSTS[0]}; org/other is running "
            f"on {HOSTS[3]}",
            f"{HOSTS[1]} and {HOSTS[2]}, API on {HOSTS[1]}; its copies are recorded here",
            # Only the reverse of this cable has its copies recorded, so it is offered in that order.
            f"{HOSTS[3]} and {HOSTS[2]}, API on {HOSTS[3]}; its copies are recorded here; org/other is running on "
            f"{HOSTS[3]}"])
        self.assertEqual([line.split("\t")[1] for line in lines[2:]],
                         [f"{IDS[0]},{IDS[1]}", f"{IDS[0]},{IDS[3]}", f"{IDS[1]},{IDS[2]}", f"{IDS[3]},{IDS[2]}"])

    def test_each_cable_keeps_its_lower_rank_first_unless_only_its_reverse_is_recorded(self):
        topology_id = self.topology["topology_id"]

        def view(rank, node, recorded_under=topology_id):
            return {"rank": rank, "node_id": node, "topology_id": recorded_under}
        cases = (
            ([], (IDS[2], IDS[3])),
            ([view(0, IDS[3]), view(1, IDS[2])], (IDS[3], IDS[2])),
            # Both orders recorded: neither is the only one, so the lower rank leads.
            ([view(0, IDS[3]), view(1, IDS[2]), view(0, IDS[2]), view(1, IDS[3])], (IDS[2], IDS[3])),
            # Records of an earlier saved cluster do not count.
            ([view(0, IDS[3], "e" * 64), view(1, IDS[2], "e" * 64)], (IDS[2], IDS[3])),
        )
        for copies, offered in cases:
            with self.subTest(copies=len(copies)):
                row = recipe(2, prepared_copies=copies)
                self.assertEqual(serve.offered_order(row, (IDS[2], IDS[3]), topology_id), offered)
        # The default cable reversed is still the first choice, with the cursor on it,
        # but it is no longer what Start uses when no machines are named.
        row = recipe(2, prepared_copies=[view(0, IDS[1]), view(1, IDS[0])])
        lines = serve.pair_lines(row, serve.ring_pairs(self.topology), self.names, width=200,
                                 topology_id=topology_id)
        self.assertEqual(lines[1], f"default\t{IDS[1]},{IDS[0]}")
        self.assertEqual(lines[2], f"node\t{IDS[1]},{IDS[0]}\t{HOSTS[1]} and {HOSTS[0]}, API on {HOSTS[1]}; its "
                                   "copies are recorded here")
        self.assertNotIn("what Start uses", " ".join(lines))

    def test_the_default_pair_names_no_nodes_and_any_other_pair_names_both_in_order(self):
        row = recipe(2)
        for node, placement in ((f"{IDS[0]},{IDS[1]}", []),
                                (f"{IDS[0]},{IDS[3]}", ["--placement-nodes", f"{HOSTS[0]},{HOSTS[3]}"]),
                                (f"{IDS[2]},{IDS[3]}", ["--placement-nodes", f"{HOSTS[2]},{HOSTS[3]}"])):
            with self.subTest(node=node):
                serving = serve.serving_nodes(row, self.names.order, node)
                self.assertEqual(serve.placement_args(row, self.names, serving, node), placement)
                requests = dict(serve.preview_requests(row, [row], names=self.names, node=node, sources=[],
                                                       files_scanned=False, source=None))
                self.assertEqual(requests["prepare"], ["./pulsar", "model", "prepare", SPEC, *placement, "--plan",
                                                       "--json"])
                self.assertEqual(requests["start"], ["./pulsar", "start", SPEC, *placement, "--dry-run", "--json"])
                self.assertEqual(requests["image"], ["./pulsar", "image", "stage", SPEC, *placement, "--plan",
                                                     "--json"])
        # Without a choice the menu plans as before the question existed.
        self.assertEqual(serve.preview_requests(row, [row], names=self.names, node=None, sources=[],
                                                files_scanned=False, source=None),
                         serve.preview_requests(row, [row], names=self.names, node=f"{IDS[0]},{IDS[1]}", sources=[],
                                                files_scanned=False, source=None))


if __name__ == "__main__":
    import unittest
    unittest.main()
