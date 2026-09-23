"""Image-ID staging observations and evidence; Bash owns the actual stream."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from diagnostics.schema import GIB, number, validate_context
from diagnostics.storage import image_storage_roots

TRANSFER_TIMEOUT_SECONDS = 1800


def disk_free(path):
    return shutil.disk_usage(path).free


def observe(context, docker, idle, read_meminfo):
    validate_context(context)
    # A successful list differentiates absence from an unavailable Docker daemon.
    info = json.loads(docker("info", "--format", "{{json .}}").stdout)
    if info.get("OSType") != "linux" or info.get("Architecture") not in (
        "aarch64",
        "arm64",
    ):
        raise RuntimeError("ARM64 Linux Docker daemon required")
    # Account for content/snapshot stores mounted separately from Docker's root.
    capacities = {str(path): disk_free(path) for path in image_storage_roots(info)}
    available = min(capacities.values())
    number(available, 0, 2**63 - 1, "Docker available bytes")
    image_id = context["request"]["image_id"]
    # save/load by immutable ID can leave an untagged image. Docker's default
    # list hides some such imports, even when exact-ID inspection succeeds.
    ids = docker("image", "ls", "--all", "--quiet", "--no-trunc").stdout.split()
    present = image_id in ids
    size = None
    if present:
        image = json.loads(docker("image", "inspect", image_id).stdout)[0]
        if (
            image["Id"] != image_id
            or image["Architecture"] != "arm64"
            or image["Os"] != "linux"
        ):
            raise RuntimeError("diagnostic image identity or platform differs")
        size = image["Size"]
        number(size, 1, 2**63 - 1, "image size")
    idle()
    memory = read_meminfo()
    if memory is None:
        raise RuntimeError("host memory observation unavailable")
    return {
        "kind": "pulsar-diagnostic-image-observation",
        "rank": context["rank"],
        "run_id": context["run_id"],
        "request_id": context["request_id"],
        "image_id": image_id,
        "present": present,
        "image_size_bytes": size,
        "docker_available_bytes": available,
        "storage_capacities": capacities,
        "host_available_bytes": memory["mem_available_bytes"],
        "idle": True,
    }


def validate_observation(value):
    if (
        value.get("kind") != "pulsar-diagnostic-image-observation"
        or type(value.get("present")) is not bool
    ):
        raise ValueError("invalid image observation")
    if value.get("idle") is not True:
        raise ValueError("node idleness unconfirmed")
    for name in ("docker_available_bytes", "host_available_bytes"):
        number(value[name], 0, 2**63 - 1, name)
    if value["present"]:
        number(value["image_size_bytes"], 1, 2**63 - 1, "image size")
    elif value["image_size_bytes"] is not None:
        raise ValueError("missing image must not report a size")


def make_plan(context, rows):
    validate_context(context)
    source = rows[0]
    missing = [row["rank"] for row in rows if not row["present"]]
    blockers = []
    if not source["present"]:
        blockers.append({"rank": 0, "reason": "controller lacks the exact local image"})
    size = source["image_size_bytes"]
    # Docker import may use temporary plus extracted layers. This conservative
    # estimate is a preflight, not a promise against external disk consumers.
    required = 2 * size + GIB if size is not None else None
    floor = context["request"]["limits"]["min_host_available_bytes"]
    for row in rows:
        validate_observation(row)
        if row["host_available_bytes"] < floor:
            blockers.append(
                {"rank": row["rank"], "reason": "insufficient available host memory"}
            )
        if (
            not row["present"]
            and required is not None
            and row["docker_available_bytes"] < required
        ):
            blockers.append(
                {"rank": row["rank"], "reason": "insufficient Docker storage space"}
            )
        if row["present"] and size is not None and row["image_size_bytes"] != size:
            blockers.append(
                {"rank": row["rank"], "reason": "image size observation differs"}
            )
    return {
        "schema_version": 1,
        "kind": "pulsar-diagnostic-image-plan",
        "request_id": context["request_id"],
        "run_id": context["run_id"],
        "image_id": context["request"]["image_id"],
        "source_rank": 0,
        "missing_ranks": missing,
        "image_size_bytes": size,
        "required_available_bytes_per_receiver": required,
        "transfer_timeout_seconds_per_rank": TRANSFER_TIMEOUT_SECONDS,
        "ready": not blockers,
        "blockers": blockers,
        # Full filesystem observations remain in the private per-node records.
        "observations": [
            {key: value for key, value in row.items() if key != "storage_capacities"}
            for row in rows
        ],
        "image_transfer_performed": False,
        "gpu_execution": False,
    }


def create_plan(output, phase, filename):
    from diagnostics.controller import phase_results, write

    context = json.loads((output / "context.json").read_text())
    result = make_plan(context, phase_results(output, phase))
    write(output / filename, result)
    return result


def targets(output):
    plan = json.loads((output / "image-plan.json").read_text())
    if not plan["ready"]:
        raise ValueError("image staging is blocked by the retained plan")
    return plan["missing_ranks"]


def recheck(output, rank):
    plan = create_plan(output, "image-check-" + str(rank), f"image-check-{rank}.json")
    original = json.loads((output / "image-plan.json").read_text())
    if not plan["ready"] or plan["image_size_bytes"] != original["image_size_bytes"]:
        raise ValueError("image staging readiness changed before transfer")
    if not set(plan["missing_ranks"]).issubset(original["missing_ranks"]):
        raise ValueError("new missing rank requires a fresh staging plan")
    return rank in plan["missing_ranks"]


def transfer_record(output, rank, returncode, state):
    from diagnostics.controller import write

    number(rank, 1, 7, "receiver rank")
    if rank not in targets(output):
        raise ValueError("receiver is not in the staging plan")
    if state == "finished":
        number(returncode, 0, 255, "transfer return code")
    context = json.loads((output / "context.json").read_text())
    write(
        output / f"transfer-{rank}.{state}.json",
        {
            "rank": rank,
            "returncode": returncode,
            "state": state,
            "run_id": context["run_id"],
            "request_id": context["request_id"],
            "streamed": state == "started"
            or (output / f"transfer-{rank}.started.json").exists(),
        },
    )


def finish(output):
    from diagnostics.controller import phase_results, write

    context = json.loads((output / "context.json").read_text())
    result = {
        "schema_version": 1,
        "kind": "pulsar-diagnostic-image-result",
        "request_id": context["request_id"],
        "run_id": context["run_id"],
        "image_id": context["request"]["image_id"],
        "successful": False,
        "gpu_execution": False,
        "qualification": False,
        "transfers": [],
    }
    try:
        if not (output / "image-plan.json").is_file():
            raise ValueError(
                "image staging preflight did not complete; inspect retained image-before evidence"
            )
        plan = json.loads((output / "image-plan.json").read_text())
        if not plan["ready"]:
            raise ValueError("image staging preflight was blocked")
        for rank in plan["missing_ranks"]:
            record = json.loads((output / f"transfer-{rank}.finished.json").read_text())
            result["transfers"].append(record)
            if (
                record.get("rank") != rank
                or record.get("run_id") != context["run_id"]
                or record.get("request_id") != context["request_id"]
                or record.get("state") != "finished"
                or type(record.get("returncode")) is not int
                or record["returncode"] != 0
            ):
                raise ValueError("image transfer failed; no automatic retry or pull")
        observations = phase_results(output, "image-after")
        if not all(row["present"] for row in observations):
            raise ValueError("exact image is not present on every confirmed rank")
        if any(
            row["image_size_bytes"] != plan["image_size_bytes"] for row in observations
        ):
            raise ValueError("post-transfer image size differs")
        result.update(
            successful=True, verified_ranks=[row["rank"] for row in observations]
        )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result["error"] = str(exc)
    result["transfer_attempted_ranks"] = [
        rank
        for rank in range(len(context["ranks"]))
        if (output / f"transfer-{rank}.started.json").exists()
    ]
    write(output / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=["plan", "targets", "recheck", "started", "finished", "finish"],
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rank", type=int)
    parser.add_argument("--returncode", type=int)
    args = parser.parse_args()
    if args.operation == "plan":
        print(json.dumps(create_plan(args.output, "image-before", "image-plan.json")))
    elif args.operation == "targets":
        for rank in targets(args.output):
            print(rank)
    elif args.operation == "recheck":
        print("missing" if recheck(args.output, args.rank) else "present")
    elif args.operation in ("started", "finished"):
        transfer_record(args.output, args.rank, args.returncode, args.operation)
    else:
        result = finish(args.output)
        print(json.dumps(result))
        return 0 if result["successful"] else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
