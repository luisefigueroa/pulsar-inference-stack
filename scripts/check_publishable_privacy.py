#!/usr/bin/env python3
"""Reject sensitive site identity and credentials in publishable repository files."""

from __future__ import annotations

import argparse
import ipaddress
import json
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable


PUBLISHABLE_PREFIXES = (
    "results/",
    "bench/results/",
    "releases/",
)
PUBLISHABLE_EXACT = {"README.md", "SECURITY.md"}
REDACTED_VALUES = {
    "",
    "redacted",
    "<redacted>",
    "omitted",
    "<omitted>",
    "private",
    "<private>",
    "not-published",
}
GENERIC_HOME_NAMES = {"operator", "user", "example", "runner"}

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"
)
SSH_PUBLIC_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:ssh-(?:rsa|ed25519)|ecdsa-sha2-nistp\d+)\s+"
    r"[A-Za-z0-9+/]{32,}={0,3}"
)
SSH_FINGERPRINT_RE = re.compile(r"\bSHA256:[A-Za-z0-9+/]{24,}={0,2}\b")
HASHED_KNOWN_HOST_RE = re.compile(r"(?m)^\|1\|[A-Za-z0-9+/=]{12,}\|[A-Za-z0-9+/=]{12,}")
SECRET_PATTERNS = (
    ("hugging-face-token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai-token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
)
STABLE_DGX_HOST_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9-])dgx-spark-\d+(?:\.[A-Za-z0-9.-]+)?"
)
LOCAL_HOST_RE = re.compile(
    r"(?i)\b[A-Za-z0-9][A-Za-z0-9.-]*\.(?:local|lan|internal|home|corp)\b"
)
HOME_PATH_RE = re.compile(r"(?P<prefix>/(?:home|Users)/)(?P<name>[A-Za-z0-9._-]+)(?=/)")
IPV4_RE = re.compile(r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.])")
IPV6_RE = re.compile(
    r"(?<![A-Fa-f0-9:])(?:[A-Fa-f0-9]{0,4}:){2,7}[A-Fa-f0-9]{0,4}"
    r"(?![A-Fa-f0-9:])"
)
MAC_ADDRESS_RE = re.compile(r"(?i)\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b")
GPU_UUID_RE = re.compile(r"(?i)\bGPU-[0-9a-f]{8,}(?:-[0-9a-f-]{4,})+\b")
NCCL_HOST_RE = re.compile(
    r"(?i)\bon\s+(?!Node-[A-Z]\b)([A-Za-z0-9][A-Za-z0-9._-]*)\s+device\b"
)
IDENTITY_VALUE_RE = re.compile(
    r"(?im)^\s*(?:[#;]\s*)?[\"\x27]?"
    r"(?:host(?:name)?|ssh[_ -]?(?:host|alias)|node[_ -]?id|"
    r"topology[_ -]?id)[\"\x27]?\s*[:=]\s*"
    r"[\"\x27]?([^\s,\"\x27]+)"
)
NETWORK_CONTEXT_RE = re.compile(
    r"(?i)(?:\bip\b|address|addr|host(?:name)?|endpoint|control|roce|ssh|"
    r"known_hosts|hostkey|interface)"
)
NETWORK_SCHEME_RE = re.compile(r"(?i)(?:https?|ssh)://")


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    location: str
    rule: str
    message: str


class PrivacyError(RuntimeError):
    pass


def _git(repo_root: pathlib.Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise PrivacyError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout


def _repo_root(value: str | None) -> pathlib.Path:
    if value:
        root = pathlib.Path(value).resolve()
    else:
        root = pathlib.Path(
            _git(pathlib.Path.cwd(), "rev-parse", "--show-toplevel")
            .decode("utf-8")
            .strip()
        ).resolve()
    if not (root / ".git").exists():
        raise PrivacyError(f"not a Git worktree: {root}")
    return root


def is_publishable_path(relative: str) -> bool:
    normalized = relative.replace("\\", "/").lstrip("./")
    if normalized in PUBLISHABLE_EXACT or normalized.endswith(".md"):
        return True
    return any(normalized.startswith(prefix) for prefix in PUBLISHABLE_PREFIXES)


def _working_tree_files(repo_root: pathlib.Path) -> list[tuple[str, bytes]]:
    tracked = _git(repo_root, "ls-files", "-z", "--cached")
    untracked = _git(repo_root, "ls-files", "-z", "--others", "--exclude-standard")
    files: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for item in tracked.split(b"\0"):
        if not item:
            continue
        relative = item.decode("utf-8", errors="strict")
        path = repo_root / relative
        if path.is_symlink():
            files.append((relative, str(path.readlink()).encode("utf-8")))
            seen.add(relative)
        elif path.is_file():
            files.append((relative, path.read_bytes()))
            seen.add(relative)
    for item in untracked.split(b"\0"):
        if not item:
            continue
        relative = item.decode("utf-8", errors="strict")
        if relative in seen:
            continue
        path = repo_root / relative
        if path.is_symlink():
            files.append((relative, str(path.readlink()).encode("utf-8")))
        elif path.is_file():
            files.append((relative, path.read_bytes()))
    return files


def _staged_files(repo_root: pathlib.Path) -> list[tuple[str, bytes]]:
    raw = _git(
        repo_root,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        "-z",
    )
    files: list[tuple[str, bytes]] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        relative = item.decode("utf-8", errors="strict")
        files.append((relative, _git(repo_root, "show", f":{relative}")))
    return files


def _line_number(text: str, offset: int) -> str:
    return str(text.count("\n", 0, offset) + 1)


def _redacted(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in REDACTED_VALUES
    if isinstance(value, list):
        return bool(value) and all(_redacted(item) for item in value)
    return False


def _sensitive_key_reason(key: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if normalized.endswith("_omitted") or normalized.endswith("_redacted"):
        return None
    if normalized in {"topology_id", "topology_ids"} or normalized.endswith(
        "_topology_id"
    ):
        return "durable topology identifier"
    if normalized in {
        "node_id",
        "node_ids",
        "node_uuid",
        "machine_id",
        "host_id",
    } or normalized.endswith(("_node_id", "_machine_id", "_host_id")):
        return "durable node identifier"
    if normalized in {"host", "hosts", "hostname", "hostnames"} or "hostname" in normalized:
        return "hostname"
    if normalized in {
        "ip",
        "ips",
        "address",
        "addresses",
        "endpoint",
        "endpoints",
        "control_ip",
        "roce_ip",
        "head_ip",
        "worker_ip",
    } or normalized.endswith(("_ip", "_ips", "_address", "_addresses")):
        return "network address"
    if normalized.startswith("ssh_") and any(
        marker in normalized
        for marker in ("host", "alias", "key", "fingerprint", "identity", "known")
    ):
        return "SSH identity"
    if normalized in {
        "host_key",
        "host_keys",
        "host_key_fingerprint",
        "known_hosts",
        "identity_file",
    }:
        return "SSH identity"
    if normalized in {
        "interface",
        "interfaces",
        "interface_name",
        "interface_names",
        "hca",
        "hcas",
    } or normalized.endswith(("_interface", "_interface_name")):
        return "site interface identity"
    if normalized in {
        "filesystem_id",
        "fs_id",
        "inode",
        "st_dev",
        "device_inode",
    }:
        return "filesystem identity"
    if normalized in {
        "mac",
        "mac_address",
        "mac_addresses",
        "serial",
        "serial_number",
        "machine_serial",
        "gpu_uuid",
        "gpu_uuids",
        "bmc_address",
        "bmc_ip",
    }:
        return "durable hardware identity"
    return None


def _scan_json_keys(
    relative: str,
    value: Any,
    *,
    pointer: str = "$",
) -> list[Finding]:
    findings: list[Finding] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_pointer = f"{pointer}.{key}"
            reason = _sensitive_key_reason(str(key))
            if reason and not _redacted(child):
                findings.append(
                    Finding(
                        relative,
                        child_pointer,
                        "sensitive-json-field",
                        f"{reason} must be omitted or explicitly redacted",
                    )
                )
            findings.extend(
                _scan_json_keys(relative, child, pointer=child_pointer)
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(
                _scan_json_keys(relative, child, pointer=f"{pointer}[{index}]")
            )
    return findings


def _allowed_ip(token: str) -> bool:
    try:
        address = ipaddress.ip_address(token)
    except ValueError:
        return True
    if address.is_loopback or address.is_unspecified:
        return True
    documentation = (
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
        ipaddress.ip_network("2001:db8::/32"),
    )
    return any(address in network for network in documentation)


def _generic_identity_value(value: str) -> bool:
    normalized = value.strip().strip(chr(34) + chr(39)).lower()
    if normalized in REDACTED_VALUES:
        return True
    if normalized.startswith(("<", "$", "{")):
        return True
    if normalized in {
        "host",
        "hostname",
        "node",
        "node-a",
        "node-b",
        "node_id",
        "topology_id",
        "rank-0",
        "rank-1",
    }:
        return True
    return False


def _scan_text(
    relative: str,
    text: str,
    *,
    scan_all_ips_override: bool | None = None,
) -> list[Finding]:
    findings: set[Finding] = set()

    pattern_rules = (
        ("private-key", PRIVATE_KEY_RE, "private key material is publishable"),
        ("ssh-public-key", SSH_PUBLIC_KEY_RE, "SSH public key material is publishable"),
        ("ssh-fingerprint", SSH_FINGERPRINT_RE, "SSH host-key fingerprint is publishable"),
        ("hashed-known-host", HASHED_KNOWN_HOST_RE, "hashed known_hosts identity is publishable"),
        ("stable-hostname", STABLE_DGX_HOST_RE, "stable lab hostname is publishable"),
        ("local-hostname", LOCAL_HOST_RE, "site-local hostname is publishable"),
        ("mac-address", MAC_ADDRESS_RE, "MAC address is publishable"),
        ("gpu-uuid", GPU_UUID_RE, "GPU UUID is publishable"),
        ("runtime-hostname", NCCL_HOST_RE, "runtime hostname is publishable"),
    )
    for rule, pattern, message in pattern_rules:
        for match in pattern.finditer(text):
            findings.add(
                Finding(relative, _line_number(text, match.start()), rule, message)
            )

    for rule, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            findings.add(
                Finding(
                    relative,
                    _line_number(text, match.start()),
                    rule,
                    "credential-like token is publishable",
                )
            )

    for match in HOME_PATH_RE.finditer(text):
        name = match.group("name")
        if name.lower() not in GENERIC_HOME_NAMES and not name.startswith("<"):
            findings.add(
                Finding(
                    relative,
                    _line_number(text, match.start()),
                    "site-home-path",
                    "user-specific home path is publishable",
                )
            )

    for match in IDENTITY_VALUE_RE.finditer(text):
        value = match.group(1)
        if not _generic_identity_value(value):
            findings.add(
                Finding(
                    relative,
                    _line_number(text, match.start(1)),
                    "site-identity-value",
                    "site identity value is publishable",
                )
            )

    scan_all_ips = (
        pathlib.PurePosixPath(relative).suffix.lower() != ".json"
        if scan_all_ips_override is None
        else scan_all_ips_override
    )
    for line_number, line in enumerate(text.splitlines(), 1):
        has_network_context = bool(
            NETWORK_CONTEXT_RE.search(line) or NETWORK_SCHEME_RE.search(line)
        )
        if not (scan_all_ips or has_network_context):
            continue
        for match in (*IPV4_RE.finditer(line), *IPV6_RE.finditer(line)):
            token = match.group(0)
            if match.start() >= 2 and line[match.start() - 2 : match.start()] == "==":
                continue
            if not _allowed_ip(token):
                findings.add(
                    Finding(
                        relative,
                        str(line_number),
                        "network-address",
                        f"non-documentation IP address {token!r} is publishable",
                    )
                )
    return sorted(findings)


def _scan_json_string_values(
    relative: str,
    value: Any,
    *,
    pointer: str = "$",
) -> list[Finding]:
    findings: list[Finding] = []
    if isinstance(value, dict):
        for key, child in value.items():
            findings.extend(
                _scan_json_string_values(
                    relative,
                    child,
                    pointer=f"{pointer}.{key}",
                )
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(
                _scan_json_string_values(
                    relative,
                    child,
                    pointer=f"{pointer}[{index}]",
                )
            )
    elif isinstance(value, str):
        stripped = value.strip().strip("[]")
        try:
            address = ipaddress.ip_address(stripped)
        except ValueError:
            pass
        else:
            if not _allowed_ip(str(address)):
                findings.append(
                    Finding(
                        relative,
                        pointer,
                        "network-address",
                        f"non-documentation IP address {str(address)!r} is publishable",
                    )
                )
    return findings


def scan_bytes(relative: str, data: bytes) -> list[Finding]:
    findings: list[Finding] = []
    for match in STABLE_DGX_HOST_RE.finditer(relative):
        findings.append(
            Finding(relative, "path", "stable-hostname", "stable hostname appears in path")
        )
        break
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [
            Finding(
                relative,
                "file",
                "non-utf8-publishable",
                "publishable file must be UTF-8 text or receive an explicit reviewed format",
            )
        ]
    findings.extend(_scan_text(relative, text))
    if pathlib.PurePosixPath(relative).suffix.lower() == ".json":
        try:
            document = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            findings.append(
                Finding(
                    relative,
                    "file",
                    "invalid-json",
                    f"publishable JSON cannot be privacy-audited: {exc}",
                )
            )
        else:
            findings.extend(_scan_json_keys(relative, document))
            findings.extend(_scan_json_string_values(relative, document))
    return sorted(set(findings))


def scan_repository_bytes(relative: str, data: bytes) -> list[Finding]:
    path = pathlib.PurePosixPath(relative)
    private_roots = {".pulsar", ".model-library", ".weight-fabric", "experiments", "exports", ".ssh"}
    private_files = {".env", ".pulsar-overlay.json", ".cluster-topology.json",
                     ".cluster-ssh-config", ".cluster-known-hosts"}
    if path.parts and (path.parts[0] in private_roots or path.as_posix() in private_files
            or (path.name.startswith(".env.") and path.name not in {".env.example", ".env.sample"})):
        return [Finding(relative, "file", "private-operational-state",
                        "site configuration, raw experiments, exports and operational state must remain untracked")]
    if is_publishable_path(relative):
        return scan_bytes(relative, data)
    if relative.startswith(("scripts/testlib/", "scripts/testdata/")):
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    high_confidence = {
        "private-key",
        "ssh-public-key",
        "ssh-fingerprint",
        "hashed-known-host",
        "stable-hostname",
        "network-address",
        "mac-address",
        "gpu-uuid",
        "runtime-hostname",
        "site-home-path",
        *(rule for rule, _pattern in SECRET_PATTERNS),
    }
    return [
        finding
        for finding in _scan_text(relative, text, scan_all_ips_override=False)
        if finding.rule in high_confidence
    ]


def scan_files(files: Iterable[tuple[str, bytes]]) -> tuple[int, list[Finding]]:
    count = 0
    findings: list[Finding] = []
    for relative, data in sorted(files):
        count += 1
        findings.extend(scan_repository_bytes(relative, data))
    return count, sorted(set(findings))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reject hostnames, addresses, SSH identity, durable node/topology "
            "identity, user paths, and credential material in publishable files."
        )
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="scan the exact staged blobs instead of the working tree",
    )
    parser.add_argument(
        "--repo-root",
        help="repository root (defaults to the current Git worktree)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo_root = _repo_root(args.repo_root)
        mode = "staged" if args.staged else "working-tree"
        files = _staged_files(repo_root) if args.staged else _working_tree_files(repo_root)
        count, findings = scan_files(files)
    except (OSError, PrivacyError, UnicodeError) as exc:
        print(f"publishable privacy: ERROR: {exc}", file=sys.stderr)
        return 2

    if findings:
        print(
            f"publishable privacy: FAIL ({len(findings)} finding(s) in {mode} scan)",
            file=sys.stderr,
        )
        for finding in findings:
            print(
                f"  {finding.path}:{finding.location}: "
                f"[{finding.rule}] {finding.message}",
                file=sys.stderr,
            )
        return 1

    print(f"publishable privacy: OK ({count} file(s), {mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
