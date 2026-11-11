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
from support.topology_fixture import Fixture, ring_probes, shuffled  # noqa: E402

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
    c = find(args[3])
    if args[2] == "{{.State.Status}}":
        print(c["state"])
    else:
        print(json.dumps({"id": c["id"], "name": "/" + c["name"], "labels": c["labels"]}))
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
if "/proc/meminfo" in command:
    print("100.00")
    sys.exit(0)
sys.exit(subprocess.call(["bash", "-c", command], env=dict(os.environ, SERVICE_NODE=node)))
'''

NVIDIA_SMI = '#!/bin/sh\nexit 0\n'


def recipe(nodes, variant=0, *, guarded=False):
    """A frozen spec on NODES nodes; VARIANT gives another spec of the same size,
    and GUARDED one that runs only as a guarded trial."""
    data = b'synthetic service weights'
    manifest = build_snapshot_manifest(model_id=MODEL_ID, snapshot_revision='c' * 40, files=[
        dict(path='weights.bin', size=len(data), sha256=hashlib.sha256(data).hexdigest())])
    draft = example(nodes)
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

    def __init__(self, root):
        self.root = Path(root)
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

    def spec(self, nodes, variant=0, *, catalog=True, guarded=False):
        spec, manifest = recipe(nodes, variant, guarded=guarded)
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

    # -- the commands ---------------------------------------------------------

    def pulsar(self, *args, **env):
        return subprocess.run([str(ROOT / 'pulsar'), *args], env={**self.env, **env}, text=True,
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
        from scripts import topology_manifest as tm
        result = self.pulsar('inventory', '--json')
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        inventory = json.loads(result.stdout)
        entries = catalog_entries(self.root, self.store)
        names = NodeNames({node['node_id']: node['hostname'] for node in self.nodes},
                          [node['node_id'] for node in self.nodes], tm.RING_CLASS)
        page = services_page.Page(inventory, entries, names=names)
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
