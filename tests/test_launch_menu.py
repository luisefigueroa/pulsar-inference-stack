"""Catalog launch shortcuts reuse existing commands and explicit permissions."""
import copy
import io
import unittest
from unittest.mock import patch

from model_library import catalog_menu as menu
from tests import test_catalog as catalog_tests
from tests import test_catalog_menu as menu_tests
from tests import test_start_blockers as start_tests
from scripts import start_blockers

WITHDRAWN_REVIEW = {"status": "withdrawn", "reviewer": "example-reviewer",
                    "reviewed_at": "2026-09-03T00:00:00Z",
                    "reason": "Later testing found inconsistent answers."}


class LaunchMenu(unittest.TestCase):
    def run_menu(self, *args, **kwargs):
        fixture = catalog_tests.Catalog()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture.run_menu(*args, **kwargs)

    def shortcut(self, label, *extra, cancelled=False, **kwargs):
        back = ["Back"] * (3 if cancelled else 2)
        return self.run_menu(["#0", "Launch options…", label, "fixture-host", *extra, *back], **kwargs)

    def test_readiness_uses_existing_dry_run_without_permissions(self):
        spec, result, actions, _, questions = self.shortcut("Check launch prerequisites")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["pulsar", "start", spec, "--node", "fixture-node", "--dry-run"]])
        self.assertEqual(questions, "")

    def test_image_check_uses_existing_command(self):
        spec, result, actions, _, questions = self.shortcut("Check serving image")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["pulsar", "image", "check", spec, "--node", "fixture-node"]])
        self.assertEqual(questions, "")

    def test_pull_stages_only_after_preview_and_confirmation(self):
        spec, result, actions, _, questions = self.shortcut(
            "Stage serving image", "Pull exact digest from registry", confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        command = ["pulsar", "image", "stage", spec, "--node", "fixture-node", "--pull"]
        self.assertEqual(actions, [command + ["--plan", "--json"], command + ["--yes"]])
        self.assertIn("Serving image staging preview", result.stdout)
        self.assertIn("fixture-host", result.stdout)
        self.assertIn("Pull the serving image", questions)
        self.assertIn("No service starts or is replaced", questions)

    def test_copy_never_adds_registry_fallback(self):
        spec, result, actions, _, questions = self.shortcut(
            "Stage serving image", "Copy serving image from this node", confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        command = ["pulsar", "image", "stage", spec, "--node", "fixture-node"]
        self.assertEqual(actions, [command + ["--plan", "--json"], command + ["--yes"]])
        self.assertIn("Copy the serving image", questions)

    def test_declined_staging_does_not_apply(self):
        _, result, actions, _, _ = self.shortcut(
            "Stage serving image", "Pull exact digest from registry", confirms=["no"], cancelled=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0][-2:], ["--plan", "--json"])
        self.assertIn("Nothing was staged", result.stdout)

    def test_back_from_staging_does_not_run_a_command(self):
        _, result, actions, choices, questions = self.shortcut("Stage serving image", "Back", cancelled=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        self.assertEqual(questions, "")
        self.assertEqual(choices.count("Launch options for the selected catalog spec\n"), 2)

    def test_failed_preview_does_not_offer_confirmation(self):
        _, result, actions, _, questions = self.shortcut(
            "Stage serving image", "Pull exact digest from registry", launch={"plan_rc": 3})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(questions, "")
        # Exit 3: the preview could not tell, which is not the same as a refusal.
        self.assertIn("? The Stage serving image preview could not finish", result.stdout)
        self.assertIn("nothing changed", " ".join(result.stdout.split()))

    def test_interrupted_preview_does_not_request_staging_or_claim_completion(self):
        _, result, actions, _, questions = self.shortcut(
            "Stage serving image", "Pull exact digest from registry", launch={"plan_rc": 130})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(questions, "")
        self.assertIn("Image preview interrupted; staging was not requested", result.stdout)

    def test_interrupted_staging_suggests_inspection_without_automatic_retry(self):
        _, result, actions, _, questions = self.shortcut(
            "Stage serving image", "Pull exact digest from registry", confirms=["yes"], launch={"stage_rc": 130})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 2)
        self.assertEqual(len(questions.splitlines()), 1)
        text = " ".join(result.stdout.split())
        self.assertIn("Interruption requested", text)
        self.assertIn("Next Check serving image", text)
        self.assertNotIn("Stage serving image finished", text)

    def test_already_present_image_does_not_offer_staging(self):
        _, result, actions, _, questions = self.shortcut(
            "Stage serving image", "Pull exact digest from registry", launch={"image_state": "ok"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(questions, "")
        self.assertIn("nothing to stage", result.stdout)

    def test_unobservable_image_plan_cannot_be_confirmed(self):
        _, result, actions, _, questions = self.shortcut(
            "Stage serving image", "Pull exact digest from registry", launch={"image_state": "unreachable"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(questions, "")

    def test_memory_warning_retry_is_separately_confirmed_for_the_same_spec_and_node(self):
        spec, result, actions, choices, questions = self.run_menu(
            ["#0", "Start", "fixture-host", "Back", "Back"], confirms=["yes", "yes"],
            launch={"start_rc": 1, "blockers": ["memory_warning"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        command = ["pulsar", "start", spec, "--node", "fixture-node"]
        self.assertEqual(actions, [command, command + ["--accept-memory-warn"]])
        # The retry question holds the first outcome; one Continue follows the retry.
        self.assertEqual(choices.count("[continue]"), 1)
        self.assertEqual(len(questions.splitlines()), 2)
        self.assertIn("Accept the reduced free-memory headroom", questions)
        self.assertIn("all other blockers still prevent start", questions)

    def test_declined_warning_is_not_remembered_as_permission(self):
        spec, result, actions, _, _ = self.run_menu(
            ["#0", "Start", "fixture-host", "Start", "fixture-host", "Back", "Back"],
            confirms=["yes", "no", "yes", "no"], launch={"start_rc": 1, "blockers": ["memory_warning"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["pulsar", "start", spec, "--node", "fixture-node"]] * 2)

    def test_other_blockers_do_not_offer_memory_acceptance(self):
        _, result, actions, choices, questions = self.run_menu(
            ["#0", "Start", "fixture-host", "Back", "Back"], confirms=["yes"],
            launch={"start_rc": 1, "blockers": ["memory_warning", "image_missing"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(questions.splitlines()), 1)
        self.assertIn("Start was refused: 2 blockers. Choose a next step\nStage serving image\nBack\n", choices)

    # TUI-34: a refused or failed Start offers the menu operations for its blockers.
    def refused(self, blockers, *answers, confirms=("yes",), launch=None, **kwargs):
        return self.run_menu(["#0", "Start", "fixture-host", *answers], confirms=list(confirms),
                             launch={"start_rc": 1, "blockers": blockers, **(launch or {})}, **kwargs)

    def offered(self, choices, title):
        return choices.split(title + "\n", 1)[1].split("\n\n", 1)[0].splitlines()

    def test_refused_start_offers_one_choice_per_operation_from_the_recorded_codes(self):
        _, result, actions, choices, _ = self.refused(
            ["memory_insufficient", "memory_insufficient", "port_in_use", "image_missing", "node_unreachable"],
            "Back", "Back", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(self.offered(choices, "Start was refused: 5 blockers. Choose a next step"),
                         ["Open Live service inventory", "Stage serving image",
                          "Check saved nodes and fabric (checks nodes now)", "Back"])
        # The choice holds the Start output; Back adds no Continue.
        self.assertNotIn("[continue]", choices)

    def test_read_only_choices_return_to_the_same_choices(self):
        _, result, actions, choices, _ = self.refused(
            ["port_in_use", "fabric_incomplete", "docker_unavailable"],
            "Open Live service inventory", "Check saved nodes and fabric (checks nodes now)", "Host diagnostics",
            "Back", "Back", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions[1:3], [["pulsar", "inventory", "menu"], ["pulsar", "topology", "check"]])
        self.assertEqual(actions[3], ["pulsar", "doctor"])
        self.assertEqual(choices.count("Start was refused: 3 blockers"), 4)
        self.assertIn("[page] Checking saved nodes and fabric…", choices)

    def test_existing_service_offers_live_status_and_a_separately_confirmed_stop(self):
        spec, result, actions, choices, questions = self.refused(
            ["service_exists"], "Stop", "fixture-host", "Back", "Back", confirms=["yes", "yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.offered(choices, "Start was refused: 1 blocker. Choose a next step"),
                         ["Live status", "Stop", "Back"])
        self.assertEqual(actions[1:], [["down.sh", spec, "--node", "fixture-node"]])
        self.assertEqual(len(questions.splitlines()), 2)
        self.assertEqual(choices.count("[continue]"), 1)

    def test_declined_stop_returns_to_the_choices_and_changes_nothing(self):
        _, result, actions, choices, _ = self.refused(
            ["service_exists"], "Stop", "fixture-host", "Back", "Back", "Back", confirms=["yes", "no"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(choices.count("Start was refused: 1 blocker"), 2)

    def test_launch_failure_is_named_and_offers_status_logs_and_stop(self):
        _, result, _, choices, _ = self.refused(
            [{"blocker": "health_timeout", "stage": "launch"}], "Back", "Back", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.offered(choices, "Start failed after launch: 1 blocker. Choose a next step"),
                         ["Live status", "Service logs", "Stop", "Back"])

    # TUI-41: Service logs reads what the failed start left, then returns to the choices.
    def test_service_logs_after_a_failed_start_reads_the_containers(self):
        spec, result, actions, choices, _ = self.refused(
            [{"blocker": "container_exited", "stage": "launch"}], "Service logs", "Back", "Back", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions[1:], [["pulsar", "logs", spec]])
        self.assertRegex(choices, rf"\[page\] Reading service logs… \| \S+/pulsar logs {spec}\n")
        self.assertEqual(choices.count("Start failed after launch: 1 blocker. Choose a next step"), 2)

    def test_missing_files_offer_the_operation_saved_records_point_to(self):
        _, _, _, choices, _ = self.refused(["model_files_not_ready"], "Back", "Back", "Back")
        self.assertEqual(self.offered(choices, "Start was refused: 1 blocker. Choose a next step"),
                         ["Download", "Back"])
        _, _, _, choices, _ = self.refused(["model_files_not_ready"], "Back", "Back", "Back", with_home=True)
        self.assertEqual(self.offered(choices, "Start was refused: 1 blocker. Choose a next step"),
                         ["Prepare", "Back"])

    def test_incomplete_membership_opens_the_topology_menu(self):
        _, result, actions, choices, _ = self.refused(
            ["topology_incomplete", "node_unreachable"], "Open Cluster topology (membership)", "Back", "Back", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.offered(choices, "Start was refused: 2 blockers. Choose a next step"),
                         ["Open Cluster topology (membership)", "Check saved nodes and fabric (checks nodes now)",
                          "Back"])
        self.assertEqual(actions[1:], [["pulsar", "topology", "menu"]])

    def test_a_launch_records_cleanup_decides_whether_stop_is_offered(self):
        _, _, _, choices, _ = self.refused(
            [{"blocker": "health_timeout", "stage": "launch", "cleanup": "removed"}], "Back", "Back")
        # Every rank's container was seen removed: nothing to stop, so no choice.
        self.assertNotIn("Choose a next step", choices)
        _, _, _, choices, _ = self.refused(
            [{"blocker": "container_start_failed", "stage": "launch", "cleanup": "unconfirmed"}],
            "Back", "Back", "Back")
        self.assertEqual(self.offered(choices, "Start failed after launch: 1 blocker. Choose a next step"),
                         ["Host diagnostics", "Live status", "Stop", "Back"])
        # A removal that could not be confirmed may have left containers to read.
        _, _, _, choices, _ = self.refused(
            [{"blocker": "health_timeout", "stage": "launch", "cleanup": "unconfirmed"}], "Back", "Back", "Back")
        self.assertEqual(self.offered(choices, "Start failed after launch: 1 blocker. Choose a next step"),
                         ["Live status", "Service logs", "Stop", "Back"])

    def test_failed_check_reruns_the_dry_run_with_full_diagnostics(self):
        spec, result, actions, choices, _ = self.refused(
            ["memory_check_failed"], "Rerun the checks with full diagnostics", "Back", "Back", "Back",
            launch={"dry_rc": 3})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions[1], ["pulsar", "start", spec, "--node", "fixture-node", "--dry-run", "--verbose"])
        # The rerun recorded the same blocker, so the choices follow its dry run.
        self.assertIn("Not ready to start: 1 blocker. Choose a next step", choices)

    def test_choices_never_grant_replacement_or_registry_pulls(self):
        _, _, actions, _, _ = self.refused(
            ["service_exists", "image_missing"], "Stage serving image", "fixture-host",
            "Pull exact digest from registry", "Back", "Back", confirms=["yes", "yes"])
        flat = [arg for action in actions for arg in action]
        self.assertNotIn("--replace", flat)
        self.assertNotIn("--pull-image", flat)
        self.assertIn(["--plan", "--json"], [action[-2:] for action in actions])

    def test_ctrl_c_at_the_choices_leaves_the_menu(self):
        _, result, actions, _, _ = self.refused(["port_in_use"], "<ctrl-c>")
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(actions), 1)

    def test_a_refusal_without_records_offers_nothing_new(self):
        _, result, _, choices, _ = self.refused([], "Back", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Choose a next step", choices)
        self.assertEqual(choices.count("[continue]"), 1)

    def test_refused_dry_run_offers_the_same_choices(self):
        _, result, _, choices, _ = self.shortcut(
            "Check launch prerequisites", "Back", launch={"dry_rc": 1, "blockers": ["image_missing"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.offered(choices, "Not ready to start: 1 blocker. Choose a next step"),
                         ["Stage serving image", "Back"])

    def test_interrupted_start_does_not_offer_a_retry(self):
        _, result, actions, choices, questions = self.run_menu(
            ["#0", "Start", "fixture-host", "Back", "Back"], confirms=["yes"],
            launch={"start_rc": 130, "blockers": ["memory_warning"]})
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(questions.splitlines()), 1)
        self.assertIn("Live status (suggested)", choices.rsplit("Choose one operation\n", 1)[1])
        self.assertIn("not confirmed cleanup or partial effects", " ".join(result.stdout.split()))

    def test_ctrl_c_at_warning_confirmation_exits_without_retry(self):
        _, result, actions, _, _ = self.run_menu(
            ["#0", "Start", "fixture-host"], confirms=["yes", "<ctrl-c>"],
            launch={"start_rc": 1, "blockers": ["memory_warning"]})
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(len(actions), 1)

    def test_successful_start_does_not_offer_a_retry(self):
        _, result, actions, _, questions = self.run_menu(
            ["#0", "Start", "fixture-host", "Back", "Back"], confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(questions.splitlines()), 1)

    def test_withdrawn_start_remains_available_with_one_informed_confirmation(self):
        spec, result, actions, _, questions = self.run_menu(
            ["#0", "Start", "fixture-host", "Back", "Back"], confirms=["yes"], review=WITHDRAWN_REVIEW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["pulsar", "start", spec, "--node", "fixture-node"]])
        self.assertEqual(len(questions.splitlines()), 1)
        self.assertIn("Withdrawn spec.", questions)
        for expected in (WITHDRAWN_REVIEW["reason"], WITHDRAWN_REVIEW["reviewed_at"]):
            self.assertIn(expected, " ".join(result.stdout.split()))
            self.assertIn(expected, " ".join(result.stderr.split()))

    def test_withdrawal_notice_is_repeated_before_the_existing_memory_confirmation(self):
        _, result, actions, _, questions = self.run_menu(
            ["#0", "Start", "fixture-host", "Back", "Back"], confirms=["yes", "yes"],
            review=WITHDRAWN_REVIEW, launch={"start_rc": 1, "blockers": ["memory_warning"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 2)
        self.assertEqual(len(questions.splitlines()), 2)
        self.assertEqual(questions.count("Withdrawn spec."), 2)
        self.assertEqual(result.stderr.count("Maintainer warning: withdrawn"), 2)

    def test_withdrawn_start_can_be_declined_without_an_action(self):
        _, result, actions, _, questions = self.run_menu(
            ["#0", "Start", "fixture-host", "Back", "Back"], confirms=["no"], review=WITHDRAWN_REVIEW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        self.assertEqual(len(questions.splitlines()), 1)
        self.assertIn(WITHDRAWN_REVIEW["reason"], " ".join(result.stderr.split()))


class LaunchPresentation(unittest.TestCase):
    def test_warning_acknowledgement_preserves_the_existing_backend_gates(self):
        for memory, expected in ((1, ["memory_insufficient", "port_in_use"]), (2, ["port_in_use"])):
            with self.subTest(memory=memory):
                fixture = start_tests.StartScenarios()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                result, codes = fixture.start("--accept-memory-warn", image="ok", memory=memory, busy_port=8000)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(codes, expected)
                self.assertFalse(fixture.ran("launched"))

    def test_warning_offer_uses_only_existing_check_blockers(self):
        warning = {"stage": "check", "blocker": "memory_warning"}
        self.assertTrue(menu.memory_warning_only([warning]))
        self.assertTrue(menu.memory_warning_only([warning, warning]))
        for records in ([], [None], [{**warning, "stage": "launch"}],
                        [warning, {"stage": "check", "blocker": "memory_insufficient"}]):
            with self.subTest(records=records):
                self.assertFalse(menu.memory_warning_only(records))

    def test_every_start_blocker_has_choices_or_a_reason_for_none(self):
        codes = set(start_blockers.BLOCKERS)
        self.assertEqual(set(menu.START_CHOICES) | menu.START_NO_CHOICE, codes)
        self.assertFalse(set(menu.START_CHOICES) & menu.START_NO_CHOICE)

    def test_start_choices_ignore_unknown_and_unchoosable_codes(self):
        row = menu_tests.row()
        records = [{"blocker": "not_a_code"}, None, {"blocker": "memory_warning"}, {"blocker": "guard_unsupported"}]
        self.assertEqual(menu.start_choices(records, row, "configured"), [])
        # An operation the recipe leaves out is not offered from here either.
        guarded = menu_tests.row(**menu_tests.GUARDED)
        self.assertEqual(menu.start_choices([{"blocker": "image_missing"}], guarded, "configured"),
                         [("image-stage", "Stage serving image")])
        historical = menu_tests.row(start_unsupported_reason="historical_spec")
        self.assertEqual(menu.start_choices([{"blocker": "image_missing"}], historical, "configured"), [])

    def image_plan(self):
        row = menu_tests.row(image={"digest": "example/image@sha256:" + "c" * 64})
        plan = {"kind": "pulsar-image-check", "operation": "stage-image", "model": row["spec_id"],
                "image": row["image"]["digest"], "mode": "pull-exact-digest", "nodes": 3,
                "ranks": [{"rank": n, "topology_index": n, "state": "missing"} for n in range(3)]}
        return row, {"schema_version": 1, "ok": True, "result": plan}

    def test_image_preview_names_the_pinned_image_and_every_rank_at_narrow_width(self):
        row, response = self.image_plan()
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertTrue(menu.render_image_plan(row, response, {0: "node-a", 1: "node-b", 2: "node-c"},
                                                  mode="pull-exact-digest", width=44))
        text = output.getvalue()
        self.assertTrue(all(len(line) <= 44 for line in text.splitlines()))
        for name in ("node-a", "node-b", "node-c"):
            self.assertIn(name, text)
        self.assertIn(row["image"]["digest"], "".join(text.split()))

    def test_preview_refuses_a_different_spec_image_or_unknown_rank(self):
        row, response = self.image_plan()
        for changes in ({"model": "f" * 64}, {"image": "another/image"}, {"mode": "stream-from-controller"},
                        {"nodes": 2}, {"ranks": []}, {"ranks": [{"rank": n, "topology_index": 0, "state": "missing"} for n in range(3)]}):
            with self.subTest(changes=changes):
                value = copy.deepcopy(response)
                value["result"].update(changes)
                with self.assertRaises(ValueError):
                    menu.render_image_plan(row, value, {0: "node-a", 1: "node-b", 2: "node-c"}, mode="pull-exact-digest")

    def test_guarded_checks_remain_available_without_ordinary_start(self):
        row = menu_tests.row(**menu_tests.GUARDED)
        offered, _, _ = menu.operations(row, "configured")
        self.assertNotIn("start", offered)
        self.assertTrue(set(menu.LAUNCH) <= set(offered))
        row.update(start_unsupported_reason="historical_spec")
        offered, _, _ = menu.operations(row, "configured")
        self.assertFalse(set(menu.LAUNCH) & set(offered))


if __name__ == "__main__":
    unittest.main()
