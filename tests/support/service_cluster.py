"""A saved four-node ring with services on it, for Stop and inventory tests.

Launch plans and service records are saved as the launcher saves them, and
each service's containers carry the labels the launcher gives them
(`rank_spec`), on the nodes their plan names. Docker and SSH are doubles that
keep every container in one JSON file; `nvidia-smi` answers nothing. Nothing
here contacts a node, and every file lives in the test's temporary directory.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for _path in (str(ROOT), str(ROOT / 'tests')):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from model_library.state import Store  # noqa: E402
from release_spec import build_snapshot_manifest  # noqa: E402
from release_spec.normalize import canonical_json_digest  # noqa: E402
from release_spec.serving import example, freeze  # noqa: E402
from scripts.container_runtime import build_plan, rank_spec  # noqa: E402
from scripts.service_state import save  # noqa: E402
from support.topology_fixture import Fixture, node_probe, ring_probes, shuffled  # noqa: E402

LABEL = 'io.pulsar.gb10.'
KEYS = ['head', 'worker', 'rank-2', 'rank-3']
MODEL_ID = 'example/service-synthetic'

# The docker double. containers.json holds {node, id, name, labels, state,
# image, cmd}; NODE is the node ID the call runs on (this node unless the SSH
# double set SERVICE_NODE). Every call is logged as [node, args] and every
# removal as [node, id]. SERVICE_DOCKER_DOWN lists node IDs whose Docker
# fails. SERVICE_SWAP_AFTER_PROBE replaces the saved cluster with the file it
# names after the first `container ls`, as a concurrent save would.
DOCKER = r'''#!/usr/bin/env python3
import json, os, shutil, sys
root = os.environ["SERVICE_FIXTURE"]
probes = {}
for name in os.listdir(root):
    if name.startswith("probe-") and name[6:-5].isdigit():
        probe = json.load(open(os.path.join(root, name)))
        probes[int(name[6:-5])] = probe
node = os.environ.get("SERVICE_NODE") or probes[0]["node_id"]
args = sys.argv[1:]
with open(os.path.join(root, "docker-calls.jsonl"), "a") as stream:
    stream.write(json.dumps([node, args]) + "\n")
def trace(what):
    # One ordered log of what every double did, as "node-N what".
    with open(os.path.join(root, "trace.log"), "a") as stream:
        via = " over ssh" if os.environ.get("SERVICE_VIA_SSH") else ""
        stream.write(f"{node.rsplit('-', 2)[-2]}-{node.rsplit('-', 1)[-1]} {what}{via}\n")
if node in os.environ.get("SERVICE_DOCKER_DOWN", "").split(","):
    print("Cannot connect to the Docker daemon", file=sys.stderr)
    sys.exit(1)
state = os.path.join(root, "containers.json")
everything = json.load(open(state)) if os.path.exists(state) else []
here = [c for c in everything if c["node"] == node]
def find(ref):
    for c in here:
        if c["id"] == ref or c["name"] == ref:
            return c
    print(f"Error: No such object: {ref}", file=sys.stderr)
    sys.exit(1)
def labelled(c, wanted):
    key, _, value = wanted.partition("=")
    return c["labels"].get(key) == value
if args == ["info"]:
    sys.exit(0)
if args[:3] == ["ps", "-a", "--format"]:
    trace("docker ps names")
    for c in here:
        print(c["name"])
    sys.exit(0)
IMAGE_ID = "sha256:" + "e" * 64
if args[:2] == ["image", "inspect"]:
    trace("docker image inspect")
    if args[2:4] == ["--format", "{{json .}}"]:
        # The pinned image, as an observation reads it.
        print(json.dumps({"Id": IMAGE_ID, "RepoDigests": ["example/image@sha256:" + "d" * 64],
                          "Config": {"Env": [], "Entrypoint": None}}))
    sys.exit(0)
if args[:2] == ["ps", "-aq"]:
    wanted = [args[i + 1].partition("=")[2] for i, a in enumerate(args) if a == "--filter"]
    for c in here:
        if all(labelled(c, w) for w in wanted):
            print(c["id"])
    sys.exit(0)
if args[:4] == ["container", "ls", "-aq", "--no-trunc"]:
    filters = [args[i + 1] for i in range(4, len(args), 2)]
    swap = os.environ.get("SERVICE_SWAP_AFTER_PROBE")
    for c in here:
        ok = True
        for f in filters:
            key, _, value = f.partition("=")
            if key == "name" and value != "^/" + c["name"] + "$":
                ok = False
            if key == "label" and not labelled(c, value):
                ok = False
        if ok:
            print(c["id"])
    if swap and not os.path.exists(os.path.join(root, "swapped")):
        open(os.path.join(root, "swapped"), "w").close()
        shutil.copyfile(swap, os.path.join(root, "topology.json"))
        shutil.copyfile(swap + ".ssh-config", os.path.join(root, "ssh-config"))
    sys.exit(0)
if args[:2] == ["inspect", "--format"]:
    trace("docker inspect " + ("by id" if len(args[3]) == 64 else "by name"))
    c = find(args[3])
    if args[2] == "{{.State.Status}}":
        print(c["state"])
    elif args[2] == "{{json .}}" and "argv" in c:
        # A rank of a multi-node service the launcher started, as docker shows
        # it: labels, environment, mounts and command read back from its run
        # command, and the host settings every multi-node example recipe asks for.
        argv, env, mounts, entrypoint, i = c["argv"], [], [], None, 1
        image = next(index for index, word in enumerate(argv) if word.startswith("example/image@sha256:"))
        while i < image:
            word, value = argv[i], argv[i + 1] if i + 1 < len(argv) else ""
            if word == "-e":
                env.append(value)
            elif word == "-v":
                source, target, _ = value.split(":")
                mounts.append({"Type": "bind", "Source": source, "Destination": target, "RW": False})
            elif word == "--mount":
                fields = dict(part.partition("=")[::2] for part in value.split(","))
                mounts.append({"Type": "bind", "Source": fields["src"], "Destination": fields["dst"], "RW": False})
            elif word == "--entrypoint":
                entrypoint = [value]
            i += 2 if word in ("-e", "-v", "--mount", "--entrypoint", "--name", "--label") else 1
        host = {"NetworkMode": "host", "IpcMode": "host", "ShmSize": 67108864, "Memory": 0, "NanoCpus": 0,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "Ulimits": [{"Name": "memlock", "Soft": -1, "Hard": -1},
                            {"Name": "stack", "Soft": 67108864, "Hard": 67108864}],
                "DeviceRequests": [{"Driver": "", "Count": -1, "DeviceIDs": None, "Capabilities": [["gpu"]]}],
                "Devices": [{"PathOnHost": "/dev/infiniband/uverbs0", "PathInContainer": "/dev/infiniband/uverbs0",
                             "CgroupPermissions": "rwm"}], "PortBindings": {}}
        print(json.dumps({"Id": c["id"], "Name": "/" + c["name"], "Image": IMAGE_ID, "Mounts": mounts,
                          "HostConfig": host,
                          "Config": {"Labels": c["labels"], "Env": env, "Entrypoint": entrypoint,
                                     "Cmd": argv[image + 1:], "Healthcheck": {"Test": ["NONE"]}},
                          "State": {"Running": c["state"] == "running", "Status": c["state"],
                                    "StartedAt": "2026-01-01T00:00:00Z"}}))
    else:
        print(json.dumps({"id": c["id"], "name": "/" + c["name"], "labels": c["labels"]}))
    sys.exit(0)
if args[:4] == ["inspect", "--type", "container", "--format"]:
    trace("docker state")
    c = find(args[5])
    print("true" if c["state"] == "running" else "false")
    sys.exit(0)
if args[:1] == ["run"]:
    # A launch: the container exists from here, labelled as its argv says.
    # SERVICE_RUN_FAILS lists node IDs whose docker run fails.
    if node in os.environ.get("SERVICE_RUN_FAILS", "").split(","):
        trace("docker run fails")
        print("docker: Error response from daemon: fixture failure", file=sys.stderr)
        sys.exit(125)
    import hashlib
    labels, name, i = {}, None, 1
    while i < len(args):
        if args[i] == "--name":
            name = args[i + 1]; i += 2; continue
        if args[i] == "--label":
            key, _, value = args[i + 1].partition("="); labels[key] = value; i += 2; continue
        i += 1
    cid = hashlib.sha256(f"{node}:{name}:{len(everything)}:{args}".encode()).hexdigest()
    everything.append({"node": node, "id": cid, "name": name, "labels": labels, "state": "running",
                       "image": "example/image@sha256:" + "d" * 64, "cmd": args[args.index("--model"):]
                       if "--model" in args else [], "argv": args})
    json.dump(everything, open(state, "w"))
    with open(os.path.join(root, "runs.jsonl"), "a") as stream:
        stream.write(json.dumps([node, args]) + "\n")
    trace(f"docker run rank {labels.get('io.pulsar.gb10.rank')}")
    print(cid)
    sys.exit(0)
if args[:2] == ["logs", "--tail"]:
    trace("docker logs")
    find(args[3])
    print("fixture engine log line")
    sys.exit(0)
if args[:1] == ["inspect"] and len(args) == 2:
    c = find(args[1])
    print(json.dumps([{"Id": c["id"], "Name": "/" + c["name"],
                       "Config": {"Labels": c["labels"], "Image": c.get("image", "example/image"),
                                  "Cmd": c.get("cmd", [])},
                       "State": {"Running": c["state"] == "running", "Status": c["state"]}}]))
    sys.exit(0)
if args[:1] == ["top"]:
    print("PID")
    sys.exit(0)
if args[:2] == ["rm", "-f"]:
    trace("docker rm " + ("by id" if len(args[2]) == 64 else "by name"))
    c = find(args[2])
    with open(os.path.join(root, "mutations.jsonl"), "a") as stream:
        stream.write(json.dumps([node, c["id"]]) + "\n")
    # SERVICE_RM_KEEPS lists node IDs whose removal reports success but keeps the container.
    if node not in os.environ.get("SERVICE_RM_KEEPS", "").split(","):
        json.dump([other for other in everything if other is not c], open(state, "w"))
    sys.exit(0)
print("unexpected docker operation: " + " ".join(args), file=sys.stderr)
sys.exit(97)
'''

# The SSH double runs the remote command on the node its alias names, with the
# docker double first on PATH; SERVICE_UNREACHABLE lists node IDs that do not
# answer. Memory reads answer a fixed size, so no host value is shown.
SSH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
RDMA_LINKS = "\n".join(f"link {hca}/1 state ACTIVE physical_state LINK_UP netdev x"
                       for hca in ("rocep0a", "rocep0b", "rocep1a", "rocep1b", "roce0", "roce1"))
root = os.environ["SERVICE_FIXTURE"]
args = sys.argv[1:]
alias = args[args.index("--") + 1]
mapping = json.load(open(os.path.join(root, "map.json")))
if alias not in mapping:
    sys.exit(255)
node = json.load(open(os.path.join(root, f"probe-{mapping[alias]}.json")))["node_id"]
with open(os.path.join(root, "ssh-calls.jsonl"), "a") as stream:
    stream.write(json.dumps([node, args[-1]]) + "\n")
if node in os.environ.get("SERVICE_UNREACHABLE", "").split(","):
    print(f"ssh: connect to host {alias} port 22: Connection timed out", file=sys.stderr)
    sys.exit(255)
command = args[-1]
if args[args.index("--") + 2:] == ["python3", "-"]:
    # The host versions an observation records, from the fixture.
    with open(os.path.join(root, "trace.log"), "a") as stream:
        stream.write(f"node-{node.rsplit('-', 1)[-1]} runtime context over ssh\n")
    sys.stdin.read()
    print(json.dumps({"architecture": "aarch64", "kernel_release": "fixture", "gpu_driver": None,
                      "container_runtime_version": None}))
    sys.exit(0)
if command == "awk '/MemAvailable/{print $2}' /proc/meminfo":
    # Preflight reads kB: 100 GiB.
    with open(os.path.join(root, "trace.log"), "a") as stream:
        stream.write(f"node-{node.rsplit('-', 1)[-1]} meminfo over ssh\n")
    print(100 * 1024 * 1024)
    sys.exit(0)
if "/proc/meminfo" in command:
    print("100.00")
    sys.exit(0)
# Preflight's node checks answer from the fixture, never from this machine:
# every RDMA adapter ACTIVE, the device present, the control address bound.
if command.startswith(("rdma link show", "test -e ", "ip -4 -o addr", "ping ", "true")):
    with open(os.path.join(root, "trace.log"), "a") as stream:
        stream.write(f"node-{node.rsplit('-', 1)[-1]} {command.split()[0]} over ssh\n")
    if command.startswith("rdma link show"):
        print(RDMA_LINKS)
    sys.exit(0)
if command.startswith("python3 ") and "probe" in command:
    with open(os.path.join(root, "trace.log"), "a") as stream:
        stream.write(f"node-{node.rsplit('-', 1)[-1]} probe over ssh\n")
    sys.stdin.read()
    print(open(os.path.join(root, f"probe-{mapping[alias]}.json")).read())
    sys.exit(0)
sys.exit(subprocess.call(["bash", "-c", command], env=dict(os.environ, SERVICE_NODE=node, SERVICE_VIA_SSH="1")))
'''

NVIDIA_SMI = '#!/bin/sh\nexit 0\n'

# Preflight's commands on this node (saved rank 0) answer from the fixture:
# rdma lists every adapter ACTIVE, ip shows this node's control address.
RDMA = r'''#!/usr/bin/env python3
import os
with open(os.path.join(os.environ["SERVICE_FIXTURE"], "trace.log"), "a") as stream:
    stream.write("node-0 rdma\n")
for hca in ("rocep0a", "rocep0b", "rocep1a", "rocep1b", "roce0", "roce1"):
    print(f"link {hca}/1 state ACTIVE physical_state LINK_UP netdev x")
'''
IP = r'''#!/usr/bin/env python3
import json, os
root = os.environ["SERVICE_FIXTURE"]
with open(os.path.join(root, "trace.log"), "a") as stream:
    stream.write("node-0 ip\n")
probe = json.load(open(os.path.join(root, "probe-0.json")))
print(f"2: {probe['control']['interface']}    inet {probe['control']['ip']}/24 brd x scope global")
'''

# The curl double: every request is logged as [url, argv]. /health answers on
# the hosts in SERVICE_HEALTHY (comma-separated; default 127.0.0.1), and the
# test completion answers on SERVICE_ANSWERS (default: the same hosts).
CURL = r'''#!/usr/bin/env python3
import json, os, sys
from urllib.parse import urlsplit
root = os.environ["SERVICE_FIXTURE"]
args = sys.argv[1:]
url = args[-1]
with open(os.path.join(root, "curl-calls.jsonl"), "a") as stream:
    stream.write(json.dumps([url, args]) + "\n")
with open(os.path.join(root, "trace.log"), "a") as stream:
    stream.write(f"curl {url}\n")
host = urlsplit(url).hostname
healthy = os.environ.get("SERVICE_HEALTHY", "127.0.0.1").split(",")
if url.endswith("/health"):
    sys.exit(0 if host in healthy else 7)
if url.endswith("/v1/completions"):
    if host not in os.environ.get("SERVICE_ANSWERS", ",".join(healthy)).split(","):
        print("curl: (7) Failed to connect", file=sys.stderr)
        sys.exit(7)
    out = args[args.index("-o") + 1]
    open(out, "w").write('{"choices":[{"text":" 4"}]}')
    sys.stdout.write("200")
    sys.exit(0)
sys.exit(2)
'''

# Status asks /health from Python, not curl: this sitecustomize answers it as
# the curl double does (SERVICE_HEALTHY) and logs "health URL" to the trace.
HEALTH_DOUBLE = r'''import os
if os.environ.get("SERVICE_FIXTURE"):
    import urllib.error, urllib.request
    from urllib.parse import urlsplit

    class _Answer:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    def _open(self, request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        with open(os.path.join(os.environ["SERVICE_FIXTURE"], "trace.log"), "a") as stream:
            stream.write(f"health {url}\n")
        if urlsplit(url).hostname in os.environ.get("SERVICE_HEALTHY", "127.0.0.1").split(","):
            return _Answer()
        raise urllib.error.URLError("fixture: no answer")

    urllib.request.OpenerDirector.open = _open
'''

# Check scripts a Start runs, as doubles in the installation copy: each logs
# its arguments and passes. Image, memory and model files are tested
# elsewhere; these tests are about where Start acts.
CHECK = r'''#!/usr/bin/env bash
printf '%s\n' "${0##*/} $*" >>"$SERVICE_FIXTURE/checks.log"
# SERVICE_WEIGHTS_RC: the model-file check's answer (1: not prepared on every rank).
if [ "${0##*/}" = check-weights.sh ] && [ -n "${SERVICE_WEIGHTS_RC:-}" ]; then exit "$SERVICE_WEIGHTS_RC"; fi
case " $* " in *" --json "*) echo '{"state":"ok","ranks":[],"reason":""}' ;; *) echo "PASS  ${0##*/}" ;; esac
exit 0
'''
OBSERVE = r'''#!/usr/bin/env bash
printf '%s\n' "observe-serving.sh $*" >>"$SERVICE_FIXTURE/checks.log"
exit "${SERVICE_VERIFY_RC:-0}"
'''

# Functions a Start reaches that would read this machine: prepared files come
# from the fixture's plan inputs, and ports are logged and free.
START_ENV = r'''
. "$SERVICE_STACK/scripts/lib.sh"
library_hot_info_for_profile() {
  local -a ids=()
  if [ -n "${SERVING_NODE_IDS[*]:-}" ]; then ids=("${SERVING_NODE_IDS[@]}"); else ids=("${CLUSTER_NODE_IDS[@]:0:$NODES}"); fi
  printf '%s\n' "prepared ${ids[*]}" >>"$SERVICE_FIXTURE/checks.log"
  python3 "$SERVICE_FIXTURE/prepared.py" "$CONF_PATH" "$CLUSTER_TOPOLOGY_ID" "${ids[@]}"
}
# Preflight's node probe and RDMA device, from the fixture: the probe of the
# node at saved rank $1, read over SSH except for this node.
probe_node_json_for_rank() {
  local position="$1"
  [ "$position" = 0 ] || "$PULSAR_SSH" -- "${CLUSTER_NODE_SSH_HOSTS[$position]}" "python3 - --probe" </dev/null
  [ "$position" != 0 ] || { printf 'node-0 probe\n' >>"$SERVICE_FIXTURE/trace.log"; python3 -c '
import json, pathlib, sys
for path in pathlib.Path(sys.argv[1]).glob("probe-*.json"):
    probe = json.loads(path.read_text())
    if probe["node_id"] == sys.argv[2]: print(json.dumps(probe))' "$SERVICE_FIXTURE" "${CLUSTER_NODE_IDS[0]}"; }
}
PULSAR_RDMA_VERBS_DEVICE="$SERVICE_FIXTURE/infiniband"
# SERVICE_ARGV_RANK: count each build of that role rank's docker command in
# argv-builds; SERVICE_ARGV_FAILS_AT: that build fails, as an unreadable launch
# plan does.
eval "_service_load_docker_argv() $(declare -f load_docker_argv_from_plan | tail -n +2)"
load_docker_argv_from_plan() {
  if [ -n "${SERVICE_ARGV_RANK:-}" ] && [ "${2:-}" = "$SERVICE_ARGV_RANK" ]; then
    printf 'build\n' >>"$SERVICE_FIXTURE/argv-builds"
    if [ "$(wc -l <"$SERVICE_FIXTURE/argv-builds")" = "${SERVICE_ARGV_FAILS_AT:-0}" ]; then
      printf 'argv of rank %s fails\n' "$2" >>"$SERVICE_FIXTURE/trace.log"
      die "fixture: the launch command of rank $2 could not be read"
    fi
  fi
  _service_load_docker_argv "$@"
}
# SERVICE_AFTER_RANK=R and SERVICE_AFTER_RANK_DOES=exit-255|interrupt: once the
# launcher logged rank R's container ID, it exits 255 as a failing step would,
# or interrupts itself as Ctrl-C does.
eval "_service_log() $(declare -f log | tail -n +2)"
log() {
  _service_log "$@"
  if [ -n "${SERVICE_AFTER_RANK:-}" ] && [[ "$*" == "rank $SERVICE_AFTER_RANK id="* ]]; then
    printf 'after rank %s: %s\n' "$SERVICE_AFTER_RANK" "$SERVICE_AFTER_RANK_DOES" >>"$SERVICE_FIXTURE/trace.log"
    case "$SERVICE_AFTER_RANK_DOES" in
      exit-255) exit 255 ;;
      interrupt) kill -INT $$ ;;
    esac
  fi
}
# SERVICE_PLAN_WITHOUT_ID: the plan up.sh writes has no plan_id.
eval "_service_write_launch_plan_file() $(declare -f write_launch_plan_file | tail -n +2)"
write_launch_plan_file() {
  _service_write_launch_plan_file "$@" || return
  if [ -n "${SERVICE_PLAN_WITHOUT_ID:-}" ] && [ "${0##*/}" = up.sh ]; then
    python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); p.pop("plan_id"); json.dump(p, open(sys.argv[1], "w"))' "$1"
  fi
}
# SERVICE_FAILS_LOADING: once a container started, the next step that builds
# the API's request arguments (the health wait's) fails, as an unexpected error would.
eval "_service_api_auth_curl_args() $(declare -f api_auth_curl_args | tail -n +2)"
api_auth_curl_args() {
  if [ -n "${SERVICE_FAILS_LOADING:-}" ] && grep -q 'docker run' "$SERVICE_FIXTURE/trace.log" 2>/dev/null; then
    printf 'loading step fails\n' >>"$SERVICE_FIXTURE/trace.log"
    return 1
  fi
  _service_api_auth_curl_args "$@"
}
port_free() {
  printf '%s\n' "port $1 ${2:-local}" >>"$SERVICE_FIXTURE/checks.log"
  printf '%s\n' "port $1 on ${2:-local}" >>"$SERVICE_FIXTURE/trace.log"
  [ "$1:${2:-local}" != "${SERVICE_BUSY_PORT:-}" ]
}
'''

PREPARED = r'''import json, os, sys
spec = json.load(open(sys.argv[1]))
topology, ids = sys.argv[2], sys.argv[3:]
# SERVICE_PREPARED_DRIFT: from the second reading on, the copies are elsewhere,
# as when files change between Start's checks and its launcher.
counter = os.path.join(os.environ["SERVICE_FIXTURE"], "prepared-readings")
count = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
open(counter, "w").write(str(count))
hub = "/var/tmp/fixture-moved" if os.environ.get("SERVICE_PREPARED_DRIFT") and count > 1 else "/var/tmp/fixture-hub"
manifest = spec["recipe"]["model"]["snapshot_manifest"]["manifest_id"]
commit = spec["recipe"]["model"]["model_commit"]
print(json.dumps(dict(schema_version=1, kind="pulsar-prepared-set", spec_id=spec["spec_id"], topology_id=topology,
                      snapshot_manifest_id=manifest, revision=commit, home_node_id=ids[0],
                      ranks=[dict(rank=r, node_id=node, hub_path=f"{hub}-{r}",
                                  path=f"{hub}-{r}/snapshots/{commit}") for r, node in enumerate(ids)])))
'''


def recipe(nodes, variant=0, *, guarded=False, ring=False):
    """A frozen spec on NODES nodes; VARIANT gives another spec of the same size,
    GUARDED one that runs only as a guarded trial, and RING one that passes data
    around a closed loop of its nodes (NCCL_ALGO=Ring)."""
    data = b'synthetic service weights'
    manifest = build_snapshot_manifest(model_id=MODEL_ID, snapshot_revision='c' * 40, files=[
        dict(path='weights.bin', size=len(data), sha256=hashlib.sha256(data).hexdigest())])
    draft = example(nodes)
    if ring:
        draft['recipe']['container_env'] = sorted([*draft['recipe']['container_env'], 'NCCL_ALGO=Ring'])
    draft['source']['image_repository'] = 'example/image'
    draft['recipe']['model'] = {'model_id': MODEL_ID, 'model_commit': 'c' * 40}
    draft['recipe']['image_digest'] = 'sha256:' + 'd' * 64
    draft['recipe']['engine_args'] += ['--max-model-len', str(1024 + variant)]
    spec = freeze(draft, manifest)
    if guarded:
        from release_spec import serving
        from serving_guard import program
        container = dict(spec['recipe']['container'], memory_limit_bytes=16 * 1024 ** 3,
                         guard=program.template(['engine'], minimum=8 * 1024 ** 3, startup=10, timeout=20))
        spec = serving.apply_overrides(spec, {'container': container})
    return spec, manifest


class ServiceCluster:
    """A saved ring of four nodes, its saved records and the containers on it."""

    def __init__(self, root, mesh=None):
        """MESH, when given, is a full mesh of that many nodes instead of the ring."""
        self.root = Path(root)
        if mesh:
            # Every node has two adapters, each on a subnet all nodes share: two
            # links between every pair, as a multi-node recipe needs.
            probes = [dict(node_probe(rank), rdma=[
                dict(hca='roce0', netdev='fabric0', cidrs=[f'198.51.100.{10 + rank}/24']),
                dict(hca='roce1', netdev='fabric1', cidrs=[f'203.0.113.{10 + rank}/24'])]) for rank in range(mesh)]
            self.fixture = Fixture(self.root, probes=probes)
        else:
            self.fixture = Fixture(self.root, probes=shuffled(ring_probes(4)))
        self.topology = self.fixture.topology
        self.topology_id = self.topology['topology_id']
        self.nodes = sorted(self.topology['nodes'], key=lambda node: node['rank'])
        self.store = Store(self.root / 'model-library')
        self.releases = self.root / 'releases'
        self.releases.mkdir()
        for name, body in (('docker', DOCKER), ('ssh', SSH), ('nvidia-smi', NVIDIA_SMI)):
            path = self.root / 'bin' / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = dict(self.fixture.env, SERVICE_FIXTURE=str(self.root), PULSAR_RELEASES_ROOT=str(self.releases),
                        INVENTORY_NVIDIA_SMI=str(self.root / 'bin' / 'nvidia-smi'), COLUMNS='80', NO_COLOR='1')
        self.containers = []

    # -- what the launcher leaves -------------------------------------------

    def hostname(self, position):
        return self.nodes[position]['hostname']

    def node_id(self, position):
        return self.nodes[position]['node_id']

    def spec(self, nodes, variant=0, *, catalog=True, guarded=False, ring=False):
        spec, manifest = recipe(nodes, variant, guarded=guarded, ring=ring)
        if catalog:
            (self.releases / f"{spec['spec_id']}.json").write_text(json.dumps(spec))
        return spec, manifest

    def plan(self, spec, manifest, positions, *, record=True, topology_id=None):
        """The launch plan for SPEC with role rank r on saved position POSITIONS[r];
        RECORD saves it and its service record as an ordinary start does."""
        topology_id = topology_id or self.topology_id
        nodes = [self.nodes[position] for position in positions]
        multi = len(nodes) > 1
        facts = dict(port=8000, served_name='served-model', topology_id=topology_id, lifecycle_action='start',
                     ranks=[dict(rank=rank, node_id=node['node_id'], hostname=node['hostname'],
                                 ssh_host=node['ssh_host'], control_ip=node['control']['ip'],
                                 control_if=node['control']['interface'], hcas='rocep0a' if multi else '')
                            for rank, node in enumerate(nodes)])
        prepared = dict(schema_version=1, kind='pulsar-prepared-set', spec_id=spec['spec_id'],
                        topology_id=topology_id, snapshot_manifest_id=manifest['manifest_id'], revision='c' * 40,
                        home_node_id=nodes[0]['node_id'],
                        ranks=[dict(rank=rank, node_id=node['node_id'], hub_path=f'/var/tmp/fixture-hub-{rank}',
                                    path=f'/var/tmp/fixture-hub-{rank}/snapshots/' + 'c' * 40)
                               for rank, node in enumerate(nodes)])
        with mock.patch.dict(os.environ, {'API_KEY': '', 'VLLM_API_KEY': ''}):
            plan = build_plan(spec, spec['spec_id'], facts, prepared)
        if record:
            save(self.store, plan)
        return plan

    def run(self, plan, positions=None, *, state='running', labels=None, name=None, ranks=None):
        """Containers of PLAN on POSITIONS (default: the nodes the plan names),
        labelled as the launcher labels them, with LABELS changed."""
        by_id = {node['node_id']: index for index, node in enumerate(self.nodes)}
        if (self.root / 'containers.json').exists():
            # Stop may have removed some since the last write.
            self.containers = self.remaining()
        if positions is None:
            positions = [by_id[rank['node_id']] for rank in plan['ranks']]
        made = []
        for rank, position in enumerate(positions):
            if ranks is not None and rank not in ranks:
                continue
            tags = dict(rank_spec(plan, rank)['labels'], **(labels or {}))
            tags = {key: value for key, value in tags.items() if value is not None}
            container = {'node': self.node_id(position), 'name': name or plan['container_name'],
                         'id': hashlib.sha256(f"{plan['plan_id']}:{rank}:{position}:{len(self.containers)}".encode())
                         .hexdigest(), 'labels': tags, 'state': state, 'image': 'example/image@sha256:' + 'd' * 64,
                         'cmd': ['--port', str(plan['port'])]}
            self.containers.append(container)
            made.append(container)
        self.write()
        return made

    def write(self):
        (self.root / 'containers.json').write_text(json.dumps(self.containers))

    def remaining(self):
        return json.loads((self.root / 'containers.json').read_text())

    def removed(self):
        path = self.root / 'mutations.jsonl'
        return [tuple(json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def recorded(self, plan):
        row = self.store.get('services', plan['service_id'])
        return row is not None and row['plan_id'] == plan['plan_id']

    def changed_membership(self):
        """A saved cluster with the nodes at positions 2 and 3 exchanged, for
        SERVICE_SWAP_AFTER_PROBE; returns its path."""
        from scripts import topology_manifest as tm
        nodes = [dict(node) for node in self.nodes]
        nodes[2], nodes[3] = dict(nodes[3], rank=2), dict(nodes[2], rank=3)
        topology = dict(self.topology, nodes=nodes)
        topology['topology_id'] = tm.topology_digest(topology)
        path = self.root / 'changed-topology.json'
        path.write_text(json.dumps(topology))
        Path(str(path) + '.ssh-config').write_text(tm.render_ssh_config_text(topology,
                                                                              topology_path=str(self.root / 'topology.json')))
        return path

    # -- Start ----------------------------------------------------------------

    def install(self):
        """A copy of the Stack in the fixture whose image, memory, model-file
        and post-health checks are doubles (CHECK, OBSERVE), with START_ENV
        for prepared files, ports and preflight's node probe, and rdma and ip
        doubles for preflight on this node. Start runs the real up.sh,
        launcher and preflight from it. Returns the copy's root."""
        import shutil
        stack = self.root / 'stack'
        if stack.exists():
            return stack
        stack.mkdir()
        for name in ('scripts', 'cluster', 'model_library', 'release_spec', 'platforms', 'serving_guard'):
            shutil.copytree(ROOT / name, stack / name, ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('pulsar', 'serve.sh'):
            shutil.copy2(ROOT / name, stack / name)
        for name in ('check-image.sh', 'check-weights.sh', 'check-memory.sh', 'sync-image.sh'):
            (stack / 'scripts' / name).write_text(CHECK)
        (stack / 'scripts' / 'observe-serving.sh').write_text(OBSERVE)
        # The SSH configuration names the helper that reads known hosts; from
        # here on every command runs from the copy.
        from scripts import topology_manifest as tm
        self.fixture.config.write_text(tm.render_ssh_config_text(
            self.topology, topology_path=str(self.fixture.path),
            tool_path=str(stack / 'scripts' / 'topology_manifest.py')))
        (self.root / 'prepared.py').write_text(PREPARED)
        (self.root / 'start-env.sh').write_text(START_ENV)
        for name, body in (('curl', CURL), ('rdma', RDMA), ('ip', IP)):
            (self.root / 'bin' / name).write_text(body)
            (self.root / 'bin' / name).chmod(0o755)
        # The RDMA device preflight looks for on this node, in the fixture.
        (self.root / 'infiniband').write_text('')
        return stack

    def start(self, spec_id, *args, **env):
        """`./pulsar start SPEC_ID ARGS` from the installation copy."""
        stack = self.install()
        environment = {**self.env, 'SERVICE_STACK': str(stack), 'BASH_ENV': str(self.root / 'start-env.sh'),
                       'WAIT_ATTEMPTS': '2', 'WAIT_SECONDS': '0', 'VLLM_IMAGE_MAINLINE': 'example/image',
                       'VLLM_EXTRA_ARGS': '', 'EXTRA_ENV': '', 'PULSAR_START_BLOCKERS_FILE':
                       str(self.root / 'blockers.jsonl'), **env}
        return subprocess.run([str(stack / 'pulsar'), 'start', spec_id, *args], env=environment, text=True,
                              capture_output=True, timeout=180, cwd=self.root)

    def status(self, spec_id, *args, **env):
        """`./pulsar status SPEC_ID ARGS` from the installation copy, with the
        real observe-serving.sh in place of its stub for this run. Status's
        own /health request answers from HEALTH_DOUBLE, not the network."""
        import shutil
        stack = self.install()
        doubles = self.root / 'python-doubles'
        doubles.mkdir(exist_ok=True)
        (doubles / 'sitecustomize.py').write_text(HEALTH_DOUBLE)
        observe = stack / 'scripts' / 'observe-serving.sh'
        shutil.copy2(ROOT / 'scripts' / 'observe-serving.sh', observe)
        environment = {**self.env, 'SERVICE_STACK': str(stack), 'BASH_ENV': str(self.root / 'start-env.sh'),
                       'VLLM_IMAGE_MAINLINE': 'example/image', 'VLLM_EXTRA_ARGS': '', 'EXTRA_ENV': '',
                       'PYTHONPATH': str(doubles), **env}
        try:
            return subprocess.run([str(stack / 'pulsar'), 'status', spec_id, *args], env=environment, text=True,
                                  capture_output=True, timeout=180, cwd=self.root)
        finally:
            observe.write_text(OBSERVE)

    def calls(self):
        """Every docker call [node, args], SSH command [node, command] and curl
        URL, in the order each tool logged it."""
        def lines(name):
            path = self.root / name
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return {'docker': lines('docker-calls.jsonl'), 'ssh': lines('ssh-calls.jsonl'),
                'runs': lines('runs.jsonl'), 'curl': [url for url, _ in lines('curl-calls.jsonl')],
                'curl_args': [args for _, args in lines('curl-calls.jsonl')],
                'checks': (self.root / 'checks.log').read_text().splitlines()
                if (self.root / 'checks.log').exists() else []}

    def trace(self):
        """The ordered trace.log lines: docker work as "node-N what [over ssh]",
        "curl URL" and "port P on HOST"."""
        path = self.root / 'trace.log'
        return path.read_text().splitlines() if path.exists() else []

    def forget_calls(self):
        for name in ('docker-calls.jsonl', 'ssh-calls.jsonl', 'runs.jsonl', 'curl-calls.jsonl', 'checks.log',
                     'trace.log', 'prepared-readings', 'argv-builds',
                     'mutations.jsonl', 'blockers.jsonl'):
            (self.root / name).unlink(missing_ok=True)

    def position_of(self, node_id):
        return next(index for index, node in enumerate(self.nodes) if node['node_id'] == node_id)

    # -- the commands ---------------------------------------------------------

    def pulsar(self, *args, **env):
        """`./pulsar ARGS`, from the installation copy once Start installed it."""
        stack = self.root / 'stack'
        command = stack / 'pulsar' if stack.exists() else ROOT / 'pulsar'
        return subprocess.run([str(command), *args], env={**self.env, **env}, text=True,
                              capture_output=True, timeout=120)

    def script(self, path, *args, **env):
        return subprocess.run([str(ROOT / path), *args], env={**self.env, **env}, text=True,
                              capture_output=True, timeout=120)

    def bash(self, body, **env):
        """BODY after lib.sh, as a stop path runs it."""
        return subprocess.run(['bash', '-c', f". '{ROOT}/scripts/lib.sh'\n{body}"], env={**self.env, **env},
                              text=True, capture_output=True, timeout=120)

    # -- inventory ------------------------------------------------------------

    def snapshot(self, *, profiles=None):
        """The raw inventory snapshot a live run collects from these doubles,
        built here so classification can be read without probing."""
        nodes = {}
        for index, node in enumerate(self.nodes):
            nodes[KEYS[index]] = dict(hostname=node['hostname'], node_id=node['node_id'], ssh_host=node['ssh_host'],
                                      control_ip=node['control']['ip'], topology_index=index, local=index == 0,
                                      confirmed=True, mem_available_gib=100.0, mem_total_gib=121.0,
                                      mem_status='ok', mem_source='proc_meminfo', probe_status='ok',
                                      probe_reason=None)
        key = {node['node_id']: KEYS[index] for index, node in enumerate(self.nodes)}
        containers = [dict(node=key[c['node']], id=c['id'], name=c['name'], running=c['state'] == 'running',
                           status=c['state'], image=c['image'], cmd=c['cmd'], labels=c['labels'], host_pids=[])
                      for c in self.containers]
        raw = dict(profiles=profiles if profiles is not None else {}, topology_id=self.topology_id,
                   worker_ip=self.nodes[1]['ssh_host'], worker_status='ok', worker_reason=None, nodes=nodes,
                   containers=containers, gpu_processes=[])
        return raw

    def pages(self, spec_id, width=80):
        """The Running services card of SPEC_ID, its choices and Stop question,
        and Home's Serving row, from a live inventory on the doubles. The
        card's inventory age reads <age>."""
        from datetime import datetime
        from model_library.catalog import entries as catalog_entries
        from model_library.node_names import NodeNames
        from scripts import cluster_status, services_page
        result = self.pulsar('inventory', '--json')
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        inventory = json.loads(result.stdout)
        entries = catalog_entries(self.root, self.store)
        # The saved shape: a ring, or a full mesh for ServiceCluster(mesh=N).
        names = NodeNames({node['node_id']: node['hostname'] for node in self.nodes},
                          [node['node_id'] for node in self.nodes], self.topology['validation']['class'])
        page = services_page.Page(inventory, entries, names=names, topology=self.topology)
        environ = {'CLUSTER_TOPOLOGY_FILE': str(self.fixture.path),
                   'PULSAR_CLUSTER_CHANGES_FILE': str(self.root / 'state/cluster-changes.json'),
                   'PULSAR_SERVICE_INVENTORY_FILE': str(self.root / 'state/service-inventory.json'),
                   'PULSAR_STORAGE_BUDGET_FILE': str(self.root / 'state/storage-budget.json')}
        with mock.patch.dict(os.environ, environ):
            now = datetime.fromisoformat(inventory['generated_at'].replace('Z', '+00:00'))
            records = cluster_status.Records(self.root, now=now, environ={'PULSAR_COLD_ROOT': ''})
            home = cluster_status.serving_rows(records, entries, width - cluster_status.LABEL_WIDTH - 24)
            block = cluster_status.home_block(records, entries, width=width)
        return {'screen': page.screen(spec_id, width=width).replace(page.age, '<age>'),
                'choices': page.choices(spec_id), 'question': page.question(spec_id) or '', 'home': home,
                'home_block': block, 'page': page}

    def inventory(self, raw=None, *extra):
        path = self.root / 'raw-snapshot.json'
        path.write_text(json.dumps(raw if raw is not None else self.snapshot()))
        result = self.pulsar('inventory', '--from-fixture', str(path), '--json', *extra)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)


def replanned(plan, **changes):
    """PLAN with CHANGES and its digest recomputed, as a different launch plan."""
    plan = dict(plan, **changes)
    plan['plan_id'] = canonical_json_digest({key: value for key, value in plan.items() if key != 'plan_id'})
    return plan
