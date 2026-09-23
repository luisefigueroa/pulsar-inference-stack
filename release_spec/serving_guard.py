"""Portable opt-in serving guard policy; absence preserves old recipe identity."""

import copy
import re
import unicodedata


def validate(value, container):
    fields = {"schema_version", "program_sha256", "entrypoint",
              "min_host_available_bytes", "startup_timeout_seconds", "timeout_seconds"}
    if not isinstance(value, dict):
        raise ValueError("guard fields are incomplete or unknown")
    version = value.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        raise ValueError("unsupported serving guard schema")
    if version == 2:
        fields.add("max_host_swap_growth_bytes")
    if set(value) != fields:
        raise ValueError("guard fields are incomplete or unknown")
    if version == 2 and (type(value["max_host_swap_growth_bytes"]) is not int
                         or not 0 <= value["max_host_swap_growth_bytes"] <= 256 * 1024**2):
        raise ValueError("guard limit out of range: max_host_swap_growth_bytes")
    if not isinstance(value["program_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", value["program_sha256"]):
        raise ValueError("guard program SHA-256 required")
    entrypoint = value["entrypoint"]
    if not isinstance(entrypoint, list) or not 1 <= len(entrypoint) <= 16 or any(
        not isinstance(x, str) or not x or len(x) > 1024 or any(unicodedata.category(c) == 'Cc' for c in x)
        for x in entrypoint
    ):
        raise ValueError("explicit pinned image entrypoint required")
    for key, low, high in (("min_host_available_bytes", 8 * 1024**3, 128 * 1024**3),
                           ("startup_timeout_seconds", 10, 14400),
                           ("timeout_seconds", 10, 21600)):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError("guard limit out of range: " + key)
    if value["startup_timeout_seconds"] > value["timeout_seconds"]:
        raise ValueError("startup limit exceeds whole-session limit")
    if not 1024**3 <= container["memory_limit_bytes"] <= 112 * 1024**3:
        raise ValueError("guard requires an explicit 1-112 GiB memory limit")
    if container["restart_policy"] != "no" or container["network_mode"] != "host":
        raise ValueError("guard requires host networking and no automatic restart")
    if container["healthcheck"] is not None:
        raise ValueError("guard owns startup health checks")
    return copy.deepcopy(value)
