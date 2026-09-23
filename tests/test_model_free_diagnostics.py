"""Model-free diagnostic contracts and real CPU guard/cancellation processes."""

import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from diagnostics import controller, guard, node, schema, worker

ROOT = Path(__file__).resolve().parents[1]


def request(data=b"pass\n", nodes=3):
    return {
        "schema_version": 1,
        "kind": "pulsar-diagnostic-request",
        "topology_id": "b" * 64,
        "image_id": "sha256:" + "a" * 64,
        "geometry": {"nodes": nodes, "tp": 1, "pp": nodes},
        "limits": {
            "memory_bytes": 16 * schema.GIB,
            "min_host_available_bytes": 64 * schema.GIB,
            "timeout_seconds": 10,
        },
        "rendezvous_port": 29501,
        "files": {"probe.py": hashlib.sha256(data).hexdigest()},
        "entrypoint": "probe.py",
    }


def context(data=b"pass\n"):
    value = request(data)
    return {
        "request": value,
        "payload": {"probe.py": base64.b64encode(data).decode()},
        "request_id": schema.digest(value),
        "run_id": "c" * 64,
        "rank": 0,
        "verbs_device": "/dev/infiniband/uverbs0",
        "guard_files": {
            name: hashlib.sha256(b"pass\n").hexdigest()
            for name in (
                "diagnostics/guard.py",
                "diagnostics/worker.py",
                "diagnostics/schema.py",
                "scripts/resource_sample.py",
                "serving_guard/runtime.py",
            )
        },
        "ranks": [
            {
                "rank": i,
                "node_id": "fixture-" + str(i),
                "control_ip": "192.0.2." + str(i + 1),
                "control_if": "fabric0",
                "hcas": "fixture_hca:1",
            }
            for i in range(3)
        ],
    }


class DiagnosticContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.payload = self.root / "payload"
        self.payload.mkdir()
        (self.payload / "probe.py").write_text("pass\n")
        self.input = self.root / "request.json"
        self.input.write_text(json.dumps(request()))

    def test_validate_cli_is_cpu_only_and_works_outside_checkout(self):
        result = subprocess.run(
            [
                str(ROOT / "pulsar"),
                "diagnostic",
                "validate",
                "--request",
                "request.json",
                "--payload-dir",
                "payload",
                "--json",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)["result"]
        self.assertEqual(value["request_id"], schema.digest(request()))
        self.assertFalse(value["physical_execution"])

    def test_run_requires_explicit_authority_before_creating_output(self):
        result = subprocess.run(
            [
                str(ROOT / "pulsar"),
                "diagnostic",
                "run",
                "--request",
                str(self.input),
                "--payload-dir",
                str(self.payload),
                "--request-id",
                schema.digest(request()),
                "--output-dir",
                str(self.root / "evidence"),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "evidence").exists())

    def test_request_and_payload_changes_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "changed since review"):
            schema.load(self.input, self.payload, "d" * 64)
        (self.payload / "probe.py").write_text("raise RuntimeError()\n")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            schema.load(self.input, self.payload)

    def test_path_symlink_and_extra_field_rejected(self):
        (self.payload / "probe.py").unlink()
        (self.payload / "probe.py").symlink_to(self.input)
        with self.assertRaises(OSError):
            schema.load(self.input, self.payload)
        value = request()
        value["files"]["../escape.py"] = "a" * 64
        with self.assertRaises(ValueError):
            schema.validate(value)
        value = request()
        value["command"] = ["sh", "-c", "anything"]
        with self.assertRaises(ValueError):
            schema.validate(value)

    def test_geometry_and_finite_integer_limits_rejected(self):
        for mutate in (
            lambda v: v["geometry"].update(tp=2),
            lambda v: v["limits"].update(memory_bytes=True),
            lambda v: v["limits"].update(timeout_seconds=float("nan")),
            lambda v: v.update(image_id="unreviewed:latest"),
        ):
            value = request()
            mutate(value)
            with self.assertRaises(ValueError):
                schema.validate(value)

    def freeze(self):
        output = self.root / "run"
        controller.freeze(self.input, self.payload, schema.digest(request()), output)
        rows = "".join(
            f"{i}\tfixture-{i}\t192.0.2.{i + 1}\tfabric0\tfixture_hca:1\n"
            for i in range(3)
        )
        controller.bind(output, "b" * 64, "/dev/infiniband/uverbs0", rows)
        return output

    def test_freeze_does_not_overwrite_and_binds_complete_topology(self):
        output = self.freeze()
        with self.assertRaises(FileExistsError):
            controller.freeze(
                self.input, self.payload, schema.digest(request()), output
            )
        for change in (
            lambda c: c["ranks"].pop(),
            lambda c: c["ranks"][1].update(rank=0),
            lambda c: c["ranks"][1].update(node_id="fixture-0"),
        ):
            value = context()
            change(value)
            with self.assertRaises(ValueError):
                schema.validate_context(value)

    def test_frozen_code_tampering_is_detected(self):
        output = self.freeze()
        (output / "code/diagnostics/guard.py").write_text("raise RuntimeError()")
        with self.assertRaisesRegex(ValueError, "frozen Stack code changed"):
            controller.bundle(output, "execute", 0)

    def test_complete_rank_results_and_cleanup_are_required(self):
        output = self.freeze()
        value = json.loads((output / "context.json").read_text())
        for phase in ("preflight", "execute", "cleanup"):
            directory = output / phase
            (directory / "jobs").mkdir(parents=True)
            (directory / "batch.json").write_text(
                json.dumps(
                    {
                        "outcome": "complete",
                        "returncode": 0,
                        "results": [{"index": i, "returncode": 0} for i in range(3)],
                    }
                )
            )
            for rank in range(3):
                (directory / "jobs" / f"{rank}.out").write_text(
                    json.dumps(
                        {
                            "rank": rank,
                            "run_id": value["run_id"],
                            "request_id": value["request_id"],
                            "image_id": request()["image_id"],
                            "ready": True,
                            "successful": True,
                            "cleanup_verified": True,
                            "idle": True,
                        }
                    )
                )
        self.assertTrue(controller.finish(output)["successful"])
        (output / "cleanup/jobs/2.out").unlink()
        self.assertFalse(controller.finish(output, "incomplete.json")["successful"])
        (output / "execute/jobs/1.out").write_text("{}")
        with self.assertRaises(ValueError):
            controller.phase_results(output, "execute")

    def test_public_contract_advertises_diagnostics(self):
        from scripts.integration_contract import contract

        self.assertIn("diagnostic.run", contract()["operations"])
        self.assertEqual(contract()["diagnostic_request_schema_versions"], [1])


class NodeIdentity(unittest.TestCase):
    def test_missing_or_wrong_image_never_starts_container(self):
        value = context()
        with (
            patch.object(
                node,
                "docker",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps([{"Id": "sha256:" + "f" * 64, "Architecture": "arm64"}]),
                ),
            ),
            patch.object(node, "idle") as idle,
        ):
            with self.assertRaisesRegex(RuntimeError, "image"):
                node.preflight(value)
            idle.assert_not_called()

    def test_occupied_gpu_refuses_start(self):
        with patch.object(
            node,
            "docker",
            side_effect=[
                subprocess.CompletedProcess([], 0, "container\n"),
                subprocess.CompletedProcess(
                    [], 0, json.dumps([{"HostConfig": {"DeviceRequests": [{}]}}])
                ),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                node.idle()

    def test_cleanup_preserves_unrelated_container(self):
        value = context()
        info = {
            "Id": "unrelated",
            "Image": value["request"]["image_id"],
            "Config": {"Labels": {}},
        }
        with (
            patch.object(node, "inspect_named", return_value=info),
            patch.object(node, "docker") as docker,
        ):
            with self.assertRaisesRegex(RuntimeError, "preserved"):
                node.cleanup(value)
            docker.assert_not_called()

    def test_owned_cleanup_requires_verified_absence(self):
        value = context()
        info = {
            "Id": "owned",
            "Image": value["request"]["image_id"],
            "Config": {"Labels": schema.labels(value)},
        }
        with (
            patch.object(node, "inspect_named", side_effect=[info, None]),
            patch.object(node, "docker") as docker,
        ):
            self.assertTrue(node.cleanup(value)["cleanup_verified"])
            docker.assert_called_once_with("rm", "-f", "owned")
        with (
            patch.object(node, "inspect_named", return_value=info),
            patch.object(node, "docker"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                node.cleanup(value)


class RdmaDeviceMapping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sysfs = self.root / "verbs"
        self.sysfs.mkdir()
        self.devices = self.root / "devices"
        self.devices.mkdir()
        for index, hca in [(0, "hca_a"), (3, "hca_b"), (7, "hca_c"), (8, "unused")]:
            path = self.sysfs / f"uverbs{index}"
            path.mkdir()
            (path / "ibdev").write_text(hca + "\n")
        self.context = context()
        self.context["ranks"][0]["hcas"] = "hca_c:1,hca_a,hca_b"
        self.available = {
            self.devices / name for name in ("rdma_cm", "uverbs0", "uverbs3", "uverbs7")
        }

    def resolve(self):
        with patch.object(Path, "is_char_device", lambda path: path in self.available):
            return node.rdma_devices(self.context, self.sysfs, self.devices)

    def test_all_selected_hcas_are_mapped_without_exposing_unused_devices(self):
        self.assertEqual(
            self.resolve(),
            [
                str(self.devices / name)
                for name in ("rdma_cm", "uverbs0", "uverbs3", "uverbs7")
            ],
        )

    def test_missing_selected_character_device_is_refused(self):
        self.available.remove(self.devices / "uverbs7")
        with self.assertRaisesRegex(RuntimeError, "character device"):
            self.resolve()

    def test_missing_hca_mapping_is_refused(self):
        self.context["ranks"][0]["hcas"] += ",unmapped"
        with self.assertRaisesRegex(RuntimeError, "missing a verbs device mapping"):
            self.resolve()

    def test_container_arguments_include_every_resolved_device(self):
        expected = self.resolve()
        with patch.object(node, "rdma_devices", return_value=expected):
            args = node.arguments(self.context, self.root)
        actual = [args[i + 1] for i, value in enumerate(args) if value == "--device"]
        self.assertEqual(actual, expected)


class DiagnosticTransport(unittest.TestCase):
    """Real Bash, batch supervision and node programs; synthetic Docker/SSH."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for package in ("diagnostics", "model_library", "release_spec", "serving_guard"):
            shutil.copytree(
                ROOT / package,
                self.root / package,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in (
            "diagnostic.sh",
            "sync-image.sh",
            "image-transfer.sh",
            "node-bundle.py",
            "model-library-common.sh",
            "resource_sample.py",
        ):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        # CPU-only fixture memory, never a production environment override.
        with (scripts / "resource_sample.py").open("a") as stream:
            stream.write(
                '\nread_meminfo=lambda *a,**k: {"mem_available_bytes":100*1024**3,"swap_used_bytes":0}\n'
            )
        with (self.root / "diagnostics/images.py").open("a") as stream:
            stream.write(
                '\ndef disk_free(path):\n import os\n rank=os.environ.get("PULSAR_TEST_NODE","0")\n return 0 if os.environ.get("PULSAR_TEST_DISK_LOW_RANK")==rank or (path.name=="containerd-images" and os.environ.get("PULSAR_TEST_CONTAINERD_DISK_LOW_RANK")==rank) else 100*1024**3\n'
            )
        with (self.root / "diagnostics/storage.py").open("a") as stream:
            stream.write(
                '\ndef containerd_roots(info, proc=None):\n import os\n if not os.environ.get("PULSAR_TEST_CONTAINERD_RESOLVED"): raise ValueError("synthetic containerd configuration unavailable")\n return [Path(info["DockerRootDir"]), Path(info["DockerRootDir"])/"containerd-images"]\n'
            )
        # This suite simulates Docker/SSH, without access to host RDMA devices.
        # The real sysfs/device mapping is covered by RdmaDeviceMapping below.
        with (self.root / "diagnostics/node.py").open("a") as stream:
            stream.write(
                '\ndef rdma_devices(context):\n return ["/dev/infiniband/rdma_cm", "/dev/infiniband/uverbs0"]\n'
            )
        (scripts / "lib.sh").write_text("""
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PULSAR_PLATFORM_ID=dgx-spark-gb10
PULSAR_DOCKER=docker
PULSAR_RDMA_VERBS_DEVICE=/dev/infiniband/uverbs0
CLUSTER_TOPOLOGY_ID=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
CLUSTER_TOPOLOGY_COUNT=3
CLUSTER_NODE_IDS=(fixture-0 fixture-1 fixture-2)
CLUSTER_NODE_CONTROL_IPS=(192.0.2.1 192.0.2.2 192.0.2.3)
CLUSTER_NODE_CONTROL_IFS=(fabric0 fabric0 fabric0)
CLUSTER_NODE_HCAS=(fixture_hca:1 fixture_hca:1 fixture_hca:1)
CLUSTER_PROFILE_HCAS=(fixture_hca:1 fixture_hca:1 fixture_hca:1)
die() { echo "$*" >&2; exit 2; }
load_cluster_topology() { return 0; }
reload_cluster_topology() {
 if [ "${PULSAR_TEST_TOPOLOGY_CHANGE:-}" = 1 ] && [ -e "$PULSAR_TEST_DOCKER_STATE/loaded-1" ]; then CLUSTER_TOPOLOGY_ID=changed; fi
 return 0
}
require_cluster_nodes() { return 0; }
require_profile_topology() { return 0; }
require_topology_ssh_trust() { return 0; }
acquire_model_library_lifecycle_lock() { return 0; }
shell_join_q() { printf '%q ' "$@"; }
ssh_node_command() { SSH_NODE_COMMAND=(bash "$REPO_DIR/fake-ssh" "$1"); }
""")
        (self.root / "fake-ssh").write_text(
            'export PULSAR_TEST_NODE="$1"\nshift\nexec bash -c "$1"\n'
        )
        binary = self.root / "bin"
        binary.mkdir()
        shutil.copyfile(ROOT / "tests/fixtures/diagnostic-docker.py", binary / "docker")
        (binary / "docker").chmod(0o755)
        (binary / "nvidia-smi").write_text("""#!/bin/sh
if [ "${PULSAR_TEST_NODE:-0}" = "${PULSAR_TEST_CLEANUP_FAIL_RANK:-none}" ] && [ -f "$PULSAR_TEST_DOCKER_STATE/${PULSAR_TEST_NODE:-0}.started" ]; then exit 1; fi
exit 0
""")
        (binary / "nvidia-smi").chmod(0o755)
        state = self.root / "docker-state"
        state.mkdir()
        payload = self.root / "payload"
        payload.mkdir()
        (payload / "probe.py").write_text("pass\n")
        (self.root / "request.json").write_text(json.dumps(request()))
        self.env = {
            **os.environ,
            "PYTHONPATH": str(self.root),
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "PULSAR_TEST_DOCKER_STATE": str(state),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for key in (
            "PULSAR_VERIFICATION_OWNER",
            "PULSAR_VERIFICATION_REPORT",
            "PULSAR_VERIFICATION_REPORT_FD",
        ):
            self.env.pop(key, None)

    def run_fixture(self, extra=None):
        output = self.root / "evidence"
        result = subprocess.run(
            [
                "bash",
                str(self.root / "scripts/diagnostic.sh"),
                "--request",
                str(self.root / "request.json"),
                "--payload-dir",
                str(self.root / "payload"),
                "--request-id",
                schema.digest(request()),
                "--output-dir",
                str(output),
                "--yes",
            ],
            env={**self.env, **(extra or {})},
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=25,
        )
        record = (
            json.loads((output / "result.json").read_text())
            if (output / "result.json").exists()
            else None
        )
        return result, record

    def test_three_rank_success_and_cleanup(self):
        result, record = self.run_fixture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(record["successful"])
        self.assertEqual(record["phases"]["execute"]["ranks"], 3)
        self.assertFalse(list((self.root / "docker-state").glob("*.json")))

    def test_escaped_log_tail_does_not_truncate_valid_rank_result(self):
        result, record = self.run_fixture({"PULSAR_TEST_ESCAPED_LOG": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(record["successful"])

    def test_wrong_image_prevents_all_gpu_starts(self):
        result, record = self.run_fixture({"PULSAR_TEST_WRONG_IMAGE_RANK": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(record, result.stderr)
        self.assertFalse(record["successful"])
        self.assertFalse(list((self.root / "docker-state").glob("*.started")))
        self.assertTrue(record["phases"]["cleanup"]["complete"])

    def test_failed_rank_cancels_peer_and_retains_failed_result(self):
        result, record = self.run_fixture(
            {"PULSAR_TEST_FAIL_RANK": "1", "PULSAR_TEST_HANG_RANK": "2"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(record, result.stderr)
        self.assertFalse(record["successful"])
        self.assertTrue(record["phases"]["cleanup"]["complete"], result.stderr)
        self.assertFalse(list((self.root / "docker-state").glob("*.json")))

    def test_cleanup_checks_other_ranks_after_one_is_unavailable(self):
        result, record = self.run_fixture({"PULSAR_TEST_CLEANUP_FAIL_RANK": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(record["phases"]["execute"]["complete"], result.stderr)
        self.assertFalse(record["phases"]["cleanup"]["complete"])
        peer = json.loads((self.root / "evidence/cleanup/jobs/2.out").read_text())
        self.assertTrue(peer["cleanup_verified"])

    def test_controller_death_cancels_owned_node_processes(self):
        output = self.root / "evidence"
        process = subprocess.Popen(
            [
                "bash",
                str(self.root / "scripts/diagnostic.sh"),
                "--request",
                str(self.root / "request.json"),
                "--payload-dir",
                str(self.root / "payload"),
                "--request-id",
                schema.digest(request()),
                "--output-dir",
                str(output),
                "--yes",
            ],
            env={**self.env, "PULSAR_TEST_HANG_ALL": "1"},
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                len(list((self.root / "docker-state").glob("*.started"))) != 3
                and time.monotonic() < deadline
            ):
                if process.poll() is not None:
                    self.fail(process.communicate()[1])
                time.sleep(0.05)
            self.assertEqual(
                len(list((self.root / "docker-state").glob("*.started"))), 3
            )
            process.kill()
            process.communicate(timeout=8)
            deadline = time.monotonic() + 4
            while (
                list((self.root / "docker-state").glob("*.json"))
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            self.assertFalse(list((self.root / "docker-state").glob("*.json")))
            self.assertFalse((output / "result.json").exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=8)


class GuardProcesses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "payload").mkdir()
        (self.root / "payload/probe.py").write_text("pass\n")
        self.cg = self.root / "cgroup"
        self.cg.mkdir()
        for name, value in {
            "memory.max": 16 * schema.GIB,
            "memory.swap.max": 0,
            "memory.current": 1000,
            "memory.peak": 1000,
            "memory.events": "oom 0\noom_kill 0\n",
        }.items():
            (self.cg / name).write_text(str(value))
        self.mem = self.root / "meminfo"
        self.mem.write_text(
            "MemAvailable: 104857600 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
        )
        self.ctx = self.root / "context.json"
        self.ctx.write_text(json.dumps(context()))
        for name in context()["guard_files"]:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("pass\n")
        self.children = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def start(self, worker, lease=0.6):
        code = (
            "import json,sys\nfrom pathlib import Path\nfrom diagnostics.guard import run\n"
            f"c=json.loads(Path({str(self.ctx)!r}).read_text())\n"
            f"r=run(c,Path({str(self.root)!r}),cgroup=Path({str(self.cg)!r}),meminfo=Path({str(self.mem)!r}),"
            f'worker=[sys.executable,"-c",{worker!r}],lease={lease},result_file=Path({str(self.root / "result.json")!r}))\n'
            "print(json.dumps(r),flush=True)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.children.append(process)
        return process

    def result(self, process):
        process.stdin.close()
        process.stdin = None
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error)
        return json.loads(output)

    def test_no_gpu_child_before_release_and_eof_cancels(self):
        marker = self.root / "started"
        process = self.start(f"from pathlib import Path;Path({str(marker)!r}).touch()")
        time.sleep(0.1)
        report = self.result(process)
        self.assertFalse(report["successful"])
        self.assertFalse(marker.exists())
        self.assertIn("disconnected", report["error"])

    def test_live_pipe_without_heartbeats_expires_and_reaps_child(self):
        marker = self.root / "child.pid"
        process = self.start(
            f"import os,time;from pathlib import Path;Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)"
        )
        process.stdin.write("G")
        process.stdin.flush()
        process.wait(timeout=5)
        report = self.result(process)
        self.assertFalse(report["successful"])
        self.assertIn("lease expired", report["error"])
        self.assertFalse(Path("/proc", marker.read_text()).exists())

    def test_sigterm_stops_child_and_reports_failure(self):
        marker = self.root / "child.pid"
        process = self.start(
            f"import os,time;from pathlib import Path;Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)",
            lease=5,
        )
        process.stdin.write("G")
        process.stdin.flush()
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=4)
        self.assertFalse(self.result(process)["successful"])
        self.assertFalse(Path("/proc", marker.read_text()).exists())

    def test_guard_numerical_conditions(self):
        limits = request()["limits"]
        before = {"mem_available_bytes": 100 * schema.GIB, "swap_used_bytes": 0}
        sample = {**before, "memory_current_bytes": 1000, "oom": 0, "oom_kill": 0}
        self.assertIsNone(guard.guard_reason(sample, before, limits, 0, 0))
        for update, elapsed, age, expected in [
            ({}, 10, 0, "time limit"),
            ({}, 0, 30, "lease"),
            ({"mem_available_bytes": 83 * schema.GIB}, 0, 0, "available"),
            ({"swap_used_bytes": 65 * 1024**2}, 0, 0, "swap"),
            ({"oom": 1}, 0, 0, "cgroup"),
        ]:
            self.assertIn(
                expected,
                guard.guard_reason({**sample, **update}, before, limits, elapsed, age),
            )

    def test_active_memory_guard_prevents_child_release(self):
        (self.cg / "memory.events").write_text("oom 1\noom_kill 0\n")
        with (
            patch.object(guard.select, "select", return_value=([sys.stdin], [], [])),
            patch.object(guard.os, "read", return_value=b"G"),
            patch.object(
                guard.subprocess,
                "Popen",
                side_effect=RuntimeError("unexpected GPU release"),
            ) as start,
        ):
            report = guard.run(
                context(),
                self.root,
                cgroup=self.cg,
                meminfo=self.mem,
                result_file=self.root / "result.json",
            )
        self.assertFalse(report["successful"])
        start.assert_not_called()

    def test_late_heartbeat_cannot_revive_expired_lease(self):
        value = context()
        value["request"]["limits"]["timeout_seconds"] = 1800
        value["request_id"] = schema.digest(value["request"])
        with (
            patch.object(guard.select, "select", return_value=([sys.stdin], [], [])),
            patch.object(guard.os, "read", return_value=b"G"),
            patch.object(guard.time, "monotonic", side_effect=[0, 31, 31, 31, 31]),
            patch.object(
                guard.subprocess,
                "Popen",
                side_effect=RuntimeError("unexpected GPU release"),
            ) as start,
        ):
            report = guard.run(
                value,
                self.root,
                cgroup=self.cg,
                meminfo=self.mem,
                result_file=self.root / "result.json",
            )
        self.assertFalse(report["successful"])
        start.assert_not_called()

    def test_wrong_cgroup_limit_never_releases_child(self):
        marker = self.root / "started"
        (self.cg / "memory.max").write_text(str(32 * schema.GIB))
        process = self.start(f"from pathlib import Path;Path({str(marker)!r}).touch()")
        _, error = process.communicate("G", timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("memory limit differs", error)
        self.assertFalse(marker.exists())

    def test_stale_result_cannot_pass_a_new_invocation(self):
        (self.root / "result.json").write_text('{"successful":true}')
        process = self.start("pass")
        _, error = process.communicate("G", timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("fresh payload result", error)

    def test_success_requires_payload_result(self):
        process = self.start("pass", lease=5)
        process.stdin.write("G")
        process.stdin.flush()
        process.wait(timeout=4)
        self.assertFalse(self.result(process)["successful"])
        process = self.start(
            f"from pathlib import Path;Path({str(self.root / 'result.json')!r}).write_text('{{\"successful\":true}}')",
            lease=5,
        )
        process.stdin.write("G")
        process.stdin.flush()
        process.wait(timeout=4)
        self.assertTrue(self.result(process)["successful"])


class AllocatorBoundary(unittest.TestCase):
    def test_torch_cap_precedes_payload(self):
        from unittest.mock import Mock

        cuda = Mock()
        cuda.device_count.return_value = 1
        cuda.get_device_capability.return_value = (12, 1)
        cuda.get_device_properties.return_value = types.SimpleNamespace(
            total_memory=128 * schema.GIB
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps(context()))

            def payload(*args, **kwargs):
                cuda.set_per_process_memory_fraction.assert_called_once_with(0.125, 0)

            with (
                patch.dict(sys.modules, {"torch": types.SimpleNamespace(cuda=cuda)}),
                patch.object(sys, "argv", ["worker", str(path)]),
                patch.dict(os.environ),
                patch.object(sys, "path", list(sys.path)),
                patch.object(worker.runpy, "run_path", side_effect=payload) as run,
            ):
                worker.main()
                run.assert_called_once()

    def test_wrong_gpu_rejected_before_payload(self):
        from unittest.mock import Mock

        cuda = Mock()
        cuda.device_count.return_value = 2
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps(context()))
            with (
                patch.dict(sys.modules, {"torch": types.SimpleNamespace(cuda=cuda)}),
                patch.object(sys, "argv", ["worker", str(path)]),
                patch.object(worker.runpy, "run_path") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "exactly one SM121"):
                    worker.main()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
