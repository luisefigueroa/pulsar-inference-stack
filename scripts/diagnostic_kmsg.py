"""Read-only diagnostic observer with one selected record source and reducer.

The Bash owner launches and closes this process. Atomic attempt-local control
messages never execute commands. The explicit development journal path reads
Bash-owned captures; it never falls back to another source or spawns a process.
"""
from __future__ import annotations

import errno
import base64
import json
import os
import secrets
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from release_spec.diagnostic import load_plan, parse_kmsg_record, require_boot, PROFILE, complete_stopped_state
from release_spec.diagnostic_state import observation, latch, reduce_observation
from scripts.diagnostic_runtime import identity_problem
from scripts.resource_sample import read_cgroup
from scripts import diagnostic_journal as journal

KMSG_PATH = "/dev/kmsg"
BOOT_PATH = "/proc/sys/kernel/random/boot_id"
MEMINFO_PATH = "/proc/meminfo"
PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
STOP = False
DRAIN = False


def read_boot():
    return require_boot(Path(BOOT_PATH).read_text().strip().replace("-", ""), "boot_id")


def read_meminfo():
    values = {}
    for line in Path(MEMINFO_PATH).read_text().splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == "kB":
            values[fields[0].rstrip(":")] = int(fields[1]) * 1024
    available, total, free = (values[k] for k in ("MemAvailable", "SwapTotal", "SwapFree"))
    if min(available, total, free) < 0 or free > total:
        raise ValueError("invalid meminfo")
    return {"monotonic_ns": time.monotonic_ns(), "mem_available_bytes": available, "swap_used_bytes": total - free}


def process_start(pid, root=None):
    try:
        value = ((root or PROC_ROOT) / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
        return value if value.isdigit() and int(value) > 0 else None
    except (OSError, IndexError):
        return None


def open_kmsg():
    fd = os.open(KMSG_PATH, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        os.lseek(fd, 0, os.SEEK_END)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_one(fd):
    try:
        data = os.read(fd, 8192)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return "eagain", None
        if exc.errno == errno.EPIPE:
            return "epipe", None
        raise
    if not data:
        return "eof", None
    return "record", decode_record(data)


class KernelRecordError(ValueError):
    def __init__(self, raw, reason):
        super().__init__(reason)
        self.raw = raw


def decode_record(raw):
    try:
        return parse_kmsg_record(raw)
    except ValueError as exc:
        raise KernelRecordError(raw, str(exc)) from exc


def read_document(path, maximum=512 * 1024):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("invalid bounded observer document")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("observer document exhausted capture bound")
    return json.loads(data)


def atomic_status(path, value):
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > 128 * 1024:
        raise ValueError("observer status exceeded bound")
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as stream:
        stream.write(data)
    os.replace(tmp, path)


class BoundWorkload:
    def __init__(self, inspected, boot):
        self.fd = None
        self.pid = inspected["State"]["Pid"]
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("workload PID is unknown")
        self.start = process_start(self.pid)
        if self.start is None or read_boot() != boot:
            raise ValueError("workload PID start/boot is unknown")
        self.relative = self.cgroup_path()
        target = CGROUP_ROOT.joinpath(self.relative.lstrip("/"))
        if target.resolve() != target or not target.is_relative_to(CGROUP_ROOT):
            raise ValueError("unsafe workload cgroup path")
        self.fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(self.fd)
            self.identity = {"pid": self.pid, "starttime": self.start, "boot_id": boot,
                             "path": self.relative, "device": info.st_dev, "inode": info.st_ino}
            self.sample(running=True)
        except BaseException:
            self.close()
            raise

    def cgroup_path(self):
        lines = (PROC_ROOT / str(self.pid) / "cgroup").read_text().splitlines()
        groups = [s[3:] for s in lines if s.startswith("0::")]
        if len(groups) != 1 or not groups[0].startswith("/") or ".." in groups[0].split("/") or groups[0] == "/":
            raise ValueError("workload cgroup identity is missing")
        return groups[0]

    def sample(self, *, running):
        boot = read_boot()
        if boot != self.identity["boot_id"]:
            raise ValueError("workload boot changed")
        if running and (process_start(self.pid) != self.start or self.cgroup_path() != self.relative):
            raise ValueError("workload PID/start/cgroup changed before sample")
        self.verify_cgroup_directory()
        counters = read_cgroup(Path("/proc/self/fd") / str(self.fd))
        if not isinstance(counters, dict) or any(type(counters.get(k)) is not int for k in (
                "memory_current_bytes", "memory_peak_bytes", "memory_swap_current_bytes", "oom", "oom_kill")):
            raise ValueError("workload counters are unknown")
        if read_boot() != boot or (running and (process_start(self.pid) != self.start or self.cgroup_path() != self.relative)):
            raise ValueError("workload identity changed across sample")
        self.verify_cgroup_directory()
        return counters

    def verify_cgroup_directory(self):
        target = CGROUP_ROOT.joinpath(self.relative.lstrip("/"))
        current = target.stat(follow_symlinks=False)
        held = os.fstat(self.fd)
        if target.resolve() != target or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise ValueError("workload cgroup directory was replaced")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Observer:
    def __init__(self, attempt):
        self.attempt = attempt
        self.plan = load_plan(attempt / "plan.json")
        self.claim = read_document(attempt / "claim.json")
        self.settings = self.plan["definition"]["observer"]
        self.state = observation()
        self.state.update(plan_id=self.plan["plan_id"], attempt_nonce=self.claim["attempt_nonce"],
                          observer_instance=secrets.token_hex(16), boot_id=read_boot())
        self.fd = None
        self.journal = None
        self.workload = None
        self.last_sample = 0
        self.start_at = None
        self.drain_at = None
        self.ready_deadline = time.monotonic_ns() + 2 * 10**9
        self.raw_bytes = 0
        self.events = (attempt / "observer.jsonl").open("xb")
        self.created = time.monotonic_ns()

    def event(self, kind, **fields):
        event = {"kind": kind, "boot_id": read_boot(), "observer_instance": self.state["observer_instance"],
                 "monotonic_ns": time.monotonic_ns(), "plan_id": self.state["plan_id"],
                 "attempt_nonce": self.state["attempt_nonce"], "container_id": self.state["container_id"],
                 "image_id": self.state["image_id"], "phase": self.state["phase"],
                 "event_sequence": self.state["event_sequence"] + 1, **fields}
        reduce_observation(self.state, event, self.settings)
        data = (json.dumps(event, sort_keys=True) + "\n").encode()
        if self.raw_bytes + len(data) <= 8 * 1024**2:
            self.events.write(data)
            self.events.flush()
            self.raw_bytes += len(data)
        else:
            latch(self.state, "coverage", "observer raw capture exhausted")

    def sample(self, *, force=False):
        now = time.monotonic_ns()
        if not force and now - self.last_sample < self.settings["sample_interval_seconds"] * 10**9:
            return
        self.event("memory", **read_meminfo())
        if self.workload and not self.state["workload_released"]:
            self.event("cgroup", identity=self.workload.identity, workload=self.workload.sample(running=True))
        elif self.start_at and not self.workload and now - self.start_at > 10**9:
            latch(self.state, "coverage", "no bound workload counters within start window")
        self.last_sample = now
        if read_boot() != self.state["boot_id"]:
            latch(self.state, "coverage", "boot changed across memory sample")
        # Only the explicit attempt is counted; input and capture are bounded.
        size = sum(p.stat().st_size for p in self.attempt.rglob("*") if p.is_file() and not p.is_symlink())
        if size > PROFILE["capture_budget_bytes"] - 1024**2:
            latch(self.state, "coverage", "attempt capture budget exhausted")

    def publish(self):
        self.state["snapshot_sequence"] += 1
        self.state["published_monotonic_ns"] = time.monotonic_ns()
        atomic_status(self.attempt / "observer-status.json", self.state)

    def control(self):
        path = self.attempt / "observer-control.json"
        if not path.exists():
            return
        command = read_document(path)
        seq = command["sequence"]
        if type(seq) is not int or seq < self.state["control_sequence"]:
            raise ValueError("observer control sequence regressed")
        if seq == self.state["control_sequence"]:
            return
        if seq != self.state["control_sequence"] + 1:
            raise ValueError("observer control sequence skipped")
        if any(command.get(k) != self.claim[k] for k in ("plan_id", "attempt_nonce", "boot_id")):
            raise ValueError("foreign observer control")
        kind = command["command"]
        if kind in ("created", "running", "stopped"):
            inspected = command["inspect"]
            claim = dict(self.claim, container_id=inspected.get("Id"))
            problem = identity_problem(inspected, self.plan, claim)
            if problem or (self.state["container_id"] and self.state["container_id"] != inspected.get("Id")):
                raise ValueError(problem or "observer container changed")
            self.state.update(container_id=inspected["Id"], image_id=inspected["Image"])
            if kind == "running":
                if inspected["State"].get("Running") is not True:
                    raise ValueError("running control lacks a running process")
                if self.workload is None:
                    self.workload = BoundWorkload(inspected, self.state["boot_id"])
                elif inspected["State"]["Pid"] != self.workload.pid:
                    raise ValueError("workload PID changed")
                self.sample(force=True)
            if kind == "stopped":
                problem = complete_stopped_state(inspected["State"], start_consumed=self.start_at is not None)
                if problem:
                    raise ValueError(problem)
                if self.start_at is not None:
                    if not self.workload:
                        latch(self.state, "coverage", "workload exited without bound resource coverage")
                    else:
                        self.event("cgroup", identity=self.workload.identity, workload=self.workload.sample(running=False))
                        self.state["workload_released"] = True
        elif kind == "starting":
            if self.start_at is not None:
                raise ValueError("start cannot be consumed twice")
            self.start_at = command["start_monotonic_ns"]
        elif kind == "finish":
            if self.drain_at is not None:
                raise ValueError("terminal request repeated")
            self.drain_at = time.monotonic_ns()
            if command["cleanup_monotonic_ns"] > self.drain_at:
                raise ValueError("invalid cleanup boundary")
            self.state["drain_request_ns"] = self.drain_at
            self.state["cleanup_monotonic_ns"] = command["cleanup_monotonic_ns"]
            self.state["phase"] = "draining"
        else:
            raise ValueError("unknown observer control")
        self.state["control_sequence"] = seq
        if kind != "finish":
            self.state["phase"] = kind

    def run(self):
        code = 1
        try:
            if self.state["boot_id"] != self.claim["boot_id"]:
                raise ValueError("observer initial boot mismatch")
            journal_mode = self.settings["backend"] == "journal"
            self.state["source_backend"] = self.settings["backend"]
            if journal_mode:
                anchor = read_document(self.attempt / "journal-anchor.json")
                self.journal = journal.Capture(self.attempt, self.state["boot_id"], anchor["record"])
                self.state.update(tail_before_ns=anchor["begin_ns"], tail_after_ns=anchor["end_ns"],
                                  journal_anchor_cursor=anchor["record"]["cursor"],
                                  journal_anchor_realtime_us=anchor["record"]["realtime_us"],
                                  journal_last_cursor=anchor["record"]["cursor"],
                                  journal_delivery_lossless=False)
            else:
                self.state["tail_before_ns"] = time.monotonic_ns()
                self.fd = open_kmsg()
                self.state["tail_after_ns"] = time.monotonic_ns()
            if read_boot() != self.state["boot_id"]:
                raise ValueError("boot changed while tailing")
            while True:
                if process_start(self.claim["owner_pid"], Path("/proc")) != self.claim["owner_starttime"]:
                    raise ValueError("operation owner disappeared; observation cannot be recovered")
                self.control()
                self.sample()
                before = time.monotonic_ns()
                final = None
                if journal_mode:
                    final_path = self.attempt / "journal-final-ready.json"
                    if final_path.exists():
                        final = read_document(final_path)
                        if (final.get("query_rc") != 0 or final.get("waited") is not True or
                                final.get("forced") is not False or final.get("identity_verified") is not True or final.get("exit_code") not in (0, 143) or
                                final.get("boot_id") != self.state["boot_id"] or self.drain_at is None or
                                final["query_begin_ns"] < self.state["cleanup_monotonic_ns"] or
                                (self.attempt / "journal-final.source-stderr").stat().st_size):
                            raise ValueError("journal final query or owned-client closure failed")
                    elif not (self.attempt / "journal-closing.json").exists():
                        client = read_document(self.attempt / "journal-client.json")
                        if process_start(client["pid"], Path("/proc")) != client["starttime"]:
                            raise ValueError("journal follow client exited before owned closure")
                    status, record = self.journal.next(final)
                else:
                    status, record = read_one(self.fd)
                after = time.monotonic_ns()
                if status == "record":
                    self.event("journal_kernel" if journal_mode else "kernel", record=record, received_monotonic_ns=after)
                    if journal_mode and after - record["timestamp_us"] * 1000 > self.settings["max_observation_age_seconds"] * 10**9:
                        latch(self.state, "coverage", "journal delivered-record read lag exceeded freshness bound")
                elif journal_mode and status == "anchor":
                    if not self.state["ready"]:
                        self.sample(force=True)
                        self.state.update(ready=True, phase="ready", ready_record_before_ns=before, ready_record_after_ns=after)
                elif journal_mode and status == "complete":
                    self.sample(force=True)
                    self.state.update(drain_before_ns=self.journal.final_boundary, drain_after_ns=after,
                                      drain_complete=True, journal_query_complete=True,
                                      journal_client_closed=True, journal_final_query=final,
                                      journal_final_cursors=len(self.journal.final_cursors))
                    code = 0
                    break
                elif journal_mode and status in ("idle", "duplicate"):
                    pass
                elif status != "eagain":
                    raise ValueError("kmsg overwrite (EPIPE)" if status == "epipe" else "kmsg EOF is not EAGAIN")
                elif self.drain_at is not None:
                    # This read was performed after receiving the cleanup request.
                    self.sample(force=True)
                    if read_boot() != self.state["boot_id"]:
                        raise ValueError("boot changed across terminal EAGAIN")
                    self.state.update(drain_before_ns=before, drain_after_ns=after, drain_complete=True,
                                      empty_interval=self.state["last_sequence"] is None)
                    code = 0
                    break
                elif not self.state["ready"]:
                    self.sample(force=True)
                    self.state.update(ready=True, phase="ready", ready_eagain_before_ns=before, ready_eagain_after_ns=after)
                now = time.monotonic_ns()
                if not self.state["ready"] and now > self.ready_deadline:
                    raise ValueError("ready drain exceeded bound")
                if self.drain_at and now - self.drain_at > (5 if journal_mode else 2) * 10**9:
                    raise ValueError("terminal drain exceeded bound")
                if STOP:
                    raise ValueError("observer terminated without terminal handshake")
                if now - self.created > self.settings["operation_seconds"] * 10**9:
                    raise ValueError("observer operation deadline exceeded")
                self.publish()
                if status in ("eagain", "idle"):
                    time.sleep(0.02)
        except KernelRecordError as exc:
            latch(self.state, "coverage", str(exc))
            atomic_status(self.attempt / "invalid-kernel-record.json", {
                "bytes": len(exc.raw), "prefix_base64": base64.b64encode(exc.raw[:8192]).decode(),
                "truncated": len(exc.raw) > 8192})
        except Exception as exc:
            latch(self.state, "coverage", str(exc))
        finally:
            if self.workload:
                self.workload.close()
            if self.fd is not None:
                os.close(self.fd)
                self.state["descriptor_closed"] = True
            if self.journal is not None:
                self.journal.close()
                self.state["descriptor_closed"] = True
            self.events.close()
            self.state["phase"] = "finished" if code == 0 else "failed"
            self.state["producer_exit_code"] = code
            self.publish()
            atomic_status(self.attempt / "observer-terminal.json", self.state)
        return code


def _handle(signum, frame):
    global STOP
    STOP = True


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "--attempt-dir":
        raise ValueError("observer requires an owned attempt directory")
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    return Observer(Path(args[1])).run()


if __name__ == "__main__":
    raise SystemExit(main())
