"""The Connect card: how to call a running service, never its key (TUI-30)."""
import contextlib
import io
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
from scripts import connect_card

SERVICE_ID = "5e" * 32
PLAN_ID = "7a" * 32
BODY = '{"model":"example","prompt":"Write one sentence about the Moon.","max_tokens":32}'


def result(**fields):
    return {"state": "running", "service_id": SERVICE_ID, "api_url": "http://spark-1:8000",
            "served_name": "example", **fields}


def request_args(lines):
    """The first request as the shell would split it."""
    start = lines.index("First request:") + 1
    return shlex.split("\n".join(lines[start:]).replace("\\\n", " "))


class Card(unittest.TestCase):
    def setUp(self):
        # The recorded launch plan is read from a temporary library, never this checkout's.
        self.library = Path(self.enterContext(tempfile.TemporaryDirectory())) / "library"
        self.enterContext(patch.dict(os.environ, {"PULSAR_MODEL_LIBRARY_DIR": str(self.library),
                                                  "VLLM_API_KEY": "fixture-secret-key"}))

    def record(self, api_auth):
        from model_library.state import Store
        store = Store(self.library)
        store.put("services", SERVICE_ID, {"plan_id": PLAN_ID})
        store.put("service-plans", PLAN_ID, {"ranks": [], **({} if api_auth is None else {"api_auth": api_auth})})

    def test_a_service_started_with_a_key_reads_it_from_the_client_environment(self):
        lines = connect_card.card(result(api_auth=True))
        self.assertEqual(lines, [
            "Connect",
            "  API:    http://spark-1:8000/v1",
            "  Model:  example",
            "  Auth:   API key required; the request reads it from VLLM_API_KEY",
            "",
            "First request:",
            "curl --fail-with-body --silent --show-error http://spark-1:8000/v1/completions \\",
            "  -H 'Content-Type: application/json' \\",
            '  -H "Authorization: Bearer ${VLLM_API_KEY:?Set the service API key in this shell}" \\',
            f"  -d '{BODY}'"])
        self.assertNotIn("fixture-secret-key", "\n".join(lines))
        args = request_args(lines)
        self.assertEqual(args[4], "http://spark-1:8000/v1/completions")
        self.assertEqual(json.loads(args[-1]), {"model": "example", "prompt": "Write one sentence about the Moon.",
                                                "max_tokens": 32})

    def test_a_service_without_a_key_sends_no_authorization_header(self):
        lines = connect_card.card(result(api_auth=False))
        self.assertIn("  Auth:   none; the API accepts requests without a key", lines)
        self.assertFalse(any("Authorization" in line for line in lines))
        self.assertEqual(request_args(lines)[-2:], ["-d", BODY])

    def test_an_unrecorded_requirement_keeps_the_header_and_says_so(self):
        for prepare in (lambda: None, lambda: self.record(None)):
            with self.subTest():
                prepare()
                lines = connect_card.card(result())
                self.assertIn("  Auth:   not recorded; add the key header if the service was started with one", lines)
                self.assertTrue(any("Authorization: Bearer ${VLLM_API_KEY" in line for line in lines))
        self.assertIn("  Auth:   not recorded; add the key header if the service was started with one",
                      connect_card.card(result(service_id=None)))

    def test_the_recorded_launch_plan_decides_when_the_result_does_not(self):
        for recorded, expected in ((True, "API key required"), (False, "none; the API accepts")):
            with self.subTest(recorded=recorded):
                self.record(recorded)
                self.assertTrue(any(expected in line for line in connect_card.card(result())))
        # A value in the result wins over the record.
        self.assertTrue(any("API key required" in line for line in connect_card.card(result(api_auth=True))))

    def test_a_loopback_url_works_only_on_the_api_node(self):
        note = ["  This loopback address works only on the API node; from another machine,",
                "  use an address that reaches that node."]
        for url in ("http://127.0.0.1:8000", "http://localhost:8000/", "http://[::1]:8000"):
            with self.subTest(url=url):
                lines = connect_card.card(result(api_url=url, api_auth=False))
                self.assertEqual(lines[4:6], note)
                self.assertEqual(lines[1], "  API:    " + url.rstrip("/") + "/v1")
        self.assertFalse(any("loopback" in line for line in connect_card.card(result(api_auth=False))))

    def test_the_served_name_comes_from_the_result_or_its_services(self):
        lines = connect_card.card(result(served_name=None, services=[{}, {"served_name": "from-service"}], api_auth=False))
        self.assertIn("  Model:  from-service", lines)
        lines = connect_card.card(result(served_name="", api_auth=False))
        self.assertIn("  Model:  not recorded; replace SERVED_MODEL in the request", lines)
        self.assertEqual(json.loads(request_args(lines)[-1])["model"], "SERVED_MODEL")

    def test_no_card_without_a_running_service_api(self):
        for fields in ({"state": "stopped"}, {"state": "partial"}, {"state": "stale"}, {"api_url": None},
                       {"api_url": ""}):
            with self.subTest(fields=fields):
                self.assertEqual(connect_card.card(result(**fields)), [])
        # Start's result has no state field; it reports a running service.
        self.assertTrue(connect_card.card({"api_url": "http://spark-1:8000", "api_auth": False}))

    def main(self, content):
        path = self.library.parent / "result.json"
        path.unlink(missing_ok=True)
        if content is not None:
            path.write_text(content)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(connect_card.main(["--result", str(path)]), 0)
        return out.getvalue()

    def test_main_prints_the_card_or_nothing(self):
        self.assertEqual(self.main(json.dumps(result(api_auth=False))).splitlines()[:2], ["", "Connect"])
        for content in (None, "", "not json", "[]", json.dumps(result(state="stopped"))):
            with self.subTest(content=content):
                self.assertEqual(self.main(content), "")


class Wrapper(unittest.TestCase):
    """with-connect-card.sh keeps the command's output and exit status."""
    COMMAND = r'''
echo "command output"
printf '%s' "$PULSAR_SERVICE_RESULT_FILE" > "$FIXTURE_DIR/result-path"
[ -z "${FIXTURE_RESULT:-}" ] || printf '%s' "$FIXTURE_RESULT" > "$PULSAR_SERVICE_RESULT_FILE"
exit "${FIXTURE_RC:-0}"
'''

    def run_wrapped(self, rc=0, written=None):
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {key: value for key, value in os.environ.items() if not key.startswith("PULSAR_")}
        env.update(FIXTURE_DIR=str(temporary), FIXTURE_RC=str(rc), PULSAR_MODEL_LIBRARY_DIR=str(temporary / "library"),
                   FIXTURE_RESULT="" if written is None else json.dumps(written))
        completed = subprocess.run(["bash", str(ROOT / "scripts/with-connect-card.sh"), "bash", "-c", self.COMMAND],
                                   env=env, text=True, capture_output=True, timeout=30)
        result_path = Path((temporary / "result-path").read_text())
        return completed, result_path

    def test_success_with_a_running_service_prints_the_card_after_the_output(self):
        completed, path = self.run_wrapped(0, result(api_auth=False))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.startswith("command output\n\nConnect\n"), completed.stdout)
        self.assertIn("http://spark-1:8000/v1/completions", completed.stdout)
        self.assertFalse(path.exists())  # the result file is removed

    def test_a_failure_keeps_its_status_and_prints_no_card(self):
        for rc in (1, 2, 3, 130):
            with self.subTest(rc=rc):
                completed, path = self.run_wrapped(rc, result(api_auth=False))
                self.assertEqual(completed.returncode, rc)
                self.assertEqual(completed.stdout, "command output\n")
                self.assertFalse(path.exists())

    def test_success_without_a_result_or_a_running_service_prints_no_card(self):
        for written in (None, result(state="stopped")):
            with self.subTest(written=written):
                completed, _ = self.run_wrapped(0, written)
                self.assertEqual((completed.returncode, completed.stdout), (0, "command output\n"))


if __name__ == "__main__":
    unittest.main()
