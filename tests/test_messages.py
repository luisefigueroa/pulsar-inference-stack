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
                     r"|[Rr]ecovery archive|[Cc]old recovery|[Aa]rchive directory|[Aa]rchive root\b"
                     r"|[Ww]eight (source|provenance)")
# Any quoted string on a non-comment line: messages are often built in tuples,
# variables or continuation lines, away from the helper that prints them.
STRING = re.compile(r"\"[^\"]*\"|'[^']*'")
# Identifiers and legacy explanations, not operator wording.
ALLOWED = {"scripts/model_identity.py", "model_library/migration_views.py", "scripts/release_consumer.py"}
# Messages that explain a removed legacy concept by its old name.
LEGACY_LINES = ("REMOVED_LIST_VALIDATED_MESSAGE=",)
DOCSTRING = ('"' * 3, "'" * 3)


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
                if line.lstrip().startswith("#"):
                    continue
                if line.lstrip().startswith(DOCSTRING) or any(marker in line for marker in LEGACY_LINES):
                    continue
                for text in STRING.findall(line.split(" # ", 1)[0]):
                    # Sentences only: identifiers, keys and paths have no spaces,
                    # and {...} inside an f-string is code, not wording.
                    text = re.sub(r"\{[^}]*\}", "", text)
                    if " " in text and RETIRED.search(text):
                        offenders.append(f"{name}:{number}: {line.strip()[:100]}")
                        break
        self.assertEqual(offenders, [], "\n".join(offenders))


class ConfLabels(unittest.TestCase):
    def test_spec_ids_and_legacy_configurations_are_named_differently(self):
        result = shell('conf_label_display ' + "ab" * 32 + '; echo; conf_label_display qwen-legacy-tp2')
        self.assertEqual(result.stdout, "spec abababababab\nlegacy configuration 'qwen-legacy-tp2'")


class RanksWithNodes(unittest.TestCase):
    def test_fabric_error_names_the_node_with_its_rank(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from test_transfer import topology
        from scripts import topology_manifest
        document = topology(2)
        document["nodes"][0]["rdma"] = []
        document["nodes"][0]["hostname"] = "spark-1"
        document["topology_id"] = topology_manifest.topology_digest(document)
        with self.assertRaisesRegex(topology_manifest.TopologyError, r"^spark-1 \(rank 0\): no RDMA HCA"):
            topology_manifest.profile_fabric(document, 1)


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

    def test_each_retired_flag_warns_once_and_matches_the_contract(self):
        # Each invocation stops at an argument check, before any lock or node.
        runs = {"stop --retain-weights": ["bash", str(ROOT / "scripts/down.sh"), "abc", "--retain-weights"],
                "observe --spec-file": [str(ROOT / "pulsar"), "observe", "--spec-file", "/tmp/candidate.json",
                                        "--verification-jobs", "0", "--json"]}
        declared = integration_contract.contract()["deprecated_flags"]
        self.assertEqual(set(declared), set(runs))
        for flag, entry in declared.items():
            with self.subTest(flag=flag):
                result = subprocess.run(runs[flag], stdin=subprocess.DEVNULL, text=True, capture_output=True,
                                        timeout=60)
                self.assertEqual(result.returncode, 2, result.stderr)
                warning = (f"warning: pulsar {flag} is deprecated: {entry['note']}. "
                           f"It is removed in CLI contract {entry['removed_in_cli_contract']}.\n")
                self.assertEqual(result.stderr.count(warning), 1, result.stderr)

    def test_release_list_warns_wherever_list_appears(self):
        result = self.run_pulsar("release", "--json", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("warning: pulsar release list is deprecated"), 1, result.stderr)

    def test_menus_refuse_json_and_name_the_command_that_prints_it(self):
        for args, hint in ((["models", "menu", "--json"], "./pulsar models list --json"),
                           (["topology", "menu", "--json"], "./pulsar topology show --json"),
                           (["configure", "archive-root", "menu", "--json"], "./pulsar configure archive-root show --json"),
                           (["gum", "--json"], "./pulsar help")):
            with self.subTest(args=args):
                result = self.run_pulsar(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f"is interactive; use {hint}", result.stderr)

    def test_an_unknown_inventory_argument_is_a_usage_error(self):
        result = self.run_pulsar("inventory", "--bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument: --bogus", result.stderr)

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
