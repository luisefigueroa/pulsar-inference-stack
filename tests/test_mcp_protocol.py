"""The MCP server speaks its protocol without a client: handshake, tool list, calls and errors.

Every server here runs a double of the pulsar file; see tests/support/mcp_session.py.
"""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp_server import protocol
from tests.support.mcp_session import CONTRACT, TOPOLOGY, Session, spec

TOOL_NAMES = ["pulsar_contract", "pulsar_topology_show", "pulsar_models_show", "pulsar_mcp_server_info",
              "pulsar_command_get", "pulsar_command_cancel", "pulsar_command_list"]


class Parse(unittest.TestCase):
    def test_a_request_and_a_notification_are_read_with_params_as_an_object(self):
        message, response = protocol.parse('{"jsonrpc": "2.0", "id": "a", "method": "ping"}')
        self.assertEqual((message["id"], message["method"], message["params"], response), ("a", "ping", {}, None))
        message, response = protocol.parse('{"jsonrpc": "2.0", "method": "notifications/initialized"}')
        self.assertEqual(("id" in message, message["params"], response), (False, {}, None))

    def test_a_line_that_is_no_message_gets_a_standard_error_or_nothing(self):
        cases = {
            "not json": (protocol.PARSE_ERROR, None),
            '{"jsonrpc": "2.0", "id": 1, "method": "ping"': (protocol.PARSE_ERROR, None),
            "[]": (protocol.INVALID_REQUEST, None),  # No batches.
            '{"id": null, "method": "ping"}': (protocol.INVALID_REQUEST, None),
            '{"id": true, "method": "ping"}': (protocol.INVALID_REQUEST, None),
            '{"id": 7}': (protocol.INVALID_REQUEST, 7),
            '{"id": 7, "method": 3}': (protocol.INVALID_REQUEST, 7),
            '{"id": 7, "method": "tools/list", "params": []}': (protocol.INVALID_PARAMS, 7),
            # An id that could not be written back as JSON.
            '{"id": NaN, "method": "ping"}': (protocol.PARSE_ERROR, None),
            '{"id": Infinity, "method": "ping"}': (protocol.PARSE_ERROR, None),
            '{"id": 1e999, "method": "ping"}': (protocol.INVALID_REQUEST, None),
            '{"id": 7, "method": "ping", "params": {"x": -Infinity}}': (protocol.PARSE_ERROR, None),
        }
        for line, (code, request_id) in cases.items():
            with self.subTest(line=line):
                message, response = protocol.parse(line)
                self.assertIsNone(message)
                self.assertEqual(response["error"]["code"], code)
                # A response to a request whose id could not be read carries no id.
                self.assertEqual(response.get("id"), request_id)
                self.assertEqual("id" in response, request_id is not None)
        for line in ('{"id": 7, "result": {}}', '{"id": 7, "error": {"code": -1, "message": "x"}}',
                     '{"method": "notifications/cancelled", "params": []}'):
            with self.subTest(line=line):
                self.assertEqual(protocol.parse(line), (None, None))

    def test_a_line_nested_too_deep_to_read_is_an_error_not_a_crash(self):
        message, response = protocol.parse('{"id": 7, "method": "ping", "params": ' + "[" * 100000 + "]" * 100000 + "}")
        self.assertIsNone(message)
        # A Python that can read this deep finds params that are no object instead.
        self.assertIn(response["error"]["code"], (protocol.PARSE_ERROR, protocol.INVALID_PARAMS))

    def test_the_string_one_and_the_number_one_are_different_ids(self):
        self.assertNotEqual(protocol.id_key("1"), protocol.id_key(1))


class Handshake(unittest.TestCase):
    def test_the_handshake_opens_lists_and_calls(self):
        session = Session(self, steps={"contract --json": {"json": CONTRACT}})
        opened = session.initialize()["result"]
        self.assertEqual(opened["protocolVersion"], "2025-11-25")
        # Tools only: no resources and no prompts.
        self.assertEqual(opened["capabilities"], {"tools": {}})
        self.assertEqual(set(opened["serverInfo"]), {"name", "version"})
        for words in ("read-only", "pulsar command line", "output_form", "envelope", "document",
                      "docs/CONTRACT.md", "pulsar_command_get"):
            self.assertIn(words, opened["instructions"])
        listed = session.request("tools/list")["result"]
        self.assertEqual([tool["name"] for tool in listed["tools"]], TOOL_NAMES)
        self.assertNotIn("nextCursor", listed)  # The whole table is one page.
        for tool in listed["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])
            self.assertTrue(tool["description"].endswith("."), tool["name"])
        # The list is the same every time, in the same order.
        self.assertEqual(session.request("tools/list")["result"], listed)
        result = session.call("pulsar_contract")
        self.assertFalse(result["isError"])
        self.assertEqual(session.checked_object(result)["output"], CONTRACT)
        self.assertEqual(session.calls(), [["contract", "--json"]])
        session.close_input()
        self.assertEqual((session.read_to_end(), session.wait_exit()), ([], 0))
        self.assertEqual(session.stderr(), "")

    def test_initialize_echoes_a_revision_it_knows_and_answers_any_other_with_the_newest(self):
        for asked, answered in (("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
                                ("2026-07-28", "2025-11-25"), ("2024-11-05", "2025-11-25"),
                                ("1900-01-01", "2025-11-25")):
            with self.subTest(asked=asked):
                session = Session(self)
                self.assertEqual(session.initialize(asked)["result"]["protocolVersion"], answered)
                # Whatever revision was agreed, the tools are listed.
                self.assertEqual(len(session.request("tools/list")["result"]["tools"]), len(TOOL_NAMES))
                session.stop()
        self.assertEqual(protocol.PROTOCOL_VERSIONS, ("2025-11-25", "2025-06-18"))

    def test_server_discover_is_method_not_found_so_that_a_client_falls_back_to_initialize(self):
        session = Session(self)
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {}}
        refused = session.request("server/discover", {"_meta": meta})
        # Any code the later revision defines (-32020 to -32022) would stop the fallback.
        self.assertEqual(refused["error"]["code"], -32601)
        self.assertNotIn("result", refused)
        self.assertEqual(session.initialize()["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(len(session.request("tools/list")["result"]["tools"]), len(TOOL_NAMES))

    def test_only_the_methods_of_the_handshake_form_are_answered(self):
        session = Session(self, steps={"contract --json": {"json": CONTRACT}})
        self.assertEqual(session.request("ping")["result"], {})  # Also before initialize.
        for method in ("tools/list", "tools/call"):
            self.assertEqual(session.request(method, {"name": "pulsar_contract"})["error"]["code"], -32600, method)
        session.initialize()
        for method in ("resources/list", "prompts/list", "logging/setLevel", "tools/unknown", "server/discover"):
            self.assertEqual(session.request(method)["error"]["code"], -32601, method)
        self.assertEqual(session.request("tools/list", {"cursor": "next"})["error"]["code"], -32602)
        self.assertEqual(session.request("tools/call", {"name": "pulsar_stop"})["error"]["code"], -32602)
        self.assertEqual(session.request("tools/call", {"name": "pulsar_contract", "arguments": []})
                         ["error"]["code"], -32602)
        self.assertEqual(session.request("ping")["result"], {})
        # A notification is never answered, known or not; nor is a response.
        session.notify("notifications/unknown")
        session.notify("notifications/cancelled", {"requestId": 999, "reason": "nothing has this id"})
        session.send({"jsonrpc": "2.0", "id": 5, "result": {}})
        session.send_line(b"")
        marker = session.post("ping")
        self.assertEqual(session.read_message()["id"], marker)
        self.assertEqual(session.calls(), [])  # Nothing above ran a command.

    def test_two_calls_sent_together_run_at_once_and_both_are_answered(self):
        steps = {f"models show {spec(number)} --json": {"name": name, "started": True, "release": True,
                                                         "json": {"kind": "fixture", "name": name}}
                 for number, name in ((1, "one"), (2, "two"))}
        session = Session(self, steps=steps)
        session.initialize()
        requests = [{"jsonrpc": "2.0", "id": f"call-{number}", "method": "tools/call",
                     "params": {"name": "pulsar_models_show", "arguments": {"spec_id": spec(number)}}}
                    for number in (1, 2)]
        # Both lines arrive in one write, before the first call has answered.
        session.send_line(b"\n".join(json.dumps(request).encode() for request in requests))
        # Each double waits to be released, so both markers show two commands running at once.
        session.marker("one.started")
        session.marker("two.started")
        session.release("two")
        second = session.response("call-2")
        self.assertEqual(session.checked_object(second["result"])["output"]["name"], "two")
        self.assertFalse(any(message.get("id") == "call-1" for message in session.messages))
        session.release("one")
        first = session.response("call-1")
        self.assertEqual(session.checked_object(first["result"])["output"]["name"], "one")

    def test_an_id_that_still_waits_for_its_answer_cannot_be_used_again(self):
        session = Session(self, steps={f"models show {spec(1)} --json": {"name": "one", "started": True,
                                                                        "release": True, "json": {"n": 1}}})
        session.initialize()
        call = {"jsonrpc": "2.0", "id": "same", "method": "tools/call",
                "params": {"name": "pulsar_models_show", "arguments": {"spec_id": spec(1)}}}
        session.send(call)
        session.marker("one.started")
        session.send(call)
        self.assertEqual(session.read_message()["error"]["code"], -32600)
        session.release("one")
        self.assertEqual(session.checked_object(session.read_message()["result"])["output"], {"n": 1})
        self.assertEqual(len(session.calls()), 1)  # The second line started nothing.

    def test_meta_keys_the_server_does_not_know_are_ignored(self):
        session = Session(self, steps={"topology show --json": {"json": TOPOLOGY}})
        meta = {"progressToken": "fixture-token", "example.invalid/trace": {"depth": 3}, "unknown": [1, 2]}
        opened = session.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "fixture-client", "version": "0"},
                                                "_meta": meta})
        self.assertEqual(opened["result"]["protocolVersion"], "2025-06-18")
        plain = session.request("tools/list")["result"]
        self.assertEqual(session.request("tools/list", {"_meta": meta})["result"], plain)
        called = session.request("tools/call", {"name": "pulsar_topology_show", "arguments": {}, "_meta": meta})
        self.assertEqual(session.checked_object(called["result"])["output"], TOPOLOGY)
        self.assertEqual(session.request("ping", {"_meta": meta})["result"], {})
        session.close_input()
        session.read_to_end()
        # A progress token gets no progress: every line the server wrote answers a request.
        self.assertTrue(all("id" in message and "method" not in message for message in session.messages))
        self.assertEqual(session.calls(), [["topology", "show", "--json"]])

    def test_an_argument_the_server_refuses_is_a_tool_result_not_a_protocol_error(self):
        session = Session(self)
        session.initialize()
        refusals = {
            "must be a complete spec ID": {"spec_id": "-" + "a" * 63},
            "must be a complete spec ID: 64": {"spec_id": "abc123"},
            "argument spec_id must be a string": {"spec_id": 5},
            "argument spec_id is required": {},
            "unexpected argument json": {"spec_id": spec(1), "json": True},
            "unexpected argument --yes": {"spec_id": spec(1), "--yes": True},
            "argument wait_seconds must be between 0 and 240": {"spec_id": spec(1), "wait_seconds": 241},
            "argument wait_seconds must be a number": {"spec_id": spec(1), "wait_seconds": "60"},
        }
        for message, arguments in refusals.items():
            with self.subTest(arguments=arguments):
                response = session.response(session.post_call("pulsar_models_show", arguments))
                self.assertNotIn("error", response)
                result = response["result"]
                self.assertIs(result["isError"], True)
                self.assertNotIn("structuredContent", result)
                self.assertEqual([block["type"] for block in result["content"]], ["text"])
                self.assertIn(message, result["content"][0]["text"])
                self.assertTrue(result["content"][0]["text"].startswith("pulsar_models_show was not run: "))
        self.assertEqual(session.calls(), [])  # Refused before any command ran.

    def test_standard_output_holds_only_protocol_messages_one_per_line(self):
        noise = "plain words on standard output\n{not json}\n"
        session = Session(self, steps={
            "contract --json": {"stdout": noise, "stderr": "words on standard error\n", "exit": 3},
            "topology show --json": {"json": {"text": "line one\nline two   é 😀"}}})
        session.send_line(b"this line is not JSON")
        session.send_line(b'{"jsonrpc": "2.0", "id": 1, "method": "ini\xff\xfe"}')
        session.send_line(b'{"jsonrpc": "2.0", "id": NaN, "method": "ping"}')
        session.send_line(b'{"jsonrpc": "2.0", "id": 1e999, "method": "ping"}')
        session.send_line(b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": ' + b"[" * 100000 + b"]" * 100000 + b"}")
        session.initialize()
        session.request("tools/list")
        self.assertEqual(session.structured("pulsar_contract")["text"], noise)
        echoed = session.structured("pulsar_topology_show")["output"]["text"]
        self.assertEqual(echoed, "line one\nline two   é \U0001f600")
        session.call("pulsar_models_show", {"spec_id": "not a spec"})
        session.request("no/such/method")
        session.close_input()
        session.read_to_end()
        self.assertEqual(session.wait_exit(), 0)
        self.assertTrue(session.raw.endswith(b"\n"))
        lines = session.raw.split(b"\n")[:-1]
        self.assertEqual(len(lines), 11)  # Five bad lines, six requests: one line for each, and no other.
        for line in lines:
            self.assertTrue(line.isascii() and line.strip() == line and line, line)
            message = json.loads(line)
            self.assertEqual(message["jsonrpc"], "2.0")
            self.assertEqual(len({"result", "error"} & set(message)), 1, message)
        self.assertEqual(session.stderr(), "")  # The commands' own standard error stays in their results.


if __name__ == "__main__":
    unittest.main()
