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


def purge_preview(*actions):
    """Purge's own plan, a (node, action) per copy of the recipe: "release-binding" where the files
    stay (the home, or a copy another recipe still uses) and "remove-copy" where they are deleted."""
    return {"ok": True, "result": {"plan": {"kind": "pulsar-purge-plan", "eligible": True, "blockers": [], "views": [],
                                            "actions": [{"rank": rank, "node_id": node, "action": action}
                                                        for rank, (node, action) in enumerate(actions)]},
                                   "incomplete_preparations": []}}


def cluster(rows, document, budget_document=None, *, archive_location="configured", saved=None):
    # Ages are counted from NOW, when the fixtures were made, not from when a test runs.
    return mp.Cluster(rows, document, (budget_document or budget())["nodes"], names=NAMES,
                      archive_location=archive_location, saved=saved, now=NOW)


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
                      "spark-3 ready to start (checked 2 hours ago) · kept · archived, verified 4 days ago", text)
        self.assertIn("org/other · 60.0 GiB · home on spark-2 · no copies on serving nodes", text)
        self.assertIn("no archive recorded", text)
        self.assertIn(mp.EXPLAIN, text)

    def test_a_copy_the_scan_did_not_find_is_said_instead_of_the_saved_ready_to_start(self):
        # The saved check found the files ready two hours ago; the scan is newer and did not find one copy.
        document = files([entry(MODEL, "org/model", [
            home("n0", binding()), location("n1", "prepared", binding()),
            location("n2", "prepared", binding(), state="missing")], specs=[SPEC])])
        text = flat(mp.page(cluster([recipe()], document), width=100))
        self.assertIn("Models and disk space · checked less than a minute ago", text)
        self.assertIn(" files not found on spark-3 · not kept · no archive recorded", text)
        self.assertNotIn("ready to start", text)
        # A home the scan did not find counts too, and a copy it could not check does not hide it.
        document = files([entry(MODEL, "org/model", [
            home("n0", binding(), state="missing"), location("n1", "prepared", binding(), state="unknown"),
            location("n2", "prepared", binding())], specs=[SPEC])])
        text = flat(mp.page(cluster([recipe()], document), width=100))
        self.assertIn(" files not found on spark-1 · not kept", text)
        self.assertNotIn("ready to start", text)

    def test_a_copy_the_scan_could_not_check_is_unknown_not_ready_and_not_absent(self):
        document = files([entry(MODEL, "org/model", [
            home("n0", binding()), location("n1", "prepared", binding(), state="unknown"),
            location("n2", "prepared", binding(), state="unknown")], specs=[SPEC])])
        text = flat(mp.page(cluster([recipe()], document), width=100))
        self.assertIn(" files could not be checked on spark-2 and spark-3 · not kept · no archive recorded", text)
        self.assertNotIn("ready to start", text)
        self.assertNotIn("not found", text)
        narrow = mp.page(cluster([recipe()], document), width=44)
        self.assertTrue(all(len(line) <= 44 for line in narrow.splitlines()), narrow)

    def test_a_copy_recorded_on_a_node_outside_confirmed_membership_does_not_hide_ready_to_start(self):
        from model_library.file_inventory import OUTSIDE_MEMBERSHIP
        # No scan reads a node that left the membership, so its records are unknown on every scan.
        gone = {**node("n9", "n9", unread=True), "error": OUTSIDE_MEMBERSHIP}
        nodes = [node("n0", "spark-1"), node("n1", "spark-2"), node("n2", "spark-3"), gone]
        document = files([entry(MODEL, "org/model", [
            home("n0", binding()), location("n1", "prepared", binding()), location("n2", "prepared", binding()),
            location("n9", "prepared", binding(), state="unknown")], specs=[SPEC])], nodes=nodes)
        text = flat(mp.page(cluster([recipe()], document), width=100))
        self.assertIn(" ready to start (checked 2 hours ago) · not kept", text)
        self.assertNotIn("could not be checked", text)

    def test_a_retained_ready_to_start_carries_the_age_of_the_saved_check_not_of_the_scan(self):
        text = flat(mp.page(cluster([recipe()], files([ready_model()])), width=100))
        self.assertIn("Models and disk space · checked less than a minute ago", text)
        self.assertIn(" ready to start (checked 2 hours ago) · not kept · no archive recorded", text)
        row = recipe(observation_age_seconds=None)
        self.assertIn(" ready to start (check time unknown) · not kept",
                      flat(mp.page(cluster([row], files([ready_model()])), width=100)))

    def test_what_the_saved_records_say_is_not_ready_stays_as_serve_says_it(self):
        # Only the saved check's "ready to start" gives way to the scan; these phrases claim no readiness.
        document = files([entry(MODEL, "org/model", [
            home("n0", binding()), location("n1", "prepared", binding(), state="missing")], specs=[SPEC])])
        row = {**recipe(), "local_state": "missing"}
        text = flat(mp.page(cluster([row], document), width=100))
        self.assertIn(" needs copying to 2 nodes · 424 GiB · not kept", text)

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
        self.assertIn(("backup", "Back up a model to the archive…"), mp.page_choices(configured))
        self.assertIn(("check", "Check the archive copies…"), mp.page_choices(configured))
        labels = [label for _, label in mp.page_choices(unset)]
        self.assertNotIn("Back up a model to the archive…", labels)
        self.assertNotIn("Check the archive copies…", labels)
        self.assertIn(mp.BACKUPS_NEED, flat(mp.page(unset, width=100)))

    def test_files_no_recipe_uses_are_named_with_their_size(self):
        document = files([ready_model(), entry(LOOSE, "org/forgotten", [home("n2")], 50 * G)])
        text = flat(mp.page(cluster([recipe()], document), width=100))
        self.assertIn("org/forgotten @ cccccccc · 50.0 GiB on spark-3 · no recipe in this catalog uses these files "
                      "· no archive recorded", text)

    def test_the_choices_follow_a_models_life_on_the_cluster_in_four_groups(self):
        found = cluster([recipe()], files([ready_model()]))
        self.assertEqual([[action for action, _ in group] for group in mp.page_groups(found)],
                         [["download", "ready", "move"], ["keep", "backup", "check"], ["free"], ["find", "again", "back"]])
        self.assertEqual([label for _, label in mp.page_choices(found)],
                         ["Download a model that has no recipe yet…", "Get a model with a recipe ready to serve…",
                          "Move a model to another node…", "Keep a model on the cluster…", "Back up a model to the archive…",
                          "Check the archive copies…", "Free up space…", "Record model files found on the nodes…",
                          "Look again (checks nodes now)", "Back"])

    def test_a_choice_that_needs_it_has_a_second_line_and_a_group_ends_with_a_blank_line(self):
        found = cluster([recipe()], files([ready_model()]))
        cards = {action: card.split("\x1f") for action, _, card in mp.page_cards(found, 80)}
        self.assertEqual(cards["download"], ["Download a model that has no recipe yet…",
                                             "    files and hashes only, to one node"])
        # The last choice of a group ends with an empty line; the last group does not.
        self.assertEqual(cards["ready"], ["Get a model with a recipe ready to serve…",
                                          "    files, copies and image; nothing starts"])
        self.assertEqual(cards["move"], ["Move a model to another node…",
                                         "    copies, checks, then frees the old node", ""])
        self.assertEqual(cards["check"], ["Check the archive copies…", "    compares hashes; changes nothing", ""])
        self.assertEqual(cards["free"], ["Free up space…", "    remove a model's files or move them", ""])
        # A label that says it all has no second line.
        self.assertEqual((cards["backup"], cards["again"], cards["back"]),
                         (["Back up a model to the archive…"], ["Look again (checks nodes now)"], ["Back"]))
        # Without an archive location the group ends with the choice that is left.
        unset = cluster([recipe()], files([ready_model()]), archive_location="not-configured")
        self.assertEqual(dict((action, card) for action, _, card in mp.page_cards(unset, 80))["keep"].split("\x1f"),
                         ["Keep a model on the cluster…", "    Free up space never removes it", ""])

    def test_a_second_line_is_short_plain_and_one_line_at_44_columns(self):
        for action, hint in mp.HINTS.items():
            with self.subTest(action=action):
                self.assertLessEqual(len(hint), 40)
                self.assertEqual(hint, hint[0] + hint[1:].rstrip("."))
                self.assertIsNone(FORBIDDEN.search(hint), hint)
        found = cluster([recipe()], files([ready_model()]))
        for width in (80, 60, 44):
            for _, _, card in mp.page_cards(found, width):
                lines = card.split("\x1f")
                # The cursor gutter takes two columns before a card's first line.
                self.assertTrue(len(lines[0]) + 2 <= width and all(len(line) <= width for line in lines[1:]), card)
                self.assertLessEqual(len([line for line in lines if line]), 2, card)
        # Narrower, a line wraps between words, under its own first column.
        narrow = dict((action, card) for action, _, card in mp.page_cards(found, 32))
        self.assertEqual(narrow["download"].split("\x1f"),
                         ["Download a model that has no", "    recipe yet…", "    files and hashes only, to",
                          "    one node"])

    def test_only_a_label_is_painted_never_its_second_line(self):
        found = cluster([recipe()], files([ready_model()]))
        cards = dict((action, card) for action, _, card in mp.page_cards(found, 80, lambda line: f"<{line}>"))
        self.assertEqual(cards["download"].split("\x1f"), ["<Download a model that has no recipe yet…>",
                                                           "    files and hashes only, to one node"])
        self.assertEqual(cards["back"], "<Back>")


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
        # The screen says it in the page's own word: kept, not unpinned.
        text = flat(mp.free_screen(cluster([recipe()], files([ready_model(pinned=True)])), width=100))
        self.assertIn("Nothing that is kept stops being kept, no archive is deleted", text)
        self.assertNotIn("unpinned", text)
        self.assertEqual(removable, [])
        self.assertIn(("org/model is kept on spark-1, spark-2 and spark-3, so Free up space… does not offer it.",
                       (f"unkeep:{SPEC}", "Stop keeping org/model")), blocked)
        self.assertEqual(mp.free_choices(cluster([recipe()], files([ready_model(pinned=True)]))),
                         [(f"unkeep:{SPEC}", "Stop keeping org/model"), ("back", "Back")])

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

    def test_recipes_outside_the_catalog_that_share_a_copy_are_counted_once(self):
        outside = [f"{index:02d}" * 32 for index in range(1, 6)]
        cases = (
            (outside[:1], [], "a recipe outside this catalog"),
            (outside, [], "5 recipes outside this catalog"),
            (outside[:2], [OTHER], "org/model (2 nodes) and 2 recipes outside this catalog"),
        )
        for sharers, catalog, expected in cases:
            with self.subTest(expected=expected):
                rows = [recipe()] + [recipe(other, "org/model", MODEL, nodes=2, engine_args=["--max-model-len", "8192"])
                                     for other in catalog]
                bindings = [binding(), *(binding(other) for other in catalog + sharers)]
                document = files([entry(MODEL, "org/model", [
                    home("n0", *bindings), location("n1", "prepared", *bindings)], specs=[SPEC, *catalog])])
                _, blocked = self.options(rows, document)
                line = next(text for text, _ in blocked if "are shared with" in text)
                self.assertIn(f" are shared with {expected}; their files stay, so removing them frees nothing.", line)
                self.assertEqual(line.count("outside this catalog"), 1 if sharers else 0)

    def test_services_started_outside_the_catalog_are_counted_once(self):
        owners = ["0a" * 32, "0b" * 32]
        users = [served("n1", spec=owner, name=f"pulsar-x{index}") for index, owner in enumerate(owners)]
        document = files([entry(MODEL, "org/model", [
            home("n0", binding()), location("n1", "prepared", binding(), users=users),
            location("n2", "prepared", binding(), users=[served("n2", spec=owners[0], name="pulsar-x0")])],
            specs=[SPEC])])
        _, blocked = self.options([recipe()], document)
        self.assertIn("2 Pulsar services started outside this catalog use the files of org/model on spark-2 and "
                      "spark-3. Stop them through Running services… first.", [text for text, _ in blocked])

    def test_a_node_that_did_not_answer_offers_nothing_destructive_anywhere(self):
        nodes = [node("n0", "spark-1"), node("n1", "spark-2"), node("n2", "spark-3", unread=True)]
        removable, blocked = self.options([recipe()], files([ready_model()], nodes=nodes))
        self.assertEqual(removable, [])
        self.assertIn("spark-3 could not be checked, so the files and room there are unknown, not absent.",
                      blocked[0][0])

    def test_a_copy_that_could_not_be_checked_on_a_node_that_answered_offers_nothing_for_that_model(self):
        document = files([entry(MODEL, "org/model", [
            home("n0", binding()), location("n1", "prepared", binding()),
            location("n2", "prepared", binding(), state="unknown")], specs=[SPEC])])
        found = cluster([recipe()], document)
        removable, blocked = mp.free_options(found)
        # A partial size would read as the whole one: no line for it, and the node is named.
        self.assertEqual(removable, [])
        self.assertIn("What removing org/model would free on spark-3 is unknown: its files there could not be fully "
                      "checked, so nothing is offered for it. Choose Look again.", [text for text, _ in blocked])
        self.assertTrue(mp.compose(found, f"purge:{SPEC}")["blocked"])

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
        self.assertIn("Its archive stays, verified 4 days ago, so it can be restored without the internet.",
                      flat(mp.render(plan, width=100)))
        # Without the archive location it cannot be restored from it until that is set.
        unset = mp.compose(cluster([recipe(archive=archived())], files([ready_model()]),
                                   archive_location="not-configured"), f"cluster:{SPEC}")
        self.assertIn("Its archive was verified 4 days ago, but the archive location is not set, so restoring it "
                      "first needs Cluster… → Settings….", flat(mp.render(unset, width=100)))

    def test_a_one_node_recipe_served_from_its_home_releases_it_and_deletes_only_the_home(self):
        row = recipe(OTHER, "org/other", OTHER_FILES, home_node="n0", nodes=1)
        document = files([entry(OTHER_FILES, "org/other", [home("n0", binding(OTHER))], 60 * G, specs=[OTHER])])
        found = cluster([row], document)
        removable, _ = mp.free_options(found)
        self.assertEqual([option["target"] for option in removable], [f"cluster:{OTHER}"])
        self.assertTrue(flat(removable[0]["text"]).startswith(
            "Remove org/other from the cluster: frees 60.0 GiB. It deletes its home on spark-1"), removable[0])
        plan = mp.compose(found, f"cluster:{OTHER}")
        self.assertEqual([step["kind"] for step in plan["steps"]], ["purge", "remove"])
        self.assertEqual(flat(plan["steps"][0]["sentence"]), "Release the copies of org/other; their files stay for "
                                                             "the steps after it, and this step deletes none.")

    def test_copies_two_recipes_share_make_the_cluster_figure_a_minimum(self):
        rows = [recipe(), recipe(OTHER, "org/model", MODEL, nodes=2, engine_args=["--max-model-len", "8192"])]
        document = files([entry(MODEL, "org/model", [
            home("n0", binding(), binding(OTHER)), location("n1", "prepared", binding(), binding(OTHER))],
            specs=[SPEC, OTHER])])
        removable, _ = mp.free_options(cluster(rows, document))
        line = next(option for option in removable if option["target"].startswith("cluster:"))
        self.assertTrue(flat(line["text"]).startswith("Remove org/model from the cluster: frees at least 212 GiB. It "
                                                      "deletes its copies, then its home"), line)

    def test_a_copy_two_recipes_share_is_deleted_by_the_purge_that_releases_it_last(self):
        rows = [recipe(), recipe(OTHER, "org/model", MODEL, nodes=2, engine_args=["--max-model-len", "8192"])]
        document = files([entry(MODEL, "org/model", [
            home("n0", binding(), binding(OTHER)), location("n1", "prepared", binding(), binding(OTHER))],
            specs=[SPEC, OTHER])])
        # Before anything runs, each recipe's own preview only releases the copy: the other still uses it.
        released = purge_preview(("n0", "release-binding"), ("n1", "release-binding"))
        plan = mp.compose(cluster(rows, document), f"cluster:{SPEC}", previews={"0": (released, 0), "1": (released, 0)})
        self.assertEqual([(step["kind"], step["facts"]["spec"]) for step in plan["steps"]],
                         [("purge", SPEC), ("purge", OTHER), ("remove", SPEC)])
        self.assertEqual(plan["blocked"], [])
        first, second = plan["steps"][:2]
        self.assertEqual(first["facts"]["nodes"], [])
        self.assertEqual(flat(first["sentence"]), "Release the copies of org/model (3 nodes); their files stay for "
                                                  "the steps after it, and this step deletes none.")
        # Once the first Purge ran, the copy is the second recipe's alone, so its Purge deletes it: the
        # plan says so, and that step's own preview at its turn is what the plan said.
        self.assertEqual(second["facts"]["nodes"], ["n1"])
        self.assertEqual(flat(second["sentence"]), "Delete the copies of org/model (2 nodes) on spark-2 (212 GiB).")
        last = purge_preview(("n0", "release-binding"), ("n1", "remove-copy"))
        self.assertEqual(mp.assess(second, last, 0, NAMES), ("same", ""))
        # The first step approved no deletion: the same preview for it still stops the run.
        self.assertEqual(mp.assess(first, last, 0, NAMES), ("differs", "it would also delete copies on spark-2"))

    def test_each_purge_of_a_cluster_plan_deletes_only_what_the_steps_before_it_leave_to_it(self):
        rows = [recipe(), recipe(OTHER, "org/model", MODEL, nodes=2, engine_args=["--max-model-len", "8192"])]
        document = files([entry(MODEL, "org/model", [
            home("n0", binding(), binding(OTHER)), location("n1", "prepared", binding(), binding(OTHER)),
            location("n2", "prepared", binding())], specs=[SPEC, OTHER])])
        found = cluster(rows, document)
        first, second = mp.compose(found, f"cluster:{SPEC}")["steps"][:2]
        # The first recipe's own copy goes with it; the copy it shares waits for the second recipe's Purge.
        self.assertEqual((first["facts"]["spec"], first["facts"]["nodes"]), (SPEC, ["n2"]))
        self.assertEqual(flat(first["sentence"]), "Delete the copies of org/model (3 nodes) on spark-3 (212 GiB).")
        self.assertEqual((second["facts"]["spec"], second["facts"]["nodes"]), (OTHER, ["n1"]))
        # The scan the page shows is not changed by planning from it.
        self.assertEqual(mp.purge_facts(found, OTHER)["freed"], {})

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
        self.assertEqual(mp.intent_choices(found, "keep"), [(f"keep:{SPEC}", "Keep org/model on the cluster"), ("back", "Back")])
        plan = mp.compose(found, f"keep:{SPEC}")
        self.assertEqual(plan["steps"][0]["command"], ["./pulsar", "model", "pin", SPEC, "--yes"])
        self.assertIn("Pulsar marks them as kept (pins them), so Free up space… never offers them",
                      flat(mp.render(plan, width=100)))
        kept = cluster([recipe()], files([ready_model(pinned=True)]))
        self.assertEqual(mp.intent_choices(kept, "keep"),
                         [(f"unkeep:{SPEC}", "Stop keeping org/model"), ("back", "Back")])
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


def found_home(node):
    """A home folder no record names."""
    return location(node, "home", hub_path=f"/fixture/homes/{MODEL}", path=f"/fixture/homes/{MODEL}/snapshots/x")


def lost_copy(node, spec=SPEC, **fields):
    """A prepared copy's folder no record names, at the path Prepare uses for SPEC (a schema-2 spec's ID)."""
    return location(node, "prepared", hub_path=f"/fixture/views/{spec}", path=f"/fixture/views/{spec}/snapshots/x",
                    **fields)


def prepare_preview(*actions):
    return {"ok": True, "result": {"kind": "pulsar-preparation-plan", "eligible": True, "blockers": [],
                                   "blocker_details": [], "total_bytes": 212 * G, "budgets": {},
                                   "actions": [{"rank": rank, "node_id": node, "action": action}
                                               for rank, (node, action) in enumerate(actions)]}}


class LookForFiles(unittest.TestCase):
    """Record model files found on the nodes…: one plan from the page's scan."""

    def test_downloads_or_copies_that_never_finished_are_said_to_once(self):
        loose = {"path": "/fixture/homes/.pending-y", "kind": "incomplete", "reason": "incomplete staging"}
        for count, words in ((1, "1 download or copy on spark-2 never finished"),
                             (2, "2 downloads or copies on spark-2 never finished")):
            with self.subTest(count=count):
                nodes = [node("n0", "spark-1"), node("n1", "spark-2", unfinished=[loose] * count),
                         node("n2", "spark-3")]
                found = mp.find_items(mp.Cluster([recipe()], files([ready_model()], nodes=nodes),
                                                 budget()["nodes"], names=NAMES))
                self.assertEqual(found["cannot"], [f"{words}, so there is nothing complete to record. Pulsar never "
                                                   "deletes files it has no record of."])

    def test_the_page_offers_it_and_says_where_files_with_no_record_are(self):
        found = cluster([recipe(home_node=None)], files([entry(MODEL, "org/model", [found_home("n1")], specs=[SPEC])]))
        self.assertIn(("find", "Record model files found on the nodes…"), mp.page_choices(found))
        self.assertIn("Files Pulsar has no record of are on spark-2; Record model files found on the nodes… "
                      "records them.", flat(mp.page(found, width=100)))

    def test_a_home_found_with_no_record_is_one_step_through_downloads_own_reuse(self):
        found = cluster([recipe(home_node=None)], files([entry(MODEL, "org/model", [found_home("n1")], specs=[SPEC])]))
        plan = mp.compose(found, "find")
        step, = plan["steps"]
        self.assertEqual((step["kind"], step["command"], step["preview"]),
                         ("register", ["./pulsar", "model", "acquire", SPEC, "--node", "spark-2", "--yes"],
                          ["./pulsar", "model", "acquire", SPEC, "--node", "spark-2", "--plan", "--json"]))
        # Download's own check hashes every file, so its preview runs when the step's turn comes.
        self.assertEqual((step["preview_now"], plan["projected"]), (False, []))
        text = flat(mp.render(plan, width=100))
        self.assertIn("1. Record the files of org/model found on spark-2 (212 GiB) as its home, the copy Pulsar keeps. "
                      "Download checks them first; nothing is downloaded.", text)
        self.assertIn("Every file is hashed when no record names the folder, so each step can take a while.", text)
        self.assertEqual(mp.plan_choices(plan), [("cancel", "Cancel"), ("go", "Go ahead")])

    def test_the_register_step_holds_only_when_download_reuses_the_files_found(self):
        step = mp.compose(cluster([recipe(home_node=None)], files([entry(MODEL, "org/model", [found_home("n1")],
                                                                         specs=[SPEC])])), "find")["steps"][0]
        reuse = {"ok": True, "result": {"kind": "pulsar-acquisition-plan", "action": "reuse", "home": {"node_id": "n1"}}}
        self.assertEqual(mp.assess(step, reuse, 0, NAMES), ("same", ""))
        fetch = {"ok": True, "result": {"kind": "pulsar-acquisition-plan", "selected_node": "n1"}}
        verdict, reason = mp.assess(step, fetch, 0, NAMES)
        self.assertEqual(verdict, "differs")
        self.assertIn("this plan never downloads", reason)
        elsewhere = {"ok": True, "result": {"kind": "pulsar-acquisition-plan", "action": "reuse",
                                            "home": {"node_id": "n2"}}}
        self.assertEqual(mp.assess(step, elsewhere, 0, NAMES)[0], "differs")
        self.assertEqual(mp.assess(step, {"ok": False, "error": {"message": "multiple managed homes match"}}, 1,
                                   NAMES), ("blocked", "multiple managed homes match"))

    def test_a_copy_no_record_names_is_taken_over_by_prepare_with_its_flag_after_a_preview(self):
        document = files([entry(MODEL, "org/model", [home("n0", binding()), location("n1", "prepared", binding()),
                                                     lost_copy("n2")], specs=[SPEC])])
        found = cluster([recipe()], document)
        plan = mp.compose(found, "find")
        step, = plan["steps"]
        self.assertEqual(step["command"], ["./pulsar", "model", "prepare", SPEC, "--adopt-unrecorded", "--yes"])
        self.assertEqual(step["preview"], ["./pulsar", "model", "prepare", SPEC, "--adopt-unrecorded", "--plan",
                                           "--json"])
        self.assertTrue(step["preview_now"])
        self.assertEqual(flat(step["sentence"]), "Take over the copy of org/model found on spark-3 after checking "
                                                 "every file (212 GiB). Pulsar records it only if every file matches; "
                                                 "nothing is copied or deleted.")
        # Prepare's own plan must answer adopt there; it then holds, and anything it would copy is said.
        preview = prepare_preview(("n0", "home-view"), ("n1", "reuse"), ("n2", "adopt"))
        same = mp.compose(found, "find", previews={"0": (preview, 0)})
        self.assertEqual((same["blocked"], same["steps"][0]["facts"]["copy_to"]), ([], []))
        copying = mp.compose(found, "find", previews={"0": (prepare_preview(("n0", "home-view"), ("n1", "copy"),
                                                                            ("n2", "adopt")), 0)})
        self.assertIn("nothing is deleted. Prepare also copies the files to spark-2, which has no copy, since every "
                      "serving node needs them before any copy is recorded.", flat(copying["steps"][0]["sentence"]))
        self.assertEqual(copying["steps"][0]["facts"]["copy_to"], ["n1"])
        nothing = mp.compose(found, "find", previews={"0": (prepare_preview(("n0", "home-view"), ("n1", "reuse"),
                                                                            ("n2", "reuse")), 0)})
        self.assertEqual(nothing["steps"], [])

    def test_when_its_turn_comes_a_take_over_that_would_copy_more_than_it_said_stops(self):
        document = files([entry(MODEL, "org/model", [home("n0", binding()), location("n1", "prepared", binding()),
                                                     lost_copy("n2")], specs=[SPEC])])
        found = cluster([recipe()], document)
        plan = mp.compose(found, "find", previews={"0": (prepare_preview(("n0", "home-view"), ("n1", "reuse"),
                                                                         ("n2", "adopt")), 0)})
        step = plan["steps"][0]
        later = prepare_preview(("n0", "home-view"), ("n1", "copy"), ("n2", "adopt"))
        self.assertEqual(mp.assess(step, later, 0, NAMES),
                         ("differs", "it would also copy the files to spark-2, which the plan did not say"))
        blocked = {"ok": True, "result": {"kind": "pulsar-preparation-plan", "eligible": False,
                                          "blockers": ["n2: a folder"], "actions": [],
                                          "blocker_details": [{"code": "unrecorded-destination", "message": "n2: a folder",
                                                               "node_id": "n2", "path": f"/fixture/views/{SPEC}",
                                                               "fix": "It cannot be taken over: its set of files "
                                                                      "differs from the manifest. Remove it."}]}}
        verdict, reason = mp.assess(step, blocked, 0, NAMES)
        self.assertEqual(verdict, "blocked")
        self.assertIn("its set of files differs from the manifest", reason)

    def test_a_home_to_record_comes_first_and_the_take_over_after_it_is_an_estimate(self):
        document = files([entry(MODEL, "org/model", [found_home("n0"), lost_copy("n1"), lost_copy("n2")],
                                specs=[SPEC])])
        plan = mp.compose(cluster([recipe(home_node=None)], document), "find")
        self.assertEqual([step["kind"] for step in plan["steps"]], ["register", "adopt"])
        self.assertEqual(plan["projected"], [1])
        self.assertEqual(plan["steps"][1]["facts"]["nodes"], ["n1", "n2"])
        self.assertIn("2. Take over the copies of org/model found on spark-2 and spark-3 after checking every file "
                      "(212 GiB each).", flat(mp.render(plan, width=100)))
        # With no preview before the home is recorded, any copy Prepare then wants is more than the plan said.
        self.assertEqual(mp.assess(plan["steps"][1], prepare_preview(("n0", "home-view"), ("n1", "adopt"),
                                                                     ("n2", "copy")), 0, NAMES)[0], "differs")

    def test_what_cannot_be_recorded_is_listed_with_why_and_what_gets_the_model_back(self):
        rows = [recipe(home_node=None), recipe(OTHER, "org/other", OTHER_FILES, home_node=None)]
        document = files([
            entry(MODEL, "org/model", [found_home("n1"), found_home("n2")], specs=[SPEC]),
            entry(OTHER_FILES, "org/other", [lost_copy("n1", OTHER)], 60 * G, specs=[OTHER]),
            entry(LOOSE, "org/forgotten", [found_home("n2")], 50 * G),
            entry(DRAFT, "org/draft", [lost_copy("n2", THIRD)], 4 * G, specs=[OTHER])])
        plan = mp.compose(cluster(rows, document), "find")
        self.assertEqual(plan["steps"], [])
        text = flat(mp.render(plan, width=100))
        self.assertIn("Nothing was found that Pulsar can record.", text)
        self.assertIn("Found, but Pulsar cannot record it here:", text)
        self.assertIn("Files of org/model were found on spark-2 and spark-3. Download records one home and stops when "
                      "it finds two, so neither is recorded", text)
        self.assertIn("A copy of org/other on spark-2: the model has no home on the cluster, the copy Pulsar keeps, "
                      "to record it against. Get a model with a recipe ready to serve… restores or downloads it", text)
        self.assertIn("org/forgotten @ cccccccc on spark-3 (50.0 GiB): no recipe in this catalog uses these files",
                      text)
        self.assertIn("A copy of org/draft @ cccccccc on spark-3 that no record names was made for a recipe outside "
                      "this catalog", text)
        self.assertEqual(mp.plan_choices(plan), [("cancel", "Cancel")])

    def test_a_node_that_did_not_answer_is_unknown_and_never_read_as_having_no_files(self):
        nodes = [node("n0", "spark-1"), node("n1", "spark-2"), node("n2", "spark-3", unread=True)]
        found = cluster([recipe(home_node=None)], files([entry(MODEL, "org/model", [found_home("n2")], specs=[SPEC])],
                                                        nodes=nodes))
        plan = mp.compose(found, "find")
        self.assertEqual(plan["steps"], [])
        text = flat(mp.render(plan, width=100))
        self.assertIn("Not checked: spark-3 could not be checked, so the files there are unknown, not absent, and "
                      "nothing is offered there.", text)
        self.assertIn("Nothing was found that Pulsar can record on the nodes that answered.", text)

    def test_a_schema_3_recipes_copy_is_found_by_the_key_prepare_uses(self):
        from model_library.state import view_key
        member = {"model_id": "org/model", "model_commit": "c" * 40, "archive": None}
        row = recipe(snapshots={"target": {**member, "snapshot_manifest_id": MODEL, "home": {"node_id": "n0"}},
                                "draft": {**member, "model_id": "org/draft", "snapshot_manifest_id": DRAFT,
                                          "home": {"node_id": "n0"}}})
        name = view_key(SPEC, "n1", MODEL)
        copy = location("n1", "prepared", hub_path=f"/fixture/views/{name}", path=f"/fixture/views/{name}/s")
        document = files([entry(MODEL, "org/model", [home("n0", binding()), copy], specs=[SPEC]),
                          entry(DRAFT, "org/draft", [home("n0", binding())], 4 * G, specs=[SPEC])])
        plan = mp.compose(cluster([row], document), "find")
        self.assertEqual([(step["kind"], step["facts"]["nodes"]) for step in plan["steps"]], [("adopt", ["n1"])])

    def test_only_this_plan_passes_the_take_over_flag(self):
        # D7: Serve, Get ready and every other menu never pass it.
        allowed = {"models_page.py", "model-library.sh", "model-library-preparation.sh", "help_pages.py",
                   "models-menu.sh"}
        users = sorted(path.name for path in (ROOT / "scripts").iterdir()
                       if path.is_file() and "--adopt-unrecorded" in path.read_text(errors="replace"))
        self.assertEqual([name for name in users if name not in allowed], [])
        commands = [step["command"] for target in ("find",)
                    for step in mp.compose(cluster([recipe()], files([entry(MODEL, "org/model", [
                        home("n0", binding()), lost_copy("n1")], specs=[SPEC])])), target)["steps"]]
        self.assertTrue(all("--adopt-unrecorded" in command for command in commands), commands)


NEW = "n" * 64
NEW_FILE = "/checkout/.pulsar/manifests/new/model@cccccccccccc.json"


def new_model(*locations, archive=None):
    """new/model, downloaded before it has a recipe: a home on spark-2 unless LOCATIONS are given."""
    return entry(NEW, "new/model", list(locations) or [home("n1")], 100 * G, archive=archive)


class FilesWithNoRecipe(unittest.TestCase):
    """A home no recipe in the catalog uses: every choice for it selects it with its saved manifest file."""

    def found(self, *entries, saved=True, **extra):
        return cluster([recipe()], files([ready_model(), *(entries or [new_model()])]),
                       saved={NEW: NEW_FILE} if saved else None, **extra)

    def test_only_valid_manifest_files_saved_in_the_checkout_are_used(self):
        import tempfile
        from release_spec import build_snapshot_manifest  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(mp.saved_manifests(root), {})
            folder = root / ".pulsar" / "manifests" / "new"
            folder.mkdir(parents=True)
            document = build_snapshot_manifest(model_id="new/model", snapshot_revision="c" * 40, files=[
                {"path": "weights.bin", "size": 3, "sha256": "b" * 64}])
            (folder / "model@cccccccccccc.json").write_text(json.dumps(document))
            (folder / "broken.json").write_text("{")
            (folder / "other.json").write_text(json.dumps({**document, "total_bytes": document["total_bytes"] + 1}))
            (folder / "notes.txt").write_text(json.dumps(document))
            self.assertEqual(mp.saved_manifests(root),
                             {document["manifest_id"]: str(folder / "model@cccccccccccc.json")})

    def test_free_up_space_offers_its_removal_with_what_getting_it_back_takes(self):
        removable, blocked = mp.free_options(self.found())
        option = next(item for item in removable if item["target"] == f"files:{NEW}")
        self.assertEqual(flat(option["text"]),
                         "Remove new/model @ cccccccc from the cluster: frees 100 GiB. No recipe in this catalog uses "
                         "these files. It deletes its home on spark-2, the copy Pulsar keeps. It has no verified "
                         "archive, so getting it back means downloading it again (100 GiB). Its plan offers to back "
                         "it up first.")
        self.assertFalse([text for text, _ in blocked if "new/model" in text])
        archived_ = mp.free_options(self.found(new_model(archive=archived())))[0]
        self.assertIn("Its archive was verified 4 days ago, so it can be restored without the internet.",
                      flat(next(item for item in archived_ if item["target"] == f"files:{NEW}")["text"]))

    def test_without_its_saved_manifest_nothing_is_offered_and_the_page_says_what_is_missing(self):
        removable, blocked = mp.free_options(self.found(saved=False))
        self.assertFalse([item for item in removable if "new/model" in item["label"]])
        self.assertIn("new/model @ cccccccc on spark-2 (100 GiB): no recipe in this catalog uses these files, and "
                      "their manifest, the list of the files and their hashes, is not saved in this checkout, so this "
                      "page does not remove them. Download a model that has no recipe yet… saves it: choose it for "
                      "the same model, commit and node; it keeps the files there and downloads nothing.",
                      [flat(text) for text, _ in blocked])
        choices, lines = mp.backup_options(self.found(saved=False))
        self.assertNotIn(f"backup-files:{NEW}", [target for target, _ in choices])
        self.assertTrue(any(line.startswith("new/model @ cccccccc (no recipe in this catalog uses these files): no "
                                            "archive recorded. This page cannot back it up: their manifest")
                            and line.endswith("it keeps the files there and downloads nothing.")
                            for line in lines), lines)
        archived_ = self.found(new_model(archive=archived()), saved=False)
        self.assertEqual(mp.check_options(archived_)[0][-1], ("back", "Back"))
        self.assertNotIn(f"check-files:{NEW}", [target for target, _ in mp.check_options(archived_)[0]])
        self.assertTrue(any("This page cannot check it: their manifest" in line
                            for line in mp.check_options(archived_)[1]))
        with self.assertRaisesRegex(ValueError, "no longer saved in this checkout"):
            mp.compose(self.found(saved=False), f"files:{NEW}")

    def test_its_home_stays_while_something_uses_it_or_it_could_not_be_checked(self):
        outside = binding("f" * 64)
        cases = {
            "copies made for a recipe outside this catalog use these files":
                new_model(home("n1"), location("n2", "prepared", outside)),
            "tool on spark-2 mounts these files": new_model(home("n1", users=[served("n1", managed=False, name="tool")])),
            "the containers on spark-2 could not be checked": new_model(home("n1", container_references=None)),
            "its files on spark-2 were not found": new_model(home("n1", state="missing")),
            "its files on spark-2 could not be checked": new_model(home("n1", state="unknown")),
        }
        for reason, model in cases.items():
            with self.subTest(reason=reason):
                found = self.found(model)
                removable, blocked = mp.free_options(found)
                self.assertFalse([item for item in removable if item["target"].startswith("files:")])
                self.assertTrue(any(flat(text).endswith(f"They stay: {reason}.") for text, _ in blocked), blocked)
                self.assertIn(f"Blocked: Its home cannot go: {reason}.",
                              flat(mp.render(mp.compose(found, f"files:{NEW}"))))
        # A node that did not answer: nothing destructive is offered anywhere, said once.
        unread = cluster([recipe()], files([ready_model(), new_model()],
                                           nodes=[node("n0", "spark-1"), node("n1", "spark-2"),
                                                  node("n2", "spark-3", unread=True)]), saved={NEW: NEW_FILE})
        self.assertEqual(mp.free_options(unread)[0], [])
        self.assertTrue(mp.compose(unread, f"files:{NEW}")["blocked"])

    def test_removing_it_is_one_step_through_its_manifest_and_offers_to_back_it_up_first(self):
        plan = mp.compose(self.found(), f"files:{NEW}")
        self.assertEqual(plan["title"], "Remove new/model @ cccccccc from the cluster")
        self.assertEqual([(step["kind"], step["command"], step["preview"]) for step in plan["steps"]],
                         [("remove", ["./pulsar", "model", "remove", "--manifest", NEW_FILE, "--yes"],
                           ["./pulsar", "model", "remove", "--manifest", NEW_FILE, "--plan", "--json"])])
        self.assertEqual(mp.plan_choices(plan), [("cancel", "Cancel"), ("go", "Go ahead"),
                                                 ("archive-first", "Back it up first, then remove it")])
        text = flat(mp.render(plan))
        self.assertIn("1. Delete the home of new/model @ cccccccc on spark-2 (100 GiB), the copy Pulsar downloaded "
                      "or restored and keeps.", text)
        self.assertIn("It has no verified archive, so getting it back means downloading it again (100 GiB).", text)
        self.assertIn("No archive is deleted, and the saved manifest file stays.", text)
        first = mp.compose(self.found(), f"files:{NEW}", archive_first=True)
        self.assertEqual([step["command"][:5] for step in first["steps"]],
                         [["./pulsar", "model", "archive", "create", "--manifest"],
                          ["./pulsar", "model", "remove", "--manifest", NEW_FILE]])
        self.assertTrue(all(step["preview_now"] for step in first["steps"]))
        self.assertIn("Its archive is made first and stays", flat(mp.render(first)))
        self.assertEqual(mp.plan_choices(first), [("cancel", "Cancel"), ("go", "Go ahead")])
        # Archived already: no step makes an archive, and none is offered.
        kept = mp.compose(self.found(new_model(archive=archived())), f"files:{NEW}", archive_first=True)
        self.assertEqual([step["kind"] for step in kept["steps"]], ["remove"])
        self.assertIn("Its archive stays, verified 4 days ago", flat(mp.render(kept)))
        # Without an archive location the plan says where it is set and offers no backup.
        unset = mp.compose(self.found(archive_location="not-configured"), f"files:{NEW}")
        self.assertEqual(mp.plan_choices(unset), [("cancel", "Cancel"), ("go", "Go ahead")])
        self.assertIn("Backing it up first needs the archive location", flat(mp.render(unset)))

    def test_a_steps_own_preview_must_name_the_same_files_and_the_same_node(self):
        plan = mp.compose(self.found(), f"files:{NEW}", archive_first=True)
        archive, remove = plan["steps"]
        removal = {"kind": "pulsar-home-removal-plan", "snapshot_manifest_id": NEW, "home": {"node_id": "n1"}}
        self.assertEqual(mp.assess(remove, {"ok": True, "result": removal}), ("same", ""))
        # The file is read again when the step's turn comes: another manifest in it is not what was approved.
        self.assertEqual(mp.assess(remove, {"ok": True, "result": {**removal, "snapshot_manifest_id": "o" * 64}}),
                         ("differs", "its manifest file names other files now than when the plan was made"))
        self.assertEqual(mp.assess(remove, {"ok": True, "result": {**removal, "home": {"node_id": "n2"}}}, 0, NAMES)[0],
                         "differs")
        copy = {"kind": "pulsar-archive-plan", "snapshot_manifest_id": NEW, "home": {"node_id": "n1"}}
        self.assertEqual(mp.assess(archive, {"ok": True, "result": copy}), ("same", ""))
        self.assertEqual(mp.assess(archive, {"ok": True, "result": {**copy, "snapshot_manifest_id": "o" * 64}})[0],
                         "differs")
        self.assertEqual(mp.assess(remove, {"ok": False, "error": {"message": "no home is registered"}}, 2),
                         ("blocked", "no home is registered"))

    def test_back_up_and_check_offer_it_by_its_archive_state(self):
        choices, lines = mp.backup_options(self.found())
        self.assertIn((f"backup-files:{NEW}", "Back up new/model @ cccccccc"), choices)
        self.assertIn("new/model @ cccccccc (no recipe in this catalog uses these files): no archive recorded.", lines)
        plan = mp.compose(self.found(), f"backup-files:{NEW}")
        self.assertEqual([(step["kind"], step["command"]) for step in plan["steps"]],
                         [("archive", ["./pulsar", "model", "archive", "create", "--manifest", NEW_FILE, "--yes"])])
        self.assertIn("1. Copy the files of new/model @ cccccccc from its home on spark-2 into the archive (100 GiB).",
                      flat(mp.render(plan)))
        # Not archived: nothing to check.
        self.assertNotIn(f"check-files:{NEW}", [target for target, _ in mp.check_options(self.found())[0]])
        archived_ = self.found(new_model(archive=archived()))
        self.assertNotIn(f"backup-files:{NEW}", [target for target, _ in mp.backup_options(archived_)[0]])
        self.assertIn((f"check-files:{NEW}", "Check the archive of new/model @ cccccccc"), mp.check_options(archived_)[0])
        check = mp.compose(archived_, f"check-files:{NEW}")
        self.assertEqual([(step["kind"], step["command"], step["preview"]) for step in check["steps"]],
                         [("verify", ["./pulsar", "model", "archive", "verify", "--manifest", NEW_FILE], None)])
        self.assertEqual(check["done"], "the archive of new/model @ cccccccc matched its expected file hashes")

    def test_it_moves_off_a_node_past_its_reserve_to_any_node_with_room(self):
        found = self.found(budget_document=budget(short=("n1",)))
        move = next(item for item in mp.free_options(found)[0] if item["target"].startswith("move-files:"))
        self.assertEqual(move["target"], f"move-files:{NEW}:n0")
        self.assertTrue(flat(move["text"]).startswith("Move the files of new/model @ cccccccc from spark-2 to spark-1: "
                                                      "frees 100 GiB on spark-2."))
        plan = mp.compose(found, move["target"])
        command = ["./pulsar", "model", "move", "--manifest", NEW_FILE, "--node", "spark-1"]
        self.assertEqual([(step["kind"], step["command"], step["preview"]) for step in plan["steps"]],
                         [("move", [*command, "--yes"], [*command, "--plan", "--json"])])
        moved = {"kind": "pulsar-home-move-plan", "snapshot_manifest_id": NEW, "source_node": "n1",
                 "destination_node": "n0", "transfer_route": "direct-ssh-roce"}
        self.assertEqual(mp.assess(plan["steps"][0], {"ok": True, "result": moved}), ("same", ""))
        self.assertEqual(mp.assess(plan["steps"][0], {"ok": True, "result": {**moved, "snapshot_manifest_id": "o" * 64}})[0],
                         "differs")
        # No saved manifest: no move is offered. No node short: none either.
        short = self.found(saved=False, budget_document=budget(short=("n1",)))
        self.assertFalse([item for item in mp.free_options(short)[0] if item["target"].startswith("move")])
        self.assertFalse([item for item in mp.free_options(self.found())[0] if item["target"].startswith("move")])
        with self.assertRaisesRegex(ValueError, "cannot be moved there"):
            mp.compose(found, f"move-files:{NEW}:n1")

    def test_its_labels_use_plain_words_and_its_screens_fit_80_60_and_44_columns(self):
        long = entry(NEW, "organization-with-a-long-name/model-family-instruct-fp8", [home("n1")], 100 * G)
        found = cluster([recipe()], files([ready_model(), long]), budget(short=("n1",)), saved={NEW: NEW_FILE})
        labels = [label for _, label in mp.free_choices(found)]
        labels += [label for intent in ("backup", "check") for _, label in mp.intent_choices(found, intent)]
        self.assertTrue(any("model-family-instruct-fp8 @ cccccccc" in label for label in labels))
        for label in labels:
            self.assertIsNone(FORBIDDEN.search(label), label)
        targets = [target for target, _ in mp.free_choices(found) + mp.intent_choices(found, "backup")
                   if "files:" in target]
        self.assertEqual(sorted(target.split(":")[0] for target in targets), ["backup-files", "files", "move-files"])
        for width in (80, 60, 44):
            with self.subTest(width=width):
                texts = [mp.page(found, width=width), mp.free_screen(found, width=width),
                         mp.intent_screen(found, "backup", width=width)]
                texts += [mp.render(mp.compose(found, target), width=width) for target in targets]
                texts.append(mp.render(mp.compose(found, f"files:{NEW}", archive_first=True), width=width))
                for text in texts:
                    self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)


class MoveAModel(unittest.TestCase):
    """Move a model to another node…: Move home at any time, to the node the operator names."""

    def found(self, **extra):
        rows = [recipe(), recipe(OTHER, "org/other", OTHER_FILES, home_node="n0", nodes=1)]
        document = files([ready_model(), entry(OTHER_FILES, "org/other", [home("n0")], 60 * G, specs=[OTHER]),
                          new_model(), entry(LOOSE, "org/forgotten", [home("n2")], 50 * G)])
        return cluster(rows, document, saved={NEW: NEW_FILE}, **extra)

    def test_each_home_is_offered_or_says_why_it_stays(self):
        choices, lines = mp.move_page_options(self.found())
        self.assertEqual(choices, [(f"pick:{OTHER_FILES}", "Move org/other from spark-1"),
                                   (f"pick:{NEW}", "Move new/model @ cccccccc from spark-2"), ("back", "Back")])
        self.assertEqual([flat(line) for line in lines], [
            "org/model (212 GiB) stays on spark-1: its copies on spark-1, spark-2 and spark-3 depend on it, and a "
            "move never removes them. Free up space… removes them first.",
            "org/other: 60.0 GiB on spark-1.",
            "new/model @ cccccccc: 100 GiB on spark-2.",
            "org/forgotten @ cccccccc (50.0 GiB) stays on spark-3: no recipe in this catalog uses these files, and "
            "their manifest, the list of the files and their hashes, is not saved in this checkout. Download a "
            "model that has no recipe yet… saves it: choose it for the same model, commit and node; it keeps the "
            "files there and downloads nothing."])
        text = flat(mp.intent_screen(self.found(), "move", width=100))
        self.assertTrue(text.startswith("Move a model to another node Pulsar copies a model's home, the copy it "
                                        "keeps, to the node you choose, checks every file there, then deletes it on "
                                        "the old node."), text)
        for _, label in choices:
            self.assertIsNone(FORBIDDEN.search(label), label)

    def test_a_home_in_use_or_not_checked_stays(self):
        cases = {"tool mounts these files": home("n1", users=[served("n1", managed=False, name="tool")]),
                 "the containers there could not be checked": home("n1", container_references=None),
                 "its files there were not found": home("n1", state="missing"),
                 "its files there could not be checked": home("n1", state="unknown")}
        for reason, location_ in cases.items():
            with self.subTest(reason=reason):
                found = cluster([recipe()], files([new_model(location_)]), saved={NEW: NEW_FILE})
                choices, lines = mp.move_page_options(found)
                self.assertEqual(choices, [("back", "Back")])
                self.assertEqual(flat(lines[0]), f"new/model @ cccccccc (100 GiB) stays on spark-2: {reason}.")

    def test_a_node_that_did_not_answer_offers_no_move(self):
        document = files([new_model()], nodes=[node("n0", "spark-1"), node("n1", "spark-2"),
                                               node("n2", "spark-3", unread=True)])
        choices, lines = mp.move_page_options(cluster([recipe()], document, saved={NEW: NEW_FILE}))
        self.assertEqual(choices, [("back", "Back")])
        self.assertIn("spark-3 could not be checked, and a move needs every node to answer, so nothing is offered.",
                      flat(lines[0]))

    def test_the_nodes_offered_can_take_the_home_and_never_include_its_own(self):
        found = self.found()
        # Files no recipe uses: any other node with room. The label says the room after the reserve.
        self.assertEqual(mp.move_destinations(found, NEW),
                         [(f"move-files:{NEW}:n0", "spark-1 (1.80 TiB free after its reserve)"),
                          (f"move-files:{NEW}:n2", "spark-3 (1.80 TiB free after its reserve)")])
        self.assertEqual([target for target, _ in mp.move_destinations(found, OTHER_FILES)],
                         [f"move:{OTHER_FILES}:n1", f"move:{OTHER_FILES}:n2"])
        # A node whose homes folder is known to lack the room is not offered; one not observed is.
        small = [node("n0", "spark-1", free=250 * G), node("n1", "spark-2"), node("n2", "spark-3")]
        tight = cluster([recipe()], files([new_model()], nodes=small), saved={NEW: NEW_FILE})
        self.assertEqual([target for target, _ in mp.move_destinations(tight, NEW)], [f"move-files:{NEW}:n2"])
        unseen = mp.Cluster([recipe()], files([new_model()]), None, names=NAMES, saved={NEW: NEW_FILE}, now=NOW)
        self.assertEqual(mp.move_destinations(unseen, NEW)[0], (f"move-files:{NEW}:n0", "spark-1 (room not observed)"))
        # No node can take it: the page says so instead of offering a choice with no answer.
        full = [node("n0", "spark-1", free=250 * G), node("n1", "spark-2"), node("n2", "spark-3", free=250 * G)]
        none = cluster([recipe()], files([new_model()], nodes=full), saved={NEW: NEW_FILE})
        self.assertEqual(mp.move_page_options(none)[0], [("back", "Back")])
        self.assertIn("no other node can take it", mp.move_page_options(none)[1][0])
        self.assertEqual(mp.move_destinations(found, "x" * 64), [])

    def test_a_target_is_the_plan_free_up_space_makes_for_the_same_move(self):
        found = self.found()
        plan = mp.compose(found, f"move:{OTHER_FILES}:n2")
        self.assertEqual(plan["steps"][0]["command"], ["./pulsar", "model", "move", OTHER, "--node", "spark-3", "--yes"])
        plan = mp.compose(found, f"move-files:{NEW}:n0")
        self.assertEqual(plan["steps"][0]["command"],
                         ["./pulsar", "model", "move", "--manifest", NEW_FILE, "--node", "spark-1", "--yes"])
        self.assertIn("Nothing is lost: the files on spark-2 are deleted only after the copy on spark-1 verifies.",
                      flat(mp.render(plan)))

    def test_a_second_set_of_files_a_recipe_loads_is_named_by_its_own_model(self):
        rows = [recipe(OTHER, "org/other", OTHER_FILES, home_node="n0", nodes=1)]
        document = files([entry(OTHER_FILES, "org/other", [home("n0")], 60 * G, specs=[OTHER]),
                          entry(DRAFT, "org/draft", [home("n1")], 4 * G, specs=[OTHER])])
        choices, lines = mp.move_page_options(cluster(rows, document))
        self.assertEqual([label for _, label in choices],
                         ["Move org/other from spark-1", "Move org/draft, which org/other also loads from spark-2",
                          "Back"])
        self.assertEqual(len(set(lines)), 2)

    def test_its_screen_fits_80_60_and_44_columns(self):
        for width in (80, 60, 44):
            text = mp.intent_screen(self.found(), "move", width=width)
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)


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

    def test_the_look_for_files_plan_fits_80_60_and_44_columns(self):
        long = "organization-with-a-long-name/model-family-instruct-fp8"
        rows = [recipe(home_node=None), recipe(OTHER, long, OTHER_FILES, home_node=None)]
        document = files([entry(MODEL, "org/model", [found_home("n0"), lost_copy("n1"), lost_copy("n2")],
                                specs=[SPEC]),
                          entry(OTHER_FILES, long, [lost_copy("n1", OTHER)], 60 * G, specs=[OTHER]),
                          entry(LOOSE, "org/forgotten", [found_home("n2")], 50 * G)])
        plan = mp.compose(cluster(rows, document), "find")
        self.assertEqual([step["kind"] for step in plan["steps"]], ["register", "adopt"])
        for width in (80, 60, 44):
            with self.subTest(width=width):
                for text in (mp.render(plan, width=width), mp.page(cluster(rows, document), width=width)):
                    self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)


if __name__ == "__main__":
    unittest.main()
