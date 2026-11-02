"""Bounded model-file discovery and inventory projection; synthetic files only."""
import hashlib
import io
import json
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from model_library.file_discovery import inspect_files
from model_library.file_inventory import combine, context, placement, render, _table
from model_library.local import home_record, prepared_record
from model_library.state import Store, shared_view
from release_spec import build_snapshot_manifest
from scripts.terminal_format import TerminalWriter


class FileInventory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = b"synthetic inventory weights"
        self.data = data
        self.manifest = build_snapshot_manifest(model_id="fixture/model", snapshot_revision="a" * 40,
            files=[{"path": "weights.bin", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}])
        self.home_root = self.root / "homes"
        self.view_root = self.root / "views"

    def hub(self, root=None, name=None):
        path = (root or self.home_root / "pulsar-homes") / (name or self.manifest["manifest_id"])
        snapshot = path / "snapshots" / self.manifest["snapshot_revision"]
        snapshot.mkdir(parents=True)
        (snapshot / "weights.bin").write_bytes(self.data)
        (path / "manifest.json").write_text(json.dumps(self.manifest))
        return path

    def home(self, path, node="n0"):
        return home_record(self.manifest, node, path, {"snapshot_manifest_id": self.manifest["manifest_id"]})

    def scan(self, **fields):
        return inspect_files({"node_id": "n0", **fields}, self.home_root, self.view_root)

    def saved(self, **fields):
        return {"homes": [], "views": [], "manifests": {self.manifest["manifest_id"]: self.manifest},
                "archives": {}, "specs": {}, "home_roots": [], "issues": [], **fields}

    def observation(self, identity="n0", files=None, containers=()):
        return {"node_id": identity, "hostname": "fixture-" + identity, "files": files,
                "containers": None if containers is None else list(containers),
                "error": "" if files is not None else "node unavailable"}

    def test_uncataloged_discovery_does_not_register_hash_or_create_state(self):
        hub = self.hub()
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch("model_library.integrity.verify_tree", side_effect=AssertionError("inventory must not hash")):
            files = self.scan()
        self.assertEqual(len(files["locations"]), 1)
        item = files["locations"][0]
        self.assertEqual((item["hub_path"], item["state"], item["registered_home"]), (str(hub), "present", False))
        self.assertFalse(self.view_root.exists())
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        document = combine(self.saved(), [self.observation(files=files)])
        self.assertEqual(document["snapshots"][0]["cleanup_spec_ids"], [])

    def test_empty_missing_roots_and_unreadable_roots_are_distinct(self):
        empty = self.scan()
        self.assertEqual({scope["status"] for scope in empty["scopes"]}, {"not-found"})
        self.assertEqual(empty["locations"], [])
        self.assertFalse(self.home_root.exists())
        self.home_root.mkdir()
        self.view_root.symlink_to(self.home_root, target_is_directory=True)
        scanned = self.scan()
        self.assertEqual(next(scope["status"] for scope in scanned["scopes"] if scope["kind"] == "prepared"), "unknown")

    def test_internal_node_state_symlink_is_never_read(self):
        hub = self.hub()
        self.view_root.mkdir()
        outside = self.root / "other-state"
        outside.mkdir()
        (self.view_root / ".pulsar-node").symlink_to(outside, target_is_directory=True)
        with patch.object(Store, "views", side_effect=AssertionError("must reject link before Store")):
            files = self.scan()
        self.assertEqual(files["locations"][0]["hub_path"], str(hub))
        self.assertFalse(files["bindings_observed"])
        self.assertIn("symlink", files["issues"][0])
        document = combine(self.saved(), [self.observation(files=files)])
        self.assertIn("retention unknown", placement(document["snapshots"][0], document["nodes"][0]))

    def test_unknown_hubs_do_not_hide_valid_neighbors_or_follow_symlinks(self):
        hub = self.hub()
        bad = self.home_root / "pulsar-homes" / ("b" * 64)
        bad.mkdir()
        (bad / "manifest.json").write_text("{malformed")
        linked = self.home_root / "pulsar-homes" / ("c" * 64)
        linked.symlink_to(hub, target_is_directory=True)
        files = self.scan()
        by_path = {item["hub_path"]: item for item in files["locations"]}
        self.assertEqual(by_path[str(hub)]["state"], "present")
        self.assertEqual(by_path[str(bad)]["state"], "unknown")
        self.assertEqual(len(files["unidentified"]), 1)
        self.assertTrue(any(scope["status"] == "partial" for scope in files["scopes"]))

    def test_same_size_changed_contents_are_not_claimed_hash_verified(self):
        hub = self.hub()
        (hub / "snapshots" / self.manifest["snapshot_revision"] / "weights.bin").write_bytes(b"x" * len(self.data))
        self.assertEqual(self.scan()["locations"][0]["state"], "present")
        document = combine(self.saved(), [self.observation(files=self.scan())])
        output = io.StringIO()
        render(document, writer=TerminalWriter(width=44, stream=output))
        self.assertIn("without hashing contents", " ".join(output.getvalue().split()))

    def test_declared_missing_file_leaves_location_unknown_not_absent(self):
        hub = self.hub()
        (hub / "snapshots" / self.manifest["snapshot_revision"] / "weights.bin").unlink()
        item = self.scan()["locations"][0]
        self.assertEqual(item["state"], "unknown")
        self.assertIn("weights.bin", item["error"])

    def test_recorded_home_outside_default_root_and_configured_overlay_root_are_scanned(self):
        old_root = self.root / "previous-home-root"
        hub = self.hub(old_root / "pulsar-homes")
        home = self.home(hub)
        files = self.scan(homes=[home])
        self.assertEqual(files["locations"][0]["state"], "present")
        self.assertTrue(files["locations"][0]["registered_home"])
        files = self.scan(home_roots=[str(old_root)])
        self.assertEqual(files["locations"][0]["hub_path"], str(hub))
        self.assertFalse(files["locations"][0]["registered_home"])

    def test_home_binding_and_shared_prepared_bindings_do_not_duplicate_files(self):
        hub = self.hub()
        home = self.home(hub)
        home_view = prepared_record(home, spec_id="1" * 64, topology_id="t", rank=0, is_home_view=True)
        working = self.hub(self.view_root, "2" * 64)
        first = shared_view(prepared_record(self.home(working), spec_id="2" * 64,
                                            topology_id="t", rank=0, pinned=True))
        second = {**first, "spec_id": "3" * 64, "pinned": False}
        files = self.scan(homes=[home], views=[home_view, first, second])
        document = combine(self.saved(homes=[home], views=[home_view, first, second]), [self.observation(files=files)])
        entry = document["snapshots"][0]
        self.assertEqual(len(entry["locations"]), 2)
        prepared = next(item for item in entry["locations"] if item["kind"] == "prepared")
        self.assertTrue(prepared["shared"])
        self.assertTrue(prepared["pinned"])
        self.assertEqual(entry["payload_bytes"], len(self.data))

    def test_three_nodes_keep_files_when_another_node_or_docker_is_unavailable(self):
        hub = self.hub()
        known = self.home(hub, node="n2")
        document = combine(self.saved(homes=[known]), [
            self.observation(files=self.scan(), containers=None),
            self.observation("n1", {"observed_at": "2026-09-05T00:00:00Z", "locations": [], "scopes": [],
                                   "disks": [], "issues": [], "unidentified": [], "bindings_observed": True}),
            self.observation("n2")])
        self.assertEqual(len(document["nodes"]), 3)
        entry = document["snapshots"][0]
        self.assertIn("container use unknown", placement(entry, document["nodes"][0]))
        self.assertEqual(placement(entry, document["nodes"][1]), "not seen in scanned roots")
        self.assertIn("unknown", placement(entry, document["nodes"][2]))
        self.assertEqual(entry["locations"][0]["state"], "present")

    def test_stopped_container_and_pins_remain_visible_without_reclaim_estimates(self):
        hub = self.hub()
        home = self.home(hub)
        binding = prepared_record(home, spec_id="1" * 64, topology_id="t", rank=0, pinned=True, is_home_view=True)
        container = {"id": "fixture-stopped", "running": False, "mounts": [str(hub.parent)]}
        saved = self.saved(homes=[home], views=[binding], specs={self.manifest["manifest_id"]: [
            {"spec_id": binding["spec_id"], "model_id": self.manifest["model_id"], "snapshot": "target"}]})
        document = combine(saved, [self.observation(files=self.scan(homes=[home], views=[binding]), containers=[container])])
        entry = document["snapshots"][0]
        self.assertEqual(entry["cleanup_spec_ids"], [binding["spec_id"]])
        self.assertIn("container referenced", placement(entry, document["nodes"][0]))
        self.assertIn("pinned", placement(entry, document["nodes"][0]))
        self.assertNotIn("reclaimable_bytes", entry)

    def test_unreachable_node_retention_is_saved_not_a_current_claim(self):
        hub = self.hub()
        home = self.home(hub)
        binding = prepared_record(home, spec_id="1" * 64, topology_id="t",
                                  rank=0, pinned=True, is_home_view=True)
        document = combine(self.saved(homes=[home], views=[binding]), [self.observation(containers=None)])
        entry, node = document["snapshots"][0], document["nodes"][0]
        self.assertEqual(entry["locations"][0]["state"], "unknown")
        self.assertFalse(entry["locations"][0]["bindings_observed"])
        words = placement(entry, node)
        self.assertIn("home unknown", words)
        self.assertIn("retention unknown", words)
        self.assertIn("saved pin", words)
        self.assertNotIn("pinned", words)
        compact = placement(entry, node, compact=True).split()
        self.assertIn("pins?", compact)
        self.assertNotIn("pin", compact)
        self.assertNotIn("shared", compact)

    def test_empty_context_does_not_create_controller_state(self):
        repo = self.root / "repo"; repo.mkdir()
        store = Store(self.root / "controller")
        result = context(repo, store, repo / ".pulsar-overlay.json")
        self.assertEqual(result["homes"], [])
        self.assertFalse(store.root.exists())

    def test_styles_and_no_color_preserve_plain_inventory_at_narrow_and_table_widths(self):
        self.hub()
        document = combine(self.saved(), [self.observation(files=self.scan()), self.observation("n1")])
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        for width in (32, 44, 100):
            outputs = {}
            for mode in ("plain", "styled", "no-color"):
                environment = {"PULSAR_COLOR": "never" if mode == "plain" else "always"}
                if mode == "no-color":
                    environment["NO_COLOR"] = "1"
                with patch.dict(os.environ, environment, clear=True):
                    output = io.StringIO()
                    render(document, writer=TerminalWriter(width=width, stream=output))
                    outputs[mode] = output.getvalue()
            self.assertNotIn("\x1b", outputs["plain"])
            self.assertIn("\x1b[", outputs["styled"])
            self.assertEqual(ansi.sub("", outputs["styled"]), outputs["plain"])
            self.assertEqual(ansi.sub("", outputs["no-color"]), outputs["plain"])
            codes = {code for match in ansi.findall(outputs["no-color"])
                     for code in match[2:-1].split(";")}
            self.assertLessEqual(codes, {"0", "1", "4"})

    def test_wide_table_and_narrow_view_preserve_timestamp_scope_and_unknowns(self):
        self.hub()
        document = combine(self.saved(), [self.observation(files=self.scan()), self.observation("n1"),
                                         self.observation("n2")])
        self.assertTrue(_table(document["snapshots"], document["nodes"], 100))
        for width in (32, 44, 100):
            output = io.StringIO()
            render(document, writer=TerminalWriter(width=width, stream=output))
            text = output.getvalue()
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
            flat = " ".join(text.split())
            self.assertIn("Refresh checks again", flat)
            self.assertIn("fixture-n0", flat)
            self.assertIn("fixture-n1", flat)
            self.assertIn("fixture-n2", flat)
            self.assertIn("not allocated", flat)
