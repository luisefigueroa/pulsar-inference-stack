"""Diagnostic input freezing, node program packaging and complete-rank results.

No topology discovery or remote execution here: the Bash boundary supplies
confirmed ranks and the existing supervised transport commands.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import secrets
import sys
import zipfile

from diagnostics.schema import digest, load, validate_context

ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def freeze(request_file, payload_dir, request_id, output):
    request, payload = load(request_file, payload_dir, request_id)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    code = output / "code"
    code.mkdir()
    files = [
        *(
            path
            for package in ("diagnostics", "model_library", "release_spec", "serving_guard")
            for path in sorted((ROOT / package).glob("*.py"))
        ),
        ROOT / "scripts/resource_sample.py",
    ]
    hashes = {}
    for path in files:
        relative = path.relative_to(ROOT)
        data = path.read_bytes()
        target = code / relative
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        hashes[str(relative)] = hashlib.sha256(data).hexdigest()
    # A regular package prevents an image's unrelated top-level `scripts`
    # package from taking precedence over our frozen resource sampler.
    package_init = b'"""Frozen Stack diagnostic helpers."""\n'
    (code / "scripts/__init__.py").write_bytes(package_init)
    hashes["scripts/__init__.py"] = hashlib.sha256(package_init).hexdigest()
    write(
        output / "input.json",
        {
            "request": request,
            "payload": payload,
            "request_id": digest(request),
            "run_id": secrets.token_hex(32),
            "guard_files": hashes,
        },
    )


def bind(output, topology_id, verbs_device, rows):
    value = json.loads((output / "input.json").read_text())
    request = value["request"]
    if topology_id != request["topology_id"]:
        raise ValueError("confirmed topology differs from reviewed request")
    ranks = []
    for line in rows.splitlines():
        rank, node, ip, iface, hcas = line.split("\t")
        ranks.append(
            {
                "rank": int(rank),
                "node_id": node,
                "control_ip": ip,
                "control_if": iface,
                "hcas": hcas,
            }
        )
    value.update(ranks=ranks, rank=0, verbs_device=verbs_device)
    validate_context(value)
    write(output / "context.json", value)


def bundle(output, phase, rank):
    value = json.loads((output / "context.json").read_text())
    value.update(rank=rank, operation=phase)
    validate_context(value)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for relative, expected in value["guard_files"].items():
            data = (output / "code" / relative).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("frozen Stack code changed")
            zipped.writestr(relative, data)
    encoded = base64.b64encode(archive.getvalue()).decode("ascii")
    argument = base64.b64encode(json.dumps(value).encode()).decode("ascii")
    return (
        "import base64,io,json,pathlib,sys,tempfile,zipfile\n"
        "with tempfile.TemporaryDirectory(prefix='pulsar-diagnostic-') as temp:\n"
        " root=pathlib.Path(temp)\n"
        f" zipfile.ZipFile(io.BytesIO(base64.b64decode({encoded!r}))).extractall(root)\n"
        " sys.path.insert(0,str(root))\n"
        " from diagnostics.node import main\n"
        " from model_library.verification_process import supervise_node\n"
        f" context=json.loads(base64.b64decode({argument!r}))\n"
        " raise SystemExit(supervise_node(lambda:main(context,root),_pulsar_control.fileno(),_pulsar_token))\n"
    )


def task(output, phase, rank, command):
    directory = output / phase
    directory.mkdir(exist_ok=True)
    (directory / "jobs").mkdir(exist_ok=True)
    program = directory / f"{rank}.program"
    with program.open("x") as stream:
        stream.write(bundle(output, phase, rank))
    write(
        directory / f"{rank}.task.json",
        {"index": rank, "node_slot": rank, "program": str(program), "command": command},
    )


def tasks(output, phase):
    count = len(json.loads((output / "context.json").read_text())["ranks"])
    directory = output / phase
    write(
        directory / "tasks.json",
        [
            json.loads((directory / f"{rank}.task.json").read_text())
            for rank in range(count)
        ],
    )


def phase_results(output, phase):
    context = json.loads((output / "context.json").read_text())
    directory = output / phase
    batch = json.loads((directory / "batch.json").read_text())
    count = len(context["ranks"])
    if (
        batch["outcome"] != "complete"
        or batch["returncode"] != 0
        or len(batch["results"]) != count
    ):
        raise ValueError(phase + ": incomplete or failed rank set")
    result = []
    for rank, row in enumerate(batch["results"]):
        if row["index"] != rank or row["returncode"] != 0:
            raise ValueError(phase + ": rank did not complete")
        # run_batch names stdout with the task index (not node identity).
        value = json.loads((directory / "jobs" / f"{rank}.out").read_text())
        if (
            value.get("rank") != rank
            or value.get("request_id") != context["request_id"]
            or value.get("run_id") != context["run_id"]
        ):
            raise ValueError(phase + ": wrong rank or invocation identity")
        if (
            phase in ("preflight", "execute") or phase.startswith("image-")
        ) and value.get("image_id") != context["request"]["image_id"]:
            raise ValueError(phase + ": image identity differs")
        if phase == "preflight" and value.get("ready") is not True:
            raise ValueError("node not ready")
        if phase.startswith("image-"):
            from diagnostics.images import validate_observation

            validate_observation(value)
        if phase == "execute" and value.get("successful") is not True:
            raise ValueError("diagnostic criteria failed")
        if phase == "cleanup" and (
            value.get("cleanup_verified") is not True or value.get("idle") is not True
        ):
            raise ValueError("cleanup or idleness unconfirmed")
        result.append(value)
    return result


def finish(output, record="result.json"):
    context = json.loads((output / "context.json").read_text())
    status = {}
    for phase in ("preflight", "execute", "cleanup"):
        try:
            values = phase_results(output, phase)
            status[phase] = {"complete": True, "ranks": len(values)}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            status[phase] = {"complete": False, "error": str(exc)}
    result = {
        "schema_version": 1,
        "kind": "pulsar-diagnostic-result",
        "request_id": context["request_id"],
        "run_id": context["run_id"],
        "image_id": context["request"]["image_id"],
        "phases": status,
        "successful": all(v["complete"] for v in status.values()),
        "qualification": False,
    }
    write(output / record, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=["freeze", "bind", "task", "tasks", "check", "finish"]
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--request")
    parser.add_argument("--payload-dir")
    parser.add_argument("--request-id")
    parser.add_argument("--topology-id")
    parser.add_argument("--verbs-device")
    parser.add_argument("--phase")
    parser.add_argument("--rank", type=int)
    argv = sys.argv[1:]
    command = []
    if "--" in argv:
        index = argv.index("--")
        command, argv = argv[index + 1 :], argv[:index]
    args = parser.parse_args(argv)
    if args.operation == "freeze":
        freeze(args.request, args.payload_dir, args.request_id, args.output)
    elif args.operation == "bind":
        bind(args.output, args.topology_id, args.verbs_device, sys.stdin.read())
    elif args.operation == "task":
        task(args.output, args.phase, args.rank, command)
    elif args.operation == "tasks":
        tasks(args.output, args.phase)
    elif args.operation == "check":
        phase_results(args.output, args.phase)
    else:
        result = finish(args.output)
        print(json.dumps(result))
        return 0 if result["successful"] else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
