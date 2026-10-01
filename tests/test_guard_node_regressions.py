"""CPU-only acceptance coverage for guarded node commands and port admission."""

from contextlib import contextmanager, ExitStack
import copy
import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from release_spec import serving
from scripts import container_runtime as runtime
from serving_guard import node, program
from tests.test_container_runtime import fixture

GIB = 1024**3


def node_fixture(nodes=1, *, token=None, api_key=""):
    with patch.dict(os.environ, {"HF_TOKEN": token or "", "VLLM_API_KEY": api_key,
                                 "API_KEY": ""}):
        if token is None:
            del os.environ["HF_TOKEN"]
        spec, facts, prepared, _, containers, images = fixture(nodes)
        settings = copy.deepcopy(spec["recipe"]["container"])
        settings.update(network_mode="host", memory_limit_bytes=16 * GIB, healthcheck=None,
                        guard=program.template(["engine"], minimum=8 * GIB, startup=10, timeout=20))
        spec = serving.apply_overrides(spec, {"container": settings})
        prepared["spec_id"] = spec["spec_id"]
        plan = runtime.build_plan(spec, spec["spec_id"], facts, prepared)
        argv = runtime.docker_argv(plan, 0)
    image = images[0]
    image.update(Architecture="arm64", Os="linux")
    container = containers[0]
    reference = image["RepoDigests"][0]
    container["Config"].update(Labels=runtime.rank_spec(plan, 0)["labels"],
                               Entrypoint=["python3"], Cmd=argv[argv.index(reference) + 1:],
                               OpenStdin=True, Healthcheck={"Test": ["NONE"]})
    container["HostConfig"].update(NetworkMode="host", PortBindings={}, Memory=16 * GIB,
                                   MemorySwap=16 * GIB, PidsLimit=512,
                                   CgroupnsMode="private", AutoRemove=True)
    container["Mounts"].append({"Type": "bind", "Source": "/proc/meminfo",
                                "Destination": "/pulsar-guard-host-meminfo", "RW": False})
    if nodes == 1:
        container["Config"]["Env"] = [
            "HF_TOKEN=" + (token or "") if item.startswith("HF_TOKEN=") else item
            for item in container["Config"]["Env"]
        ]
    return {"plan": plan, "rank": 0, "argv": argv}, container, image


@contextmanager
def node_host(context, container, image, *, occupied_ports=()):
    """Replace host IO while retaining node preflight, observation and cleanup."""
    state = {"container": None, "done": False}
    ports = []
    process = MagicMock()
    process.poll.side_effect = lambda: 0 if state["done"] else None
    process.returncode = 0

    def docker(*args, **kwargs):
        if args[:2] == ("image", "inspect"):
            output = json.dumps([image])
        elif args[:2] == ("container", "ls"):
            output = container["Id"] if state["container"] is not None else ""
        elif args[:2] == ("container", "inspect"):
            output = json.dumps([state["container"]])
        elif args == ("ps", "-q"):
            output = ""
        elif args == ("rm", "-f", container["Id"]):
            state["container"] = None
            output = ""
        else:
            raise AssertionError("unexpected Docker operation")
        return subprocess.CompletedProcess([], 0, output, "")

    def bind(address):
        ports.append(address[1])
        if address[1] in occupied_ports:
            raise OSError(errno.EADDRINUSE, "synthetic occupied port")

    def launch(*args, **kwargs):
        state["container"] = copy.deepcopy(container)

        def control(value):
            if value == b".":
                report = {**node.identity(context["plan"], 0),
                          "kind": "pulsar-serving-guard-rank", "stopped": True}
                kwargs["stdout"].write(json.dumps(report) + "\n")
                kwargs["stdout"].flush()
                state["done"] = True

        process.stdin.write.side_effect = control
        return process

    with ExitStack() as stack:
        docker_mock = stack.enter_context(patch.object(node, "docker", side_effect=docker))
        stack.enter_context(patch.object(node.subprocess, "run", return_value=
            subprocess.CompletedProcess([], 0, "", "")))
        stack.enter_context(patch.object(node, "read_meminfo", return_value=
            {"mem_available_bytes": 64 * GIB}))
        sockets = stack.enter_context(patch.object(node.socket, "socket"))
        sockets.return_value.__enter__.return_value.bind.side_effect = bind
        popen = stack.enter_context(patch.object(node.subprocess, "Popen", side_effect=launch))
        health = stack.enter_context(patch.object(node, "health_ready", return_value=True))
        observation = stack.enter_context(patch.object(node, "observe_rank", wraps=node.observe_rank))
        stack.enter_context(patch.object(node.time, "sleep"))
        yield process, popen, docker_mock, health, observation, ports, state


class GuardNodeRegressions(unittest.TestCase):
    def test_single_node_executes_with_unset_empty_and_present_hf_token(self):
        for token in (None, "", "synthetic-hf-token"):
            for api_key in ("", "synthetic-api-key"):
                with self.subTest(token=token, authenticated=bool(api_key)):
                    context, container, image = node_fixture(token=token, api_key=api_key)
                    original = list(context["argv"])
                    with tempfile.TemporaryDirectory() as directory, \
                            node_host(context, container, image) as host:
                        process, popen, docker, health, observation, ports, state = host
                        report = node.execute(context, Path(directory))
                    self.assertTrue(report["stopped"])
                    self.assertEqual(context["argv"], original)
                    self.assertEqual(popen.call_args.args[0][1:], original[1:])
                    self.assertEqual([call.args[0] for call in process.stdin.write.call_args_list],
                                     [b"G", b"H", b"."])
                    health.assert_called_once_with(context["plan"], original)
                    observation.assert_called_once()
                    self.assertEqual(ports, [context["plan"]["port"]])
                    self.assertIsNone(state["container"])
                    docker.assert_any_call("rm", "-f", container["Id"])
                    observed = runtime.observe_rank(context["plan"], 0, container, image)
                    serialized = json.dumps({"report": report, "observation": observed})
                    for secret in (token, api_key):
                        if secret:
                            self.assertNotIn(secret, serialized)

    def test_execute_rejects_changed_nonsecret_arguments_before_host_io(self):
        context, container, image = node_fixture(token="synthetic-hf-token",
                                                 api_key="synthetic-api-key")
        hf_index = context["argv"].index("HF_TOKEN=synthetic-hf-token")
        changes = {
            "memory": lambda argv: argv.__setitem__(argv.index("--memory") + 1, str(32 * GIB)),
            "hf_name": lambda argv: argv.__setitem__(hf_index, "OTHER_TOKEN=synthetic-hf-token"),
            "hf_flag": lambda argv: argv.__setitem__(hf_index - 1, "--label"),
            "hf_invalid_type": lambda argv: argv.__setitem__(hf_index, None),
            "extra_hf": lambda argv: argv.extend(["-e", "HF_TOKEN=extra"]),
            "missing_hf": lambda argv: argv.__delitem__(slice(hf_index - 1, hf_index + 1)),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                modified = {**context, "argv": list(context["argv"])}
                change(modified["argv"])
                with tempfile.TemporaryDirectory() as directory, \
                        node_host(modified, container, image) as host:
                    with self.assertRaisesRegex(ValueError, "frozen node command differs") as error:
                        node.execute(modified, Path(directory))
                    host[1].assert_not_called()
                    host[2].assert_not_called()
                self.assertNotIn("synthetic-hf-token", str(error.exception))
                self.assertNotIn("synthetic-api-key", str(error.exception))

    def test_execute_requires_exactly_one_nonempty_api_credential(self):
        context, container, image = node_fixture(token="synthetic-hf-token",
                                                 api_key="synthetic-api-key")
        index = context["argv"].index("--api-key")
        invalid = [context["argv"][:index],
                   context["argv"][:index + 1],
                   context["argv"][:index + 1] + [""],
                   context["argv"] + ["--api-key", "extra"]]
        for argv in invalid:
            with self.subTest(argv_length=len(argv)), tempfile.TemporaryDirectory() as directory, \
                    node_host(context, container, image) as host:
                with self.assertRaisesRegex(ValueError, "guarded API credential"):
                    node.execute({**context, "argv": argv}, Path(directory))
                host[1].assert_not_called()
                host[2].assert_not_called()

    def test_single_node_ignores_occupied_unused_master_port(self):
        context, container, image = node_fixture()
        plan = context["plan"]
        self.assertEqual(plan["master_port"], 29500)
        self.assertNotIn("--master-port", context["argv"])
        with node_host(context, container, image, occupied_ports=[29500]) as host:
            result, _ = node.preflight(plan, 0)
            self.assertTrue(result["ready"])
            self.assertEqual(host[5], [plan["port"]])

    def test_occupied_api_port_blocks_single_and_multi_node(self):
        for nodes in (1, 3):
            with self.subTest(nodes=nodes):
                context, container, image = node_fixture(nodes)
                plan = context["plan"]
                with node_host(context, container, image, occupied_ports=[plan["port"]]) as host:
                    with self.assertRaises(OSError) as error:
                        node.preflight(plan, 0)
                    self.assertEqual(error.exception.errno, errno.EADDRINUSE)
                    self.assertEqual(host[5], [plan["port"]])

    def test_occupied_master_port_blocks_multi_node_head(self):
        context, container, image = node_fixture(3)
        plan = context["plan"]
        with node_host(context, container, image, occupied_ports=[plan["master_port"]]) as host:
            with self.assertRaises(OSError) as error:
                node.preflight(plan, 0)
            self.assertEqual(error.exception.errno, errno.EADDRINUSE)
            self.assertEqual(host[5], [plan["port"], plan["master_port"]])


if __name__ == "__main__":
    unittest.main()
