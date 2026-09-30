"""Freeze guarded serving inputs and package existing confirmed-node transport."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sys
import zipfile

from model_library.verification_process import process_identity
from release_spec import serving
from release_spec.normalize import canonical_json_digest
from scripts.container_runtime import docker_argv, validate_plan
from serving_guard.program import digest, program

ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    """Publish a fresh invocation record; never replace previous evidence."""
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def read_regular(path, limit=64 * 1024**2):
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("regular bounded file required")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file exceeds size limit")
        return data


def initialize(spec_file, spec_id, output):
    spec = serving.load_spec(spec_file)
    if spec["spec_id"] != spec_id or "guard" not in spec["recipe"]["container"]:
        raise ValueError("exact guarded serving spec required")
    if spec["recipe"]["container"]["guard"]["program_sha256"] != digest(program()):
        raise ValueError("installed guard program differs from reviewed recipe")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    write(output / "spec.json", spec)
    code = output / "code"
    hashes = {}
    paths = [p for package in ("serving_guard", "model_library", "release_spec")
             for p in sorted((ROOT / package).glob("*.py"))]
    paths += [ROOT / "scripts/resource_sample.py", ROOT / "scripts/container_runtime.py"]
    for path in paths:
        relative = path.relative_to(ROOT)
        target = code / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        target.write_bytes(data)
        hashes[str(relative)] = hashlib.sha256(data).hexdigest()
    data = b'"""Frozen Stack serving helpers."""\n'
    (code / "scripts/__init__.py").write_bytes(data)
    hashes["scripts/__init__.py"] = hashlib.sha256(data).hexdigest()
    write(output / "code-hashes.json", hashes)


def activate(output, owner):
    spec = serving.load_spec(output / "spec.json")
    plan = validate_plan(json.loads(read_regular(output / "plan.json")))
    if plan["spec_id"] != spec["spec_id"] or plan["selected_spec_id"] != spec["spec_id"]:
        raise ValueError("preflight selected a different guarded recipe")
    if plan["guard_program"] != program():
        raise ValueError("guard implementation changed during preflight")
    plan["lifecycle_action"] = "start"
    plan["plan_id"] = canonical_json_digest({k: v for k, v in plan.items() if k != "plan_id"})
    validate_plan(plan)
    write(output / "active-plan.json", plan)
    write(output / "context.json", {"plan": plan, "ranks": plan["ranks"],
          "guard_files": json.loads((output / "code-hashes.json").read_text())})
    identity = process_identity(owner)
    if identity is None:
        raise ValueError("controller identity unavailable")
    write(output / "controller.json", {"run_id": plan["guard_run_id"], "owner": identity})


def bundle(output, phase, rank, *, local_head=False):
    record = json.loads(read_regular(output / "context.json"))
    plan = validate_plan(record["plan"])
    context = {"plan": plan, "rank": rank, "operation": phase,
               "ready_file": str(output / "ready.json") if rank == 0 and local_head else None}
    if phase == "execute":
        context["argv"] = docker_argv(plan, rank)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for relative, expected in record["guard_files"].items():
            data = (output / "code" / relative).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("frozen serving helpers changed")
            zipped.writestr(relative, data)
    encoded = base64.b64encode(archive.getvalue()).decode()
    argument = base64.b64encode(json.dumps(context).encode()).decode()
    return (
        "import base64,io,json,pathlib,sys,tempfile,zipfile\n"
        "with tempfile.TemporaryDirectory(prefix='pulsar-serving-guard-') as temp:\n"
        " root=pathlib.Path(temp)\n"
        f" zipfile.ZipFile(io.BytesIO(base64.b64decode({encoded!r}))).extractall(root)\n"
        " sys.path.insert(0,str(root))\n"
        " from serving_guard.node import main\n"
        " from model_library.verification_process import supervise_node\n"
        f" context=json.loads(base64.b64decode({argument!r}))\n"
        " raise SystemExit(supervise_node(lambda:main(context,root),_pulsar_control.fileno(),_pulsar_token))\n"
    )


def task(output, phase, rank, command, *, local_head=False):
    directory = output / phase
    (directory / "jobs").mkdir(parents=True, exist_ok=True)
    source = directory / f"{rank}.program"
    with source.open("x") as out:
        out.write(bundle(output, phase, rank, local_head=local_head))
    write(directory / f"{rank}.task.json",
          {"index": rank, "node_slot": rank, "program": str(source), "command": command})


def tasks(output, phase):
    count = len(json.loads(read_regular(output / "context.json"))["ranks"])
    directory = output / phase
    write(directory / "tasks.json", [json.loads(read_regular(directory / f"{rank}.task.json"))
                                     for rank in range(count)])


def check(output, phase):
    plan = json.loads((output / "active-plan.json").read_text())
    directory = output / phase
    batch = json.loads((directory / "batch.json").read_text())
    count = len(plan["ranks"])
    if batch["outcome"] != "complete" or batch["returncode"] != 0 or len(batch["results"]) != count:
        raise ValueError("incomplete guarded rank phase: " + phase)
    for rank, row in enumerate(batch["results"]):
        value = json.loads((directory / "jobs" / f"{rank}.out").read_text())
        if row["index"] != rank or row["returncode"] != 0 or any(
            value.get(k) != v for k, v in {"rank": rank, "run_id": plan["guard_run_id"], "spec_id": plan["spec_id"]}.items()
        ):
            raise ValueError("guarded rank identity or result differs")
        key = {"preflight": "ready", "execute": "stopped", "cleanup": "cleanup_verified"}[phase]
        if value.get(key) is not True:
            raise ValueError("guarded rank criteria failed: " + phase)
    return count


def stop(output, run_id):
    plan = validate_plan(json.loads(read_regular(output / "active-plan.json")))
    controller = json.loads(read_regular(output / "controller.json"))
    if plan["guard_run_id"] != run_id or controller["run_id"] != run_id:
        raise ValueError("guard invocation changed")
    if (output / "result.json").exists():
        raise ValueError("guarded session already completed")
    if process_identity(controller["owner"][0]) != controller["owner"]:
        raise ValueError("controller no longer alive; reconcile physical state")
    path = output / "stop-request.json"
    if not path.exists():
        write(path, {"run_id": run_id, "requested_stop": True})
    elif json.loads(read_regular(path)) != {"run_id": run_id, "requested_stop": True}:
        raise ValueError("stop request differs")
    os.kill(controller["owner"][0], signal.SIGTERM)
    return {"run_id": run_id, "stop_requested": True, "cleanup_complete": False}


def finish(output):
    plan = validate_plan(json.loads((output / "active-plan.json").read_text()))
    phases = {}
    for phase in ("preflight", "execute", "cleanup"):
        try:
            phases[phase] = {"complete": True, "ranks": check(output, phase)}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            phases[phase] = {"complete": False, "error": str(exc)}
    requested = False
    if (output / "stop-request.json").exists():
        requested = json.loads(read_regular(output / "stop-request.json")) == {
            "run_id": plan["guard_run_id"], "requested_stop": True}
    result = {"schema_version": 1, "kind": "pulsar-guarded-serving-result",
              "run_id": plan["guard_run_id"], "service_id": plan["service_id"],
              "spec_id": plan["spec_id"], "phases": phases, "requested_stop": requested,
              "status": "stopped" if requested and phases["cleanup"]["complete"] else "failed",
              "qualification": False}
    write(output / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["initialize", "activate", "task", "tasks", "check", "finish"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--spec-file")
    parser.add_argument("--spec-id")
    parser.add_argument("--owner", type=int)
    parser.add_argument("--phase")
    parser.add_argument("--rank", type=int)
    parser.add_argument("--local-head", action="store_true")
    argv = sys.argv[1:]
    command = []
    if "--" in argv:
        index = argv.index("--")
        command, argv = argv[index + 1:], argv[:index]
    args = parser.parse_args(argv)
    if args.operation == "initialize":
        initialize(args.spec_file, args.spec_id, args.output)
    elif args.operation == "activate":
        activate(args.output, args.owner)
    elif args.operation == "task":
        task(args.output, args.phase, args.rank, command, local_head=args.local_head)
    elif args.operation == "tasks":
        tasks(args.output, args.phase)
    elif args.operation == "check":
        check(args.output, args.phase)
    else:
        result = finish(args.output)
        print(json.dumps(result))
        return 0 if result["status"] == "stopped" else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
