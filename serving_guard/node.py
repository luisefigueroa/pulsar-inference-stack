"""Supervise an already-verified serving plan on one confirmed node."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request

from diagnostics.node import docker, idle
from model_library.verification_process import cancellation_signals
from scripts.container_runtime import PREFIX, docker_argv, observe_rank, validate_plan
from scripts.resource_sample import read_meminfo


def inspect_named(plan):
    result = docker("container", "ls", "-aq", "--no-trunc", "--filter",
                    "name=^/" + plan["container_name"] + "$")
    ids = result.stdout.split()
    if not ids:
        return None
    if len(ids) != 1:
        raise RuntimeError("ambiguous serving container")
    return json.loads(docker("container", "inspect", ids[0]).stdout)[0]


def owned(info, plan, rank):
    labels = (info.get("Config") or {}).get("Labels") or {}
    return (labels.get(PREFIX + "guard-run") == plan["guard_run_id"]
            and labels.get(PREFIX + "launch-plan") == plan["plan_id"]
            and labels.get(PREFIX + "spec-id") == plan["spec_id"]
            and labels.get(PREFIX + "rank") == ("single" if len(plan["ranks"]) == 1 else str(rank)))


def identity(plan, rank):
    return {"rank": rank, "run_id": plan["guard_run_id"], "spec_id": plan["spec_id"]}


def preflight(plan, rank):
    validate_plan(plan)
    recipe = plan["spec"]["recipe"]
    guard = recipe["container"]["guard"]
    reference = plan["spec"]["source"]["image_repository"] + "@" + recipe["image_digest"]
    image = json.loads(docker("image", "inspect", reference).stdout)[0]
    if (image.get("Architecture") != "arm64" or image.get("Os") != "linux"
            or image.get("Config", {}).get("Entrypoint") != guard["entrypoint"]
            or not any(ref.endswith("@" + recipe["image_digest"]) for ref in image.get("RepoDigests", []))):
        raise RuntimeError("pinned ARM64 image or guarded entrypoint unavailable")
    if inspect_named(plan) is not None:
        raise RuntimeError("serving container name occupied; replacement is not supported")
    idle()
    memory = read_meminfo()
    if memory is None or memory["mem_available_bytes"] < max(guard["min_host_available_bytes"], recipe["container"]["memory_limit_bytes"]):
        raise RuntimeError("insufficient host memory")
    if rank == 0:
        for port in (plan["port"], plan["master_port"]):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("0.0.0.0", port))
    return {**identity(plan, rank), "ready": True, "image_id": image["Id"]}, image


def cleanup(plan, rank):
    info = inspect_named(plan)
    if info is not None:
        if not owned(info, plan, rank):
            raise RuntimeError("unrelated serving container preserved")
        docker("rm", "-f", info["Id"])
    if inspect_named(plan) is not None:
        raise RuntimeError("guarded serving cleanup unconfirmed")
    return {**identity(plan, rank), "cleanup_verified": True}


def verify_argv(plan, rank, argv):
    actual = list(argv)
    if rank == 0 and plan["api_auth"]:
        if actual.count("--api-key") != 1:
            raise ValueError("guarded API credential binding differs")
        index = actual.index("--api-key") + 1
        if index >= len(actual) or not actual[index]:
            raise ValueError("guarded API credential unavailable")
        actual[index] = "<credential>"
    if actual != docker_argv(plan, rank, include_secrets=False):
        raise ValueError("frozen node command differs from serving plan")


def health_ready(plan, argv):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None

    headers = {}
    if plan["api_auth"]:
        headers["Authorization"] = "Bearer " + argv[argv.index("--api-key") + 1]
    request = urllib.request.Request(f"http://127.0.0.1:{plan['port']}/health", headers=headers)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def execute(context, root):
    plan, rank = context["plan"], context["rank"]
    argv = context["argv"]
    verify_argv(plan, rank, argv)
    _, image = preflight(plan, rank)
    process = None
    started = time.monotonic()
    ready = False
    deadline = plan["spec"]["recipe"]["container"]["guard"]["timeout_seconds"] + 35
    output = root / "container-output"
    try:
        with output.open("w+") as stream, cancellation_signals() as cancelled:
            process = subprocess.Popen([os.environ.get("PULSAR_DOCKER", "docker"), *argv[1:]],
                                       stdin=subprocess.PIPE, stdout=stream, stderr=stream)
            info = None
            while process.poll() is None and time.monotonic() - started < 20:
                if cancelled["signal"]:
                    raise RuntimeError("cancelled before guarded release")
                info = inspect_named(plan)
                if info is not None and info.get("State", {}).get("Running"):
                    break
                time.sleep(.1)
            if info is None or not info.get("State", {}).get("Running"):
                raise RuntimeError("serving guard did not start")
            # Readback of image, command, ownership, mounts and resource limits
            # precedes G. Until then only the framework-free CPU guard runs.
            observe_rank(plan, rank, info, image)
            process.stdin.write(b"G")
            process.stdin.flush()
            while process.poll() is None:
                if cancelled["signal"] or time.monotonic() - started > deadline:
                    try:
                        process.stdin.write(b"Q")
                        process.stdin.flush()
                    except (OSError, ValueError):
                        pass
                    # Let the in-container guard reap the model and emit its
                    # final metrics before the driver closes the Docker client.
                    break
                try:
                    if rank == 0 and not ready and health_ready(plan, argv):
                        ready = True
                        process.stdin.write(b"H")
                        # Only the local head writes the controller's readiness
                        # record; this is API health, not a model-quality result.
                        with Path(context["ready_file"]).open("x") as out:
                            json.dump({**identity(plan, rank), "api_healthy": True,
                                       "service_id": plan["service_id"]}, out)
                    process.stdin.write(b".")
                    process.stdin.flush()
                except BrokenPipeError:
                    break
                time.sleep(.5)
            process.wait(timeout=5)
            size = stream.tell()
            if size > 34 * 1024**2:
                raise RuntimeError("serving output exceeds limit")
            stream.seek(max(0, size - 1024**2))
            tail = stream.read()
            reports = []
            for line in tail.splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("kind") == "pulsar-serving-guard-rank":
                    reports.append(value)
            if len(reports) != 1 or any(reports[0].get(k) != v for k, v in identity(plan, rank).items()):
                raise RuntimeError("missing or inconsistent serving guard report")
            report = reports[0]
            if process.returncode != 0 or not report.get("stopped"):
                raise RuntimeError("serving guard stopped workload: " + json.dumps(report))
            return report
    finally:
        if process is not None:
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        cleanup(plan, rank)


def main(context, root):
    try:
        plan, rank = context["plan"], context["rank"]
        validate_plan(plan)
        if not 0 <= rank < len(plan["ranks"]) or "guard_run_id" not in plan:
            raise ValueError("invalid guarded serving rank")
        operation = context["operation"]
        if operation == "preflight":
            result, _ = preflight(plan, rank)
        elif operation == "cleanup":
            result = cleanup(plan, rank)
        elif operation == "execute":
            result = execute(context, root)
        else:
            raise ValueError("unsupported guard node operation")
        print(json.dumps(result), flush=True)
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print("guarded serving rank failed: " + str(exc), file=__import__("sys").stderr)
        return 3
