"""The archive search a download without a spec runs first: manifests only, synthetic folders."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.archive_search import find_archives, main  # noqa: E402
from release_spec import build_snapshot_manifest  # noqa: E402

MODEL, COMMIT = "example/model", "a" * 40


def manifest(model=MODEL, commit=COMMIT, size=3):
    return build_snapshot_manifest(model_id=model, snapshot_revision=commit,
                                   files=[{"path": "weights.bin", "size": size, "sha256": "b" * 64}])


class ArchiveSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "archive"
        self.root.mkdir()

    def archive(self, document, name=None):
        hub = self.root / "pulsar-snapshots" / (name or document["manifest_id"])
        hub.mkdir(parents=True)
        (hub / "manifest.json").write_text(json.dumps(document))
        return hub

    def test_the_archive_of_the_model_commit_is_found_by_its_manifest_alone(self):
        wanted = manifest()
        hub = self.archive(wanted)
        self.archive(manifest(commit="c" * 40))
        self.archive(manifest(model="example/other"))
        found = find_archives(str(self.root), MODEL, COMMIT)
        self.assertEqual(found["state"], "searched")
        self.assertEqual(found["archives"], [{"snapshot_manifest_id": wanted["manifest_id"], "path": str(hub),
                                              "manifest_path": str(hub / "manifest.json"),
                                              "total_bytes": 3, "file_count": 1}])
        self.assertEqual(found["skipped"], 0)
        # No model file exists in these folders: the search read none.

    def test_no_location_and_an_empty_one_are_different_answers(self):
        for root in (None, ""):
            self.assertEqual(find_archives(root, MODEL, COMMIT)["state"], "not-configured")
        empty = find_archives(str(self.root), MODEL, COMMIT)
        self.assertEqual((empty["state"], empty["archives"]), ("searched", []))

    def test_a_location_that_cannot_be_read_is_unknown_not_absent(self):
        missing = find_archives(str(self.root / "not-mounted"), MODEL, COMMIT)
        self.assertEqual((missing["state"], missing["archives"]), ("unreadable", []))
        self.archive(manifest())
        if os.geteuid() != 0:
            (self.root / "pulsar-snapshots").chmod(0)
            self.addCleanup((self.root / "pulsar-snapshots").chmod, 0o700)
            self.assertEqual(find_archives(str(self.root), MODEL, COMMIT)["state"], "unreadable")

    def test_staging_and_damaged_folders_never_match_and_never_stop_the_search(self):
        wanted = manifest()
        self.archive(wanted, name=".pending-1")
        self.archive(wanted, name="f" * 64)                  # its name is not its manifest's identity
        self.archive({**wanted, "total_bytes": 9}, name="e" * 64)  # fields do not match the canonical manifest
        (self.root / "pulsar-snapshots" / ("d" * 64)).mkdir()   # no manifest
        (self.root / "pulsar-snapshots" / "notes.txt").write_text("x")
        found = find_archives(str(self.root), MODEL, COMMIT)
        self.assertEqual((found["state"], found["archives"], found["skipped"]), ("searched", [], 4))
        self.archive(wanted)
        self.assertEqual(len(find_archives(str(self.root), MODEL, COMMIT)["archives"]), 1)

    def test_the_command_reads_the_location_from_the_environment(self):
        import contextlib
        import io
        self.archive(manifest())
        output = io.StringIO()
        previous = os.environ.get("PULSAR_COLD_ROOT")
        os.environ["PULSAR_COLD_ROOT"] = str(self.root)
        try:
            with contextlib.redirect_stdout(output):
                self.assertEqual(main([MODEL, COMMIT]), 0)
        finally:
            if previous is None:
                del os.environ["PULSAR_COLD_ROOT"]
            else:
                os.environ["PULSAR_COLD_ROOT"] = previous
        self.assertEqual(len(json.loads(output.getvalue())["archives"]), 1)


if __name__ == "__main__":
    unittest.main()
