"""Collect one node's read-only inventory. The controller sends this file over SSH."""
import json
import math
from pathlib import Path
import subprocess
import sys


OWNED_LABELS = frozenset("io.pulsar.gb10." + name for name in (
    "managed", "conf", "rank", "world-size", "topology", "node-id", "weight-source",
    "weight-owner", "weight-config", "model-revision", "model-identity-status",
    "launch-contract", "spec-decode", "spec-id", "selected-spec-id", "launch-plan",
))


def output(*command):
    try:
        return subprocess.check_output(command, stderr=subprocess.DEVNULL, text=True, errors="replace")
    except (OSError, subprocess.CalledProcessError):
        return None


def memory_gib(path):
    values = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemAvailable:", "MemTotal:") and parts[1].isdigit():
            values[parts[0]] = round(int(parts[1]) / 1048576, 2)
    return values


def gpu_processes(smi):
    rows = []
    csv = output(smi, "--query-compute-apps=pid,process_name,used_gpu_memory", "--format=csv,noheader,nounits")
    for line in (csv or "").splitlines():
        parts = [field.strip() for field in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        try:
            mib = int(float(parts[2].replace("MiB", "").strip()))
        except (IndexError, ValueError, OverflowError):
            mib = None
        rows.append(dict(pid=int(parts[0]), process_name=parts[1], used_memory_mib=mib, status="ok"))
    return rows


def container_record(data):
    config = data["Config"] or {}
    labels = {k: v for k, v in (config.get("Labels") or {}).items() if k in OWNED_LABELS and v is not None}
    name = data["Name"].lstrip("/")
    image = config.get("Image") or ""
    cmd = config.get("Cmd") or []
    command_text = " ".join(str(arg) for arg in cmd).lower()
    managed = str(labels.get("io.pulsar.gb10.managed", "")).lower() == "true"
    if not (managed or "vllm" in name.lower() or "vllm" in image.lower() or "vllm" in command_text
            or "--served-model-name" in command_text or "--model" in cmd):
        return None
    state = data["State"]
    running = state["Running"]
    if not isinstance(running, bool):
        raise ValueError("invalid Docker running state")
    return dict(id=data["Id"], name=name, image=image, cmd=cmd if isinstance(cmd, list) else [], labels=labels,
                running=running, status=state.get("Status") or ("running" if running else "exited"))


def host_pids(docker, container_id):
    result = output(docker, "top", container_id, "-eo", "pid")
    if result is None:
        return None
    lines = result.split()
    if not lines or lines[0] != "PID" or not all(pid.isascii() and pid.isdigit() for pid in lines[1:]):
        return None
    return [int(pid) for pid in lines[1:]]


def collect(docker="docker", smi="nvidia-smi", meminfo="/proc/meminfo", *, local=False):
    memory = memory_gib(meminfo)
    sample = dict(mem_available_gib=memory.get("MemAvailable:"), mem_total_gib=memory.get("MemTotal:"),
                  gpu_processes=[], containers=[], docker_status="unavailable")
    docker_available = output(docker, "info") is not None
    if local and not docker_available:
        return sample
    sample["gpu_processes"] = gpu_processes(smi)
    if not docker_available:
        return sample
    sample["docker_status"] = "incomplete"
    ids = output(docker, "ps", "-aq")
    if ids is None:
        return sample
    for container_id in ids.split():
        raw = output(docker, "inspect", container_id)
        if raw is None:
            return sample
        try:
            inspected = json.loads(raw)
            record = container_record(inspected[0])
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return sample
        if record is not None:
            record["host_pids"] = host_pids(docker, container_id) if record["running"] else []
            sample["containers"].append(record)
    sample["docker_status"] = "ok"
    return sample


def read_sample(path):
    """Accept a complete node response before giving any of its rows to the classifier."""
    with path.open(encoding="utf-8") as source:
        sample = json.load(source)
    if not isinstance(sample, dict) or sample.get("docker_status") not in ("ok", "unavailable", "incomplete"):
        raise ValueError("missing Docker status")
    for key in ("mem_available_gib", "mem_total_gib"):
        value = sample[key]
        if value is not None and (type(value) not in (float, int) or not math.isfinite(value) or value < 0):
            raise ValueError("invalid memory reading")
    containers, processes = sample["containers"], sample["gpu_processes"]
    if not isinstance(containers, list) or not isinstance(processes, list):
        raise ValueError("missing node inventory rows")
    for row in containers:
        if not isinstance(row, dict) or not all(isinstance(row[k], str) for k in ("id", "name", "image", "status")):
            raise ValueError("invalid container identity")
        if not isinstance(row["running"], bool) or not isinstance(row["labels"], dict):
            raise ValueError("invalid container state")
        if not all(isinstance(v, str) for v in row["labels"].values()):
            raise ValueError("invalid container labels")
        if not isinstance(row["cmd"], list) or not all(isinstance(arg, str) for arg in row["cmd"]):
            raise ValueError("invalid container command")
        pids = row["host_pids"]
        if pids is None and row["running"]:
            continue
        if not isinstance(pids, list) or not all(type(pid) is int for pid in pids):
            raise ValueError("invalid container processes")
    for row in processes:
        if (not isinstance(row, dict) or type(row["pid"]) is not int or not isinstance(row["process_name"], str)
                or row["status"] != "ok" or (row["used_memory_mib"] is not None and type(row["used_memory_mib"]) is not int)):
            raise ValueError("invalid GPU process")
    return sample


if __name__ == "__main__":
    local = sys.argv[1:2] == ["--local"]
    sample = collect(*(sys.argv[2:] if local else sys.argv[1:]), local=local)
    if local and sample["docker_status"] != "ok":
        sys.exit("Docker is unavailable on this node — inventory is incomplete; no lifecycle action is safe"
                 if sample["docker_status"] == "unavailable" else "container inventory failed on this node")
    json.dump(sample, sys.stdout)
