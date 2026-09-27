"""Human messages share one prefix convention; deprecated aliases warn once."""
import os
import re
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import integration_contract


def shell(body, **env):
    return subprocess.run(["bash", "-c", f". '{ROOT}/scripts/lib.sh'; SCRIPT_NAME=check-weights; {body}"],
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env},
                          text=True, capture_output=True, timeout=30)


class Prefixes(unittest.TestCase):
    def test_errors_and_warnings_use_one_prefix_on_stderr_without_script_names(self):
        result = shell('log "Stopped."; warn "archive is stale"; die "model files are not ready" 3',
                       PULSAR_VERBOSE="0")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "Stopped.\n")
        self.assertEqual(result.stderr, "warning: archive is stale\nerror: model files are not ready\n")

    def test_verbose_restores_the_script_name(self):
        result = shell('log "checking"; die "failed"', PULSAR_VERBOSE="1")
        self.assertEqual(result.stdout, "[check-weights] checking\n")
        self.assertEqual(result.stderr, "[check-weights] error: failed\n")

    def test_no_old_style_prefixes_remain_in_shell_sources(self):
        offenders = []
        for path in [*ROOT.glob("scripts/*.sh"), *ROOT.glob("cluster/*.sh"), ROOT / "serve.sh"]:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if "] ERROR:" in line or "] warn:" in line or 'log "WARNING' in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        for path in [*ROOT.glob("scripts/*.py"), *ROOT.glob("model_library/*.py"), *ROOT.glob("release_spec/*.py")]:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if "sys.stderr" in line and ": ERROR: {" in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [])

    def test_start_verbose_tags_even_argument_errors(self):
        result = subprocess.run([str(ROOT / "pulsar"), "start", "ab" * 32, "--verbose", "--bogus"],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("[up] error: unknown argument: --bogus", result.stderr)


# Words the glossary in docs/OPERATIONS.md#terms retired from human output.
RETIRED = re.compile(r"\b[Pp]rofiles?\b|\bconf=|(?<!served-)model-name|[Hh]ot staging|[Cc]luster nodes?"
                     r"|[Rr]ecovery archive|[Cc]old recovery|[Aa]rchive directory|[Aa]rchive root\b")
MESSAGE = re.compile(r"\b(die|warn|error_line|usage_die|log|echo|print_hanging|field|emit|print|fail|ok|bad|record)\b"
                     r"[^#]*[\"']")
# Identifiers and legacy explanations, not operator wording.
ALLOWED = {"scripts/model_identity.py", "model_library/migration_views.py", "scripts/release_consumer.py"}


class Glossary(unittest.TestCase):
    def test_retired_terms_stay_out_of_messages(self):
        offenders = []
        sources = [*ROOT.glob("scripts/*.sh"), *ROOT.glob("scripts/*.py"), *ROOT.glob("cluster/*.sh"),
                   *ROOT.glob("model_library/*.py"), ROOT / "serve.sh", ROOT / "pulsar"]
        for path in sources:
            name = str(path.relative_to(ROOT))
            if name in ALLOWED:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                code = line.split(" #", 1)[0]
                if MESSAGE.search(code) and RETIRED.search(code) and "--" + "skip" not in code:
                    offenders.append(f"{name}:{number}: {line.strip()[:100]}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class Deprecations(unittest.TestCase):
    def run_pulsar(self, *args):
        return subprocess.run([str(ROOT / "pulsar"), *args], stdin=subprocess.DEVNULL,
                              text=True, capture_output=True, timeout=60)

    def test_each_alias_warns_once_and_matches_the_contract(self):
        declared = integration_contract.contract()["deprecated_commands"]
        for alias, entry in declared.items():
            with self.subTest(alias=alias):
                result = self.run_pulsar(*alias.split())
                self.assertEqual(result.returncode, 0, result.stderr)
                warning = (f"warning: pulsar {alias} is deprecated; use {entry['replacement']}. "
                           f"It is removed in CLI contract {entry['removed_in_cli_contract']}.\n")
                self.assertEqual(result.stderr.count(warning), 1, result.stderr)

    def test_wizard_without_a_terminal_lists_like_models(self):
        wizard = self.run_pulsar("wizard")
        models = self.run_pulsar("models")
        self.assertEqual(wizard.stdout, models.stdout)

    def test_current_commands_do_not_warn(self):
        for args in (["help"], ["models"], ["contract"]):
            with self.subTest(args=args):
                self.assertNotIn("deprecated", self.run_pulsar(*args).stderr)


if __name__ == "__main__":
    unittest.main()
