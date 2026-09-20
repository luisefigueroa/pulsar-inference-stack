"""Diagnostic Docker argv and exact effective-control validation; no execution."""
from __future__ import annotations

import re
from pathlib import Path
from release_spec.diagnostic import PROFILE, complete_stopped_state, emit_entrypoint, sha256_bytes, image_evidence, image_environment

HEX64 = re.compile(r"^[0-9a-f]{64}$")
ENTRYPOINT = "pulsar-diagnostic-entrypoint.sh"
PATH_VALUE = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def exact(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(exact(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(exact(a, b) for a, b in zip(actual, expected))
    return actual == expected


def container_id(inspect):
    return inspect.get("Id") if isinstance(inspect, dict) else None


def environment(plan):
    if plan["definition"]["schema_version"] == 2:
        _, config = image_evidence(plan["definition"])
        return {**image_environment(config), **plan["definition"]["environment"]}
    return {"HOME": "/tmp", "PATH": PATH_VALUE, **plan["definition"]["environment"]}


def expected_cmd(plan):
    return ["--noprofile", "--norc", "/pulsar-check/" + ENTRYPOINT]


def docker_create_argv(plan, *, name, input_dir, attempt_nonce, entrypoint_rel=ENTRYPOINT):
    if entrypoint_rel != ENTRYPOINT or any(c in input_dir for c in ",\n\r"):
        raise ValueError("unsupported input mount or entrypoint")
    overrides = plan["definition"]["environment"] if plan["definition"]["schema_version"] == 2 else environment(plan)
    env = [part for k, v in overrides.items() for part in ("--env", k + "=" + v)]
    labels = {"io.pulsar.diagnostic": "true", "io.pulsar.diagnostic.plan-id": plan["plan_id"],
              "io.pulsar.diagnostic.attempt-nonce": attempt_nonce, "io.pulsar.diagnostic.node-id": plan["node_id"]}
    return ["create", "--name", name, "--pull", "never", "--network", "none", "--runtime", "runc",
            "--read-only", "--gpus", "device=0", "--memory", str(PROFILE["memory_bytes"]),
            "--memory-swap", str(PROFILE["memory_swap_bytes"]), "--cpus", "2", "--pids-limit", "256",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--ipc", "private",
            "--shm-size", str(PROFILE["shm_size_bytes"]), "--tmpfs", "/tmp:" + PROFILE["tmpfs"]["/tmp"],
            "--restart", "no", "--no-healthcheck", "--stop-signal", "SIGTERM", "--stop-timeout", "-1", "--log-driver", "local", "--log-opt", "max-size=16m",
            "--log-opt", "max-file=1", "--log-opt", "compress=false", "--entrypoint", "/bin/bash", "--workdir", "/",
            *env, "--mount", f"type=bind,src={input_dir},dst=/pulsar-check,readonly,bind-propagation=rprivate",
            *[part for k, v in labels.items() for part in ("--label", k + "=" + v)],
            plan["definition"]["image_reference"], *expected_cmd(plan)]


def identity_problem(inspect, plan, claim, *, reconcile=False):
    if not isinstance(inspect, dict) or not isinstance(inspect.get("Id"), str) or not HEX64.fullmatch(inspect["Id"]):
        return "complete container ID missing"
    if not reconcile and inspect["Id"] != claim.get("container_id"):
        return "container identity mismatch"
    if inspect.get("Image") != plan["definition"]["image_id"]:
        return "image identity mismatch"
    if inspect.get("Name") != "/" + claim["intended_name"]:
        return "intended container name mismatch"
    labels = inspect.get("Config", {}).get("Labels")
    if not isinstance(labels, dict):
        return "ownership labels missing"
    expected = {"io.pulsar.diagnostic": "true", "io.pulsar.diagnostic.plan-id": plan["plan_id"],
                "io.pulsar.diagnostic.attempt-nonce": claim["attempt_nonce"], "io.pulsar.diagnostic.node-id": plan["node_id"]}
    if any(labels.get(k) != v for k, v in expected.items()):
        return "ownership labels mismatch"
    if any(k.startswith("io.pulsar.gb10.") for k in labels):
        return "serving ownership labels are forbidden"
    return None


def validate_created_container(inspect, plan, *, input_dir, expected_cid=None, attempt_nonce=None):
    if not isinstance(inspect, dict) or not isinstance(inspect.get("Id"), str) or not HEX64.fullmatch(inspect["Id"]):
        return "invalid container identity"
    if expected_cid and inspect["Id"] != expected_cid:
        return "container identity mismatch"
    if inspect.get("Image") != plan["definition"]["image_id"]:
        return "image identity mismatch"
    problem = complete_stopped_state(inspect.get("State"), start_consumed=False)
    if problem or inspect["State"]["Status"] != "created":
        return problem or "container was already started"
    host, config = inspect.get("HostConfig"), inspect.get("Config")
    if not isinstance(host, dict) or not isinstance(config, dict):
        return "incomplete effective controls"
    required = {"NetworkMode": "none", "Runtime": "runc", "ReadonlyRootfs": True,
                "Privileged": False, "AutoRemove": False, "NanoCpus": 2000000000,
                "Memory": 16 * 1024**3, "MemorySwap": 16 * 1024**3, "PidsLimit": 256,
                "CapDrop": ["ALL"], "IpcMode": "private", "ShmSize": 64 * 1024**2,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "LogConfig": {"Type": "local", "Config": {"max-size": "16m", "max-file": "1", "compress": "false"}}}
    for key, expected in required.items():
        if not exact(host.get(key), expected):
            return "effective control mismatch: " + key
    if host.get("SecurityOpt") not in (["no-new-privileges"], ["no-new-privileges:true"]):
        return "no-new-privileges must be exactly enabled"
    requests = host.get("DeviceRequests")
    if not exact(requests, [{"Driver": "", "Count": 0, "DeviceIDs": ["0"], "Capabilities": [["gpu"]], "Options": {}}]):
        return "GPU device request must select only device 0"
    neutral = {"CapAdd": None, "Devices": [], "DeviceCgroupRules": None, "Binds": None,
               "VolumesFrom": None, "Sysctls": None, "ExtraHosts": None, "PortBindings": {},
               "PublishAllPorts": False, "PidMode": "", "UTSMode": "", "UsernsMode": "",
               "CgroupnsMode": "private", "CgroupParent": "", "OomKillDisable": False,
               "CpuQuota": 0, "CpuPeriod": 0, "CpusetCpus": "", "CpusetMems": "",
               "StorageOpt": None, "Init": False}
    for key, expected in neutral.items():
        if key not in host or type(host[key]) is not type(expected) or host[key] != expected:
            return "unexpected or missing effective control: " + key
    # Docker may add neutral metadata across versions, but unknown controls never
    # acquire authority by omission. The adapter explicitly enumerates them.
    optional_neutral = {"ContainerIDFile": "", "VolumeDriver": "", "ConsoleSize": [0, 0],
        "Dns": None, "DnsOptions": None, "DnsSearch": None, "GroupAdd": None, "Links": None,
        "Cgroup": "", "OomScoreAdj": 0, "Isolation": "", "CpuShares": 0, "BlkioWeight": 0,
        "BlkioWeightDevice": [], "BlkioDeviceReadBps": [], "BlkioDeviceWriteBps": [],
        "BlkioDeviceReadIOps": [], "BlkioDeviceWriteIOps": [], "CpuRealtimePeriod": 0,
        "CpuRealtimeRuntime": 0, "MemoryReservation": 0, "MemorySwappiness": None,
        "Ulimits": None, "CpuCount": 0, "CpuPercent": 0, "IOMaximumIOps": 0, "IOMaximumBandwidth": 0}
    allowed = set(required) | set(neutral) | set(optional_neutral) | {"SecurityOpt", "DeviceRequests", "Tmpfs", "Mounts", "MaskedPaths", "ReadonlyPaths"}
    if set(host) - allowed:
        return "unsupported effective host control"
    for key, value in optional_neutral.items():
        if key in host and not exact(host[key], value):
            return "unexpected effective host control: " + key
    if "Mounts" in host:
        expected_mounts = [{"Type": "bind", "Source": str(Path(input_dir).resolve()), "Target": "/pulsar-check", "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"}}]
        if not exact(host["Mounts"], expected_mounts):
            return "unexpected host mount specification"
    for key, profile_key in (("MaskedPaths", "masked_paths"), ("ReadonlyPaths", "readonly_paths")):
        if not exact(host.get(key), PROFILE[profile_key]):
            return "unapproved or missing proc protection profile: " + key
    if host.get("Tmpfs") != {"/tmp": "rw,nosuid,nodev,exec,size=512m"}:
        return "tmpfs must be the exact bounded executable profile"
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list) or len(mounts) != 1 or not isinstance(mounts[0], dict):
        return "exactly one sealed input bind is required"
    mount = mounts[0]
    if set(mount) != {"Type", "Source", "Destination", "RW", "Propagation", "Mode"}:
        return "unexpected or missing bind mount fields"
    for key, expected in {"Type": "bind", "Source": str(Path(input_dir).resolve()), "Destination": "/pulsar-check", "RW": False, "Propagation": "rprivate"}.items():
        if type(mount.get(key)) is not type(expected) or mount[key] != expected:
            return "input mount source/type/access mismatch"
    if mount.get("Mode") not in ("", "ro"):
        return "unexpected bind mount options"
    if config.get("Entrypoint") != ["/bin/bash"] or config.get("Cmd") != expected_cmd(plan):
        return "entrypoint or command differs from compiled argv"
    if inspect.get("Path") != "/bin/bash" or inspect.get("Args") != expected_cmd(plan):
        return "actual execution path/argv differs from compiled command"
    if config.get("StopSignal") != "SIGTERM" or type(config.get("StopTimeout")) is not int or config["StopTimeout"] != -1:
        return "stop must request cooperative SIGTERM without daemon kill timeout"
    if config.get("WorkingDir") != "/" or config.get("User") != "":
        return "unexpected working directory or image user"
    env = config.get("Env")
    expected_env = [k + "=" + v for k, v in environment(plan).items()]
    if not isinstance(env, list) or len(env) != len(expected_env) or any(type(x) is not str for x in env) or sorted(env) != sorted(expected_env):
        return "effective environment differs from explicit plan"
    if config.get("Healthcheck") != {"Test": ["NONE"]} or config.get("Volumes") not in (None, {}):
        return "inherited healthcheck or volume is not permitted"
    labels = config.get("Labels", {})
    if labels.get("io.pulsar.diagnostic.plan-id") != plan["plan_id"] or labels.get("io.pulsar.diagnostic.node-id") != plan["node_id"] or labels.get("io.pulsar.diagnostic") != "true":
        return "diagnostic ownership mismatch"
    if attempt_nonce and labels.get("io.pulsar.diagnostic.attempt-nonce") != attempt_nonce:
        return "diagnostic nonce mismatch"
    return None


def write_entrypoint(definition, directory):
    text = emit_entrypoint(definition)
    path = directory / ENTRYPOINT
    path.write_text(text)
    path.chmod(0o555)
    return sha256_bytes(text.encode())
