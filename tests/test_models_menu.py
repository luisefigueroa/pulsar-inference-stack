"""Models and disk space…: the page, its intents and their plans, driven with doubles for ui.sh,
./pulsar and the node tools (tests/test_serve_menu.build_root). No command here reaches Docker,
SSH, a GPU or the Hugging Face Hub."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.state import now  # noqa: E402
from tests.test_models_page import (DRAFT, G, MODEL, OTHER, OTHER_FILES, SPEC, archived, binding, budget,  # noqa: E402
                                    entry, files, found_home, home, location, lost_copy, node, prepare_preview,
                                    purge_preview, ready_model, recipe)
from tests.test_serve_menu import Harness, build_root, root_environment  # noqa: E402
from tests.test_serve_plan import envelope  # noqa: E402

FIND = "Record model files found on the nodes…"
DOWNLOAD = "Download a model that has no recipe yet…"
READY = "Get a model with a recipe ready to serve…"
MOVE = "Move a model to another node…"
KEEP = "Keep a model on the cluster…"
BACKUP = "Back up a model to the archive…"
CHECK = "Check the archive copies…"


def page_menu(*, archives=False, second_lines=True):
    """The page's menu as the double logs it: each choice, then its second line where it has one."""
    protect = [KEEP, "    Free up space never removes it"]
    if archives:
        protect += [BACKUP, CHECK, "    compares hashes; changes nothing"]
    lines = ["Models and disk space", DOWNLOAD, "    files and hashes only, to one node",
             READY, "    files, copies and image; nothing starts",
             MOVE, "    copies, checks, then frees the old node", *protect,
             "Free up space…", "    remove a model's files or move them",
             FIND, "    files Pulsar has no record of", "Look again (checks nodes now)", "Back"]
    return [line for line in lines if second_lines or not line.startswith(" ")]


PAGE = page_menu()
WITH_ARCHIVES = page_menu(archives=True)
SCAN = ["model files --json", "model budget --json"]


def retention_plan(action, pinned):
    return envelope({"kind": "pulsar-retention-plan", "action": action,
                     "views": [{"node_id": item, "pinned": pinned} for item in ("n0", "n1", "n2")]})


def purge_plan(*nodes, eligible=True):
    return envelope({"plan": {"kind": "pulsar-purge-plan", "eligible": eligible, "blockers": [], "views": [],
                              "actions": [{"node_id": item, "action": "remove-copy", "rank": index}
                                          for index, item in enumerate(nodes)]},
                     "incomplete_preparations": []})


class Menu(Harness):
    MENUS = ("models-menu.sh", "serve-menu.sh", "cluster-review.sh")
    MENU = "models-menu.sh"

    def setUp(self):
        super().setUp()
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.entries(recipe())
        self.fixture("files", envelope(dict(files([ready_model()]), observed_at=now())))
        self.fixture("budget", envelope(budget()))
        self.fixture("purge-plan", purge_plan("n1", "n2"))
        self.fixture("pin-plan", retention_plan("pin", False))
        self.fixture("unpin-plan", retention_plan("unpin", True))

    def text(self, result):
        return " ".join(result.stdout.split())

    def with_archive(self):
        return {"PULSAR_COLD_ROOT": str(self.archive)}

    def again(self):
        """Before another run of the menu in one test: it starts at its first answer, with its own logs."""
        for name in ("answers.n", "choose.log", "pulsar.log"):
            (self.root / name).unlink(missing_ok=True)
        for count in self.fixtures.glob("*.count"):
            count.unlink()


class ThePage(Menu):
    def test_entering_reads_the_files_and_the_budget_once_and_shows_room_and_models(self):
        result = self.run_menu(["Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus(), [PAGE])
        self.assertEqual(self.commands(), SCAN)
        text = self.text(result)
        self.assertIn("Room spark-1 2.00 TiB free · room for 1.00 TiB", text)
        self.assertIn("Models org/model · 212 GiB · home on spark-1 (also its copy there) · copies on spark-2 and "
                      "spark-3 ready to start (checked 2 hours ago) · not kept · no archive recorded", text)
        self.assertIn("Backups need the archive location: Cluster… → Settings…", text)
        self.assert_plain_choices()

    def test_with_an_archive_location_back_up_and_check_are_offered(self):
        result = self.run_menu(["Back"], **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus(), [WITH_ARCHIVES])

    def test_the_choices_are_grouped_and_a_blank_line_ends_each_group_but_the_last(self):
        result = self.run_menu(["Back"], **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        logged = self.choose_log.read_text().split("-- end of menu --")[0].splitlines()[1:]
        groups = [group.splitlines() for group in "\n".join(logged).split("\n\n")]
        self.assertEqual([[line for line in group if not line.startswith(" ")] for group in groups],
                         [[DOWNLOAD, READY, MOVE], [KEEP, BACKUP, CHECK], ["Free up space…"],
                          [FIND, "Look again (checks nodes now)", "Back"]])

    def test_a_short_terminal_shows_the_labels_alone_so_the_page_stays_above_its_choices(self):
        page_log = self.root / "paged.log"
        result = self.run_menu(["Back"])
        page = len(result.stdout.strip("\n").splitlines())
        cards, labels = 17, 8  # the menu's rows with and without second lines and group gaps
        # Rows for the page, a blank line, the choices and Gum's five rows.
        for rows, second_lines, paged in ((page + 1 + cards + 5, True, False),
                                          (page + 1 + cards + 4, False, False),
                                          (page + 1 + labels + 5, False, False),
                                          (page + 1 + labels + 4, True, True)):
            with self.subTest(rows=rows):
                self.again()
                page_log.unlink(missing_ok=True)
                result = self.run_menu(["Free up space…", "Back", "Back"], ROWS=str(rows), PAGE_LOG=str(page_log))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.menus()[0], page_menu(second_lines=second_lines))
                # Too short for the labels alone too: the page opens in the pager, then the full choices.
                self.assertEqual(page_log.exists() and "paged" in page_log.read_text().splitlines()[0:1], paged)
                # A choice is reached by its label either way.
                self.assertEqual(self.menus()[1][0], "Free up space")

    def test_screens_reuse_the_scan_until_a_plan_runs_or_look_again(self):
        result = self.run_menu(["Free up space…", "Back", "Keep a model on the cluster…", "Back", "Free up space…",
                                "Remove org/model from spark-2 and spark-3: frees 424 GiB", "Cancel", "Back",
                                "Look again (checks nodes now)", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        # One scan for every screen and the Cancel; the plan's own preview runs once; Look again scans again.
        self.assertEqual(self.commands(), SCAN + [f"model purge {SPEC} --plan --json"] + SCAN)

    def test_a_copy_the_scan_did_not_find_is_not_read_as_ready_and_look_again_says_so_again(self):
        # The saved check found the files ready; since then the copy on spark-3 is gone.
        self.fixture("files", envelope(files([entry(MODEL, "org/model", [
            home("n0", binding()), location("n1", "prepared", binding()),
            location("n2", "prepared", binding(), state="missing")], specs=[SPEC])])))
        result = self.run_menu(["Look again (checks nodes now)", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), SCAN + SCAN)
        text = self.text(result)
        # The scan's age is counted from when the fixtures were made, so it is not part of what is compared.
        self.assertEqual(text.count("Models and disk space · checked "), 2)
        self.assertEqual(text.count(" files not found on spark-3 · not kept · no archive recorded"), 2)
        self.assertNotIn("ready to start", text)

    def test_a_node_that_did_not_answer_is_named_and_nothing_destructive_is_offered(self):
        nodes = [node("n0", "spark-1"), node("n1", "spark-2"), node("n2", "spark-3", unread=True)]
        self.fixture("files", envelope(files([ready_model()], nodes=nodes)))
        result = self.run_menu(["Free up space…", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Free up space", "Back"])
        text = self.text(result)
        self.assertIn("spark-3 not checked, so its files and room are unknown", text)
        self.assertIn("spark-3 could not be checked, so the files and room there are unknown, not absent.", text)

    def test_a_scan_that_could_not_run_is_not_read_as_no_models(self):
        self.fixture("files", rc=3)
        (self.fixtures / "files.json").unlink()
        result = self.run_menu(["Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("✗ Models and disk space could not be checked: the model files scan exited 3",
                      self.text(result))
        self.assertEqual(self.menus(), [["Models and disk space", "Look again", "Back"]])


class SharedStorageMenu(Menu):
    MENUS = (*Menu.MENUS, "storage_overview.py")

    def test_unwritable_refresh_lock_falls_back_to_direct_node_checks(self):
        env = {"PULSAR_MENU_SESSION": str(self.root)}
        self.assertEqual(self.run_menu(["Back"], **env).returncode, 0)
        directory = Path(root_environment(self.root)["PULSAR_MODEL_LIBRARY_DIR"]) / ".storage-overview"
        lock = directory / "refresh.lock"
        if os.geteuid() == 0:
            # Root bypasses file permissions. A directory also prevents opening this lock for writing.
            lock.unlink()
            lock.mkdir()
        else:
            lock.chmod(0o400)
        (directory / "storage").touch()
        self.again()
        result = self.run_menu(["Back"], **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), SCAN)
        self.assertEqual(self.menus(), [PAGE])

    def test_revisit_uses_saved_checks_and_look_again_runs_both(self):
        env = {"PULSAR_MENU_SESSION": str(self.root)}
        result = self.run_menu(["Back"], **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), SCAN)
        self.again()
        result = self.run_menu(["Look again (checks nodes now)", "Back"], **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), SCAN)
        self.assertEqual(self.menus(), [PAGE, PAGE])
        self.assertEqual(self.text(result).count("Checking model files on every node"), 1)
        self.assertIn("Files checked less than a minute ago · Budget checked less than a minute ago", self.text(result))

    def test_cached_page_still_rechecks_an_approved_purge_before_execution(self):
        env = {"PULSAR_MENU_SESSION": str(self.root)}
        self.assertEqual(self.run_menu(["Back"], **env).returncode, 0)
        self.again()
        self.fixture("purge-plan", purge_plan("n0", "n1", "n2"), nth=2)
        result = self.run_menu(["Free up space…", "Remove org/model from spark-2 and spark-3: frees 424 GiB",
                                "Go ahead", "Continue", "Back", "Back"], **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("it would also delete copies on spark-1", self.text(result))
        self.assertEqual(self.commands(), [f"model purge {SPEC} --plan --json"] * 2 + SCAN)
        self.assertEqual(self.ran(), [])


class FreeUpSpace(Menu):
    def test_removing_from_serving_nodes_is_a_purge_plan_that_starts_on_cancel_then_looks_again(self):
        result = self.run_menu(["Free up space…", "Remove org/model from spark-2 and spark-3: frees 424 GiB",
                                "Go ahead", "Continue", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1][0], "Free up space")
        self.assertEqual(self.menus()[2], ["Free up space", "Cancel", "Go ahead"])
        self.assertIn("Free up space\t0", self.default_log.read_text().splitlines())
        text = self.text(result)
        self.assertIn("1. Delete the copies of org/model on spark-2 and spark-3 (424 GiB).", text)
        self.assertIn("Its home on spark-1, the copy Pulsar keeps, stays, so serving it again copies from there, "
                      "with no download.", text)
        self.assertIn("✓ Done: org/model is removed from spark-2 and spark-3.", text)
        self.assertIn(f"Same as: ./pulsar model purge {SPEC[:12]} --yes", text)
        # The plan's preview, its own again at its turn, the purge, then a new scan for the page.
        self.assertEqual(self.commands(), SCAN + [f"model purge {SPEC} --plan --json"] * 2
                         + [f"model purge {SPEC} --yes"] + SCAN)
        self.assert_plain_choices()

    def test_removing_from_the_cluster_without_an_archive_can_back_it_up_first(self):
        self.fixture("archive-plan", envelope({"kind": "pulsar-archive-plan", "home": {"node_id": "n0"},
                                               "archive_root": str(self.archive)}))
        self.fixture("remove-plan", envelope({"kind": "pulsar-home-removal-plan", "eligible": True,
                                              "home": {"node_id": "n0"}, "blockers": []}))
        result = self.run_menu(["Free up space…", "Remove org/model from the cluster: frees 636 GiB",
                                "Back it up first, then remove it", "Go ahead", "Continue", "Back", "Back"],
                               **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[2], ["Free up space", "Cancel", "Go ahead", "Back it up first, then remove it"])
        self.assertEqual(self.menus()[3], ["Free up space", "Cancel", "Go ahead"])
        text = self.text(result)
        self.assertIn("It has no verified archive, so getting it back means downloading it again (212 GiB).", text)
        self.assertIn("1. Copy the model files from its home on spark-1 into the archive (212 GiB). An archive "
                      "already there is verified, never replaced.", text)
        self.assertEqual(self.ran(), [f"model archive create {SPEC} --yes", f"model purge {SPEC} --yes",
                                      f"model remove {SPEC} --yes"])
        # Remove home's own preview runs only at its turn, after Purge.
        self.assertEqual(self.commands().count(f"model remove {SPEC} --plan --json"), 1)
        self.assertIn(f"Same as: ./pulsar model archive create {SPEC[:12]} --yes ./pulsar model purge {SPEC[:12]} "
                      f"--yes ./pulsar model remove {SPEC[:12]} --yes", text)
        self.assert_plain_choices()

    def test_a_preview_that_reads_stdin_does_not_take_the_previews_after_it(self):
        # ssh to a remote node reads its standard input; the plan's later previews still run.
        self.fixture("archive-plan", envelope({"kind": "pulsar-archive-plan", "home": {"node_id": "n0"},
                                               "archive_root": str(self.archive)}))
        self.fixture("remove-plan", envelope({"kind": "pulsar-home-removal-plan", "eligible": True,
                                              "home": {"node_id": "n0"}, "blockers": []}))
        answers = ["Free up space…", "Remove org/model from the cluster: frees 636 GiB",
                   "Back it up first, then remove it", "Cancel", "Back", "Back"]
        expected = self.run_menu(answers, **self.with_archive())
        self.assertEqual(expected.returncode, 0, expected.stderr)
        previews = [command for command in self.commands() if command.endswith("--plan --json")]
        self.assertGreater(len(previews), 1, self.commands())
        self.log.unlink()
        self.answers.with_suffix(".n").unlink(missing_ok=True)
        for path in self.fixtures.glob("*.count"):
            path.unlink()
        # Backing it up first previews the archive, then the purge, before the plan shows.
        self.assertLess(previews.index(f"model archive create {SPEC} --plan --json"),
                        previews.index(f"model purge {SPEC} --plan --json", 1))
        (self.fixtures / "archive-plan.reads-stdin").touch()
        result = self.run_menu(answers, **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([command for command in self.commands() if command.endswith("--plan --json")], previews)

    def test_a_step_whose_own_preview_deletes_more_stops_before_it_changes_anything(self):
        self.fixture("purge-plan", purge_plan("n0", "n1", "n2"), nth=2)
        result = self.run_menu(["Free up space…", "Remove org/model from spark-2 and spark-3: frees 424 GiB",
                                "Go ahead", "Continue", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [])
        self.assertIn("✗ Step 1 did not run: it would also delete copies on spark-1. Pulsar stopped before it, so it "
                      "can ask again.", self.text(result))

    def test_removing_a_model_whose_copy_two_recipes_share_runs_through_to_its_home(self):
        self.entries(recipe(), recipe(OTHER, "org/model", MODEL, nodes=2, engine_args=["--max-model-len", "8192"]))
        self.fixture("files", envelope(files([entry(MODEL, "org/model", [
            home("n0", binding(), binding(OTHER)), location("n1", "prepared", binding(), binding(OTHER))],
            specs=[SPEC, OTHER])])))
        # While both recipes use the copy, each one's Purge only releases it. The fourth preview is the
        # second recipe's own at its turn, after the first Purge ran: the copy is then its alone to delete.
        self.fixture("purge-plan", purge_preview(("n0", "release-binding"), ("n1", "release-binding")))
        self.fixture("purge-plan", purge_preview(("n0", "release-binding"), ("n1", "remove-copy")), nth=4)
        self.fixture("remove-plan", envelope({"kind": "pulsar-home-removal-plan", "eligible": True,
                                              "home": {"node_id": "n0"}, "blockers": []}))
        result = self.run_menu(["Free up space…", "Remove org/model from the cluster: frees at least 212 GiB",
                                "Go ahead", "Continue", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.text(result)
        self.assertIn("1. Release the copies of org/model (3 nodes); their files stay for the steps after it, and "
                      "this step deletes none.", text)
        self.assertIn("2. Delete the copies of org/model (2 nodes) on spark-2 (212 GiB).", text)
        self.assertEqual(self.ran(), [f"model purge {SPEC} --yes", f"model purge {OTHER} --yes",
                                      f"model remove {SPEC} --yes"])
        self.assertIn("✓ Done: org/model is removed from the cluster.", text)
        self.assert_plain_choices()

    def test_a_kept_model_offers_only_to_stop_keeping_it_which_is_its_own_plan(self):
        self.fixture("files", envelope(files([ready_model(pinned=True)])))
        result = self.run_menu(["Free up space…", "Stop keeping org/model", "Go ahead", "Continue", "Back",
                                "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Free up space", "Stop keeping org/model", "Back"])
        self.assertIn("org/model is kept on spark-1, spark-2 and spark-3, so Free up space… does not offer it.",
                      self.text(result))
        # Unpinning is the operator's own choice and plan; nothing is purged with it.
        self.assertEqual(self.ran(), [f"model unpin {SPEC} --yes"])

    def test_moving_a_home_off_a_node_past_its_reserve_is_a_named_step(self):
        self.entries(recipe(OTHER, "org/other", OTHER_FILES, home_node="n0", nodes=1))
        self.fixture("files", envelope(files([entry(OTHER_FILES, "org/other", [home("n0")], 60 * G, specs=[OTHER])])))
        self.fixture("budget", envelope(budget(short=("n0",))))
        self.fixture("move-plan", envelope({"kind": "pulsar-home-move-plan", "source_node": "n0",
                                            "destination_node": "n1", "transfer_route": "direct-ssh-roce"}))
        result = self.run_menu(["Free up space…", "1", "Go ahead", "Continue", "Back", "Back"], COLUMNS="60")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Removing it frees as much, so it comes first; at 60 columns the move's label is a card of lines.
        menu = self.menus()[1]
        self.assertEqual(menu[1], "Remove org/other from the cluster: frees 60.0 GiB")
        card = menu[2:menu.index("Back")]
        self.assertGreater(len(card), 1)
        self.assertTrue(all(len(line) <= 60 for line in card), card)
        self.assertEqual(" ".join(" ".join(card).split()),
                         "Move the files of org/other from spark-1 to spark-2: frees 60.0 GiB on spark-1")
        self.assertEqual(self.ran(), [f"model move {OTHER} --node spark-2 --yes"])
        self.assertIn("Nothing is lost: the files on spark-1 are deleted only after the copy on spark-2 verifies.",
                      self.text(result))


class FilesWithNoRecipe(Menu):
    """A home no recipe uses, as Download a model that has no recipe yet… leaves it: the page reaches
    it with the manifest file that download saved."""

    def setUp(self):
        super().setUp()
        from release_spec import build_snapshot_manifest
        manifest = build_snapshot_manifest(model_id="new/model", snapshot_revision="c" * 40, files=[
            {"path": "weights.bin", "size": 3, "sha256": "b" * 64}])
        self.files_id = manifest["manifest_id"]
        self.manifest = self.root / ".pulsar" / "manifests" / "new" / "model@cccccccccccc.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps(manifest))
        self.scan()
        for key, kind in (("remove-plan", "pulsar-home-removal-plan"), ("archive-plan", "pulsar-archive-plan")):
            self.fixture(key, envelope({"kind": kind, "snapshot_manifest_id": self.files_id,
                                        "home": {"node_id": "n1"}, "eligible": True, "blockers": []}))

    def scan(self, archive=None):
        self.fixture("files", envelope(files([ready_model(), entry(self.files_id, "new/model", [home("n1")], 100 * G,
                                                                   archive=archive)])))

    REMOVE = "Remove new/model @ cccccccc from the cluster: frees 100 GiB"

    def test_free_up_space_removes_it_through_its_manifest_after_backing_it_up_when_asked(self):
        result = self.run_menu(["Free up space…", self.REMOVE, "Back it up first, then remove it", "Go ahead",
                                "Continue", "Back", "Back"], **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.text(result)
        self.assertIn("new/model @ cccccccc · 100 GiB on spark-2 · no recipe in this catalog uses these files · no "
                      "archive recorded", text)
        # The plan starts on Cancel; backing it up first is a choice of its own, never a default.
        self.assertEqual(self.menus()[2], ["Free up space", "Cancel", "Go ahead", "Back it up first, then remove it"])
        self.assertEqual(self.menus()[3], ["Free up space", "Cancel", "Go ahead"])
        self.assertIn("Free up space\t0", self.default_log.read_text())
        self.assertIn("Remove new/model @ cccccccc from the cluster Pulsar will:", text)
        self.assertIn("1. Copy the files of new/model @ cccccccc from its home on spark-2 into the archive (100 GiB).",
                      text)
        self.assertIn("2. Delete the home of new/model @ cccccccc on spark-2 (100 GiB)", text)
        self.assertEqual(self.ran(), [f"model archive create --manifest {self.manifest} --yes",
                                      f"model remove --manifest {self.manifest} --yes"])
        # Each step's own preview ran with the same file: for the first plan, for the plan that
        # backs it up first, and again when each step's turn came.
        archive, remove = (f"model archive create --manifest {self.manifest} --plan --json",
                           f"model remove --manifest {self.manifest} --plan --json")
        self.assertEqual([line for line in self.commands() if "--plan --json" in line],
                         [remove, archive, remove, archive, remove])
        self.assertIn("✓ Done: new/model @ cccccccc is removed from the cluster.", text)
        # A long command breaks between its arguments, never inside the path.
        self.assertIn("Same as: ./pulsar model archive create", text)
        self.assertEqual(text.count(f"--manifest {self.manifest}"), 2)
        self.assert_plain_choices()
        self.assert_no_unapproved_authority()

    def test_a_manifest_file_that_names_other_files_at_the_preview_stops_the_plan(self):
        self.fixture("remove-plan", envelope({"kind": "pulsar-home-removal-plan", "snapshot_manifest_id": "o" * 64,
                                              "home": {"node_id": "n1"}, "eligible": True, "blockers": []}))
        result = self.run_menu(["Free up space…", self.REMOVE, "Cancel", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[2], ["Free up space", "Cancel"])
        self.assertIn("Blocked: Step 1 cannot run as planned: its manifest file names other files now than when the "
                      "plan was made.", self.text(result))
        self.assertEqual(self.ran(), [])

    def test_a_manifest_file_that_changes_before_its_step_runs_stops_before_anything_is_deleted(self):
        self.fixture("remove-plan", envelope({"kind": "pulsar-home-removal-plan", "snapshot_manifest_id": "o" * 64,
                                              "home": {"node_id": "n1"}, "eligible": True, "blockers": []}), nth=2)
        result = self.run_menu(["Free up space…", self.REMOVE, "Go ahead", "Continue", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("✗ Step 1 did not run: its manifest file names other files now than when the plan was made.",
                      self.text(result))
        self.assertEqual(self.ran(), [])

    def test_without_the_saved_file_nothing_is_offered_for_it(self):
        self.manifest.unlink()
        result = self.run_menu(["Free up space…", "Back", "Back"], **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.REMOVE, self.menus()[1])
        self.assertIn("their manifest, the list of the files and their hashes, is not saved in this checkout, so this "
                      "page does not remove them.", self.text(result))
        self.assertEqual(self.ran(), [])

    def test_back_up_and_check_reach_it_by_the_same_file(self):
        result = self.run_menu([BACKUP, "Back up new/model @ cccccccc", "Go ahead", "Continue", "Back", "Back"],
                               **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Back up org/model", self.menus()[1])
        self.assertEqual(self.ran(), [f"model archive create --manifest {self.manifest} --yes"])
        self.assertIn("✓ Done: new/model @ cccccccc is backed up.", self.text(result))
        self.scan(archive={"verified": True, "verified_at": "2020-01-01T00:00:00Z", "root": "/fixture/archive"})
        self.again()
        result = self.run_menu([CHECK, "Check the archive of new/model @ cccccccc", "Go ahead", "Continue", "Back",
                                "Back"], **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [f"model archive verify --manifest {self.manifest}"])
        self.assertIn("✓ Done: the archive of new/model @ cccccccc matched its expected file hashes.",
                      self.text(result))
        self.assert_plain_choices()

    def test_move_a_model_asks_the_node_and_runs_the_move_the_operator_named(self):
        self.fixture("move-plan", envelope({"kind": "pulsar-home-move-plan", "snapshot_manifest_id": self.files_id,
                                            "source_node": "n1", "destination_node": "n2",
                                            "transfer_route": "controller-stream-relay"}))
        result = self.run_menu([MOVE, "Move new/model @ cccccccc from spark-2",
                                "spark-3 (1.80 TiB free after its reserve)", "Go ahead", "Continue", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.text(result)
        # org/model has copies that depend on its home, so it is named with why it stays, not offered.
        self.assertEqual(self.menus()[1], ["Move a model to another node", "Move new/model @ cccccccc from spark-2",
                                           "Back"])
        self.assertIn("org/model (212 GiB) stays on spark-1: its copies on spark-1, spark-2 and spark-3 depend on it",
                      text)
        self.assertEqual(self.menus()[2], ["Which node should keep the files?",
                                           "spark-1 (1.80 TiB free after its reserve)",
                                           "spark-3 (1.80 TiB free after its reserve)", "Back"])
        self.assertEqual(self.menus()[3], ["Move a model to another node", "Cancel", "Go ahead"])
        self.assertIn("Move a model to another node\t0", self.default_log.read_text())
        self.assertEqual(self.ran(), [f"model move --manifest {self.manifest} --node spark-3 --yes"])
        self.assertIn("✓ Done: the files of new/model @ cccccccc are on spark-3.", text)
        self.assert_plain_choices()
        self.assert_no_unapproved_authority()

    def test_move_a_model_runs_nothing_on_back_or_cancel(self):
        for answers in ([MOVE, "Back", "Back"],
                        [MOVE, "Move new/model @ cccccccc from spark-2", "Back", "Back", "Back"],
                        [MOVE, "Move new/model @ cccccccc from spark-2", "esc", "Back", "Back"],
                        [MOVE, "Move new/model @ cccccccc from spark-2", "spark-1 (1.80 TiB free after its reserve)",
                         "Cancel", "Back", "Back"]):
            with self.subTest(answers=answers):
                self.again()
                result = self.run_menu(answers)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.ran(), [])
                # No move is previewed before the operator names the node.
                self.assertEqual([line for line in self.commands() if line.startswith("model move")],
                                 [] if answers[-3] != "Cancel" else
                                 [f"model move --manifest {self.manifest} --node spark-1 --plan --json"])

    def test_it_moves_off_a_node_past_its_reserve_by_the_same_file(self):
        self.fixture("budget", envelope(budget(short=("n1",))))
        self.fixture("move-plan", envelope({"kind": "pulsar-home-move-plan", "snapshot_manifest_id": self.files_id,
                                            "source_node": "n1", "destination_node": "n0",
                                            "transfer_route": "direct-ssh-roce"}))
        move = "Move the files of new/model @ cccccccc from spark-2 to spark-1: frees 100 GiB on spark-2"
        # At 80 columns the label is a card of two lines, chosen by its place.
        result = self.run_menu(["Free up space…", "2", "Go ahead", "Continue", "Back", "Back"])
        self.assertEqual(" ".join(" ".join(self.menus()[1][3:5]).split()), move)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [f"model move --manifest {self.manifest} --node spark-1 --yes"])
        self.assertIn("✓ Done: the files of new/model @ cccccccc are on spark-1.", self.text(result))


class Intents(Menu):
    def test_keep_a_model_ready_is_pin_by_that_name(self):
        result = self.run_menu(["Keep a model on the cluster…", "Keep org/model on the cluster", "Go ahead", "Continue", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Keep a model on the cluster", "Keep org/model on the cluster", "Back"])
        self.assertEqual(self.menus()[2], ["Keep a model on the cluster", "Cancel", "Go ahead"])
        text = self.text(result)
        self.assertIn("Pulsar marks them as kept (pins them), so Free up space… never offers them and Purge refuses "
                      "them until you stop keeping it.", text)
        self.assertEqual(self.ran(), [f"model pin {SPEC} --yes"])
        self.assertIn(f"Same as: ./pulsar model pin {SPEC[:12]} --yes", text)
        self.assert_plain_choices()

    def test_back_up_a_model_covers_both_of_its_snapshots_in_one_plan(self):
        member = {"model_id": "org/model", "model_commit": "c" * 40, "archive": None}
        self.entries(recipe(snapshots={
            "target": {**member, "snapshot_manifest_id": MODEL, "home": {"node_id": "n0"}},
            "draft": {**member, "model_id": "org/draft", "snapshot_manifest_id": DRAFT, "home": {"node_id": "n1"}}}))
        self.fixture("files", envelope(files([ready_model(), entry(DRAFT, "org/draft", [home("n1", binding())], 4 * G,
                                                                    specs=[SPEC])])))
        self.fixture("archive-plan", envelope({"kind": "pulsar-archive-plan", "home": {"node_id": "n0"}}), nth=1)
        self.fixture("archive-plan", envelope({"kind": "pulsar-archive-plan", "home": {"node_id": "n1"}}), nth=2)
        self.fixture("archive-plan", envelope({"kind": "pulsar-archive-plan", "home": {"node_id": "n0"}}), nth=3)
        self.fixture("archive-plan", envelope({"kind": "pulsar-archive-plan", "home": {"node_id": "n1"}}), nth=4)
        result = self.run_menu(["Back up a model to the archive…", "Back up org/model", "Go ahead", "Continue", "Back", "Back"],
                               **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [f"model archive create {SPEC} --snapshot target --yes",
                                      f"model archive create {SPEC} --snapshot draft --yes"])
        text = self.text(result)
        self.assertIn("2. Copy the files of org/draft (a model it also loads) from its home on spark-2 into the "
                      "archive (4.00 GiB).", text)
        self.assertIn("✓ Done: org/model is backed up.", text)
        self.assert_plain_choices()

    def test_check_the_archives_runs_verify_with_nothing_to_preview(self):
        self.entries(recipe(archive=archived()))
        result = self.run_menu(["Check the archive copies…", "Check the archives of org/model", "Go ahead", "Continue",
                                "Back", "Back"], **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [f"model archive verify {SPEC}"])
        self.assertNotIn("model archive verify --plan", " ".join(self.commands()))
        self.assertIn("✓ Done: every archive of org/model matched its expected file hashes.", self.text(result))

    def test_get_a_model_ready_opens_the_serve_plan_without_start_and_reuses_the_scan_on_back(self):
        result = self.run_menu(["Get a model with a recipe ready to serve…", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1][0], "Get a model ready")
        self.assertEqual(self.menus()[2], PAGE)
        self.assertEqual(self.commands().count("model files --json"), 1)


class LookForFiles(Menu):
    def test_it_records_a_home_through_downloads_reuse_then_takes_over_the_copies_with_the_flag(self):
        self.entries(recipe(home_node=None))
        self.fixture("files", envelope(files([entry(MODEL, "org/model", [found_home("n0"), lost_copy("n1"),
                                                                         lost_copy("n2")], specs=[SPEC])])))
        self.fixture("acquire-plan", envelope({"kind": "pulsar-acquisition-plan", "action": "reuse",
                                               "home": {"node_id": "n0"}}))
        self.fixture("prepare-plan", prepare_preview(("n0", "home-view"), ("n1", "adopt"), ("n2", "adopt")))
        result = self.run_menu([FIND, "Go ahead", "Continue", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Record model files", "Cancel", "Go ahead"])
        self.assertIn("Record model files" + chr(9) + "0", self.default_log.read_text().splitlines())
        text = self.text(result)
        self.assertIn("1. Record the files of org/model found on spark-1 (212 GiB) as its home, the copy Pulsar keeps. "
                      "Download checks them first; nothing is downloaded.", text)
        self.assertIn("2. Take over the copies of org/model found on spark-2 and spark-3 after checking every file",
                      text)
        # Nothing ran before Go ahead: each step's own preview runs at its turn, then the step.
        self.assertEqual(self.commands(), SCAN + [
            f"model acquire {SPEC} --node spark-1 --plan --json", f"model acquire {SPEC} --node spark-1 --yes",
            f"model prepare {SPEC} --adopt-unrecorded --plan --json", f"model prepare {SPEC} --adopt-unrecorded --yes"]
            + SCAN)
        self.assertIn(f"Same as: ./pulsar model acquire {SPEC[:12]} --node spark-1 --yes ./pulsar model prepare "
                      f"{SPEC[:12]} --adopt-unrecorded --yes", text)
        self.assert_plain_choices()

    def test_files_download_would_fetch_instead_of_reusing_stop_the_plan_before_anything_runs(self):
        self.entries(recipe(home_node=None))
        self.fixture("files", envelope(files([entry(MODEL, "org/model", [found_home("n0")], specs=[SPEC])])))
        self.fixture("acquire-plan", envelope({"kind": "pulsar-acquisition-plan", "selected_node": "n0"}))
        result = self.run_menu([FIND, "Go ahead", "Continue", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [])
        self.assertIn("✗ Step 1 did not run: the files found on spark-1 do not match the recipe, so Download would "
                      "fetch them from Hugging Face; this plan never downloads.", self.text(result))


class WithoutGum(unittest.TestCase):
    def test_the_page_names_the_commands_behind_its_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_root(root, ("models-menu.sh",), gum=True)
            env = {key: value for key, value in os.environ.items() if not key.startswith(("PULSAR_", "GUM_"))}
            env.update(root_environment(root))
            result = subprocess.run(["bash", str(root / "scripts" / "models-menu.sh")], env=env, text=True,
                                    capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("needs an interactive terminal with Gum", result.stderr)
            for command in ("./pulsar model files", "./pulsar model budget", "./pulsar model purge SPEC --plan",
                            "./pulsar model remove SPEC --plan", "./pulsar model move SPEC --node NODE --plan",
                            "./pulsar model pin SPEC --plan", "./pulsar model archive create SPEC --plan",
                            "./pulsar model archive verify SPEC", "./pulsar model acquire SPEC --node NODE --plan",
                            "./pulsar model prepare SPEC --adopt-unrecorded --plan",
                            "./pulsar model remove --manifest FILE --plan"):
                self.assertIn(f"  {command}", result.stderr)
            self.assertFalse((root / "pulsar.log").exists())


COMMIT = "c" * 40
NEW = "org/new-model"


def acquire_plan(action="download", search="searched", host="n1"):
    document = {"kind": "pulsar-acquisition-plan", "action": action, "model_id": NEW, "snapshot_revision": COMMIT,
                "selected_node": host, "existing_homes": [],
                "source": {"model_id": NEW, "snapshot_revision": COMMIT,
                           "files": [{"path": "config.json", "size": G}, {"path": "weights.bin", "size": 211 * G}]},
                "archive_search": {"state": search, "archives": []} if search else None}
    if action == "reuse":
        document["home"] = {"node_id": host}
    return envelope(document)


class DownloadWithoutARecipe(Menu):
    """Download a model that has no recipe yet…: model acquire with a model and a commit, never a spec."""
    PLAN = f"model acquire --model-id {NEW} --model-commit main --node spark-2 --plan --json"
    # The same preview again directly before the run, with the exact commit.
    CHECK = f"model acquire --model-id {NEW} --model-commit {COMMIT} --node spark-2 --plan --json"

    def manifest(self):
        return self.root / ".pulsar" / "manifests" / "org" / f"new-model@{COMMIT[:12]}.json"

    def run_menu(self, answers, *arguments, **extra):
        # Each run of the menu starts at its first answer, with its own logs.
        for name in ("answers.n", "choose.log", "pulsar.log"):
            (self.root / name).unlink(missing_ok=True)
        for count in self.fixtures.glob("*.count"):
            count.unlink()
        return super().run_menu(answers, *arguments, **extra)

    def go(self, *last, **extra):
        return self.run_menu([DOWNLOAD, NEW, "main", "spark-2", *last], **extra)

    def test_it_asks_model_commit_and_node_then_runs_the_exact_commit_and_saves_the_manifest(self):
        self.fixture("acquire-plan", acquire_plan())
        result = self.go("Download 212 GiB to spark-2", "Continue", "Back",
                         INPUT_LOG=str(self.root / "input.log"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "input.log").read_text().splitlines(),
                         ["Which model on Hugging Face?\tORG/NAME",
                          f"Which commit, branch or tag of {NEW}?\tmain"])
        self.assertEqual(self.menus()[1], ["Which node should keep the files?", "spark-1", "spark-2", "spark-3", "Back"])
        self.assertEqual(self.menus()[2], ["Download a model", "Cancel", "Download 212 GiB to spark-2"])
        # The cursor starts on Cancel.
        self.assertIn("Download a model\t0", self.default_log.read_text())
        run = f"model acquire --model-id {NEW} --model-commit {COMMIT} --node spark-2 --manifest-out {self.manifest()} --yes"
        # The branch names the preview only; the run names the commit it printed. Then the page looks again.
        self.assertEqual(self.commands(), SCAN + [self.PLAN, self.CHECK, run] + SCAN)
        text = self.text(result)
        self.assertIn(f"1. Download {NEW} at commit cccccccccccc from Hugging Face to spark-2 (2 files, 212 GiB) and "
                      "check the SHA-256 of every file. The files become its home, the copy Pulsar keeps.", text)
        self.assertIn("2. Save the list of its files and their hashes (its manifest). A recipe written later for "
                      f"this model starts from this file: {self.manifest()} The archive", text)
        self.assertIn("The archive was searched and holds no copy of these files.", text)
        self.assertIn("Nothing is prepared for serving and nothing starts.", text)
        self.assertIn(f"✓ Done: {NEW} at commit cccccccccccc is on spark-2, downloaded and checked.", text)
        self.assertIn("No recipe uses these files yet, so this page cannot back them up or remove them.", text)
        # A long command breaks only between its arguments, each line but the last ending " \\".
        commands = text.replace(" \\ ", " ")
        self.assertIn(f"./pulsar model archive create --manifest {self.manifest()} --plan", commands)
        self.assertIn(f"./pulsar model remove --manifest {self.manifest()} --plan", commands)
        # The command shown is the command that ran, and nothing more.
        self.assertIn(f"Same as: ./pulsar {run} Models and disk space", commands)
        self.assertNotIn("--repo-root", text)
        self.assertTrue(self.manifest().parent.is_dir())
        self.assert_plain_choices()

    def test_cancel_and_back_run_nothing(self):
        self.fixture("acquire-plan", acquire_plan())
        for answers in (["Cancel", "Back"], ["esc", "Back"]):
            result = self.go(*answers)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [])
        for answers in ([DOWNLOAD, "esc", "Back"], [DOWNLOAD, NEW, "esc", "Back"], [DOWNLOAD, NEW, "main", "Back", "Back"]):
            result = self.run_menu(answers)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.commands(), SCAN)
        self.assertFalse(self.manifest().parent.exists())

    def test_a_refusal_of_a_second_copy_is_shown_as_the_command_said_it_and_nothing_runs(self):
        reason = ("an archive already holds org/new-model at " + COMMIT + " (/archive/pulsar-snapshots/x); nothing "
                  "was downloaded. Next: ./pulsar model restore --manifest /archive/pulsar-snapshots/x/manifest.json "
                  "--node spark-2 --plan")
        self.fixture("acquire-plan", envelope(error={"code": "prerequisite_failed", "message": reason}), rc=1)
        result = self.go("Continue", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), SCAN + [self.PLAN])
        text = self.text(result)
        self.assertIn(f"✗ {NEW} at main was not downloaded to spark-2: an archive already holds", text)
        self.assertIn("Next: ./pulsar model restore --manifest /archive/pulsar-snapshots/x/manifest.json", text)
        self.assertIn("Nothing was changed.", text)
        self.assertNotIn("Cancel", [label for menu in self.menus() for label in menu])

    def test_files_already_on_the_node_are_kept_without_a_download_and_a_saved_manifest_is_not_replaced(self):
        self.fixture("acquire-plan", acquire_plan("reuse", search=None))
        self.manifest().parent.mkdir(parents=True)
        from release_spec import build_snapshot_manifest
        kept = json.dumps(build_snapshot_manifest(model_id=NEW, snapshot_revision=COMMIT, files=[
            {"path": "weights.bin", "size": 3, "sha256": "b" * 64}]))
        self.manifest().write_text(kept)
        result = self.go("Keep the files found on spark-2", "Continue", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [f"model acquire --model-id {NEW} --model-commit {COMMIT} --node spark-2 --yes"])
        self.assertEqual(self.manifest().read_text(), kept)
        text = self.text(result)
        self.assertIn("Nothing is downloaded.", text)
        self.assertIn(f"This file exists and is never replaced: {self.manifest()} Nothing is prepared", text)
        self.assertIn(f"Its manifest was already in this file: {self.manifest()} No recipe uses", text)
        self.assertIn("; nothing was downloaded.", text)

    def test_a_file_that_is_not_this_models_manifest_is_never_used_and_never_replaced(self):
        from scripts.download_page import manifest_path
        # Two model names never share a path, whatever dashes they hold.
        self.assertNotEqual(manifest_path(self.root, "org--team/model", COMMIT),
                            manifest_path(self.root, "org/team--model", COMMIT))
        self.fixture("acquire-plan", acquire_plan())
        self.manifest().parent.mkdir(parents=True)
        from release_spec import build_snapshot_manifest
        rows = [{"path": "weights.bin", "size": 3, "sha256": "b" * 64}]
        # Another commit with the same first 12 characters, another model, a file that names this model and
        # commit but is not a valid manifest (the commands that take it would refuse it), and one that is no JSON.
        for other in (json.dumps(build_snapshot_manifest(model_id=NEW, snapshot_revision=COMMIT[:12] + "d" * 28,
                                                         files=rows)),
                      json.dumps(build_snapshot_manifest(model_id="org/other", snapshot_revision=COMMIT, files=rows)),
                      json.dumps({"model_id": NEW, "snapshot_revision": COMMIT, "files": []}), "not json"):
            self.manifest().write_text(other)
            result = self.go("Continue", "Back")
            self.assertEqual(result.returncode, 0, result.stderr)
            text = self.text(result)
            self.assertIn("exists and is not the manifest of this model and commit. This page never replaces a file. "
                          f"Move this file away, then choose this again: {self.manifest()} Nothing was changed.", text)
            self.assertEqual(self.ran(), [])
            self.assertEqual(self.manifest().read_text(), other)

    def test_files_that_were_to_be_kept_and_are_gone_at_the_run_never_become_a_download(self):
        # Approved: keep the files found. Before the run the command's own preview says download.
        self.fixture("acquire-plan", acquire_plan("reuse", search=None), nth=1)
        self.fixture("acquire-plan", acquire_plan("download"), nth=2)
        result = self.go("Keep the files found on spark-2", "Continue", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [])
        self.assertEqual(self.commands(), SCAN + [self.PLAN, self.CHECK] + SCAN)
        text = self.text(result)
        self.assertIn("✗ Nothing ran: what the command would do changed after the plan was shown. Choose this again "
                      "to see the plan as it is now.", text)
        self.assertNotIn("Done:", text)
        # A preview that fails at that moment stops the run the same way.
        self.fixture("acquire-plan", acquire_plan(), nth=1)
        self.fixture("acquire-plan", envelope(error={"code": "prerequisite_failed", "message": "no"}), rc=1, nth=2)
        result = self.go("Download 212 GiB to spark-2", "Continue", "Back")
        self.assertEqual(self.ran(), [])
        self.assertIn("✗ Nothing ran:", self.text(result))

    def test_with_no_node_that_answered_it_offers_no_node_and_runs_nothing(self):
        nodes = [node(identity, host, unread=True) for identity, host in (("n0", "spark-1"), ("n1", "spark-2"))]
        self.fixture("files", envelope(files([], nodes=nodes)))
        result = self.run_menu([DOWNLOAD, NEW, "main", "Continue", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No node answered the check, so there is nowhere to download to.", self.text(result))
        self.assertNotIn("Which node should keep the files?", [menu[0] for menu in self.menus()])
        self.assertEqual(self.commands(), SCAN)

    def test_what_is_typed_never_reaches_a_command_unless_it_is_a_model_name_and_a_ref(self):
        for answers, words in (([DOWNLOAD, "org/new model; rm", "Continue", "Back"], "That is not a model name."),
                               ([DOWNLOAD, "--yes", "Continue", "Back"], "That is not a model name."),
                               ([DOWNLOAD, NEW, "--plan", "Continue", "Back"], "That is not a commit, a branch or a tag.")):
            result = self.run_menu(answers)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(words, self.text(result))
        self.assertEqual(self.ran(), [])
        self.assertFalse(any("acquire" in line for line in self.commands()))

    def test_a_failed_or_unreadable_step_is_said_and_the_archive_that_could_not_be_read_too(self):
        self.fixture("acquire-plan", acquire_plan(search="unreadable"))
        self.fixture("acquire", rc=1)
        result = self.go("Download 212 GiB to spark-2", "Continue", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.text(result)
        self.assertIn("Not checked: The archive location could not be read in full, so whether it holds these files "
                      "is unknown.", text)
        self.assertIn(f"✗ {NEW} at commit cccccccccccc was not put on spark-2. The reason is above.", text)
        self.assertNotIn("✓ Done", text)
        # A preview that is not the plan this page reads never offers a run.
        self.fixture("acquire-plan", envelope({"kind": "pulsar-acquisition-plan", "snapshot_revision": "main"}))
        result = self.go("Continue", "Back")
        self.assertIn("its preview could not be read", self.text(result))
        self.assertEqual(self.ran(), [])


if __name__ == "__main__":
    unittest.main()
