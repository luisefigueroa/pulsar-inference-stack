"""Closed diagnostic requests and immutable, bounded Python payloads."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat

GIB = 1024**3
MAX_PAYLOAD = 4 * 1024**2
LABEL = "io.pulsar.diagnostic"
CONTAINER_NAME = "pulsar-diagnostic-gpu0"


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def keys(value, expected, field):
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise ValueError(f"{field}: fields must be {expected}")


def number(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{field}: integer between {low} and {high} required")


def sha(value, field, prefix=""):
    if not isinstance(value, str) or not re.fullmatch(
        re.escape(prefix) + "[0-9a-f]{64}", value
    ):
        raise ValueError(f"{field}: SHA-256 required")


def validate(request):
    keys(
        request,
        "schema_version kind topology_id image_id geometry limits rendezvous_port files entrypoint",
        "request",
    )
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise ValueError("unsupported diagnostic request schema")
    if request["kind"] != "pulsar-diagnostic-request":
        raise ValueError("invalid diagnostic request kind")
    sha(request["topology_id"], "topology_id")
    sha(request["image_id"], "image_id", "sha256:")
    geometry = request["geometry"]
    keys(geometry, "nodes tp pp", "geometry")
    for field in ("nodes", "tp", "pp"):
        number(geometry[field], 1, 8, field)
    if geometry["nodes"] != geometry["tp"] * geometry["pp"]:
        raise ValueError("one GPU per node requires nodes = tp * pp")
    limits = request["limits"]
    keys(limits, "memory_bytes min_host_available_bytes timeout_seconds", "limits")
    number(limits["memory_bytes"], GIB, 64 * GIB, "memory_bytes")
    number(
        limits["min_host_available_bytes"],
        64 * GIB,
        128 * GIB,
        "min_host_available_bytes",
    )
    number(limits["timeout_seconds"], 10, 1800, "timeout_seconds")
    number(request["rendezvous_port"], 1024, 65535, "rendezvous_port")
    files = request["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= 32:
        raise ValueError("files: 1 to 32 flat Python files required")
    for name, value in files.items():
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-zA-Z][a-zA-Z0-9_]{0,100}\.py", name
        ):
            raise ValueError("payload names must be flat Python filenames")
        sha(value, "payload file hash")
    if request["entrypoint"] not in files:
        raise ValueError("entrypoint must name a payload file")
    return request


def read_regular(path, limit=MAX_PAYLOAD):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("regular bounded file required")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file exceeds size limit")
        return data


def load(request_file, payload_dir, expected_id=None):
    request = validate(json.loads(read_regular(request_file)))
    if expected_id is not None and digest(request) != expected_id:
        raise ValueError("request changed since review")
    directory = Path(payload_dir)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("payload directory must be a real directory")
    payload = {}
    total = 0
    for name, expected in request["files"].items():
        data = read_regular(directory / name)
        total += len(data)
        if total > MAX_PAYLOAD or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("payload size or hash differs from request")
        try:
            compile(data, name, "exec")
        except SyntaxError as exc:
            raise ValueError(f"payload does not compile: {name}") from exc
        payload[name] = base64.b64encode(data).decode("ascii")
    return request, payload


def verify_payload(request, payload):
    validate(request)
    if set(payload) != set(request["files"]):
        raise ValueError("payload file coverage differs")
    total = 0
    for name, encoded in payload.items():
        data = base64.b64decode(encoded, validate=True)
        total += len(data)
        if (
            total > MAX_PAYLOAD
            or hashlib.sha256(data).hexdigest() != request["files"][name]
        ):
            raise ValueError("payload hash or size differs")
    return payload


def validate_context(context):
    request = validate(context["request"])
    verify_payload(request, context["payload"])
    if context["request_id"] != digest(request):
        raise ValueError("request identity differs")
    sha(context["run_id"], "run_id")
    ranks = context["ranks"]
    count = request["geometry"]["nodes"]
    if len(ranks) != count or [r["rank"] for r in ranks] != list(range(count)):
        raise ValueError("incomplete or reordered rank coverage")
    if len({r["node_id"] for r in ranks}) != count:
        raise ValueError("duplicate node identity")
    for row in ranks:
        if not isinstance(row["node_id"], str) or not row["node_id"]:
            raise ValueError("missing node identity")
        for field in ("control_ip", "control_if", "hcas"):
            if not isinstance(row[field], str) or not row[field] or "\n" in row[field]:
                raise ValueError("incomplete confirmed fabric")
    number(context["rank"], 0, count - 1, "rank")
    if not isinstance(context.get("guard_files"), dict):
        raise ValueError("guard file hashes required")
    required = {
        "diagnostics/guard.py",
        "diagnostics/worker.py",
        "diagnostics/schema.py",
        "scripts/resource_sample.py",
        "serving_guard/runtime.py",
    }
    if (
        not required.issubset(context["guard_files"])
        or len(context["guard_files"]) > 256
    ):
        raise ValueError("incomplete guard file coverage")
    for name, value in context["guard_files"].items():
        if not re.fullmatch(
            r"(?:diagnostics|scripts|model_library|release_spec|serving_guard)/[A-Za-z_][A-Za-z_0-9]*\.py",
            name,
        ):
            raise ValueError("invalid guard filename")
        sha(value, "guard file hash")
    if not re.fullmatch(
        r"/dev/infiniband/uverbs[0-9]+", context.get("verbs_device", "")
    ):
        raise ValueError("invalid platform verbs device")
    return context


def labels(context):
    return {
        LABEL: context["run_id"],
        LABEL + ".request": context["request_id"],
        LABEL + ".rank": str(context["rank"]),
        LABEL + ".node": context["ranks"][context["rank"]]["node_id"],
    }


def owned(info, context):
    return info.get("Image") == context["request"]["image_id"] and all(
        info.get("Config", {}).get("Labels", {}).get(k) == v
        for k, v in labels(context).items()
    )
