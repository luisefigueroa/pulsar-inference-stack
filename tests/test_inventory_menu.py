"""Inventory navigation with synthetic snapshots and public-command doubles."""
import copy
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
        self.assertTrue(any("across its participating nodes" in line for line in lines))

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
                        self.assertLessEqual(len(fields[2]), width - 6)
                        self.assertIn(SPEC[:12], fields[2])


class Navigation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root / "scripts"; scripts.mkdir()
        for name in ("inventory-menu.sh", "inventory_menu.py"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
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
elif sys.argv[1] in ("status", "stop"):
    rc = config.get(sys.argv[1] + "_rc", 0)
    print("Existing command " + sys.argv[1] + (" refused" if rc else " completed"))
    sys.exit(rc)
else: sys.exit(99)
''')
        pulsar.chmod(0o755)

    def run_menu(self, answers, *, snapshots=None, confirms=(), **config):
        if snapshots is None:
            snapshots = [snapshot(self.spec["spec_id"])]
        (self.root / "fixture.json").write_text(json.dumps({"snapshots": snapshots, **config}))
        (self.root / "answers").write_text("\n".join(answers) + "\n")
        (self.root / "confirms").write_text("\n".join(confirms) + "\n")
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PULSAR_", "CLUSTER_"))}
        env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1", COLUMNS="44",
                   INVENTORY_MENU_FIXTURE=str(self.root), MENU_ANSWERS=str(self.root / "answers"),
                   MENU_CONFIRMS=str(self.root / "confirms"), CHOICES_LOG=str(self.root / "choices"),
                   CONFIRM_LOG=str(self.root / "questions"))
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
        self.assertNotIn("Select a confirmed physical node", choices)
        self.assertEqual(questions, "")

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
        self.assertIn("across its participating nodes", questions)

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

    def test_other_workloads_remain_in_the_existing_full_inventory(self):
        result, calls, choices, _ = self.run_menu(["Show full inventory", "Back"], snapshots=[snapshot("f" * 64)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["inventory"], calls)
        self.assertFalse(any(call[0] in ("stop", "status") for call in calls))
        self.assertNotIn("Detailed status", choices)

    def test_ctrl_c_at_confirmation_does_not_stop(self):
        result, calls, _, _ = self.run_menu(["#0", "Stop service"], confirms=["<ctrl-c>"])
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertFalse(any(call[0] == "stop" for call in calls))

    def test_interrupted_inventory_exits_without_service_actions(self):
        result, calls, _, questions = self.run_menu([], snapshots=[{"failure": 130}])
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(calls, [["inventory", "--json"]])
        self.assertEqual(questions, "")


if __name__ == "__main__":
    unittest.main()
