"""Read-only node collection, including partial observations and process ownership."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import inventory_node as node


class NodeSample(unittest.TestCase):
    def test_local_docker_outage_skips_gpu_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpu = root / "gpu"
            gpu.write_text('#!/bin/sh\ntouch "$0.called"\n')
            gpu.chmod(0o700)
            result = subprocess.run([sys.executable, node.__file__, "--local", "false", str(gpu)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Docker is unavailable", result.stderr)
            self.assertFalse((root / "gpu.called").exists())

    def test_collects_only_relevant_containers_and_keeps_unattributed_gpu_processes(self):
        managed = {"Id": "a", "Name": "/model", "Config": {"Image": "engine", "Cmd": [],
                   "Labels": {"io.pulsar.gb10.managed": "true", "unrelated": "ignored"}},
                   "State": {"Running": True, "Status": "running"}}
        other = dict(managed, Id="b", Name="/builder", Config={"Image": "builder", "Cmd": []})
        responses = {
            ("smi", "--query-compute-apps=pid,process_name,used_gpu_memory",
             "--format=csv,noheader,nounits"): "45, engine, 13 MiB\n90, training, [N/A]\n",
            ("docker", "info"): "ok",
            ("docker", "ps", "-aq"): "a\nb",
            ("docker", "inspect", "a"): json.dumps([managed]),
            ("docker", "inspect", "b"): json.dumps([other]),
            ("docker", "top", "a", "-eo", "pid"): "PID\n45\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            meminfo = Path(directory) / "meminfo"
            meminfo.write_text("MemAvailable: 524288 kB\nMemTotal: 1048576 kB\n")
            with patch.object(node, "output", side_effect=lambda *args: responses[args]):
                sample = node.collect("docker", "smi", meminfo)
            self.assertEqual((sample["mem_available_gib"], sample["mem_total_gib"]), (0.5, 1))
            self.assertEqual(sample["docker_status"], "ok")
            container, = sample["containers"]
            self.assertEqual(container["id"], "a")
            self.assertEqual(container["host_pids"], [45])
            self.assertEqual(container["labels"], {"io.pulsar.gb10.managed": "true"})
            self.assertEqual([(row["pid"], row["used_memory_mib"]) for row in sample["gpu_processes"]],
                             [(45, 13), (90, None)])

            # A container disappearing between listing and inspection makes the node incomplete.
            responses["docker", "inspect", "b"] = None
            with patch.object(node, "output", side_effect=lambda *args: responses[args]):
                incomplete = node.collect("docker", "smi", meminfo)
            self.assertEqual(incomplete["docker_status"], "incomplete")
            self.assertEqual(incomplete["containers"], [container])

    def test_remote_docker_outage_keeps_independent_gpu_and_memory_readings(self):
        def output(tool, *args):
            return None if tool == "docker" else "45, engine, 13 MiB\n"
        with tempfile.TemporaryDirectory() as directory:
            meminfo = Path(directory) / "meminfo"
            meminfo.write_text("MemAvailable: 524288 kB\n")
            with patch.object(node, "output", side_effect=output):
                sample = node.collect("docker", "smi", meminfo)
        self.assertEqual(sample["docker_status"], "unavailable")
        self.assertEqual(sample["mem_available_gib"], 0.5)
        self.assertEqual([row["pid"] for row in sample["gpu_processes"]], [45])

    def test_incomplete_process_list_is_unknown_and_header_only_is_empty(self):
        for result, expected in ((None, None), ("", None), ("PID\n42\n?\n", None), ("PID\n", [])):
            with self.subTest(result=result), patch.object(node, "output", return_value=result):
                self.assertEqual(node.host_pids("docker", "container"), expected)

    def test_incomplete_or_malformed_process_data_cannot_be_used_as_a_successful_node_reading(self):
        sample = dict(docker_status="ok", containers=[], gpu_processes=[], mem_available_gib=12, mem_total_gib=24)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample"
            path.write_text(json.dumps(sample))
            self.assertEqual(node.read_sample(path), sample)
            for bad in (dict(sample, containers=None), dict(sample, gpu_processes=[{"pid": 10}]),
                        dict(sample, mem_available_gib=-1)):
                with self.subTest(bad=bad):
                    path.write_text(json.dumps(bad))
                    with self.assertRaises((ValueError, KeyError)):
                        node.read_sample(path)
