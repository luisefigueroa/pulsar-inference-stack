"""Inventory navigation with synthetic snapshots and public-command doubles."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import inventory_menu
from tests import test_catalog as catalog_tests
from tests.test_container_runtime import fixture

ROOT = Path(__file__).resolve().parents[1]
SPEC = "a" * 64
CATALOG = [{"spec_id": SPEC, "model_id": "example/model", "nodes": 1}]


def managed(data):
    """Give every rank the Pulsar-managed label that stop --all selects by."""
    for service in data["services"]:
        for rank in service["ranks"]:
            rank["labels"] = {"io.pulsar.gb10.managed": "true"}
    return data


def other(name, ownership="unknown"):
    """A running container outside the catalog."""
    return {"service_id": name, "conf": None, "container_name": name, "state": "running",
            "ownership": ownership, "safe_to_stop": False, "ranks": [{"rank": None, "node": "head"}]}


def snapshot(spec=SPEC, *, count=1, safe=True, state="running", node="worker"):
    nodes = {
        "head": {"node_id": "node-0", "hostname": "fixture-local", "confirmed": True, "probe_status": "ok"},
        "worker": {"node_id": "node-1", "hostname": "fixture-remote", "confirmed": True, "probe_status": "ok"},
    }
    ranks = ([{"rank": "single", "node": node}] if count == 1 else
             [{"rank": str(i), "node": key} for i, key in enumerate(nodes)])
    return {"schema_version": 1, "generated_at": "2026-09-05T00:00:00Z", "nodes": nodes,
            "worker": {"status": "ok"}, "unmanaged_gpu_processes": [],
            "services": [{"service_id": spec, "conf": spec, "state": state,
                          "ownership": "managed", "safe_to_stop": safe, "ranks": ranks}]}


class Projection(unittest.TestCase):
    def test_only_published_specs_get_actions_without_probing(self):
        data = snapshot()
        data["services"] += snapshot("f" * 64)["services"]
        before = copy.deepcopy(data)
        with patch("subprocess.run", side_effect=AssertionError("projection must not probe")):
            projected = inventory_menu.project(data, CATALOG)
        self.assertEqual(set(projected), {SPEC})
        self.assertEqual(projected[SPEC]["node_id"], "node-1")
        self.assertEqual(data, before)
        with self.assertRaises(inventory_menu.ServiceNotObserved):
            inventory_menu.view(data, CATALOG, "f" * 64)

    def test_single_node_actions_keep_observed_placement(self):
        lines = inventory_menu.view(snapshot(), CATALOG, SPEC)
        self.assertIn(f"status\t{SPEC}\tnode-1", lines)
        self.assertIn(f"stop\t{SPEC}\tnode-1", lines)
        self.assertTrue(any("fixture-remote" in line for line in lines))

    def test_multi_node_stop_has_no_single_node_override(self):
        catalog = [{**CATALOG[0], "nodes": 2}]
        data = snapshot(count=2, state="partial")
        data["services"][0]["ranks"].pop()
        lines = inventory_menu.view(data, catalog, SPEC)
        self.assertIn(f"stop\t{SPEC}\t-", lines)
        # TUI-43: the question names the observed nodes, as the catalog's Stop does.
        question = next(line for line in lines if line.startswith("confirm\t"))
        self.assertIn("Stop and remove the service containers of", question)
        self.assertIn("Model files, pins and archives are kept.", question)

    def test_unsafe_or_ambiguous_services_do_not_offer_stop(self):
        unsafe = snapshot(safe=False)
        unknown = snapshot(); unknown["nodes"]["worker"].pop("node_id")
        ambiguous = snapshot(); ambiguous["services"][0]["ranks"] += [{"rank": "single", "node": "head"}]
        legacy = snapshot(); legacy["services"][0]["ownership"] = "legacy"
        for data in (unsafe, unknown, ambiguous, legacy):
            with self.subTest(data=data):
                lines = inventory_menu.view(data, CATALOG, SPEC)
                self.assertFalse(any(line.startswith(("stop\t", "confirm\t")) for line in lines))
                self.assertTrue(any(line.startswith("status\t") for line in lines))

    def test_unobserved_nodes_do_not_turn_empty_inventory_into_confirmed_absence(self):
        data = snapshot(); data["services"] = []
        data["nodes"]["worker"]["probe_status"] = "unreachable"
        text = " ".join(" ".join(inventory_menu.view(data, CATALOG)).split())
        self.assertIn("Unobserved nodes may still be running services", text)

    def test_missing_node_inventory_is_rejected(self):
        data = snapshot(); data["nodes"] = {}
        with self.assertRaises(ValueError):
            inventory_menu.view(data, CATALOG)

    def test_labels_and_headers_fit_narrow_terminals(self):
        catalog = [{**CATALOG[0], "model_id": "example/" + "long-model-" * 12}]
        for width in (32, 44, 80):
            with self.subTest(width=width):
                lines = inventory_menu.view(snapshot(state="degraded"), catalog, width=width)
                for line in lines:
                    fields = line.split("\t")
                    if fields[0] == "header":
                        self.assertLessEqual(len(fields[1]), width)
                    if fields[0] == "service":
                        self.assertLessEqual(len(fields[2]), width - 2)
                        self.assertIn(SPEC[:12], fields[2])


    # TUI-77: the age, the nodes in rank order, and what each row runs on.
    def test_observed_age_and_nodes_in_rank_order(self):
        catalog = [{**CATALOG[0], "nodes": 2}]
        data = snapshot(count=2)
        data["services"][0]["ranks"] = [{"rank": "1", "node": "head"}, {"rank": "0", "node": "worker"}]
        now = datetime(2026, 9, 5, 0, 5, 30, tzinfo=timezone.utc)
        text = "\n".join(inventory_menu.view(data, catalog, SPEC, width=80, now=now))
        self.assertIn("Observed  5 minutes ago (00:00 UTC)", text)
        self.assertIn("Nodes observed fixture-remote, fixture-local", text)
        data["generated_at"] = "not a time"
        self.assertIn("Observed  not a time", "\n".join(inventory_menu.view(data, catalog, SPEC)))

    def test_service_rows_name_the_node_count_and_api_port(self):
        data = snapshot()
        data["services"][0]["api_port"] = 8001
        data["services"][0]["ranks"][0]["observed_api_port"] = 8000
        labels = lambda data, catalog, width: [line.split("\t")[2] for line in
                                               inventory_menu.view(data, catalog, width=width)
                                               if line.startswith("service\t")]
        # The port the container runs with, not today's configuration.
        self.assertEqual(labels(data, CATALOG, 80), [f"RUNNING [{SPEC[:12]}] example/model · 1 node · port 8000"])
        del data["services"][0]["ranks"][0]["observed_api_port"]
        self.assertEqual(labels(data, CATALOG, 80), [f"RUNNING [{SPEC[:12]}] example/model · 1 node · port 8001"])
        partial = snapshot(count=2, state="partial"); partial["services"][0]["ranks"].pop()
        self.assertEqual(labels(partial, [{**CATALOG[0], "nodes": 2}], 80),
                         [f"PARTIAL [{SPEC[:12]}] example/model · 1 of 2 nodes"])
        # Narrow terminals keep the model's name before the details.
        self.assertEqual(labels(data, CATALOG, 50), [f"RUNNING [{SPEC[:12]}] example/model · 1 node"])
        self.assertEqual(labels(data, CATALOG, 44), [f"RUNNING [{SPEC[:12]}] example/model"])

    def test_other_services_each_get_a_read_only_line(self):
        data = snapshot()
        data["services"] += [other("vllm-port80"), other("old-pulsar", "legacy")]
        text = "\n".join(line.split("\t")[1] for line in inventory_menu.view(data, CATALOG, width=80))
        self.assertIn("Other services 2\n  vllm-port80 · unknown owner · read-only\n"
                      "  old-pulsar · older Pulsar labels · read-only\n", text)
        data["services"] += [other(f"extra-{index}") for index in range(3)]
        text = "\n".join(line.split("\t")[1] for line in inventory_menu.view(data, CATALOG, width=80))
        self.assertIn("  extra-0 · unknown owner · read-only\n  and 2 more in Show full inventory\n", text)
        self.assertNotIn("extra-1", text)

    # TUI-78: stop --all names every service with the Pulsar-managed label.
    def test_stop_all_question_names_every_managed_service(self):
        data = managed(snapshot())
        private = managed(snapshot("f" * 64, node="head"))["services"][0]
        private["served_name"] = "private-model"
        data["services"] += [private, other("vllm-port80")]
        question = next(line.split("\t")[1] for line in inventory_menu.view(data, CATALOG)
                        if line.startswith("stopall\t"))
        self.assertEqual(question, f"Stop every Pulsar service on all confirmed nodes? It removes the containers "
                                   f"of example/model [{SPEC[:12]}] on fixture-remote; private-model "
                                   f"[{'f' * 12}] on fixture-local. Containers Pulsar does not manage are "
                                   "untouched. Model files, pins and archives are kept.")
        data["nodes"]["head"]["probe_status"] = "unreachable"
        question = next(line for line in inventory_menu.view(data, CATALOG) if line.startswith("stopall\t"))
        self.assertIn("fixture-local could not be observed, and Stop refuses until every confirmed node answers.",
                      question)
        # A mixed service names only the nodes whose containers carry the label.
        mixed = managed(snapshot(count=2))
        mixed["services"][0]["ranks"][1]["labels"] = {}
        question = next(line for line in inventory_menu.view(mixed, [{**CATALOG[0], "nodes": 2}])
                        if line.startswith("stopall\t"))
        self.assertIn(f"example/model [{SPEC[:12]}] on fixture-local.", question)
        self.assertNotIn("fixture-remote", question)
        # Nothing carries the label: no stop --all choice.
        unlabelled = snapshot(); unlabelled["services"].append(other("vllm-port80"))
        self.assertFalse(any(line.startswith("stopall\t") for line in inventory_menu.view(unlabelled, CATALOG)))


class Navigation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root / "scripts"; scripts.mkdir()
        for name in ("inventory-menu.sh", "inventory_menu.py", "with-connect-card.sh", "connect_card.py"):
            shutil.copy(ROOT / "scripts" / name, scripts / name)
        (scripts / "ui.sh").write_text(catalog_tests.Catalog.UI)
        (self.root / "releases").mkdir()
        self.spec = fixture()[0]
        (self.root / "releases" / f"{self.spec['spec_id']}.json").write_text(json.dumps(self.spec))
        self.log = self.root / "calls.jsonl"
        pulsar = self.root / "pulsar"
        pulsar.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["INVENTORY_MENU_FIXTURE"])
with (root / "calls.jsonl").open("a") as log: log.write(json.dumps(sys.argv[1:]) + "\\n")
config = json.loads((root / "fixture.json").read_text())
if sys.argv[1:] == ["inventory", "--json"]:
    counter = root / "counter"
    index = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(index + 1))
    data = config["snapshots"][min(index, len(config["snapshots"]) - 1)]
    if "failure" in data:
        print("fixture inventory unavailable", file=sys.stderr); sys.exit(data["failure"])
    print(json.dumps(data))
elif sys.argv[1] == "inventory": print("Full inventory fixture")
elif sys.argv[1:] == ["models", "menu"]:
    # Record what the catalog menu would open, and the inventory it was given.
    captured = os.environ.get("PULSAR_CATALOG_INVENTORY")
    (root / "opened.json").write_text(json.dumps({
        "spec": os.environ.get("PULSAR_CATALOG_OPEN_SPEC"),
        "inventory": json.loads(pathlib.Path(captured).read_text()) if captured else None}))
    sys.exit(config.get("catalog_rc", 0))
elif sys.argv[1] in ("status", "stop", "logs"):
    rc = config.get(sys.argv[1] + "_rc", 0)
    print("Existing command " + sys.argv[1] + (" refused" if rc else " completed"))
    sys.exit(rc)
else: sys.exit(99)
''')
        pulsar.chmod(0o755)

    def run_menu(self, answers, *, snapshots=None, confirms=(), continue_rc=0, **config):
        if snapshots is None:
            snapshots = [snapshot(self.spec["spec_id"])]
        (self.root / "fixture.json").write_text(json.dumps({"snapshots": snapshots, **config}))
        (self.root / "answers").write_text("\n".join(answers) + "\n")
        (self.root / "confirms").write_text("\n".join(confirms) + "\n")
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PULSAR_", "CLUSTER_"))}
        env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1", COLUMNS="44",
                   INVENTORY_MENU_FIXTURE=str(self.root), MENU_ANSWERS=str(self.root / "answers"),
                   MENU_CONFIRMS=str(self.root / "confirms"), CHOICES_LOG=str(self.root / "choices"),
                   CONFIRM_LOG=str(self.root / "questions"), CONTINUE_RC=str(continue_rc))
        result = subprocess.run(["bash", str(self.root / "scripts/inventory-menu.sh")],
                                env=env, text=True, capture_output=True, timeout=30)
        choices = (self.root / "choices").read_text() if (self.root / "choices").exists() else ""
        self.assertNotIn("MISSING OPTION", choices)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        questions = (self.root / "questions").read_text() if (self.root / "questions").exists() else ""
        return result, calls, choices, questions

    def test_status_uses_existing_command_and_observed_node_without_picker(self):
        result, calls, choices, questions = self.run_menu(["#0", "Detailed status", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["status", self.spec["spec_id"], "--node", "node-1"], calls)
        self.assertNotIn("Which machine", choices)
        self.assertEqual(questions, "")
        # Its output opens in the pager after a spinner; closing it is the Continue.
        self.assertIn("[page] Checking service status… | ", choices)
        self.assertNotIn("[continue]", choices)

    # TUI-41: Service logs reads every rank of the selected service.
    def test_service_logs_reads_the_selected_service_without_a_question(self):
        result, calls, choices, questions = self.run_menu(["#0", "Service logs", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["logs", self.spec["spec_id"]], calls)
        self.assertIn("Selected catalog service\nDetailed status\nService logs\n", choices)
        self.assertIn("[page] Reading service logs… | ", choices)
        self.assertEqual(questions, "")
        self.assertNotIn("[continue]", choices)
        self.setUp()
        result, _, choices, _ = self.run_menu(["#0", "Service logs", "Back", "Back"], logs_rc=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[continue]", choices)

    def test_status_outcome_separates_not_running_from_unknown(self):
        for rc, expected in ((0, None), (1, "▲ example/model [{id}] is not running."),
                             (3, "? Whether example/model [{id}] is running is unknown")):
            with self.subTest(rc=rc):
                for used in ("counter", "answers.n", "confirms.n"):  # Each run starts afresh.
                    (self.root / used).unlink(missing_ok=True)
                result, _, _, _ = self.run_menu(["#0", "Detailed status", "Back", "Back"], status_rc=rc)
                self.assertEqual(result.returncode, 0, result.stderr)
                outcome = " ".join(result.stdout.split())
                if expected is None:  # Status output is the result; no outcome line follows.
                    self.assertNotIn("is not running", outcome)
                    self.assertNotIn("exit 0", outcome)
                else:
                    self.assertIn(expected.format(id=self.spec["spec_id"][:12]), outcome)
                    self.assertIn(f"exit {rc} after", outcome)

    def test_stop_refreshes_scope_then_confirms_the_existing_command(self):
        first = snapshot(self.spec["spec_id"])
        moved = snapshot(self.spec["spec_id"], node="head")
        empty = snapshot(self.spec["spec_id"]); empty["services"] = []
        result, calls, _, questions = self.run_menu(["#0", "Stop service", "Back"],
            snapshots=[first, moved, empty], confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in calls if call[0] == "stop"],
                         [["stop", self.spec["spec_id"], "--node", "node-0"]])
        self.assertIn("fixture-local", questions)
        self.assertNotIn("fixture-remote", questions)
        self.assertNotIn("error:", result.stderr)
        # The same outcome line as the catalog menu's operations.
        # TUI-43: the outcome names the model as well as the spec.
        self.assertRegex(" ".join(result.stdout.split()),
                         rf"✓ Stop finished for example/model \[{self.spec['spec_id'][:12]}\] in \d+s ")
        # TUI-75: and the command it ran; at 44 columns it moves whole to its own line.
        self.assertIn(f"  Same as:\n    ./pulsar stop {self.spec['spec_id'][:12]} --node node-0\n", result.stdout)

    def test_declined_stop_never_calls_stop(self):
        result, calls, _, questions = self.run_menu(["#0", "Stop service", "Back", "Back"], confirms=["no"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "stop" for call in calls))
        self.assertIn("Model files, pins and archives are kept", questions)

    def test_multi_node_stop_keeps_the_existing_whole_service_scope(self):
        self.spec = fixture(2)[0]
        (self.root / "releases" / f"{self.spec['spec_id']}.json").write_text(json.dumps(self.spec))
        active = snapshot(self.spec["spec_id"], count=2)
        empty = copy.deepcopy(active); empty["services"] = []
        result, calls, _, questions = self.run_menu(["#0", "Stop service", "Back"],
            snapshots=[active, active, empty], confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in calls if call[0] == "stop"], [["stop", self.spec["spec_id"]]])
        # TUI-43: the question names the observed nodes and says archives are kept.
        self.assertIn(f"Stop and remove the service containers of example/model [{self.spec['spec_id'][:12]}] on ",
                      questions)
        self.assertIn("fixture-local", questions)
        self.assertIn("Model files, pins and archives are kept.", questions)

    def test_lost_observability_prevents_confirmation(self):
        before = snapshot(self.spec["spec_id"])
        after = snapshot(self.spec["spec_id"], safe=False)
        result, calls, _, questions = self.run_menu(["#0", "Stop service", "Back", "Back"], snapshots=[before, after])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "stop" for call in calls))
        self.assertEqual(questions, "")

    def test_failed_refresh_does_not_reuse_old_stop_scope(self):
        result, calls, _, questions = self.run_menu(["#0", "Stop service", "Back"],
            snapshots=[snapshot(self.spec["spec_id"]), {"failure": 1}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "stop" for call in calls))
        self.assertEqual(questions, "")

    def test_disappeared_service_cannot_be_confirmed(self):
        before = snapshot(self.spec["spec_id"])
        after = copy.deepcopy(before); after["services"] = []
        result, calls, _, questions = self.run_menu(["#0", "Stop service", "Back"], snapshots=[before, after])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "stop" for call in calls))
        self.assertEqual(questions, "")

    def test_stop_refusal_is_retained_and_not_retried(self):
        result, calls, _, _ = self.run_menu(["#0", "Stop service", "Back", "Back"], confirms=["yes"], stop_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(call[0] == "stop" for call in calls), 1)
        self.assertIn("Existing command stop refused", result.stdout)
        outcome = " ".join(result.stdout.split())
        self.assertIn(f"✗ Stop was refused or failed for example/model [{self.spec['spec_id'][:12]}].", outcome)
        self.assertRegex(outcome, r"exit 1 after \d+s · the reasons are above")

    def test_other_workloads_remain_in_the_existing_full_inventory(self):
        result, calls, choices, _ = self.run_menu(["Show full inventory", "Back"], snapshots=[snapshot("f" * 64)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["inventory"], calls)
        self.assertIn("[page] Checking service inventory… | ", choices)
        self.assertIn("Full inventory fixture", result.stdout)
        self.assertFalse(any(call[0] in ("stop", "status") for call in calls))
        self.assertNotIn("Detailed status", choices)

    def test_stop_outcome_waits_for_continue_and_keeps_ctrl_c(self):
        result, calls, choices, _ = self.run_menu(["#0", "Stop service", "Back", "Back"], confirms=["yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        # The outcome stays on screen until Continue; the refreshed menu follows.
        blocks = [block.splitlines()[0].strip() for block in choices.strip().split("\n\n")]
        self.assertEqual(blocks[blocks.index("[continue]"):], ["[continue]", "Selected catalog service",
                                                                "Select a catalog service"])
        for used in ("counter", "answers.n", "confirms.n", "choices"):
            (self.root / used).unlink(missing_ok=True)
        result, calls, choices, _ = self.run_menu(["#0", "Stop service"], confirms=["yes"], continue_rc=130)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertTrue(choices.rstrip().endswith("[continue]"), choices)

    def test_ctrl_c_at_confirmation_does_not_stop(self):
        result, calls, _, _ = self.run_menu(["#0", "Stop service"], confirms=["<ctrl-c>"])
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertFalse(any(call[0] == "stop" for call in calls))

    def test_open_in_catalog_passes_the_service_and_its_inventory_then_refreshes(self):
        first = snapshot(self.spec["spec_id"])
        result, calls, choices, _ = self.run_menu(["#0", "Open in catalog", "Back", "Back"],
                                                  snapshots=[first, snapshot(self.spec["spec_id"], node="head")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in calls if call[0] != "inventory"], [["models", "menu"]])
        opened = json.loads((self.root / "opened.json").read_text())
        self.assertEqual(opened, {"spec": self.spec["spec_id"], "inventory": first})
        # Back from the catalog returns to the same service with a fresh
        # inventory; the service list refreshes again after that.
        self.assertEqual(calls.count(["inventory", "--json"]), 3)
        self.assertEqual(choices.count("Selected catalog service\n"), 2)

    def test_ctrl_c_in_the_opened_catalog_exits_the_inventory_menu(self):
        result, calls, _, _ = self.run_menu(["#0", "Open in catalog"], catalog_rc=130)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(calls.count(["inventory", "--json"]), 1)

    def test_stop_every_service_refreshes_then_confirms_stop_all(self):
        spec = self.spec["spec_id"]
        result, calls, choices, questions = self.run_menu(
            ["Stop every Pulsar service…", "Back"], confirms=["yes"],
            snapshots=[managed(snapshot(spec)), managed(snapshot(spec, node="head")), snapshot(spec)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in calls if call[0] != "inventory"], [["stop", "--all", "--yes"]])
        # The question comes from the refreshed inventory and defaults to No.
        self.assertIn(f"containers of example/model [{spec[:12]}] on fixture-local.", questions)
        self.assertNotIn("fixture-remote", questions)
        self.assertIn("Containers Pulsar does not manage are untouched", questions)
        self.assertIn("Refresh inventory\nShow full inventory\nStop every Pulsar service…\nBack\n", choices)
        text = " ".join(result.stdout.split())
        self.assertIn("✓ Stop finished for every Pulsar service", text)
        self.assertIn("Same as: ./pulsar stop --all --yes", text)
        self.assertIn("[continue]", choices)
        # The menu after the stop offers no stop --all, since nothing carries the label.
        self.assertTrue(choices.endswith("Refresh inventory\nShow full inventory\nBack\n\n"), choices)

    def test_declined_or_vanished_stop_all_stops_nothing(self):
        spec = self.spec["spec_id"]
        result, calls, _, _ = self.run_menu(["Stop every Pulsar service…", "Back"], confirms=["no"],
                                            snapshots=[managed(snapshot(spec))])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "stop" for call in calls))
        self.assertIn("Nothing was stopped.", result.stdout)
        self.setUp()
        result, calls, _, questions = self.run_menu(["Stop every Pulsar service…", "Back"],
                                                    snapshots=[managed(snapshot(spec)), snapshot(spec)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "stop" for call in calls))
        self.assertEqual(questions, "")
        self.assertIn("No Pulsar service was observed just now; nothing was stopped.", result.stdout)

    def test_interrupted_inventory_exits_without_service_actions(self):
        result, calls, _, questions = self.run_menu([], snapshots=[{"failure": 130}])
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(calls, [["inventory", "--json"]])
        self.assertEqual(questions, "")


if __name__ == "__main__":
    unittest.main()
