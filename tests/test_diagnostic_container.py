"""Synthetic diagnostic-container tests. No live Docker, kmsg, topology or GPU."""
from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from release_spec import serving
from release_spec.diagnostic import (
    PROFILE, counted_kernel_failure, freeze_plan, parse_kmsg_record, reduce_event,
    initial_state, complete_stopped_state, emit_entrypoint, terminal_problem,
)
from scripts.diagnostic_runtime import validate_created_container, docker_create_argv
from tests.support.topology_fixture import Fixture

PY = sys.executable
IMAGE = "sha256:" + "a" * 64
CONFIG = "sha256:" + "b" * 64
BOOT = "c" * 32
CID = "d" * 64


def meminfo_text(available_gib=40, swap_used=0):
    avail = available_gib * 1024 * 1024
    swap = swap_used // 1024
    return f"MemAvailable: {avail} kB\nSwapTotal: {swap + 10} kB\nSwapFree: 10 kB\n"


def kmsg_line(seq, message, ts=1000):
    return f"6,{seq},{ts},-;{message}\n".encode()


def definition(inputs, steps=None):
    return {
        "schema_version": 1,
        "kind": "pulsar-diagnostic-definition",
        "image_reference": IMAGE,
        "image_id": IMAGE,
        "image_config_digest": CONFIG,
        "platform": "linux/arm64",
        "inputs": inputs,
        "steps": steps or [{"argv": ["python3", "-I", "/pulsar-check/step.py"], "timeout_seconds": 30}],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "working_directory": "/",
        "observer": {
            "backend": "kmsg",
            "sample_interval_seconds": 0.25,
            "max_observation_age_seconds": 1,
            "mem_available_floor_bytes": PROFILE["mem_available_floor_bytes"],
            "swap_growth_limit_bytes": PROFILE["swap_growth_limit_bytes"],
            "workload_deadline_seconds": 600,
            "operation_seconds": 1800,
            "cleanup_reserve_seconds": 300,
        },
    }


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ["DIAG_FAKE_ROOT"])
args = sys.argv[1:]
with (root / "docker-calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
hang = os.environ.get("DIAG_HANG")
if hang and args and args[0] == hang:
    time.sleep(30)
state_path = root / "docker-state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {"containers": {}, "by_name": {}, "removed": []}
cmd = args[0] if args else ""
image = os.environ.get("DIAG_IMAGE_ID", "sha256:" + "a" * 64)
cid = os.environ.get("DIAG_CID", "d" * 64)
pid = int(os.environ.get("DIAG_PID", "4242"))

def save():
    state_path.write_text(json.dumps(state))

def flag(name, default=None):
    if name in args:
        return args[args.index(name) + 1]
    return default

def has(name):
    return name in args

def inspect_body(item):
    return {"Id": item["id"], "Image": image, "Os": "linux", "Architecture": "arm64",
            "State": item["state"], "HostConfig": item["host"], "Mounts": item["mounts"],
            "Config": item["config"]}

def parse_create(args):
    mounts, labels, env, tmpfs, capdrop, secopt = [], {}, [], {}, [], []
    host = {"Privileged": False, "AutoRemove": False, "Devices": [], "CapAdd": [],
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}, "ReadonlyRootfs": False,
            "DeviceRequests": [], "Tmpfs": {}, "PidMode": "", "UTSMode": ""}
    entrypoint, cmd = None, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--network": host["NetworkMode"] = args[i+1]; i += 2; continue
        if a == "--runtime": host["Runtime"] = args[i+1]; i += 2; continue
        if a == "--read-only": host["ReadonlyRootfs"] = True; i += 1; continue
        if a == "--gpus" and args[i+1].startswith("device="):
            host["DeviceRequests"] = [{"DeviceIDs": [args[i+1].split("=",1)[1]], "Capabilities": [["gpu"]]}]
            i += 2; continue
        if a == "--memory": host["Memory"] = int(args[i+1]); i += 2; continue
        if a == "--memory-swap": host["MemorySwap"] = int(args[i+1]); i += 2; continue
        if a == "--cpus": host["NanoCpus"] = int(float(args[i+1]) * 1e9); i += 2; continue
        if a == "--pids-limit": host["PidsLimit"] = int(args[i+1]); i += 2; continue
        if a == "--cap-drop": capdrop.append(args[i+1]); i += 2; continue
        if a == "--security-opt": secopt.append(args[i+1]); i += 2; continue
        if a == "--ipc": host["IpcMode"] = args[i+1]; i += 2; continue
        if a == "--shm-size": host["ShmSize"] = int(args[i+1]); i += 2; continue
        if a == "--tmpfs":
            path, _, opt = args[i+1].partition(":"); tmpfs[path] = opt; i += 2; continue
        if a == "--restart": host["RestartPolicy"]["Name"] = args[i+1]; i += 2; continue
        if a == "--no-healthcheck": host["_no_healthcheck"] = True; i += 1; continue
        if a == "--entrypoint": entrypoint = [args[i+1]]; i += 2; continue
        if a == "-w": host["_workdir"] = args[i+1]; i += 2; continue
        if a == "-e":
            k, _, v = args[i+1].partition("="); env.append((k, v)); i += 2; continue
        if a == "--mount":
            fields = dict(part.split("=", 1) for part in args[i+1].split(",") if "=" in part)
            dest = fields.get("dst") or fields.get("destination")
            rw = "readonly" not in args[i+1]
            mounts.append({"Type": fields.get("type", "bind"), "Source": fields.get("src") or fields.get("source"),
                           "Destination": dest, "RW": rw,
                           "Propagation": fields.get("bind-propagation") or fields.get("propagation") or ""})
            i += 2; continue
        if a == "--label":
            k, _, v = args[i+1].partition("="); labels[k] = v; i += 2; continue
        if a == "--name": i += 2; continue
        if a == "--pull": i += 2; continue
        if a.startswith("sha256:") or (len(a)==71 and a.startswith("sha256")):
            cmd = args[i+1:]
            break
        i += 1
    host["CapDrop"] = capdrop
    host["SecurityOpt"] = secopt
    host["Tmpfs"] = tmpfs
    health = {"Test": ["NONE"]} if host.pop("_no_healthcheck", False) else None
    workdir = host.pop("_workdir", "/")
    config = {"User": "", "Entrypoint": entrypoint, "Cmd": cmd, "Labels": labels,
              "Env": [f"{k}={v}" for k, v in env], "Healthcheck": health, "WorkingDir": workdir}
    return host, mounts, config

if cmd == "info":
    if has("-f") or "-f" in args:
        print("runc nvidia")
    else:
        print("Runtimes: runc nvidia")
    sys.exit(0)
if cmd == "ps":
    sys.exit(0)
if cmd == "image" and args[1:2] == ["inspect"]:
    print(json.dumps([{"Id": image, "Os": "linux", "Architecture": "arm64",
                       "Config": {"Image": os.environ.get("DIAG_CONFIG_ID", "sha256:" + "b"*64), "Os": "linux", "Architecture": "arm64"}}]))
    sys.exit(0)
if cmd == "create":
    if os.environ.get("DIAG_CREATE_FAIL"):
        sys.stderr.write("create failed\n"); sys.exit(1)
    name = flag("--name")
    host, mounts, config = parse_create(args)
    item = {"id": cid, "name": name, "host": host, "mounts": mounts, "config": config,
            "state": {"Status": "created", "Running": False, "Restarting": False, "OOMKilled": False,
                      "ExitCode": 0, "Error": "", "Pid": 0, "StartedAt": "0001-01-01T00:00:00Z",
                      "FinishedAt": "0001-01-01T00:00:00Z"}}
    state["containers"][cid] = item
    state["by_name"][name] = cid
    save()
    if os.environ.get("DIAG_CREATE_LOST"):
        sys.stderr.write("create reply lost\n"); sys.exit(124)
    print(cid)
    sys.exit(0)
if cmd == "inspect":
    target = args[-1]
    if target in state.get("by_name", {}):
        target = state["by_name"][target]
    if target in state.get("removed", []):
        sys.stderr.write("Error: No such container\n"); sys.exit(1)
    item = state["containers"].get(target)
    if item is None:
        sys.stderr.write("Error: No such container\n"); sys.exit(1)
    if item["state"].get("Running") is True and os.environ.get("DIAG_KEEP_RUNNING") != "1":
        item["_live"] = item.get("_live", 0) + 1
        if item["_live"] >= 2:
            item["state"].update(Status="exited", Running=False, Restarting=False, Pid=0,
                                 ExitCode=int(os.environ.get("DIAG_EXIT_CODE", "0")),
                                 FinishedAt="2026-09-19T00:00:01Z")
        save()
    body = inspect_body(item)
    if os.environ.get("DIAG_EMPTY_STATE"):
        calls = [json.loads(line) for line in (root/"docker-calls.jsonl").read_text().splitlines() if line.strip()]
        if any(c[0] == "stop" for c in calls):
            body["State"] = {}
    print(json.dumps([body])); sys.exit(0)
if cmd == "start":
    target = args[-1]
    item = state["containers"][target]
    item["state"] = {"Status": "running", "Running": True, "Restarting": False, "OOMKilled": False,
                     "ExitCode": 0, "Error": "", "Pid": pid,
                     "StartedAt": "2026-09-19T00:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z"}
    save()
    if os.environ.get("DIAG_START_FAIL"):
        sys.stderr.write("start failed\n"); sys.exit(1)
    sys.exit(0)
if cmd == "logs":
    sys.stdout.write(os.environ.get("DIAG_LOGS", "PULSAR_STEP 0 start\nPULSAR_STEP 0 ok\n"))
    sys.exit(0)
if cmd == "stop":
    if "--timeout" not in args or args[args.index("--timeout")+1] != "-1":
        sys.stderr.write("expected cooperative stop\n"); sys.exit(2)
    if "-f" in args or "--force" in args:
        sys.stderr.write("force stop refused\n"); sys.exit(2)
    target = args[-1]
    item = state["containers"][target]
    item["state"].update(Status="exited", Running=False, Pid=0, FinishedAt="2026-09-19T00:00:02Z")
    extra = os.environ.get("DIAG_KMSG_ON_STOP")
    kmsg = os.environ.get("PULSAR_KMSG_PATH")
    if extra and kmsg:
        fd = os.open(kmsg, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(fd, extra.encode())
        finally:
            os.close(fd)
    save()
    if os.environ.get("DIAG_STOP_FAIL"):
        sys.stderr.write("stop failed\n"); sys.exit(1)
    sys.exit(0)
if cmd == "rm":
    if "-f" in args or "--force" in args:
        sys.stderr.write("rm -f refused\n"); sys.exit(2)
    target = args[-1]
    state["removed"].append(target)
    state["containers"].pop(target, None)
    save()
    sys.exit(0)
sys.stderr.write("unsupported docker " + cmd + "\n"); sys.exit(2)
'''


class DiagnosticUnit(unittest.TestCase):
    def test_kmsg_parse_and_markers(self):
        record = parse_kmsg_record(kmsg_line(3, "NVRM: NV_ERR_NO_MEMORY"))
        self.assertEqual(record["sequence"], 3)
        self.assertTrue(counted_kernel_failure(record["message"]))
        self.assertFalse(counted_kernel_failure('apparmor="DENIED"'))
        with self.assertRaises(Exception):
            parse_kmsg_record(b"not-a-record")

    def test_sequence_gap_latches_coverage(self):
        state = initial_state({"plan_id": "p", "boot_id": BOOT})
        state["phase"] = "observing"
        reduce_event(state, {"kind": "kernel", "boot_id": BOOT, "record": parse_kmsg_record(kmsg_line(1, "ok")),
                             "received_monotonic_ns": 1}, floor=4, swap_limit=1, baseline_swap=0)
        reduce_event(state, {"kind": "kernel", "boot_id": BOOT, "record": parse_kmsg_record(kmsg_line(3, "ok")),
                             "received_monotonic_ns": 2}, floor=4, swap_limit=1, baseline_swap=0)
        self.assertFalse(state["coverage_ok"])

    def test_memory_breach_survives_healthy_sample(self):
        state = initial_state({"plan_id": "p", "boot_id": BOOT})
        state["phase"] = "observing"
        state["baseline_swap_bytes"] = 0
        floor = PROFILE["mem_available_floor_bytes"]
        reduce_event(state, {"kind": "memory", "boot_id": BOOT, "mem_available_bytes": floor - 1,
                             "swap_used_bytes": 0, "sampled_at": "t1", "monotonic_ns": 1},
                     floor=floor, swap_limit=256 * 1024 ** 2, baseline_swap=0)
        reduce_event(state, {"kind": "memory", "boot_id": BOOT, "mem_available_bytes": floor * 2,
                             "swap_used_bytes": 0, "sampled_at": "t2", "monotonic_ns": 2},
                     floor=floor, swap_limit=256 * 1024 ** 2, baseline_swap=0)
        from release_spec.diagnostic import memory_problem
        self.assertIsNotNone(memory_problem(state, floor=floor, swap_limit=256 * 1024 ** 2))

    def test_empty_state_is_not_stopped(self):
        self.assertIsNotNone(complete_stopped_state({}))
        self.assertIsNotNone(complete_stopped_state({"Status": "exited"}))
        self.assertIsNone(complete_stopped_state({
            "Status": "exited", "Running": False, "Restarting": False, "Pid": 0, "ExitCode": 0,
            "OOMKilled": False, "Error": "", "StartedAt": "t", "FinishedAt": "t"}))

    def test_network_none_still_invalid_for_serving_spec(self):
        spec = json.loads((ROOT / "tests/fixtures/contracts/spec.json").read_text())
        with self.assertRaises(serving.ReleaseSpecError):
            serving.apply_overrides(spec, {"container": {"network_mode": "none"}})

    def test_security_and_command_must_be_exact(self):
        plan = {"plan_id": "e" * 64, "node_id": "n", "definition": {"image_id": IMAGE}}
        inspect = {
            "Id": CID, "Image": IMAGE,
            "State": {"Status": "created", "Running": False},
            "HostConfig": {
                "NetworkMode": "none", "Privileged": False, "AutoRemove": False, "Runtime": "runc",
                "DeviceRequests": [{"DeviceIDs": ["0"], "Capabilities": [["gpu"]]}],
                "Devices": [], "Memory": PROFILE["memory_bytes"], "MemorySwap": PROFILE["memory_swap_bytes"],
                "NanoCpus": PROFILE["nano_cpus"], "PidsLimit": 256, "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges=false"], "ReadonlyRootfs": True, "IpcMode": "private",
                "ShmSize": PROFILE["shm_size_bytes"],
                "Tmpfs": {"/tmp": "rw,nosuid,nodev,exec,size=512m"},
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            },
            "Mounts": [
                {"Type": "bind", "Source": "/in", "Destination": "/pulsar-check", "RW": False,
                 "Propagation": "rprivate"},
            ],
            "Config": {"Entrypoint": ["/bin/bash"],
                       "Cmd": ["/pulsar-check/pulsar-diagnostic-entrypoint.sh"],
                       "WorkingDir": "/",
                       "Labels": {"io.pulsar.diagnostic": "true", "io.pulsar.diagnostic.plan-id": "e" * 64},
                       "Healthcheck": {"Test": ["NONE"]}},
        }
        self.assertIsNotNone(validate_created_container(inspect, plan, input_dir="/in"))
        inspect["HostConfig"]["SecurityOpt"] = ["no-new-privileges"]
        inspect["HostConfig"]["IpcMode"] = "host"
        self.assertIsNotNone(validate_created_container(inspect, plan, input_dir="/in"))
        inspect["HostConfig"]["IpcMode"] = "private"
        inspect["Config"]["Cmd"] = ["printf", "pulsar-diagnostic-entrypoint.sh"]
        self.assertIsNotNone(validate_created_container(inspect, plan, input_dir="/in"))

    def test_first_step_failure_is_encoded_without_shell_semicolon(self):
        text = emit_entrypoint({"environment": {}, "steps": [
            {"argv": ["python3", "-I", "/pulsar-check/one.py"], "timeout_seconds": 10},
            {"argv": ["python3", "-u", "/pulsar-check/two.py"], "timeout_seconds": 10},
        ]})
        self.assertIn("set -euo pipefail", text)
        body = text.split("pipefail", 1)[1]
        self.assertNotIn(";", body.replace("'\"'\"'", ""))
        self.assertLess(text.index("one.py"), text.index("two.py"))
        self.assertIn("timeout --signal=TERM", text)


class DiagnosticCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        script = self.inputs / "step.py"
        script.write_text("print('ok')\n")
        os.chmod(script, 0o644)
        info = script.stat()
        self.defn = definition([{
            "name": "step.py", "sha256": __import__("hashlib").sha256(script.read_bytes()).hexdigest(),
            "bytes": info.st_size, "mode": stat.S_IMODE(info.st_mode),
        }])
        (self.root / "definition.json").write_text(json.dumps(self.defn))
        os.mkfifo(self.root / "kmsg")
        self.kmsg_fd = os.open(self.root / "kmsg", os.O_RDWR | os.O_NONBLOCK)
        def _close_kmsg():
            fd = getattr(self, "kmsg_fd", -1)
            if fd >= 0:
                os.close(fd)
                self.kmsg_fd = -1
        self.addCleanup(_close_kmsg)
        (self.root / "boot").write_text(BOOT + "\n")
        (self.root / "meminfo").write_text(meminfo_text())
        topo = self.root / "topo"
        topo.mkdir()
        self.fixture = Fixture(topo, nodes=1)
        self.docker = self.root / "docker"
        self.docker.write_text(FAKE_DOCKER)
        os.chmod(self.docker, 0o755)
        bin_dir = self.root / "topo" / "bin"
        nvidia = bin_dir / "nvidia-smi"
        nvidia.write_text("#!/bin/sh\necho NVIDIA GB10\n")
        os.chmod(nvidia, 0o755)
        for name, body in (
            ("ss", "#!/bin/sh\nexit 0\n"),
            ("lsof", "#!/bin/sh\nexit 1\n"),
            ("curl", "#!/bin/sh\nexit 1\n"),
        ):
            path = bin_dir / name
            path.write_text(body)
            os.chmod(path, 0o755)
        proc = self.root / "proc" / "4242"
        proc.mkdir(parents=True)
        rest = ["S", "1", "4242", "4242", "0", "-1", "0", "0", "0", "0", "0", "0", "0", "0", "0", "20", "0", "1", "0", "999"]
        (proc / "stat").write_text("4242 (workload) " + " ".join(rest) + "\n")
        (proc / "cgroup").write_text("0::/diag-test\n")
        cgroup = self.root / "cgroup" / "diag-test"
        cgroup.mkdir(parents=True)
        (cgroup / "memory.current").write_text("1000\n")
        (cgroup / "memory.peak").write_text("2000\n")
        (cgroup / "memory.swap.current").write_text("0\n")
        (cgroup / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
        homes = self.root / "homes"; homes.mkdir()
        hot = self.root / "hot"; hot.mkdir()
        self.env = dict(self.fixture.env)
        self.env.update({
            "PULSAR_DOCKER": str(self.docker),
            "PULSAR_NVIDIA_SMI": str(nvidia),
            "PULSAR_KMSG_PATH": str(self.root / "kmsg"),
            "PULSAR_BOOT_ID_PATH": str(self.root / "boot"),
            "PULSAR_MEMINFO_PATH": str(self.root / "meminfo"),
            "PULSAR_MEMINFO_FILE": str(self.root / "meminfo"),
            "PULSAR_PROC_ROOT": str(self.root / "proc"),
            "PULSAR_CGROUP_ROOT": str(self.root / "cgroup"),
            "PULSAR_HOME_ROOT": str(homes),
            "PULSAR_HOT_ROOT": str(hot),
            "PULSAR_COLD_ROOT": "",
            "DIAG_FAKE_ROOT": str(self.root),
            "DIAG_IMAGE_ID": IMAGE,
            "DIAG_CONFIG_ID": CONFIG,
            "DIAG_CID": CID,
            "DIAG_PID": "4242",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        })

    def pulsar(self, *args, extra=None, timeout=20):
        env = dict(self.env)
        if extra:
            env.update(extra)
        return subprocess.run([str(ROOT / "pulsar"), *args], env=env, text=True,
                              capture_output=True, timeout=timeout, cwd=self.root)

    def plan(self):
        out = self.root / "plan.json"
        result = self.pulsar("diagnostic", "plan", "--definition", str(self.root / "definition.json"),
                             "--node", "fixture-node-0", "--inputs", str(self.inputs),
                             "--out", str(out), "--json")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)["result"]

    def test_plan_is_non_executing_and_hash_bound(self):
        plan = self.plan()
        self.assertEqual(plan["kind"], "pulsar-diagnostic-plan")
        self.assertEqual(len(plan["plan_id"]), 64)
        self.assertFalse((self.root / "docker-calls.jsonl").exists())

    def test_denied_kmsg_refuses_before_create(self):
        self.plan()
        os.close(self.kmsg_fd)
        self.kmsg_fd = -1
        os.chmod(self.root / "kmsg", 0)
        attempt = self.root / "attempt-deny"
        result = self.pulsar("diagnostic", "run", "--plan", str(self.root / "plan.json"),
                             "--attempt-dir", str(attempt), "--yes", "--json")
        os.chmod(self.root / "kmsg", 0o644)
        payload = json.loads(result.stdout)["result"] if result.stdout.strip().startswith("{") else {}
        if result.returncode == 0:
            self.assertEqual(payload.get("outcome"), "preflight_failed", payload)
        else:
            self.assertNotEqual(result.returncode, 0)
        calls = []
        if (self.root / "docker-calls.jsonl").exists():
            calls = [json.loads(line) for line in (self.root / "docker-calls.jsonl").read_text().splitlines()]
        self.assertFalse(any(c[0] in ("create", "start") for c in calls), calls)

    def test_complete_run_and_show(self):
        self.plan()
        attempt = self.root / "attempt-ok"
        result = self.pulsar("diagnostic", "run", "--plan", str(self.root / "plan.json"),
                             "--attempt-dir", str(attempt), "--yes", "--json")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)["result"]
        self.assertEqual(payload["outcome"], "succeeded", payload)
        self.assertTrue(payload["observation"]["drain_complete"])
        calls = [json.loads(line) for line in (self.root / "docker-calls.jsonl").read_text().splitlines()]
        self.assertEqual([c for c in calls if c[0] == "start"], [["start", CID]])
        self.assertIn(["stop", "--timeout", "-1", CID], calls)
        self.assertIn(["rm", CID], calls)
        self.assertFalse(any(c[0] in ("rm", "stop") and ("-f" in c or "--force" in c) for c in calls))
        shown = self.pulsar("diagnostic", "show", "--attempt-dir", str(attempt), "--json")
        self.assertEqual(json.loads(shown.stdout)["result"]["outcome"], "succeeded")

    def test_queued_kernel_error_on_stop_is_not_success(self):
        self.plan()
        attempt = self.root / "attempt-kmsg"
        extra = {"DIAG_KMSG_ON_STOP": kmsg_line(1, "safe").decode() + kmsg_line(2, "NVRM: NV_ERR_NO_MEMORY").decode()}
        result = self.pulsar("diagnostic", "run", "--plan", str(self.root / "plan.json"),
                             "--attempt-dir", str(attempt), "--yes", "--json", extra=extra)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)["result"]
        self.assertNotEqual(payload["outcome"], "succeeded", payload)
        self.assertGreater(payload["observation"].get("driver_memory_errors") or 0, 0)

    def test_empty_stopped_state_does_not_remove(self):
        self.plan()
        attempt = self.root / "attempt-empty"
        result = self.pulsar("diagnostic", "run", "--plan", str(self.root / "plan.json"),
                             "--attempt-dir", str(attempt), "--yes", "--json",
                             extra={"DIAG_EMPTY_STATE": "1"})
        payload = json.loads(result.stdout)["result"]
        calls = [json.loads(line) for line in (self.root / "docker-calls.jsonl").read_text().splitlines()]
        self.assertFalse(any(c[0] == "rm" for c in calls), payload)
        self.assertNotEqual(payload.get("outcome"), "succeeded")

    def test_concurrent_loser_does_not_write_winner(self):
        self.plan()
        attempt = self.root / "attempt-race"
        winner = subprocess.Popen(
            [str(ROOT / "pulsar"), "diagnostic", "run", "--plan", str(self.root / "plan.json"),
             "--attempt-dir", str(attempt), "--yes", "--json"],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (attempt / "claim.json").exists():
            if winner.poll() is not None:
                break
            time.sleep(0.02)
        loser = subprocess.run(
            [str(ROOT / "pulsar"), "diagnostic", "run", "--plan", str(self.root / "plan.json"),
             "--attempt-dir", str(attempt), "--yes", "--json"],
            env=self.env, text=True, capture_output=True, timeout=10)
        winner_out, winner_err = winner.communicate(timeout=15)
        self.assertNotEqual(loser.returncode, 0, loser.stdout + loser.stderr)
        self.assertTrue(winner.returncode == 0 or (attempt / "result.json").exists(),
                        winner_err + (winner_out or ""))
        if (attempt / "claim.json").exists():
            claim = json.loads((attempt / "claim.json").read_text())
            self.assertNotEqual(claim.get("owner_pid"), os.getpid())

    def test_sigterm_after_start_still_stops(self):
        self.plan()
        attempt = self.root / "attempt-term"
        child = subprocess.Popen(
            [str(ROOT / "pulsar"), "diagnostic", "run", "--plan", str(self.root / "plan.json"),
             "--attempt-dir", str(attempt), "--yes", "--json"],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not (attempt / "container-start.json").exists():
            if child.poll() is not None:
                break
            time.sleep(0.02)
        if child.poll() is None:
            os.kill(child.pid, signal.SIGTERM)
        try:
            stdout, stderr = child.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.kill(child.pid, signal.SIGKILL)
            stdout, stderr = child.communicate()
            self.fail({"stderr": stderr, "stdout": stdout, "timeout": True})
        calls_path = self.root / "docker-calls.jsonl"
        calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
        self.assertTrue(any(c[0] == "stop" for c in calls) or (attempt / "guard-stop.json").exists(),
                        {"exit": child.returncode, "calls": calls, "stderr": stderr, "stdout": stdout})

    def test_stale_terminal_is_rejected(self):
        plan = freeze_plan(self.defn, node_id="fixture-node-0", inputs_root=str(self.inputs))
        claim = {"plan_id": plan["plan_id"], "attempt_nonce": "n", "boot_id": BOOT,
                 "container_id": CID, "image_id": IMAGE}
        terminal = initial_state({**claim, "plan_id": plan["plan_id"]})
        terminal.update(phase="finished", observer_exit_code=0, coverage_ok=True, drain_complete=True,
                        drain_monotonic_ns=5, last_sample_monotonic_ns=1, samples=1, kernel_records=0,
                        driver_memory_errors=0, min_mem_available_bytes=40 * 1024 ** 3,
                        mem_available_bytes=40 * 1024 ** 3, swap_used_bytes=0, max_swap_used_bytes=0)
        problem = terminal_problem(terminal, plan=plan, claim=claim, previous=None,
                                   cleanup_monotonic_ns=10, baseline_swap=0)
        self.assertIsNotNone(problem)

    def test_serving_contract_still_advertises_start(self):
        result = subprocess.run([str(ROOT / "pulsar"), "contract", "--json"], text=True,
                                capture_output=True, cwd=self.root)
        document = json.loads(result.stdout)["result"]
        self.assertIn("start", document["operations"])
        self.assertIn("diagnostic.run", document["operations"])

    def test_doctor_gpu_mismatch_bars_create(self):
        self.plan()
        nvidia = self.root / "topo" / "bin" / "nvidia-smi"
        nvidia.write_text("#!/bin/sh\necho OtherGPU\n")
        os.chmod(nvidia, 0o755)
        result = self.pulsar("diagnostic", "run", "--plan", str(self.root / "plan.json"),
                             "--attempt-dir", str(self.root / "attempt-doctor"), "--yes", "--json")
        payload = json.loads(result.stdout)["result"] if result.stdout.strip().startswith("{") else {}
        self.assertEqual(payload.get("outcome"), "preflight_failed", payload)
        calls = []
        if (self.root / "docker-calls.jsonl").exists():
            calls = [json.loads(line) for line in (self.root / "docker-calls.jsonl").read_text().splitlines()]
        self.assertFalse(any(c[0] in ("create", "start") for c in calls), calls)

    def test_create_hang_is_bounded(self):
        self.plan()
        started = time.monotonic()
        result = self.pulsar("diagnostic", "run", "--plan", str(self.root / "plan.json"),
                             "--attempt-dir", str(self.root / "attempt-hang"), "--yes", "--json",
                             extra={"DIAG_HANG": "create"}, timeout=12)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 8, elapsed)
        payload = json.loads(result.stdout)["result"] if result.stdout.strip().startswith("{") else {}
        self.assertNotEqual(payload.get("outcome"), "succeeded", payload)
        calls = []
        if (self.root / "docker-calls.jsonl").exists():
            calls = [json.loads(line) for line in (self.root / "docker-calls.jsonl").read_text().splitlines()]
        self.assertFalse(any(c[0] == "start" for c in calls), calls)

    def test_nonzero_workload_exit_is_not_success(self):
        self.plan()
        result = self.pulsar("diagnostic", "run", "--plan", str(self.root / "plan.json"),
                             "--attempt-dir", str(self.root / "attempt-failstep"), "--yes", "--json",
                             extra={"DIAG_EXIT_CODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)["result"]
        self.assertNotEqual(payload.get("outcome"), "succeeded", payload)
        self.assertEqual(payload.get("workload_exit_code"), 1)

    def test_cgroup_oom_is_not_success(self):
        self.plan()
        (self.root / "cgroup" / "diag-test" / "memory.events").write_text(
            "low 0\nhigh 0\nmax 0\noom 1\noom_kill 1\n")
        result = self.pulsar("diagnostic", "run", "--plan", str(self.root / "plan.json"),
                             "--attempt-dir", str(self.root / "attempt-oom"), "--yes", "--json")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)["result"]
        self.assertNotEqual(payload.get("outcome"), "succeeded", payload)

    def test_cleanup_only_never_starts(self):
        attempt = self.root / "attempt-cleanup"
        attempt.mkdir()
        result = self.pulsar("diagnostic", "cleanup", "--attempt-dir", str(attempt), "--yes", "--json")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)["result"]
        self.assertEqual(payload.get("outcome"), "cleanup_unconfirmed", payload)
        self.assertTrue(payload.get("cleanup_only"))
        calls = []
        if (self.root / "docker-calls.jsonl").exists():
            calls = [json.loads(line) for line in (self.root / "docker-calls.jsonl").read_text().splitlines()]
        self.assertFalse(any(c[0] in ("create", "start") for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
