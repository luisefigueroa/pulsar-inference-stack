"""Container PID 1 for a guarded serving session; no framework imports."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from scripts.resource_sample import read_meminfo, read_memory_events

LEASE_SECONDS = 30
SWAP_SLACK = 64 * 1024**2
MAX_LOG_BYTES = 32 * 1024**2


def swap_allowance(limits):
    value = limits.get("max_host_swap_growth_bytes", SWAP_SLACK)
    if type(value) is not int or not 0 <= value <= 256 * 1024**2:
        raise ValueError("invalid host swap growth allowance")
    return value


def guard_reason(sample, before, limits, elapsed, lease_age):
    """Common diagnostic/serving conditions; none means a usable sample."""
    allowance = swap_allowance(limits)
    floor = max(limits["min_host_available_bytes"],
                before["mem_available_bytes"] - limits["memory_bytes"])
    if lease_age >= LEASE_SECONDS:
        return "controller lease expired"
    if elapsed >= limits["timeout_seconds"]:
        return "time limit"
    if sample["mem_available_bytes"] < floor:
        return "host available memory"
    if sample["swap_used_bytes"] > before["swap_used_bytes"] + allowance:
        return "host swap growth"
    if sample["oom"] or sample["oom_kill"] or sample["memory_current_bytes"] > limits["memory_bytes"]:
        return "cgroup memory"
    return None


def stop_child(child):
    if child is None:
        return
    # Keep the leader unreaped while addressing its group, preventing PID reuse.
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            break
        if signum == signal.SIGTERM:
            time.sleep(.2)
    child.wait(timeout=2)
    if child.stdout:
        child.stdout.close()


def run(context, command, *, cgroup=Path("/sys/fs/cgroup"),
        meminfo=Path("/pulsar-guard-host-meminfo"), control=None,
        lease=LEASE_SECONDS):
    limits = context["limits"]
    allowance = swap_allowance(limits)
    if int((cgroup / "memory.max").read_text()) != limits["memory_bytes"]:
        raise ValueError("container memory limit differs")
    if int((cgroup / "memory.swap.max").read_text()) != 0:
        raise ValueError("container swap must be disabled")
    before = read_meminfo(meminfo)
    if before is None or before["mem_available_bytes"] < max(limits["min_host_available_bytes"], limits["memory_bytes"]):
        raise ValueError("insufficient host memory before release")
    initial_events = read_memory_events(cgroup / "memory.events")
    if initial_events is None:
        raise ValueError("resource guard sample unavailable")
    baseline = {**before, **initial_events,
                "memory_current_bytes": int((cgroup / "memory.current").read_text()),
                "memory_peak_bytes": int((cgroup / "memory.peak").read_text()),
                "elapsed_seconds": 0.0}
    fd = sys.stdin.fileno() if control is None else control
    interrupted = []

    def cancel(signum, _frame):
        interrupted.append(signum)

    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    child = None
    # Logging cannot hold the lease/memory loop hostage when an output reader
    # disappears. Backpressure is a stop condition, never a blocking write.
    output_fd = sys.stdout.fileno()
    os.set_blocking(output_fd, False)
    started = last_beat = time.monotonic()
    ready = False
    peak = total_log = 0
    minimum = before["mem_available_bytes"]
    tail = pending = b""
    report = {"schema_version": 1, "kind": "pulsar-serving-guard-rank",
              "run_id": context["run_id"], "spec_id": context["spec_id"],
              "rank": context["rank"], "stopped": False, "ready": False,
              "baseline_sample": baseline, "last_sample": None, "trigger_sample": None,
              "host_swap_growth_limit_bytes": allowance,
              "max_observed_host_swap_growth_bytes": 0,
              "effective_host_available_floor_bytes": max(limits["min_host_available_bytes"],
                  before["mem_available_bytes"] - limits["memory_bytes"])}
    try:
        while True:
            readable, _, _ = select.select([fd], [], [], .25)
            now = time.monotonic()
            if now - last_beat >= lease:
                raise RuntimeError("controller lease expired")
            release = stop = False
            if readable:
                data = os.read(fd, 4096)
                if not data:
                    raise RuntimeError("controller disconnected")
                if any(c not in b".GHQ" for c in data):
                    raise RuntimeError("invalid guard control message")
                last_beat = now
                release = b"G" in data and child is None
                stop = b"Q" in data
                if b"H" in data:
                    if child is None:
                        raise RuntimeError("readiness before release")
                    ready = True
            host = read_meminfo(meminfo)
            events = read_memory_events(cgroup / "memory.events")
            if host is None or events is None:
                raise RuntimeError("resource guard sample unavailable")
            current = int((cgroup / "memory.current").read_text())
            peak = max(peak, int((cgroup / "memory.peak").read_text()))
            minimum = min(minimum, host["mem_available_bytes"])
            now = time.monotonic()
            sample = {**host, **events, "memory_current_bytes": current,
                      "memory_peak_bytes": peak, "elapsed_seconds": now - started}
            report["last_sample"] = sample
            report["max_observed_host_swap_growth_bytes"] = max(
                report["max_observed_host_swap_growth_bytes"], host["swap_used_bytes"] - before["swap_used_bytes"])
            reason = guard_reason(sample,
                                  before, limits, now - started, now - last_beat)
            if context["rank"] == 0 and not ready and now - started >= limits["startup_timeout_seconds"]:
                reason = "startup time limit"
            if interrupted or reason or now - last_beat >= lease:
                if reason:
                    report["trigger_sample"] = dict(sample)
                raise RuntimeError(reason or "interrupted or controller lease expired")
            if stop:
                report["stopped"] = True
                break
            if release:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         start_new_session=True)
                os.set_blocking(child.stdout.fileno(), False)
            if child is None:
                continue
            if select.select([child.stdout], [], [], 0)[0]:
                chunk = os.read(child.stdout.fileno(), 65536)
                total_log += len(chunk)
                if total_log > MAX_LOG_BYTES:
                    raise RuntimeError("model log limit")
                tail = (tail + chunk)[-65536:]
                # Keep docker logs useful during startup. The node driver drains
                # output to its owned file; lifetime output has an explicit cap.
                pending += chunk
                if len(pending) > 1024**2:
                    raise RuntimeError("model log backpressure")
            if pending:
                try:
                    written = os.write(output_fd, pending[:65536])
                except BlockingIOError:
                    written = 0
                pending = pending[written:]
            status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is not None:
                raise RuntimeError("model process exited")
    except (OSError, ValueError, RuntimeError) as exc:
        report["error"] = str(exc)
    finally:
        stop_child(child)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        # Never restore blocking mode here: even the final report must not
        # prevent PID 1 exiting and the container namespace being torn down.
    report.update(ready=ready, elapsed_seconds=time.monotonic() - started,
                  cgroup_peak_bytes=peak, min_host_available_bytes=minimum,
                  log_tail=tail.decode(errors="replace"),
                  unflushed_log_bytes=len(pending))
    return report


def emit_report(report, *, timeout=2):
    """Bounded final output after child cleanup, even if the reader is stalled."""
    data = ("\n" + json.dumps(report) + "\n").encode()
    fd = sys.stdout.fileno()
    os.set_blocking(fd, False)
    offset = 0
    deadline = time.monotonic() + timeout
    while offset < len(data) and time.monotonic() < deadline:
        if not select.select([], [fd], [], min(.1, max(0, deadline-time.monotonic())))[1]:
            continue
        try:
            offset += os.write(fd, data[offset:offset+16384])
        except BlockingIOError:
            continue
        except BrokenPipeError:
            return False
    return offset == len(data)


def main():
    context = json.loads(sys.argv[1])
    report = run(context, sys.argv[2:])
    emitted = emit_report(report)
    return 0 if report["stopped"] and emitted else 3


if __name__ == "__main__":
    raise SystemExit(main())
