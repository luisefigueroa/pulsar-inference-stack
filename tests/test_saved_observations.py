"""The saved service inventory and storage budget, and the home menu's Running block."""
import copy
from datetime import datetime, timedelta, timezone
import io
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

    def test_cluster_changes_are_stamped_privately_and_validated(self):
        path = self.root / "state" / "cluster-changes.json"
        self.assertIsNone(saved.load_changes(path))
        body = {"topology_id": "c" * 64, "result": "changed", "issues": [], "found": [],
                "missing": [{"hostname": "fixture-rank-1", "node_id": "fixture-node-1", "position": 1,
                             "answered": False}], "changed": [], "untrusted": []}
        self.assertTrue(saved.save_changes(body, path, clock=lambda: "2026-11-09T00:00:00Z"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(saved.load_changes(path), {**body, "schema_version": 1, "kind": "pulsar-cluster-changes",
                                                    "observed_at": "2026-11-09T00:00:00Z"})
        self.assertTrue((self.root / "state" / "cluster-changes.json.lock").exists())
        for broken in ({**body, "result": "maybe"}, {**body, "topology_id": ""},
                       {key: value for key, value in body.items() if key != "untrusted"}, [], None):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    saved.save_changes(broken, path)
        # The time is stamped at write and cannot come from the caller.
        saved.save_changes({**body, "observed_at": "2020-01-01T00:00:00Z"}, path, clock=lambda: "2026-11-09T00:00:01Z")
        self.assertEqual(saved.load_changes(path)["observed_at"], "2026-11-09T00:00:01Z")
        with patch.dict(os.environ, {"PULSAR_CLUSTER_CHANGES_FILE": str(path)}):
            self.assertEqual(saved.record_path(self.root, "cluster-changes"), path)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(saved.record_path(self.root, "cluster-changes"),
                             self.root / ".pulsar" / "cluster-changes.json")

    def test_an_unchanged_record_is_carried_to_the_enrolled_topology_id(self):
        path = self.root / "cluster-changes.json"
        self.assertFalse(saved.carry_changes(path, "a" * 64, "b" * 64))
        self.assertFalse(path.exists())
        body = {"topology_id": "a" * 64, "result": "unchanged", "issues": [], "found": [], "missing": [],
                "changed": [], "untrusted": [{"address": "printer.local", "possible_reinstall": False}]}
        saved.save_changes(body, path, clock=lambda: "2026-11-09T00:00:00Z")
        self.assertTrue(saved.carry_changes(path, "a" * 64, "b" * 64))
        self.assertEqual(saved.load_changes(path), {**body, "topology_id": "b" * 64, "schema_version": 1,
                                                    "kind": "pulsar-cluster-changes",
                                                    "observed_at": "2026-11-09T00:00:00Z"})
        # Another ID, or any other result, is left exactly as it is.
        before = path.read_bytes()
        self.assertFalse(saved.carry_changes(path, "a" * 64, "c" * 64))
        saved.save_changes({**body, "result": "changed", "topology_id": "b" * 64}, path)
        before = path.read_bytes()
        self.assertFalse(saved.carry_changes(path, "b" * 64, "c" * 64))
        self.assertEqual(path.read_bytes(), before)

    def missing_body(self, topology="a" * 64, **extra):
        return {"topology_id": topology, "result": "changed", "issues": [], "found": [],
                "missing": [{"hostname": "fixture-rank-2", "node_id": "fixture-node-2", "position": 2,
                             "answered": False}], "changed": [], "untrusted": [], **extra}

    def test_a_kept_answer_stays_until_a_discovery_sees_other_changes(self):
        path = self.root / "cluster-changes.json"
        with self.assertRaisesRegex(ValueError, "no member that did not answer"):
            saved.keep_changes(path)
        saved.save_changes({**self.missing_body(), "result": "unchanged", "missing": []}, path)
        with self.assertRaisesRegex(ValueError, "no member that did not answer"):
            saved.keep_changes(path)
        saved.save_changes(self.missing_body(), path, clock=lambda: "2026-11-09T00:00:00Z")
        record = saved.keep_changes(path, clock=lambda: "2026-11-09T00:01:00Z")
        self.assertEqual(record["kept"]["missing"], ["fixture-node-2"])
        self.assertEqual(record["kept"]["kept_at"], "2026-11-09T00:01:00Z")
        self.assertEqual(saved.load_changes(path), record)
        # The same changes seen again later keep the answer, with a new observed_at.
        saved.save_changes(self.missing_body(), path, clock=lambda: "2026-11-09T01:00:00Z")
        again = saved.load_changes(path)
        self.assertEqual((again["observed_at"], again["kept"]), ("2026-11-09T01:00:00Z", record["kept"]))
        # A caller cannot write a kept answer or a check itself.
        saved.save_changes(self.missing_body(kept={"bogus": True}, check={"bogus": True}), path)
        self.assertEqual(saved.load_changes(path)["kept"], record["kept"])
        self.assertNotIn("check", saved.load_changes(path))
        # Other changes, or another membership, drop it: the question is asked again.
        found = {"hostname": "fixture-new", "node_id": "fixture-node-9", "ssh_host": "fixture-new.local",
                 "possible_reinstall": False}
        for body in (self.missing_body(found=[found]), self.missing_body(topology="b" * 64)):
            with self.subTest(topology=body["topology_id"][:1], found=bool(body["found"])):
                saved.save_changes(self.missing_body(), path)
                saved.keep_changes(path)
                saved.save_changes(body, path)
                self.assertNotIn("kept", saved.load_changes(path))

    def check_body(self, topology="a" * 64):
        return {"topology_id": topology, "links": "verified", "identities": "verified",
                "nodes": [{"hostname": "fixture-rank-0", "node_id": "fixture-node-0", "answered": True,
                           "failures": [], "warnings": ["hf CLI not found"]},
                          {"hostname": "fixture-rank-1", "node_id": "fixture-node-1", "answered": False,
                           "failures": [], "warnings": []}]}

    def test_a_cluster_check_is_kept_beside_the_changes_of_its_membership(self):
        path = self.root / "cluster-changes.json"
        with self.assertRaisesRegex(ValueError, "another membership"):
            saved.save_check(self.check_body(), path)
        saved.save_changes(self.missing_body(), path, clock=lambda: "2026-11-09T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "another membership"):
            saved.save_check(self.check_body("b" * 64), path)
        self.assertTrue(saved.save_check(self.check_body(), path, clock=lambda: "2026-11-09T00:00:30Z"))
        check = saved.load_changes(path)["check"]
        self.assertEqual(check["checked_at"], "2026-11-09T00:00:30Z")
        self.assertNotIn("topology_id", check)
        self.assertEqual([node["answered"] for node in check["nodes"]], [True, False])
        # A later discovery of the same membership keeps it; a save of another drops it.
        saved.save_changes(self.missing_body(), path, clock=lambda: "2026-11-09T02:00:00Z")
        self.assertEqual(saved.load_changes(path)["check"], check)
        saved.save_changes(self.missing_body("b" * 64), path)
        self.assertNotIn("check", saved.load_changes(path))
        for broken in ({**self.check_body(), "links": "maybe"},
                       {**self.check_body(), "nodes": [{"hostname": "", "answered": True, "failures": [],
                                                        "warnings": []}]},
                       {**self.check_body(), "nodes": [{"hostname": "x", "answered": "yes", "failures": [],
                                                        "warnings": []}]}, []):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                saved.save_changes(self.missing_body(), path)
                saved.save_check(broken, path)
        # The command line saves it, and a mismatch is one warning that never fails its caller.
        saved.save_changes(self.missing_body(), path)
        env = {**os.environ, "PULSAR_CLUSTER_CHANGES_FILE": str(path), "PYTHONPATH": str(ROOT)}
        result = subprocess.run([sys.executable, "-m", "scripts.saved_observations", "save-cluster-check"],
                                input=json.dumps(self.check_body("b" * 64)), env=env, text=True,
                                capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0)
        self.assertIn("the cluster check was not saved", result.stderr)
        result = subprocess.run([sys.executable, "-m", "scripts.saved_observations", "keep-cluster-changes"],
                                env=env, text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(saved.load_changes(path)["kept"]["missing"], ["fixture-node-2"])

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

    def home(self, *args, **extra):
        """`home`'s change key and its block, from its first line and the rest."""
        result = self.run_command("home", *args, stdin=json.dumps({"entries": ENTRIES}), **extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        first, block = result.stdout.split("\n", 1)
        self.assertTrue(first.startswith("key\t"), first)
        return first[len("key\t"):], block

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
        key, block = self.home(**overrides)
        self.assertEqual(key.split()[:2], ["2026-09-05T00:00:00Z", "last-seen"])
        self.assertIn("example/model · last seen running on", block)

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
        key, block = self.home("--width", "80")
        self.assertEqual(key, "none none")
        self.assertEqual(block.splitlines()[0][:26], "Running   not observed yet")
        (self.root / ".pulsar").mkdir()
        (self.root / ".pulsar/service-inventory.json").write_text("{broken")
        (self.root / ".pulsar/storage-budget.json").write_text(json.dumps(budget_record()))
        key, block = self.home("--width", "80")
        text = " ".join(block.split())
        self.assertIn("Services saved inventory unreadable; Cluster overview (checks nodes now) replaces it", text)
        self.assertIn("Disk node-1 can still copy 150 GiB · 300 GiB free", text)
        # An unreadable record keys as none, so the block prints again once it is replaced.
        self.assertEqual(key, "none 2026-09-04T22:00:00Z")
        # A catalog that cannot be read prints nothing, not even the key.
        result = self.run_command("home", stdin="not json")
        self.assertEqual((result.returncode, result.stdout), (2, ""))

    def test_the_key_comes_from_the_same_single_read_as_the_block(self):
        (self.root / ".pulsar").mkdir()
        inventory, budget = self.root / ".pulsar/service-inventory.json", self.root / ".pulsar/storage-budget.json"
        inventory.write_text(json.dumps(observed()))
        budget.write_text(json.dumps(budget_record()))
        reads = []

        def counted(loader):
            def read(path):
                reads.append(Path(path).name)
                return loader(path)
            return read

        stdout = io.StringIO()
        with patch.object(saved, "load_inventory", counted(saved.load_inventory)), \
                patch.object(saved, "load_budget", counted(saved.load_budget)), \
                patch.dict(os.environ, {saved.INVENTORY_ENV: str(inventory), saved.BUDGET_ENV: str(budget)}), \
                patch("sys.stdin", io.StringIO(json.dumps({"entries": ENTRIES}))), patch("sys.stdout", stdout):
            code = saved.main(["home", "--now", "2026-09-05T00:11:00Z", "--width", "80",
                               "--repo-root", str(self.root)])
        self.assertEqual(code, 0)
        # Exactly one read of each record gives both the key and the block.
        self.assertEqual(sorted(reads), ["service-inventory.json", "storage-budget.json"])
        first, block = stdout.getvalue().split("\n", 1)
        at = datetime(2026, 9, 5, 0, 11, tzinfo=timezone.utc)
        self.assertEqual(first, "key\t" + saved.change_key(saved.load_inventory(inventory),
                                                          saved.load_budget(budget), at))
        self.assertEqual(first, "key\t2026-09-05T00:00:00Z last-seen 2026-09-04T22:00:00Z")
        self.assertIn("last seen running on fixture-remote", " ".join(block.split()))

    def test_a_save_waits_for_the_record_lock(self):
        (self.root / ".pulsar").mkdir()
        for command, name, document in (("save-inventory", "service-inventory.json", observed()),
                                        ("save-budget", "storage-budget.json", [budget_node("node-1")])):
            with self.subTest(command=command):
                path = self.root / ".pulsar" / name
                with saved.record_lock(path):
                    process = subprocess.Popen(
                        [sys.executable, "-m", "scripts.saved_observations", command, "--repo-root", str(self.root)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
                        cwd=ROOT, text=True)
                    process.stdin.write(json.dumps(document))
                    process.stdin.close()
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.wait(timeout=1.5)
                    self.assertFalse(path.exists())
                process.wait(timeout=60)
                with process.stdout, process.stderr:
                    output = (process.returncode, process.stdout.read(), process.stderr.read())
                self.assertEqual(output, (0, "", ""))
                self.assertTrue(path.exists())
                lock = path.with_name(name + ".lock")
                self.assertTrue(stat.S_ISREG(lock.lstat().st_mode))
                self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_a_lock_that_is_not_a_regular_file_refuses_the_save(self):
        (self.root / ".pulsar").mkdir()
        (self.root / ".pulsar/service-inventory.json.lock").symlink_to(self.root / "elsewhere")
        result = self.run_command("save-inventory", stdin=json.dumps(observed()))
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertRegex(result.stderr, r"\Awarning: the service inventory was not saved to \S+: ")
        self.assertFalse((self.root / ".pulsar/service-inventory.json").exists())
        self.assertFalse((self.root / "elsewhere").exists())

    def test_the_key_and_the_block_agree_on_last_seen_at_one_instant(self):
        (self.root / ".pulsar").mkdir()
        (self.root / ".pulsar/service-inventory.json").write_text(json.dumps(observed()))
        for minutes, state in ((9, "fresh"), (11, "last-seen")):
            with self.subTest(minutes=minutes):
                key, block = self.home("--now", f"2026-09-05T00:{minutes:02d}:00Z", "--width", "80")
                block = " ".join(block.split())
                self.assertEqual(key, f"2026-09-05T00:00:00Z {state} none")
                self.assertEqual("last seen running on fixture-remote" in block, state == "last-seen", block)
                self.assertEqual("last seen, not current" in block, state == "last-seen", block)
                self.assertIn(f"services {minutes} minutes ago (00:00 UTC)", block)
        for value in ("2026-09-05T00:09:00", "yesterday"):
            with self.subTest(value=value):
                result = self.run_command("home", "--now", value, stdin=json.dumps({"entries": ENTRIES}))
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertIn("2026-11-05T22:00:00Z", result.stderr)


class RunningBlock(unittest.TestCase):
    def block(self, inventory, budget=None, *, width=80, now=NOW, entries=ENTRIES, **options):
        text = saved.home_block(inventory, budget, entries, width=width, now=now, **options)
        for line in text.splitlines():
            self.assertLessEqual(len(line), width, line)
            # Wraps fall between parts, never between a number and its unit
            # or after a separator.
            self.assertNotRegex(line, r"(\bport|\bof|·)$", line)
            self.assertNotRegex(line, r"^\s+(GiB|TiB|MiB|KiB|UTC\))", line)
            self.assertNotIn("\u00a0", line)
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

    def test_a_service_with_execution_overrides_reads_modified_recipe(self):
        data = observed()
        data["services"][0]["matches_selected_spec"] = False
        data["services"].append(outside())
        data["services"][1]["matches_selected_spec"] = False
        for width in (80, 44):
            with self.subTest(width=width):
                flat = self.flat(self.block(data, width=width))
                self.assertIn("example/model · running on fixture-remote · modified recipe · port 8000", flat)
                # A service outside the catalog has no selected spec to differ from.
                self.assertIn("experiment-model · running on fixture-local · port 8001 · outside the catalog", flat)
                self.assertEqual(flat.count("modified recipe"), 1)
        data["services"][0]["matches_selected_spec"] = True
        self.assertNotIn("modified recipe", self.flat(self.block(data)))

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
