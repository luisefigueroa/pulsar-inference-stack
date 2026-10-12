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


def read_regular(path, limit=64 * 1024**2, *, dir_fd=None):
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
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
    if spec["spec_id"] != spec_id:
        raise ValueError(f"--spec-id {spec_id[:12]} is not the ID of {spec_file} ({spec['spec_id'][:12]}); "
                         "pass the reviewed spec's complete ID")
    if "guard" not in spec["recipe"]["container"]:
        raise ValueError(f"{spec_file} has no serving guard (recipe.container.guard); "
                         "serve it with ./pulsar start")
    if spec["recipe"]["container"]["guard"]["program_sha256"] != digest(program()):
        raise ValueError("the installed guard program differs from the one this spec was reviewed with; "
                         "create a new candidate with ./pulsar guarded template (docs/GUARDED_SERVING.md)")
    try:
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        raise ValueError(f"output directory {output} already exists; each run needs a new directory") from None
    except FileNotFoundError:
        raise ValueError(f"the parent directory of {output} does not exist") from None
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
        raise ValueError(f"guarded run {run_id[:12]} already finished; nothing to stop. Its outcome is in "
                         f"{output / 'result.json'}")
    if process_identity(controller["owner"][0]) != controller["owner"]:
        raise ValueError(f"guarded run {run_id[:12]}'s controller exited without finishing. Next: ./pulsar stop "
                         f"{plan['selected_spec_id']} stops the trial's containers on its recorded nodes "
                         "and retires its service record")
    path = output / "stop-request.json"
    if not path.exists():
        write(path, {"run_id": run_id, "requested_stop": True})
    elif json.loads(read_regular(path)) != {"run_id": run_id, "requested_stop": True}:
        raise ValueError("stop request differs")
    os.kill(controller["owner"][0], signal.SIGTERM)
    return {"run_id": run_id, "stop_requested": True, "cleanup_complete": False}


def fail_prerequisites(output, returncode):
    """Retain failed planning evidence without claiming activation or cleanup."""
    from scripts.public_cli import redact_diagnostic
    spec = serving.load_spec(output / "spec.json")
    diagnostics = []
    for name in ("prerequisites.stdout", "prerequisites.stderr"):
        with (output / name).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            offset = max(0, stream.tell() - 16000)
            stream.seek(offset)
            data = stream.read()
            # Do not expose a partial credential from the truncated first line.
            if offset:
                data = data.partition(b"\n")[2]
            diagnostics.append(data.decode("utf-8", errors="replace"))
    detail = redact_diagnostic("\n".join(diagnostics)).strip()[-4000:]
    message = f"guarded serving prerequisites failed (exit {returncode})"
    if detail:
        message += ":\n" + detail
    result = {"schema_version": 1, "kind": "pulsar-guarded-serving-result",
              "run_id": None, "service_id": None, "spec_id": spec["spec_id"],
              "phases": {"prerequisites": {"complete": False, "returncode": returncode},
                         **{phase: {"complete": False, "not_started": True}
                            for phase in ("preflight", "execute", "cleanup")}},
              "requested_stop": False, "status": "failed", "qualification": False}
    print(message, file=sys.stderr)
    write(output / "result.json", result)
    return result


def finish(output, state_root=None):
    if (output / "result.json").exists():
        raise ValueError("guarded session already finalized")
    plan = validate_plan(json.loads((output / "active-plan.json").read_text()))
    # Bash has joined/cancelled the batch before finalizing. These dispatch
    # programs embed credential-bearing argv and are not retained evidence.
    # SIGKILL cannot run this finalizer; abrupt controller loss needs private
    # artifact review as well as physical-state reconciliation.
    for rank in range(len(plan["ranks"])):
        (output / "execute" / f"{rank}.program").unlink(missing_ok=True)
    phases = {}
    for phase in ("preflight", "execute", "cleanup"):
        if not (output / phase / "batch.json").exists():
            # The phase never ran a batch, as a failed prerequisite check records it.
            phases[phase] = {"complete": False, "not_started": True}
            continue
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
    if state_root is not None and phases["cleanup"]["complete"]:
        from model_library.state import Store
        from scripts.service_state import retire_plan
        result["service_locator_retired"] = retire_plan(Store(state_root), plan)
    write(output / "result.json", result)
    return result


def unverified_ranks(output, count):
    """Ranks whose cleanup was not verified; every rank when the batch is unreadable."""
    try:
        results = {row["index"]: row for row in json.loads(read_regular(output / "cleanup" / "batch.json"))["results"]}
    except (ValueError, OSError, KeyError, TypeError):
        return list(range(count))
    missing = []
    for rank in range(count):
        try:
            verified = (results[rank]["returncode"] == 0 and json.loads(
                read_regular(output / "cleanup" / "jobs" / f"{rank}.out")).get("cleanup_verified") is True)
        except (ValueError, OSError, KeyError, TypeError):
            verified = False
        if not verified:
            missing.append(rank)
    return missing


def summary(output, result, plan):
    """The run's outcome for people; --json and direct callers keep result.json's JSON."""
    run, phases, count = result["run_id"][:12], result["phases"], len(plan["ranks"])
    why = "after a stop request" if result["requested_stop"] else "without a stop request"
    if result["status"] == "stopped":
        head = f"Guarded run {run} stopped as requested."
    elif phases["preflight"].get("not_started"):
        head = f"Guarded run {run} ended before any rank started ({why})."
    else:
        failed = next((name for name in ("preflight", "execute", "cleanup") if not phases[name]["complete"]), None)
        head = (f"Guarded run {run} failed in {failed} ({why})." if failed
                else f"Guarded run {run} ended {why}; it is recorded as failed.")

    def state(name):
        phase = phases[name]
        if phase.get("not_started"):
            return "not started"
        return f"complete on {phase['ranks']} of {count} ranks" if phase["complete"] else "incomplete"

    lines = [head, f"  preflight  {state('preflight')}", f"  execute    {state('execute')}"]
    cleanup, next_step = phases["cleanup"], None
    if cleanup["complete"]:
        record = "service record retired" if result.get("service_locator_retired") else "no active service record"
        lines.append(f"  cleanup    verified on {cleanup['ranks']} of {count} ranks; {record}")
    elif cleanup.get("not_started"):
        lines.append("  cleanup    not needed; no rank started")
    else:
        where = ", ".join(f"{plan['ranks'][rank].get('hostname') or plan['ranks'][rank]['node_id']} (rank {rank})"
                          for rank in unverified_ranks(output, count))
        lines.append(f"  cleanup    not verified on {where}; their containers may still be running")
        next_step = (f"Next: ./pulsar stop {plan['selected_spec_id']} stops the trial's containers on its "
                     "recorded nodes and retires its service record")
    lines.append(f"  result     {output / 'result.json'}")
    if next_step:
        lines.append(next_step)
    elif result["status"] != "stopped" and not phases["preflight"].get("not_started"):
        ran = [f"{output / name / 'jobs'}" for name in ("preflight", "execute") if not phases[name].get("not_started")]
        lines.append("Rank reports: " + ", ".join(ran))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["initialize", "activate", "task", "tasks", "check", "finish", "fail-prerequisites"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--spec-file")
    parser.add_argument("--spec-id")
    parser.add_argument("--owner", type=int)
    parser.add_argument("--state-root")
    parser.add_argument("--returncode", type=int)
    parser.add_argument("--phase")
    parser.add_argument("--rank", type=int)
    parser.add_argument("--local-head", action="store_true")
    argv = sys.argv[1:]
    command = []
    if "--" in argv:
        index = argv.index("--")
        command, argv = argv[index + 1:], argv[:index]
    args = parser.parse_args(argv)
    try:
        if args.operation == "initialize":
            initialize(args.spec_file, args.spec_id, args.output)
        elif args.operation == "activate":
            activate(args.output, args.owner)
        elif args.operation == "task":
            task(args.output, args.phase, args.rank, command, local_head=args.local_head)
        elif args.operation == "tasks":
            tasks(args.output, args.phase)
        elif args.operation == "fail-prerequisites":
            fail_prerequisites(args.output, args.returncode)
        elif args.operation == "check":
            check(args.output, args.phase)
        else:
            result = finish(args.output, args.state_root)
            if os.environ.get("PULSAR_HUMAN_OUTPUT") == "1":
                plan = json.loads(read_regular(args.output / "active-plan.json"))
                print(summary(args.output, result, plan))
            else:
                print(json.dumps(result))
            return 0 if result["status"] == "stopped" else 3
    except (ValueError, OSError) as exc:
        # An expected refusal is one line; a defect elsewhere keeps its traceback.
        print(f"error: {exc}", file=sys.stderr)
        # Nothing has been created when initialization refuses its inputs.
        return 2 if args.operation == "initialize" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
