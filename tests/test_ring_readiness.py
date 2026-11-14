"""The readiness phrase on a saved ring: a recipe on one or two nodes can serve
on several placements, so the phrase names the nodes it is about and, when the
default placement is not ready to start, a placement whose copies this saved
cluster's records show complete. On a full mesh nothing changes. Synthetic records only;
nothing here runs a command or probes a node."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.node_names import NodeNames  # noqa: E402
from scripts import models_page as mp  # noqa: E402
from scripts import serve_plan as serve  # noqa: E402
from tests.test_catalog_menu import HOME, checked, row  # noqa: E402
from tests.test_models_page import binding, budget, entry, files, home, location, node  # noqa: E402

GIB = 1024 ** 3
SIZES = {None: 212 * GIB}
MODEL = "11" * 32
CURRENT, EARLIER = "7" * 64, "e" * 64
IDS = ["n0", "n1", "n2", "n3"]
HOSTS = ["node-a", "node-b", "node-c", "node-d"]


def ring(hosts=HOSTS):
    """A saved ring of four: node-a, node-b, node-c, node-d in cable order, node-d cabled back to node-a."""
    topology = {"topology_id": CURRENT, "validation": {"class": "roce-ring"},
                "nodes": [{"node_id": node_id, "hostname": host, "rank": rank}
                          for rank, (node_id, host) in enumerate(zip(IDS, hosts))],
                "links": [{"ranks": [rank, (rank + 1) % 4]} for rank in range(4)]}
    names = NodeNames(dict(zip(IDS, hosts)), list(IDS), "roce-ring")
    return topology, names


TOPOLOGY, NAMES = ring()
PAIRS = serve.ring_pairs(TOPOLOGY)
MESH = NodeNames(dict(zip(IDS, HOSTS)), list(IDS), "roce-full-mesh")


def view(rank, node_id, topology_id=CURRENT, manifest=None):
    found = {"rank": rank, "node_id": node_id, "topology_id": topology_id}
    if manifest:
        found["snapshot_manifest_id"] = manifest
    return found


def recipe(nodes=1, *copies, state="changed", **fields):
    """A recipe whose home is on node-a, with prepared copies COPIES and saved local state STATE."""
    return checked(geometry={"nodes": nodes, "tp": nodes, "pp": 1}, home=HOME, local_state=state,
                   snapshot_manifest_id=MODEL, prepared_copies=list(copies), **fields)


# The trial's two cases, on synthetic machines: copies of an earlier saved
# cluster on the default placement, and copies prepared under this ring elsewhere.
ONE_NODE = recipe(1, view(0, "n0", EARLIER), view(0, "n2"), view(0, "n3"))
PAIR = recipe(2, view(0, "n0", EARLIER), view(1, "n1", EARLIER), view(0, "n3"), view(1, "n2"))


def phrase(state, *, names=NAMES, topology_id=CURRENT, context=True):
    extra = {"host": names, "pairs": serve.ring_pairs(TOPOLOGY), "topology_id": topology_id} if context else {}
    return serve.readiness(state, order=list(names.order), archive_location="disabled", sizes=SIZES,
                           service=None, ring=names.ring, **extra)


class RingPhrase(unittest.TestCase):
    def test_one_node_default_needing_a_check_names_where_copies_are_recorded(self):
        self.assertEqual(phrase(ONE_NODE), "needs its files checked on node-a · copies recorded on node-c and node-d")
        more = recipe(1, view(0, "n1"), view(0, "n2"), view(0, "n3"))
        self.assertEqual(phrase(more), "needs its files checked on node-a · copies recorded on node-b, node-c and 1 more")

    def test_a_pairs_copies_are_recorded_only_in_the_role_order_it_was_prepared(self):
        self.assertEqual(phrase(PAIR), "needs its files checked on node-a + node-b · copies recorded on node-d + node-c")
        # node-d then node-c is recorded; node-c then node-d is not.
        self.assertTrue(serve.copies_recorded(PAIR, ("n3", "n2"), CURRENT))
        self.assertFalse(serve.copies_recorded(PAIR, ("n2", "n3"), CURRENT))
        both = recipe(2, view(0, "n3"), view(1, "n2"), view(0, "n1"), view(1, "n2"))
        self.assertEqual(phrase(both), "needs its files checked on node-a + node-b · copies recorded on node-b + node-c "
                                       "and 1 more pair")

    def test_a_pair_whose_snapshots_have_homes_on_both_machines_names_both_for_a_copy(self):
        # The target's home is on node-a and the draft's on node-b. Each machine needs the other
        # snapshot, so the phrase names both machines, as the count of copies does.
        member = {"model_id": "org/model", "model_commit": "c" * 40, "archive": None}
        state = recipe(2, state="missing", snapshots={
            "target": {**member, "home": HOME, "snapshot_manifest_id": "1" * 64},
            "draft": {**member, "home": {**HOME, "node_id": "n1"}, "snapshot_manifest_id": "2" * 64}})
        self.assertEqual(serve.copies_needed(state, ["n0", "n1"]), 2)
        self.assertTrue(phrase(state).startswith("needs copying to node-a and node-b · "), phrase(state))

    def test_a_pair_that_needs_a_copy_names_the_machine_it_goes_to(self):
        state = recipe(2, view(0, "n3"), view(1, "n2"), state="missing")
        self.assertEqual(phrase(state), "needs copying to node-b · 212 GiB · copies recorded on node-d + node-c")

    def test_a_ready_default_names_its_nodes_and_nothing_else(self):
        self.assertEqual(phrase(recipe(1, view(0, "n0"), view(0, "n2"), state="ready")), "ready to start on node-a")
        self.assertEqual(phrase(recipe(2, view(0, "n0"), view(1, "n1"), view(0, "n3"), view(1, "n2"),
                                       state="ready")), "ready to start on node-a + node-b")
        # The saved check does not say which nodes it covered: without this
        # cluster's records of the default placement, no nodes are named.
        self.assertEqual(phrase(recipe(1, view(0, "n0", EARLIER), state="ready")), "ready to start")

    def test_copies_of_an_earlier_saved_cluster_never_count(self):
        state = recipe(2, view(0, "n0", EARLIER), view(1, "n1", EARLIER), view(0, "n3", EARLIER),
                       view(1, "n2", EARLIER))
        self.assertEqual(phrase(state), "needs its files checked on node-a + node-b")
        self.assertFalse(serve.copies_recorded(state, ("n3", "n2"), CURRENT))
        # Without the saved cluster's topology_id no copy counts.
        self.assertEqual(phrase(ONE_NODE, topology_id=None), "needs its files checked on node-a")

    def test_every_required_snapshot_must_be_copied_for_copies_to_count(self):
        draft = "22" * 32
        snapshots = {"target": {"model_id": "org/model", "model_commit": "c" * 40, "home": HOME,
                                "snapshot_manifest_id": MODEL},
                     "draft": {"model_id": "org/draft", "model_commit": "d" * 40, "home": HOME,
                               "snapshot_manifest_id": draft}}
        partial = recipe(1, view(0, "n2", manifest=MODEL), view(0, "n3", manifest=MODEL),
                         view(0, "n3", manifest=draft), snapshots=snapshots)
        self.assertEqual(phrase(partial), "needs its files checked on node-a · copies recorded on node-d")

    def test_whole_ring_and_refused_recipes_and_services_are_unchanged(self):
        loop = recipe(2, view(0, "n3"), view(1, "n2"), container_env=["NCCL_ALGO=Ring"])
        whole = recipe(4, container_env=["NCCL_ALGO=Ring"])
        for state in (loop, whole, recipe(3), row(geometry={"nodes": 1, "tp": 1, "pp": 1})):
            with self.subTest(nodes=state["geometry"]["nodes"]):
                self.assertEqual(phrase(state), phrase(state, context=False))
        running = {"state": "running", "hosts": ["node-c"]}
        self.assertEqual(serve.readiness(ONE_NODE, order=IDS, archive_location="disabled", sizes=SIZES,
                                         service=running, ring=True, host=NAMES, pairs=PAIRS, topology_id=CURRENT),
                         "running on node-c")

    def test_the_pair_question_offers_the_order_the_phrase_names(self):
        # The trial's pair: copies recorded as node-d then node-c, so that is the choice offered.
        lines = serve.pair_lines(PAIR, PAIRS, NAMES, width=200, topology_id=CURRENT)
        self.assertEqual([line.split("\t")[1:] for line in lines if line.endswith("its copies are recorded here")],
                         [["n3,n2", "node-d and node-c, API on node-d; its copies are recorded here"]])
        self.assertEqual(len([line for line in lines if line.startswith("node\t")]), 4)
        state = recipe(2, view(0, "n2"), view(1, "n3"))
        lines = serve.pair_lines(state, PAIRS, NAMES, width=200, topology_id=CURRENT)
        self.assertEqual([line.split("\t")[1] for line in lines if line.endswith("its copies are recorded here")],
                         ["n2,n3"])
        # Records are never called ready, in the question or the phrase.
        self.assertNotIn("ready", " ".join(serve.pair_lines(PAIR, PAIRS, NAMES, width=200, topology_id=CURRENT)))
        self.assertNotIn("ready", phrase(PAIR))

    def test_the_trial_pair_with_both_orders_recorded_and_no_check(self):
        # Both pairs have the same evidence: complete records, no saved check. One phrase names both.
        state = recipe(2, view(0, "n0"), view(1, "n1"), view(0, "n3"), view(1, "n2"), state="unknown")
        self.assertEqual(phrase(state), "needs its files checked on node-a + node-b or node-d + node-c")
        self.assertEqual(phrase(state), serve.READINESS["verify-or"].format(hosts="node-a + node-b",
                                                                           others="node-d + node-c"))


# The default placement's own copies are recorded under this saved cluster.
DEFAULT_PAIR = (view(0, "n0"), view(1, "n1"))


class SameState(unittest.TestCase):
    """Placements in the same state (copies recorded, no saved check) share one phrase;
    where the default differs, the separate "copies recorded on" clause stays."""

    def test_two_node_placements_in_the_same_state(self):
        cases = (
            ((view(0, "n3"), view(1, "n2")), "needs its files checked on node-a + node-b or node-d + node-c"),
            # ring_placements' order: node-b + node-c comes before node-d + node-c.
            ((view(0, "n3"), view(1, "n2"), view(0, "n1"), view(1, "n2")),
             "needs its files checked on node-a + node-b, node-b + node-c or 1 more pair"),
            ((view(0, "n3"), view(1, "n2"), view(0, "n1"), view(1, "n2"), view(1, "n3")),
             "needs its files checked on node-a + node-b, node-a + node-d or 2 more pairs"),
            # Nothing else recorded: the default alone, as before.
            ((), "needs its files checked on node-a + node-b"),
        )
        for copies, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(phrase(recipe(2, *DEFAULT_PAIR, *copies, state="unknown")), expected)

    def test_one_node_placements_in_the_same_state(self):
        cases = (
            ((view(0, "n2"),), "needs its files checked on node-a or node-c"),
            ((view(0, "n2"), view(0, "n3")), "needs its files checked on node-a, node-c or node-d"),
            ((view(0, "n1"), view(0, "n2"), view(0, "n3")),
             "needs its files checked on node-a, node-b, node-c or 1 more node"),
        )
        for copies, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(phrase(recipe(1, view(0, "n0"), *copies)), expected)
        five = ["node-a", "node-b", "node-c", "node-d", "node-e"]
        topology = {"topology_id": CURRENT, "validation": {"class": "roce-ring"},
                    "nodes": [{"node_id": f"n{rank}", "hostname": host, "rank": rank} for rank, host in enumerate(five)],
                    "links": [{"ranks": [rank, (rank + 1) % 5]} for rank in range(5)]}
        names = NodeNames({f"n{rank}": host for rank, host in enumerate(five)}, [f"n{rank}" for rank in range(5)],
                          "roce-ring")
        state = recipe(1, *(view(0, f"n{rank}") for rank in range(5)))
        self.assertEqual(serve.readiness(state, order=names.order, archive_location="disabled", sizes=SIZES,
                                         service=None, ring=True, host=names, pairs=serve.ring_pairs(topology),
                                         topology_id=CURRENT),
                         "needs its files checked on node-a, node-b, node-c or 2 more nodes")

    def test_a_default_in_another_state_keeps_the_separate_clause(self):
        cases = (
            # A copy of an earlier saved cluster on the default.
            (ONE_NODE, "needs its files checked on node-a · copies recorded on node-c and node-d"),
            (PAIR, "needs its files checked on node-a + node-b · copies recorded on node-d + node-c"),
            # The home alone, with no copy recorded on the default.
            (recipe(1, view(0, "n2")), "needs its files checked on node-a · copies recorded on node-c"),
            # The default needs a copy.
            (recipe(2, view(0, "n0"), view(0, "n3"), view(1, "n2"), state="missing"),
             "needs copying to node-b · 212 GiB · copies recorded on node-d + node-c"),
        )
        for state, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(phrase(state), expected)

    def test_a_ready_default_is_unchanged(self):
        state = recipe(2, *DEFAULT_PAIR, view(0, "n3"), view(1, "n2"), state="ready")
        self.assertEqual(phrase(state), "ready to start on node-a + node-b")


class Mesh(unittest.TestCase):
    """A full mesh has one placement: every phrase and both screens are as before."""
    STATES = (ONE_NODE, PAIR, recipe(1, view(0, "n0"), state="ready"), recipe(2, state="missing"), recipe(3),
              row(geometry={"nodes": 1, "tp": 1, "pp": 1}),
              # Placements in the same state on a ring.
              recipe(2, *DEFAULT_PAIR, view(0, "n3"), view(1, "n2"), state="unknown"),
              recipe(1, view(0, "n0"), view(0, "n2"), view(0, "n3")))

    def test_the_phrase_is_unchanged_with_or_without_the_ring_context(self):
        for state in self.STATES:
            with self.subTest(nodes=state["geometry"]["nodes"], copies=len(state["prepared_copies"])):
                self.assertEqual(phrase(state, names=MESH), phrase(state, names=MESH, context=False))

    def test_the_serve_list_is_unchanged(self):
        rows = [dict(state, spec_id=f"{index:02d}" * 32) for index, state in enumerate(self.STATES)]
        for width in (78, 58):
            plain = serve.list_lines(rows, width=width, inventory=None, archive_location="disabled",
                                     order=IDS, repo=ROOT)
            context = serve.list_lines(rows, width=width, inventory=None, archive_location="disabled",
                                       order=IDS, repo=ROOT, host=MESH, pairs=PAIRS, topology_id=CURRENT)
            self.assertEqual(context, plain)

    def test_the_models_page_is_unchanged(self):
        document, rows = models_scan()
        for width in (80, 60):
            plain = mp.page(mp.Cluster(rows, document, budget()["nodes"], names=MESH), width=width)
            context = mp.page(mp.Cluster(rows, document, budget()["nodes"], names=MESH, pairs=PAIRS,
                                         topology_id=CURRENT), width=width)
            self.assertEqual(context, plain)


def models_scan(hosts=HOSTS, same_state=False):
    """The trial's two recipes on the Models page: homes on node-a, copies of an earlier
    cluster on node-a and node-b, and copies prepared under the ring on node-c and node-d.
    SAME_STATE: the copies on node-a and node-b are recorded under this ring too."""
    if same_state:
        pair = recipe(2, *DEFAULT_PAIR, view(0, "n3"), view(1, "n2"), state="unknown")
        one = recipe(1, view(0, "n0"), view(0, "n2"), view(0, "n3"))
    else:
        pair, one = PAIR, ONE_NODE
    pair = dict(pair, spec_id="cd" * 32, model_id="org/pair-model", snapshot_manifest_id="33" * 32)
    one = dict(one, spec_id="ab" * 32, model_id="org/one-model")
    document = files([entry(MODEL, "org/one-model", [home("n0", binding(one["spec_id"])),
                                                      location("n2", "prepared", binding(one["spec_id"])),
                                                      location("n3", "prepared", binding(one["spec_id"]))],
                            specs=[one["spec_id"]]),
                      entry("33" * 32, "org/pair-model", [home("n0", binding(pair["spec_id"])),
                                                          location("n1", "prepared", binding(pair["spec_id"])),
                                                          location("n2", "prepared", binding(pair["spec_id"])),
                                                          location("n3", "prepared", binding(pair["spec_id"]))],
                            specs=[pair["spec_id"]])],
                     nodes=[node(node_id, host) for node_id, host in zip(IDS, hosts)])
    return document, [one, pair]


class Screens(unittest.TestCase):
    """Both screens name the placements at 80 and 60 columns, breaking only between parts."""

    def serve_list(self, columns, names=NAMES, same_state=False):
        document, rows = models_scan(same_state=same_state)
        lines = serve.list_lines(rows, width=columns - 2, inventory=None, archive_location="disabled",
                                 order=list(names.order), repo=ROOT, ring=True, host=names, pairs=PAIRS,
                                 topology_id=CURRENT)
        return [line.split("\t")[-1] for line in lines if line != "gap"]

    def models(self, columns, hosts=HOSTS, names=NAMES, same_state=False):
        document, rows = models_scan(hosts, same_state)
        cluster = mp.Cluster(rows, document, budget()["nodes"], names=names, pairs=PAIRS, topology_id=CURRENT)
        return mp.page(cluster, width=columns)

    def test_the_serve_list_at_80_and_60_columns(self):
        for columns in (80, 60):
            with self.subTest(columns=columns):
                text = self.serve_list(columns)
                self.assertTrue(all(len(line) <= columns - 2 for line in text), text)
                for part in ("needs its files checked on node-a", "copies recorded on node-c and node-d",
                             "needs its files checked on node-a + node-b", "copies recorded on node-d + node-c"):
                    self.assertTrue(any(part in line for line in text), (part, text))

    def test_the_models_page_at_80_and_60_columns(self):
        for columns in (80, 60):
            with self.subTest(columns=columns):
                text = self.models(columns)
                self.assertTrue(all(len(line) <= columns for line in text.splitlines()), text)
                self.assertNotIn(" ", text)
                for part in ("needs its files checked on node-a", "copies recorded on node-c and node-d",
                             "needs its files checked on node-a + node-b", "copies recorded on node-d + node-c"):
                    self.assertTrue(any(part in line for line in text.splitlines()), (part, text))
                self.assertFalse(any(line.lstrip().startswith("·") for line in text.splitlines()), text)

    def test_placements_in_the_same_state_fit_both_screens(self):
        # The phrase is one part: where it is wider than the line it wraps
        # between words, but never inside a pair.
        for columns in (80, 60):
            with self.subTest(columns=columns):
                for text in (self.serve_list(columns, same_state=True),
                             self.models(columns, same_state=True).splitlines()):
                    self.assertTrue(all(len(line) <= columns for line in text), text)
                    self.assertFalse(any(line.lstrip().startswith(("·", "+")) for line in text), text)
                    self.assertFalse(any(line.rstrip().endswith("+") for line in text), text)
                    self.assertNotIn(" ", "\n".join(text))
                    for pair in ("node-a + node-b", "node-d + node-c"):
                        self.assertTrue(any(pair in line for line in text), (pair, text))
                    flat = " ".join(" ".join(text).split())
                    self.assertIn("needs its files checked on node-a + node-b or node-d + node-c", flat)
                    self.assertIn("needs its files checked on node-a, node-c or node-d", flat)
                    self.assertNotIn("copies recorded on", flat)

    def test_long_hostnames_still_fit_at_60_columns(self):
        hosts = [f"{name}-with-a-long-hostname" for name in HOSTS]
        _, names = ring(hosts)
        for text in (self.models(60, hosts, names).splitlines(), self.serve_list(60, names)):
            self.assertTrue(all(len(line) <= 60 for line in text), text)
            self.assertFalse(any(line.lstrip().startswith("·") for line in text), text)
            self.assertIn("node-d-with-a-long-hostname + node-c-with-a-long-hostname", " ".join(" ".join(text).split()))


class Commands(unittest.TestCase):
    """The menus' commands read the saved ring themselves: its pairs and its
    topology_id reach the phrase, or the clause silently disappears."""

    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = directory
        self.topology = directory / "topology.json"
        self.topology.write_text(json.dumps(TOPOLOGY))
        document, rows = models_scan()
        self.catalog = directory / "catalog.json"
        self.catalog.write_text(json.dumps({"entries": rows}))
        self.files = directory / "files.json"
        self.files.write_text(json.dumps(document))
        self.budget = directory / "budget.json"
        self.budget.write_text(json.dumps(budget()))

    def run_main(self, main, argv, stdin=""):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"CLUSTER_TOPOLOGY_FILE": str(self.topology)}), \
                mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(output):
            self.assertEqual(main([*argv, "--archive-location", "disabled", "--width", "80",
                                   "--repo-root", str(self.repo)]), 0)
        # The list's KIND<TAB>…<TAB>TEXT lines by their text; the page as printed.
        return " ".join(" ".join(line.split("\t")[-1] for line in output.getvalue().splitlines()
                                 if line != "gap").split())

    def test_the_serve_list_command_names_the_recorded_pair(self):
        text = self.run_main(serve.main, ["list"], self.catalog.read_text())
        self.assertIn("needs its files checked on node-a + node-b copies recorded on node-d + node-c", text)
        self.assertIn("needs its files checked on node-a copies recorded on node-c and node-d", text)

    def test_the_models_page_command_names_the_recorded_pair(self):
        text = self.run_main(mp.main, ["page", "--files", str(self.files), "--budget", str(self.budget),
                                       "--catalog", str(self.catalog)])
        self.assertIn("needs its files checked on node-a + node-b · copies recorded on node-d + node-c", text)

    def test_on_a_full_mesh_the_commands_name_nothing(self):
        self.topology.write_text(json.dumps({**TOPOLOGY, "validation": {"class": "roce-full-mesh"}}))
        text = self.run_main(serve.main, ["list"], self.catalog.read_text())
        self.assertNotIn("copies recorded on", text)
        self.assertNotIn("checked on", text)


if __name__ == "__main__":
    unittest.main()
