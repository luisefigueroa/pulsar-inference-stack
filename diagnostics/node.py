"""One supervised node's Docker checks and diagnostic container adapter.

Bash supplies confirmed placement and the existing framed SSH transport. This
helper never discovers nodes, stages images, reads model bytes or stops services.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time

from diagnostics.schema import CONTAINER_NAME, labels, owned, validate_context
from model_library.verification_process import cancellation_signals
from scripts.resource_sample import read_meminfo

# JSON escaping can expand a 64 KiB log tail to nearly 384 KiB. Leave room for
# that, the bounded payload result and Docker's own diagnostics; never parse a
# silently truncated report.
MAX_REPORT_CHARS = 1024 * 1024


def rdma_devices(
    context, sysfs=Path("/sys/class/infiniband_verbs"), devices=Path("/dev/infiniband")
):
    """Resolve every selected HCA to its actual verbs device on this rank."""
    names = set()
    for selector in context["ranks"][context["rank"]]["hcas"].split(","):
        name = selector.removeprefix("=").split(":", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise RuntimeError(
                "explicit HCA names required for diagnostic device mapping"
            )
        names.add(name)
    mapping = {}
    for path in sorted(sysfs.glob("uverbs*")):
        if not re.fullmatch(r"uverbs[0-9]+", path.name):
            continue
        hca = (path / "ibdev").read_text().strip()
        if hca in names:
            if hca in mapping:
                raise RuntimeError("ambiguous verbs device for selected HCA")
            mapping[hca] = devices / path.name
    if set(mapping) != names:
        raise RuntimeError("selected HCA is missing a verbs device mapping")
    paths = [devices / "rdma_cm", *sorted(mapping.values())]
    if not all(path.is_char_device() for path in paths):
        raise RuntimeError("selected RDMA character device is unavailable")
    return [str(path) for path in paths]


def docker(*args, check=True):
    result = subprocess.run(
        [os.environ.get("PULSAR_DOCKER", "docker"), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if check and result.returncode:
        raise RuntimeError("Docker operation failed: " + args[0])
    return result


def inspect_named(context):
    result = docker(
        "container",
        "ls",
        "-aq",
        "--no-trunc",
        "--filter",
        "name=^/" + CONTAINER_NAME + "$",
    )
    ids = result.stdout.split()
    if not ids:
        return None
    if len(ids) != 1:
        raise RuntimeError("ambiguous diagnostic container")
    return json.loads(docker("container", "inspect", ids[0]).stdout)[0]


def idle():
    ids = docker("ps", "-q").stdout.split()
    if ids:
        for info in json.loads(docker("container", "inspect", *ids).stdout):
            host = info.get("HostConfig", {})
            if host.get("DeviceRequests") or any(
                "nvidia" in d.get("PathOnHost", "") for d in host.get("Devices") or []
            ):
                raise RuntimeError("GPU container already running")
    result = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode or result.stdout.strip():
        raise RuntimeError("GPU is occupied or process inventory unavailable")


def preflight(context):
    validate_context(context)
    info = json.loads(
        docker("image", "inspect", context["request"]["image_id"]).stdout
    )[0]
    if info["Id"] != context["request"]["image_id"] or info["Architecture"] != "arm64":
        raise RuntimeError("exact ARM64 image is not present")
    if inspect_named(context) is not None:
        raise RuntimeError(
            "diagnostic container name is occupied; no replacement permitted"
        )
    idle()
    memory = read_meminfo()
    limits = context["request"]["limits"]
    if (
        memory is None
        or memory["mem_available_bytes"]
        < limits["min_host_available_bytes"] + limits["memory_bytes"]
    ):
        raise RuntimeError("insufficient available host memory")
    selected_devices = rdma_devices(context)
    return {
        "rank": context["rank"],
        "request_id": context["request_id"],
        "run_id": context["run_id"],
        "image_id": info["Id"],
        "ready": True,
        "rdma_devices": selected_devices,
    }


def arguments(context, root):
    request = context["request"]
    row = context["ranks"][context["rank"]]
    memory = str(request["limits"]["memory_bytes"])
    env = {
        "NVIDIA_VISIBLE_DEVICES": "0",
        "CUDA_VISIBLE_DEVICES": "0",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "MAX_JOBS": "1",
        "CMAKE_BUILD_PARALLEL_LEVEL": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "RANK": str(context["rank"]),
        "WORLD_SIZE": str(request["geometry"]["nodes"]),
        "LOCAL_RANK": "0",
        "MASTER_ADDR": context["ranks"][0]["control_ip"],
        "MASTER_PORT": str(request["rendezvous_port"]),
        "NCCL_NET": "IB",
        "NCCL_IB_DISABLE": "0",
        "NCCL_IB_HCA": row["hcas"],
        "NCCL_SOCKET_IFNAME": row["control_if"],
        "GLOO_SOCKET_IFNAME": row["control_if"],
        "NCCL_IB_QPS_PER_CONNECTION": "4",
        "NCCL_DEBUG": "INFO",
    }
    args = [
        "run",
        "--rm",
        "-i",
        "--sig-proxy=false",
        "--name",
        CONTAINER_NAME,
        "--pull",
        "never",
        "--gpus",
        "device=0",
        "--network",
        "host",
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--cpus",
        "4",
        "--pids-limit",
        "512",
        "--shm-size",
        "1g",
        "--restart",
        "no",
        "--no-healthcheck",
        "--workdir",
        "/diagnostic",
        "--ulimit",
        "memlock=-1:-1",
        "--mount",
        f"type=bind,src={root},dst=/diagnostic,readonly",
        "--mount",
        "type=bind,src=/proc/meminfo,dst=/diagnostic-host-meminfo,readonly",
    ]
    for path in rdma_devices(context):
        args += ["--device", path]
    for key, value in labels(context).items():
        args += ["--label", key + "=" + value]
    for key, value in env.items():
        args += ["-e", key + "=" + value]
    args += ["--entrypoint", "python3", request["image_id"], "-m", "diagnostics.guard"]
    return args


def verify_container(info, context, root):
    if not owned(info, context):
        raise RuntimeError("container identity differs")
    host = info["HostConfig"]
    limits = context["request"]["limits"]
    if (
        host["Memory"] != limits["memory_bytes"]
        or host["MemorySwap"] != limits["memory_bytes"]
        or host["NetworkMode"] != "host"
        or host["RestartPolicy"]["Name"] != "no"
        or not host["AutoRemove"]
        or host.get("Privileged")
        or host.get("PidMode")
        or host.get("IpcMode") == "host"
        or host.get("NanoCpus") != 4_000_000_000
        or host.get("PidsLimit") != 512
        or host.get("ShmSize") != 1024**3
    ):
        raise RuntimeError("container limits or isolation differ")
    if sorted(
        (m["Source"], m["Destination"], m["RW"]) for m in info["Mounts"]
    ) != sorted(
        [
            (str(root), "/diagnostic", False),
            ("/proc/meminfo", "/diagnostic-host-meminfo", False),
        ]
    ):
        raise RuntimeError("unexpected mount or writable diagnostic payload")
    requests = host["DeviceRequests"]
    if len(requests) != 1 or requests[0]["DeviceIDs"] != ["0"]:
        raise RuntimeError("GPU placement differs")
    expected = arguments(context, root)
    expected_env = dict(
        expected[i + 1].split("=", 1) for i, flag in enumerate(expected) if flag == "-e"
    )
    actual_env = dict(value.split("=", 1) for value in info["Config"].get("Env", []))
    if any(actual_env.get(key) != value for key, value in expected_env.items()):
        raise RuntimeError("rank, fabric or guard environment differs")
    actual_devices = sorted(
        (value["PathOnHost"], value["PathInContainer"])
        for value in host.get("Devices", [])
    )
    expected_devices = sorted(
        (expected[i + 1], expected[i + 1])
        for i, flag in enumerate(expected)
        if flag == "--device"
    )
    if actual_devices != expected_devices:
        raise RuntimeError("RDMA device placement differs")
    if info["Config"]["Entrypoint"] != ["python3"] or info["Config"]["Cmd"] != [
        "-m",
        "diagnostics.guard",
    ]:
        raise RuntimeError("guard command differs")
    if info["Config"].get("WorkingDir") != "/diagnostic":
        raise RuntimeError("guard working directory differs")


def cleanup(context):
    info = inspect_named(context)
    if info is not None:
        if not owned(info, context):
            raise RuntimeError(
                "unrelated container occupies diagnostic name; preserved"
            )
        docker("rm", "-f", info["Id"])
    if inspect_named(context) is not None:
        raise RuntimeError("diagnostic container cleanup unconfirmed")
    return {
        "rank": context["rank"],
        "run_id": context["run_id"],
        "request_id": context["request_id"],
        "cleanup_verified": True,
    }


def execute(context, root):
    preflight(
        context
    )  # Recheck after complete all-rank preflight; no reuse of stale idleness.
    (root / "payload").mkdir()
    for name, encoded in context["payload"].items():
        (root / "payload" / name).write_bytes(base64.b64decode(encoded, validate=True))
    (root / "context.json").write_text(json.dumps(context))
    # Raw output stays in this node's owned scratch directory and in the
    # controller's framed result. Container guard bounds payload log capture.
    process = None
    output = root / "container-output"
    started = time.monotonic()
    try:
        with output.open("w+") as stream, cancellation_signals() as cancelled:
            process = subprocess.Popen(
                [os.environ.get("PULSAR_DOCKER", "docker"), *arguments(context, root)],
                stdin=subprocess.PIPE,
                stdout=stream,
                stderr=stream,
            )
            info = None
            while process.poll() is None and time.monotonic() - started < 20:
                if cancelled["signal"]:
                    raise RuntimeError("node driver cancelled before release")
                info = inspect_named(context)
                if info is not None and info["State"]["Running"]:
                    break
                time.sleep(0.1)
            if info is None or not info["State"]["Running"]:
                raise RuntimeError("guard container did not start")
            verify_container(info, context, root)
            process.stdin.write(b"G")
            process.stdin.flush()
            while process.poll() is None:
                if (
                    cancelled["signal"]
                    or time.monotonic() - started
                    > context["request"]["limits"]["timeout_seconds"] + 30
                ):
                    raise RuntimeError("node driver cancelled or timed out")
                try:
                    process.stdin.write(b".")
                    process.stdin.flush()
                except BrokenPipeError:
                    break
                time.sleep(0.5)
            process.wait(timeout=5)
            stream.seek(0)
            text = stream.read(MAX_REPORT_CHARS + 1)
            if len(text) > MAX_REPORT_CHARS:
                raise RuntimeError("container report exceeds size limit")
            lines = text.splitlines()
            reports = []
            for line in lines:
                try:
                    value = json.loads(line)
                    if (
                        isinstance(value, dict)
                        and value.get("kind") == "pulsar-diagnostic-rank"
                    ):
                        reports.append(value)
                except ValueError:
                    pass
            if len(reports) != 1:
                raise RuntimeError("missing or ambiguous container result")
            report = reports[0]
            if process.returncode or report.get("successful") is not True:
                raise RuntimeError("payload failed: " + json.dumps(report))
            return report
    finally:
        if process is not None:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
        cleanup(context)


def main(context, root):
    try:
        validate_context(context)
        operation = context["operation"]
        if operation.startswith("image-"):
            from diagnostics.images import observe

            result = observe(context, docker, idle, read_meminfo)
        elif operation == "preflight":
            result = preflight(context)
        elif operation == "execute":
            result = execute(context, root)
        elif operation == "cleanup":
            result = cleanup(context)
            idle()
            result["idle"] = True
        else:
            raise ValueError("unknown diagnostic node operation")
        print(json.dumps(result), flush=True)
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # Verification transport preserves stderr on failed/cancelled ranks.
        import sys

        print("diagnostic rank failed: " + str(exc), file=sys.stderr, flush=True)
        return 3
