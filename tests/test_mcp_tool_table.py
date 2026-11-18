"""The MCP server's tool table: what is shipped, that it is read-only by construction,
and that the package keeps to itself.

The table's output forms are literals, because the package imports nothing
that knows the contract. These tests compare them with the contract.
"""
import ast
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp_server import __main__ as server, protocol, tool_table
from mcp_server.tool_table import Argument, Refused, Tool, operation_tool
from scripts import integration_contract

OPERATION_TOOLS = ["pulsar_contract", "pulsar_topology_show", "pulsar_models_show"]
SERVER_TOOLS = ["pulsar_mcp_server_info", "pulsar_command_get", "pulsar_command_cancel", "pulsar_command_list"]
# Every option a tool of the shipped table can put on a command line. Anything else fails.
ALLOWED_FLAGS = {"--json"}
# Options no tool may ever produce: each one changes something, or takes away a check.
NEVER_PRODUCED = {"--yes", "--replace", "--pull-image", "--pull", "--export-tag", "--accept-memory-warn",
                  "--skip-preflight", "--follow", "--candidate", "--accept-new-host-keys", "--accept-key-change",
                  "--remove-missing", "--discard-unpromoted", "--out", "--manifest-out"}
SPEC = "0123456789abcdef" * 4
LIFECYCLE_PACKAGES = {"model_library", "release_spec", "serving_guard"}


class ShippedTable(unittest.TestCase):
    def setUp(self):
        self.tools = {tool.name: tool for tool in tool_table.TOOLS}
        self.contract = integration_contract.contract()

    def test_the_table_holds_exactly_three_operation_tools_and_the_four_server_tools(self):
        self.assertEqual([tool.name for tool in tool_table.TOOLS], OPERATION_TOOLS + SERVER_TOOLS)
        self.assertEqual([tool.name for tool in tool_table.TOOLS if tool.operation], OPERATION_TOOLS)
        self.assertEqual([tool.action for tool in tool_table.TOOLS if tool.action],
                         ["server_info", "command_get", "command_cancel", "command_list"])
        self.assertEqual({tool.grant for tool in tool_table.TOOLS}, {"read"})

    def test_each_operation_tool_runs_an_operation_of_the_contract_in_its_output_form(self):
        outputs = self.contract["operation_outputs"]
        for name in OPERATION_TOOLS:
            tool = self.tools[name]
            with self.subTest(tool=name):
                self.assertIn(tool.operation, self.contract["operations"])
                self.assertEqual(tool.output_form, outputs[tool.operation]["form"])
                # A command is named after its operation, with spaces for the dots.
                self.assertEqual(" ".join(tool.command), tool.operation.replace(".", " "))
                self.assertEqual(name, tool_table.tool_name(tool.operation, tool.fixed_flags))
        self.assertEqual(tool_table.tool_name("ssh-trust.check"), "pulsar_ssh_trust_check")
        self.assertEqual(tool_table.tool_name("model.acquire", ("--plan",)), "pulsar_model_acquire_plan")
        self.assertEqual(tool_table.tool_name("start", ("--dry-run",)), "pulsar_start_dry_run")
        # What pulsar_mcp_server_info runs is the contract tool's own command line.
        self.assertEqual(list(server.CONTRACT_COMMAND), self.tools["pulsar_contract"].command_line({}))
        self.assertEqual(outputs["contract"]["form"], "envelope")

    def test_the_command_lines_are_fixed_and_json_is_added_by_the_table(self):
        self.assertEqual(self.tools["pulsar_contract"].command_line({}), ["contract", "--json"])
        self.assertEqual(self.tools["pulsar_topology_show"].command_line({}), ["topology", "show", "--json"])
        models_show = self.tools["pulsar_models_show"]
        values = models_show.checked_arguments({"spec_id": SPEC, "wait_seconds": 5})
        # wait_seconds is the server's; it never reaches a command line.
        self.assertEqual(models_show.command_line(values), ["models", "show", SPEC, "--json"])
        for tool in tool_table.TOOLS:
            names = {argument.name for argument in tool.all_arguments}
            self.assertFalse(names & {"json", "--json"}, tool.name)  # The caller cannot add or drop it.
            if tool.action:
                self.assertEqual((tool.command, tool.fixed_flags, tool.arguments, tool.flags()), ((), (), (), set()))

    def test_every_flag_a_tool_can_produce_is_on_the_allow_list(self):
        produced = set()
        for tool in tool_table.TOOLS:
            produced |= tool.flags()
        self.assertEqual(produced, ALLOWED_FLAGS)
        self.assertFalse(ALLOWED_FLAGS & NEVER_PRODUCED)
        for tool in tool_table.TOOLS:
            # No word of a command line starts with "-" but the listed flags.
            fixed = [*tool.command, *tool.fixed_flags]
            self.assertEqual({word for word in fixed if word.startswith("-")} - ALLOWED_FLAGS, set(), tool.name)

    def test_only_a_tool_that_saves_and_ends_nothing_is_marked_read_only(self):
        hints = {tool["name"]: tool["annotations"] for tool in map(Tool.listed, tool_table.TOOLS)}
        for name in OPERATION_TOOLS + ["pulsar_mcp_server_info", "pulsar_command_get", "pulsar_command_list"]:
            self.assertEqual(hints[name], {"readOnlyHint": True}, name)
        self.assertEqual(hints["pulsar_command_cancel"], {"readOnlyHint": False, "destructiveHint": False})
        saving = operation_tool("pulsar_inventory", "inventory", "inventory", "document", "A test entry.",
                                saves="the inventory, on the controller")
        self.assertIs(saving.listed()["annotations"]["readOnlyHint"], False)

    def test_each_tool_is_listed_with_a_closed_schema_of_its_arguments(self):
        listed = {tool.name: tool.listed() for tool in tool_table.TOOLS}
        for name, entry in listed.items():
            with self.subTest(tool=name):
                self.assertEqual(set(entry), {"name", "description", "inputSchema", "annotations"})
                self.assertRegex(entry["name"], r"^pulsar_[a-z_]+$")
                schema = entry["inputSchema"]
                self.assertEqual((schema["type"], schema["additionalProperties"]), ("object", False))
        wait = {"type": "number", "minimum": 0, "maximum": 240}
        schema = listed["pulsar_models_show"]["inputSchema"]
        self.assertEqual(list(schema["properties"]), ["spec_id", "wait_seconds"])
        self.assertEqual(schema["required"], ["spec_id"])
        self.assertEqual(schema["properties"]["spec_id"]["pattern"], "^[0-9a-f]{64}$")
        self.assertEqual({key: schema["properties"]["wait_seconds"][key] for key in wait}, wait)
        self.assertNotIn("required", listed["pulsar_contract"]["inputSchema"])
        self.assertEqual(list(listed["pulsar_command_get"]["inputSchema"]["properties"]), ["command_id", "wait_seconds"])
        self.assertEqual(listed["pulsar_command_cancel"]["inputSchema"]["required"], ["command_id"])
        self.assertEqual(listed["pulsar_command_list"]["inputSchema"]["properties"], {})
        self.assertEqual(listed["pulsar_mcp_server_info"]["inputSchema"]["properties"], {})
        self.assertEqual((tool_table.WAIT_SECONDS_DEFAULT, tool_table.WAIT_SECONDS_MINIMUM,
                          tool_table.WAIT_SECONDS_MAXIMUM), (60, 0, 240))

    def test_each_description_says_what_the_tool_reaches_and_saves(self):
        for name in OPERATION_TOOLS:
            description = self.tools[name].description
            self.assertRegex(description, r"(Reaches nothing|contacts no node)", name)
            self.assertIn("saves nothing", description, name)
        # No description names a real place: the table is a tracked file.
        for tool in tool_table.TOOLS:
            self.assertNotRegex(tool.description, r"/home/|\d+\.\d+\.\d+\.\d+", tool.name)


class ArgumentRules(unittest.TestCase):
    """The rules hold for any table. The entries here are test entries; none is shipped."""

    def tool(self, *arguments, **fields):
        return operation_tool("pulsar_fixture", "fixture", "fixture run", "envelope", "A test entry.",
                              arguments=arguments, **fields)

    def refused(self, tool, arguments, message):
        with self.assertRaises(Refused) as caught:
            tool.command_line(tool.checked_arguments(arguments))
        self.assertIn(message, str(caught.exception))

    def test_a_spec_id_is_64_lower_case_hexadecimal_characters(self):
        tool = self.tool(Argument("spec_id", "spec_id", "A spec.", required=True))
        self.assertEqual(tool.command_line(tool.checked_arguments({"spec_id": SPEC})), ["fixture", "run", SPEC, "--json"])
        for value in (SPEC[:63], SPEC + "0", SPEC.upper(), "g" * 64, SPEC[:12], "-" + SPEC[1:], SPEC + "\n",
                      " " + SPEC[1:], "", None, 7, [SPEC]):
            with self.subTest(value=value):
                self.refused(tool, {"spec_id": value}, "argument spec_id must be")

    def test_a_path_is_absolute(self):
        tool = self.tool(Argument("file", "path", "A file.", flag="--file"))
        self.assertEqual(tool.command_line(tool.checked_arguments({"file": "/fixture/spec.json"})),
                         ["fixture", "run", "--file", "/fixture/spec.json", "--json"])
        for value in ("spec.json", "./spec.json", "~/spec.json", "../spec.json", "", "-/x", "--yes", "/x\0y"):
            with self.subTest(value=value):
                self.refused(tool, {"file": value}, "argument file must")

    def test_a_number_has_a_range(self):
        tool = self.tool(Argument("jobs", "integer", "Jobs.", flag="--verification-jobs", minimum=1, maximum=8))
        self.assertEqual(tool.command_line(tool.checked_arguments({"jobs": 8})),
                         ["fixture", "run", "--verification-jobs", "8", "--json"])
        for value in (0, 9, -1, 10 ** 400, 2.0, 1.5, True, "3", None):
            with self.subTest(value=value):
                self.refused(tool, {"jobs": value}, "argument jobs must be")
        wait = tool_table.WAIT_SECONDS
        self.assertEqual([wait.checked(value) for value in (0, 0.25, 60, 240)], [0, 0.25, 60, 240])
        for value in (-1, -0.001, 240.5, float("inf"), float("nan"), True, "60"):
            with self.subTest(wait_seconds=value):
                with self.assertRaises(Refused):
                    wait.checked(value)
        # A number without a range, or with one that reaches below zero, cannot be written into a table.
        for limits in ({}, {"minimum": 1}, {"minimum": -5, "maximum": 5}, {"minimum": 3, "maximum": 2}):
            with self.assertRaises(ValueError):
                Argument("jobs", "integer", "Jobs.", flag="--jobs", **limits)

    def test_a_choice_is_one_of_its_listed_values(self):
        tool = self.tool(Argument("policy", "choice", "A policy.", choices=("baseline-v1", "baseline-v2")))
        self.assertEqual(tool.command_line(tool.checked_arguments({"policy": "baseline-v2"})),
                         ["fixture", "run", "baseline-v2", "--json"])
        for value in ("baseline-v3", "", "--yes", "BASELINE-V1", 1, None):
            with self.subTest(value=value):
                self.refused(tool, {"policy": value}, "argument policy must be")
        with self.assertRaises(ValueError):
            Argument("policy", "choice", "A policy.")

    def test_a_value_that_starts_with_a_dash_is_refused(self):
        tool = self.tool(Argument("node", "text", "A node.", flag="--node"),
                         Argument("name", "text", "A name."))
        self.assertEqual(tool.command_line(tool.checked_arguments({"node": "fixture-node-1", "name": "a b"})),
                         ["fixture", "run", "a b", "--node", "fixture-node-1", "--json"])
        for value in ("-", "--yes", "-n", "--node=fixture-node-2", "--", ""):
            for name in ("node", "name"):
                with self.subTest(name=name, value=value):
                    self.refused(tool, {name: value}, f"argument {name} must")

    def test_a_flag_is_true_or_false_and_fixed_flags_are_always_there(self):
        tool = self.tool(Argument("full", "flag", "Hash every file.", flag="--full"), fixed_flags=("--plan",))
        self.assertEqual(tool.command_line(tool.checked_arguments({"full": True})),
                         ["fixture", "run", "--full", "--plan", "--json"])
        for arguments in ({"full": False}, {}):
            self.assertEqual(tool.command_line(tool.checked_arguments(arguments)), ["fixture", "run", "--plan", "--json"])
        self.assertEqual(tool.flags(), {"--full", "--plan", "--json"})
        for value in ("true", 1, None, "--full"):
            self.refused(tool, {"full": value}, "argument full must be true or false")

    def test_unknown_and_missing_arguments_are_refused(self):
        tool = self.tool(Argument("spec_id", "spec_id", "A spec.", required=True))
        self.refused(tool, {}, "argument spec_id is required")
        for name in ("yes", "--yes", "json", "node", "spec"):
            self.refused(tool, {"spec_id": SPEC, name: True}, f"unexpected argument {name}")

    def test_a_table_entry_that_makes_no_sense_cannot_be_written(self):
        with self.assertRaises(ValueError):
            Tool(name="pulsar_fixture", description="Neither an operation nor an action.")
        with self.assertRaises(ValueError):
            operation_tool("pulsar_fixture", "fixture", "fixture", "stream", "A stream is no result.")
        with self.assertRaises(ValueError):
            operation_tool("pulsar_fixture", "fixture", "fixture", "envelope", "Two arguments with one name.",
                           arguments=(Argument("wait_seconds", "text", "Taken."),))
        with self.assertRaises(ValueError):
            Argument("full", "flag", "A flag without its option.")
        with self.assertRaises(ValueError):
            Argument("full", "word", "An unknown kind.")


class ImportBoundary(unittest.TestCase):
    MODULES = ["mcp_server", "mcp_server.protocol", "mcp_server.tool_table", "mcp_server.commands",
               "mcp_server.supervisor", "mcp_server.__main__"]

    def test_importing_the_package_loads_no_lifecycle_code(self):
        program = ("import sys\n"
                   f"sys.path.insert(0, {str(ROOT)!r})\n"
                   f"import {', '.join(self.MODULES)}\n"
                   "print(sorted(name for name in sys.modules if name.split('.')[0] in "
                   "('model_library', 'release_spec', 'serving_guard', 'scripts')))\n"
                   "print(sorted(name for name in sys.modules if name.startswith('mcp_server')))\n")
        # -I: a clean interpreter, without this test's path or the person's settings; -B: no bytecode files.
        result = subprocess.run([sys.executable, "-I", "-B", "-c", program], capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.stderr, "")
        loaded, package = result.stdout.splitlines()
        self.assertEqual(loaded, "[]")
        self.assertEqual(package, str(sorted(self.MODULES)))

    def imports(self, path):
        """Every module a file imports, wherever in the file."""
        found = set()
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, f"{path.name}: a relative import")
                found.add(node.module)
        return found

    def test_the_package_imports_the_standard_library_itself_and_the_terminal_writer_only(self):
        files = sorted((ROOT / "mcp_server").glob("*.py"))
        self.assertEqual([path.name for path in files],
                         ["__init__.py", "__main__.py", "commands.py", "protocol.py", "supervisor.py", "tool_table.py"])
        for path in files:
            for module in sorted(self.imports(path)):
                top = module.split(".")[0]
                with self.subTest(file=path.name, module=module):
                    self.assertNotIn(top, LIFECYCLE_PACKAGES)
                    if top == "scripts":
                        # Help and error lines for people are printed with the shared writer, and by nothing else.
                        self.assertEqual((path.name, module), ("__main__.py", "scripts.terminal_format"))
                    elif top != "mcp_server":
                        self.assertIn(top, sys.stdlib_module_names)
        # The supervisor is run as a file: it imports nothing of the package either.
        self.assertFalse({module for module in self.imports(ROOT / "mcp_server/supervisor.py")
                          if module.startswith("mcp_server")})
        # The writer that the server borrows imports the standard library only.
        for module in self.imports(ROOT / "scripts/terminal_format.py"):
            self.assertIn(module.split(".")[0], sys.stdlib_module_names)

    def test_the_server_starts_no_thread_and_opens_no_listener(self):
        for path in sorted((ROOT / "mcp_server").glob("*.py")):
            modules = {module.split(".")[0] for module in self.imports(path)}
            self.assertFalse(modules & {"threading", "concurrent", "multiprocessing", "asyncio", "_thread",
                                        "socket", "socketserver", "http", "ssl"}, path.name)
        self.assertEqual(protocol.END_SIGNALS, tuple(__import__("signal").Signals[name]
                                                     for name in ("SIGINT", "SIGTERM", "SIGHUP")))


if __name__ == "__main__":
    unittest.main()
