#!/usr/bin/env python3
"""Read-only node memory and owned-container cgroup samples for Stack."""
from __future__ import annotations
import argparse
import json
import math
import os
import re
import signal
import subprocess
import time
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
from typing import Any
SAMPLE_SCHEMA_VERSION=1
SAMPLE_KIND='pulsar-model-serving-resource-sample'
SAFE_RANK_RE=re.compile(r'^(?:single|0|[1-9][0-9]*)$')
SESSION_TOKEN_RE=re.compile(r'^[0-9a-f]{32}$')
STOP=False
EXPECTED_SPEC_ID=None
EXPECTED_NODE_ID=None
EXPECTED_TOPOLOGY_ID=None
class ResourceMonitorError(ValueError):
    """Resource monitoring input or state is unsafe or invalid."""

def fail(message: str) -> None:
    raise ResourceMonitorError(message)

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )

def safe_rank(value: str) -> str:
    if not isinstance(value, str) or SAFE_RANK_RE.fullmatch(value) is None:
        fail("rank label must be 'single' or a non-negative integer")
    return value

def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None

def read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int] | None:
    text = _read_text(path)
    if text is None:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].endswith(":"):
            continue
        try:
            values[parts[0][:-1]] = int(parts[1]) * 1024
        except ValueError:
            continue
    required = {"MemAvailable", "SwapTotal", "SwapFree"}
    if not required.issubset(values):
        return None
    return {
        "mem_available_bytes": values["MemAvailable"],
        "swap_used_bytes": max(0, values["SwapTotal"] - values["SwapFree"]),
    }

def read_pressure_some_total(
    path: Path = Path("/proc/pressure/memory"),
) -> int | None:
    text = _read_text(path)
    if text is None:
        return None
    for line in text.splitlines():
        if not line.startswith("some "):
            continue
        for item in line.split()[1:]:
            if item.startswith("total="):
                try:
                    return int(item.split("=", 1)[1])
                except ValueError:
                    return None
    return None

def _read_nonnegative_int(path: Path) -> int | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        value = int(text.strip())
    except ValueError:
        return None
    return value if value >= 0 else None

def read_memory_events(path: Path) -> dict[str, int] | None:
    text = _read_text(path)
    if text is None:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            values[parts[0]] = int(parts[1])
        except ValueError:
            continue
    if "oom" not in values or "oom_kill" not in values:
        return None
    return {"oom": values["oom"], "oom_kill": values["oom_kill"]}

def cgroup_for_container(container_name: str, *, proc_root=Path('/proc'), cgroup_root=Path('/sys/fs/cgroup')) -> Path | None:
    try:
        result = subprocess.run(
            [
                os.environ.get("PULSAR_DOCKER", "docker"),
                "inspect",
                "--format",
                "{{json .}}",
                container_name,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        document=json.loads(result.stdout)
        labels=document.get('Config',{}).get('Labels') or {}
        if (not EXPECTED_SPEC_ID or labels.get('io.pulsar.gb10.managed')!='true'
                or labels.get('io.pulsar.gb10.spec-id')!=EXPECTED_SPEC_ID
                or labels.get('io.pulsar.gb10.node-id')!=EXPECTED_NODE_ID
                or labels.get('io.pulsar.gb10.topology')!=EXPECTED_TOPOLOGY_ID):
            return None
        if document.get('State',{}).get('Running') is not True:
            return None
        pid = int(document.get('State',{}).get('Pid') or 0)
    except (ValueError,TypeError,AttributeError):
        return None
    if pid <= 0:
        return None
    cgroup_text = _read_text(proc_root / str(pid) / "cgroup")
    if cgroup_text is None:
        return None
    relative = None
    for line in cgroup_text.splitlines():
        if line.startswith("0::"):
            relative = line[3:]
            break
    if not relative or not relative.startswith("/"):
        return None
    parts = PurePosixPath(relative).parts
    if ".." in parts:
        return None
    root = cgroup_root
    target = root.joinpath(*parts[1:])
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved

def read_cgroup(cgroup: Path | None) -> dict[str, int | None] | None:
    if cgroup is None:
        return None
    current = _read_nonnegative_int(cgroup / "memory.current")
    peak = _read_nonnegative_int(cgroup / "memory.peak")
    if current is None or peak is None:
        return None
    events = read_memory_events(cgroup / "memory.events")
    return {
        "memory_current_bytes": current,
        "memory_peak_bytes": peak,
        "memory_swap_current_bytes": _read_nonnegative_int(
            cgroup / "memory.swap.current"
        ),
        "oom": events["oom"] if events is not None else None,
        "oom_kill": events["oom_kill"] if events is not None else None,
    }

def make_sample(
    *,
    rank: str,
    cgroup: Path | None,
    meminfo_path: Path = Path("/proc/meminfo"),
    pressure_path: Path = Path("/proc/pressure/memory"),
) -> dict[str, Any]:
    return {
        "schema_version": SAMPLE_SCHEMA_VERSION,
        "kind": SAMPLE_KIND,
        "rank": safe_rank(rank),
        "sampled_at": utc_now(),
        "monotonic_ns": time.monotonic_ns(),
        "node": read_meminfo(meminfo_path),
        "node_memory_pressure_some_total_us": read_pressure_some_total(
            pressure_path
        ),
        "workload": read_cgroup(cgroup),
    }

def _handle_stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True

def collect(
    *, rank: str, container_name: str, interval: float, session_token: str
) -> int:
    safe_rank(rank)
    if SESSION_TOKEN_RE.fullmatch(session_token) is None:
        fail("session token is invalid")
    if not container_name or any(ch in container_name for ch in "\r\n\0"):
        fail("container name is invalid")
    if not math.isfinite(interval) or interval < 0.1 or interval > 60:
        fail("sample interval must be between 0.1 and 60 seconds")
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    cgroup: Path | None = None
    while not STOP:
        if cgroup is None or not cgroup.exists():
            cgroup = cgroup_for_container(container_name)
        sample = make_sample(rank=rank, cgroup=cgroup)
        print(json.dumps(sample, sort_keys=True, separators=(",", ":")), flush=True)
        deadline = time.monotonic() + interval
        while not STOP and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return 0


def main():
    global EXPECTED_SPEC_ID, EXPECTED_NODE_ID, EXPECTED_TOPOLOGY_ID
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rank-label',required=True)
    parser.add_argument('--container-name',required=True)
    parser.add_argument('--spec-id',required=True)
    parser.add_argument('--node-id',required=True)
    parser.add_argument('--topology-id',required=True)
    parser.add_argument('--interval',type=float,default=0.25)
    parser.add_argument('--session-token',required=True)
    args=parser.parse_args()
    EXPECTED_SPEC_ID=args.spec_id;EXPECTED_NODE_ID=args.node_id;EXPECTED_TOPOLOGY_ID=args.topology_id
    if not re.fullmatch(r'[0-9a-f]{64}',EXPECTED_SPEC_ID) or not re.fullmatch(r'[0-9a-f]{64}',EXPECTED_TOPOLOGY_ID):
        parser.error('spec and topology IDs must be complete digests')
    return collect(rank=args.rank_label,container_name=args.container_name,interval=args.interval,session_token=args.session_token)

if __name__=='__main__': raise SystemExit(main())
