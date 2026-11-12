"""The readiness phrase on a saved ring: a recipe on one or two nodes can serve
on several placements, so the phrase names the nodes it is about and, when the
default placement is not ready, a placement whose files this saved cluster's
records show ready. On a full mesh nothing changes. Synthetic records only;
nothing here runs a command or probes a node."""
from pathlib import Path
import sys
import unittest

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
    def test_one_node_default_needing_a_check_names_where_files_are_ready(self):
        self.assertEqual(phrase(ONE_NODE), "needs its files checked on node-a · files ready on node-c and node-d")
        more = recipe(1, view(0, "n1"), view(0, "n2"), view(0, "n3"))
        self.assertEqual(phrase(more), "needs its files checked on node-a · files ready on node-b, node-c and 1 more")

    def test_a_pair_is_ready_only_in_the_role_order_it_was_prepared(self):
        self.assertEqual(phrase(PAIR), "needs its files checked on node-a + node-b · files ready on node-d + node-c")
        # node-d then node-c is ready; node-c then node-d is not.
        self.assertTrue(serve.files_ready(PAIR, ("n3", "n2"), CURRENT))
        self.assertFalse(serve.files_ready(PAIR, ("n2", "n3"), CURRENT))
        both = recipe(2, view(0, "n3"), view(1, "n2"), view(0, "n1"), view(1, "n2"))
        self.assertEqual(phrase(both), "needs its files checked on node-a + node-b · files ready on node-b + node-c "
                                       "and 1 more pair")

    def test_a_pair_that_needs_a_copy_names_the_machine_it_goes_to(self):
        state = recipe(2, view(0, "n3"), view(1, "n2"), state="missing")
        self.assertEqual(phrase(state), "needs copying to node-b · 212 GiB · files ready on node-d + node-c")

    def test_a_ready_default_names_its_nodes_and_nothing_else(self):
        self.assertEqual(phrase(recipe(1, view(0, "n0"), view(0, "n2"), state="ready")), "ready to start on node-a")
        self.assertEqual(phrase(recipe(2, view(0, "n0"), view(1, "n1"), view(0, "n3"), view(1, "n2"),
                                       state="ready")), "ready to start on node-a + node-b")
        # The saved check does not say which nodes it covered: without this
        # cluster's records of the default placement, no nodes are named.
        self.assertEqual(phrase(recipe(1, view(0, "n0", EARLIER), state="ready")), "ready to start")

    def test_copies_of_an_earlier_saved_cluster_never_count_as_ready(self):
        state = recipe(2, view(0, "n0", EARLIER), view(1, "n1", EARLIER), view(0, "n3", EARLIER),
                       view(1, "n2", EARLIER))
        self.assertEqual(phrase(state), "needs its files checked on node-a + node-b")
        self.assertFalse(serve.files_ready(state, ("n3", "n2"), CURRENT))
        # Without the saved cluster's topology_id nothing is ready.
        self.assertEqual(phrase(ONE_NODE, topology_id=None), "needs its files checked on node-a")

    def test_every_required_snapshot_must_be_copied_for_files_to_be_ready(self):
        draft = "22" * 32
        snapshots = {"target": {"model_id": "org/model", "model_commit": "c" * 40, "home": HOME,
                                "snapshot_manifest_id": MODEL},
                     "draft": {"model_id": "org/draft", "model_commit": "d" * 40, "home": HOME,
                               "snapshot_manifest_id": draft}}
        partial = recipe(1, view(0, "n2", manifest=MODEL), view(0, "n3", manifest=MODEL),
                         view(0, "n3", manifest=draft), snapshots=snapshots)
        self.assertEqual(phrase(partial), "needs its files checked on node-a · files ready on node-d")

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

    def test_the_pair_question_still_says_files_are_ready_in_that_order(self):
        lines = serve.pair_lines(PAIR, PAIRS, NAMES, width=200, topology_id=CURRENT)
        ready = [line for line in lines if line.endswith("its files are ready here")]
        self.assertEqual(ready, [])  # node-d + node-c is not one of the question's choices
        state = recipe(2, view(0, "n2"), view(1, "n3"))
        lines = serve.pair_lines(state, PAIRS, NAMES, width=200, topology_id=CURRENT)
        self.assertEqual([line.split("\t")[1] for line in lines if line.endswith("its files are ready here")],
                         ["n2,n3"])


class Mesh(unittest.TestCase):
    """A full mesh has one placement: every phrase and both screens are as before."""
    STATES = (ONE_NODE, PAIR, recipe(1, view(0, "n0"), state="ready"), recipe(2, state="missing"), recipe(3),
              row(geometry={"nodes": 1, "tp": 1, "pp": 1}))

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


def models_scan(hosts=HOSTS):
    """The trial's two recipes on the Models page: homes on node-a, copies of an earlier
    cluster on node-a and node-b, and copies prepared under the ring on node-c and node-d."""
    pair = dict(PAIR, spec_id="cd" * 32, model_id="org/pair-model", snapshot_manifest_id="33" * 32)
    one = dict(ONE_NODE, spec_id="ab" * 32, model_id="org/one-model")
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

    def serve_list(self, columns, names=NAMES):
        document, rows = models_scan()
        lines = serve.list_lines(rows, width=columns - 2, inventory=None, archive_location="disabled",
                                 order=list(names.order), repo=ROOT, ring=True, host=names, pairs=PAIRS,
                                 topology_id=CURRENT)
        return [line.split("\t")[-1] for line in lines if line != "gap"]

    def models(self, columns, hosts=HOSTS, names=NAMES):
        document, rows = models_scan(hosts)
        cluster = mp.Cluster(rows, document, budget()["nodes"], names=names, pairs=PAIRS, topology_id=CURRENT)
        return mp.page(cluster, width=columns)

    def test_the_serve_list_at_80_and_60_columns(self):
        for columns in (80, 60):
            with self.subTest(columns=columns):
                text = self.serve_list(columns)
                self.assertTrue(all(len(line) <= columns - 2 for line in text), text)
                for part in ("needs its files checked on node-a", "files ready on node-c and node-d",
                             "needs its files checked on node-a + node-b", "files ready on node-d + node-c"):
                    self.assertTrue(any(part in line for line in text), (part, text))

    def test_the_models_page_at_80_and_60_columns(self):
        for columns in (80, 60):
            with self.subTest(columns=columns):
                text = self.models(columns)
                self.assertTrue(all(len(line) <= columns for line in text.splitlines()), text)
                self.assertNotIn(" ", text)
                for part in ("needs its files checked on node-a", "files ready on node-c and node-d",
                             "needs its files checked on node-a + node-b", "files ready on node-d + node-c"):
                    self.assertTrue(any(part in line for line in text.splitlines()), (part, text))
                self.assertFalse(any(line.lstrip().startswith("·") for line in text.splitlines()), text)

    def test_long_hostnames_still_fit_at_60_columns(self):
        hosts = [f"{name}-with-a-long-hostname" for name in HOSTS]
        _, names = ring(hosts)
        for text in (self.models(60, hosts, names).splitlines(), self.serve_list(60, names)):
            self.assertTrue(all(len(line) <= 60 for line in text), text)
            self.assertFalse(any(line.lstrip().startswith("·") for line in text), text)
            self.assertIn("node-d-with-a-long-hostname + node-c-with-a-long-hostname", " ".join(" ".join(text).split()))


if __name__ == "__main__":
    unittest.main()
