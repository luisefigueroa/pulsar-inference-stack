"""Synthetic Git/LFS acquisition checks and a fake rank-local hf downloader."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.integrity import StorageError, read_json
from model_library.local import begin_staging, finish_staging
from model_library.source import (normalize_inventory, validate_source, verify_download,
    compare_inventory_to_manifest, prepare_download, clean_download_metadata)
from release_spec import pretty_json_bytes


def upstream():
    git_bytes = b'{"model_type":"synthetic"}\n'
    weight_bytes = b"synthetic weights"
    raw = {"id": "example/synthetic", "sha": "a" * 40, "siblings": [
        {"type": "file", "path": "config.json", "size": len(git_bytes),
         "blob_id": hashlib.sha1(f"blob {len(git_bytes)}\0".encode() + git_bytes).hexdigest()},
        {"type": "file", "path": "weights.bin", "size": len(weight_bytes), "blob_id": "b" * 40,
         "lfs": {"size": len(weight_bytes), "sha256": hashlib.sha256(weight_bytes).hexdigest()}}]}
    return raw, {"config.json": git_bytes, "weights.bin": weight_bytes}


class SourceAcquisition(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw, self.files = upstream()
        self.source = normalize_inventory(self.raw, "example/synthetic", "a" * 40)
        self.stage = begin_staging(self.root / "pulsar-homes")
        self.prepared = prepare_download(self.stage, self.source)
        self.snapshot = Path(self.prepared["snapshot_path"])
        for name, data in self.files.items(): (self.snapshot / name).write_bytes(data)

    def test_upstream_git_and_lfs_bytes_produce_canonical_manifest(self):
        manifest = verify_download(self.snapshot, self.source)
        self.assertEqual(manifest["model_id"], "example/synthetic")
        self.assertEqual(manifest["file_count"], 2)
        self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(self.files["config.json"]).hexdigest())
        compare_inventory_to_manifest(self.source, manifest)
        self.assertEqual(verify_download(self.snapshot, self.source, manifest), manifest)

    def test_mutable_revision_resolves_only_during_inventory(self):
        self.assertEqual(normalize_inventory(self.raw, "example/synthetic")["snapshot_revision"], "a" * 40)
        with self.assertRaisesRegex(StorageError, "requested exact commit"):
            normalize_inventory(self.raw, "example/synthetic", "c" * 40)
        wrong = copy.deepcopy(self.raw); wrong["sha"] = "main"
        with self.assertRaises(StorageError): normalize_inventory(wrong, "example/synthetic")

    def test_wrong_model_and_duplicate_paths_rejected(self):
        with self.assertRaisesRegex(StorageError, "selected model"):
            normalize_inventory(self.raw, "example/other")
        bad = copy.deepcopy(self.raw); bad["siblings"].append(bad["siblings"][0])
        with self.assertRaisesRegex(StorageError, "sorted and unique"):
            normalize_inventory(bad, "example/synthetic")

    def test_paths_and_downloader_metadata_cannot_be_smuggled(self):
        for name in ("../escape", "/escape", ".cache/huggingface/download/file", ".cache", "config.json/child"):
            bad = copy.deepcopy(self.raw); bad["siblings"][1]["path"] = name
            with self.subTest(name=name), self.assertRaises(StorageError):
                normalize_inventory(bad, "example/synthetic")

    def test_missing_size_digest_and_conflicting_lfs_size_rejected(self):
        for mutate in (lambda x: x["siblings"][0].pop("blob_id"),
                       lambda x: x["siblings"][1]["lfs"].update(size=999),
                       lambda x: x["siblings"][0].update(size=True),
                       lambda x: x["siblings"][0].update(blob_id="0" * 40)):
            bad = copy.deepcopy(self.raw); mutate(bad)
            with self.assertRaises(StorageError): normalize_inventory(bad, "example/synthetic")

    def test_each_upstream_object_hash_is_enforced(self):
        for name in self.files:
            original = self.files[name]
            (self.snapshot / name).write_bytes(b"x" * len(original))
            with self.subTest(name=name), self.assertRaisesRegex(StorageError, "differs from upstream"):
                verify_download(self.snapshot, self.source)
            (self.snapshot / name).write_bytes(original)

    def test_expected_spec_detects_git_content_sha256_mismatch(self):
        manifest = verify_download(self.snapshot, self.source)
        from release_spec import build_snapshot_manifest
        changed = copy.deepcopy(manifest["files"]); changed[0]["sha256"] = "e" * 64
        expected = build_snapshot_manifest(model_id=manifest["model_id"], snapshot_revision=manifest["snapshot_revision"], files=changed)
        # Git inventory has SHA-1; the final independent SHA-256 check still applies.
        compare_inventory_to_manifest(self.source, expected)
        with self.assertRaisesRegex(StorageError, "SHA-256 manifest differs"):
            verify_download(self.snapshot, self.source, expected)

    def test_extra_missing_and_symlink_files_rejected(self):
        extra = self.snapshot / "extra"; extra.write_text("extra")
        with self.assertRaisesRegex(StorageError, "file set differs"):
            verify_download(self.snapshot, self.source)
        extra.unlink()
        weights = self.snapshot / "weights.bin"; weights.unlink()
        with self.assertRaisesRegex(StorageError, "file set differs"):
            verify_download(self.snapshot, self.source)
        external = self.root / "outside"; external.write_bytes(self.files["weights.bin"])
        weights.symlink_to(external)
        with self.assertRaisesRegex(StorageError, "link or special"):
            verify_download(self.snapshot, self.source)

    def test_replacement_during_read_is_rejected(self):
        real_read = os.read
        replaced = False
        def replace(fd, length):
            nonlocal replaced
            result = real_read(fd, length)
            if result and not replaced:
                replaced = True
                original = self.snapshot / "config.json"
                original.unlink(); original.write_bytes(self.files["config.json"])
            return result
        with patch("model_library.source.os.read", side_effect=replace):
            with self.assertRaisesRegex(StorageError, "changed|replaced"):
                verify_download(self.snapshot, self.source)

    def test_cleanup_only_removes_owned_downloader_metadata(self):
        metadata = self.snapshot / ".cache/huggingface/download"
        metadata.mkdir(parents=True); (metadata / "cached-info").write_text("transient")
        unrelated = self.root / "unrelated"; unrelated.write_text("retain")
        clean_download_metadata(self.stage, self.source)
        self.assertFalse((self.stage / ".download-cache").exists())
        self.assertFalse((self.snapshot / ".cache").exists())
        self.assertEqual(unrelated.read_text(), "retain")
        verify_download(self.snapshot, self.source)

    def test_cleanup_rejects_unowned_paths_and_symlink_cache(self):
        with self.assertRaisesRegex(StorageError, "owned acquisition staging"):
            clean_download_metadata(self.root, self.source)
        cache = self.snapshot / ".cache"; cache.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(StorageError): clean_download_metadata(self.stage, self.source)

    def test_publication_is_atomic_and_never_replaces(self):
        clean_download_metadata(self.stage, self.source)
        manifest = verify_download(self.snapshot, self.source)
        destination = self.stage.parent / manifest["manifest_id"]
        stamp = finish_staging(self.stage, destination, manifest)
        self.assertEqual(stamp["method"], "sha256")
        self.assertEqual(read_json(destination / "manifest.json"), manifest)
        second = begin_staging(destination.parent)
        snapshot = Path(prepare_download(second, self.source)["snapshot_path"])
        for name, data in self.files.items(): (snapshot / name).write_bytes(data)
        clean_download_metadata(second, self.source)
        with self.assertRaisesRegex(StorageError, "without replacement"):
            finish_staging(second, destination, manifest)

    def test_sourcing_shell_helper_has_no_actions(self):
        script = 'model_node() { exit 90; }; ssh_node() { exit 91; }; source "$1"; echo loaded'
        result = subprocess.run(["bash", "-c", script, "test", str(ROOT / "scripts/acquire-source.sh")], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "loaded")

    def test_fake_hf_uses_selected_stage_for_all_transient_data(self):
        # A fake local hf writes only synthetic bytes. No Hub call or model download.
        for path in self.snapshot.iterdir(): path.unlink()
        binary = self.root / "bin"; binary.mkdir()
        fake = binary / "hf"
        fake.write_text("#!/usr/bin/env python3\n" + r'''
import json,os,pathlib,sys
a=sys.argv; out=pathlib.Path(a[a.index('--local-dir')+1]); stage=out.parent.parent
assert a[1:3]==['download','example/synthetic']
assert a[a.index('--revision')+1]=='a'*40
for key in ('HF_HUB_CACHE','HF_XET_CACHE','HF_ASSETS_CACHE','TMPDIR'):
    assert pathlib.Path(os.environ[key]).is_relative_to(stage/'.download-cache')
assert os.environ['HF_HOME']=='unchanged-auth-location'
(out/'config.json').write_bytes(b'{"model_type":"synthetic"}\n')
(out/'weights.bin').write_bytes(b'synthetic weights')
meta=out/'.cache/huggingface/download';meta.mkdir(parents=True)
(meta/'info').write_text('transient')
''')
        fake.chmod(0o700)
        source_path = self.root / "source.json"; source_path.write_bytes(pretty_json_bytes(self.source))
        script = r'''
set -euo pipefail
REPO_DIR=$1
shell_join_q() { printf '%q ' "$@"; }
model_node() {
  printf '%s' "$2" | python3 -c '
import json,sys
from model_library.source import prepare_download,clean_download_metadata
r=json.load(sys.stdin)
if r["operation"]=="source-prepare": print(json.dumps(prepare_download(r["stage"],r["source"])))
elif r["operation"]=="source-clean": clean_download_metadata(r["stage"],r["source"])
else: raise SystemExit(9)
'
}
source "$REPO_DIR/scripts/acquire-source.sh"
source_download_on_rank 0 "$2" "$(cat "$3")"
'''
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"],
                   PYTHONPATH=str(ROOT), HF_HOME="unchanged-auth-location", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(["bash", "-c", script, "test", str(ROOT), str(self.stage), str(source_path)],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        verify_download(self.snapshot, self.source)
        self.assertFalse((self.stage / ".download-cache").exists())


if __name__ == "__main__": unittest.main()
