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
        for args in (["start", "ab" * 32, "--bogus"], ["stop", "ab" * 32, "--bogus"], ["stop", "abc"],
                     ["model", "bogus-operation"], ["status", "ab" * 32, "--bogus"]):
            with self.subTest(args=args):
                status, response = pulsar(*args)
                self.assertEqual(status, 2)
                self.assertEqual(response["error"]["code"], "usage_error", response)

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
        self.assertEqual(status, 2)
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


if __name__ == "__main__":
    unittest.main()
