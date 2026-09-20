"""Attempt-local data and transition helpers. Bash owns every external process."""
from __future__ import annotations

import base64
import fcntl
import json
import os
import secrets
import stat
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from release_spec.diagnostic import load_plan, verify_plan, freeze_plan, verify_payload_dir, sha256_bytes, sha256_file, complete_stopped_state, image_evidence
from release_spec.diagnostic_state import lifecycle, fail, advance, snapshot_problem, final_result
from scripts import diagnostic_kmsg as native
from scripts import diagnostic_journal as journal
from scripts.diagnostic_runtime import docker_create_argv, identity_problem, validate_created_container, write_entrypoint, ENTRYPOINT, environment


def read_json(path):
    return native.read_document(Path(path))


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value, *, exclusive=False):
    path = Path(path)
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    if len(data) > (256 if path.name == "plan.json" else 64) * 1024:
        raise ValueError("attempt record exceeds bound")
    temp = path if exclusive else path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if not exclusive:
            os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        if not exclusive and temp.exists():
            temp.unlink()


def process_start(pid, *, root=None):
    return native.process_start(pid, Path(root) if root else Path("/proc"))


def owner_pid():
    return int(os.environ["PULSAR_DIAGNOSTIC_OWNER_PID"])


def proc_root():
    return native.PROC_ROOT


def cgroup_fs_root():
    return native.CGROUP_ROOT


def authorize(attempt, *, initial=False, cleanup=False, takeover=False):
    """Inherited locked description + live PID/start + nonce, before every write."""
    if attempt.is_symlink() or attempt.resolve() != attempt or not attempt.is_dir():
        raise ValueError("attempt must be a canonical private directory")
    info, lock = os.fstat(9), (attempt / "attempt.lock").stat()
    if (info.st_dev, info.st_ino) != (lock.st_dev, lock.st_ino):
        raise ValueError("operation does not hold the attempt lock")
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
    pid = owner_pid()
    start = process_start(pid)
    if start is None:
        raise ValueError("controller process-start identity missing")
    if initial:
        return None
    claim = read_json(attempt / "claim.json")
    if cleanup:
        if claim["owner_pid"] != pid and process_start(claim["owner_pid"]) == claim["owner_starttime"]:
            raise ValueError("cleanup cannot take over a live owner")
        if not takeover:
            nonce = os.environ.get("PULSAR_DIAGNOSTIC_CLEANUP_NONCE", "")
            if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
                raise ValueError("cleanup operation nonce is missing")
            operation = read_json(attempt / ("cleanup-" + nonce + "-claim.json"))
            if operation["owner_pid"] != pid or operation["owner_starttime"] != start:
                raise ValueError("cleanup operation owner changed")
        return claim
    if claim["owner_pid"] != pid or claim["owner_starttime"] != start or claim["attempt_nonce"] != os.environ.get("PULSAR_DIAGNOSTIC_NONCE"):
        raise ValueError("operation owner identity mismatch")
    return claim


def copy_payload(plan, attempt):
    source, dest = Path(plan["inputs_root"]), attempt / "inputs"
    verify_payload_dir(plan["definition"], source)
    dest.mkdir(mode=0o700)
    for row in plan["definition"]["inputs"]:
        fd = os.open(source / row["name"], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("payload is not a uniquely linked regular file")
            data = stream.read(row["bytes"] + 1)
            after = os.fstat(stream.fileno())
        stable = ("st_dev", "st_ino", "st_size", "st_mode", "st_nlink", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, k) != getattr(after, k) for k in stable) or len(data) != row["bytes"] or sha256_bytes(data) != row["sha256"]:
            raise ValueError("payload changed during sealing")
        path = dest / row["name"]
        with path.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        path.chmod(row["mode"] & 0o555)
    write_entrypoint(plan["definition"], dest)
    verify_payload_dir(plan["definition"], dest, sealed=True)
    sync_directory(dest)
    dest.chmod(0o555)
    return dest


def rehash_before_start(attempt):
    plan = verify_plan(read_json(attempt / "plan.json"))
    verify_payload_dir(plan["definition"], attempt / "inputs", sealed=True)
    entry = attempt / "inputs" / ENTRYPOINT
    info = entry.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o555 or sha256_file(entry) != plan["entrypoint_sha256"]:
        raise ValueError("sealed entrypoint type/mode/hash changed")
    return {"ok": True}


def prepare(plan_path, attempt):
    authorize(attempt, initial=True)
    plan = verify_plan(read_json(plan_path))
    claim = {"schema_version": 2, "kind": "pulsar-diagnostic-claim", "plan_id": plan["plan_id"],
             "attempt_nonce": secrets.token_hex(16), "boot_id": native.read_boot(), "node_id": plan["node_id"],
             "owner_pid": owner_pid(), "owner_starttime": process_start(owner_pid()), "claimed_monotonic_ns": time.monotonic_ns()}
    claim["intended_name"] = "pulsar-diag-" + claim["attempt_nonce"]
    save(attempt / "claim.json", claim, exclusive=True)
    # Store canonical intent first; no subsequent operation opens plan_path.
    save(attempt / "plan.json", plan, exclusive=True)
    save(attempt / "lifecycle.json", lifecycle(), exclusive=True)
    copy_payload(plan, attempt)
    return claim


def inspect_document(path):
    doc = read_json(path)
    if not isinstance(doc, list) or len(doc) != 1 or not isinstance(doc[0], dict):
        raise ValueError("inspect must contain exactly one complete object")
    return doc[0]


def validate_image_document(doc, plan):
    definition = plan["definition"]
    if doc.get("Id") != definition["image_id"]:
        raise ValueError("loaded image identity differs from the plan")
    if definition["schema_version"] == 2:
        manifest, raw_config = image_evidence(definition)
        descriptor = doc.get("Descriptor", {})
        if (descriptor.get("digest") != definition["image_reference"] or
                descriptor.get("mediaType") != manifest["mediaType"] or
                descriptor.get("size") != len(base64.b64decode(definition["image_evidence"]["manifest_base64"]))):
            raise ValueError("loaded manifest descriptor differs from verified bytes")
        if "ConfigDigest" in doc and doc["ConfigDigest"] != definition["image_config_digest"]:
            raise ValueError("reported config digest differs from verified bytes")
        if doc.get("RootFS") != {"Type": "layers", "Layers": raw_config["rootfs"]["diff_ids"]}:
            raise ValueError("loaded ordered rootfs differs from verified config")
        if (doc.get("Variant") or "") != (raw_config.get("variant") or ""):
            raise ValueError("loaded image variant differs from verified config")
        if doc.get("Config") != raw_config["config"]:
            raise ValueError("loaded image-owned config differs from verified bytes")
    elif doc.get("ConfigDigest", doc.get("Id")) != definition["image_config_digest"]:
        raise ValueError("loaded image config identity is not established")
    if type(doc.get("Os")) is not str or type(doc.get("Architecture")) is not str or doc["Os"] + "/" + doc["Architecture"] != definition["platform"]:
        raise ValueError("loaded image platform differs from the plan")
    config = doc.get("Config")
    if not isinstance(config, dict) or config.get("Volumes") not in (None, {}) or config.get("User") not in (None, ""):
        raise ValueError("image has implicit volumes or a different user")
    declared = environment(plan)
    seen = set()
    for item in config.get("Env", []):
        key, sep, value = item.partition("=")
        if not sep or key in seen or key not in declared:
            raise ValueError("image environment is not explicitly declared in the plan")
        seen.add(key)
    return {"ok": True}


def control(attempt, state, claim, command, **fields):
    state["control_sequence"] += 1
    state["observer_phase"] = "draining" if command == "finish" else command
    doc = {k: claim[k] for k in ("plan_id", "attempt_nonce", "boot_id")}
    doc.update(sequence=state["control_sequence"], command=command, **fields)
    save(attempt / "observer-control.json", doc)


def snapshot(attempt):
    path = attempt / "observer-status.json"
    return read_json(path) if path.exists() else None


def observer_alive(state):
    pid, start = state.get("observer_pid"), state.get("observer_starttime")
    return type(pid) is int and isinstance(start, str) and process_start(pid) == start


def identity_matches(inspected, claim, plan):
    return identity_problem(inspected, plan, claim)


def sample_owned_cgroup(inspected, claim, plan):
    """Read-only compatibility helper; missing identity/counters are always unknown."""
    try:
        problem = identity_problem(inspected, plan, claim)
        if problem:
            raise ValueError(problem)
        bound = native.BoundWorkload(inspected, native.read_boot())
        try:
            if claim.get("workload_starttime") and bound.start != claim["workload_starttime"]:
                raise ValueError("PID start changed")
            return {"unknown": False, "pid": bound.pid, "starttime": bound.start, "workload": bound.sample(running=True)}
        finally:
            bound.close()
    except (ValueError, OSError, KeyError, TypeError):
        return {"unknown": True, "workload": None}


def helper_main(argv):
    cmd, args = argv[0], argv[1:]
    mapping = {}
    while args:
        key = args.pop(0)
        if key == "--json":
            continue
        if not key.startswith("--") or not args:
            raise ValueError("invalid diagnostic argument")
        mapping[key[2:].replace("-", "_")] = args.pop(0)
    if cmd == "plan":
        plan = freeze_plan(read_json(mapping["definition"]), node_id=mapping["node"], inputs_root=str(Path(mapping["inputs"]).resolve()))
        save(Path(mapping["out"]), plan, exclusive=True)
        return plan
    attempt = Path(mapping["attempt_dir"]).resolve()
    if cmd == "show":
        return read_json(attempt / "result.json")
    if cmd == "prepare":
        return prepare(Path(mapping["plan"]), attempt)
    if cmd == "field":
        filename = mapping["file"]
        if filename == "lifecycle.json" and mapping.get("cleanup_only") == "true":
            filename = "cleanup-" + os.environ["PULSAR_DIAGNOSTIC_CLEANUP_NONCE"] + "-lifecycle.json"
        doc = read_json(attempt / filename)
        value = doc
        for key in mapping["key"].split("."):
            value = value[key]
        return value
    cleanup_only = mapping.get("cleanup_only") == "true"
    if cleanup_only and cmd not in ("cleanup-owner", "created", "identity", "stopped", "check-stopped", "failure", "cleanup-facts", "closed", "finalize", "ack"):
        raise ValueError("cleanup-only never admits create/start or observation recovery")
    claim = authorize(attempt, cleanup=cleanup_only, takeover=cmd == "cleanup-owner")
    plan = verify_plan(read_json(attempt / "plan.json"))
    cleanup_nonce = secrets.token_hex(16) if cmd == "cleanup-owner" else os.environ.get("PULSAR_DIAGNOSTIC_CLEANUP_NONCE")
    capture = attempt / ("cleanup-" + cleanup_nonce) if cleanup_only else attempt
    if cmd != "cleanup-owner" and Path(mapping.get("capture_dir", str(attempt))) != capture:
        raise ValueError("command capture directory is not owned by this operation")
    state_path = attempt / ("cleanup-" + cleanup_nonce + "-lifecycle.json" if cleanup_only else "lifecycle.json")
    state = read_json(attempt / "lifecycle.json" if cmd == "cleanup-owner" else state_path)
    if cmd == "cleanup-owner":
        state["phase"] = "stopping_or_exited"
    identity_claim = dict(claim, container_id=state["container_id"])
    result = {"ok": True}
    if cmd == "admit":
        if mapping["local_node"] != plan["node_id"] or mapping["local_ssh"] != "local":
            raise ValueError("diagnostic node is not the confirmed local host")
        validate_image_document(inspect_document(attempt / "image.stdout"), plan)
        advance(state, "admitted")
        state["admitted_monotonic_ns"] = time.monotonic_ns()
    elif cmd == "journal-begin":
        name = mapping["name"]
        if name not in ("anchor", "final") or plan["definition"]["observer"]["backend"] != "journal":
            raise ValueError("unsupported journal query")
        save(attempt / ("journal-" + name + "-begin.json"), {"monotonic_ns": time.monotonic_ns(), "boot_id": native.read_boot()}, exclusive=True)
    elif cmd == "journal-anchor":
        begin = read_json(attempt / "journal-anchor-begin.json")
        if int(mapping["rc"]) != 0 or (attempt / "journal-anchor.source-stderr").stat().st_size or begin["boot_id"] != claim["boot_id"] or native.read_boot() != claim["boot_id"]:
            raise ValueError("journal current-source query failed")
        row = journal.anchor(attempt / "journal-anchor.stdout", claim["boot_id"])
        result = {"record": row, "begin_ns": begin["monotonic_ns"], "end_ns": time.monotonic_ns()}
        save(attempt / "journal-anchor.json", result, exclusive=True)
    elif cmd == "journal-started":
        pid = int(mapping["pid"])
        start = process_start(pid)
        if start is None:
            raise ValueError("journal client process identity missing")
        save(attempt / "journal-client.json", {"pid": pid, "starttime": start}, exclusive=True)
    elif cmd == "journal-closing":
        save(attempt / "journal-closing.json", {"monotonic_ns": time.monotonic_ns()}, exclusive=True)
    elif cmd == "journal-closed":
        result = json.loads(mapping["facts"])
        result.update(boot_id=native.read_boot(), completed_ns=time.monotonic_ns(),
                      query_begin_ns=read_json(attempt / "journal-final-begin.json")["monotonic_ns"])
        save(attempt / "journal-final-ready.json", result, exclusive=True)
    elif cmd == "observer-started":
        state["observer_started"] = True
        state["observer_pid"] = int(mapping["pid"])
        state["observer_starttime"] = process_start(state["observer_pid"])
    elif cmd in ("guard", "ready", "ack"):
        observed = snapshot(attempt)
        if cmd == "ack":
            if not observed or observed.get("control_sequence") != state["control_sequence"]:
                return {"ok": False}
            # Identity and latches are checked by guard, not synthesized here.
            result = {"ok": True}
        else:
            problem = snapshot_problem(observed, plan, claim, state)
            if not observer_alive(state):
                problem = "observer process is absent or has a different start identity"
            now = time.monotonic_ns()
            if now - claim["claimed_monotonic_ns"] >= (plan["definition"]["observer"]["operation_seconds"] - plan["definition"]["observer"]["cleanup_reserve_seconds"]) * 10**9:
                problem = problem or "cleanup reserve reached"
            start_path = attempt / "container-start.json"
            if start_path.exists() and now - read_json(start_path)["monotonic_ns"] >= plan["definition"]["observer"]["workload_deadline_seconds"] * 10**9:
                problem = problem or "workload deadline including start reached"
            if not problem:
                state["observer_instance"] = observed["observer_instance"]
                state["snapshot_sequence"] = observed["snapshot_sequence"]
                state["last_snapshot"] = observed
                if cmd == "ready":
                    advance(state, "observer_ready")
            elif cmd == "guard":
                fail(state, problem)
            result = {"ok": problem is None, "reason": problem}
    elif cmd == "create-claim":
        if state["phase"] != "observer_ready":
            raise ValueError("create lacks observer readiness")
        if time.monotonic_ns() - state["admitted_monotonic_ns"] > 600 * 10**9:
            raise ValueError("readiness has expired")
        argv = docker_create_argv(plan, name=claim["intended_name"], input_dir=str(attempt / "inputs"), attempt_nonce=claim["attempt_nonce"])
        save(attempt / "container-create.json", {"argv": argv, "intended_name": claim["intended_name"], "nonce": claim["attempt_nonce"], "monotonic_ns": time.monotonic_ns()}, exclusive=True)
        state["create_consumed"] = True
        advance(state, "create_claimed")
        result = argv
    elif cmd == "created":
        inspected = inspect_document(capture / mapping["file"])
        problem = identity_problem(inspected, plan, identity_claim, reconcile=state["container_id"] is None)
        if problem:
            raise ValueError(problem)
        state["container_id"] = inspected["Id"]
        if state["phase"] == "create_claimed":
            advance(state, "created")
        if not cleanup_only:
            control(attempt, state, claim, "created", inspect=inspected)
        result = inspected["Id"]
    elif cmd in ("identity", "controls", "stopped", "check-stopped", "running"):
        inspected = inspect_document(capture / mapping["file"])
        problem = identity_problem(inspected, plan, identity_claim)
        if problem:
            raise ValueError(problem)
        if cmd == "controls":
            problem = validate_created_container(inspected, plan, input_dir=str(attempt / "inputs"), expected_cid=state["container_id"], attempt_nonce=claim["attempt_nonce"])
            if problem:
                raise ValueError(problem)
            rehash_before_start(attempt)
            advance(state, "controls_verified")
            state["controls_sha256"] = sha256_bytes(json.dumps(inspected, sort_keys=True).encode())
        elif cmd in ("stopped", "check-stopped"):
            problem = complete_stopped_state(inspected.get("State"), start_consumed=state["start_consumed"])
            if problem:
                raise ValueError(problem)
            if cmd == "stopped":
                advance(state, "stopping_or_exited")
                state["cleanup"]["stopped_state"] = inspected["State"]
                if not cleanup_only:
                    control(attempt, state, claim, "stopped", inspect=inspected)
        elif cmd == "running":
            status = inspected.get("State", {})
            if status.get("Running") is not True or status.get("Restarting") is not False or status.get("Status") != "running" or type(status.get("Pid")) is not int or status["Pid"] <= 0:
                return {"ok": False, "reason": "container is not running"}
            advance(state, "running_or_uncertain")
            control(attempt, state, claim, "running", inspect=inspected)
    elif cmd == "consume-start":
        if state["phase"] != "controls_verified":
            raise ValueError("start lacks accepted controls")
        rehash_before_start(attempt)
        problem = snapshot_problem(snapshot(attempt), plan, claim, state)
        if not observer_alive(state):
            problem = "observer process is absent or has a different start identity"
        if problem:
            raise ValueError(problem)
        record = {"container_id": state["container_id"], "attempt_nonce": claim["attempt_nonce"], "monotonic_ns": time.monotonic_ns(), "controls_sha256": state["controls_sha256"], "payload_sha256": plan["payload_sha256"], "entrypoint_sha256": plan["entrypoint_sha256"]}
        save(attempt / "container-start.json", record, exclusive=True)
        state["start_consumed"] = True
        advance(state, "start_claimed")
        control(attempt, state, claim, "starting", start_monotonic_ns=record["monotonic_ns"])
    elif cmd == "failure":
        fail(state, mapping["reason"])
    elif cmd == "cleanup-facts":
        advance(state, "removed_or_unknown")
        facts = json.loads(mapping["facts"])
        if set(facts) != {"stop_rc", "rm_rc", "query_rc", "logs_rc"} or any(v is not None and (type(v) is not int or not 0 <= v <= 255) for v in facts.values()):
            raise ValueError("cleanup command facts are incomplete or untyped")
        # Positive absence is only the successful exact filtered query's bytes.
        query = capture / "absence.stdout"
        facts["absent"] = facts.get("query_rc") == 0 and query.is_file() and query.read_bytes() == b""
        facts["completed_monotonic_ns"] = time.monotonic_ns()
        state["cleanup"].update(facts)
        if state.get("observer_started") and not cleanup_only:
            control(attempt, state, claim, "finish", cleanup_monotonic_ns=state["cleanup"]["completed_monotonic_ns"])
    elif cmd == "closed":
        advance(state, "observer_closed_or_unknown")
        state["observer_closure"] = json.loads(mapping["facts"])
    elif cmd == "finalize":
        # A consumed durable receipt cannot be reset by a later failed helper.
        state["create_consumed"] |= (attempt / "container-create.json").exists()
        state["start_consumed"] |= (attempt / "container-start.json").exists()
        terminal = read_json(attempt / "observer-terminal.json") if (attempt / "observer-terminal.json").exists() else None
        output = capture / "logs.stdout"
        text = output.read_text() if output.exists() and output.stat().st_size <= 12 * 1024**2 else None
        result = final_result(plan, claim, state, terminal, workload_text=text, cleanup_only=cleanup_only)
        advance(state, "terminal")
        if cleanup_only:
            result["cleanup_operation_nonce"] = cleanup_nonce
        save(attempt / ("cleanup-" + cleanup_nonce + "-result.json" if cleanup_only else "result.json"), result, exclusive=True)
    elif cmd == "cleanup-owner":
        fail(state, "cleanup-only cannot certify abandoned observation")
        capture.mkdir(mode=0o700)
        save(attempt / ("cleanup-" + cleanup_nonce + "-claim.json"), {
            "operation_nonce": cleanup_nonce, "attempt_nonce": claim["attempt_nonce"], "plan_id": plan["plan_id"],
            "owner_pid": owner_pid(), "owner_starttime": process_start(owner_pid()), "boot_id": native.read_boot(),
            "claimed_monotonic_ns": time.monotonic_ns()}, exclusive=True)
        result = cleanup_nonce
    else:
        raise ValueError("unknown diagnostic helper: " + cmd)
    save(state_path, state, exclusive=cmd == "cleanup-owner")
    return result
