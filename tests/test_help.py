"""Operator help shares one style, fits a narrow terminal and covers the README."""
import contextlib
import io
import json
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
from unittest import mock

from scripts import help_pages
from scripts.terminal_format import TerminalWriter, colors_enabled, emit_help, emit_laid_out_help, styles_enabled

# The operator commands ./pulsar help lists for setup, catalog and serving.
# The guide pages read docs/OPERATIONS.md, so a Terms table edit that breaks
# them fails here.
COMMANDS = [
    ("help",), ("help", "terms"), ("help", "workflow"),
    ("models", "--help"), ("model", "--help"), ("start", "--help"), ("stop", "--help"),
    ("status", "--help"), ("inventory", "--help"), ("doctor", "--help"), ("topology", "--help"),
    ("ssh-trust", "--help"), ("configure", "archive-root", "--help"), ("contract", "--help"),
    ("image", "--help"), ("guarded", "--help"),
    ("memory", "--help"), ("evidence", "--help"), ("privacy", "--help"), ("contribution", "--help"),
    ("observe", "--help"), ("policy", "--help"), ("selftest", "--help"),
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
        for args in (("models", "--help"), ("status", "--help"), ("stop", "--help"), ("start", "--help"),
                     ("image", "--help"), ("guarded", "--help"), ("memory", "--help"),
                     ("evidence", "--help"), ("privacy", "--help"), ("contribution", "--help")):
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

    def test_help_never_fails_with_or_without_json(self):
        # --help is for people: help on stdout and exit 0, even with --json. image and
        # guarded keep their usage dictionary under --json for integrations.
        for args in [*COMMANDS, ("resources", "--help")]:
            if args[0] in ("help", "image", "guarded"):
                continue
            for extra in ((), ("--json",)):
                with self.subTest(command=" ".join(args + extra)):
                    # run_help also requires exit 0.
                    self.assertRegex(self.run_help(args + extra, 80), r"^(?i:usage):")

    def test_models_help_documents_its_commands_and_hides_internal_options(self):
        text = self.run_help(("models", "--help"), 80)
        for word in ("list", "show SPEC", "check SPEC", "menu", "--json", "--node"):
            self.assertIn(word, text)
        for internal in ("--repo-root", "--state-root", "REPO_ROOT"):
            self.assertNotIn(internal, text)

    def test_status_help_names_node_spec_file_and_json(self):
        text = " ".join(self.run_help(("status", "--help"), 80).split())
        for word in ("--node NODE One-node spec: the recorded service's node, by hostname or node ID",
                     "--spec-file FILE", "--json"):
            self.assertIn(word, text)

    def test_command_groups_print_help_and_keep_their_json_usage(self):
        root = Path(self.tmp.name)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PULSAR_", "CLUSTER_"))}
        env.update(COLUMNS="80", CLUSTER_TOPOLOGY_FILE=str(root / "absent-topology.json"),
                   PULSAR_SSH="/bin/false", PULSAR_DOCKER="/bin/false", PYTHONDONTWRITEBYTECODE="1")
        for group, subcommands in (("image", {"check", "stage"}),
                                   ("guarded", {"template", "validate", "run", "stop", "reconcile"})):
            with self.subTest(command=group):
                self.assertFalse(self.run_help((group, "--help"), 80).lstrip().startswith("{"))
                bare = subprocess.run([str(ROOT / "pulsar"), group], env=env, stdin=subprocess.DEVNULL,
                                      text=True, capture_output=True, timeout=60)
                self.assertEqual(bare.returncode, 2)
                self.assertEqual(bare.stdout, "")
                self.assertTrue(bare.stderr.startswith(f"usage: pulsar {group} "), bare.stderr)
                # Integrations keep the usage dictionary under --json.
                for args in ([group, "--help", "--json"], [group, "--json"]):
                    result = subprocess.run([str(ROOT / "pulsar"), *args], env=env, stdin=subprocess.DEVNULL,
                                            text=True, capture_output=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    envelope = json.loads(result.stdout)
                    self.assertTrue(envelope["ok"])
                    self.assertEqual(set(envelope["result"]), subcommands | {"scope"})

    def test_subcommand_help_lists_every_subcommand_under_its_pulsar_name(self):
        from scripts.public_cli import SUBCOMMANDS
        for command, (_, subcommands) in SUBCOMMANDS.items():
            with self.subTest(command=command):
                text = self.run_help((command, "--help"), 80)
                for name in subcommands:
                    self.assertRegex(text, rf"(?m)^  {name}  ")
                    # Each listed subcommand's own help names it as a pulsar command.
                    self.assertTrue(self.run_help((command, name, "--help"), 80)
                                    .startswith(f"usage: pulsar {command} {name} "))

    def test_top_level_help_uses_spec_terms(self):
        text = " ".join(self.run_help(("help",), 100).split())
        self.assertIn("pulsar memory verify --file FILE --spec-file FILE", text)
        self.assertIn("Author and inspect a complete JSON serving spec.", text)
        self.assertNotIn("--spec-file SPEC", text)

    def test_reference_lists_the_guides_and_stop_all(self):
        text = self.run_help(("help",), 100)
        self.assertLess(text.index("Guides"), text.index("Set up this checkout"))
        for command in ("pulsar help terms", "pulsar help workflow", "pulsar stop --all --yes"):
            self.assertRegex(text, rf"(?m)^  {re.escape(command)}$")

    def test_terms_page_shows_every_row_of_the_docs_table(self):
        lines = (ROOT / "docs/OPERATIONS.md").read_text().split("## Terms\n", 1)[1].split("\n## ", 1)[0]
        # The header and separator rows come first.
        rows = [line.strip("|").split(" | ") for line in lines.splitlines() if line.startswith("| ")][2:]
        self.assertGreaterEqual(len(rows), 16)
        text = self.run_help(("help", "terms"), 100)
        terms = re.findall(r"(?m)^  (\S.*)$", text)
        self.assertEqual(terms, [term.strip().replace("`", "") for term, _ in rows])
        joined = " ".join(text.split())
        for _, meaning in rows:
            self.assertIn(" ".join(meaning.replace("`", "").split()), joined)
        # The words the setup steps use are explained.
        for word in ("membership", "SSH trust", "fabric", "controller", "service"):
            self.assertIn(word, terms)
        for marker in ("`", "**", "]("):
            self.assertNotIn(marker, text)

    def test_workflow_page_has_one_sentence_per_step_in_lifecycle_order(self):
        text = self.run_help(("help", "workflow"), 100)
        steps = re.findall(r"(?m)^  (\d)\. (.+)$", text)
        self.assertEqual(steps, [("1", "Download or Restore"), ("2", "Prepare"), ("3", "Start"),
                                 ("4", "Connect"), ("5", "Stop"), ("6", "Purge prepared copies")])
        for step, sentence in help_pages.workflow():
            with self.subTest(step=step):
                self.assertTrue(sentence.endswith(".") and ". " not in sentence, sentence)
        self.assertIn("preparing does not start a service", " ".join(text.split()))

    def test_unknown_help_topic_names_the_topics(self):
        result = subprocess.run([str(ROOT / "pulsar"), "help", "glossary"], stdin=subprocess.DEVNULL,
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "Unknown help topic: glossary\n"
                                        "Try: ./pulsar help, ./pulsar help terms or ./pulsar help workflow\n")

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


class Guide(unittest.TestCase):
    """The terms page reads a Terms table; anything else is an error naming the line."""
    TABLE = ("# Guide\n\n## Terms\n\nIntro.\n\n| Term | Meaning |\n| --- | --- |\n"
             "| spec | The `releases/` spec, see [Terms](#terms). |\n| pipe | Shown as `a \\| b`, **always**. |\n"
             "\n## Next\n\n| not | a term |\n")

    def page(self, text):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "OPERATIONS.md"
        if text is not None:
            path.write_text(text)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, {"PULSAR_COLOR": "never"}):
            rc = help_pages.main(["terms", "--file", str(path)])
        return rc, out.getvalue(), err.getvalue()

    def test_rows_become_plain_text_and_the_table_ends_at_the_next_heading(self):
        rc, out, err = self.page(self.TABLE)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("  spec\n    The releases/ spec, see Terms.\n  pipe\n    Shown as a | b, always.\n", out)
        self.assertNotIn("not", out.split("pipe", 1)[1])

    def test_a_table_it_cannot_read_is_an_error_not_an_empty_page(self):
        cases = {
            "missing file": (None, "cannot read "),
            "no section": (self.TABLE.replace("## Terms", "## Words"), "has no '## Terms' section"),
            "no table": ("## Terms\n\nIntro only.\n", "the Terms section has no table rows"),
            "header": (self.TABLE.replace("| Term | Meaning |", "| Word | Meaning |"),
                       "line 7: the Terms table's header must read 'Term | Meaning'"),
            "separator": (self.TABLE.replace("| --- | --- |", "| spec | x |"),
                          "line 8: expected the Terms table's separator row"),
            "extra cell": (self.TABLE.replace("| spec | The", "| spec | extra | The"),
                           "line 9: a Terms row needs 2 cells, a term and its meaning; found 3"),
            "empty meaning": (self.TABLE.replace("| pipe | Shown as `a \\| b`, **always**. |", "| pipe |  |"),
                              "line 10: a Terms row has an empty meaning"),
        }
        for name, (text, message) in cases.items():
            with self.subTest(name):
                rc, out, err = self.page(text)
                self.assertEqual(rc, 1)
                self.assertEqual(out, "")
                self.assertTrue(err.startswith("error: ") and message in err, err)
                self.assertEqual(len(err.splitlines()), 1, err)


ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Styles(unittest.TestCase):
    """A terminal gets styles after clig.dev, NO_COLOR and man-pages(7): headings
    bold in the accent, menu choices bold, repo terms in the accent, literal text
    cyan and placeholders underlined. Without them the text is unchanged."""

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    def env(self, **values):
        clean = {key: value for key, value in os.environ.items()
                 if key not in ("PULSAR_COLOR", "NO_COLOR", "PULSAR_ACCENT", "TERM")}
        return mock.patch.dict(os.environ, {**clean, **values}, clear=True)

    def render(self, text, width=80, role=None, **values):
        out = self.Terminal()
        with self.env(TERM="xterm-256color", **values):
            TerminalWriter(width=width, stream=out).emit(text, initial_indent="  ", role=role, markup=role is None)
        return out.getvalue()

    def page(self, topic, width=80, **values):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PULSAR_", "CLUSTER_")) and key != "NO_COLOR"}
        env.update(COLUMNS=str(width), PYTHONDONTWRITEBYTECODE="1", **values)
        args = ["help"] if topic == "reference" else ["help", topic]
        result = subprocess.run([str(ROOT / "pulsar"), *args], env=env, stdin=subprocess.DEVNULL,
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def command(self, args, width=80, **values):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PULSAR_", "CLUSTER_")) and key != "NO_COLOR"}
        env.update(COLUMNS=str(width), PYTHONDONTWRITEBYTECODE="1", PULSAR_SSH="/bin/false",
                   PULSAR_DOCKER="/bin/false", **values)
        result = subprocess.run([str(ROOT / "pulsar"), *args], env=env, stdin=subprocess.DEVNULL,
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_every_command_help_strips_to_its_plain_text(self):
        from scripts.public_cli import SUBCOMMANDS
        screens = [args for args in COMMANDS if args[0] != "help"]
        screens += [(command, name, "--help") for command, (_, names) in SUBCOMMANDS.items() for name in names]
        for args in screens:
            with self.subTest(command=" ".join(args)):
                plain = self.command(args, 44)
                self.assertNotIn("\x1b", plain)
                self.assertEqual(ANSI.sub("", self.command(args, 44, PULSAR_COLOR="always")), plain)

    def test_command_help_marks_usage_option_terms_and_metavariables(self):
        expected = {
            ("models", "--help"): ["\x1b[1;94musage:\x1b[0m\x1b[36m pulsar models list [--json]\x1b[0m\n",
                                   "  \x1b[36mshow \x1b[0m\x1b[4;36mSPEC\x1b[0m     One spec in detail",
                                   "\nnever contact nodes.\n"],
            ("start", "--help"): ["\x1b[36m pulsar start \x1b[0m\x1b[4;36mSPEC_ID\x1b[0m",
                                  "  \x1b[36m--spec-file \x1b[0m\x1b[4;36mFILE\x1b[0m             Use"],
            ("guarded", "--help"): ["\x1b[4;36mSHA256\x1b[0m", "\x1b[4;36mNEW_DIR\x1b[0m"],
            ("model", "--help"): ["\x1b[1;94mUsage:\x1b[0m\x1b[36m pulsar model \x1b[0m\x1b[4;36mOPERATION\x1b[0m",
                                  "\n\x1b[1;94mOptions:\x1b[0m\n",
                                  "\x1b[4;36mORG\x1b[0m\x1b[36m/\x1b[0m\x1b[4;36mNAME\x1b[0m",
                                  "  \x1b[36m--placement-nodes \x1b[0m\x1b[4;36mLIST\x1b[0m  Ordered"],
            ("topology", "--help"): ["\x1b[1;94mUsage:\x1b[0m\x1b[36m pulsar topology \x1b[0m\x1b[4;36mCOMMAND\x1b[0m",
                                     "  \x1b[36msetup [--candidate \x1b[0m\x1b[4;36mHOST\x1b[0m\x1b[36m ...]\x1b[0m\n"],
            ("doctor", "--help"): ["\x1b[1;94mUsage:\x1b[0m\x1b[36m ./pulsar doctor [--json]\x1b[0m\n"],
        }
        for args, parts in expected.items():
            styled = self.command(args, 80, PULSAR_COLOR="always")
            for part in parts:
                with self.subTest(command=" ".join(args), part=part):
                    self.assertIn(part, styled)

    def test_styles_follow_the_terminal_and_the_operator(self):
        cases = [  # environment, a terminal?, (styles, colors)
            ({"TERM": "xterm-256color"}, True, (True, True)),
            ({"TERM": "xterm-256color", "NO_COLOR": "1"}, True, (True, False)),
            ({"TERM": "xterm-256color", "NO_COLOR": ""}, True, (True, True)),
            ({"TERM": "xterm-256color", "PULSAR_COLOR": "never"}, True, (False, False)),
            ({"TERM": "dumb"}, True, (False, False)),
            ({}, True, (False, False)),
            ({"TERM": "xterm-256color"}, False, (False, False)),
            ({"PULSAR_COLOR": "always"}, False, (True, True)),
            ({"PULSAR_COLOR": "always", "NO_COLOR": "1"}, False, (True, False)),
        ]
        for values, terminal, expected in cases:
            with self.subTest(values=values, terminal=terminal), self.env(**values):
                stream = self.Terminal() if terminal else io.StringIO()
                self.assertEqual((styles_enabled(stream), colors_enabled(stream)), expected)

    def test_each_mark_takes_its_style(self):
        text = "**Start** runs `pulsar start SPEC --node NODE` for a *service*; `PULSAR_COLOR=never` is literal."
        self.assertEqual(self.render(text, width=100),
                         "  \x1b[1mStart\x1b[0m runs \x1b[36mpulsar start \x1b[0m\x1b[4;36mSPEC\x1b[0m"
                         "\x1b[36m --node \x1b[0m\x1b[4;36mNODE\x1b[0m for a \x1b[94mservice\x1b[0m; "
                         "\x1b[36mPULSAR_COLOR=never\x1b[0m is literal.\n")
        # NO_COLOR keeps bold and underline (no-color.org, FAQ).
        self.assertEqual(self.render(text, width=100, NO_COLOR="1"),
                         "  \x1b[1mStart\x1b[0m runs pulsar start \x1b[4mSPEC\x1b[0m --node \x1b[4mNODE\x1b[0m "
                         "for a service; PULSAR_COLOR=never is literal.\n")
        self.assertEqual(self.render(text, width=100, PULSAR_COLOR="never"),
                         "  Start runs pulsar start SPEC --node NODE for a service; PULSAR_COLOR=never is literal.\n")

    def test_headings_use_the_menu_accent(self):
        for accent, sgr in (("4", "34"), ("12", "94"), ("208", "38;5;208"), ("#5f87ff", "38;2;95;135;255"),
                            ("teal", "94")):
            with self.subTest(accent=accent):
                self.assertEqual(self.render("Guides", role="heading", PULSAR_ACCENT=accent),
                                 f"  \x1b[1;{sgr}mGuides\x1b[0m\n")
        self.assertEqual(self.render("Guides", role="heading", NO_COLOR="1"), "  \x1b[1mGuides\x1b[0m\n")

    def test_a_styled_line_wraps_like_the_plain_one(self):
        text = "Run `./pulsar topology check` to test every pair of nodes, since *membership* is only saved."
        for width in (32, 44, 60):
            with self.subTest(width=width):
                styled = self.render(text, width)
                plain = self.render(text, width, PULSAR_COLOR="never")
                self.assertEqual(ANSI.sub("", styled), plain)
                self.assertNotIn(" ", styled + plain)
                self.assertTrue(all(len(line) <= width for line in plain.splitlines()), plain)
                # A literal that fits on a line never breaks inside.
                self.assertIn("./pulsar topology check", plain)
        heading = self.render("A heading long enough to wrap onto a second line", width=32, role="heading")
        self.assertEqual(heading, "  \x1b[1;94mA heading long enough to wrap\x1b[0m\n"
                                  "  \x1b[1;94monto a second line\x1b[0m\n")

    def test_pages_strip_to_their_plain_text_and_fit(self):
        for topic in ("reference", "terms", "workflow"):
            for width in (44, 60, 80):
                with self.subTest(topic=topic, width=width):
                    styled = self.page(topic, width, PULSAR_COLOR="always")
                    plain = self.page(topic, width)
                    self.assertNotIn("\x1b", plain)
                    self.assertEqual(ANSI.sub("", styled), plain)
                    self.assertTrue(all(len(line) <= width for line in plain.splitlines()))

    def test_pages_mark_headings_terms_choices_and_commands(self):
        expected = {
            "reference": ["\x1b[1;94mPulsar Inference Stack\x1b[0m\n", "\n\x1b[1;94mGuides\x1b[0m\n",
                          "  \x1b[36mpulsar models show \x1b[0m\x1b[4;36mSPEC\x1b[0m\n", "\x1b[1mHelp\x1b[0m",
                          "\x1b[4;36mSPEC\x1b[0m is a catalog spec ID."],
            "terms": ["\x1b[1;94mWhat the words mean\x1b[0m\n", "  \x1b[1;94mspec\x1b[0m\n",
                      "\x1b[36mreleases/\x1b[0m", "\x1b[36m./pulsar topology check\x1b[0m",
                      "\x1b[36mdocs/OPERATIONS.md\x1b[0m"],
            "workflow": ["\x1b[1;94mHow serving works\x1b[0m\n", "  \x1b[1;94m1. Download or Restore\x1b[0m\n",
                         "\x1b[94mhome\x1b[0m", "\x1b[1mRestore\x1b[0m", "\x1b[36mpulsar model acquire\x1b[0m"],
        }
        for topic, parts in expected.items():
            styled = self.page(topic, 100, PULSAR_COLOR="always")
            muted = self.page(topic, 100, PULSAR_COLOR="always", NO_COLOR="1")
            for part in parts:
                with self.subTest(topic=topic, part=part):
                    self.assertIn(part, styled)
            # Without color, headings stay bold and placeholders underlined.
            self.assertNotRegex(muted, r"\x1b\[[0-9;]*(3[0-9]|9[0-7])m")
            self.assertIn("\x1b[1m", muted)


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

    LAID_OUT = """\
Usage: pulsar example OPERATION [SPEC_ID] [options]

  acquire       Download an exact snapshot
  archive create|verify   Save or verify an archive

Options:
  --model-id ORG/NAME --model-commit COMMIT   First acquisition before a spec
  --one SPACE Not an option row
Archive location is explicit PULSAR_COLD_ROOT.
"""

    def render(self, width, text=None, render=emit_help, terminal=False, **values):
        output = Styles.Terminal() if terminal else io.StringIO()
        clean = {key: value for key, value in os.environ.items()
                 if key not in ("PULSAR_COLOR", "NO_COLOR", "PULSAR_ACCENT", "TERM")}
        values = values or ({"TERM": "xterm-256color"} if terminal else {"PULSAR_COLOR": "never"})
        with mock.patch.dict(os.environ, {**clean, **values}, clear=True):
            render(self.TEXT if text is None else text, TerminalWriter(width=width, stream=output))
        return output.getvalue()

    def test_a_help_screen_styles_its_usage_and_option_terms_only(self):
        for width in (80, 32):
            with self.subTest(width=width):
                styled = self.render(width, terminal=True)
                self.assertEqual(ANSI.sub("", styled), self.render(width))
                # Paragraphs and bullets stay plain.
                self.assertIn("\nA note.\n  * a bullet\n  * another bullet\n", styled)
        styled = self.render(80, terminal=True)
        self.assertIn("\x1b[1;94musage:\x1b[0m\x1b[36m pulsar example \x1b[0m\x1b[4;36mSPEC_ID\x1b[0m"
                      "\x1b[36m [--node \x1b[0m\x1b[4;36mNODE_ID\x1b[0m\x1b[36m] [--spec-file \x1b[0m"
                      "\x1b[4;36mFILE\x1b[0m\x1b[36m]\x1b[0m\n       \x1b[36mpulsar example --all\x1b[0m\n", styled)
        self.assertIn("  \x1b[36m--node \x1b[0m\x1b[4;36mNODE_ID\x1b[0m    One node\n", styled)
        # A wide option stacks; its term is styled on its own line.
        stacked = self.render(44, "usage: pulsar x\n\n  --memory-estimate-file FILE  Explicit estimate\n",
                              terminal=True)
        self.assertIn("  \x1b[36m--memory-estimate-file \x1b[0m\x1b[4;36mFILE\x1b[0m\n      Explicit estimate\n",
                      stacked)

    def test_text_without_a_usage_line_is_never_styled(self):
        # Questions and the SSH help pass through here too.
        for text in ("\nPurge fixture/model: delete 1 working copy?\n", "Options:\n  --node NODE  One node\n"):
            with self.subTest(text=text):
                self.assertEqual(self.render(80, text, terminal=True), self.render(80, text))

    def test_a_laid_out_help_screen_keeps_its_lines_and_styles_them(self):
        expected = io.StringIO()
        with mock.patch.dict(os.environ, {"PULSAR_COLOR": "never"}):
            writer = TerminalWriter(width=60, stream=expected)
            for line in self.LAID_OUT.splitlines():
                writer.emit(line.rstrip(), subsequent_indent="    " if line.startswith("  ") else "")
        self.assertEqual(self.render(60, self.LAID_OUT, emit_laid_out_help), expected.getvalue())
        styled = self.render(60, self.LAID_OUT, emit_laid_out_help, terminal=True)
        self.assertEqual(ANSI.sub("", styled), expected.getvalue())
        self.assertIn("\x1b[1;94mUsage:\x1b[0m\x1b[36m pulsar example \x1b[0m\x1b[4;36mOPERATION\x1b[0m", styled)
        self.assertIn("  \x1b[36marchive create|verify\x1b[0m   Save or verify an archive\n", styled)
        self.assertIn("\n\x1b[1;94mOptions:\x1b[0m\n", styled)
        self.assertIn("\x1b[36m--model-id \x1b[0m\x1b[4;36mORG\x1b[0m\x1b[36m/\x1b[0m\x1b[4;36mNAME\x1b[0m", styled)
        # One space is not a column, and a paragraph's capitals are not metavariables.
        self.assertIn("\n  --one SPACE Not an option row\nArchive location is explicit PULSAR_COLD_ROOT.\n", styled)

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
