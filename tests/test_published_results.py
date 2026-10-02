"""Published evidence views reuse canonical judgements, never live or private data."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import shlex
import subprocess
import os
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr

from model_library import catalog, catalog_menu, published_results as results
from model_library.state import Store
from release_spec import pretty_json_bytes
from release_spec.evidence_v2 import evaluate_measurements, evidence_summary, verify_evidence
from scripts.terminal_format import TerminalWriter
from tests.test_current_evidence import make_run

ROOT = Path(__file__).resolve().parents[1]


def publish(repo, *, name="campaign-1", suite="baseline-v2", speculative=False, fail=False, empty=False, shift=0):
    source = repo.parent / ("unpublished-" + repo.name + "-" + name)
    spec, run = make_run(source, speculative=speculative)
    (source / "policy.json").write_bytes((ROOT / f"policy/{suite}.json").read_bytes())
    if fail:
        path = source / "measurements/evaluate-gsm8k.json"
        value = json.loads(path.read_text())
        value["evaluate-gsm8k"].update(correct_count=0, accuracy="0")
        path.write_bytes(pretty_json_bytes(value))
    if shift:
        def shifted(value):
            return (datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(days=shift)).isoformat().replace("+00:00", "Z")
        for gate in run["gates"]:
            for key in ("started_at", "ended_at"):
                gate[key] = shifted(gate[key])
        path = source / "measurements/validate-soak.json"
        value = json.loads(path.read_text())
        for key in ("started_at", "ended_at"):
            value["validate-soak"][key] = shifted(value["validate-soak"][key])
        path.write_bytes(pretty_json_bytes(value))
    if empty:
        for path in (source / "measurements").iterdir():
            path.unlink()
        run["gates"] = []
        run["input_sha256"].pop("dataset", None)
    evaluation, _ = evaluate_measurements(spec, source / "policy.json", source / "measurements")
    run.update(run_id=name, outcome=evaluation["outcome"], policy_digest=evaluation["policy_digest"],
               measurement_sha256=evaluation["measurement_sha256"])
    run["input_sha256"]["policy"] = hashlib.sha256((source / "policy.json").read_bytes()).hexdigest()
    (source / "run.json").write_bytes(pretty_json_bytes(run))
    (repo / "releases").mkdir(parents=True, exist_ok=True)
    (repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
    directory = repo / "results" / suite / spec["spec_id"] / name
    directory.mkdir(parents=True)
    for path in [source / "run.json", source / "policy.json", *(source / "measurements").iterdir()]:
        shutil.copyfile(path, directory / path.name)
    verified = verify_evidence(source / "spec.json", source / "run.json", source)
    (directory / "summary.json").write_bytes(pretty_json_bytes(evidence_summary(verified, spec)))
    return spec, directory


class PublishedResults(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "catalog"

    def test_no_results_is_explicit_and_does_not_create_state(self):
        spec, _ = make_run(self.root / "unpublished")
        (self.repo / "releases").mkdir(parents=True)
        (self.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
        with patch.object(results, "verify_evidence", side_effect=AssertionError("must not read unpublished evidence")):
            report = results.collect(self.repo, spec["spec_id"])
        self.assertEqual(report, {"runs": [], "issues": []})
        self.assertEqual(results.compact_lines(report), ["Published results: none supplied"])
        self.assertFalse((self.repo / ".model-library").exists())

    def test_current_spec_schemas_show_verified_dates_workload_and_policy_scope(self):
        for speculative, suite in ((False, "baseline-v1"), (True, "baseline-v2")):
            with self.subTest(speculative=speculative):
                repo = self.repo / suite
                spec, directory = publish(repo, suite=suite, speculative=speculative)
                original = {path: path.read_bytes() for path in directory.iterdir()}
                report = results.collect(repo, spec["spec_id"])
                self.assertEqual(report["issues"], [])
                run, = report["runs"]
                self.assertEqual(run["status"], "verified", run)
                self.assertEqual(run["outcome"], "pass")
                self.assertTrue(run["ended_at"])
                output = io.StringIO()
                results.render(report, spec["spec_id"], writer=TerminalWriter(width=44, stream=output))
                text = " ".join(output.getvalue().split())
                for expected in ("Historical measurements", "input /", "output tokens", "concurrency", "samples",
                                 "Run outcome", "Policy outcome", "Run started", "Run ended", "evidence summary"):
                    self.assertIn(expected, text)
                if suite == "baseline-v2":
                    self.assertIn("Capture repeatability is diagnostic", text)
                for line in output.getvalue().splitlines():
                    if not line.startswith(("./pulsar", "  --")):
                        self.assertLessEqual(len(line), 44, line)
                self.assertEqual(original, {path: path.read_bytes() for path in directory.iterdir()})

    def test_newer_failed_run_is_not_hidden_by_an_older_pass(self):
        spec, _ = publish(self.repo, name="older-pass")
        publish(self.repo, name="newer-fail", fail=True, shift=1)
        report = results.collect(self.repo, spec["spec_id"])
        self.assertEqual([run["outcome"] for run in report["runs"]], ["fail", "pass"])
        text = " ".join(results.compact_lines(report))
        self.assertIn("Latest dated run (verified): baseline-v2 fail", text)
        self.assertNotIn("baseline-v2 pass", text)

    def test_untimed_incomplete_run_remains_unknown(self):
        spec, _ = publish(self.repo, empty=True)
        report = results.collect(self.repo, spec["spec_id"])
        run, = report["runs"]
        self.assertEqual(run["status"], "verified", run)
        self.assertEqual(run["outcome"], "incomplete")
        self.assertIsNone(run["ended_at"])
        self.assertIn("no recorded run time", " ".join(results.compact_lines(report)))
        self.assertNotIn("Latest dated", " ".join(results.compact_lines(report)))

    def test_bad_evidence_is_not_a_pass_and_does_not_hide_other_runs(self):
        spec, directory = publish(self.repo, name="bad")
        publish(self.repo, name="good")
        path = directory / "summary.json"
        value = json.loads(path.read_text()); value["outcome"] = "fail"
        path.write_bytes(pretty_json_bytes(value))
        report = results.collect(self.repo, spec["spec_id"])
        bad = next(run for run in report["runs"] if run["run_id"] == "bad")
        self.assertEqual(bad["status"], "unverified")
        self.assertNotIn("outcome", bad)
        self.assertIn("1 unverified run", " ".join(results.compact_lines(report)))
        self.assertEqual(sum(run["status"] == "verified" for run in report["runs"]), 1)

    def test_missing_or_tampered_measurement_is_unverified(self):
        spec, directory = publish(self.repo)
        (directory / "benchmark-serving.json").unlink()
        run, = results.collect(self.repo, spec["spec_id"])["runs"]
        self.assertEqual(run["status"], "unverified")
        self.assertNotIn("outcome", run)

    def test_result_identity_must_match_its_directory(self):
        spec, directory = publish(self.repo)
        directory.rename(directory.with_name("another-id"))
        run, = results.collect(self.repo, spec["spec_id"])["runs"]
        self.assertEqual(run["status"], "unverified")
        self.assertIn("identity differ", run["reason"])

    def test_linked_and_unknown_result_locations_are_explicit(self):
        spec, directory = publish(self.repo)
        link = directory.with_name("linked-run")
        link.symlink_to(self.root / "unpublished-catalog-campaign-1", target_is_directory=True)
        unknown = self.repo / "results" / "future-suite" / spec["spec_id"]
        unknown.mkdir(parents=True)
        report = results.collect(self.repo, spec["spec_id"])
        linked = next(run for run in report["runs"] if run["run_id"] == "linked-run")
        self.assertEqual(linked["status"], "unverified")
        self.assertIn("Unsupported published result suite", report["issues"][0])

    def test_linked_results_root_is_not_followed(self):
        spec, directory = publish(self.repo)
        public = self.repo / "results"
        outside = self.root / "unpublished-results"
        public.rename(outside)
        public.symlink_to(outside, target_is_directory=True)
        with patch.object(results, "verify_evidence", side_effect=AssertionError("must not follow links")):
            report = results.collect(self.repo, spec["spec_id"])
        self.assertEqual(report["runs"], [])
        self.assertTrue(report["issues"])
        self.assertNotIn("none supplied", " ".join(results.compact_lines(report)))

    def test_measurement_changed_after_verification_is_not_displayed_as_verified(self):
        spec, directory = publish(self.repo)
        verifier = results.verify_evidence
        def change_after_verification(*args):
            verified = verifier(*args)
            path = directory / "benchmark-serving.json"
            path.write_bytes(path.read_bytes() + b"\n")
            return verified
        with patch.object(results, "verify_evidence", side_effect=change_after_verification):
            run, = results.collect(self.repo, spec["spec_id"])["runs"]
        self.assertEqual(run["status"], "unverified")
        self.assertNotIn("measurements", run)

    def test_results_command_keeps_catalog_json_and_state_unchanged(self):
        spec, _ = publish(self.repo)
        state = self.root / "missing-state"
        args = ["--repo-root", str(self.repo), "--state-root", str(state)]
        expected = catalog.entries(self.repo, Store(state))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(catalog.main(["show", spec["spec_id"], "--json", *args]), 0)
        self.assertEqual(json.loads(output.getvalue())["entries"], expected)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(catalog.main(["results", spec["spec_id"], *args]), 0)
        self.assertIn("Historical measurements", output.getvalue())
        command = output.getvalue().split("Full evidence summary (JSON):\n")[1]
        self.assertEqual(shlex.split(command.replace("\\\n", " ")), ["./pulsar", "evidence", "summary",
            "--spec-file", f"releases/{spec['spec_id']}.json", "--run",
            f"results/baseline-v2/{spec['spec_id']}/campaign-1/run.json", "--evidence-root", "results", "--json"])
        self.assertFalse(state.exists())
        process = subprocess.run([str(ROOT / "pulsar"), "models", "results", spec["spec_id"], *args],
                                 env={**os.environ, "PULSAR_DOCKER": "/bin/false", "PULSAR_SSH": "/bin/false"},
                                 text=True, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("Historical measurements", process.stdout)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(catalog.main(["results", "f" * 64, *args]), 2)

    def test_historical_evidence_has_an_explicit_existing_inspection_route(self):
        from tests.test_release_contribution import make_contribution
        spec, _, _, _ = make_contribution(self.root / "historical")
        (self.repo / "releases").mkdir(parents=True)
        (self.repo / "releases" / f"{spec['spec_id']}.json").write_bytes(pretty_json_bytes(spec))
        report = results.collect(self.repo, spec["spec_id"])
        self.assertTrue(report["historical"])
        output = io.StringIO()
        results.render(report, spec["spec_id"], writer=TerminalWriter(stream=output))
        self.assertIn("--historical", output.getvalue())
        self.assertIn("bound evidence references", " ".join(output.getvalue().split()))
        self.assertNotIn("None supplied", output.getvalue())

    def test_published_outcome_never_changes_menu_eligibility_or_spec_metadata(self):
        spec, _ = publish(self.repo, fail=True)
        row, = catalog.entries(self.repo, Store(self.root / "absent-state"))
        before = copy.deepcopy(row)
        expected = catalog_menu.operations(row, "configured")
        report = results.collect(self.repo, spec["spec_id"])
        for width in (44, 80):
            lines = catalog_menu.view_lines(row, "configured", published=report, width=width)
            headings = [line.split("\t", 1)[1] for line in lines if line.startswith("header\t")]
            text = " ".join(headings)
            for group in ("Selected recipe", "Current observations", "Published results", "Live service: not observed"):
                self.assertIn(group, text)
            self.assertTrue(all(len(line) <= width - 4 for line in headings))
            self.assertEqual(catalog_menu.operations(row, "configured"), expected)
        self.assertEqual(row, before)

    def test_settings_summary_does_not_infer_defaults_or_repeated_value_precedence(self):
        arguments = ["--max-model-len=8192", "--max-num-seqs", "4", "--quantization", "fp8"]
        self.assertEqual(catalog_menu.explicit_settings(arguments), "context 8192; max sequences 4; quantization fp8")
        self.assertIn("repeated values", catalog_menu.explicit_settings([*arguments, "--max-model-len", "4096"]))
        self.assertEqual(catalog_menu.explicit_settings([]), "see Show details")

    def test_compare_picker_excludes_current_and_historical_but_allows_guarded(self):
        selected = {"spec_id": "a" * 64}
        historical = {"spec_id": "b" * 64, "historical": True}
        guarded = {"spec_id": "c" * 64, "model_id": "example/model", "historical": False,
                   "local_state": "unknown", "start_supported": False, "start_unsupported_reason": "guard_unsupported"}
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps({"entries": [selected, historical, guarded]}))), redirect_stdout(output):
            self.assertEqual(catalog_menu.main(["labels", "--compare-with", selected["spec_id"]]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertTrue(output.getvalue().startswith(guarded["spec_id"] + "\t"))


if __name__ == "__main__":
    unittest.main()
