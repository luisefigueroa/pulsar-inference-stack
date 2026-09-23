#!/usr/bin/env python3
"""Synthetic Docker process for diagnostic transport tests; touches no hardware."""

import json
import os
from pathlib import Path
import signal
import sys
import time

args = sys.argv[1:]
rank = int(os.environ.get("PULSAR_TEST_NODE", "0"))
root = Path(os.environ["PULSAR_TEST_DOCKER_STATE"])
state = root / f"{rank}.json"
image = "sha256:" + "a" * 64


def values(flag):
    return [args[i + 1] for i, word in enumerate(args) if word == flag]


def image_present():
    return (
        str(rank)
        not in os.environ.get("PULSAR_TEST_MISSING_IMAGE_RANKS", "").split(",")
        or (root / f"loaded-{rank}").exists()
    )


if args[:1] == ["info"]:
    if str(rank) == os.environ.get("PULSAR_TEST_DOCKER_UNAVAILABLE_RANK"):
        raise SystemExit(1)
    print(
        json.dumps(
            {
                "OSType": "linux",
                "Architecture": "aarch64",
                "DockerRootDir": str(root),
                "Driver": os.environ.get("PULSAR_TEST_STORAGE_DRIVER", "overlay2"),
                "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]]
                if os.environ.get("PULSAR_TEST_STORAGE_DRIVER") == "overlayfs"
                else [],
                "Containerd": {"Address": "/run/fixture-containerd.sock"},
            }
        )
    )
elif args[:2] == ["image", "ls"]:
    hidden_import = (
        os.environ.get("PULSAR_TEST_HIDE_UNTAGGED_IMPORTS")
        and (root / f"loaded-{rank}").exists()
        and "--all" not in args
    )
    if image_present() and not hidden_import:
        print(image)
elif args[:2] == ["image", "inspect"]:
    if not image_present():
        raise SystemExit(1)
    if str(rank) == os.environ.get("PULSAR_TEST_WRONG_IMAGE_RANK"):
        image = "sha256:" + "f" * 64
    if (root / f"loaded-{rank}").exists() and str(rank) == os.environ.get(
        "PULSAR_TEST_WRONG_LOADED_IMAGE_RANK"
    ):
        image = "sha256:" + "f" * 64
    print(
        json.dumps(
            [{"Id": image, "Architecture": "arm64", "Os": "linux", "Size": 1024**3}]
        )
    )
elif args[:1] in (["save"], ["load"], ["pull"]):
    with (root / "mutations").open("a") as stream:
        stream.write(args[0] + " " + str(rank) + "\n")
    if args[0] == "pull":
        raise SystemExit("pull fallback is forbidden")
    if os.environ.get("PULSAR_TEST_SLOW_STREAM"):
        identity = [
            os.getpid(),
            Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19],
        ]
        (root / f"client-{args[0]}-{rank}.json").write_text(
            json.dumps({"identity": identity, "pgid": os.getpgrp()})
        )
    if args[0] == "save":
        if os.environ.get("PULSAR_TEST_SOURCE_FAIL"):
            raise SystemExit(1)
        print("synthetic image bytes", flush=True)
        if os.environ.get("PULSAR_TEST_SLOW_STREAM"):
            time.sleep(60)
    else:
        data = sys.stdin.read()
        if str(rank) == os.environ.get("PULSAR_TEST_LOAD_FAIL_RANK") or not data:
            raise SystemExit(1)
        if str(rank) == os.environ.get("PULSAR_TEST_LOAD_NO_IMAGE_RANK"):
            raise SystemExit(0)
        (root / f"loaded-{rank}").touch()
elif args[:2] == ["container", "ls"] or args[:1] == ["ps"]:
    if state.exists():
        print("fixture-container-" + str(rank))
elif args[:2] == ["container", "inspect"]:
    print(json.dumps([json.loads(state.read_text())]))
elif args[:1] == ["rm"]:
    if state.exists():
        data = json.loads(state.read_text())
        try:
            os.kill(data["State"]["Pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        state.unlink(missing_ok=True)
elif args[:1] == ["run"]:
    mounts = []
    for value in values("--mount"):
        pieces = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
        mounts.append(
            {"Source": pieces["src"], "Destination": pieces["dst"], "RW": False}
        )
    payload_root = Path(mounts[0]["Source"])
    context = json.loads((payload_root / "context.json").read_text())
    info = {
        "Id": "fixture-container-" + str(rank),
        "Image": image,
        "Config": {
            "Labels": dict(value.split("=", 1) for value in values("--label")),
            "Env": values("-e"),
            "WorkingDir": values("--workdir")[0],
            "Entrypoint": ["python3"],
            "Cmd": ["-m", "diagnostics.guard"],
        },
        "HostConfig": {
            "Memory": int(values("--memory")[0]),
            "MemorySwap": int(values("--memory-swap")[0]),
            "NetworkMode": "host",
            "RestartPolicy": {"Name": "no"},
            "AutoRemove": True,
            "DeviceRequests": [{"DeviceIDs": ["0"]}],
            "Devices": [
                {"PathOnHost": path, "PathInContainer": path}
                for path in values("--device")
            ],
            "NanoCpus": 4_000_000_000,
            "PidsLimit": 512,
            "ShmSize": 1024**3,
        },
        "Mounts": mounts,
        "State": {"Running": True, "Pid": os.getpid()},
    }
    state.write_text(json.dumps(info))
    (root / f"{rank}.started").touch()

    def stop(_signum, _frame):
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            control = os.read(0, 1)
            if not control:
                raise SystemExit(3)
            if control == b"G":
                break
        if str(rank) == os.environ.get("PULSAR_TEST_HANG_RANK") or os.environ.get(
            "PULSAR_TEST_HANG_ALL"
        ):
            time.sleep(60)
        success = str(rank) != os.environ.get("PULSAR_TEST_FAIL_RANK")
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "pulsar-diagnostic-rank",
                    "rank": rank,
                    "run_id": context["run_id"],
                    "request_id": context["request_id"],
                    "image_id": image,
                    "successful": success,
                    "log_tail": "\x00" * 65536
                    if os.environ.get("PULSAR_TEST_ESCAPED_LOG")
                    else "",
                }
            ),
            flush=True,
        )
        raise SystemExit(0 if success else 3)
    finally:
        state.unlink(missing_ok=True)
else:
    raise SystemExit("unsupported fake Docker operation: " + repr(args))
