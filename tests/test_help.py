"""Operator help shares one style, fits a narrow terminal and covers the README."""
import io
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.terminal_format import TerminalWriter, emit_help

# The operator commands ./pulsar help lists for setup, catalog and serving.
COMMANDS = [
    ("help",), ("models", "--help"), ("model", "--help"), ("start", "--help"), ("stop", "--help"),
    ("status", "--help"), ("inventory", "--help"), ("doctor", "--help"), ("topology", "--help"),
    ("ssh-trust", "--help"), ("configure", "archive-root", "--help"), ("contract", "--help"),
]


class Help(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cache = {}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_help(self, args, width):
        if (args, width) not in self.cache:
            root = Path(self.tmp.name)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PULSAR_", "CLUSTER_"))}
            # Help never reaches a node, Docker or saved state; these doubles would fail if it did.
            env.update(COLUMNS=str(width), CLUSTER_TOPOLOGY_FILE=str(root / "absent-topology.json"),
                       PULSAR_MODEL_LIBRARY_DIR=str(root / "absent-library"),
                       PULSAR_SSH="/bin/false", PULSAR_DOCKER="/bin/false", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([str(ROOT / "pulsar"), *args], env=env, stdin=subprocess.DEVNULL,
                                    text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, (args, result.stderr))
            self.cache[(args, width)] = result.stdout
        return self.cache[(args, width)]

    def test_every_command_help_fits_44_columns(self):
        for args in COMMANDS:
            with self.subTest(command=" ".join(args)):
                text = self.run_help(args, 44)
                self.assertTrue(text.strip())
                self.assertEqual([line for line in text.splitlines() if len(line) > 44], [], text)

    def test_help_leads_with_usage_then_one_sentence_then_aligned_options(self):
        for args in (("models", "--help"), ("status", "--help"), ("stop", "--help"), ("start", "--help")):
            with self.subTest(command=args[0]):
                text = self.run_help(args, 80)
                lines = text.splitlines()
                self.assertTrue(lines[0].startswith(f"usage: pulsar {args[0]} "), text)
                blocks = text.split("\n\n")
                options = [line for line in lines if re.match(r"^  -", line) or re.match(r"^  [a-z]+( SPEC)?  ", line)]
                columns = {len(re.match(r"^(  \S+(?: \S+)*? {2,})", line).group(1)) for line in options}
                self.assertLessEqual(len(columns), 1, text)
                if args[0] != "start":  # start keeps its option list and notes unchanged
                    self.assertFalse(blocks[1].startswith(" "), text)
                    self.assertTrue(blocks[1].rstrip().endswith("."), text)

    def test_models_help_documents_its_commands_and_hides_internal_options(self):
        text = self.run_help(("models", "--help"), 80)
        for word in ("list", "show SPEC", "check SPEC", "menu", "--json", "--node"):
            self.assertIn(word, text)
        for internal in ("--repo-root", "--state-root", "REPO_ROOT"):
            self.assertNotIn(internal, text)

    def test_status_help_names_node_spec_file_and_json(self):
        text = self.run_help(("status", "--help"), 80)
        for word in ("--node NODE_ID", "--spec-file FILE", "--json"):
            self.assertIn(word, text)

    def test_top_level_help_uses_spec_terms(self):
        text = " ".join(self.run_help(("help",), 100).split())
        self.assertIn("pulsar memory verify --file FILE --spec-file FILE", text)
        self.assertIn("Author and inspect a complete JSON serving spec.", text)
        self.assertNotIn("--spec-file SPEC", text)

    def test_readme_example_commands_exist_in_help(self):
        readme = (ROOT / "README.md").read_text()
        commands = [line for block in re.findall(r"```sh\n(.*?)```", readme, re.S)
                    for line in block.splitlines() if line.startswith("./pulsar")]
        self.assertIn("./pulsar model prepare SPEC --node NODE --yes", commands)
        overview = self.run_help(("help",), 100)
        for line in commands:
            with self.subTest(line=line):
                tokens = shlex.split(line)[1:]
                if not tokens or tokens[0] == "help":
                    continue
                self.assertIn(f"pulsar {tokens[0]}", overview)
                text = self.run_help((tokens[0], "--help"), 100)
                for token in tokens[1:]:
                    if token.startswith("--"):
                        self.assertIn(token, text)
                    elif token.islower() and token.isalpha():
                        self.assertRegex(text, rf"\b{token}\b")


class Renderer(unittest.TestCase):
    TEXT = """\
usage: pulsar example SPEC_ID [--node NODE_ID] [--spec-file FILE]
       pulsar example --all

Do one thing to the exact spec on every participating node, with a sentence long enough to wrap.

  --node NODE_ID    One node
  --spec-file FILE  A candidate file with a description that is long enough to wrap
                    and a continuation line
  --json            JSON

A note.
  * a bullet
  * another bullet
"""

    def render(self, width):
        output = io.StringIO()
        emit_help(self.TEXT, TerminalWriter(width=width, stream=output))
        return output.getvalue()

    def test_options_align_and_wrap_under_their_column(self):
        text = self.render(80)
        self.assertIn("  --node NODE_ID    One node\n", text)
        self.assertIn("  --spec-file FILE  A candidate file with a description that is long enough to\n"
                      "                    wrap and a continuation line\n", text)
        self.assertIn("A note.\n  * a bullet\n  * another bullet\n", text)

    def test_narrow_width_keeps_bracket_groups_and_stacks_wide_options(self):
        text = self.render(32)
        self.assertTrue(all(len(line) <= 32 for line in text.splitlines()), text)
        self.assertIn("usage: pulsar example SPEC_ID\n         [--node NODE_ID]\n         [--spec-file FILE]\n"
                      "       pulsar example --all\n", text)
        self.assertIn("  --spec-file FILE  A candidate\n                    file with a\n", text)
        output = io.StringIO()
        emit_help("  --memory-estimate-file FILE  Explicit estimate of resident weights\n",
                  TerminalWriter(width=44, stream=output))
        self.assertEqual(output.getvalue(), "  --memory-estimate-file FILE\n"
                                            "      Explicit estimate of resident weights\n")


if __name__ == "__main__":
    unittest.main()
