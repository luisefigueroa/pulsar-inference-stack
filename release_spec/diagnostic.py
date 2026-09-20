"""Closed diagnostic-container documents and pure observation reducers.

Serving specs, catalogs and resource-sample schemas are unchanged. This module
is standard-library-only besides release_spec helpers.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
import re
import shlex
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from release_spec.normalize import canonical_json_digest
from release_spec.serving import choice, closed, integer, invalid, load_json

GIB = 1024 ** 3
PROFILE = {
    "control_profile_revision": 2,
    "runtime": "runc",
    "network_mode": "none",
    "readonly_rootfs": True,
    "nano_cpus": 2_000_000_000,
    "memory_bytes": 16 * GIB,
    "memory_swap_bytes": 16 * GIB,
    "pids_limit": 256,
    "cap_drop": ["ALL"],
    "security_opt": ("no-new-privileges", "no-new-privileges:true"),
    "ipc_mode": "private",
    "shm_size_bytes": 64 * 1024 * 1024,
    "input_destination": "/pulsar-check",
    "output_destination": "/pulsar-output",
    "tmpfs": {"/tmp": "rw,nosuid,nodev,exec,size=512m"},
    "workload_deadline_seconds": 600,
    "operation_seconds": 1800,
    "cleanup_reserve_seconds": 300,
    "sample_interval_seconds": 0.25,
    "max_observation_age_seconds": 1,
    "mem_available_floor_bytes": 4 * GIB,
    "swap_growth_limit_bytes": 256 * 1024 * 1024,
    "readiness_age_seconds": 600,
    "capture_budget_bytes": 64 * 1024 * 1024,
    "gpu_device_id": "0",
    "stop_signal": "SIGTERM",
    "stop_timeout_seconds": -1,
    "log_driver": "local",
    "log_options": {"max-size": "16m", "max-file": "1", "compress": "false"},
    "step_output_limit_bytes": 262144,
    # This capability accepts one explicit proc protection profile. A daemon
    # reporting a different profile is refused, never inferred compatible.
    "masked_paths": ["/proc/acpi", "/proc/asound", "/proc/interrupts", "/proc/kcore", "/proc/keys",
                     "/proc/latency_stats", "/proc/timer_list", "/proc/timer_stats", "/proc/sched_debug",
                     "/proc/scsi", "/sys/firmware", "/sys/devices/virtual/powercap"],
    "readonly_paths": ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"],
}

DEFINITION_KIND = "pulsar-diagnostic-definition"
PLAN_KIND = "pulsar-diagnostic-plan"
RESULT_KIND = "pulsar-diagnostic-result"
CLAIM_KIND = "pulsar-diagnostic-claim"
DEFINITION_FIELDS = {
    "schema_version", "kind", "image_reference", "image_id", "image_config_digest",
    "platform", "inputs", "steps", "environment", "working_directory", "observer",
}
OBSERVER_FIELDS = {
    "backend", "sample_interval_seconds", "max_observation_age_seconds",
    "mem_available_floor_bytes", "swap_growth_limit_bytes",
    "workload_deadline_seconds", "operation_seconds", "cleanup_reserve_seconds",
}
INPUT_FIELDS = {"name", "sha256", "bytes", "mode"}
STEP_FIELDS = {"argv", "timeout_seconds"}
PLAN_FIELDS = {
    "schema_version", "kind", "plan_id", "definition", "node_id", "inputs_root",
    "entrypoint_sha256", "payload_sha256",
    "control_profile",
}
PHASES = (
    "claimed", "observer_ready", "created", "controls_verified", "start_claimed",
    "running_or_uncertain", "stopping_or_exited", "removed_or_unknown",
    "observer_closed_or_unknown", "terminal",
)
OUTCOMES = ("succeeded", "failed_clean", "cleanup_unconfirmed", "preflight_failed")
KERNEL_MARKERS = ("NV_ERR_NO_MEMORY", "NVRM: Xid", "Out of memory:", "invoked oom-killer", "oom-kill:", "Killed process")
KMSG_PREFIX = re.compile(r"^(\d+),(\d+),(\d+),([a-z-]*);(.*)$", re.S)
NAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._+-]{0,127}$")
NODE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
BOOT_RE = re.compile(r"^[0-9a-f]{32}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_image(value: Any, path: str) -> str:
    if not isinstance(value, str) or not IMAGE_RE.fullmatch(value):
        invalid(path, "expected sha256:<64 hex>")
    return value


def require_boot(value: Any, path: str) -> str:
    if not isinstance(value, str) or not BOOT_RE.fullmatch(value):
        invalid(path, "expected 32-hex boot identity")
    return value


def parse_kmsg_record(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, (bytes, bytearray)) or not raw or len(raw) >= 8192 or not raw.endswith(b"\n"):
        invalid("kmsg", "oversized or empty kernel record")
    try:
        text = bytes(raw).decode("utf-8", "surrogateescape")
    except Exception:
        invalid("kmsg", "undecodable kernel record")
    if text.endswith("\n"):
        text = text[:-1]
    match = KMSG_PREFIX.fullmatch(text)
    if not match:
        invalid("kmsg", "malformed kernel record")
    facility_level, sequence, timestamp, flags, message = match.groups()
    if int(facility_level) > 191 or flags not in ("-", "c") or any(not line.startswith(" ") for line in message.split("\n")[1:]):
        invalid("kmsg", "unsupported flags or ambiguous whole record")
    return {
        "facility_level": int(facility_level),
        "sequence": int(sequence),
        "timestamp_us": int(timestamp),
        "flags": flags,
        "message": message,
        "raw_sha256": sha256_bytes(bytes(raw)),
        "raw": text + "\n",
    }


def counted_kernel_failure(message: str) -> bool:
    if not isinstance(message, str):
        return False
    return any(marker in message for marker in KERNEL_MARKERS)


def _verify_input(item: Any, index: int) -> dict:
    row = closed(item, INPUT_FIELDS, f"inputs[{index}]")
    name = row["name"]
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or "/" in name or name in (".", ".."):
        invalid(f"inputs[{index}].name", "expected a single-path regular file name")
    if not isinstance(row["sha256"], str) or not HEX64_RE.fullmatch(row["sha256"]):
        invalid(f"inputs[{index}].sha256", "expected 64 hex")
    integer(row["bytes"], f"inputs[{index}].bytes", 1)
    integer(row["mode"], f"inputs[{index}].mode", 0)
    if row["bytes"] > 16 * 1024 * 1024:
        invalid(f"inputs[{index}].bytes", "input exceeds 16 MiB")
    if row["mode"] > 0o777 or row["mode"] & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        invalid(f"inputs[{index}].mode", "setuid/setgid/sticky inputs are not permitted")
    return row


def _verify_step(item: Any, index: int, version: int = 1) -> dict:
    row = closed(item, STEP_FIELDS | ({"capture_file"} if version == 2 else set()), f"steps[{index}]")
    if version == 2 and row["capture_file"] not in (None, "/tmp/result.json"):
        invalid(f"steps[{index}].capture_file", "only the bounded tmpfs result destination is supported")
    argv = row["argv"]
    if not isinstance(argv, list) or not argv or any(not isinstance(part, str) or not part or "\0" in part for part in argv):
        invalid(f"steps[{index}].argv", "expected a nonempty argv array of strings")
    integer(row["timeout_seconds"], f"steps[{index}].timeout_seconds", 1)
    if row["timeout_seconds"] > PROFILE["workload_deadline_seconds"]:
        invalid(f"steps[{index}].timeout_seconds", "step timeout exceeds workload deadline")
    return row


def _verify_observer(value: Any) -> dict:
    observer = closed(value, OBSERVER_FIELDS, "observer")
    choice(observer["backend"], ("kmsg", "journal"), "observer.backend")
    interval = observer["sample_interval_seconds"]
    if type(interval) not in (int, float) or not (0 < interval <= PROFILE["sample_interval_seconds"]):
        invalid("observer.sample_interval_seconds", "must be >0 and at most 0.25s")
    integer(observer["max_observation_age_seconds"], "observer.max_observation_age_seconds", 1)
    if observer["max_observation_age_seconds"] > PROFILE["max_observation_age_seconds"]:
        invalid("observer.max_observation_age_seconds", "cannot relax 1s staleness")
    integer(observer["mem_available_floor_bytes"], "observer.mem_available_floor_bytes", 1)
    if observer["mem_available_floor_bytes"] < PROFILE["mem_available_floor_bytes"]:
        invalid("observer.mem_available_floor_bytes", "cannot relax 4 GiB floor")
    integer(observer["swap_growth_limit_bytes"], "observer.swap_growth_limit_bytes", 1)
    if observer["swap_growth_limit_bytes"] > PROFILE["swap_growth_limit_bytes"]:
        invalid("observer.swap_growth_limit_bytes", "cannot relax 256 MiB swap-growth")
    integer(observer["workload_deadline_seconds"], "observer.workload_deadline_seconds", 1)
    if observer["workload_deadline_seconds"] > PROFILE["workload_deadline_seconds"]:
        invalid("observer.workload_deadline_seconds", "cannot relax 600s deadline")
    integer(observer["operation_seconds"], "observer.operation_seconds", 1)
    if observer["operation_seconds"] > PROFILE["operation_seconds"]:
        invalid("observer.operation_seconds", "cannot relax 1800s operation")
    integer(observer["cleanup_reserve_seconds"], "observer.cleanup_reserve_seconds", PROFILE["cleanup_reserve_seconds"])
    if observer["cleanup_reserve_seconds"] + observer["workload_deadline_seconds"] > observer["operation_seconds"]:
        invalid("observer.cleanup_reserve_seconds", "cleanup reserve must fit in the operation budget")
    return observer


def image_evidence(definition: dict) -> tuple[dict, dict]:
    """Verify exact OCI bytes before interpreting image-owned configuration."""
    evidence = closed(definition["image_evidence"], {"manifest_base64", "config_base64"}, "image_evidence")
    raw = []
    for key in ("manifest_base64", "config_base64"):
        value = evidence[key]
        if not isinstance(value, str) or len(value) > 65536:
            invalid("image_evidence", "raw metadata exceeds the bounded input")
        raw.append(base64.b64decode(value, validate=True))
    manifest, config = (json.loads(value) for value in raw)
    if "sha256:" + sha256_bytes(raw[0]) != definition["image_reference"] or definition["image_id"] != definition["image_reference"]:
        invalid("image_evidence", "manifest bytes do not bind the selected image")
    descriptor = manifest.get("config", {})
    if (manifest.get("schemaVersion") != 2 or
            manifest.get("mediaType") != "application/vnd.oci.image.manifest.v1+json" or
            descriptor.get("mediaType") != "application/vnd.oci.image.config.v1+json" or
            descriptor.get("digest") != definition["image_config_digest"] or
            descriptor.get("size") != len(raw[1]) or
            "sha256:" + sha256_bytes(raw[1]) != definition["image_config_digest"]):
        invalid("image_evidence", "manifest-to-config byte binding failed")
    if config.get("os", "") + "/" + config.get("architecture", "") != definition["platform"]:
        invalid("image_evidence", "config platform mismatch")
    rootfs = config.get("rootfs", {})
    if rootfs.get("type") != "layers" or not isinstance(rootfs.get("diff_ids"), list) or not rootfs["diff_ids"]:
        invalid("image_evidence", "ordered rootfs diff IDs are missing")
    for digest in rootfs["diff_ids"]:
        require_image(digest, "image_evidence.rootfs")
    if not isinstance(config.get("config"), dict):
        invalid("image_evidence", "image-owned config missing")
    image_environment(config)
    return manifest, config


def image_environment(config: dict) -> dict:
    result = {}
    values = config["config"].get("Env", [])
    if not isinstance(values, list):
        invalid("image_evidence", "image environment missing")
    for value in values:
        if not isinstance(value, str) or "\0" in value or "=" not in value:
            invalid("image_evidence", "malformed image environment")
        key, value = value.split("=", 1)
        if not ENV_RE.fullmatch(key) or key in result:
            invalid("image_evidence", "ambiguous image environment")
        result[key] = value
    return result


def verify_definition(document: Any) -> dict:
    version = document.get("schema_version") if isinstance(document, dict) else None
    fields = DEFINITION_FIELDS | ({"image_evidence"} if version == 2 else set())
    data = copy.deepcopy(closed(document, fields, "definition"))
    if type(version) is not int or version not in (1, 2) or data["kind"] != DEFINITION_KIND:
        invalid("kind", "unsupported diagnostic definition")
    require_image(data["image_reference"], "image_reference")
    require_image(data["image_id"], "image_id")
    require_image(data["image_config_digest"], "image_config_digest")
    if data["image_reference"] == data["image_config_digest"] and data["image_id"] == data["image_config_digest"]:
        # Allowed only when the operator supplies identical values; still three fields.
        pass
    if not isinstance(data["platform"], str) or not re.fullmatch(r"linux/(arm64|amd64)", data["platform"]):
        invalid("platform", "expected linux/arm64 or linux/amd64")
    if not isinstance(data["inputs"], list) or not data["inputs"]:
        invalid("inputs", "expected a nonempty input inventory")
    data["inputs"] = [_verify_input(item, index) for index, item in enumerate(data["inputs"])]
    names = [item["name"] for item in data["inputs"]]
    if "pulsar-diagnostic-entrypoint.sh" in names or len(names) > 32 or sum(i["bytes"] for i in data["inputs"]) > 16 * 1024**2:
        invalid("inputs", "reserved entrypoint name or bounded inventory exceeded")
    if len(names) != len(set(names)):
        invalid("inputs", "duplicate input names")
    if not isinstance(data["steps"], list) or not data["steps"]:
        invalid("steps", "expected a nonempty argv step list")
    data["steps"] = [_verify_step(item, index, version) for index, item in enumerate(data["steps"])]
    if len(data["steps"]) > 16:
        invalid("steps", "at most sixteen steps are supported")
    env = data["environment"]
    if not isinstance(env, dict) or any(not ENV_RE.fullmatch(str(key)) or not isinstance(value, str) or "\0" in value
                                        for key, value in env.items()):
        invalid("environment", "expected a map of safe environment names to strings")
    reserved = {"PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "BASH_ENV", "ENV", "BASHOPTS", "SHELLOPTS", "CDPATH", "GLOBIGNORE"}
    if version == 2:
        _, config = image_evidence(data)
        # Loader/PATH values remain image-owned. HOME is an explicit tmpfs
        # cache-placement choice, never an implicit change to the image.
        reserved.remove("HOME")
        if env.get("HOME") != "/tmp":
            invalid("environment.HOME", "development v2 requires explicit /tmp HOME")
        if (set(image_environment(config)) & (reserved - {"PATH", "LD_LIBRARY_PATH"})):
            invalid("image_evidence", "unsupported inherited execution hooks")
    if reserved & set(env):
        invalid("environment", "PATH/HOME/loader variables are reserved")
    if data["working_directory"] != "/":
        invalid("working_directory", "v1 working directory must be /")
    data["observer"] = _verify_observer(data["observer"])
    return data


def emit_entrypoint(definition: dict) -> str:
    # All workload output is captured in bounded tmpfs files before encoding.
    # Markers cannot be confused with arbitrary payload stdout/stderr.
    lines = ["#!/bin/bash", "set -euo pipefail", "cd /", "umask 077"]
    if definition["schema_version"] == 1:
        lines.append("export HOME=/tmp")
    lines += ["ulimit -f 256",
             "step_pid=", "cancelled=0", "cancel_step() {", "  cancelled=1",
             "  if [ -n \"$step_pid\" ]; then kill -TERM \"$step_pid\" 2>/dev/null || :; fi", "}",
             "trap cancel_step TERM INT"]
    for key, value in sorted(definition["environment"].items()):
        lines.append(f"export {key}={shlex.quote(value)}")
    for index, step in enumerate(definition["steps"]):
        quoted = " ".join(shlex.quote(part) for part in step["argv"])
        limit = integer(step["timeout_seconds"], f"steps[{index}].timeout_seconds", 1)
        lines.append(f"timeout --signal=TERM --kill-after=2s {limit}s {quoted} </dev/null >/tmp/pulsar-step.out 2>/tmp/pulsar-step.err &")
        lines += ["step_pid=$!", "while :", "do",
                  "  if wait \"$step_pid\"; then rc=0; break; else rc=$?; fi",
                  "  kill -0 \"$step_pid\" 2>/dev/null || break", "done", "step_pid="]
        if step.get("capture_file"):
            # Fixed data-file capture after wait and before the fail-first exit:
            # preserve numerical details even when the fixture returns failure.
            lines += ["file_capture=1", "if [ -f /tmp/result.json ] && [ ! -L /tmp/result.json ] && [ \"$(wc -c </tmp/result.json)\" -lt 262144 ]",
                      "then", "  if ! { printf '\\nPULSAR_OUTPUT_JSON\\n'; cat -- /tmp/result.json; } >>/tmp/pulsar-step.out; then file_capture=0; fi",
                      "else", "  file_capture=0", "fi"]
        lines += ["out_bytes=$(wc -c </tmp/pulsar-step.out)", "err_bytes=$(wc -c </tmp/pulsar-step.err)", "capture=1"]
        if step.get("capture_file"):
            lines.append("capture=$file_capture")
        lines += [
                  "if [ \"$out_bytes\" -ge 262144 ] || [ \"$err_bytes\" -ge 262144 ]", "then", "  capture=0", "fi",
                  f"printf 'PULSAR_STEP {index} %s %s %s %s\\n' \"$rc\" \"$capture\" \"$(base64 -w0 /tmp/pulsar-step.out)\" \"$(base64 -w0 /tmp/pulsar-step.err)\"",
                  "if [ \"$rc\" -ne 0 ]", "then", "  exit \"$rc\"", "fi",
                  "if [ \"$cancelled\" -ne 0 ]", "then", "  exit 143", "fi",
                  "if [ \"$capture\" -ne 1 ]", "then", "  exit 125", "fi"]
    return "\n".join(lines) + "\n"


def verify_payload_dir(definition: dict, root: Path, *, sealed: bool = False) -> str:
    if root.is_symlink() or not root.is_dir():
        invalid("inputs_root", "payload root must be a regular directory")
    digest = hashlib.sha256()
    for item in definition["inputs"]:
        path = root / item["name"]
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            invalid("inputs", f"{item['name']} must be a regular file without extra links")
        info = path.stat()
        if info.st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
            invalid("inputs", f"{item['name']} does not match the frozen inventory")
        if stat.S_IMODE(info.st_mode) != (item["mode"] & (0o555 if sealed else 0o777)):
            invalid("inputs", f"{item['name']} mode does not match the frozen inventory")
        digest.update(item["name"].encode())
        digest.update(bytes.fromhex(item["sha256"]))
    extra = {entry.name for entry in os.scandir(root) if entry.name != "pulsar-diagnostic-entrypoint.sh"}
    expected = {item["name"] for item in definition["inputs"]}
    if extra != expected:
        invalid("inputs", "payload directory contents do not match the inventory")
    return digest.hexdigest()


def freeze_plan(definition: Any, *, node_id: str, inputs_root: str) -> dict:
    verified = verify_definition(definition)
    if not isinstance(node_id, str) or not NODE_RE.fullmatch(node_id):
        invalid("node_id", "invalid node identity")
    root = Path(inputs_root)
    payload = verify_payload_dir(verified, root)
    entrypoint = emit_entrypoint(verified)
    plan = {
        "schema_version": 2,
        "kind": PLAN_KIND,
        "definition": verified,
        "node_id": node_id,
        "inputs_root": str(root),
        "entrypoint_sha256": sha256_bytes(entrypoint.encode()),
        "payload_sha256": payload,
        "control_profile": json.loads(json.dumps(PROFILE)),
    }
    identity = {key: plan[key] for key in ("schema_version", "kind", "definition", "node_id",
                                           "entrypoint_sha256", "payload_sha256", "control_profile")}
    plan["plan_id"] = canonical_json_digest(identity)
    return closed(plan, PLAN_FIELDS, "plan")


def verify_plan(document: Any) -> dict:
    plan = copy.deepcopy(closed(document, PLAN_FIELDS, "plan"))
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 2 or plan["kind"] != PLAN_KIND:
        invalid("kind", "unsupported diagnostic plan")
    if not isinstance(plan["plan_id"], str) or not HEX64_RE.fullmatch(plan["plan_id"]):
        invalid("plan_id", "expected 64 hex")
    verify_definition(plan["definition"])
    if canonical_json_digest(plan["control_profile"]) != canonical_json_digest(PROFILE):
        invalid("control_profile", "unsupported diagnostic control profile")
    if not isinstance(plan["node_id"], str) or not NODE_RE.fullmatch(plan["node_id"]):
        invalid("node_id", "invalid node identity")
    if not isinstance(plan["inputs_root"], str) or not plan["inputs_root"]:
        invalid("inputs_root", "missing payload root")
    # Plan verification is data-only. Caller bytes are read at freeze/sealing,
    # never by observation, mutation authorization, cleanup or publication.
    digest = hashlib.sha256()
    for item in plan["definition"]["inputs"]:
        digest.update(item["name"].encode())
        digest.update(bytes.fromhex(item["sha256"]))
    identity = {key: plan[key] for key in ("schema_version", "kind", "definition", "node_id", "entrypoint_sha256", "payload_sha256", "control_profile")}
    if canonical_json_digest(identity) != plan["plan_id"] or sha256_bytes(emit_entrypoint(plan["definition"]).encode()) != plan["entrypoint_sha256"] \
            or digest.hexdigest() != plan["payload_sha256"]:
        invalid("plan_id", "plan identity does not match canonical contents")
    return plan


def load_plan(path: str | Path) -> dict:
    return verify_plan(load_json(path))


def initial_state(binding: dict) -> dict:
    return {
        **binding,
        "phase": "starting",
        "samples": 0,
        "kernel_records": 0,
        "driver_memory_errors": 0,
        "first_driver_memory_error": None,
        "min_mem_available_bytes": None,
        "max_swap_used_bytes": None,
        "first_memory_breach": None,
        "coverage_ok": True,
        "coverage_reason": None,
        "last_sequence": None,
        "last_sample_at": None,
        "last_sample_monotonic_ns": None,
        "drain_complete": False,
        "drain_monotonic_ns": None,
        "continuation": "",
        "pending_unsafe_fragment": False,
        "cgroup_samples": 0,
        "workload_oom": None,
        "workload_oom_kill": None,
        "workload_swap_bytes": None,
        "first_cgroup_oom": None,
    }


def latch_coverage(state: dict, reason: str) -> None:
    if state.get("coverage_ok") is False:
        return
    state["coverage_ok"] = False
    state["coverage_reason"] = reason
    state["phase"] = "failed"


def reduce_kernel_record(state: dict, record: dict, *, received_monotonic_ns: int) -> None:
    if state.get("coverage_ok") is False:
        return
    parsed = record if "sequence" in record and "message" in record else parse_kmsg_record(record.get("raw", b""))
    sequence = parsed["sequence"]
    previous = state.get("last_sequence")
    if previous is not None and sequence != previous + 1:
        latch_coverage(state, "kernel sequence gap or regression")
        return
    state["last_sequence"] = sequence
    state["kernel_records"] += 1
    flags = parsed.get("flags") or ""
    message = parsed["message"]
    if "c" in flags:
        state["continuation"] = (state.get("continuation") or "") + message
        if counted_kernel_failure(state["continuation"]):
            state["pending_unsafe_fragment"] = True
        return
    text = (state.get("continuation") or "") + message
    state["continuation"] = ""
    if counted_kernel_failure(text):
        state["driver_memory_errors"] += 1
        state["pending_unsafe_fragment"] = False
        if state["first_driver_memory_error"] is None:
            state["first_driver_memory_error"] = {
                "sequence": sequence,
                "timestamp_us": parsed["timestamp_us"],
                "received_monotonic_ns": received_monotonic_ns,
                "message_sha256": parsed["raw_sha256"],
                "attribution": "unknown-host-wide",
            }
    elif state.get("pending_unsafe_fragment"):
        latch_coverage(state, "ambiguous kernel fragment")


def reduce_memory(state: dict, sample: dict, *, floor: int, swap_limit: int, baseline_swap: int) -> None:
    available = sample.get("mem_available_bytes")
    swap = sample.get("swap_used_bytes")
    if type(available) is not int or available < 0 or type(swap) is not int or swap < 0:
        latch_coverage(state, "invalid memory sample")
        return
    state["samples"] += 1
    state["last_sample_at"] = sample.get("sampled_at")
    state["last_sample_monotonic_ns"] = sample.get("monotonic_ns")
    lowest = state.get("min_mem_available_bytes")
    if lowest is None or available < lowest:
        state["min_mem_available_bytes"] = available
    if available < floor and state.get("first_memory_breach") is None:
        state["first_memory_breach"] = {"at": sample.get("sampled_at"), "mem_available_bytes": available}
    highest = state.get("max_swap_used_bytes")
    if highest is None or swap > highest:
        state["max_swap_used_bytes"] = swap
    used_growth = swap - baseline_swap
    if used_growth < 0:
        used_growth = 0
    state["swap_growth_bytes"] = used_growth


def memory_problem(state: dict, *, floor: int, swap_limit: int) -> str | None:
    lowest = state.get("min_mem_available_bytes")
    if type(lowest) is int and lowest < floor:
        return "MemAvailable below 4 GiB floor"
    growth = state.get("swap_growth_bytes")
    highest = state.get("max_swap_used_bytes")
    if type(growth) is int and growth >= swap_limit:
        return "node swap grew by at least 256 MiB"
    if type(highest) is int and type(state.get("baseline_swap_bytes")) is int \
            and highest - state["baseline_swap_bytes"] >= swap_limit:
        return "node swap grew by at least 256 MiB"
    return None


def reduce_event(state: dict, event: dict, *, floor: int, swap_limit: int, baseline_swap: int) -> None:
    kind = event.get("kind")
    if kind in ("drain_complete", "finished"):
        if event.get("boot_id") != state.get("boot_id") or not event.get("observer_instance") or event.get("observer_instance") != state.get("observer_instance"):
            latch_coverage(state, "terminal observer identity mismatch")
            return
    if kind == "ready":
        if state["phase"] != "starting":
            latch_coverage(state, "duplicate observer ready")
            return
        if event.get("kmsg_ready") is not True or event.get("journal_backend"):
            latch_coverage(state, "observer ready is not a kmsg tail drain")
            return
        require_boot(event.get("boot_id"), "ready.boot_id")
        if state.get("boot_id") and event["boot_id"] != state["boot_id"]:
            latch_coverage(state, "kernel boot mismatch")
            return
        state["boot_id"] = event["boot_id"]
        state["observer_instance"] = event.get("observer_instance")
        state["kmsg_tail_monotonic_ns"] = event.get("tail_monotonic_ns")
        state["phase"] = "observing"
        mem = event.get("memory") or {}
        reduce_memory(state, mem, floor=floor, swap_limit=swap_limit, baseline_swap=baseline_swap)
        state["baseline_swap_bytes"] = mem.get("swap_used_bytes")
    elif kind == "memory":
        if event.get("boot_id") != state.get("boot_id"):
            latch_coverage(state, "kernel boot mismatch")
            return
        instance = event.get("observer_instance")
        if instance and state.get("observer_instance") and instance != state["observer_instance"]:
            latch_coverage(state, "observer instance changed")
            return
        reduce_memory(state, event, floor=floor, swap_limit=swap_limit,
                      baseline_swap=state.get("baseline_swap_bytes") if type(state.get("baseline_swap_bytes")) is int else baseline_swap)
    elif kind == "kernel":
        if event.get("boot_id") != state.get("boot_id"):
            latch_coverage(state, "kernel boot mismatch")
            return
        instance = event.get("observer_instance")
        if instance and state.get("observer_instance") and instance != state["observer_instance"]:
            latch_coverage(state, "observer instance changed")
            return
        reduce_kernel_record(state, event.get("record") or event, received_monotonic_ns=int(event.get("received_monotonic_ns") or 0))
    elif kind == "cgroup":
        if event.get("boot_id") and state.get("boot_id") and event.get("boot_id") != state.get("boot_id"):
            latch_coverage(state, "kernel boot mismatch")
            return
        if event.get("unknown") is True or event.get("workload") is None:
            latch_coverage(state, "workload cgroup counters are unknown after start")
            return
        workload = event.get("workload") or {}
        oom = workload.get("oom")
        oom_kill = workload.get("oom_kill")
        swap = workload.get("memory_swap_current_bytes")
        if type(oom) is not int or type(oom_kill) is not int or type(swap) is not int:
            latch_coverage(state, "workload cgroup counters are unknown after start")
            return
        state["cgroup_samples"] = int(state.get("cgroup_samples") or 0) + 1
        state["workload_oom"] = oom
        state["workload_oom_kill"] = oom_kill
        state["workload_swap_bytes"] = swap if type(swap) is int else None
        state["workload_pid"] = event.get("pid")
        if oom > 0 or oom_kill > 0:
            if state.get("first_cgroup_oom") is None:
                state["first_cgroup_oom"] = {
                    "oom": oom, "oom_kill": oom_kill, "pid": event.get("pid"),
                    "monotonic_ns": event.get("monotonic_ns"),
                }
    elif kind == "coverage":
        latch_coverage(state, str(event.get("reason") or "observer coverage failed"))
    elif kind == "drain_complete":
        if event.get("fresh_eagain") is not True:
            latch_coverage(state, "terminal drain was not a fresh EAGAIN")
            return
        reported = event.get("last_sequence")
        if reported is not None and state.get("last_sequence") is not None and reported < state["last_sequence"]:
            latch_coverage(state, "drain sequence is not continuous")
            return
        if reported is not None:
            if state.get("last_sequence") is None:
                state["last_sequence"] = reported
            elif reported != state["last_sequence"] and reported != state["last_sequence"]:
                if reported > state["last_sequence"]:
                    latch_coverage(state, "drain skipped kernel records")
                    return
        state["drain_complete"] = True
        state["drain_monotonic_ns"] = event.get("monotonic_ns")
        if reported is None and state.get("last_sequence") is None:
            state["empty_drain_interval"] = True
    elif kind == "finished":
        state["observer_exit_code"] = event.get("exit_code")
        if event.get("controlled_stop") is True and event.get("exit_code") == 0 and state.get("coverage_ok") is True:
            if state["phase"] != "failed":
                state["phase"] = "finished"
        else:
            state["phase"] = "failed"
    else:
        invalid("event.kind", "unknown observer event")


def terminal_problem(terminal: dict, *, plan: dict, claim: dict, previous: dict | None,
                     cleanup_monotonic_ns: int | None, baseline_swap: int) -> str | None:
    if not isinstance(terminal, dict):
        return "invalid terminal snapshot"
    for key in ("plan_id", "attempt_nonce", "boot_id", "container_id", "image_id"):
        if terminal.get(key) != claim.get(key) and key != "container_id":
            if key == "plan_id" and terminal.get(key) != plan["plan_id"]:
                return "terminal plan mismatch"
        if key == "attempt_nonce" and terminal.get(key) != claim.get("attempt_nonce"):
            return "terminal attempt mismatch"
        if key == "boot_id" and terminal.get(key) != claim.get("boot_id"):
            return "terminal boot mismatch"
        if key == "image_id" and terminal.get(key) != plan["definition"]["image_id"]:
            return "terminal image mismatch"
    if terminal.get("plan_id") != plan["plan_id"]:
        return "terminal plan mismatch"
    if terminal.get("container_id") != claim.get("container_id"):
        return "terminal container mismatch"
    if terminal.get("coverage_ok") is not True:
        return terminal.get("coverage_reason") or "terminal coverage failed"
    if terminal.get("continuation"):
        return "unclassified trailing continuation must fail closed"
    if terminal.get("pending_unsafe_fragment"):
        return "ambiguous kernel fragment"
    if terminal.get("observer_exit_code") != 0:
        return "observer did not close cleanly"
    if terminal.get("drain_complete") is not True:
        return "terminal kernel drain is incomplete"
    drain_at = terminal.get("drain_monotonic_ns")
    if cleanup_monotonic_ns is not None:
        if type(drain_at) not in (int, float) or drain_at < cleanup_monotonic_ns:
            return "terminal coverage end does not follow cleanup"
        sample_at = terminal.get("last_sample_monotonic_ns")
        if type(sample_at) not in (int, float) or sample_at < cleanup_monotonic_ns:
            return "terminal memory sample does not follow cleanup"
    if terminal.get("driver_memory_errors"):
        return "terminal retained a driver or host-OOM kernel error"
    if terminal.get("first_driver_memory_error") is not None:
        return "terminal retained a driver or host-OOM kernel error"
    if terminal.get("first_cgroup_oom") is not None:
        return "owned cgroup recorded OOM or oom-kill"
    if claim.get("phase") in ("start_claimed", "running_or_uncertain", "stopping_or_exited"):
        samples = terminal.get("cgroup_samples")
        if type(samples) is not int or samples < 1:
            return "workload cgroup coverage missing after start"
        if terminal.get("workload_oom_kill") is None or terminal.get("workload_oom") is None:
            return "workload cgroup counters are unknown after start"
    observer = plan["definition"]["observer"]
    problem = memory_problem(terminal, floor=observer["mem_available_floor_bytes"],
                             swap_limit=observer["swap_growth_limit_bytes"])
    if problem:
        return "terminal collector retained a memory-floor or swap-growth breach"
    if type(terminal.get("mem_available_bytes", terminal.get("min_mem_available_bytes"))) is not int:
        return "terminal memory state missing"
    if previous is not None:
        for key in ("boot_id", "attempt_nonce", "container_id", "plan_id"):
            if terminal.get(key) != previous.get(key):
                return "terminal collector identity changed"
        for key in ("samples", "kernel_records", "driver_memory_errors"):
            if type(terminal.get(key)) is not int or type(previous.get(key)) is not int or terminal[key] < previous[key]:
                return "terminal collector counters regressed"
    if terminal.get("phase") == "finished" and terminal.get("observer_exit_code") == 0:
        return None
    return "collector terminal phase is not a clean finish"


def complete_stopped_state(state: Any, *, start_consumed: bool | None = None) -> str | None:
    if not isinstance(state, dict) or not state:
        return "stopped state is missing"
    if state.get("Running") is not False:
        return "container is not proven non-running"
    if state.get("Restarting") is not False:
        return "container is not proven non-restarting"
    status = state.get("Status")
    if status not in ("exited", "created"):
        return "container status is not a stopped state"
    if type(state.get("Pid")) is not int:
        return "stopped pid is not an integer"
    if state["Pid"] != 0:
        return "stopped container still has a pid"
    if type(state.get("ExitCode")) is not int:
        return "exit code is not an integer"
    if type(state.get("OOMKilled")) is not bool:
        return "OOMKilled is not a boolean"
    if not isinstance(state.get("Error"), str):
        return "Error is not a string"
    if not isinstance(state.get("StartedAt"), str) or not state["StartedAt"]:
        return "StartedAt is missing"
    if not isinstance(state.get("FinishedAt"), str) or not state["FinishedAt"]:
        return "FinishedAt is missing"
    try:
        started = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))
        finished = datetime.fromisoformat(state["FinishedAt"].replace("Z", "+00:00"))
        if started.tzinfo is None or finished.tzinfo is None:
            return "timestamps must include timezone"
        if status == "created":
            if start_consumed is True or started.year != 1 or finished.year != 1 or state["ExitCode"] != 0 or state["OOMKilled"] or state["Error"]:
                return "created disposition does not match reached phase"
        elif started.year == 1 or finished.year == 1 or finished < started:
            return "exited timestamps are invalid"
    except (ValueError, OverflowError):
        return "stopped timestamps are invalid"
    return None
