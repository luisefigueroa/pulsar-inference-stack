"""Parameterized physical-node doubles for real storage CLI integration tests.

Fixture data owns node identities, roots, roles, reachability and container
references. Tools consume that data; no hostname selects product behavior.
Only network, Docker and Hub boundaries are replaced. Storage verification,
records, planning, transport orchestration and publication remain real.
"""
from __future__ import annotations

import base64
import ast
import codecs
import hashlib
import json
import os
from pathlib import Path
import runpy
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def read():
    path = contained(os.environ["MODEL_LIBRARY_FIXTURE"])
    return json.loads(path.read_text())


def contained(path):
    """No fake service may even read model data outside its temporary fixture."""
    root = Path(os.environ["MODEL_LIBRARY_FIXTURE_ROOT"]).resolve()
    value = Path(path)
    if not value.is_absolute() or ".." in value.parts or not value.is_relative_to(root) or not value.resolve().is_relative_to(root):
        raise RuntimeError(f"fixture refused data path outside temporary root: {value}")
    return value


def check_data(value):
    if isinstance(value, dict):
        for item in value.values(): check_data(item)
    elif isinstance(value, list):
        for item in value: check_data(item)
    elif isinstance(value, str) and value.startswith("/"):
        contained(value)


def node_request(value, cfg, current):
    # A virtual configured root represents the same pathname on separate
    # physical filesystems. Only these two policy roots are remapped by rank.
    for key, configured, resolved in (
        ("home_root", cfg["configured_home_root"], current["home_root"]),
        ("view_root", cfg["configured_view_root"], current["view_root"])):
        if key in value:
            if value[key] in cfg.get("permitted_roots", []):
                contained(value[key])
                continue
            if value[key] not in {configured, resolved}:
                raise RuntimeError("fixture received an unexpected configured storage root")
            value[key] = resolved
    check_data(value)
    return value


def guarded_node_arguments(argv, cfg, current):
    if argv[:1] != ["-c"] or len(argv) != 2:
        raise RuntimeError("fixture expected one immutable bundled node program")
    program = argv[1]
    tree = ast.parse(program)
    assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
        and len(n.targets) == 1 and isinstance(n.targets[0], ast.Attribute)
        and isinstance(n.targets[0].value, ast.Name) and n.targets[0].value.id == "sys"
        and n.targets[0].attr == "stdin"]
    if len(assignments) != 1:
        raise RuntimeError("fixture cannot locate bundled node request")
    expression = assignments[0].value.args[0].func.value.args[0]
    if not isinstance(expression, ast.Constant) or not isinstance(expression.value, str):
        raise RuntimeError("fixture node request is not a fixed encoded document")
    encoded = expression.value
    value = node_request(json.loads(base64.b64decode(encoded)), cfg, current)
    replacement = base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()
    if program.count(repr(encoded)) != 1:
        raise RuntimeError("fixture node request cannot be safely replaced")
    return ["-c", program.replace(repr(encoded), repr(replacement))], value


def check_shell_paths(command):
    # shell_join_q uses Bash ANSI-C quoting for multiline bundled programs.
    # Decode literal quotes without evaluating shell expansions or commands.
    command = re.sub(r"\$'((?:\\.|[^'\\])*)'", lambda match:
        shlex.quote(codecs.decode(match.group(1), "unicode_escape")), command)
    for token in shlex.split(command):
        if token.startswith("/"):
            contained(token)


def rank():
    return int(os.environ.get("FIXTURE_RANK", "0"))


def event(kind, **fields):
    cfg = read()
    line = json.dumps({"kind": kind, "rank": rank(), **fields}) + "\n"
    fd = os.open(contained(cfg["events"]), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try: os.write(fd, line.encode())
    finally: os.close(fd)


class RepoFile:
    def __init__(self, path, data, lfs=False):
        self.path = path; self.size = len(data)
        self.blob_id = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if lfs:
            from types import SimpleNamespace
            self.lfs = SimpleNamespace(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        else: self.lfs = None


class HfApi:
    def model_info(self, model_id, revision, expand):
        from types import SimpleNamespace
        cfg = read(); event("inventory", model=model_id, selector=revision)
        if cfg.get("hub_unavailable") or not cfg["nodes"][rank()]["available"]:
            raise RuntimeError("fixture source is unreachable")
        if model_id != cfg["model_id"]:
            raise RuntimeError("fixture model not found")
        if revision not in ("main", cfg["revision"]):
            raise RuntimeError("fixture revision not found")
        return SimpleNamespace(private=False, sha=cfg["revision"], id=model_id)

    def list_repo_tree(self, model_id, repo_type, revision, recursive, expand):
        cfg = read()
        return [RepoFile(name, base64.b64decode(row["data"]), row["lfs"])
                for name, row in sorted(cfg["files"].items())]


def tool(kind, argv):
    cfg = read(); current = cfg["nodes"][rank()]
    if kind == "python":
        for arg in argv:
            if arg.startswith("/") and arg != str(Path(__file__).resolve()):
                contained(arg)
        if argv[:2] == ["-m", "model_library.controller"]:
            request = json.load(sys.stdin)
            check_data(request)
            event("controller-operation", operation=request["operation"])
            if cfg.get("controller_fault") == request["operation"]:
                print("fixture interrupted controller publication", file=sys.stderr)
                return 2
            return subprocess.run([sys.executable, *argv], input=json.dumps(request).encode()).returncode
        os.execv(sys.executable, [sys.executable, *argv])
    if kind == "node-python":
        if not current["available"]: return 255
        os.environ["PULSAR_HOME_ROOT"] = str(contained(current["home_root"]))
        os.environ["PULSAR_HOT_ROOT"] = str(contained(current["view_root"]))
        if argv == ["-m", "model_library.node"]:
            value = node_request(json.load(sys.stdin), cfg, current)
            return subprocess.run([sys.executable, *argv], input=json.dumps(value).encode()).returncode
        guarded, request = guarded_node_arguments(argv, cfg, current)
        event("node-operation", operation=request["operation"])
        fault = cfg.get("node_fault", {})
        if fault.get("operation") == request["operation"] and fault.get("rank") == rank():
            if fault.get("after"):
                result = subprocess.run([sys.executable, *guarded], capture_output=True)
                if result.returncode:
                    sys.stderr.buffer.write(result.stderr)
                    return result.returncode
            print("fixture interrupted the selected node operation", file=sys.stderr)
            return 255
        os.execv(sys.executable, [sys.executable, *guarded])
    if kind == "node-check":
        return 0 if cfg["nodes"][int(argv[0])]["available"] else 255
    if kind == "control":
        target_rank = int(argv[0]); command = argv[1]
        if not cfg["nodes"][target_rank]["available"]: return 255
        check_shell_paths(command)
        return subprocess.run(["bash", "-c", command], env={**os.environ, "FIXTURE_RANK": str(target_rank)}).returncode
    if kind == "docker":
        if not current["available"]: return 255
        containers = current["containers"]
        for container in containers:
            for mount in container["Mounts"]: contained(mount["Source"])
        if argv[:2] == ["ps", "-aq"]:
            print("\n".join(row["Id"] for row in containers)); return 0
        if argv and argv[0] == "inspect":
            print(json.dumps([row for row in containers if row["Id"] in argv[1:]])); return 0
        raise RuntimeError("unexpected Docker action in storage-only fixture: " + repr(argv))
    if kind == "hf":
        if argv[:2] != ["download", cfg["model_id"]] or cfg.get("hub_unavailable"):
            return 2
        revision = argv[argv.index("--revision") + 1]
        if revision != cfg["revision"]: return 2
        destination = contained(argv[argv.index("--local-dir") + 1])
        stage = destination.parent.parent
        for name in ("HF_HUB_CACHE", "HF_XET_CACHE", "HF_ASSETS_CACHE", "TMPDIR"):
            if not contained(os.environ[name]).is_relative_to(stage):
                raise RuntimeError("download cache escaped owned stage")
        event("download", destination=str(destination))
        for name, row in cfg["files"].items():
            path = contained(destination / name); path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(row["data"]))
        bookkeeping = contained(destination / ".cache/huggingface/download")
        bookkeeping.mkdir(parents=True); (bookkeeping / "fixture").write_text("metadata")
        return 0
    if kind == "ip":
        if argv[:3] != ["-j", "route", "get"]: return 2
        if cfg.get("wrong_route"):
            print(json.dumps([{"dev": "mgmt0", "prefsrc": current["control_ip"]}]))
        else:
            print(json.dumps([{"dev": "fabric0", "prefsrc": current["fabric_ip"]}]))
        return 0
    if kind == "rsync":
        listing = contained(next(x.split("=", 1)[1] for x in argv if x.startswith("--files-from=")))
        source, destination = argv[-2:]
        endpoints = []
        for value in (source, destination):
            if value.startswith("/"): endpoints.append(contained(value))
            else:
                alias, path = value.split(":", 1)
                if alias not in [node["alias"] for node in cfg["nodes"]]:
                    raise RuntimeError("transfer used an unconfirmed alias")
                endpoints.append(contained(path))
        event("transfer", source=str(endpoints[0]), destination=str(endpoints[1]))
        if cfg.get("transfer_failure"): return 23
        for raw in listing.read_bytes().split(b"\0"):
            if not raw: continue
            relative = raw.decode(); destination = contained(endpoints[1] / relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(contained(endpoints[0] / relative), destination)
        return 0
    if kind == "ssh":
        alias, command = argv[-2:]
        matches = [node for node in cfg["nodes"] if node["alias"] == alias]
        if len(matches) != 1 or not matches[0]["available"]: return 255
        check_shell_paths(command)
        event("relay", target_rank=matches[0]["rank"])
        child_env = {**os.environ, "FIXTURE_RANK": str(matches[0]["rank"])}
        return subprocess.run(shlex.split(command), env=child_env).returncode
    raise RuntimeError("unknown fixture tool: " + kind)


class Fixture:
    def __init__(self, root, nodes=1):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.repo = self.root / "stack"; self.repo.mkdir()
        # Private site files and Git state are deliberately absent. Synthetic
        # catalog entries can be added here without touching the tested checkout.
        for directory in ("scripts", "cluster", "model_library", "release_spec", "platforms", "policy"):
            if (ROOT / directory).is_dir():
                shutil.copytree(ROOT / directory, self.repo / directory,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.repo / "releases").mkdir()
        self.config_path = self.root / "fixture.json"
        self.state = self.root / "state"
        self.archive = self.root / "archive"; self.archive.mkdir()
        temporary = self.root / "tmp"; temporary.mkdir()
        (self.root / "overlay.json").write_text(json.dumps({"schema_version": 1,
            "kind": "pulsar-deployment-overlay", "defaults": {
                "port": 8000, "served_name": "synthetic", "cache_root": None, "placement": None}, "specs": {}}))
        make_topology = runpy.run_path(str(ROOT / "tests/test_transfer.py"))["topology"]
        topology = make_topology(max(nodes, 2))
        if nodes == 1:
            from scripts.topology_manifest import topology_digest, validate_manifest
            topology["nodes"] = topology["nodes"][:1]; topology["links"] = []
            topology["validation"]["min_rails_per_pair"] = 0
            topology["topology_id"] = topology_digest(topology)
            validate_manifest(topology, require_verified=True)
        self.topology = topology
        (self.root / "topology.json").write_text(json.dumps(topology))
        self.cfg = {"model_id": "example/synthetic-storage", "revision": "a" * 40,
            "configured_home_root": str(self.root / "virtual/homes"),
            "configured_view_root": str(self.root / "virtual/views"),
            "permitted_roots": [],
            "events": str(self.root / "events.jsonl"), "hub_unavailable": False,
            "files": {"config.json": {"data": base64.b64encode(b'{"model_type":"synthetic"}\n').decode(), "lfs": False},
                      "weights.bin": {"data": base64.b64encode(b"synthetic weights").decode(), "lfs": True}},
            "nodes": [{"rank": i, "node_id": n["node_id"], "role": "controller" if i == 0 else "worker",
                       "alias": n["ssh_host"], "control_ip": n["control"]["ip"], "fabric_ip": n["rdma"][0]["cidrs"][0].split("/")[0],
                       "home_root": str(self.root / f"rank-{i}" / "homes"),
                       "view_root": str(self.root / f"rank-{i}" / "views"),
                       "available": True, "containers": []} for i, n in enumerate(topology["nodes"])]}
        self.save()
        binary = self.root / "bin"; binary.mkdir()
        support = Path(__file__).resolve()
        for name, kind in (("python3", "python"), ("hf", "hf"), ("docker", "docker"), ("ip", "ip"), ("rsync", "rsync"), ("ssh", "ssh"), ("node-python", "node-python")):
            target = binary / name
            target.write_text('#!'+sys.executable+'\nimport runpy,sys\nm=runpy.run_path('+repr(str(support))+')\nraise SystemExit(m["tool"]('+repr(kind)+',sys.argv[1:]))\n')
            target.chmod(0o700)
        module_root = self.root / "modules"; hub = module_root / "huggingface_hub"; hub.mkdir(parents=True)
        imports = 'import runpy\n_m=runpy.run_path('+repr(str(support))+')\nHfApi=_m["HfApi"]\nRepoFile=_m["RepoFile"]\n'
        (hub / "__init__.py").write_text(imports)
        (hub / "hf_api.py").write_text('from . import RepoFile\n')
        envfile = self.root / "env.sh"
        def array(values): return "(" + " ".join(shlex.quote(str(x)) for x in values) + ")"
        envfile.write_text(f'''
. {shlex.quote(str(self.repo/'scripts/lib.sh'))}
export PULSAR_HOME_ROOT={shlex.quote(self.cfg['configured_home_root'])}
export PULSAR_HOT_ROOT={shlex.quote(self.cfg['configured_view_root'])}
load_cluster_topology() {{
  CLUSTER_TOPOLOGY_COUNT={nodes}; CLUSTER_TOPOLOGY_ID={topology['topology_id']}; CLUSTER_TOPOLOGY_LOADED=1
  CLUSTER_NODE_IDS={array(n['node_id'] for n in topology['nodes'])}
  CLUSTER_NODE_HOSTNAMES={array(n['hostname'] for n in topology['nodes'])}
  CLUSTER_NODE_SSH_HOSTS={array(n['ssh_host'] for n in topology['nodes'])}
  CLUSTER_NODE_CONTROL_IPS={array(n['control']['ip'] for n in topology['nodes'])}
  CLUSTER_NODE_CONTROL_IFS={array(n['control']['interface'] for n in topology['nodes'])}
  CLUSTER_PROFILE_HCAS={array('roce0' for n in topology['nodes'])}
}}
require_cluster_nodes() {{ load_cluster_topology; [ "$CLUSTER_TOPOLOGY_COUNT" -ge "${{1:-1}}" ]; }}
require_profile_topology() {{ require_cluster_nodes "$NODES"; }}
require_topology_ssh_trust() {{ return 0; }}
ssh_node() {{
  local target_rank="$1"; shift
  python3 {shlex.quote(str(support))} control "$target_rank" "$*"
}}
''')
        self.env = {**os.environ, "MODEL_LIBRARY_FIXTURE": str(self.config_path), "MODEL_LIBRARY_FIXTURE_ROOT": str(self.root), "FIXTURE_RANK": "0",
            "BASH_ENV": str(envfile), "PYTHONPATH": str(module_root) + os.pathsep + str(self.repo),
            "PYTHONDONTWRITEBYTECODE": "1", "PULSAR_MODEL_LIBRARY_DIR": str(self.state),
            "PULSAR_COLD_ROOT": str(self.archive), "PULSAR_NODE_PYTHON": str(binary / "node-python"),
            "PULSAR_HOME_ROOT": self.cfg["configured_home_root"], "PULSAR_HOT_ROOT": self.cfg["configured_view_root"],
            "HF_CACHE": str(self.root / "hf-cache"), "HF_HOME": str(self.root / "hf-auth"), "TMPDIR": str(temporary),
            "PULSAR_DOCKER": str(binary / "docker"), "PULSAR_IP": str(binary / "ip"),
            "PULSAR_RSYNC": str(binary / "rsync"), "PULSAR_SSH": str(binary / "ssh"),
            "CLUSTER_TOPOLOGY_FILE": str(self.root / "topology.json"),
            "PULSAR_OVERLAY_PATH": str(self.root / "overlay.json"), "PULSAR_RELEASES_ROOT": str(self.repo / "releases"),
            "VLLM_IMAGE_MAINLINE": "example/image", "PATH": str(binary) + os.pathsep + os.environ["PATH"]}
        for variable in ("PULSAR_SPEC_FILE", "VLLM_EXTRA_ARGS", "EXTRA_ENV"):
            self.env.pop(variable, None)
        self.manifest_path = self.root / "manifest.json"
        self.spec_path = self.root / "candidate.json"
        self.spec = None

    def save(self): self.config_path.write_text(json.dumps(self.cfg))

    def run(self, *args, spec=False):
        command = ["bash", str(self.repo / "scripts/model-library.sh"), *map(str, args)]
        if spec: command += [self.spec["spec_id"], "--spec-file", str(self.spec_path)]
        command += ["--json"]
        return subprocess.run(command, env=self.env, cwd=self.repo, text=True, capture_output=True)

    def events(self, kind=None):
        path = Path(self.cfg["events"])
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if kind is None or row["kind"] == kind]

    def acquire(self, node=0):
        return self.run("acquire", "--model-id", self.cfg["model_id"], "--revision", self.cfg["revision"],
                        "--node", self.cfg["nodes"][node]["node_id"], "--manifest-out", self.manifest_path, "--yes")

    def candidate(self, nodes=1):
        from release_spec import load_snapshot_manifest, verify_spec, spec_id_for, pretty_json_bytes
        from scripts.release_consumer import build_profile_identity, argv_from_identity
        manifest = load_snapshot_manifest(self.manifest_path)
        args = ["--tensor-parallel-size", str(nodes), "--max-model-len", "1024"]
        if nodes > 1: args += ["--distributed-executor-backend", "mp"]
        identity, gaps = build_profile_identity(model_id=manifest["model_id"], image="example/image@sha256:" + "b" * 64,
            nodes=nodes, gpu_mem_util="0.8", engine_args=args, container_env=[], spec_decode_args=[], spec_decode=False,
            platform_id="dgx-spark-gb10", snapshot_revision=manifest["snapshot_revision"], files=manifest["files"])
        if gaps: raise AssertionError(gaps)
        self.spec = verify_spec({"schema_version": 1, "kind": "pulsar-release-spec", "spec_id": spec_id_for(identity),
            "state": "measured", "identity": identity, "launch_contract": {"stack_version": "e" * 40, "argv": argv_from_identity(identity)},
            "measurements": [], "baselines": [], "evidence": [], "review": {}})
        self.spec_path.write_bytes(pretty_json_bytes(self.spec))
        return self.spec


if __name__ == "__main__": raise SystemExit(tool(sys.argv[1], sys.argv[2:]))
