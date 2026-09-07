"""Unit tests for the stdlib-only ADR 0017 release spec package."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from release_spec import (  # noqa: E402
    ReleaseSpecError,
    build_snapshot_manifest,
    identity_block,
    load_spec,
    normalize_container_env,
    normalize_engine_args,
    normalize_snapshot_files,
    pretty_json_bytes,
    snapshot_file_lists_equal,
    snapshot_manifest_id,
    spec_id_for,
    verify_spec,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
GOLDEN_MEASURED = FIXTURES / "golden_measured.json"
GOLDEN_RELEASED = FIXTURES / "golden_released.json"
ENGINE_SPELLINGS = FIXTURES / "engine_arg_spellings.json"
GOLDEN_SPEC_ID = "9cd7164d49591f763ba506d7845a13f96b247bbae193bce49f978a67e1e4aa16"
FORBIDDEN_ENGINE_FLAGS = (
    "--tensor-parallel-size",
    "-tp",
    "--pipeline-parallel-size",
    "-pp",
    "--port",
    "--host",
    "--served-model-name",
    "--model",
    "--api-key",
    "--download-dir",
)


def _load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sync_argv(document: dict) -> dict:
    """Recompute launch_contract.argv from the (canonical) identity."""
    from release_spec.identity import argv_from_identity, canonical_identity

    document["launch_contract"]["argv"] = argv_from_identity(
        canonical_identity(document["identity"])
    )
    return document


def _copy(document: dict) -> dict:
    return copy.deepcopy(document)


class ReleaseSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.measured = _load_json(GOLDEN_MEASURED)
        self.released = _load_json(GOLDEN_RELEASED)

    def test_isolation_stdlib_only(self) -> None:
        package_dir = REPO_ROOT / "release_spec"
        stdlib = sys.stdlib_module_names
        forbidden_roots = {"scripts", "validate", "platforms"}
        for path in sorted(package_dir.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue
                    if node.module is None:
                        continue
                    names = [node.module]
                else:
                    continue
                for name in names:
                    root = name.split(".")[0]
                    self.assertNotIn(
                        root,
                        forbidden_roots,
                        f"{path.name} imports {name}",
                    )
                    self.assertIn(
                        root,
                        stdlib,
                        f"{path.name} imports non-stdlib module {name}",
                    )

    def test_launch_contract_argv_must_match_identity(self) -> None:
        document = _copy(self.measured)
        document["launch_contract"]["argv"] = ["--max-model-len", "1"]
        with self.assertRaisesRegex(ReleaseSpecError, "launch_contract.argv"):
            verify_spec(document)
        document = _copy(self.measured)
        document["launch_contract"]["argv"] = document["launch_contract"]["argv"][:-2]
        with self.assertRaisesRegex(ReleaseSpecError, "launch_contract.argv"):
            verify_spec(document)

    def test_golden_spec_id_is_pinned(self) -> None:
        measured = verify_spec(self.measured)
        released = verify_spec(self.released)
        self.assertEqual(measured["spec_id"], GOLDEN_SPEC_ID)
        self.assertEqual(released["spec_id"], GOLDEN_SPEC_ID)
        self.assertEqual(measured["identity"], released["identity"])

    def test_non_identity_sections_do_not_change_spec_id(self) -> None:
        mutations = []
        state_released = _copy(self.measured)
        state_released["state"] = "released"
        state_released["review"] = {
            "status": "stable",
            "reviewer": "example-reviewer",
            "reviewed_at": "2026-09-02T00:00:00Z",
        }
        mutations.append(state_released)

        launch = _copy(self.measured)
        launch["launch_contract"]["stack_version"] = "1.2.3-test"
        mutations.append(launch)

        measurements = _copy(self.measured)
        measurements["measurements"][0]["outcome"] = "fail"
        mutations.append(measurements)

        baselines = _copy(self.measured)
        baselines["baselines"][0]["claimed"] = "9"
        mutations.append(baselines)

        evidence = _copy(self.measured)
        evidence["evidence"][0]["path"] = "results/example/other.json"
        mutations.append(evidence)

        review = _copy(self.released)
        review["review"]["status"] = "withdrawn"
        review["review"]["reason"] = "Superseded by a better recipe."
        review["review"]["reviewer"] = "other-reviewer"
        mutations.append(review)

        for document in mutations:
            self.assertEqual(spec_id_for(identity_block(document)), GOLDEN_SPEC_ID)
            self.assertEqual(verify_spec(document)["spec_id"], GOLDEN_SPEC_ID)

    def test_each_identity_field_changes_spec_id(self) -> None:
        def hashed(identity: dict) -> str:
            return spec_id_for(identity)

        original = identity_block(self.measured)
        self.assertEqual(hashed(original), GOLDEN_SPEC_ID)

        model_id = _copy(original)
        model_id["model_id"] = "other-org/other-model"
        model_id["snapshot_manifest"]["model_id"] = "other-org/other-model"
        model_id["snapshot_manifest"]["manifest_id"] = snapshot_manifest_id(
            model_id["snapshot_manifest"]
        )
        self.assertNotEqual(hashed(model_id), GOLDEN_SPEC_ID)

        commit = _copy(original)
        commit["snapshot_revision"] = "b" * 40
        commit["snapshot_manifest"]["snapshot_revision"] = "b" * 40
        commit["snapshot_manifest"]["manifest_id"] = snapshot_manifest_id(
            commit["snapshot_manifest"]
        )
        self.assertNotEqual(hashed(commit), GOLDEN_SPEC_ID)

        file_sha = _copy(original)
        file_sha["snapshot_manifest"]["files"][0]["sha256"] = "3" * 64
        file_sha["snapshot_manifest"]["manifest_id"] = snapshot_manifest_id(
            file_sha["snapshot_manifest"]
        )
        self.assertNotEqual(hashed(file_sha), GOLDEN_SPEC_ID)

        engine = _copy(original)
        engine["engine_args"] = [
            "--max-model-len",
            "8192",
            "--moe-backend",
            "marlin",
        ]
        self.assertNotEqual(hashed(engine), GOLDEN_SPEC_ID)

        env = _copy(original)
        env["container_env"] = ["VLLM_MARLIN_USE_ATOMIC_ADD=0"]
        self.assertNotEqual(hashed(env), GOLDEN_SPEC_ID)

        image = _copy(original)
        image["image"] = {"digest": "sha256:" + "d" * 64}
        self.assertNotEqual(hashed(image), GOLDEN_SPEC_ID)

        platform = _copy(original)
        platform["geometry"] = dict(platform["geometry"])
        platform["geometry"]["platform_id"] = "other-platform"
        self.assertNotEqual(hashed(platform), GOLDEN_SPEC_ID)

        two_node = _copy(original)
        two_node["geometry"] = {
            "platform_id": "dgx-spark-gb10",
            "nodes": 2,
            "tp": 2,
            "pp": 1,
            "fabric": "roce-v2",
        }
        self.assertNotEqual(hashed(two_node), GOLDEN_SPEC_ID)
        two_node_spec = _copy(self.measured)
        two_node_spec["identity"] = two_node
        two_node_spec["spec_id"] = hashed(two_node)
        _sync_argv(two_node_spec)
        self.assertEqual(verify_spec(two_node_spec)["spec_id"], hashed(two_node))

    def test_engine_arg_spellings_collapse(self) -> None:
        corpus = _load_json(ENGINE_SPELLINGS)
        canonical_args = corpus["engine_args"]["canonical"]
        for spelling in corpus["engine_args"]["spellings"]:
            self.assertEqual(normalize_engine_args(spelling), canonical_args)
        canonical_env = corpus["container_env"]["canonical"]
        for spelling in corpus["container_env"]["spellings"]:
            self.assertEqual(normalize_container_env(spelling), canonical_env)

    def test_json_valued_flag_keeps_quotes_and_spaces(self) -> None:
        # The live Super profile's MTP config: a JSON value must survive as
        # one token, from either the conf-style string or a literal list.
        config = '{"method":"mtp","num_speculative_tokens":1,"moe_backend":"triton"}'
        spaced = '{"method": "mtp", "num_speculative_tokens": 1}'
        canonical = ["--speculative-config", config]
        self.assertEqual(normalize_engine_args(canonical), canonical)
        self.assertEqual(
            normalize_engine_args(f"--speculative-config '{config}'"),
            canonical,
        )
        self.assertEqual(
            normalize_engine_args([f"--speculative-config={config}"]),
            canonical,
        )
        self.assertEqual(
            normalize_engine_args(["--speculative-config", spaced]),
            ["--speculative-config", spaced],
        )
        document = _copy(self.measured)
        document["identity"]["engine_args"] = canonical
        document["spec_id"] = spec_id_for(identity_block(document))
        _sync_argv(document)
        self.assertEqual(
            verify_spec(document)["identity"]["engine_args"],
            canonical,
        )
        with self.assertRaisesRegex(ReleaseSpecError, "must not be empty"):
            normalize_engine_args(["--max-model-len", ""])
        with self.assertRaisesRegex(ReleaseSpecError, "must be a string"):
            normalize_engine_args(["--max-model-len", 131072])

    def test_engine_arg_order_is_identity(self) -> None:
        identity = identity_block(self.measured)
        swapped = _copy(identity)
        swapped["engine_args"] = [
            "--moe-backend",
            "marlin",
            "--max-model-len",
            "131072",
        ]
        self.assertNotEqual(spec_id_for(identity), spec_id_for(swapped))

    def test_forbidden_engine_flags(self) -> None:
        for flag in FORBIDDEN_ENGINE_FLAGS:
            for token in (flag, f"{flag}=1"):
                document = _copy(self.measured)
                document["identity"]["engine_args"] = [token]
                with self.assertRaisesRegex(ReleaseSpecError, flag):
                    verify_spec(document)
        allowed = _copy(self.measured)
        allowed["identity"]["engine_args"] = list(
            allowed["identity"]["engine_args"]
        ) + ["--distributed-executor-backend", "mp"]
        allowed["spec_id"] = spec_id_for(allowed["identity"])
        _sync_argv(allowed)
        verify_spec(allowed)

    def test_snapshot_files_sorted_unique_relative(self) -> None:
        unsorted = [
            {"path": "model.safetensors", "size": 8, "sha256": "1" * 64},
            {"path": "config.json", "size": 12, "sha256": "2" * 64},
        ]
        built = build_snapshot_manifest(
            model_id="example-org/example-model",
            snapshot_revision="a" * 40,
            files=unsorted,
        )
        self.assertEqual(
            [item["path"] for item in built["files"]],
            ["config.json", "model.safetensors"],
        )
        self.assertEqual(
            normalize_snapshot_files(unsorted),
            built["files"],
        )

        document = _copy(self.measured)
        document["identity"]["snapshot_manifest"]["files"] = unsorted
        with self.assertRaisesRegex(ReleaseSpecError, "sorted"):
            verify_spec(document)

        duplicate = _copy(self.measured)
        files = duplicate["identity"]["snapshot_manifest"]["files"]
        files.append(_copy(files[0]))
        with self.assertRaisesRegex(ReleaseSpecError, "duplicates"):
            verify_spec(duplicate)

        absolute = _copy(self.measured)
        absolute["identity"]["snapshot_manifest"]["files"][0]["path"] = "/config.json"
        with self.assertRaisesRegex(ReleaseSpecError, "absolute"):
            verify_spec(absolute)

        parent = _copy(self.measured)
        parent["identity"]["snapshot_manifest"]["files"][0]["path"] = "../config.json"
        with self.assertRaisesRegex(ReleaseSpecError, r"\.\."):
            verify_spec(parent)

        non_ascii = _copy(self.measured)
        non_ascii["identity"]["snapshot_manifest"]["files"][0]["path"] = (
            "caf" + chr(233) + ".json"
        )
        with self.assertRaisesRegex(ReleaseSpecError, "ASCII"):
            verify_spec(non_ascii)

    def test_digest_only_manifest_rejected(self) -> None:
        as_digest = _copy(self.measured)
        as_digest["identity"]["snapshot_manifest"] = as_digest["identity"][
            "snapshot_manifest"
        ]["manifest_id"]
        with self.assertRaisesRegex(ReleaseSpecError, "digest-only"):
            verify_spec(as_digest)
        digest_object = _copy(self.measured)
        digest_object["identity"]["snapshot_manifest"] = {"digest": "a" * 64}
        with self.assertRaisesRegex(ReleaseSpecError, "digest-only"):
            verify_spec(digest_object)

    def test_manifest_id_matches_library_algorithm(self) -> None:
        manifest = self.measured["identity"]["snapshot_manifest"]
        payload = {
            key: value for key, value in manifest.items() if key != "manifest_id"
        }
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(manifest["manifest_id"], expected)
        self.assertEqual(snapshot_manifest_id(manifest), expected)

    def test_snapshot_file_lists_equal(self) -> None:
        files = self.measured["identity"]["snapshot_manifest"]["files"]
        reversed_files = list(reversed(files))
        self.assertTrue(snapshot_file_lists_equal(files, reversed_files))
        different = _copy(files)
        different[0]["sha256"] = "3" * 64
        self.assertFalse(snapshot_file_lists_equal(files, different))

    def test_closed_keys(self) -> None:
        top = _copy(self.measured)
        top["extra"] = "nope"
        with self.assertRaisesRegex(ReleaseSpecError, "document"):
            verify_spec(top)

        identity = _copy(self.measured)
        identity["identity"]["extra"] = "nope"
        with self.assertRaisesRegex(ReleaseSpecError, "identity"):
            verify_spec(identity)

        file_entry = _copy(self.measured)
        file_entry["identity"]["snapshot_manifest"]["files"][0]["extra"] = "nope"
        with self.assertRaisesRegex(ReleaseSpecError, r"files\[0\]"):
            verify_spec(file_entry)

        review = _copy(self.released)
        review["review"]["extra"] = "nope"
        with self.assertRaisesRegex(ReleaseSpecError, "review"):
            verify_spec(review)

    def test_measured_requires_empty_review(self) -> None:
        document = _copy(self.measured)
        document["review"] = {
            "status": "stable",
            "reviewer": "example-reviewer",
            "reviewed_at": "2026-09-02T00:00:00Z",
        }
        with self.assertRaisesRegex(ReleaseSpecError, "empty object"):
            verify_spec(document)

    def test_released_requires_full_review(self) -> None:
        empty = _copy(self.released)
        empty["review"] = {}
        with self.assertRaisesRegex(ReleaseSpecError, "review"):
            verify_spec(empty)
        missing = _copy(self.released)
        del missing["review"]["reviewer"]
        with self.assertRaisesRegex(ReleaseSpecError, "review"):
            verify_spec(missing)

    def test_passing_status_requires_passing_suite(self) -> None:
        def released(status: str, measurements: list) -> dict:
            document = _copy(self.released)
            document["review"]["status"] = status
            if status == "withdrawn":
                document["review"]["reason"] = "Withdrawn after further review."
            document["measurements"] = measurements
            return document

        baseline = self.released["measurements"][0]
        failed_baseline = _copy(baseline)
        failed_baseline["outcome"] = "fail"
        other_policy = _copy(baseline)
        other_policy["criterion_id"] = "example-accuracy"
        other_policy["policy_digest"] = "c" * 64
        deep = {
            "criterion_id": "example-deep",
            "suite": "deep",
            "policy_digest": None,
            "thresholds": _copy(baseline["thresholds"]),
            "outcome": "pass",
            "evidence_ids": ["evidence-1"],
        }
        failed_deep = _copy(deep)
        failed_deep["outcome"] = "fail"

        with self.assertRaisesRegex(ReleaseSpecError, "requires baseline-v1"):
            verify_spec(released("stable", []))
        with self.assertRaisesRegex(ReleaseSpecError, "outcome to be 'pass'"):
            verify_spec(released("stable", [baseline, failed_baseline]))
        with self.assertRaisesRegex(ReleaseSpecError, "one baseline-v1 policy_digest"):
            verify_spec(released("stable", [baseline, other_policy]))
        with self.assertRaisesRegex(ReleaseSpecError, "requires deep measurements"):
            verify_spec(released("validated", [baseline]))
        with self.assertRaisesRegex(ReleaseSpecError, "every deep outcome"):
            verify_spec(released("validated", [baseline, failed_deep]))
        with self.assertRaisesRegex(ReleaseSpecError, "requires baseline-v1"):
            verify_spec(released("validated", [deep]))
        verify_spec(released("validated", [baseline, deep]))
        # failed and withdrawn carry no suite requirement; measured judges nothing.
        verify_spec(released("failed", [failed_baseline]))
        verify_spec(released("withdrawn", []))
        measured = _copy(self.measured)
        measured["measurements"] = [failed_baseline]
        verify_spec(measured)

    def test_review_status_enum(self) -> None:
        document = _copy(self.released)
        document["review"]["status"] = "Validated"
        with self.assertRaisesRegex(ReleaseSpecError, "review.status"):
            verify_spec(document)
        experimental = _copy(self.released)
        experimental["review"]["status"] = "experimental"
        verify_spec(experimental)

    def test_measurements_rules(self) -> None:
        missing_digest = _copy(self.measured)
        missing_digest["measurements"][0]["policy_digest"] = None
        with self.assertRaisesRegex(ReleaseSpecError, "policy_digest"):
            verify_spec(missing_digest)

        deep = _copy(self.measured)
        deep["measurements"][0]["suite"] = "deep"
        deep["measurements"][0]["policy_digest"] = None
        deep["measurements"][0]["thresholds"] = []
        with self.assertRaisesRegex(ReleaseSpecError, "thresholds"):
            verify_spec(deep)

        unknown = _copy(self.measured)
        unknown["measurements"][0]["evidence_ids"] = ["missing-evidence"]
        with self.assertRaisesRegex(ReleaseSpecError, "evidence"):
            verify_spec(unknown)

        operator = _copy(self.measured)
        operator["measurements"][0]["thresholds"][0]["operator"] = "eq"
        with self.assertRaisesRegex(ReleaseSpecError, "operator"):
            verify_spec(operator)

    def test_geometry_rules(self) -> None:
        product = _copy(self.measured)
        product["identity"]["geometry"]["tp"] = 2
        with self.assertRaisesRegex(ReleaseSpecError, "tp \\* pp"):
            verify_spec(product)

        fabric = _copy(self.measured)
        fabric["identity"]["geometry"]["nodes"] = 2
        fabric["identity"]["geometry"]["tp"] = 2
        fabric["identity"]["geometry"]["fabric"] = "local"
        with self.assertRaisesRegex(ReleaseSpecError, "fabric"):
            verify_spec(fabric)

        boolean_int = _copy(self.measured)
        boolean_int["identity"]["geometry"]["nodes"] = True
        with self.assertRaisesRegex(ReleaseSpecError, "positive integer"):
            verify_spec(boolean_int)

    def test_floats_rejected(self) -> None:
        document = _copy(self.measured)
        document["identity"]["geometry"]["nodes"] = 1.0
        with self.assertRaisesRegex(ReleaseSpecError, "JSON float"):
            verify_spec(document)

    def test_privacy_screen(self) -> None:
        site_path = "/mnt/" + "site"
        ipv4 = ".".join(["192", "0", "2", "1"])
        token = "HF_TOKEN=" + "hf_" + ("a" * 32)
        uri = "https://" + "example.invalid" + "/model"
        cases = (
            ("identity.engine_args", "$" + "HOME/models"),
            ("identity.engine_args", "${" + "MODEL_DIR}"),
            ("identity.container_env", "CACHE=%" + "APPDATA%"),
            ("identity.engine_args", "../weights"),
            ("identity.engine_args", site_path),
            ("identity.engine_args", ipv4),
            ("identity.container_env", token),
            ("identity.engine_args", uri),
        )
        for kind, bad in cases:
            document = _copy(self.measured)
            if kind.endswith("engine_args"):
                document["identity"]["engine_args"] = [bad]
            else:
                document["identity"]["container_env"] = [bad]
            with self.assertRaisesRegex(ReleaseSpecError, "private"):
                verify_spec(document)
        self.assertIn(
            "VLLM_MARLIN_USE_ATOMIC_ADD=1",
            verify_spec(self.measured)["identity"]["container_env"],
        )

    def test_spec_id_mismatch_rejected(self) -> None:
        document = _copy(self.measured)
        document["spec_id"] = "0" * 64
        with self.assertRaisesRegex(ReleaseSpecError, "spec_id"):
            verify_spec(document)

    def test_duplicate_keys_are_rejected_on_load(self) -> None:
        text = GOLDEN_MEASURED.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            top = pathlib.Path(tmp) / "dup-top.json"
            # A second, valid "state" after the first: last-key-wins parsers accept it.
            top.write_text(text.rstrip().rstrip("}") + ',\n  "state": "measured"\n}\n', encoding="utf-8")
            with self.assertRaisesRegex(ReleaseSpecError, "duplicate key 'state'"):
                load_spec(top)
            nested = pathlib.Path(tmp) / "dup-nested.json"
            nested.write_text(
                text.replace('"fabric": "local",', '"fabric": "local",\n      "fabric": "local",', 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ReleaseSpecError, "duplicate key 'fabric'"):
                load_spec(nested)
            result = subprocess.run(
                [sys.executable, "-m", "release_spec", "verify", str(top)],
                cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("duplicate key", result.stderr)

    def test_canonical_round_trip(self) -> None:
        spec = verify_spec(self.measured)
        again = verify_spec(json.loads(pretty_json_bytes(spec)))
        self.assertEqual(spec, again)

    def test_cli_verify_id_show(self) -> None:
        def run(*args: str, file: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
            command = [sys.executable, "-m", "release_spec", *args]
            if file is not None:
                command.append(str(file))
            return subprocess.run(
                command,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )

        verify = run("verify", file=GOLDEN_MEASURED)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertEqual(
            verify.stdout,
            f"spec_id={GOLDEN_SPEC_ID} state=measured\n",
        )

        ident = run("id", file=GOLDEN_RELEASED)
        self.assertEqual(ident.returncode, 0, ident.stderr)
        self.assertEqual(ident.stdout, GOLDEN_SPEC_ID + "\n")

        show = run("show", file=GOLDEN_MEASURED)
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertEqual(
            show.stdout,
            pretty_json_bytes(verify_spec(self.measured)).decode("utf-8"),
        )

        usage = run("nope")
        self.assertEqual(usage.returncode, 2)
        self.assertIn("error:", usage.stderr)
        self.assertNotIn("Traceback", usage.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            broken = pathlib.Path(tmp) / "broken.json"
            broken.write_text("{", encoding="utf-8")
            failed = run("verify", file=broken)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error:", failed.stderr)
        self.assertNotIn("Traceback", failed.stderr)
        self.assertTrue(failed.stderr.startswith("error:"))
        load_spec(GOLDEN_MEASURED)


if __name__ == "__main__":
    unittest.main()
