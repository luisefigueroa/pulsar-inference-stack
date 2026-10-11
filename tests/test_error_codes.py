"""Envelope error codes are specific, advertised by the contract and documented."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import integration_contract
from scripts import document_cli, public_cli


def pulsar(*args):
    result = subprocess.run([str(ROOT / "pulsar"), *args, "--json"], text=True, capture_output=True)
    return result.returncode, json.loads(result.stdout)


class Completed:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


class ErrorCodes(unittest.TestCase):
    def test_usage_errors_are_not_spec_errors(self):
        for args in (["memory"], ["spec", "verify"], ["policy", "show", "unknown"]):
            with self.subTest(args=args):
                status, response = pulsar(*args)
                self.assertEqual(status, 2)
                self.assertEqual(response["error"]["code"], "usage_error", response)

    def test_lifecycle_argument_mistakes_are_usage_errors(self):
        # Argument parsing happens before any lock, topology or node access.
        spec = "ab" * 32
        for args in (["start", spec, "--bogus"], ["stop", spec, "--bogus"], ["stop", "abc"], ["stop"],
                     ["model", "bogus-operation"], ["model"], ["status", spec, "--bogus"], ["status"],
                     ["start", spec, "--force"], ["start", spec, "--weight-source", "copy"],
                     ["start", spec, "--spec-decode", "--no-spec-decode"],
                     ["model", "prepare", spec, "--backend", "other"], ["observe", "--service-id"],
                     ["status", spec, "--node"], ["stop", spec, "--node"], ["observe", "--node", "--full"],
                     ["status", spec, "--service-id", spec], ["model", "prepare", spec, "--transport", "nope"],
                     ["model", "prepare", spec, "--copy-streams", "3"],
                     ["bogus"]):
            with self.subTest(args=args):
                status, response = pulsar(*args)
                self.assertEqual(status, 2)
                self.assertEqual(response["error"]["code"], "usage_error", response)

    def test_resource_options_do_not_take_the_next_flag(self):
        # Stops at argument parsing, before any node is sampled.
        result = subprocess.run(["bash", str(ROOT / "scripts/resources.sh"), "--interval", "--jsonl"],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual((result.returncode, result.stderr.strip()), (2, "error: --interval requires a value"))

    def test_a_missing_or_unknown_subcommand_names_the_valid_ones(self):
        for args, message in (
                (["evidence"], "pulsar evidence needs a subcommand: measurement, evaluate, verify or summary; "
                               "see ./pulsar evidence --help"),
                (["memory", "--bogus"], "pulsar memory needs a subcommand: verify; see ./pulsar memory --help"),
                (["privacy", "bogus"], "pulsar privacy has no subcommand bogus; use check or commits; "
                                       "see ./pulsar privacy --help")):
            with self.subTest(args=args):
                status, response = pulsar(*args)
                self.assertEqual((status, response["error"]["code"]), (2, "usage_error"))
                self.assertEqual(response["error"]["details"][0]["message"], message)
                human = subprocess.run([str(ROOT / "pulsar"), *args], text=True, capture_output=True)
                self.assertEqual((human.returncode, human.stdout), (2, ""))
                # The command names itself, so the human line drops the "command:" field label.
                self.assertEqual(human.stderr, f"error: {message}\n")

    def test_a_shortened_spec_id_under_json_is_a_usage_error_before_any_lock_or_probe(self):
        ids = sorted(path.stem for path in (ROOT / "releases").glob("*.json") if len(path.stem) == 64)
        spec = ids[0]
        absent = next(prefix for prefix in ("0" * 12, "f" * 12, "a" * 12)
                      if not any(spec_id.startswith(prefix) for spec_id in ids))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probe = root / "probe"
            probe.write_text(f"#!/bin/sh\ntouch {root / 'probed'}\nexit 1\n")
            probe.chmod(0o755)
            env = {**os.environ, "PULSAR_SSH": str(probe), "PULSAR_DOCKER": str(probe),
                   "PULSAR_MODEL_LIBRARY_DIR": str(root / "library"),
                   "CLUSTER_TOPOLOGY_FILE": str(root / "absent-topology.json")}
            for args, detail in (((spec[:12],), f"{spec[:12]} matches {spec}"),
                                 ((absent,), f"{absent} is not a catalog spec ID; see ./pulsar models list --json"),
                                 (("not-a-spec",), "not-a-spec is not a catalog spec ID")):
                for command in (["status"], ["start"], ["stop"], ["model", "info"]):
                    with self.subTest(command=command, args=args):
                        result = subprocess.run([str(ROOT / "pulsar"), *command, *args, "--json"],
                                                env=env, text=True, capture_output=True, timeout=60)
                        response = json.loads(result.stdout)
                        self.assertEqual((result.returncode, response["error"]["code"]), (2, "usage_error"))
                        self.assertIn("--json requires the complete 64-character spec ID; " + detail,
                                      response["error"]["details"][0]["message"])
            # Neither the lifecycle lock nor any node or Docker probe was reached.
            self.assertFalse((root / "library").exists())
            self.assertFalse((root / "probed").exists())

    def test_stop_without_a_selector_says_so(self):
        status, response = pulsar("stop")
        self.assertEqual((status, response["error"]["code"]), (2, "usage_error"))
        self.assertEqual(response["error"]["details"][0]["message"], "stop requires a spec ID or --all")

    def test_malformed_json_is_invalid_spec_not_a_file_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "spec.json"
            path.write_text("{bad")
            status, response = pulsar("spec", "verify", "--file", str(path))
        self.assertEqual(response["error"]["code"], "invalid_spec", response)

    def test_unreadable_input_file_is_a_file_error(self):
        with tempfile.TemporaryDirectory() as temp:
            status, response = pulsar("spec", "verify", "--file", str(Path(temp) / "missing.json"))
        self.assertEqual(status, 2)
        self.assertEqual(response["error"]["code"], "file_error", response)

    def test_invalid_content_remains_invalid_spec(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "spec.json"
            path.write_text('{"schema_version": 2}')
            status, response = pulsar("spec", "verify", "--file", str(path))
        self.assertEqual(status, 2)
        self.assertEqual(response["error"]["code"], "invalid_spec", response)

    def main(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = public_cli.main([*argv, "--json"])
        return status, json.loads(output.getvalue())

    def test_non_json_stack_output_is_reported_as_a_stack_defect(self):
        with patch.object(public_cli, "run_command", return_value=Completed("not json")):
            status, response = self.main("status", "a" * 64)
        self.assertEqual(status, 3)
        self.assertEqual(response["error"]["code"], "invalid_stack_output")
        self.assertIn("Stack defect", response["error"]["message"])

    def test_stop_reports_whether_a_service_was_stopped(self):
        def stop(command, env, cwd):
            Path(env["PULSAR_STOP_RESULT_FILE"]).write_text(json.dumps({"spec_id": "a" * 64, "stopped": False}))
            return Completed()
        with patch.object(public_cli, "run_command", side_effect=stop):
            status, response = self.main("stop", "a" * 64)
        self.assertEqual(status, 0)
        self.assertEqual(response["result"], {"completed": True, "spec_id": "a" * 64, "stopped": False})
        with patch.object(public_cli, "run_command", return_value=Completed()):
            status, response = self.main("stop", "--all")
        self.assertEqual(response["result"], {"completed": True, "stopped": None})


class Drift(unittest.TestCase):
    def test_contract_publishes_every_code_with_its_exit_status(self):
        published = integration_contract.contract()["error_codes"]
        self.assertEqual(published, {code: {"exit_status": status, "meaning": meaning}
                                     for code, (status, meaning) in document_cli.ERROR_CODES.items()})
        for status in {str(status) for status, _ in document_cli.ERROR_CODES.values()} | {"0"}:
            self.assertIn(status, integration_contract.contract()["exit_statuses"])

    def test_every_emitted_code_is_in_the_table(self):
        source = "".join((ROOT / name).read_text() for name in ("scripts/public_cli.py", "scripts/document_cli.py"))
        emitted = set(re.findall(r"code=\s*['\"]([a-z_]+)['\"]", source))
        emitted |= set(re.findall(r"return ['\"]([a-z_]+)['\"]", source.split("def error_code", 1)[1].split("def ", 1)[0]))
        self.assertTrue(emitted)
        self.assertLessEqual(emitted, set(document_cli.ERROR_CODES))

    def test_contract_document_lists_every_code_and_exit_status(self):
        text = (ROOT / "docs/CONTRACT.md").read_text()
        for code, (status, _) in document_cli.ERROR_CODES.items():
            self.assertRegex(text, rf"\| `{code}` \| {re.escape(str(status))} \|")
        for status, meaning in document_cli.EXIT_STATUSES.items():
            self.assertIn(f"| {status} | {meaning} |", text)

    def test_contract_document_describes_every_contract_field(self):
        text = (ROOT / "docs/CONTRACT.md").read_text()
        section = text.split("### The contract document", 1)[1].split("\n#", 1)[0]
        for field in integration_contract.contract():
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", section)

    def test_an_unknown_command_under_json_gets_the_envelope(self):
        for args in (["bogus", "--json"], ["--json"]):
            with self.subTest(args=args):
                result = subprocess.run([str(ROOT / "pulsar"), *args], text=True, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], "usage_error")


if __name__ == "__main__":
    unittest.main()
