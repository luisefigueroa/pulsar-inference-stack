"""Storage plans name each blocker's cause and fix beside the unchanged messages (TUI-74, #58)."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library import controller
from model_library.planning import preparation_plan, preparation_set_plan, purge_plan, removal_plan
from model_library.state import Store
from release_spec import serving
from release_spec.normalize import snapshot_manifest_id

MANIFEST = json.loads((ROOT / "tests/fixtures/contracts/manifest.json").read_text())
TOPOLOGY = "d" * 64
SPEC = "b" * 64
MANAGED = "io.pulsar.gb10.managed"
SELECTED, CONF, SPEC_LABEL = "io.pulsar.gb10.selected-spec-id", "io.pulsar.gb10.conf", "io.pulsar.gb10.spec-id"
CODES = {"container-reference", "pinned", "changed-copy", "insufficient-space", "reuse-unverified",
         "dependent-copies", "incomplete-preparations", "archive-required", "discard-required", "catalog-snapshot"}


def home(node="n0", manifest=MANIFEST["manifest_id"], name="target"):
    hub = f"/fixture/homes/{manifest}/{name}"
    return {"schema_version": 1, "kind": "pulsar-home", "snapshot_manifest_id": manifest, "node_id": node,
            "hub_path": hub, "path": hub + "/snapshots/" + "a" * 40,
            "verification": {"snapshot_manifest_id": manifest}, "verified_at": "2026-09-05T00:00:00Z"}


def view(node="n1", rank=1, spec_id=SPEC, pinned=False, manifest=MANIFEST["manifest_id"], topology=TOPOLOGY):
    record = home(node, manifest)
    hub = f"/fixture/views/{spec_id}/{node}"
    return {**record, "kind": "pulsar-prepared-view", "hub_path": hub, "path": hub + "/snapshots/" + "a" * 40,
            "spec_id": spec_id, "topology_id": topology, "rank": rank, "pinned": pinned, "is_home_view": False}


def container(mount, labels=None, running=True, name="fixture-vllm", identifier="c" * 64):
    return {"id": identifier, "name": name, "mounts": [mount], "running": running, "labels": labels or {}}


def observed(*nodes, containers=(), **fields):
    return [{"node_id": node, "observable": True, "containers": list(containers) if node == nodes[-1] else [],
             **fields.get(node, {})} for node in nodes]


def spec_fixture(snapshots=1):
    draft = serving.example(2)
    draft["recipe"]["model"] = {"model_id": MANIFEST["model_id"], "model_commit": MANIFEST["snapshot_revision"]}
    draft["recipe"]["image_digest"] = "sha256:" + "b" * 64
    if snapshots == 1:
        return serving.freeze(draft, MANIFEST)
    second = copy.deepcopy(MANIFEST)
    second["snapshot_revision"] = "e" * 40
    second["manifest_id"] = snapshot_manifest_id(second)
    draft["schema_version"] = 2
    draft["recipe"]["required_snapshots"] = {"draft": {"model_id": second["model_id"],
                                                        "model_commit": second["snapshot_revision"]}}
    draft["recipe"]["engine_args"] += ["--speculative_config.model", "pulsar-snapshot:draft"]
    return serving.freeze(draft, {"target": MANIFEST, "draft": second})


def budget(available=1000, reserve=10, used=0, limit=1000):
    return {"available": available, "reserve": reserve, "used": used, "limit": limit}


class Details(unittest.TestCase):
    def assert_paired(self, plan):
        """Every detail explains one of the plan's messages with a known code and a fix."""
        self.assertIsInstance(plan["blocker_details"], list)
        for item in plan["blocker_details"]:
            self.assertIn(item["code"], CODES)
            self.assertIn(item["message"], plan["blockers"])
            self.assertTrue(item["fix"])
        self.assertEqual(plan["eligible"], not plan["blockers"])


class Purge(Details):
    def plan(self, *views, containers=()):
        return purge_plan(views=list(views), node_ids=["n1"], observations=observed("n1", containers=containers))

    def test_an_eligible_plan_has_an_empty_list(self):
        plan = self.plan(view())
        self.assertEqual((plan["eligible"], plan["blocker_details"]), (True, []))

    def test_pinned_copy(self):
        plan = self.plan(view(pinned=True))
        self.assert_paired(plan)
        self.assertEqual(plan["blocker_details"], [{
            "code": "pinned", "message": f"prepared copy for {SPEC} is pinned; explicitly unpin first",
            "node_id": "n1", "fix": "unpin the prepared copies first"}])

    def test_container_names_its_state_mount_and_files(self):
        record = view()
        labels = {MANAGED: "true", SELECTED: "cd" * 32, CONF: "ef" * 32, SPEC_LABEL: "01" * 32}
        plan = self.plan(record, containers=[container(record["hub_path"], labels)])
        self.assert_paired(plan)
        self.assertEqual(plan["blocker_details"], [{
            "code": "container-reference", "message": f"a container references the prepared copy for {SPEC}",
            "node_id": "n1", "fix": f"stop the Pulsar service for spec {'cd' * 6}",
            "container": {"id": "c" * 64, "name": "fixture-vllm", "state": "running", "spec_id": "cd" * 32},
            "mount": record["hub_path"], "path": record["path"]}])

    def test_spec_label_precedence_is_selected_then_conf_then_spec_id(self):
        record = view()
        for labels, expected in (({SELECTED: "cd" * 32, CONF: "ef" * 32, SPEC_LABEL: "01" * 32}, "cd" * 32),
                                 ({CONF: "ef" * 32, SPEC_LABEL: "01" * 32}, "ef" * 32),
                                 ({SPEC_LABEL: "01" * 32}, "01" * 32)):
            with self.subTest(expected=expected[:2]):
                found = container(record["hub_path"], {MANAGED: "true", **labels})
                detail = self.plan(record, containers=[found])["blocker_details"][0]
                self.assertEqual(detail["container"]["spec_id"], expected)
                self.assertEqual(detail["fix"], f"stop the Pulsar service for spec {expected[:12]}")

    def test_a_container_pulsar_did_not_start_has_no_spec_and_is_never_removed(self):
        record = view()
        for labels in ({}, {SELECTED: "cd" * 32}, {MANAGED: "false", SELECTED: "cd" * 32}):
            with self.subTest(labels=labels):
                detail = self.plan(record, containers=[container(record["hub_path"], labels)])["blocker_details"][0]
                self.assertNotIn("spec_id", detail["container"])
                self.assertEqual(detail["fix"], "remove the container or its mount; Pulsar never removes it")

    def test_stopped_unknown_and_unnamed_containers(self):
        record = view()
        for running, state in ((False, "stopped"), (None, "unknown"), (True, "running")):
            with self.subTest(state=state):
                found = container(record["hub_path"], running=running, name="")
                detail = self.plan(record, containers=[found])["blocker_details"][0]
                self.assertEqual(detail["container"]["state"], state)
                self.assertEqual(detail["container"]["name"], "c" * 12)

    def test_the_covering_mount_is_named(self):
        record = view()
        parent = str(Path(record["hub_path"]).parent)
        inside = record["hub_path"] + "/snapshots"
        for mount in (parent, inside, record["path"]):
            with self.subTest(mount=mount):
                detail = self.plan(record, containers=[container(mount)])["blocker_details"][0]
                self.assertEqual((detail["mount"], detail["path"]), (mount, record["path"]))
        self.assertTrue(self.plan(record, containers=[container("/elsewhere")])["eligible"])

    def test_each_mounting_container_is_its_own_detail(self):
        record = view(pinned=True)
        plan = self.plan(record, containers=[container(record["hub_path"], identifier="1" * 64, name="one"),
                                             container(record["hub_path"], identifier="2" * 64, name="two")])
        self.assert_paired(plan)
        self.assertEqual([item["code"] for item in plan["blocker_details"]],
                         ["pinned", "container-reference", "container-reference"])
        self.assertEqual([item["container"]["name"] for item in plan["blocker_details"][1:]], ["one", "two"])
        self.assertEqual(len(plan["blockers"]), 2)


class Removal(Details):
    def plan(self, views=(), containers=(), **flags):
        flags = {"published": True, "archive_verified": True, **flags}
        return removal_plan(home=home(), views=list(views), node_ids=["n0"],
                            observations=observed("n0", containers=containers), **flags)

    def codes(self, plan):
        self.assert_paired(plan)
        return [item["code"] for item in plan["blocker_details"]]

    def test_every_removal_cause(self):
        self.assertEqual(self.plan()["blocker_details"], [])
        self.assertEqual(self.codes(self.plan(views=[view()])), ["dependent-copies"])
        self.assertEqual(self.codes(self.plan(containers=[container(home()["hub_path"], running=False)])),
                         ["container-reference"])
        self.assertEqual(self.codes(self.plan(archive_verified=False)), ["archive-required"])
        self.assertEqual(self.codes(self.plan(published=False, archive_verified=False)), ["discard-required"])
        self.assertEqual(self.plan(published=False, archive_verified=False, discard_unpromoted=True)["blocker_details"], [])
        self.assertEqual(self.codes(self.plan(discard_unpromoted=True)), ["catalog-snapshot"])
        self.assertEqual(self.codes(self.plan(views=[view()], archive_verified=False)),
                         ["dependent-copies", "archive-required"])

    def test_a_container_on_the_home_names_the_home_files(self):
        record = home()
        plan = self.plan(containers=[container(record["hub_path"], {MANAGED: "true", SPEC_LABEL: "01" * 32},
                                               running=False)])
        detail = plan["blocker_details"][0]
        self.assertEqual(detail["message"], "a container still references the home, including stopped containers")
        self.assertEqual((detail["node_id"], detail["path"], detail["container"]["state"], detail["container"]["spec_id"]),
                         ("n0", record["path"], "stopped", "01" * 32))

    def test_incomplete_preparations_are_a_cause(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            (repo / "releases").mkdir(parents=True)
            store = Store(Path(temporary) / "state")
            record = home()
            store.put("homes", record["snapshot_manifest_id"], record)
            plan = controller.run(store, repo, {
                "operation": "plan-remove", "snapshot_manifest_id": record["snapshot_manifest_id"], "node_ids": ["n0"],
                "observations": observed("n0"), "views": [], "archive_verified": True,
                "transactions": [{"spec_id": SPEC}]})
        self.assertEqual(self.codes(plan), ["incomplete-preparations"])
        self.assertEqual(plan["blocker_details"][-1]["fix"], "purge the prepared copies of each dependent spec first")


class Preparation(Details):
    def setUp(self):
        self.spec = spec_fixture()

    def plan(self, views=(), containers=(), budgets=None, observations=None, **fields):
        return preparation_plan(spec=self.spec, home=home(), node_ids=["n0", "n1"], topology_id=TOPOLOGY,
                                observations=observations or observed("n0", "n1", containers=containers),
                                views=list(views), budgets=budgets or {"n1": budget()}, **fields)

    def mine(self, **fields):
        return view(spec_id=self.spec["spec_id"], **fields)

    def test_an_eligible_plan_has_an_empty_list(self):
        plan = self.plan()
        self.assertEqual(([a["action"] for a in plan["actions"]], plan["blocker_details"]), (["home-view", "copy"], []))

    def test_a_changed_copy_needs_purge(self):
        plan = self.plan(views=[self.mine(topology="e" * 64)])
        self.assert_paired(plan)
        self.assertEqual(plan["blocker_details"], [{
            "code": "changed-copy", "message": "n1: explicitly purge the changed prepared copy before preparing",
            "node_id": "n1", "fix": "purge the prepared copies, then prepare"}])

    def test_a_pinned_and_mounted_copy_lists_both_causes_under_one_message(self):
        old = self.mine(topology="e" * 64, pinned=True)
        plan = self.plan(views=[old], containers=[container(old["hub_path"], {MANAGED: "true", CONF: "ef" * 32})])
        self.assert_paired(plan)
        self.assertEqual([item["code"] for item in plan["blocker_details"]], ["pinned", "container-reference"])
        self.assertEqual({item["message"] for item in plan["blocker_details"]},
                         {"n1: existing prepared copy is pinned or referenced"})
        self.assertEqual(plan["blocker_details"][1]["container"]["spec_id"], "ef" * 32)

    def test_insufficient_space(self):
        plan = self.plan(budgets={"n1": budget(available=11)})
        self.assert_paired(plan)
        self.assertEqual(plan["blocker_details"], [{
            "code": "insufficient-space", "message": "n1: insufficient copy budget or disk space", "node_id": "n1",
            "fix": "free space or purge unused prepared copies on this node"}])

    def test_an_unverified_shared_copy(self):
        candidate = view(spec_id="cd" * 32)
        observations = observed("n0", "n1", n1={"reuse_view": candidate, "view_verified": False})
        plan = self.plan(observations=observations)
        self.assert_paired(plan)
        self.assertEqual([(item["code"], item["node_id"]) for item in plan["blocker_details"]],
                         [("reuse-unverified", "n1")])

    def test_a_set_plan_repeats_member_details_with_their_snapshot(self):
        spec = spec_fixture(snapshots=2)
        members = []
        for name, model in serving.required_snapshots(spec).items():
            manifest = model["snapshot_manifest"]["manifest_id"]
            members.append(preparation_plan(
                spec=spec, home=home(manifest=manifest, name=name), node_ids=["n0", "n1"], topology_id=TOPOLOGY,
                observations=observed("n0", "n1"), snapshot=name,
                views=[view(spec_id=spec["spec_id"], manifest=manifest, topology="e" * 64)] if name == "draft" else [],
                budgets={"n1": budget(available=13)}))
        # Each snapshot fits alone (2 of 3 bytes); together they do not.
        plan = preparation_set_plan(spec, members)
        self.assert_paired(plan)
        self.assertEqual([(item["code"], item.get("snapshot")) for item in plan["blocker_details"]],
                         [("changed-copy", "draft"), ("insufficient-space", None)])
        self.assertEqual(plan["blocker_details"][0]["message"],
                         "draft: n1: explicitly purge the changed prepared copy before preparing")
        combined = "n1: insufficient combined snapshot copy budget or disk space"
        self.assertEqual(plan["blocker_details"][1]["message"], combined)
        self.assertEqual(plan["blockers"].count(combined), 1)


if __name__ == "__main__":
    unittest.main()
