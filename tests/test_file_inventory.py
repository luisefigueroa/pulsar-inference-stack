"""Bounded model-file discovery and inventory projection; synthetic files only."""
import contextlib
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
from model_library.file_inventory import (FOLDER_STATES, UNFINISHED, UNFINISHED_PREPARATION, _elide, _labels, _table, combine, context,
                                          OUTSIDE_MEMBERSHIP, main, placement, purge_estimate, purge_line, read_budget,
                                          render)
from model_library.local import home_record, prepared_record
from model_library.state import Store, now, shared_view
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

    def test_only_a_recorded_preparation_folder_names_its_spec(self):
        prepared = self.view_root / (".pending-" + "2" * 32)
        download = self.home_root / "pulsar-homes" / (".pending-" + "3" * 32)
        prepared.mkdir(parents=True)
        download.mkdir(parents=True)
        Store(self.view_root / ".pulsar-node").put("transactions", "d" * 64, {
            "stage": str(prepared), "node_id": "n0", "spec_id": "fixture-spec", "kind": "pulsar-preparation-staging"})
        Store(self.view_root / ".pulsar-node").put("transactions", "e" * 64, {
            "stage": str(download), "node_id": "other", "spec_id": "elsewhere"})
        by_path = {item["path"]: item for item in self.scan()["unidentified"]}
        self.assertEqual(by_path[str(prepared)]["preparation_spec_id"], "fixture-spec")
        self.assertNotIn("preparation_spec_id", by_path[str(download)])
        self.assertEqual({item["kind"] for item in by_path.values()}, {"incomplete"})

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
        self.assertIn("pins unknown", placement(document["snapshots"][0], document["nodes"][0]))

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
        self.assertIn("Names and sizes were compared, not file contents.", " ".join(output.getvalue().split()))

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
        self.assertEqual(placement(entry, document["nodes"][1]), "none in Pulsar's folders on this node")
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
        self.assertIn("in use by a container", placement(entry, document["nodes"][0]))
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
        self.assertIn("pins unknown", words)
        self.assertIn("pinned (saved record)", words)
        self.assertNotIn("pinned,", words)
        cell = placement(entry, node, compact=True).split(", ")
        self.assertEqual(cell[:2], ["home unknown", "pins unknown"])
        self.assertNotIn("pinned", cell)
        self.assertNotIn("shared", cell)
        codes = placement(entry, node, compact=True, codes=True).split()
        self.assertIn("pins?", codes)
        self.assertNotIn("pin", codes)
        self.assertNotIn("shared", codes)

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
        lines, key = _table(document["snapshots"], document["nodes"], 100)
        self.assertTrue(lines and key)
        for width in (32, 44, 100):
            output = io.StringIO()
            render(document, writer=TerminalWriter(width=width, stream=output))
            text = output.getvalue()
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
            flat = " ".join(text.split())
            self.assertIn("Checked less than a minute ago (3 nodes)", flat)
            self.assertIn("fixture-n0", flat)
            self.assertIn("fixture-n1", flat)
            self.assertIn("fixture-n2", flat)
            self.assertIn("not what deleting would free", flat)


class Page(unittest.TestCase):
    """The overview and detail pages, rendered from hand-built inventories without a filesystem."""
    ANSI = re.compile(r"\x1b\[[0-9;]*m")

    @staticmethod
    def location(node, kind="home", **fields):
        return {"node_id": node, "kind": kind, "hub_path": "/fixture/" + kind, "state": "present",
                "registered_home": True, "bindings": [], "bindings_observed": True, "pinned": False,
                "shared": False, "container_references": [], **fields}

    @staticmethod
    def entry(model, commit, locations, size=1024**3):
        return {"snapshot_manifest_id": hashlib.sha256(model.encode()).hexdigest(), "model_id": model,
                "model_commit": commit, "payload_bytes": size, "locations": locations, "archive": None,
                "catalog_specs": [], "cleanup_spec_ids": []}

    @staticmethod
    def node(identity, **fields):
        scopes = [{"path": "/fixture/homes", "kind": "home", "status": "observed"},
                  {"path": "/fixture/views", "kind": "prepared", "status": "observed"}]
        disks = [{"path": "/fixture/homes", "available": 2 * 1024**4, "total": 4 * 1024**4},
                 {"path": "/fixture/views", "available": 2 * 1024**4, "total": 4 * 1024**4}]
        return {"node_id": identity, "hostname": "alpha-" + identity, "observed_at": now(), "status": "observed",
                "error": "", "scopes": scopes, "disks": disks, "issues": [], "unidentified": [],
                "container_error": "", **fields}

    @staticmethod
    def document(nodes, snapshots, **fields):
        return {"schema_version": 1, "kind": "pulsar-model-file-inventory", "observed_at": now(),
                "nodes": nodes, "snapshots": snapshots, "issues": [], **fields}

    def shared_model(self):
        """One model: a shared home in use on node 1, a shared copy on node 2, nothing on node 3."""
        return self.document(
            [self.node("1"), self.node("2"), self.node("3")],
            [self.entry("fixture/Shared-Model", "a" * 40, [
                self.location("1", shared=True, container_references=[{"id": "c"}]),
                self.location("2", "prepared", shared=True)])])

    def two_node_model(self):
        return self.document(
            [self.node("1"), self.node("2")],
            [self.entry("fixture/Shared-Model", "a" * 40, [
                self.location("1", shared=True, container_references=[{"id": "c"}]),
                self.location("2", "prepared", shared=True)])])

    def text(self, document, width, **options):
        output = io.StringIO()
        with patch.dict(os.environ, {"PULSAR_COLOR": "never"}, clear=True):
            render(document, writer=TerminalWriter(width=width, stream=output), **options)
        return output.getvalue()

    def test_words_fill_the_cells_at_100_columns_and_the_key_explains_only_what_is_shown(self):
        text = self.text(self.shared_model(), 100)
        lines = text.splitlines()
        header = next(line for line in lines if line.startswith("Model snapshot"))
        self.assertEqual(header.split(), ["Model", "snapshot", "Size", "per", "copy", "alpha-1", "alpha-2", "alpha-3"])
        row = next(line for line in lines if line.startswith("Shared-Model @aaaaaaaa"))
        self.assertIn("1.00 GiB", row)
        self.assertIn("home, shared, in use", row)
        self.assertIn("copy, shared", row)
        self.assertTrue(row.rstrip().endswith("—"), row)
        self.assertNotIn("fixture/", row)
        flat = " ".join(text.split())
        self.assertIn("— none on this node · shared: more than one spec uses it · "
                      "in use: a container mounts it, even stopped", flat)
        for absent in ("pinned:", "pins unknown", "H home", "Payload"):
            self.assertNotIn(absent, flat)
        self.assertIn("Size per copy is the model's listed size. Shared copies are stored once, "
                      "so it is not what deleting would free.", flat)

    def test_short_codes_replace_the_words_at_60_columns_with_a_key(self):
        document = self.two_node_model()
        # The words need 67 columns here, the codes 56.
        wide, _ = _table(document["snapshots"], document["nodes"], 67)
        self.assertIn("home, shared, in use", wide[1])
        text = self.text(document, 60)
        lines = text.splitlines()
        self.assertTrue(all(len(line) <= 60 for line in lines), text)
        row = next(line for line in lines if "@aaaaaaaa" in line)
        self.assertIn("H shared used", row)
        self.assertIn("P shared", row)
        self.assertNotIn("home, shared", text)
        flat = " ".join(text.split())
        self.assertIn("H home; P copy; shared: more than one spec uses it; used: a container mounts it, even stopped",
                      flat)

    def test_stacked_entries_remain_the_narrowest_layout(self):
        text = self.text(self.two_node_model(), 44)
        self.assertTrue(all(len(line) <= 44 for line in text.splitlines()), text)
        self.assertNotIn("Model snapshot", text)
        flat = " ".join(text.split())
        self.assertIn("fixture/Shared-Model @ aaaaaaaaaaaa", flat)
        self.assertIn("Size per copy 1.00 GiB", flat)
        self.assertIn("alpha-1 home, shared, in use by a container", flat)
        self.assertIn("alpha-2 prepared copy, shared", flat)

    def test_long_names_keep_their_end_and_commit_so_similar_rows_stay_distinct(self):
        base = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"
        entries = [self.entry(base, "c" * 40, [self.location("1", shared=True)], 21 * 1024**3),
                   self.entry(base + "-DSpark", "8" * 40, [self.location("1", shared=True)], 1024**3)]
        document = self.document([self.node("1")], entries)
        lines, _ = _table(entries, document["nodes"], 58)
        first = [line[:29].rstrip() for line in lines[1:]]
        self.assertEqual(len(set(first)), 2, lines)
        self.assertIn("-DSpark @88888888", first[1])
        self.assertTrue(first[0].endswith("NVFP4 @cccccccc"), first)
        self.assertTrue(all(len(line) <= 58 for line in lines), lines)
        self.assertEqual(_elide("short @12345678", 30), "short @12345678")
        self.assertEqual(len(_elide(base + " @12345678", 24)), 24)
        # The catalog's recipe list cuts model names by the same rule.
        from scripts.terminal_format import cut_middle
        self.assertEqual(cut_middle("DeepSeek-V4.1-Flash-1017", 13), "DeepSe…h-1017")
        self.assertEqual(cut_middle("short", 13), "short")
        self.assertEqual([cut_middle("abc", width) for width in (1, 0)], ["…", ""])

    def test_the_org_is_kept_only_when_two_labels_would_read_the_same(self):
        same = [self.entry("one/Model", "a" * 40, []), self.entry("two/Model", "a" * 40, []),
                self.entry("one/Other", "a" * 40, [])]
        self.assertEqual(_labels(same), ["one/Model @aaaaaaaa", "two/Model @aaaaaaaa", "Other @aaaaaaaa"])

    def test_a_short_label_does_not_reject_a_table_with_room(self):
        entries = [self.entry("x/m", "b" * 40, [self.location("1")])]
        lines, key = _table(entries, [self.node("1")], 60)
        self.assertTrue(lines, "a 9-character label must not fail the 16-column minimum")
        self.assertEqual(key, "")

    def test_equal_free_space_prints_once_and_different_free_space_prints_per_disk(self):
        one = self.text(self.document([self.node("1")], []), 100)
        self.assertEqual(one.count("Free space"), 1)
        self.assertIn("Free space      2.00 TiB (homes and prepared copies)", one)
        views = {"path": "/fixture/views", "available": 3 * 1024**4}
        node = self.node("1")
        node["disks"][1] = views
        two = self.text(self.document([node], []), 100)
        self.assertIn("Free space      2.00 TiB (homes)", two)
        self.assertIn("Free space      3.00 TiB (prepared copies)", two)
        node["scopes"] = []
        node["disks"].append({"path": "/fixture/lost", "error": "gone"})
        lone = self.text(self.document([node], []), 100)
        self.assertIn("2.00 TiB at /fixture/homes", lone)
        self.assertIn("Free space      could not be checked at /fixture/lost: gone", lone)
        self.assertNotIn("Disk free", lone)

    def test_two_disks_with_the_same_kind_of_folder_keep_their_paths(self):
        node = self.node("1")
        node["scopes"].append({"path": "/fixture/extra-homes", "kind": "home", "status": "observed"})
        node["disks"].append({"path": "/fixture/extra-homes", "available": 10 * 1024**3, "total": 20 * 1024**3})
        node["disks"][1] = {"path": "/fixture/views", "available": 3 * 1024**4}
        text = " ".join(self.text(self.document([node], []), 100).split())
        self.assertIn("Free space 2.00 TiB (homes) at /fixture/homes", text)
        self.assertIn("Free space 10.0 GiB (homes) at /fixture/extra-homes", text)
        self.assertIn("Free space 3.00 TiB (prepared copies)", text)
        self.assertNotIn("(prepared copies) at", text)

    def test_folder_states_are_worded_for_the_reader_not_copied_from_discovery(self):
        states = ("observed", "not-found", "partial", "unknown")
        node = self.node("1", scopes=[{"path": "/fixture/" + state, "kind": "home" if state != "unknown" else "prepared",
                                       "status": state, **({"error": "denied"} if state == "unknown" else {})}
                                      for state in states])
        text = self.text(self.document([node], []), 100)
        for state in states:
            self.assertIn(FOLDER_STATES[state] + " — /fixture/" + state, text)
        self.assertIn("Homes folder", text)
        self.assertIn("Copies folder   could not be checked — /fixture/unknown", text)
        self.assertIn("Not observed    denied", text)
        for raw in ("not-found", "— observed", "Scope"):
            self.assertNotIn(raw, text.replace("/fixture/not-found", ""))

    def test_leftovers_say_what_they_are(self):
        node = self.node("1", unidentified=[
            {"path": "/fixture/homes/.pending-" + "0" * 32, "kind": "incomplete",
             "reason": "incomplete staging; not a complete snapshot"},
            {"path": "/fixture/homes/" + "1" * 64, "kind": "home", "reason": "not a regular managed directory"}])
        flat = " ".join(self.text(self.document([node], []), 100).split())
        self.assertIn("Unfinished copy /fixture/homes/.pending-" + "0" * 32 + " — " + UNFINISHED, flat)
        self.assertIn("has not finished: still running, or interrupted", flat)
        self.assertIn("Not recognized /fixture/homes/" + "1" * 64 + " — not a regular managed directory", flat)
        self.assertNotIn("Inspect only", flat)
        self.assertNotIn("incomplete staging", flat)

    def test_a_recorded_preparation_names_the_spec_whose_purge_removes_it(self):
        stage = "/fixture/views/.pending-" + "2" * 32
        node = self.node("1", unidentified=[{"path": stage, "kind": "incomplete", "reason": "incomplete staging",
                                             "preparation_spec_id": "fixture-spec"}])
        flat = " ".join(self.text(self.document([node], []), 100).split())
        self.assertIn("Unfinished copy " + stage + " — " + UNFINISHED_PREPARATION.format(spec="fixture-spec"), flat)
        self.assertIn("Purge on that spec removes it", flat)
        self.assertNotIn("no Pulsar command removes it", flat)

    def test_a_node_that_could_not_be_checked_says_so(self):
        node = self.node("1", status="unknown", error="node unavailable", observed_at=None, scopes=[], disks=[])
        flat = " ".join(self.text(self.document([node], []), 100).split())
        self.assertIn("Files could not be checked: node unavailable", flat)
        self.assertNotIn("Files checked", flat)

    def test_checked_line_gives_the_age_and_node_count(self):
        one = self.text(self.document([self.node("1")], []), 100)
        self.assertIn("Checked   less than a minute ago (1 node)", one)
        self.assertNotRegex(one, r"20\d\d-\d\d-\d\dT")
        self.assertIn("Checked   not observed", self.text(self.document([self.node("1")], [], observed_at=None), 100))

    def test_the_menu_hint_appears_only_when_a_menu_asks_for_it(self):
        hint = "Close this page, then choose a snapshot for its paths and cleanup choices."
        document = self.shared_model()
        self.assertNotIn(hint, " ".join(self.text(document, 100).split()))
        self.assertIn(hint, " ".join(self.text(document, 100, menu=True).split()))
        detail = self.text(document, 100, snapshot=document["snapshots"][0]["snapshot_manifest_id"], menu=True)
        self.assertNotIn("Close this page", detail)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "files.json"
            path.write_text(json.dumps({"ok": True, "result": document}))
            shown = {}
            for flag in ((), ("--menu",)):
                output = io.StringIO()
                with patch.dict(os.environ, {"COLUMNS": "100", "PULSAR_COLOR": "never"}), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(main(["render", "--file", str(path), *flag]), 0)
                shown[flag] = " ".join(output.getvalue().split())
        self.assertNotIn(hint, shown[()])
        self.assertIn(hint, shown[("--menu",)])

    def test_detail_page_uses_the_plain_words(self):
        document = self.shared_model()
        entry = document["snapshots"][0]
        flat = " ".join(self.text(document, 100, snapshot=entry["snapshot_manifest_id"]).split())
        self.assertIn("Size per copy 1.00 GiB", flat)
        self.assertNotIn("Payload", flat)
        self.assertIn("No catalog spec uses these files, so Pulsar has no cleanup for them. Nothing was changed.", flat)
        self.assertIn("alpha-3 none in Pulsar's folders on this node", flat)
        self.assertIn("Files checked", flat)

    def test_styles_and_no_color_leave_the_plain_pages_unchanged_in_every_layout(self):
        node = self.node("1", unidentified=[{"path": "/fixture/homes/.pending-" + "0" * 32, "kind": "incomplete",
                                             "reason": "incomplete staging"}])
        document = self.document([node, *self.shared_model()["nodes"][1:]], self.shared_model()["snapshots"])
        two = self.two_node_model()
        for width, subject in ((100, document), (60, two), (44, two)):
            outputs = {}
            for mode in ("plain", "styled", "no-color"):
                environment = {"PULSAR_COLOR": "never" if mode == "plain" else "always"}
                if mode == "no-color":
                    environment["NO_COLOR"] = "1"
                with patch.dict(os.environ, environment, clear=True):
                    output = io.StringIO()
                    render(subject, writer=TerminalWriter(width=width, stream=output), menu=True)
                    outputs[mode] = output.getvalue()
            self.assertNotIn("\x1b", outputs["plain"])
            self.assertIn("\x1b[", outputs["styled"])
            self.assertEqual(self.ANSI.sub("", outputs["styled"]), outputs["plain"], width)
            self.assertEqual(self.ANSI.sub("", outputs["no-color"]), outputs["plain"], width)
            self.assertTrue(all(len(line) <= width for line in outputs["plain"].splitlines()), width)
            codes = {code for match in self.ANSI.findall(outputs["no-color"]) for code in match[2:-1].split(";")}
            self.assertLessEqual(codes, {"0", "1", "4"})


class PurgeEstimate(unittest.TestCase):
    """What Purge through one spec would free on the snapshot page and in the snapshot choices."""
    SPEC, OTHER = "1" * 64, "2" * 64
    G = 1024**3

    def binding(self, spec=None, pinned=False):
        return {"spec_id": spec or self.SPEC, "pinned": pinned}

    def copy(self, node, *bindings, kind="prepared", **fields):
        return Page.location(node, kind, bindings=list(bindings) or [self.binding()], **fields)

    def entry(self, *locations, size=102 * 1024**3):
        entry = Page.entry("fixture/Model", "a" * 40, list(locations), size=size)
        entry.update(catalog_specs=[{"spec_id": self.SPEC, "snapshot": "target", "model_id": "fixture/Model"}],
                     cleanup_spec_ids=[self.SPEC])
        return entry

    def nodes(self):
        return [Page.node("1"), Page.node("2"), Page.node("3")]

    def estimate(self, entry):
        return purge_estimate(entry, self.SPEC, self.nodes())

    def line(self, entry):
        return purge_line(entry, self.SPEC, self.nodes())

    def test_only_this_specs_unshared_unpinned_unused_copies_count(self):
        entry = self.entry(self.copy("1", kind="home"), self.copy("2"), self.copy("3"))
        self.assertEqual(self.estimate(entry), {"bytes_by_node": {"2": 102 * self.G, "3": 102 * self.G},
                                                "pinned_on": [], "in_use_on": [], "unknown_on": []})
        self.assertEqual(self.line(entry), "Purge via [111111111111] would free 204 GiB in all on alpha-2, alpha-3.")

    def test_a_shared_copy_or_the_home_frees_nothing(self):
        entry = self.entry(self.copy("1", kind="home"),
                           self.copy("2", self.binding(), self.binding(self.OTHER), shared=True))
        self.assertEqual(self.estimate(entry)["bytes_by_node"], {})
        self.assertIn("would free no space: it only releases the home or copies other specs share", self.line(entry))

    def test_a_pin_of_this_spec_blocks_purge_and_another_specs_pin_does_not(self):
        entry = self.entry(self.copy("1", self.binding(pinned=True), pinned=True), self.copy("2"))
        estimate = self.estimate(entry)
        self.assertEqual((estimate["pinned_on"], estimate["bytes_by_node"]), (["1"], {"2": 102 * self.G}))
        self.assertEqual(self.line(entry),
                         "Purge via [111111111111] is blocked: pinned on alpha-1. Choose Unpin prepared copies first.")
        other = self.entry(self.copy("1", self.binding(), self.binding(self.OTHER, pinned=True),
                                     pinned=True, shared=True))
        self.assertEqual(self.estimate(other)["pinned_on"], [])

    @staticmethod
    def service(spec=None, managed=True):
        """A container that mounts the files: a Pulsar service for SPEC, or one Pulsar did not start."""
        labels = {"io.pulsar.gb10.managed": "true"} if managed else {"org.example": "x"}
        if spec:
            labels["io.pulsar.gb10.selected-spec-id"] = spec
        return {"id": "c", "running": True, "mounts": ["/fixture"], "labels": labels}

    def test_a_mounted_home_this_spec_uses_blocks_purge_and_frees_nothing(self):
        entry = self.entry(self.copy("1", kind="home", container_references=[self.service(self.SPEC)]),
                           self.copy("2", self.binding(pinned=True), pinned=True))
        estimate = self.estimate(entry)
        self.assertEqual((estimate["in_use_on"], estimate["pinned_on"], estimate["bytes_by_node"]), (["1"], ["2"], {}))
        self.assertEqual(self.line(entry), "Purge via [111111111111] is blocked: in use on alpha-1; pinned on alpha-2. "
                                           "Choose Stop first, then Unpin prepared copies.")

    def test_unread_container_use_or_pins_make_the_estimate_partial(self):
        entry = self.entry(self.copy("1", container_references=None), self.copy("2"),
                           self.copy("3", bindings_observed=False))
        estimate = self.estimate(entry)
        self.assertEqual((estimate["unknown_on"], estimate["bytes_by_node"]), (["1", "3"], {"2": 102 * self.G}))
        self.assertEqual(self.line(entry), "Purge via [111111111111] would free at least 102 GiB on alpha-2; "
                                           "not known on alpha-1, alpha-3, which could not be fully checked.")
        only = self.entry(self.copy("1", container_references=None))
        self.assertIn("what it would free is not known on alpha-1", self.line(only))
        # A pin from a saved record the node could not confirm makes the node unknown, as on the page.
        saved = self.entry(self.copy("1", self.binding(pinned=True), pinned=True, bindings_observed=False))
        self.assertEqual((self.estimate(saved)["pinned_on"], self.estimate(saved)["unknown_on"]), ([], ["1"]))
        self.assertNotIn("blocked", self.line(saved))

    def test_the_remedy_names_who_started_the_mounting_container(self):
        def line(container, catalog=None):
            return purge_line(self.entry(self.copy("1", container_references=[container])), self.SPEC,
                              self.nodes(), catalog)
        blocked = "Purge via [111111111111] is blocked: in use on alpha-1. "
        self.assertEqual(line(self.service(self.SPEC)), blocked + "Choose Stop first.")
        self.assertEqual(line(self.service(self.OTHER), {self.OTHER: "fixture/Other"}),
                         blocked + "Stop fixture/Other first, from its own recipe or Live service inventory.")
        self.assertEqual(line(self.service("3" * 64)), blocked + "Stop it from Live service inventory first.")
        self.assertEqual(line(self.service(managed=False)),
                         blocked + "A container Pulsar did not start mounts the files; remove it outside Pulsar first.")
        # A pin adds Unpin after the container's own remedy.
        entry = self.entry(self.copy("1", self.binding(pinned=True), pinned=True,
                                     container_references=[self.service(managed=False)]))
        self.assertTrue(purge_line(entry, self.SPEC, self.nodes()).endswith(
            "remove it outside Pulsar first, then Unpin prepared copies."))

    def test_a_confirmed_node_that_could_not_be_read_makes_the_figure_a_minimum(self):
        entry = self.entry(self.copy("2"))
        for broken in ({"status": "unknown", "error": "node unavailable"}, {"container_error": "docker failed"}):
            nodes = [Page.node("1", **broken), Page.node("2")]
            estimate = purge_estimate(entry, self.SPEC, nodes)
            self.assertEqual((estimate["unknown_on"], estimate["bytes_by_node"]), (["1"], {"2": 102 * self.G}))
            self.assertEqual(purge_line(entry, self.SPEC, nodes),
                             "Purge via [111111111111] would free at least 102 GiB on alpha-2; not known on "
                             "alpha-1, which could not be fully checked.")
        document = Page.document([Page.node("1", status="unknown", error="node unavailable"), Page.node("2")], [entry])
        self.assertNotIn("frees", self.run_main(document, "choices"))

    def test_a_node_whose_records_could_not_be_read_makes_the_figure_a_minimum(self):
        # combine() keeps such a node observed with no error; its records field says what failed.
        case = FileInventory("setUp")
        case.setUp()
        self.addCleanup(case.temp.cleanup)
        broken = case.view_root / ".pulsar-node" / "views"
        broken.mkdir(parents=True)
        (broken / ("f" * 64 + ".json")).write_text("{not json")
        manifest = case.manifest["manifest_id"]
        copy = {"kind": "prepared", "hub_path": "/fixture/views/copy", "path": "/fixture/views/copy/snapshot",
                "state": "present", "snapshot_manifest_id": manifest, "registered_home": False,
                "verified_at": None, "bindings": [self.binding()]}
        healthy = {"observed_at": now(), "locations": [copy], "scopes": [], "disks": [], "issues": [],
                   "unidentified": [], "bindings_observed": True}
        saved = case.saved(specs={manifest: [{"spec_id": self.SPEC, "model_id": "fixture/model", "snapshot": "target"}]})
        document = combine(saved, [case.observation(files=case.scan()), case.observation("n1", healthy)])
        nodes = document["nodes"]
        self.assertEqual([(node["status"], node["error"], node["records"]) for node in nodes],
                         [("observed", "", "unreadable"), ("observed", "", "observed")])
        entry = document["snapshots"][0]
        self.assertEqual(purge_estimate(entry, self.SPEC, nodes)["unknown_on"], ["n0"])
        self.assertEqual(purge_line(entry, self.SPEC, nodes),
                         f"Purge via [111111111111] would free at least {len(case.data)} bytes on fixture-n1; "
                         "not known on fixture-n0, which could not be fully checked.")

    def test_a_pin_in_another_snapshot_of_the_spec_blocks_this_snapshots_purge(self):
        # Purge covers every snapshot the spec has copies of, so the draft's pin blocks the target page.
        target = self.entry(self.copy("1", kind="home"), self.copy("2"))
        draft = Page.entry("fixture/Draft", "b" * 40, [self.copy("3", self.binding(pinned=True), pinned=True)])
        draft.update(catalog_specs=[{"spec_id": self.SPEC, "snapshot": "draft", "model_id": "fixture/Model"}],
                     cleanup_spec_ids=[self.SPEC])
        snapshots = [target, draft]
        estimate = purge_estimate(target, self.SPEC, self.nodes(), snapshots)
        self.assertEqual((estimate["pinned_on"], estimate["bytes_by_node"]), (["3"], {"2": 102 * self.G}))
        self.assertEqual(purge_line(target, self.SPEC, self.nodes(), snapshots=snapshots),
                         "Purge via [111111111111] is blocked: pinned on alpha-3 (its draft snapshot). "
                         "Choose Unpin prepared copies first.")
        document = Page.document(self.nodes(), snapshots)
        self.assertNotIn("frees", self.run_main(document, "choices"))
        page = " ".join(self.run_main(document, "render", "--snapshot", target["snapshot_manifest_id"]).split())
        self.assertIn("is blocked: pinned on alpha-3 (its draft snapshot).", page)
        # Without the draft's pin the target's figure stands.
        draft["locations"][0]["bindings"], draft["locations"][0]["pinned"] = [self.binding()], False
        self.assertIn("— frees 102 GiB", self.run_main(document, "choices"))

    def test_a_node_outside_confirmed_membership_counts_only_where_it_holds_the_specs_copy(self):
        departed = Page.node("9", status="unknown", error=OUTSIDE_MEMBERSHIP, observed_at=None, scopes=[], disks=[])
        nodes = [*self.nodes(), departed]
        entry = self.entry(self.copy("2"), Page.location("9", bindings=[], state="unknown",
                                                         bindings_observed=False, container_references=None))
        self.assertEqual(purge_estimate(entry, self.SPEC, nodes)["unknown_on"], [])
        self.assertEqual(purge_line(entry, self.SPEC, nodes), "Purge via [111111111111] would free 102 GiB on alpha-2.")
        entry["locations"].append(self.copy("9", state="unknown", bindings_observed=False, container_references=None))
        self.assertEqual(purge_estimate(entry, self.SPEC, nodes)["unknown_on"], ["9"])
        self.assertEqual(purge_line(entry, self.SPEC, nodes),
                         "Purge via [111111111111] would free at least 102 GiB on alpha-2; not known on alpha-9, "
                         "which could not be fully checked.")

    def test_a_missing_copy_frees_nothing_and_a_spec_without_copies_says_so(self):
        self.assertEqual(self.estimate(self.entry(self.copy("2", state="missing")))["bytes_by_node"], {})
        self.assertIn("no prepared copies of this snapshot are recorded for it", self.line(self.entry(Page.location("1"))))

    def run_main(self, document, command, *arguments, columns=100):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "files.json"
            path.write_text(json.dumps({"ok": True, "result": document}))
            output = io.StringIO()
            with patch.dict(os.environ, {"COLUMNS": str(columns), "PULSAR_COLOR": "never"}), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(main([command, "--file", str(path), *arguments]), 0)
        return output.getvalue()

    def test_snapshot_page_and_choice_say_what_purge_would_free(self):
        entry = self.entry(self.copy("1", kind="home"), self.copy("2"))
        document = Page.document(self.nodes(), [entry])
        page = " ".join(self.run_main(document, "render", "--snapshot", entry["snapshot_manifest_id"]).split())
        self.assertIn("Purge via [111111111111] would free 102 GiB on alpha-2.", page)
        self.assertIn("fixture/Model @ aaaaaaaa — frees 102 GiB", self.run_main(document, "choices"))
        # Too narrow for the whole size: the suffix is left off rather than cut.
        self.assertNotIn("frees", self.run_main(document, "choices", columns=36))
        # A blocked or partly unknown purge, or several specs, give the choice no size.
        entry["locations"][1]["container_references"] = None
        self.assertNotIn("frees", self.run_main(document, "choices"))
        entry["locations"][1]["container_references"] = []
        entry["cleanup_spec_ids"] = [self.SPEC, self.OTHER]
        self.assertNotIn("frees", self.run_main(document, "choices"))


class Budget(unittest.TestCase):
    """The storage budget table on the overview page, read from model budget --json."""
    G = 1024**3

    def budget_file(self, folder, envelope=None, err=""):
        path = Path(folder) / "budget.json"
        path.write_text("" if envelope is None else json.dumps(envelope))
        if err:
            path.with_suffix(".err").write_text(err)
        return path

    def nodes(self):
        return [{"node_id": "1", "available": 1900 * self.G, "used": 450 * self.G, "total": 4000 * self.G,
                 "reserve": 100 * self.G, "limit": 2250 * self.G, "path": "/fixture/views"},
                {"node_id": "2", "available": 200 * self.G, "used": 300 * self.G, "total": 4000 * self.G,
                 "reserve": 205 * self.G, "limit": 400 * self.G, "path": "/fixture/views"}]

    def test_read_budget_takes_the_result_or_the_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            ok = {"schema_version": 1, "ok": True, "result": {"kind": "pulsar-storage-budget", "nodes": self.nodes()}}
            self.assertEqual(read_budget(self.budget_file(folder, ok)), (self.nodes(), None))
            failed = {"schema_version": 1, "ok": False, "error": {"message": "node storage is unobservable"}}
            self.assertEqual(read_budget(self.budget_file(folder, failed)), (None, "node storage is unobservable"))
            err = "warning: slow\nerror: budget inspection requires confirmed topology\n"
            self.assertEqual(read_budget(self.budget_file(folder, None, err)),
                             (None, "budget inspection requires confirmed topology"))
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(read_budget(self.budget_file(folder)), (None, "model budget returned no result"))

    def page(self, document, width, **options):
        output = io.StringIO()
        with patch.dict(os.environ, {"PULSAR_COLOR": "never"}, clear=True):
            render(document, writer=TerminalWriter(width=width, stream=output), **options)
        return output.getvalue()

    def test_the_table_sits_under_the_nodes_and_fits_every_width(self):
        document = Page().two_node_model()
        for width in (80, 60, 44):
            text = self.page(document, width, budget=(self.nodes(), None))
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
            flat = " ".join(text.split())
            self.assertIn("Storage budget Node", flat)
            self.assertIn("alpha-2 is 5.00 GiB past its reserve or copy budget.", flat)
            self.assertLess(flat.index("alpha-2 Free space"), flat.index("Storage budget"))
            self.assertIn("Observed prepared-copy storage only; space is not reserved.", flat)
        failed = self.page(document, 80, budget=(None, "node storage is unobservable"))
        self.assertIn("Storage budget not observed; node storage is unobservable", " ".join(failed.split()))
        # The snapshot page shows no budget, and neither does a page rendered without one.
        snapshot = document["snapshots"][0]["snapshot_manifest_id"]
        self.assertNotIn("Storage budget", self.page(document, 80, snapshot=snapshot, budget=(self.nodes(), None)))
        self.assertNotIn("Storage budget", self.page(document, 80))
