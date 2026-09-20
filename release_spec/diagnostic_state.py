"""Capability-specific lifecycle and observation decisions. No process operations.

Lifecycle facts advance once; failures latch. Only final_result chooses an outcome.
"""
from __future__ import annotations

import base64
import copy
import re
import time
from release_spec.diagnostic import PROFILE, counted_kernel_failure, complete_stopped_state

PHASES = ("claimed", "admitted", "observer_ready", "create_claimed", "created", "controls_verified",
          "start_claimed", "running_or_uncertain", "stopping_or_exited", "removed_or_unknown",
          "observer_closed_or_unknown", "terminal")


def lifecycle():
    return {"schema_version": 2, "phase": "claimed", "failures": [], "container_id": None,
            "create_consumed": False, "start_consumed": False, "observer_instance": None,
            "snapshot_sequence": 0, "control_sequence": 0, "observer_phase": "ready", "cleanup": {}, "observer_closure": {}}


def fail(state, reason):
    if reason not in state["failures"] and len(state["failures"]) < 32:
        state["failures"].append(str(reason)[:300])


def advance(state, phase):
    current = PHASES.index(state["phase"])
    target = PHASES.index(phase)
    if target < current or (target > current + 1 and target < PHASES.index("stopping_or_exited")):
        raise ValueError("invalid diagnostic transition: " + state["phase"] + " -> " + phase)
    state["phase"] = phase


def observation():
    return {"schema_version": 2, "kind": "pulsar-diagnostic-observation", "phase": "opening",
            "snapshot_sequence": 0, "event_sequence": 0, "control_sequence": 0,
            "plan_id": None, "attempt_nonce": None, "boot_id": None, "observer_instance": None,
            "container_id": None, "image_id": None, "kernel_records": 0, "last_sequence": None,
            "samples": 0, "cgroup_samples": 0, "min_mem_available_bytes": None,
            "max_swap_used_bytes": None, "baseline_swap_bytes": None,
            "first_safety_failure": None, "first_coverage_failure": None,
            "last_sample_monotonic_ns": None, "last_cgroup_monotonic_ns": None,
            "workload": None, "workload_identity": None, "workload_released": False,
            "ready": False, "drain_complete": False, "descriptor_closed": False}


def latch(state, category, reason):
    key = "first_" + category + "_failure"
    if state[key] is None:
        state[key] = str(reason)[:300]


def reduce_observation(state, event, settings):
    """Every event carries observed boot/instance and increasing receive sequence."""
    expected_sequence = state["event_sequence"] + 1
    state["event_sequence"] = expected_sequence
    if type(event.get("event_sequence")) is not int or event["event_sequence"] != expected_sequence:
        latch(state, "coverage", "observer event sequence changed")
        return
    for key in ("boot_id", "observer_instance", "plan_id", "attempt_nonce", "container_id", "image_id", "phase"):
        if key not in event or event[key] != state.get(key):
            latch(state, "coverage", "observer event identity or phase changed: " + key)
            return
    if not isinstance(event.get("kind"), str):
        latch(state, "coverage", "observer event kind is missing")
        return
    kind = event.get("kind")
    now = event.get("monotonic_ns")
    if type(now) is not int or now <= 0:
        latch(state, "coverage", "missing observation time")
        return
    if kind in ("kernel", "journal_kernel"):
        record = event["record"]
        seq = record.get("sequence")
        if kind == "kernel":
            if state["last_sequence"] is not None and seq != state["last_sequence"] + 1:
                latch(state, "coverage", "kernel sequence gap or regression")
            state["last_sequence"] = seq
        else:
            state["journal_last_cursor"] = record["cursor"]
            state["journal_last_realtime_us"] = record["realtime_us"]
        state["kernel_records"] += 1
        # The continuation ABI does not supply an unambiguous completion token.
        # Preserve records but never classify an incomplete fragment as clean.
        if kind == "kernel" and record["flags"] != "-":
            latch(state, "coverage", "ambiguous kernel continuation")
        if counted_kernel_failure(record["message"]):
            latch(state, "safety", "kernel driver-memory/Xid/host-OOM event (host-wide attribution unknown)")
            state["kernel_faults"] = state.get("kernel_faults", 0) + 1
            if state.get("first_kernel_fault") is None:
                state["first_kernel_fault"] = {"sequence": seq, "cursor": record.get("cursor"), "timestamp_us": record["timestamp_us"],
                    "received_monotonic_ns": now, "raw_sha256": record["raw_sha256"], "attribution": "unknown-host-wide"}
    elif kind == "memory":
        available, swap = event.get("mem_available_bytes"), event.get("swap_used_bytes")
        if type(available) is not int or type(swap) is not int or min(available, swap) < 0:
            latch(state, "coverage", "memory sample is unknown")
            return
        last = state["last_sample_monotonic_ns"]
        if last is not None and (now <= last or now - last > settings["max_observation_age_seconds"] * 10**9):
            latch(state, "coverage", "memory sampling gap or time regression")
        state["last_sample_monotonic_ns"] = now
        state["mem_available_bytes"] = available
        state["samples"] += 1
        state["min_mem_available_bytes"] = min(available, state["min_mem_available_bytes"] if state["min_mem_available_bytes"] is not None else available)
        state["max_swap_used_bytes"] = max(swap, state["max_swap_used_bytes"] or 0)
        if state["baseline_swap_bytes"] is None:
            state["baseline_swap_bytes"] = swap
        if available < settings["mem_available_floor_bytes"]:
            latch(state, "safety", "MemAvailable below floor")
        if state["max_swap_used_bytes"] - state["baseline_swap_bytes"] >= settings["swap_growth_limit_bytes"]:
            latch(state, "safety", "host swap growth reached limit")
    elif kind == "cgroup":
        identity, counters = event.get("identity"), event.get("workload")
        fields = ("memory_current_bytes", "memory_peak_bytes", "memory_swap_current_bytes", "oom", "oom_kill")
        if not isinstance(identity, dict) or any(identity.get(k) is None for k in ("pid", "starttime", "boot_id", "device", "inode", "path")):
            latch(state, "coverage", "workload identity is unknown")
            return
        if not isinstance(counters, dict) or any(type(counters.get(k)) is not int or counters[k] < 0 for k in fields):
            latch(state, "coverage", "workload counters are unknown")
            return
        if state["workload_identity"] is not None and state["workload_identity"] != identity:
            latch(state, "coverage", "workload PID/start/boot/cgroup changed")
        old = state["workload"]
        if old and any(counters[k] < old[k] for k in ("memory_peak_bytes", "oom", "oom_kill")):
            latch(state, "coverage", "workload counters regressed")
        last = state["last_cgroup_monotonic_ns"]
        if last is not None and (now <= last or now - last > settings["max_observation_age_seconds"] * 10**9):
            latch(state, "coverage", "workload sampling gap or time regression")
        state["workload_identity"], state["workload"] = copy.deepcopy(identity), dict(counters)
        state["last_cgroup_monotonic_ns"] = now
        state["cgroup_samples"] += 1
        if counters["oom"] or counters["oom_kill"]:
            latch(state, "safety", "owned workload cgroup OOM")
        if counters["memory_swap_current_bytes"] >= settings["swap_growth_limit_bytes"]:
            latch(state, "safety", "owned workload swap reached limit")
    else:
        latch(state, "coverage", "unknown observation event")


def snapshot_problem(snapshot, plan, claim, state, *, now_ns=None, terminal=False, include_safety=True):
    if not isinstance(snapshot, dict) or type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 2:
        return "observer snapshot is missing or malformed"
    journal_mode = plan["definition"]["observer"]["backend"] == "journal"
    if journal_mode and (snapshot.get("source_backend") != "journal" or
                         snapshot.get("journal_delivery_lossless") is not False or
                         not isinstance(snapshot.get("journal_anchor_cursor"), str) or
                         not isinstance(snapshot.get("journal_last_cursor"), str)):
        return "journal source or cursor window is incomplete"
    if type(snapshot.get("control_sequence")) is not int or snapshot["control_sequence"] != state["control_sequence"]:
        return "observer has not acknowledged the current phase"
    if not terminal and snapshot.get("phase") != state["observer_phase"]:
        return "observer phase differs from the acknowledged control"
    for key in ("plan_id", "attempt_nonce", "boot_id"):
        if snapshot.get(key) != claim.get(key):
            return "observer snapshot identity mismatch: " + key
    instance = snapshot.get("observer_instance")
    if not isinstance(instance, str) or not re.fullmatch(r"[0-9a-f]{32}", instance) or (state.get("observer_instance") and instance != state["observer_instance"]):
        return "observer instance changed or missing"
    seq = snapshot.get("snapshot_sequence")
    if type(seq) is not int or seq < 1 or seq < state.get("snapshot_sequence", 0):
        return "observer snapshot sequence regressed"
    previous = state.get("last_snapshot")
    if previous:
        if seq == previous["snapshot_sequence"] and snapshot != previous:
            return "observer changed a published snapshot without advancing sequence"
        for key in ("samples", "kernel_records", "cgroup_samples", "event_sequence", "last_sample_monotonic_ns"):
            if type(snapshot.get(key)) is not int or snapshot[key] < previous[key]:
                return "observer counters or sample time regressed"
        if snapshot.get("min_mem_available_bytes", 0) > previous["min_mem_available_bytes"] or snapshot.get("max_swap_used_bytes", 0) < previous["max_swap_used_bytes"]:
            return "observer discarded resource extrema"
        for key in ("first_safety_failure", "first_coverage_failure"):
            if previous.get(key) is not None and snapshot.get(key) != previous[key]:
                return "observer discarded an irreversible failure"
        delta = snapshot["kernel_records"] - previous["kernel_records"]
        if previous.get("last_sequence") is not None and (
            type(snapshot.get("last_sequence")) is not int or snapshot["last_sequence"] < previous["last_sequence"] + delta or
            (delta == 0 and snapshot["last_sequence"] != previous["last_sequence"])):
            return "observer discarded kernel sequence continuity"
    if snapshot.get("container_id") != state.get("container_id"):
        return "observer container binding mismatch"
    if state.get("container_id") and snapshot.get("image_id") != plan["definition"]["image_id"]:
        return "observer image binding mismatch"
    now = time.monotonic_ns() if now_ns is None else now_ns
    for key in ("samples", "kernel_records", "cgroup_samples", "event_sequence", "baseline_swap_bytes", "min_mem_available_bytes", "max_swap_used_bytes"):
        if type(snapshot.get(key)) is not int or snapshot[key] < 0:
            return "observer counters or resource values are incomplete"
    sequence = snapshot.get("last_sequence")
    if (journal_mode and sequence is not None) or (not journal_mode and ((snapshot["kernel_records"] == 0 and sequence is not None) or (snapshot["kernel_records"] > 0 and (type(sequence) is not int or sequence < 0)))):
        return "kernel sequence/empty interval is incomplete"
    stamp = snapshot.get("last_sample_monotonic_ns")
    maximum = plan["definition"]["observer"]["max_observation_age_seconds"] * 10**9
    if type(stamp) is not int or stamp > now or now - stamp > maximum:
        return "observer memory snapshot is stale or missing"
    ready_keys = ("ready_record_before_ns", "ready_record_after_ns") if journal_mode else ("ready_eagain_before_ns", "ready_eagain_after_ns")
    times = [snapshot.get(k) for k in ("tail_before_ns", "tail_after_ns", *ready_keys)]
    published = snapshot.get("published_monotonic_ns")
    if any(type(t) is not int or t <= 0 for t in times) or times != sorted(times) or times[-1] > stamp:
        return "observer ready boundary is incomplete"
    if type(published) is not int or not stamp <= published <= now:
        return "observer publication time is incomplete"
    if snapshot["cgroup_samples"]:
        identity, counters = snapshot.get("workload_identity"), snapshot.get("workload")
        if not isinstance(identity, dict) or set(identity) != {"pid", "starttime", "boot_id", "device", "inode", "path"}:
            return "observer workload identity is incomplete"
        if any(type(identity.get(k)) is not int or identity[k] <= 0 for k in ("pid", "device", "inode")) or not isinstance(identity.get("starttime"), str) or not identity["starttime"].isdigit() or int(identity["starttime"]) <= 0 or identity["boot_id"] != claim["boot_id"]:
            return "observer workload identity is malformed"
        if type(identity.get("path")) is not str or not identity["path"].startswith("/") or identity["path"] == "/" or any(p in (".", "..") for p in identity["path"].split("/")):
            return "observer workload cgroup path is malformed"
        if not isinstance(counters, dict) or any(type(counters.get(k)) is not int or counters[k] < 0 for k in ("memory_current_bytes", "memory_peak_bytes", "memory_swap_current_bytes", "oom", "oom_kill")):
            return "observer workload counters are incomplete"
        cgroup_time = snapshot.get("last_cgroup_monotonic_ns")
        if type(cgroup_time) is not int or not 0 < cgroup_time <= published or (snapshot.get("workload_released") is not True and now - cgroup_time > maximum):
            return "observer workload sample is stale or incomplete"
        if previous and previous.get("workload_identity") is not None and identity != previous["workload_identity"]:
            return "observer workload binding changed"
    if snapshot.get("first_coverage_failure"):
        return snapshot["first_coverage_failure"]
    if include_safety and snapshot.get("first_safety_failure"):
        return snapshot["first_safety_failure"]
    if snapshot.get("ready") is not True:
        return "observer is not ready"
    if terminal:
        if journal_mode and (snapshot.get("journal_query_complete") is not True or snapshot.get("journal_client_closed") is not True):
            return "journal final reconciliation is incomplete"
        cleanup = state.get("cleanup", {}).get("completed_monotonic_ns")
        begin, end = snapshot.get("drain_before_ns"), snapshot.get("drain_after_ns")
        request = snapshot.get("drain_request_ns")
        if snapshot.get("phase") != "finished" or snapshot.get("drain_complete") is not True or snapshot.get("descriptor_closed") is not True or type(snapshot.get("producer_exit_code")) is not int or snapshot["producer_exit_code"] != 0:
            return "observer terminal handshake is incomplete"
        if any(type(x) is not int for x in (cleanup, request, begin, end)) or not cleanup <= request <= begin <= end <= stamp <= now:
            return "terminal boundary does not follow cleanup with fresh resources"
        if state["start_consumed"] and (snapshot.get("cgroup_samples", 0) < 1 or snapshot.get("workload_released") is not True):
            return "workload coverage is incomplete through stopped state"
    return None


def step_results(text, plan):
    results = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        parts = line.split(" ")
        if len(parts) != 6 or parts[:2] != ["PULSAR_STEP", str(index)] or parts[3] not in ("0", "1"):
            raise ValueError("workload output lacks exact ordered step records")
        code = int(parts[2])
        if not 0 <= code <= 255 or index >= len(plan["definition"]["steps"]):
            raise ValueError("invalid step completion")
        out, err = (base64.b64decode(p, validate=True) for p in parts[4:])
        if len(out) > 262144 or len(err) > 262144:
            raise ValueError("step output capture exceeded its bound")
        complete = parts[3] == "1" and len(out) < 262144 and len(err) < 262144
        results.append({"index": index, "exit_code": code, "capture_complete": complete,
                        "stdout_bytes": len(out), "stderr_bytes": len(err)})
        if (code or not complete) and index != len(lines) - 1:
            raise ValueError("a step executed after first failure")
    if not results or (results[-1]["exit_code"] == 0 and results[-1]["capture_complete"] and len(results) != len(plan["definition"]["steps"])):
        raise ValueError("step completion records are incomplete")
    return results


def final_result(plan, claim, state, snapshot, *, workload_text=None, cleanup_only=False):
    cleanup, closure = state["cleanup"], state["observer_closure"]
    problem = snapshot_problem(snapshot, plan, claim, state, terminal=True, include_safety=False) if snapshot else "observer did not publish a terminal snapshot"
    stopped = cleanup.get("stopped_state")
    stopped_problem = complete_stopped_state(stopped, start_consumed=state["start_consumed"]) if state["container_id"] else None
    cleanup_clean = (not state["create_consumed"]) or (
        state["container_id"] is not None and
        cleanup.get("absent") is True and cleanup.get("query_rc") == 0 and
        cleanup.get("stop_rc") in (None, 0) and cleanup.get("rm_rc") in (None, 0) and
        stopped_problem is None and cleanup.get("rm_rc") == 0)
    closed = closure.get("waited") is True and type(closure.get("exit_code")) is int and closure["exit_code"] == 0 and closure.get("forced") is False
    if problem is None and not closed:
        problem = "actual observer closure did not confirm the terminal publication"
    steps, workload_problem = [], None
    if state["start_consumed"]:
        try:
            steps = step_results(workload_text or "", plan)
            if not all(row["capture_complete"] for row in steps):
                workload_problem = "step output capture exhausted"
            elif not stopped or stopped.get("ExitCode") != steps[-1]["exit_code"] or stopped.get("OOMKilled") is not False or stopped.get("Error") != "":
                workload_problem = "container exit does not match complete workload records"
            elif steps[-1]["exit_code"] != 0:
                workload_problem = "workload step failed"
        except (ValueError, TypeError):
            workload_problem = "bounded ordered workload capture is incomplete"
    # Cleanup uncertainty dominates every safety/workload outcome.
    if not cleanup_clean or closure.get("clients_closed") is not True or (state.get("observer_started") and not closure.get("waited")):
        outcome = "cleanup_unconfirmed"
    elif not state["create_consumed"]:
        outcome = "preflight_failed"
    elif cleanup_only or state["failures"] or problem or (snapshot or {}).get("first_safety_failure") or workload_problem or not closed or not state["start_consumed"]:
        outcome = "failed_clean"
    else:
        outcome = "succeeded"
    return {"schema_version": 2, "kind": "pulsar-diagnostic-result", "outcome": outcome,
            "plan_id": plan["plan_id"], "attempt_nonce": claim["attempt_nonce"], "phase": "terminal",
            "start_consumed": state["start_consumed"], "create_consumed": state["create_consumed"],
            "failures": state["failures"], "workload": {"steps": steps, "problem": workload_problem},
            "workload_exit_code": stopped.get("ExitCode") if stopped else None,
            "safety": {"first_failure": (snapshot or {}).get("first_safety_failure")},
            "coverage": {"complete": problem is None and not cleanup_only, "problem": problem,
                         "scope": "journal-delivered current-boot records and sampled resources" if plan["definition"]["observer"]["backend"] == "journal" else "native kmsg and sampled resources"},
            "cleanup": cleanup, "observer_closure": closure, "observation": snapshot,
            "cleanup_only": cleanup_only, "cannot_certify_lost_window": cleanup_only}
