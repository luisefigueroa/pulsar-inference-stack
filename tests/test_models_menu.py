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
from tests.test_models_page import (DRAFT, G, MODEL, OTHER, OTHER_FILES, SPEC, archived, binding, budget,  # noqa: E402
                                    entry, files, found_home, home, location, lost_copy, node, prepare_preview,
                                    purge_preview, ready_model, recipe)
from tests.test_serve_menu import Harness, build_root, root_environment  # noqa: E402
from tests.test_serve_plan import envelope  # noqa: E402

FIND = "Look for model files Pulsar has no record of…"
DOWNLOAD = "Download a model that has no recipe yet…"
PAGE = ["Models and disk space", "Free up space…", "Get a model ready ahead of time…", DOWNLOAD,
        "Keep a model ready…", FIND, "Look again (checks nodes now)", "Back"]
WITH_ARCHIVES = ["Models and disk space", "Free up space…", "Get a model ready ahead of time…", DOWNLOAD,
                 "Keep a model ready…", "Back up a model…", "Check the archives…", FIND, "Look again (checks nodes now)", "Back"]
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
        self.fixture("files", envelope(files([ready_model()])))
        self.fixture("budget", envelope(budget()))
        self.fixture("purge-plan", purge_plan("n1", "n2"))
        self.fixture("pin-plan", retention_plan("pin", False))
        self.fixture("unpin-plan", retention_plan("unpin", True))

    def text(self, result):
        return " ".join(result.stdout.split())

    def with_archive(self):
        return {"PULSAR_COLD_ROOT": str(self.archive)}


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

    def test_screens_reuse_the_scan_until_a_plan_runs_or_look_again(self):
        result = self.run_menu(["Free up space…", "Back", "Keep a model ready…", "Back", "Free up space…",
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
        self.assertEqual(text.count("Models and disk space · checked less than a minute ago"), 2)
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
        result = self.run_menu(["Free up space…", "Stop keeping org/model ready", "Go ahead", "Continue", "Back",
                                "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Free up space", "Stop keeping org/model ready", "Back"])
        self.assertIn("org/model is kept ready on spark-1, spark-2 and spark-3, so Free up space… does not offer it.",
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


class Intents(Menu):
    def test_keep_a_model_ready_is_pin_by_that_name(self):
        result = self.run_menu(["Keep a model ready…", "Keep org/model ready", "Go ahead", "Continue", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Keep a model ready", "Keep org/model ready", "Back"])
        self.assertEqual(self.menus()[2], ["Keep a model ready", "Cancel", "Go ahead"])
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
        result = self.run_menu(["Back up a model…", "Back up org/model", "Go ahead", "Continue", "Back", "Back"],
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
        result = self.run_menu(["Check the archives…", "Check the archives of org/model", "Go ahead", "Continue",
                                "Back", "Back"], **self.with_archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [f"model archive verify {SPEC}"])
        self.assertNotIn("model archive verify --plan", " ".join(self.commands()))
        self.assertIn("✓ Done: every archive of org/model matched its expected file hashes.", self.text(result))

    def test_get_a_model_ready_opens_the_serve_plan_without_start_and_reuses_the_scan_on_back(self):
        result = self.run_menu(["Get a model ready ahead of time…", "Back", "Back"])
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
        self.assertEqual(self.menus()[1], ["Look for model files", "Cancel", "Go ahead"])
        self.assertIn("Look for model files" + chr(9) + "0", self.default_log.read_text().splitlines())
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
                            "./pulsar model prepare SPEC --adopt-unrecorded --plan"):
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
