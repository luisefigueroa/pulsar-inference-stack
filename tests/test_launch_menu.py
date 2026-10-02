"""Catalog launch shortcuts reuse existing commands and explicit permissions."""
import copy
import io
import unittest
from unittest.mock import patch

from model_library import catalog_menu as menu
from tests import test_catalog as catalog_tests
from tests import test_catalog_menu as menu_tests
from tests import test_start_blockers as start_tests

WITHDRAWN_REVIEW = {"status": "withdrawn", "reviewer": "example-reviewer",
                    "reviewed_at": "2026-09-03T00:00:00Z",
                    "reason": "Later testing found inconsistent answers."}


class LaunchMenu(unittest.TestCase):
    def run_menu(self, *args, **kwargs):
        fixture = catalog_tests.Catalog()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture.run_menu(*args, **kwargs)

    def shortcut(self, label, *extra, **kwargs):
        return self.run_menu(["#0", "Launch options…", label, "fixture-host", *extra, "Back", "Back"], **kwargs)

    def test_readiness_uses_existing_dry_run_without_permissions(self):
        spec, result, actions, _, questions = self.shortcut("Check launch prerequisites")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["pulsar", "start", spec, "--node", "fixture-node", "--dry-run"]])
        self.assertEqual(questions, "")

    def test_image_check_uses_existing_command(self):
        spec, result, actions, _, questions = self.shortcut("Check pinned image")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [["pulsar", "image", "check", spec, "--node", "fixture-node"]])
        self.assertEqual(questions, "")

    def test_pull_stages_only_after_preview_and_confirmation(self):
        spec, result, actions, _, questions = self.shortcut(
            "Stage pinned image", "Pull pinned image from registry", confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        command = ["pulsar", "image", "stage", spec, "--node", "fixture-node", "--pull"]
        self.assertEqual(actions, [command + ["--plan", "--json"], command + ["--yes"]])
        self.assertIn("Pinned image staging preview", result.stdout)
        self.assertIn("fixture-host", result.stdout)
        self.assertIn("Pull the pinned image", questions)
        self.assertIn("No service starts or is replaced", questions)

    def test_copy_never_adds_registry_fallback(self):
        spec, result, actions, _, questions = self.shortcut(
            "Stage pinned image", "Copy pinned image from this node", confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        command = ["pulsar", "image", "stage", spec, "--node", "fixture-node"]
        self.assertEqual(actions, [command + ["--plan", "--json"], command + ["--yes"]])
        self.assertIn("Copy the pinned image", questions)

    def test_declined_staging_does_not_apply(self):
        _, result, actions, _, _ = self.shortcut(
            "Stage pinned image", "Pull pinned image from registry", confirms=["no"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0][-2:], ["--plan", "--json"])
        self.assertIn("Nothing was staged", result.stdout)

    def test_back_from_staging_does_not_run_a_command(self):
        _, result, actions, _, questions = self.shortcut("Stage pinned image", "Back")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, [])
        self.assertEqual(questions, "")

    def test_failed_preview_does_not_offer_confirmation(self):
        _, result, actions, _, questions = self.shortcut(
            "Stage pinned image", "Pull pinned image from registry", launch={"plan_rc": 3})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(questions, "")
        self.assertIn("nothing was staged", result.stdout)

    def test_already_present_image_does_not_offer_staging(self):
        _, result, actions, _, questions = self.shortcut(
            "Stage pinned image", "Pull pinned image from registry", launch={"image_state": "ok"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(questions, "")
        self.assertIn("nothing to stage", result.stdout)

    def test_unobservable_image_plan_cannot_be_confirmed(self):
        _, result, actions, _, questions = self.shortcut(
            "Stage pinned image", "Pull pinned image from registry", launch={"image_state": "unreachable"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(questions, "")

    def test_memory_warning_retry_is_separately_confirmed_for_the_same_spec_and_node(self):
        spec, result, actions, _, questions = self.run_menu(
            ["#0", "Start", "fixture-host", "Back", "Back"], confirms=["yes", "yes"],
            launch={"start_rc": 1, "blockers": ["memory_warning"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        command = ["pulsar", "start", spec, "--node", "fixture-node"]
        self.assertEqual(actions, [command, command + ["--accept-memory-warn"]])
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
        _, result, actions, _, questions = self.run_menu(
            ["#0", "Start", "fixture-host", "Back", "Back"], confirms=["yes"],
            launch={"start_rc": 1, "blockers": ["memory_warning", "image_missing"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(questions.splitlines()), 1)

    def test_interrupted_start_does_not_offer_a_retry(self):
        _, _, actions, _, questions = self.run_menu(
            ["#0", "Start", "fixture-host", "Back", "Back"], confirms=["yes"],
            launch={"start_rc": 130, "blockers": ["memory_warning"]})
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(questions.splitlines()), 1)

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
