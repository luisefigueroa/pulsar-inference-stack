#!/usr/bin/env python3
"""Schema owner for Pulsar operator platform reference files.

A platform reference is the closed set of GPU, architecture, RDMA-device, and
memory-policy constants that operator probes enforce. It is not ADR 0004
``hardware_class``, not release geometry, and not a serving permit.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shlex
import sys
from typing import Any

SCHEMA_VERSION = 1
KIND = "pulsar-platform-reference"
DEFAULT_PLATFORM_ID = "dgx-spark-gb10"
MEMORY_MODEL = "unified"
SAFE_PLATFORM_ID = re.compile(r"^[a-z][a-z0-9-]*$")
SAFE_ARCH = re.compile(r"^[A-Za-z0-9_]+$")
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLATFORMS_DIR = REPO_ROOT / "platforms"

TOP_FIELDS = (
    "schema_version",
    "kind",
    "platform_id",
    "display_name",
    "gpu_name",
    "architectures",
    "accelerators_per_node",
    "rdma",
    "memory",
)
RDMA_FIELDS = ("min_active_links_for_qualify", "verbs_device")
MEMORY_FIELDS = (
    "model",
    "hard_floor_available_gib",
    "min_os_buffer_gib",
    "launch_spike_gib",
    "overhead_gib_default",
    "preflight_warn_available_gib",
    "cold_start_footprint_slack",
)
EXPORT_KEYS = (
    "PULSAR_PLATFORM_ID",
    "PULSAR_PLATFORM_DISPLAY_NAME",
    "PULSAR_GPU_NAME",
    "PULSAR_ARCHITECTURES",
    "PULSAR_ACCELERATORS_PER_NODE",
    "PULSAR_RDMA_MIN_ACTIVE_LINKS",
    "PULSAR_RDMA_VERBS_DEVICE",
    "PULSAR_MEMORY_MODEL",
    "PULSAR_HARD_FLOOR_AVAILABLE_GIB",
    "PULSAR_MIN_OS_BUFFER_GIB",
    "PULSAR_LAUNCH_SPIKE_GIB",
    "PULSAR_OVERHEAD_GIB_DEFAULT",
    "PULSAR_PREFLIGHT_WARN_AVAILABLE_GIB",
    "PULSAR_COLD_START_FOOTPRINT_SLACK",
)


class PlatformReferenceError(ValueError):
    pass


def fail(message: str) -> None:
    raise PlatformReferenceError(message)


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or isinstance(value, bool):
        fail(f"{label}: expected an object")
    return value


def _require_exact_keys(
    document: dict[str, Any], fields: tuple[str, ...], label: str
) -> None:
    expected = set(fields)
    observed = set(document)
    extra = observed - expected
    missing = expected - observed
    if extra:
        fail(f"{label}: unknown field(s): {', '.join(sorted(extra))}")
    if missing:
        fail(f"{label}: missing field(s): {', '.join(sorted(missing))}")


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        fail(f"{label}: expected a non-empty string")
    if any(ch in value for ch in "\r\n\t"):
        fail(f"{label}: contains control characters")
    return value


def _require_int(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{label}: expected an integer")
    if value < minimum:
        fail(f"{label}: must be >= {minimum}")
    return value


def _require_nonneg_number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{label}: expected a number")
    if value < 0:
        fail(f"{label}: must be >= 0")
    return value


def _shell_number(value: int | float) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    dumped = json.dumps(value)
    if dumped in ("NaN", "Infinity", "-Infinity"):
        fail("memory number is not finite")
    return dumped


def validate_platform_reference(value: Any) -> dict[str, Any]:
    document = _require_object(value, "platform reference")
    _require_exact_keys(document, TOP_FIELDS, "platform reference")
    version = _require_int(
        document.get("schema_version"), "schema_version", minimum=1
    )
    if version != SCHEMA_VERSION:
        fail(f"schema_version {version} is unsupported")
    kind = _require_text(document.get("kind"), "kind")
    if kind != KIND:
        fail(f"kind must be {KIND}")
    platform_id = _require_text(document.get("platform_id"), "platform_id")
    if not SAFE_PLATFORM_ID.fullmatch(platform_id):
        fail("platform_id is not a safe identifier")
    display_name = _require_text(document.get("display_name"), "display_name")
    gpu_name = _require_text(document.get("gpu_name"), "gpu_name")
    raw_arches = document.get("architectures")
    if not isinstance(raw_arches, list) or not raw_arches:
        fail("architectures: expected a non-empty list")
    architectures: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_arches):
        arch = _require_text(item, f"architectures[{index}]")
        if not SAFE_ARCH.fullmatch(arch):
            fail(f"architectures[{index}]: unsafe architecture token")
        if arch in seen:
            fail(f"architectures[{index}]: duplicate {arch}")
        seen.add(arch)
        architectures.append(arch)
    accelerators = _require_int(
        document.get("accelerators_per_node"),
        "accelerators_per_node",
        minimum=1,
    )
    rdma = _require_object(document.get("rdma"), "rdma")
    _require_exact_keys(rdma, RDMA_FIELDS, "rdma")
    min_links = _require_int(
        rdma.get("min_active_links_for_qualify"),
        "rdma.min_active_links_for_qualify",
        minimum=0,
    )
    verbs = _require_text(rdma.get("verbs_device"), "rdma.verbs_device")
    if not verbs.startswith("/dev/"):
        fail("rdma.verbs_device must be a /dev path")
    memory = _require_object(document.get("memory"), "memory")
    _require_exact_keys(memory, MEMORY_FIELDS, "memory")
    model = _require_text(memory.get("model"), "memory.model")
    if model != MEMORY_MODEL:
        fail(f"memory.model must be {MEMORY_MODEL}")
    hard_floor = _require_nonneg_number(
        memory.get("hard_floor_available_gib"),
        "memory.hard_floor_available_gib",
    )
    min_buffer = _require_nonneg_number(
        memory.get("min_os_buffer_gib"), "memory.min_os_buffer_gib"
    )
    spike = _require_nonneg_number(
        memory.get("launch_spike_gib"), "memory.launch_spike_gib"
    )
    overhead = _require_nonneg_number(
        memory.get("overhead_gib_default"), "memory.overhead_gib_default"
    )
    warn_gib = _require_nonneg_number(
        memory.get("preflight_warn_available_gib"),
        "memory.preflight_warn_available_gib",
    )
    slack = memory.get("cold_start_footprint_slack")
    if isinstance(slack, bool) or not isinstance(slack, (int, float)):
        fail("memory.cold_start_footprint_slack: expected a number")
    if not 0 < float(slack) <= 1:
        fail("memory.cold_start_footprint_slack must be in (0, 1]")
    return {
        "schema_version": version,
        "kind": kind,
        "platform_id": platform_id,
        "display_name": display_name,
        "gpu_name": gpu_name,
        "architectures": architectures,
        "accelerators_per_node": accelerators,
        "rdma": {
            "min_active_links_for_qualify": min_links,
            "verbs_device": verbs,
        },
        "memory": {
            "model": model,
            "hard_floor_available_gib": hard_floor,
            "min_os_buffer_gib": min_buffer,
            "launch_spike_gib": spike,
            "overhead_gib_default": overhead,
            "preflight_warn_available_gib": warn_gib,
            "cold_start_footprint_slack": slack,
        },
    }


def load_platform_file(path: pathlib.Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
    except OSError as exc:
        fail(f"{path}: {exc}")
    except json.JSONDecodeError as exc:
        fail(f"{path}: invalid JSON ({exc})")
    return validate_platform_reference(document)


def _selected_file() -> tuple[pathlib.Path, str | None]:
    if "PULSAR_PLATFORM_FILE" in os.environ:
        raw = os.environ["PULSAR_PLATFORM_FILE"]
        if raw == "":
            fail("PULSAR_PLATFORM_FILE is empty")
        path = pathlib.Path(raw)
        if not path.is_absolute():
            fail("PULSAR_PLATFORM_FILE must be an absolute path")
        return path, None
    if "PULSAR_PLATFORM" in os.environ:
        platform_id = os.environ["PULSAR_PLATFORM"]
        if platform_id == "":
            fail("PULSAR_PLATFORM is empty")
    else:
        platform_id = DEFAULT_PLATFORM_ID
    if not SAFE_PLATFORM_ID.fullmatch(platform_id):
        fail("PULSAR_PLATFORM is not a safe identifier")
    return PLATFORMS_DIR / f"{platform_id}.json", platform_id


def load_current_platform() -> dict[str, Any]:
    path, expected_id = _selected_file()
    if not path.is_file():
        fail(f"platform reference file is missing: {path}")
    document = load_platform_file(path)
    if expected_id is not None and document["platform_id"] != expected_id:
        fail(
            "platform_id "
            f"{document['platform_id']} does not match selected id {expected_id}"
        )
    return document


def export_shell(document: dict[str, Any] | None = None) -> str:
    platform = document if document is not None else load_current_platform()
    memory = platform["memory"]
    rdma = platform["rdma"]
    values = {
        "PULSAR_PLATFORM_ID": platform["platform_id"],
        "PULSAR_PLATFORM_DISPLAY_NAME": platform["display_name"],
        "PULSAR_GPU_NAME": platform["gpu_name"],
        "PULSAR_ARCHITECTURES": " ".join(platform["architectures"]),
        "PULSAR_ACCELERATORS_PER_NODE": str(platform["accelerators_per_node"]),
        "PULSAR_RDMA_MIN_ACTIVE_LINKS": str(
            rdma["min_active_links_for_qualify"]
        ),
        "PULSAR_RDMA_VERBS_DEVICE": rdma["verbs_device"],
        "PULSAR_MEMORY_MODEL": memory["model"],
        "PULSAR_HARD_FLOOR_AVAILABLE_GIB": _shell_number(
            memory["hard_floor_available_gib"]
        ),
        "PULSAR_MIN_OS_BUFFER_GIB": _shell_number(memory["min_os_buffer_gib"]),
        "PULSAR_LAUNCH_SPIKE_GIB": _shell_number(memory["launch_spike_gib"]),
        "PULSAR_OVERHEAD_GIB_DEFAULT": _shell_number(
            memory["overhead_gib_default"]
        ),
        "PULSAR_PREFLIGHT_WARN_AVAILABLE_GIB": _shell_number(
            memory["preflight_warn_available_gib"]
        ),
        "PULSAR_COLD_START_FOOTPRINT_SLACK": _shell_number(
            memory["cold_start_footprint_slack"]
        ),
    }
    lines = [
        f"export {key}={shlex.quote(values[key])}" for key in EXPORT_KEYS
    ]
    return "\n".join(lines) + "\n"


def pretty_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load and validate a Pulsar operator platform reference."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("print-json", help="print the selected platform as JSON")
    sub.add_parser(
        "export-shell",
        help="print shell assignments for the selected platform",
    )
    args = parser.parse_args(argv)
    try:
        platform = load_current_platform()
        if args.command == "print-json":
            sys.stdout.write(pretty_json(platform))
        else:
            sys.stdout.write(export_shell(platform))
    except PlatformReferenceError as exc:
        print(f"platform-reference: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
