"""Models and disk space…: the page, Free up space…, Keep, Back up and Check, and their plans,
projected from hand-built catalog rows, model files scans and storage budgets. Nothing here
reaches a node."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.node_names import NodeNames  # noqa: E402
from scripts import models_page as mp  # noqa: E402
from tests.test_catalog_menu import checked  # noqa: E402
from tests.test_cluster_menu import FORBIDDEN  # noqa: E402

G = 1024**3
T = 1024**4
SPEC, OTHER, THIRD = "ab" * 32, "cd" * 32, "ef" * 32
MODEL, DRAFT, OTHER_FILES, LOOSE = "11" * 32, "22" * 32, "33" * 32, "44" * 32
NAMES = NodeNames({"n0": "spark-1", "n1": "spark-2", "n2": "spark-3"}, ["n0", "n1", "n2"])
MANAGED = "io.pulsar.gb10.managed"
SELECTED = "io.pulsar.gb10.selected-spec-id"


def utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.now(timezone.utc)


def binding(spec=SPEC, pinned=False):
    return {"spec_id": spec, "pinned": pinned}


def location(node, kind="prepared", *bindings, home=False, users=(), **fields):
    return {"node_id": node, "kind": kind, "hub_path": f"/fixture/{kind}/{node}", "path": f"/fixture/{kind}/{node}",
            "state": "present", "registered_home": home, "bindings": list(bindings), "bindings_observed": True,
            "pinned": any(item["pinned"] for item in bindings), "shared": len({item["spec_id"] for item in bindings}) > 1,
            "container_references": list(users), **fields}


def home(node, *bindings, **fields):
    return location(node, "home", *bindings, home=True, **fields)


def entry(manifest, model, locations, size=212 * G, specs=(), archive=None):
    return {"snapshot_manifest_id": manifest, "model_id": model, "model_commit": "c" * 40, "payload_bytes": size,
            "locations": locations, "archive": archive,
            "catalog_specs": [{"spec_id": spec, "snapshot": "target", "model_id": model} for spec in specs],
            "cleanup_spec_ids": list(specs)}


def node(identity, host, *, free=2 * T, unread=False, unfinished=()):
    scopes = [{"path": "/fixture/homes", "kind": "home", "status": "observed"},
              {"path": "/fixture/views", "kind": "prepared", "status": "observed"}]
    disks = [{"path": "/fixture/homes", "available": free, "total": 4 * T},
             {"path": "/fixture/views", "available": free, "total": 4 * T}]
    found = {"node_id": identity, "hostname": host, "observed_at": utc(NOW), "status": "observed", "error": "",
             "scopes": scopes, "disks": disks, "issues": [], "unidentified": list(unfinished), "container_error": "",
             "records": "observed"}
    if unread:
        found.update(status="unknown", error="node files not observed (exit 255)", scopes=[], disks=[],
                     observed_at=None, records="unreadable")
    return found


def files(snapshots, *, nodes=None, minutes=0):
    return {"schema_version": 1, "kind": "pulsar-model-file-inventory",
            "observed_at": utc(NOW - timedelta(minutes=minutes)),
            "nodes": nodes or [node("n0", "spark-1"), node("n1", "spark-2"), node("n2", "spark-3")],
            "snapshots": snapshots, "issues": []}


def budget(*, short=(), reserve=200 * G):
    """Each node can still copy 1 TiB, or is 10 GiB past its reserve when SHORT."""
    nodes = []
    for identity in ("n0", "n1", "n2"):
        available = reserve - 10 * G if identity in short else reserve + T
        nodes.append({"node_id": identity, "available": available, "used": 300 * G, "total": 4 * T,
                      "reserve": reserve, "limit": 3 * T, "path": "/fixture/views"})
    return {"schema_version": 1, "kind": "pulsar-storage-budget", "nodes": nodes}


def recipe(spec=SPEC, model="org/model", manifest=MODEL, *, home_node="n0", nodes=3, archive=None, **fields):
    row = checked(spec_id=spec, model_id=model, snapshot_manifest_id=manifest, local_state="ready",
                  geometry={"nodes": nodes, "tp": nodes, "pp": 1},
                  home={"node_id": home_node, "path": "/fixture/home", "verified_at": utc(NOW)} if home_node else None,
                  archive=archive, archive_age_seconds=4 * 86400 if archive else None, **fields)
    return row


def archived(days=4):
    return {"snapshot_manifest_id": MODEL, "verified": True, "verified_at": utc(NOW - timedelta(days=days)),
            "root": "/fixture/archive"}


def served(node_id="n1", spec=SPEC, managed=True, name="pulsar-model-r1"):
    labels = {MANAGED: "true", SELECTED: spec} if managed else {}
    return {"id": "c" * 64, "name": name, "running": True, "mounts": [f"/fixture/prepared/{node_id}"],
            "labels": labels}


def ready_model(*, pinned=False, users_on=None, archive=None):
    """org/model: its home on spark-1, which doubles as that node's copy, and copies on spark-2 and spark-3."""
    def users(node_id):
        return [served(node_id)] if users_on == node_id else []
    return entry(MODEL, "org/model", [
        home("n0", binding(pinned=pinned)),
        location("n1", "prepared", binding(pinned=pinned), users=users("n1")),
        location("n2", "prepared", binding(pinned=pinned))], specs=[SPEC], archive=archive)


def cluster(rows, document, budget_document=None, *, archive_location="configured"):
    return mp.Cluster(rows, document, (budget_document or budget())["nodes"], names=NAMES,
                      archive_location=archive_location)


def flat(text):
    return " ".join(text.split())


class Page(unittest.TestCase):
    def test_room_on_each_node_then_each_model_with_size_place_readiness_kept_and_archive(self):
        rows = [recipe(archive=archived()), recipe(OTHER, "org/other", OTHER_FILES, home_node="n1")]
        document = files([ready_model(pinned=True), entry(OTHER_FILES, "org/other", [home("n1")], 60 * G,
                                                           specs=[OTHER])])
        text = flat(mp.page(cluster(rows, document), width=100))
        self.assertIn("Models and disk space · checked less than a minute ago", text)
        self.assertIn("Room spark-1 2.00 TiB free · room for 1.00 TiB", text)
        self.assertIn("Models org/model · 212 GiB · home on spark-1 (also its copy there) · copies on spark-2 and "
                      "spark-3 ready to start · kept · archived, verified 4 days ago", text)
        self.assertIn("org/other · 60.0 GiB · home on spark-2 · no copies on serving nodes", text)
        self.assertIn("no archive recorded", text)
        self.assertIn(mp.EXPLAIN, text)

    def test_a_node_that_did_not_answer_is_unknown_not_absent(self):
        nodes = [node("n0", "spark-1"), node("n1", "spark-2"), node("n2", "spark-3", unread=True)]
        text = flat(mp.page(cluster([recipe()], files([ready_model()], nodes=nodes)), width=100))
        self.assertIn("spark-3 not checked, so its files and room are unknown (node files not observed (exit 255))",
                      text)
        self.assertNotIn("spark-3 2.00 TiB", text)

    def test_a_node_past_its_reserve_says_by_how_much(self):
        text = flat(mp.page(cluster([recipe()], files([ready_model()]), budget(short=("n0",))), width=100))
        self.assertIn("spark-1 2.00 TiB free · 10.0 GiB past its reserve", text)

    def test_without_an_archive_location_back_up_and_check_are_not_offered_and_the_page_says_where(self):
        configured = cluster([recipe()], files([ready_model()]))
        unset = cluster([recipe()], files([ready_model()]), archive_location="not-configured")
        self.assertIn(("backup", "Back up a model…"), mp.page_choices(configured))
        self.assertIn(("check", "Check the archives…"), mp.page_choices(configured))
        labels = [label for _, label in mp.page_choices(unset)]
        self.assertNotIn("Back up a model…", labels)
        self.assertNotIn("Check the archives…", labels)
        self.assertIn(mp.BACKUPS_NEED, flat(mp.page(unset, width=100)))

    def test_files_no_recipe_uses_are_named_with_their_size(self):
        document = files([ready_model(), entry(LOOSE, "org/forgotten", [home("n2")], 50 * G)])
        text = flat(mp.page(cluster([recipe()], document), width=100))
        self.assertIn("org/forgotten @ cccccccc · 50.0 GiB on spark-3 · no recipe in this catalog uses these files",
                      text)


class FreeUpSpace(unittest.TestCase):
    def options(self, rows, document, **extra):
        return mp.free_options(cluster(rows, document, **extra))

    def test_removing_from_serving_nodes_keeps_the_home_and_says_it_comes_back_with_a_copy(self):
        removable, _ = self.options([recipe()], files([ready_model()]))
        purge = next(option for option in removable if option["target"] == f"purge:{SPEC}")
        # The Model files page's estimate: the two copies only this recipe uses; the home frees nothing.
        self.assertEqual(purge["size"], 2 * 212 * G)
        self.assertEqual(flat(purge["text"]), "Remove org/model from spark-2 and spark-3: frees 424 GiB. Its home on "
                                              "spark-1 stays, so serving it again copies from there, with no download.")

    def test_removing_from_the_cluster_says_the_archive_state_largest_gain_first(self):
        removable, _ = self.options([recipe(archive=archived())], files([ready_model()]))
        self.assertEqual([option["target"] for option in removable], [f"cluster:{SPEC}", f"purge:{SPEC}"])
        self.assertEqual(removable[0]["size"], 3 * 212 * G)
        self.assertEqual(flat(removable[0]["text"]),
                         "Remove org/model from the cluster: frees 636 GiB. It deletes its copies, then its home on "
                         "spark-1, the copy Pulsar keeps. Its archive was verified 4 days ago, so it can be restored "
                         "without the internet.")

    def test_without_an_archive_a_download_would_be_needed_and_backing_up_first_is_offered(self):
        removable, _ = self.options([recipe()], files([ready_model()]))
        self.assertIn("It has no verified archive, so getting it back means downloading it again (212 GiB). Its plan "
                      "offers to back it up first.", flat(removable[0]["text"]))
        removable, _ = self.options([recipe()], files([ready_model()]), archive_location="not-configured")
        self.assertIn("Backing it up first needs the archive location; Cluster… → Settings… sets it.",
                      flat(removable[0]["text"]))

    def test_a_serving_model_is_not_removable_and_names_running_services(self):
        removable, blocked = self.options([recipe()], files([ready_model(users_on="n1")]))
        self.assertEqual(removable, [])
        self.assertIn("org/model is serving on spark-2, or its stopped containers remain there. Stop it through "
                      "Running services… first.", [text for text, _ in blocked])

    def test_a_kept_model_is_not_removable_and_offers_to_stop_keeping_it_never_unpinning_on_its_own(self):
        removable, blocked = self.options([recipe()], files([ready_model(pinned=True)]))
        self.assertEqual(removable, [])
        self.assertIn(("org/model is kept ready on spark-1, spark-2 and spark-3, so Free up space… does not offer it.",
                       (f"unkeep:{SPEC}", "Stop keeping org/model ready")), blocked)
        self.assertEqual(mp.free_choices(cluster([recipe()], files([ready_model(pinned=True)]))),
                         [(f"unkeep:{SPEC}", "Stop keeping org/model ready"), ("back", "Back")])

    def test_a_container_pulsar_did_not_start_is_named_read_only(self):
        document = files([entry(MODEL, "org/model", [
            home("n0", binding()), location("n1", "prepared", binding(), users=[served(managed=False, name="lab-vllm")]),
            location("n2", "prepared", binding())], specs=[SPEC])])
        removable, blocked = self.options([recipe()], document)
        self.assertEqual(removable, [])
        self.assertIn("lab-vllm, a container Pulsar did not start, mounts the files of org/model on spark-2. Pulsar "
                      "never removes it (read-only).", [text for text, _ in blocked])

    def test_a_copy_another_recipe_shares_frees_nothing_and_its_files_stay(self):
        rows = [recipe(), recipe(OTHER, "org/model", MODEL, nodes=2,
                                 engine_args=["--max-model-len", "8192"])]
        document = files([entry(MODEL, "org/model", [
            home("n0", binding(), binding(OTHER)), location("n1", "prepared", binding(), binding(OTHER))],
            specs=[SPEC, OTHER])])
        removable, blocked = self.options(rows, document)
        self.assertFalse([option for option in removable if option["target"].startswith("purge:")])
        self.assertTrue(any("are shared with" in text and "their files stay, so removing them frees nothing" in text
                            for text, _ in blocked), blocked)
        # Removing the model from the cluster purges both recipes, then its home.
        cluster_line = next(option for option in removable if option["target"].startswith("cluster:"))
        self.assertTrue(cluster_line["label"].startswith("Remove org/model from the cluster: frees "), cluster_line)

    def test_a_node_that_did_not_answer_offers_nothing_destructive_anywhere(self):
        nodes = [node("n0", "spark-1"), node("n1", "spark-2"), node("n2", "spark-3", unread=True)]
        removable, blocked = self.options([recipe()], files([ready_model()], nodes=nodes))
        self.assertEqual(removable, [])
        self.assertIn("spark-3 could not be checked, so the files and room there are unknown, not absent.",
                      blocked[0][0])

    def test_unfinished_copies_are_cleared_through_their_recipe_and_unrecorded_ones_never(self):
        stage = {"path": "/fixture/views/.pending-x", "kind": "incomplete", "reason": "incomplete staging",
                 "preparation_spec_id": OTHER, "container_references": []}
        loose = {"path": "/fixture/homes/.pending-y", "kind": "incomplete", "reason": "incomplete staging"}
        nodes = [node("n0", "spark-1"), node("n1", "spark-2", unfinished=[stage, loose]), node("n2", "spark-3")]
        rows = [recipe(), recipe(OTHER, "org/other", OTHER_FILES, home_node=None)]
        removable, blocked = self.options(rows, files([ready_model()], nodes=nodes))
        clear = next(option for option in removable if option["target"] == f"clear:{OTHER}")
        # Unfinished copies have no size in the scan: unknown, never zero, and listed last.
        self.assertIsNone(clear["size"])
        self.assertEqual(removable[-1], clear)
        self.assertEqual(clear["label"], "Clear 1 unfinished copy of org/other on spark-2: frees an unknown amount")
        self.assertIn("1 unfinished download or copy on spark-2 has no record naming a recipe. Pulsar never deletes "
                      "files it has no record of.", [text for text, _ in blocked])

    def test_a_home_moves_only_off_a_node_past_its_reserve_to_one_with_room_when_nothing_depends_on_it(self):
        rows = [recipe(OTHER, "org/other", OTHER_FILES, home_node="n0", nodes=1)]
        document = files([entry(OTHER_FILES, "org/other", [home("n0")], 60 * G, specs=[OTHER])])
        removable, _ = self.options(rows, document, budget_document=budget(short=("n0",)))
        move = next(option for option in removable if option["target"].startswith("move:"))
        self.assertEqual(move["target"], f"move:{OTHER_FILES}:n1")
        self.assertEqual(flat(move["text"]), "Move the files of org/other from spark-1 to spark-2: frees 60.0 GiB on "
                                             "spark-1. spark-1 is 10.0 GiB past its reserve and spark-2 has room. Its "
                                             "home, the copy Pulsar keeps, moves; nothing is lost.")
        # No node short: no move. A copy depending on the home: no move (Move home refuses it).
        removable, _ = self.options(rows, document)
        self.assertFalse([option for option in removable if option["target"].startswith("move:")])
        bound = files([entry(OTHER_FILES, "org/other", [home("n0", binding(OTHER))], 60 * G, specs=[OTHER])])
        removable, _ = self.options(rows, bound, budget_document=budget(short=("n0",)))
        self.assertFalse([option for option in removable if option["target"].startswith("move:")])

    def test_every_label_uses_plain_words(self):
        rows = [recipe(), recipe(OTHER, "org/other", OTHER_FILES, home_node="n0", nodes=1)]
        document = files([ready_model(), entry(OTHER_FILES, "org/other", [home("n0")], 60 * G, specs=[OTHER])])
        for found in (cluster(rows, document, budget(short=("n0",))),
                      cluster(rows, files([ready_model(pinned=True)]))):
            labels = [label for _, label in mp.free_choices(found)]
            labels += [label for _, label in mp.page_choices(found)]
            for intent in ("keep", "backup", "check"):
                labels += [label for _, label in mp.intent_choices(found, intent)]
            self.assertTrue(labels)
            for label in labels:
                self.assertIsNone(FORBIDDEN.search(label), label)


class Plans(unittest.TestCase):
    def test_remove_from_serving_nodes_is_one_purge_step_with_its_preview_now(self):
        plan = mp.compose(cluster([recipe()], files([ready_model()])), f"purge:{SPEC}")
        self.assertEqual([step["kind"] for step in plan["steps"]], ["purge"])
        step = plan["steps"][0]
        self.assertEqual(step["command"], ["./pulsar", "model", "purge", SPEC, "--yes"])
        self.assertEqual(step["preview"], ["./pulsar", "model", "purge", SPEC, "--plan", "--json"])
        self.assertTrue(step["preview_now"])
        self.assertEqual(step["shown"], f"./pulsar model purge {SPEC[:12]} --yes")
        self.assertEqual(mp.plan_choices(plan), [("cancel", "Cancel"), ("go", "Go ahead")])

    def test_remove_from_the_cluster_without_an_archive_offers_backing_up_first(self):
        found = cluster([recipe()], files([ready_model()]))
        plan = mp.compose(found, f"cluster:{SPEC}")
        self.assertEqual([step["kind"] for step in plan["steps"]], ["purge", "remove"])
        # Remove home's preview refuses while copies depend on the home: it is an estimate until Purge ran.
        self.assertEqual(plan["projected"], [1])
        self.assertIn(("archive-first", "Back it up first, then remove it"), mp.plan_choices(plan))
        text = flat(mp.render(plan, width=100))
        self.assertIn("It has no verified archive, so getting it back means downloading it again (212 GiB).", text)
        first = mp.compose(found, f"cluster:{SPEC}", archive_first=True)
        self.assertEqual([step["kind"] for step in first["steps"]], ["archive", "purge", "remove"])
        self.assertEqual(first["steps"][0]["command"], ["./pulsar", "model", "archive", "create", SPEC, "--yes"])
        self.assertNotIn(("archive-first", "Back it up first, then remove it"), mp.plan_choices(first))
        self.assertIn("Its archive is made first and stays", flat(mp.render(first, width=100)))

    def test_remove_from_the_cluster_with_an_archive_says_it_can_be_restored(self):
        plan = mp.compose(cluster([recipe(archive=archived())], files([ready_model()])), f"cluster:{SPEC}")
        self.assertEqual(mp.plan_choices(plan), [("cancel", "Cancel"), ("go", "Go ahead")])
        self.assertIn("Its archive stays, so it can be restored without the internet.",
                      flat(mp.render(plan, width=100)))

    def test_a_preview_that_refuses_blocks_the_plan_and_one_with_nothing_left_drops_its_step(self):
        found = cluster([recipe()], files([ready_model()]))
        refused = {"ok": True, "result": {"plan": {"kind": "pulsar-purge-plan", "eligible": False,
                                                   "blockers": ["prepared copy is pinned"],
                                                   "blocker_details": [{"code": "pinned", "message": "pinned",
                                                                        "node_id": "n1", "fix": "unpin first"}],
                                                   "actions": [], "views": []}, "incomplete_preparations": []}}
        plan = mp.compose(found, f"purge:{SPEC}", previews={"0": (refused, 0)})
        self.assertNotIn(("go", "Go ahead"), mp.plan_choices(plan))
        self.assertTrue(plan["blocked"])
        empty = {"ok": True, "result": {"plan": {"kind": "pulsar-purge-plan", "eligible": True, "blockers": [],
                                                 "actions": [], "views": []}, "incomplete_preparations": []}}
        plan = mp.compose(found, f"purge:{SPEC}", previews={"0": (empty, 0)})
        self.assertEqual(plan["steps"], [])
        self.assertIn("Nothing to do", mp.render(plan, width=100))

    def test_a_step_whose_own_preview_deletes_more_than_the_plan_said_differs(self):
        step = mp.compose(cluster([recipe()], files([ready_model()])), f"purge:{SPEC}")["steps"][0]
        more = {"plan": {"kind": "pulsar-purge-plan", "eligible": True, "blockers": [], "views": [],
                         "actions": [{"node_id": "n0", "action": "remove-copy"}, {"node_id": "n1", "action": "remove-copy"}]},
                "incomplete_preparations": []}
        self.assertEqual(mp.assess(step, more, 0, NAMES), ("differs", "it would also delete copies on spark-1"))

    def test_keep_and_stop_keeping_are_pin_and_unpin(self):
        found = cluster([recipe()], files([ready_model()]))
        self.assertEqual(mp.intent_choices(found, "keep"), [(f"keep:{SPEC}", "Keep org/model ready"), ("back", "Back")])
        plan = mp.compose(found, f"keep:{SPEC}")
        self.assertEqual(plan["steps"][0]["command"], ["./pulsar", "model", "pin", SPEC, "--yes"])
        self.assertIn("Pulsar marks them as kept (pins them), so Free up space… never offers them",
                      flat(mp.render(plan, width=100)))
        kept = cluster([recipe()], files([ready_model(pinned=True)]))
        self.assertEqual(mp.intent_choices(kept, "keep"),
                         [(f"unkeep:{SPEC}", "Stop keeping org/model ready"), ("back", "Back")])
        self.assertEqual(mp.compose(kept, f"unkeep:{SPEC}")["steps"][0]["command"],
                         ["./pulsar", "model", "unpin", SPEC, "--yes"])

    def test_back_up_covers_every_required_snapshot_in_one_plan_and_skips_what_is_archived(self):
        member = {"model_id": "org/model", "model_commit": "c" * 40}
        row = recipe(snapshots={
            "target": {**member, "snapshot_manifest_id": MODEL, "home": {"node_id": "n0"}, "archive": None},
            "draft": {**member, "model_id": "org/draft", "snapshot_manifest_id": DRAFT, "home": {"node_id": "n1"},
                      "archive": None}})
        document = files([ready_model(), entry(DRAFT, "org/draft", [home("n1", binding())], 4 * G, specs=[SPEC])])
        plan = mp.compose(cluster([row], document), f"backup:{SPEC}")
        self.assertEqual([step["command"] for step in plan["steps"]], [
            ["./pulsar", "model", "archive", "create", SPEC, "--snapshot", "target", "--yes"],
            ["./pulsar", "model", "archive", "create", SPEC, "--snapshot", "draft", "--yes"]])
        text = flat(mp.render(plan, width=100))
        self.assertIn("1. Copy the model files from its home on spark-1 into the archive (212 GiB).", text)
        self.assertIn("2. Copy the files of org/draft (a model it also loads) from its home on spark-2 into the "
                      "archive (4.00 GiB).", text)
        self.assertIn("No archive is replaced or deleted.", text)
        row["snapshots"]["target"]["archive"] = archived()
        plan = mp.compose(cluster([row], document), f"backup:{SPEC}")
        self.assertEqual(len(plan["steps"]), 1)
        self.assertIn("archived already, verified 4 days ago", flat(mp.render(plan, width=100)))

    def test_check_the_archives_verifies_each_archived_snapshot_with_no_preview(self):
        plan = mp.compose(cluster([recipe(archive=archived())], files([ready_model()])), f"check:{SPEC}")
        self.assertEqual(plan["steps"][0]["command"], ["./pulsar", "model", "archive", "verify", SPEC])
        self.assertIsNone(plan["steps"][0]["preview"])
        self.assertIn("nothing is changed", flat(mp.render(plan, width=100)))

    def test_the_outcome_names_the_commands_that_ran(self):
        plan = mp.compose(cluster([recipe()], files([ready_model()])), f"cluster:{SPEC}")
        text = flat(mp.outcome(plan, done=2, ran=[step["shown"] for step in plan["steps"]], width=100))
        self.assertIn("✓ Done: org/model is removed from the cluster.", text)
        self.assertIn(f"Same as: ./pulsar model purge {SPEC[:12]} --yes ./pulsar model remove {SPEC[:12]} --yes", text)
        failed = flat(mp.outcome(plan, done=1, failed=1, rc=1, ran=[step["shown"] for step in plan["steps"]], width=100))
        self.assertIn("✗ Step 2 did not finish: Delete the home of the model files on spark-1 (212 GiB). The reason is "
                      "above. Done: 1 step before it.", failed)


class Widths(unittest.TestCase):
    def test_every_screen_fits_80_60_and_44_columns(self):
        rows = [recipe(archive=archived()), recipe(OTHER, "organization-with-a-long-name/model-family-instruct-fp8",
                                                   OTHER_FILES, home_node="n0", nodes=1)]
        document = files([ready_model(), entry(OTHER_FILES, "organization-with-a-long-name/model-family-instruct-fp8",
                                               [home("n0")], 60 * G, specs=[OTHER])])
        found = cluster(rows, document, budget(short=("n0",)))
        screens = [mp.page, mp.free_screen]
        for width in (80, 60, 44):
            with self.subTest(width=width):
                texts = [screen(found, width=width) for screen in screens]
                texts += [mp.intent_screen(found, intent, width=width) for intent in ("keep", "backup", "check")]
                texts += [mp.render(mp.compose(found, target), width=width)
                          for target, _ in mp.free_choices(found) if target != "back"]
                for text in texts:
                    self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)


if __name__ == "__main__":
    unittest.main()
