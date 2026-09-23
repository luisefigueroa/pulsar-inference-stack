"""Container PID 1: lease, time and GB10 memory guards before GPU execution.

Independent of the host transport process: a disconnected/killed controller
cannot keep a workload alive by leaving Docker's daemon running.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from diagnostics.schema import read_regular, validate_context
from scripts.resource_sample import read_meminfo, read_memory_events

from serving_guard.runtime import LEASE_SECONDS, SWAP_SLACK, guard_reason, stop_child


def verify_files(context, root):
    for name, expected in context["guard_files"].items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid guard path")
        if hashlib.sha256(read_regular(root / path)).hexdigest() != expected:
            raise ValueError("guard bytes changed")
    for name, expected in context["request"]["files"].items():
        if (
            hashlib.sha256(read_regular(root / "payload" / name)).hexdigest()
            != expected
        ):
            raise ValueError("payload bytes changed")


def run(
    context,
    root=Path("/diagnostic"),
    *,
    cgroup=Path("/sys/fs/cgroup"),
    meminfo=Path("/diagnostic-host-meminfo"),
    worker=None,
    lease=LEASE_SECONDS,
    result_file=Path("/tmp/pulsar-diagnostic-result.json"),
):
    validate_context(context)
    verify_files(context, root)
    if result_file.exists() or result_file.is_symlink():
        raise ValueError("fresh payload result path required")
    limits = context["request"]["limits"]
    if int((cgroup / "memory.max").read_text()) != limits["memory_bytes"]:
        raise ValueError("container memory limit differs")
    if int((cgroup / "memory.swap.max").read_text()) != 0:
        raise ValueError("container swap must be disabled")
    before = read_meminfo(meminfo)
    if (
        before is None
        or before["mem_available_bytes"]
        < limits["min_host_available_bytes"] + limits["memory_bytes"]
    ):
        raise ValueError("insufficient host memory before release")
    interrupted = []

    def cancel(signum, _frame):
        interrupted.append(signum)

    previous = {
        sig: signal.signal(sig, cancel)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    }
    child = None
    started = last_beat = time.monotonic()
    peak = 0
    minimum = before["mem_available_bytes"]
    tail = b""
    report = {
        "schema_version": 1,
        "kind": "pulsar-diagnostic-rank",
        "successful": False,
        "request_id": context["request_id"],
        "run_id": context["run_id"],
        "rank": context["rank"],
        "image_id": context["request"]["image_id"],
    }
    try:
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0.25)
            now = time.monotonic()
            # A delayed renewal cannot revive a lease that already expired.
            if now - last_beat >= lease:
                raise RuntimeError("controller lease expired")
            release = False
            if ready:
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    raise RuntimeError("controller disconnected")
                if any(c not in b".G" for c in data):
                    raise RuntimeError("invalid guard control message")
                last_beat = now
                release = b"G" in data and child is None
            host = read_meminfo(meminfo)
            events = read_memory_events(cgroup / "memory.events")
            if host is None or events is None:
                raise RuntimeError("resource guard sample unavailable")
            current = int((cgroup / "memory.current").read_text())
            peak = max(peak, int((cgroup / "memory.peak").read_text()))
            minimum = min(minimum, host["mem_available_bytes"])
            now = time.monotonic()
            reason = guard_reason(
                {**host, **events, "memory_current_bytes": current},
                before,
                limits,
                now - started,
                now - last_beat,
            )
            if interrupted or reason or now - last_beat >= lease:
                raise RuntimeError(reason or "interrupted or controller lease expired")
            if release:
                # Both configuration readback and a current resource/time/lease
                # sample must pass before a process can initialize CUDA.
                command = worker or [
                    sys.executable,
                    "-m",
                    "diagnostics.worker",
                    str(root / "context.json"),
                ]
                child = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                os.set_blocking(child.stdout.fileno(), False)
            if child is None:
                continue
            chunk = (
                os.read(child.stdout.fileno(), 65536)
                if select.select([child.stdout], [], [], 0)[0]
                else b""
            )
            tail = (tail + chunk)[-65536:]
            status = os.waitid(
                os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
            )
            if status is not None:
                if status.si_code != os.CLD_EXITED or status.si_status != 0:
                    raise RuntimeError(f"payload exited with status {status.si_status}")
                value = json.loads(read_regular(result_file, 65536))
                if not isinstance(value, dict) or value.get("successful") is not True:
                    raise RuntimeError("payload did not report successful criteria")
                report.update(successful=True, payload_result=value)
                break
    except (OSError, ValueError, RuntimeError) as exc:
        report["error"] = str(exc)
    finally:
        stop_child(child)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    report.update(
        elapsed_seconds=time.monotonic() - started,
        cgroup_peak_bytes=peak,
        min_host_available_bytes=minimum,
        log_tail=tail.decode(errors="replace"),
    )
    return report


def main():
    context = json.loads(Path("/diagnostic/context.json").read_text())
    report = run(context)
    print(json.dumps(report), flush=True)
    return 0 if report["successful"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
