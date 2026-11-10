#!/usr/bin/env python3
"""Read-only GB10/Docker/RDMA node probe used locally and over SSH."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
from typing import Any


def run(argv: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, proc.stdout.strip()


def load_json_command(argv: list[str]) -> Any:
    rc, output = run(argv)
    if rc != 0 or not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def ipv4_addresses() -> dict[str, list[str]]:
    records = load_json_command(["ip", "-j", "-4", "addr", "show"])
    result: dict[str, list[str]] = {}
    if not isinstance(records, list):
        return result
    for record in records:
        ifname = str(record.get("ifname") or "")
        if not ifname or ifname == "lo":
            continue
        values: list[str] = []
        for address in record.get("addr_info") or []:
            if address.get("family") != "inet":
                continue
            local = str(address.get("local") or "")
            prefix = address.get("prefixlen")
            if local and isinstance(prefix, int):
                values.append(f"{local}/{prefix}")
        if values:
            result[ifname] = sorted(set(values))
    return result


def default_control(addresses: dict[str, list[str]]) -> tuple[str, str]:
    routes = load_json_command(["ip", "-j", "-4", "route", "show", "default"])
    if not isinstance(routes, list):
        routes = []
    routes.sort(key=lambda row: int(row.get("metric") or 0))
    for route in routes:
        dev = str(route.get("dev") or "")
        preferred = str(route.get("prefsrc") or "")
        if not dev:
            continue
        if preferred:
            return dev, preferred
        cidrs = addresses.get(dev) or []
        if cidrs:
            return dev, cidrs[0].split("/", 1)[0]
    for dev, cidrs in sorted(addresses.items()):
        if cidrs:
            return dev, cidrs[0].split("/", 1)[0]
    return "", ""


def rdma_links(addresses: dict[str, list[str]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    records = load_json_command(["rdma", "-j", "link", "show"])
    if isinstance(records, dict):
        records = records.get("link") or records.get("links") or []
    if isinstance(records, list):
        for record in records:
            state = str(record.get("state") or "").upper()
            if state != "ACTIVE":
                continue
            link_name = str(
                record.get("ifname")
                or record.get("name")
                or record.get("link")
                or ""
            )
            hca = link_name.split("/", 1)[0]
            netdev = str(record.get("netdev") or "")
            if hca and netdev:
                links.append(
                    {
                        "hca": hca,
                        "netdev": netdev,
                        "cidrs": addresses.get(netdev) or [],
                    }
                )

    if not links:
        _, output = run(["rdma", "link", "show"])
        pattern = re.compile(
            r"(?:^|\n)link\s+(\S+?)(?:/\d+)?\s+state\s+ACTIVE\b"
            r".*?\bnetdev\s+(\S+)",
            re.IGNORECASE,
        )
        for match in pattern.finditer(output):
            hca = match.group(1).split("/", 1)[0]
            netdev = match.group(2)
            links.append(
                {
                    "hca": hca,
                    "netdev": netdev,
                    "cidrs": addresses.get(netdev) or [],
                }
            )

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for link in links:
        key = (link["hca"], link["netdev"])
        unique[key] = link
    return [unique[key] for key in sorted(unique)]


def ssh_host_public_keys() -> list[dict[str, str]]:
    keys: list[dict[str, str]] = []
    for path in sorted(pathlib.Path("/etc/ssh").glob("ssh_host_*_key.pub")):
        try:
            fields = path.read_text(encoding="utf-8").strip().split()
        except OSError:
            continue
        if len(fields) < 2:
            continue
        algorithm, public_key = fields[:2]
        keys.append(
            {
                "algorithm": algorithm,
                "public_key": public_key,
            }
        )
    return keys


def node_identifier(hostname: str, control_ip: str) -> str:
    seed = ""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            seed = pathlib.Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if seed:
            break
    if not seed:
        seed = f"{hostname}\0{control_ip}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


# The node's operator prerequisites beyond the platform: the tools the
# installation table lists and the `hf` CLI that Download runs on this node.
# Only `pulsar doctor`, the menu's Check the cluster now (through doctor) and
# guided setup's steps 1 and 3 ask for them (--prerequisites-only). They never feed
# `qualified` or `reject_reasons`, so a node without them can still be a
# member and serve; and the Hub request never runs in discovery, `topology
# check` or start's probes.
MINIMUM_PYTHON = (3, 11)
HUB_TIMEOUT_SECONDS = 10

# Runs under the `hf` installation's own Python, as Download's source
# inventory does, with the login user's own Hub authentication. It prints
# state words only: never the token, and never the account name `whoami`
# returns. Compatible with the older Pythons an `hf` install may use.
HF_CLASSIFIER = r"""
import json, signal, sys
result = {"version": "", "download_ready": False, "login": None}
try:
    import huggingface_hub as hub
except Exception:
    print(json.dumps(dict(result, importable=False)))
    raise SystemExit(0)
result["importable"] = True
result["version"] = str(getattr(hub, "__version__", "") or "")[:40]
try:
    import inspect
    from huggingface_hub import HfApi
    from huggingface_hub.hf_api import RepoFile  # noqa: F401  Download lists RepoFile rows
    parameters = inspect.signature(HfApi.model_info).parameters
    tree = inspect.signature(HfApi.list_repo_tree).parameters
    result["download_ready"] = "expand" in parameters and "expand" in tree
except Exception:
    HfApi = None
token = None
try:
    from huggingface_hub import get_token
    token = get_token()
except Exception:
    token = None
if not token:
    result["login"] = "missing"
elif HfApi is None:
    result["login"] = "not-reached"
else:
    def expired(*_):
        raise TimeoutError("the Hub did not answer in time")
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(int(sys.argv[1]))
    try:
        HfApi().whoami(token=token)
        result["login"] = "accepted"
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        result["login"] = "rejected" if status == 401 else "not-reached"
    finally:
        signal.alarm(0)
print(json.dumps(result))
"""


def find_hf_cli() -> str:
    """Find `hf` exactly as Download does: `command -v hf`, else the
    `$HOME/.hf-cli/venv/bin/hf` installation (scripts/acquire-source.sh)."""
    found = shutil.which("hf")
    if found:
        return found
    fallback = pathlib.Path(os.environ.get("HOME", "")) / ".hf-cli/venv/bin/hf"
    if os.environ.get("HOME") and fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    return ""


def hf_python(cli: str) -> str:
    """The Python that owns CLI, resolved from its first line as Download's
    source inventory resolves it; empty when it cannot be identified."""
    try:
        resolved = os.path.realpath(cli)
        with open(resolved, "rb") as stream:
            first = stream.readline(4096).decode("utf-8", "replace").rstrip("\n")
    except OSError:
        return ""
    if first == "#!/usr/bin/env python3":
        python = shutil.which("python3") or ""
    elif first.startswith("#!/"):
        python = first[2:]
    else:
        return ""
    if not python or any(character.isspace() for character in python):
        return ""
    name = os.path.basename(python)
    if not (name in ("python", "python3") or name.startswith("python3.")):
        return ""
    return python if os.path.isfile(python) and os.access(python, os.X_OK) else ""


def hf_prerequisite(timeout: int = HUB_TIMEOUT_SECONDS) -> dict[str, Any]:
    """The `hf` CLI's state and its Hub login, as state words.

    state: ok, missing, unusable (its Python cannot be identified or cannot
    import the Hub library), too-old (it lacks what Download uses) or
    not-checked (the check did not finish). login: accepted, missing (no
    login on this node), rejected (the Hub refused it), not-reached (the Hub
    could not be reached) or None when `hf` itself is not usable.
    """
    cli = find_hf_cli()
    if not cli:
        return {"state": "missing", "version": "", "login": None,
                "legacy_cli": bool(shutil.which("huggingface-cli"))}
    python = hf_python(cli)
    if not python:
        return {"state": "unusable", "version": "", "login": None, "legacy_cli": False}
    environment = dict(os.environ, HF_HUB_OFFLINE="0", PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(
            [python, "-c", HF_CLASSIFIER, str(timeout)],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=timeout + 15, env=environment, cwd="/",
        )
        lines = proc.stdout.strip().splitlines()
        answer = json.loads(lines[-1]) if proc.returncode == 0 and lines else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {"state": "not-checked", "version": "", "login": None, "legacy_cli": False}
    if not isinstance(answer, dict):
        return {"state": "not-checked", "version": "", "login": None, "legacy_cli": False}
    version = answer.get("version") if isinstance(answer.get("version"), str) else ""
    if answer.get("importable") is not True:
        return {"state": "unusable", "version": "", "login": None, "legacy_cli": False}
    login = answer.get("login")
    if login not in ("accepted", "missing", "rejected", "not-reached"):
        login = "not-reached"
    state = "ok" if answer.get("download_ready") is True else "too-old"
    return {"state": state, "version": version, "login": login, "legacy_cli": False}


def prerequisites() -> dict[str, Any]:
    version = sys.version_info[:3]
    return {
        "python": {"version": ".".join(map(str, version)),
                   "ok": tuple(version[:2]) >= MINIMUM_PYTHON},
        "flock": bool(shutil.which("flock")),
        "rsync": bool(shutil.which("rsync")),
        "hf": hf_prerequisite(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssh-host", default="local")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--include-ssh-host-keys", action="store_true")
    parser.add_argument("--identity-only", action="store_true")
    parser.add_argument("--prerequisites-only", action="store_true")
    parser.add_argument("--expected-gpu", default="NVIDIA GB10")
    parser.add_argument("--expected-arch", action="append", default=None)
    parser.add_argument("--min-active-rdma", type=int, default=1)
    args = parser.parse_args()
    expected_arches = args.expected_arch if args.expected_arch else [
        "aarch64",
        "arm64",
    ]
    if args.min_active_rdma < 0:
        raise SystemExit("probe-node: --min-active-rdma must be >= 0")

    hostname = socket.gethostname().split(".", 1)[0]
    if args.prerequisites_only:
        # A separate answer: no hardware facts, no `qualified`, nothing
        # discovery or membership reads.
        print(json.dumps({
            "probe_schema_version": 1,
            "kind": "pulsar-node-prerequisites",
            "local": bool(args.local),
            "ssh_host": args.ssh_host,
            "hostname": hostname,
            "prerequisites": prerequisites(),
        }, sort_keys=True))
        return 0
    if args.identity_only:
        result = {
            "probe_schema_version": 2 if args.include_ssh_host_keys else 1,
            "local": bool(args.local),
            "ssh_host": args.ssh_host,
            "node_id": node_identifier(hostname, ""),
            "hostname": hostname,
        }
        if args.include_ssh_host_keys:
            result["ssh_host_keys"] = ssh_host_public_keys()
        print(json.dumps(result, sort_keys=True))
        return 0

    arch = run(["uname", "-m"])[1]
    gpu = run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]
    )[1].splitlines()
    gpu_name = gpu[0].strip() if gpu else ""

    docker_rc, docker_info = run(["docker", "info"], timeout=12)
    docker_ok = docker_rc == 0
    runtime_rc, runtimes = run(
        [
            "docker",
            "info",
            "--format",
            "{{range $k,$v := .Runtimes}}{{$k}} {{end}}",
        ],
        timeout=12,
    )
    docker_nvidia = (
        runtime_rc == 0
        and "nvidia" in runtimes.split()
        or "nvidia.com/gpu" in docker_info
    )

    addresses = ipv4_addresses()
    control_if, control_ip = default_control(addresses)
    rdma = rdma_links(addresses)

    reasons: list[str] = []
    if arch not in expected_arches:
        reasons.append(
            f"arch is {arch or 'unknown'}, expected {expected_arches[0]}"
        )
    if gpu_name != args.expected_gpu:
        reasons.append(
            f"GPU is {gpu_name or 'unavailable'}, expected {args.expected_gpu}"
        )
    if not docker_ok:
        reasons.append("Docker daemon unavailable")
    elif not docker_nvidia:
        reasons.append("Docker NVIDIA runtime/CDI unavailable")
    if not control_if or not control_ip:
        reasons.append("no IPv4 control-plane address")
    if len(rdma) < args.min_active_rdma:
        if not rdma:
            reasons.append("no active RDMA links")
        else:
            reasons.append(
                f"{len(rdma)} active RDMA links, expected at least "
                f"{args.min_active_rdma}"
            )
    elif rdma and not any(link.get("cidrs") for link in rdma):
        reasons.append("active RDMA links have no IPv4 addresses")

    result = {
        "probe_schema_version": 2 if args.include_ssh_host_keys else 1,
        "local": bool(args.local),
        "ssh_host": args.ssh_host,
        "node_id": node_identifier(hostname, control_ip),
        "hostname": hostname,
        "arch": arch,
        "gpu": gpu_name,
        "docker_ok": docker_ok,
        "docker_nvidia": bool(docker_nvidia),
        "control": {"interface": control_if, "ip": control_ip},
        "ipv4": addresses,
        "rdma": rdma,
        "qualified": not reasons,
        "reject_reasons": reasons,
    }
    if args.include_ssh_host_keys:
        result["ssh_host_keys"] = ssh_host_public_keys()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
