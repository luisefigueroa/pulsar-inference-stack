"""The saved service inventory and storage budget, and the home menu's Running block."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import saved_observations as saved
from tests.test_home_menu import never_checked
from tests.test_inventory_menu import SPEC, outside, snapshot

ROOT = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
G = 1024 ** 3
NOW = datetime(2026, 9, 5, 0, 3, 30, tzinfo=timezone.utc)
ENTRIES = [{**never_checked("a"), "model_id": "example/model"}]


def budget_node(node_id, available=300 * G, *, reserve=100 * G, limit=200 * G, used=50 * G):
    """A model budget node, shaped as tests.test_catalog.Catalog.budget() gives it."""
    return {"node_id": node_id, "available": available, "used": used, "total": 1000 * G,
            "reserve": reserve, "limit": limit, "path": "/fixture/views"}


def budget_record(*nodes, saved_at="2026-09-04T22:00:00Z"):
    return {"schema_version": 1, "kind": saved.BUDGET_KIND, "saved_at": saved_at,
            "nodes": list(nodes) or [budget_node("node-1")]}


def observed():
    """A complete inventory taken at 00:00 UTC: example/model runs on fixture-remote
    with its port and measured GPU memory, and both nodes report their memory."""
    data = snapshot()
    data["nodes"]["head"].update(mem_available_gib=15.2, mem_total_gib=121.7)
    data["nodes"]["worker"].update(mem_available_gib=19.4, mem_total_gib=121.7)
    data["services"][0]["ranks"][0].update(observed_api_port=8000, gpu_memory={"measured_mib": 91692})
    return data


class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))

    def test_inventory_is_saved_privately_and_reads_back_unchanged(self):
        path = self.root / "state" / "service-inventory.json"
        self.assertIsNone(saved.load_inventory(path))
        self.assertTrue(saved.save_inventory(observed(), path))
        self.assertTrue(stat.S_ISREG(path.lstat().st_mode))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), observed())
        self.assertEqual(saved.load_inventory(path), observed())

    def test_invalid_inventories_are_refused_before_writing(self):
        path = self.root / "service-inventory.json"
        future = observed(); future["schema_version"] = 2
        nodes = observed(); nodes["nodes"] = []
        stamp = observed(); stamp["generated_at"] = "yesterday"
        naive = observed(); naive["generated_at"] = "2026-09-05T00:00:00"
        for document in (future, nodes, stamp, naive, [], None):
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    saved.save_inventory(document, path)
                self.assertFalse(path.exists())

    def test_an_older_inventory_never_replaces_a_newer_one(self):
        path = self.root / "service-inventory.json"
        newer = observed(); newer["generated_at"] = "2026-09-05T00:00:01.500000+00:00"
        self.assertTrue(saved.save_inventory(newer, path))
        # Second precision with Z, as inventory.sh writes it, against microseconds.
        self.assertFalse(saved.save_inventory(observed(), path))
        self.assertEqual(saved.load_inventory(path)["generated_at"], newer["generated_at"])
        later = observed(); later["generated_at"] = "2026-09-05T00:00:02Z"
        self.assertTrue(saved.save_inventory(later, path))
        self.assertEqual(saved.load_inventory(path)["generated_at"], "2026-09-05T00:00:02Z")

    def test_a_corrupt_saved_file_is_replaced_never_compared(self):
        for name, content in (("service-inventory.json", "{not json"), ("storage-budget.json", '{"kind": 1}')):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text(content)
                load = saved.load_inventory if name.startswith("service") else saved.load_budget
                with self.assertRaises(ValueError):
                    load(path)
                if name.startswith("service"):
                    self.assertTrue(saved.save_inventory(observed(), path))
                else:
                    self.assertTrue(saved.save_budget([budget_node("node-1")], path))
                self.assertIsNotNone(load(path))

    def test_budget_is_stamped_when_saved(self):
        path = self.root / "nested" / "storage-budget.json"
        self.assertIsNone(saved.load_budget(path))
        nodes = [budget_node("node-0"), budget_node("node-1")]
        self.assertTrue(saved.save_budget(nodes, path, clock=lambda: "2026-09-05T00:01:00Z"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(saved.load_budget(path), {"schema_version": 1, "kind": "pulsar-saved-storage-budget",
                                                   "saved_at": "2026-09-05T00:01:00Z", "nodes": nodes})
        # Without a clock the stamp is the current UTC time.
        before = datetime.now(timezone.utc).replace(microsecond=0)
        saved.save_budget(nodes, path)
        stamp = datetime.fromisoformat(saved.load_budget(path)["saved_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before, stamp)

    def test_invalid_budgets_are_refused(self):
        path = self.root / "storage-budget.json"
        unnamed = budget_node("node-0"); unnamed.pop("node_id")
        fractional = budget_node("node-0"); fractional["available"] = 1.5
        flag = budget_node("node-0"); flag["limit"] = True
        for nodes in ([], [unnamed], [fractional], [flag], {"nodes": [budget_node("node-0")]}, ["node-0"]):
            with self.subTest(nodes=nodes):
                with self.assertRaises(ValueError):
                    saved.save_budget(nodes, path)
                self.assertFalse(path.exists())

    def test_change_key_moves_only_with_a_new_observation_or_turning_last_seen(self):
        inventory, budget = observed(), budget_record()
        self.assertEqual(saved.change_key(None, None), "none none")
        key = saved.change_key(inventory, budget, NOW)
        self.assertEqual(key, "2026-09-05T00:00:00Z fresh 2026-09-04T22:00:00Z")
        self.assertEqual(saved.change_key(inventory, budget, NOW + timedelta(minutes=5)), key)
        stale = saved.change_key(inventory, None, NOW + timedelta(minutes=11))
        self.assertEqual(stale, "2026-09-05T00:00:00Z last-seen none")
        self.assertEqual(saved.change_key(inventory, None, NOW + timedelta(hours=3)), stale)
        newer = observed(); newer["generated_at"] = "2026-09-05T00:02:00Z"
        self.assertNotEqual(saved.change_key(newer, budget, NOW), key)
        self.assertNotEqual(saved.change_key(inventory, budget_record(saved_at="2026-09-05T00:01:00Z"), NOW), key)


class Command(unittest.TestCase):
    """python3 -m scripts.saved_observations, as inventory.sh and model budget run it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.env = {key: value for key, value in os.environ.items()
                    if key not in (saved.INVENTORY_ENV, saved.BUDGET_ENV, "PULSAR_COLOR", "CLUSTER_TOPOLOGY_FILE")}
        self.env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")

    def run_command(self, *args, stdin="", **extra):
        return subprocess.run([sys.executable, "-m", "scripts.saved_observations", *args,
                               "--repo-root", str(self.root)],
                              input=stdin, env={**self.env, **extra}, cwd=ROOT, text=True, capture_output=True,
                              timeout=60)

    def test_saving_into_the_checkout_and_through_the_overrides(self):
        result = self.run_command("save-inventory", stdin=json.dumps(observed()))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(json.loads((self.root / ".pulsar/service-inventory.json").read_text()), observed())
        result = self.run_command("save-budget", stdin=json.dumps([budget_node("node-1")]))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(json.loads((self.root / ".pulsar/storage-budget.json").read_text())["nodes"],
                         [budget_node("node-1")])
        inventory, budget = self.root / "elsewhere/inventory.json", self.root / "elsewhere/budget.json"
        overrides = {saved.INVENTORY_ENV: str(inventory), saved.BUDGET_ENV: str(budget)}
        self.run_command("save-inventory", stdin=json.dumps(observed()), **overrides)
        self.run_command("save-budget", stdin=json.dumps([budget_node("node-2")]), **overrides)
        self.assertEqual(json.loads(inventory.read_text()), observed())
        self.assertEqual(json.loads(budget.read_text())["nodes"], [budget_node("node-2")])
        key = self.run_command("key", **overrides)
        self.assertEqual(key.stdout.split()[:2], ["2026-09-05T00:00:00Z", "last-seen"])

    def test_a_failed_save_is_one_warning_and_never_fails_its_caller(self):
        for command, stdin, extra in (
                ("save-inventory", "not json", {}),
                ("save-inventory", json.dumps({**observed(), "schema_version": 2}), {}),
                ("save-budget", json.dumps([{"node_id": "node-1"}]), {}),
                ("save-budget", json.dumps([budget_node("node-1")]), {saved.BUDGET_ENV: "relative/budget.json"}),
                ("save-inventory", json.dumps(observed()), {saved.INVENTORY_ENV: "relative/inventory.json"})):
            with self.subTest(command=command, stdin=stdin[:20], extra=extra):
                result = self.run_command(command, stdin=stdin, **extra)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                what = "service inventory" if command == "save-inventory" else "storage budget"
                self.assertRegex(result.stderr, rf"\Awarning: the {what} was not saved to \S+: \S.*\n\Z")
        self.assertFalse((self.root / ".pulsar").exists())
        self.assertFalse((ROOT / "relative").exists())

    def test_home_reads_the_records_and_says_when_one_is_unreadable(self):
        catalog = json.dumps({"entries": ENTRIES})
        result = self.run_command("home", "--width", "80", stdin=catalog)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0][:26], "Running   not observed yet")
        (self.root / ".pulsar").mkdir()
        (self.root / ".pulsar/service-inventory.json").write_text("{broken")
        (self.root / ".pulsar/storage-budget.json").write_text(json.dumps(budget_record()))
        result = self.run_command("home", "--width", "80", stdin=catalog)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("Services saved inventory unreadable; Cluster overview (checks nodes now) replaces it", text)
        self.assertIn("Disk node-1 can still copy 150 GiB · 300 GiB free", text)
        # An unreadable record keys as none, so the block prints again once it is replaced.
        key = self.run_command("key")
        self.assertEqual(key.stdout, "none 2026-09-04T22:00:00Z\n")


class RunningBlock(unittest.TestCase):
    def block(self, inventory, budget=None, *, width=80, now=NOW, entries=ENTRIES, **options):
        text = saved.home_block(inventory, budget, entries, width=width, now=now, **options)
        for line in text.splitlines():
            self.assertLessEqual(len(line), width, line)
            # Wraps fall between parts, never between a number and its unit
            # or after a separator.
            self.assertNotRegex(line, r"(\bport|\bof|·)$", line)
            self.assertNotRegex(line, r"^\s+(GiB|TiB|MiB|KiB|UTC\))", line)
            self.assertNotIn(" ", line)
        return text

    def flat(self, text):
        return " ".join(text.split())

    def test_a_fresh_service_with_its_nodes_port_memory_and_ages(self):
        budget = budget_record(budget_node("node-0", 1400 * G, limit=3000 * G), budget_node("node-1", 1100 * G))
        for width in (80, 60, 44):
            with self.subTest(width=width):
                text = self.block(observed(), budget, width=width)
                flat = self.flat(text)
                self.assertTrue(text.startswith("Running\n  Services  example/model · running on"))
                self.assertIn("example/model · running on fixture-remote · port 8000 · 89.5 GiB GPU memory", flat)
                self.assertIn("Memory fixture-local 15 GiB available of 122 fixture-remote 19 GiB available of 122",
                              flat)
                self.assertIn("Disk fixture-local can still copy 1.27 TiB · 1.37 TiB free "
                              "fixture-remote can still copy 150 GiB · 1.07 TiB free", flat)
                self.assertIn("Observed services 3 minutes ago (00:00 UTC) · disk 2 hours ago; "
                              "Cluster overview (checks nodes now) rechecks", flat)
                self.assertNotIn("last seen", flat)
                self.assertNotIn("GPU work", flat)
                # Rows in the setup status's layout: labels at 2, values at 12.
                self.assertRegex(text, r"(?m)^  Memory    fixture-local ")

    def test_an_inventory_older_than_ten_minutes_reads_last_seen(self):
        flat = self.flat(self.block(observed(), now=NOW + timedelta(minutes=11), width=44))
        self.assertIn("example/model · last seen running on fixture-remote", flat)
        self.assertIn("services 14 minutes ago (00:00 UTC); last seen, not current; "
                      "Cluster overview (checks nodes now) rechecks", flat)
        self.assertIn("Disk not observed yet; Cluster overview (checks nodes now) or Model files and disk space "
                      "(checks nodes now) reads it", flat)

    def test_a_node_that_did_not_answer_leaves_services_unknown_not_absent(self):
        data = observed()
        data["services"] = []
        data["nodes"]["worker"].update(probe_status="unreachable", probe_reason="ssh timed out")
        flat = self.flat(self.block(data, width=44))
        self.assertIn("Services unknown; fixture-remote not observed", flat)
        self.assertIn("fixture-remote not observed: ssh timed out", flat)
        self.assertNotIn("none running", flat)
        # A service seen elsewhere does not make the rest known.
        data = observed()
        data["services"][0]["ranks"][0]["node"] = "head"
        data["nodes"]["worker"].update(probe_status="unreachable", probe_reason="ssh timed out")
        flat = self.flat(self.block(data))
        self.assertIn("example/model · running on fixture-local", flat)
        self.assertIn("others unknown; fixture-remote not observed", flat)
        # Only the probe summary failed: no node to name.
        data = observed()
        data["services"] = []
        data["worker"]["status"] = "failed"
        self.assertIn("Services unknown; a node could not be observed", self.flat(self.block(data)))

    def test_a_complete_inventory_without_services_reads_none_running(self):
        data = observed()
        data["services"] = []
        data["nodes"]["head"].pop("mem_available_gib")
        flat = self.flat(self.block(data))
        self.assertIn("Services none running", flat)
        self.assertIn("fixture-local memory not measured", flat)

    def test_a_one_node_cluster_shows_no_placeholder_worker(self):
        data = observed()
        data["services"][0]["ranks"][0]["node"] = "head"
        data["worker"] = {"status": "unset"}
        data["nodes"]["worker"] = {"hostname": None, "node_id": None, "confirmed": False, "probe_status": "unset",
                                   "probe_reason": "no other nodes confirmed", "mem_available_gib": None}
        flat = self.flat(self.block(data))
        self.assertIn("Memory fixture-local 15 GiB available of 122", flat)
        self.assertNotIn("worker", flat)
        self.assertNotIn("no other nodes confirmed", flat)

    def test_services_outside_the_catalog_and_gpu_work_outside_pulsar(self):
        data = observed()
        data["services"].append(outside())
        data["services"][1]["state"] = "stale"
        data["unmanaged_gpu_processes"] = [{"node": "head", "pid": 1, "used_memory_mib": 40960},
                                           {"node": "head", "pid": 2, "used_memory_mib": 3072},
                                           {"node": "worker", "pid": 3, "used_memory_mib": 0}]
        for width in (80, 44):
            with self.subTest(width=width):
                text = self.block(data, width=width)
                flat = self.flat(text)
                self.assertIn("experiment-model · exited containers on fixture-local · port 8001 · "
                              "outside the catalog", flat)
                self.assertIn("GPU work 3 processes Pulsar did not start · 43.0 GiB on fixture-local", flat)
                # Each service starts its own row; a wrapped part starts with its separator.
                self.assertRegex(text, r"(?m)^ {12}experiment-model ")
                self.assertIn("outside the catalog", text)
        data["unmanaged_gpu_processes"] = data["unmanaged_gpu_processes"][:1]
        self.assertIn("GPU work 1 process Pulsar did not start", self.flat(self.block(data)))

    def test_disk_rows_name_each_node_and_its_headroom(self):
        # A saved budget always has whole bytes; a node without its copy
        # budget stands in for headroom the budget could not tell.
        budget = budget_record(budget_node("e" * 40, 60 * G, reserve=72 * G),
                               budget_node("node-1", 500 * G),
                               {**budget_node("node-0", 900 * G), "limit": None},
                               budget_node("f" * 40, 400 * G))
        for width in (80, 44):
            with self.subTest(width=width):
                text = self.block(observed(), budget, hostnames={"f" * 40: "fixture-third"}, width=width)
                flat = self.flat(text)
                self.assertIn("Disk fixture-local headroom not observed · 900 GiB free "
                              "fixture-remote can still copy 150 GiB · 500 GiB free "
                              "eeeeeeeeeeee 12.0 GiB past its reserve or copy budget · 60.0 GiB free "
                              "fixture-third can still copy 150 GiB · 400 GiB free", flat)
                self.assertNotIn("e" * 13, flat)

    def test_one_record_without_the_other(self):
        flat = self.flat(self.block(None, budget_record()))
        self.assertIn("Services not observed yet Memory not observed yet Disk node-1 can still copy 150 GiB", flat)
        self.assertIn("Observed disk 2 hours ago; Cluster overview (checks nodes now) rechecks", flat)
        flat = self.flat(self.block(observed()))
        self.assertIn("Disk not observed yet;", flat)
        self.assertIn("Observed services 3 minutes ago (00:00 UTC); Cluster overview", flat)

    def test_neither_record_is_one_row(self):
        for width in (80, 44):
            text = self.block(None, None, width=width)
            self.assertEqual(self.flat(text), "Running not observed yet; Cluster overview (checks nodes now) "
                                              "probes the nodes and saves what it sees")
            self.assertTrue(text.startswith("Running   not observed yet;"))

    def test_an_unreadable_record_says_so_and_names_the_choice_that_replaces_it(self):
        flat = self.flat(self.block(None, None, unreadable={"inventory", "budget"}))
        self.assertIn("Services saved inventory unreadable; Cluster overview (checks nodes now) replaces it", flat)
        self.assertIn("Disk saved budget unreadable; Cluster overview (checks nodes now) or Model files and disk "
                      "space (checks nodes now) replaces it", flat)
        self.assertNotIn("Observed", flat)
        # An inventory the projection rejects reads as unreadable too.
        duplicate = observed()
        duplicate["services"].append(copy.deepcopy(duplicate["services"][0]))
        self.assertIn("saved inventory unreadable", self.flat(self.block(duplicate, budget_record())))

    def test_a_long_model_name_is_cut_never_broken(self):
        entries = [{**ENTRIES[0], "model_id": "example/" + "long-model-" * 8 + "end"}]
        for width in (80, 44):
            text = self.block(observed(), entries=entries, width=width)
            first = text.splitlines()[1]
            self.assertTrue(first.startswith("  Services  example/"), first)
            self.assertIn("…", first)
            self.assertTrue(first.endswith("end") or "end ·" in first, first)

    def test_styles_strip_back_to_the_plain_text(self):
        budget = budget_record()
        for width in (80, 44):
            with patch.dict(os.environ, {"PULSAR_COLOR": "never"}):
                plain = saved.home_block(observed(), budget, ENTRIES, width=width, now=NOW)
                lone = saved.home_block(None, None, ENTRIES, width=width, now=NOW)
            with patch.dict(os.environ, {"PULSAR_COLOR": "always"}):
                os.environ.pop("NO_COLOR", None)
                styled = saved.home_block(observed(), budget, ENTRIES, width=width, now=NOW)
                lone_styled = saved.home_block(None, None, ENTRIES, width=width, now=NOW)
            self.assertNotIn("\x1b", plain)
            self.assertEqual(ANSI.sub("", styled), plain)
            self.assertEqual(ANSI.sub("", lone_styled), lone)
            self.assertIn("\x1b[1mCluster", styled)
            self.assertRegex(styled, r"\A\x1b\[1;[0-9;]+mRunning\x1b\[0m\n")
            self.assertNotIn("**", plain + lone)


if __name__ == "__main__":
    unittest.main()
