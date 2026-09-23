"""Read-only image-filesystem discovery without assuming containerd shares Docker's root."""

from __future__ import annotations

import glob
import os
from pathlib import Path
import subprocess
import tomllib
import re

from diagnostics.schema import read_regular


def daemon_options(argv):
    """Accept daemon invocations only, never subcommands or unknown overrides."""
    if (
        not argv
        or not Path(argv[0]).is_absolute()
        or Path(argv[0]).name != "containerd"
    ):
        raise ValueError("unrecognized containerd executable")
    names = {
        "--config": "config",
        "-c": "config",
        "--root": "root",
        "--state": "state",
        "--address": "address",
        "-a": "address",
        "--log-level": "log-level",
        "-l": "log-level",
    }
    result = {"config": "/etc/containerd/config.toml"}
    index = 1
    while index < len(argv):
        flag, separator, value = argv[index].partition("=")
        if flag not in names:
            raise ValueError("containerd invocation is not a supported daemon")
        if not separator:
            index += 1
            if index >= len(argv):
                raise ValueError("missing containerd option value")
            value = argv[index]
        result[names[flag]] = value
        index += 1
    for name, value in result.items():
        if name != "log-level" and not Path(value).is_absolute():
            raise ValueError("relative containerd paths cannot be verified")
    return result


def unchanged(path, started):
    """Reject edits or replacements after this daemon started."""
    path = Path(path)
    info = path.lstat()
    if path.is_symlink() or max(info.st_mtime, info.st_ctime) > started:
        raise ValueError("containerd configuration changed after daemon startup")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def audit_config(config_path, config, started):
    pending = [str(config_path)]
    pending.extend(config.get("imports", []))
    records = {}
    seen = set()
    while pending:
        pattern = pending.pop()
        if not isinstance(pattern, str) or not Path(pattern).is_absolute():
            raise ValueError("absolute containerd imports required")
        if pattern in seen:
            continue
        seen.add(pattern)
        if len(seen) > 128:
            raise ValueError("too many containerd config imports")
        # Bound import expansion and reject directories modified since startup,
        # including deletion/addition of a matching import.
        parent = Path(pattern).parent
        if glob.has_magic(str(parent)):
            raise ValueError("wildcard containerd import directories are unsupported")
        while not parent.exists():
            parent = parent.parent
        records[str(parent)] = unchanged(parent, started)
        matches = []
        for match in glob.iglob(pattern):
            matches.append(Path(match))
            if len(matches) + len(records) > 256:
                raise ValueError("too many containerd config files")
        for path in matches:
            records[str(path)] = unchanged(path, started)
            value = tomllib.loads(read_regular(path, 1024**2).decode())
            pending.extend(value.get("imports", []))
    return records


def roots_from_config(config, options, address, docker_root, mountinfo=""):
    root = Path(options.get("root", config["root"]))
    actual_address = options.get("address", config.get("grpc", {}).get("address", ""))
    if not actual_address or Path(actual_address).resolve() != Path(address).resolve():
        raise ValueError("containerd daemon does not match Docker endpoint")
    plugins = config.get("plugins", {})
    overlay = plugins.get("io.containerd.snapshotter.v1.overlayfs", {})
    snapshot = Path(
        overlay.get("root_path") or root / "io.containerd.snapshotter.v1.overlayfs"
    )
    # Default plugin directories share the configured root filesystem. Querying
    # statvfs there requires no access to protected plugin contents. Explicit
    # plugin roots and separately mounted stores need their own measurements.
    roots = [Path(docker_root), root]
    if overlay.get("root_path"):
        roots.append(snapshot)
    stores = [root / "io.containerd.content.v1.content", snapshot]
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 6 or " - " not in line:
            raise ValueError("invalid filesystem mount observation")
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        if any(mount == path or path in mount.parents for path in stores):
            roots.append(mount)
    if config.get("temp"):
        roots.append(Path(config["temp"]))
    if not all(path.is_absolute() for path in roots):
        raise ValueError("absolute containerd storage paths required")
    return list(dict.fromkeys(roots))


def containerd_roots(info, proc=Path("/proc")):
    address = info.get("Containerd", {}).get("Address")
    if not isinstance(address, str) or not Path(address).is_absolute():
        raise ValueError("Docker did not identify its local containerd endpoint")
    boot = next(
        int(line.split()[1])
        for line in (proc / "stat").read_text().splitlines()
        if line.startswith("btime ")
    )
    candidates = []
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if (process / "comm").read_text().strip() != "containerd":
                continue
            command = (process / "cmdline").read_bytes()
            argv = command.decode().rstrip("\0").split("\0")
            options = daemon_options(argv)
            identity = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
            started = boot + int(identity) / os.sysconf("SC_CLK_TCK")
            binary = unchanged(argv[0], started)
            # `config dump` loads imports/defaults but does not apply root/address
            # daemon flags; apply those explicitly below. It never starts a daemon.
            result = subprocess.run(
                [argv[0], "--config=" + options["config"], "config", "dump"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            if len(result.stdout) > 1024**2:
                raise ValueError("containerd configuration is too large")
            config = tomllib.loads(result.stdout)
            records = audit_config(options["config"], config, started)
            roots = roots_from_config(
                config,
                options,
                address,
                info["DockerRootDir"],
                (proc / "self/mountinfo").read_text(),
            )
            if (process / "cmdline").read_bytes() != command or (
                process / "stat"
            ).read_text().rsplit(")", 1)[1].split()[19] != identity:
                raise ValueError("containerd process changed during observation")
            if unchanged(argv[0], started) != binary or any(
                unchanged(Path(path), started) != signature
                for path, signature in records.items()
            ):
                raise ValueError("containerd configuration changed during observation")
            candidates.append(roots)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            subprocess.SubprocessError,
        ):
            continue
    if len(candidates) != 1:
        raise ValueError(
            "cannot verify one unchanged containerd configuration for Docker"
        )
    return candidates[0]


def image_storage_roots(info):
    root = Path(info["DockerRootDir"])
    if not root.is_absolute():
        raise ValueError("Docker storage directory is not absolute")
    if info.get("Driver") == "overlay2":
        return [root]
    if info.get("Driver") == "overlayfs" and [
        "driver-type",
        "io.containerd.snapshotter.v1",
    ] in info.get("DriverStatus", []):
        return containerd_roots(info)
    raise ValueError(
        "Docker image storage layout requires a separately verified capacity check"
    )
