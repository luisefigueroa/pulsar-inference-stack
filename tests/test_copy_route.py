"""Copies between nodes that share no link: the route, the SSH jump through a
neighbour, the proofs before the copy, and the copies that need no hop, which
must not change. Synthetic topologies, files and doubles only; nothing here
contacts a node or runs real SSH, rsync or ip."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'scripts')]
from release_spec import build_snapshot_manifest
from support.topology_fixture import Fixture, ring_probes, shuffled
from tests.test_transfer import topology as mesh_topology
from model_library.integrity import StorageError
from model_library.transfer import copy_route


def manifest_for(files):
    return build_snapshot_manifest(model_id='example/model', snapshot_revision='a' * 40, files=[
        dict(path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest()) for path, data in files.items()])


def ring_topology(test, count=4):
    """An enrolled ring membership, saved ranks in cable order from fixture-ring-dorado."""
    fixture = Fixture(Path(test.enterContext(tempfile.TemporaryDirectory())), probes=shuffled(ring_probes(count)))
    return fixture.topology


def routes(topology):
    """What `ip -j route get TARGET` answers on each node: for an address on one
    of the node's link subnets, that link's interface and the node's own address."""
    import ipaddress
    answers = {}
    for node in topology['nodes']:
        for link in node.get('rdma') or []:
            for cidr in link.get('cidrs') or []:
                own = ipaddress.ip_interface(cidr)
                for other in topology['nodes']:
                    for far in other.get('rdma') or []:
                        for far_cidr in far.get('cidrs') or []:
                            target = ipaddress.ip_interface(far_cidr)
                            if other is not node and target.network == own.network:
                                answers[f"{node['rank']}:{target.ip}"] = {'dev': link['netdev'], 'prefsrc': str(own.ip)}
    return answers


# The doubles log each call as one JSON line in calls.jsonl, in order.
RSYNC = r'''#!/usr/bin/env python3
import json,os,pathlib,shutil,sys
root=pathlib.Path(os.environ['TRANSFER_FIXTURE']);args=sys.argv[1:]
with (root/'calls.jsonl').open('a') as log: log.write(json.dumps(['rsync',args])+'\n')
listfile=pathlib.Path(next(a.split('=',1)[1] for a in args if a.startswith('--files-from=')))
source,dest=args[-2:]
source=source.split(':',1)[1] if not source.startswith('/') else source
dest=dest.split(':',1)[1] if not dest.startswith('/') else dest
for name in listfile.read_bytes().split(b'\0'):
 if name:
  target=pathlib.Path(dest)/name.decode();target.parent.mkdir(parents=True,exist_ok=True)
  shutil.copyfile(pathlib.Path(source)/name.decode(),target)
'''
IP = r'''#!/usr/bin/env python3
import json,os,pathlib,sys
root=pathlib.Path(os.environ['TRANSFER_FIXTURE']);args=sys.argv[1:]
with (root/'calls.jsonl').open('a') as log: log.write(json.dumps(['ip',0,args])+'\n')
answer=json.loads((root/'routes.json').read_text()).get('0:'+args[-1])
if answer is None: sys.exit(2)
print(json.dumps([answer]))
'''
# The SSH double runs a short proof command through a route: it answers when
# the jump is allowed (TRANSFER_SSH_REFUSE names a machine that refuses it).
SSH = r'''#!/usr/bin/env python3
import json,os,pathlib,sys
root=pathlib.Path(os.environ['TRANSFER_FIXTURE']);args=sys.argv[1:]
with (root/'calls.jsonl').open('a') as log: log.write(json.dumps(['ssh',args])+'\n')
refuse=os.environ.get('TRANSFER_SSH_REFUSE','')
if refuse and any(refuse in a for a in args if a.startswith('ProxyCommand=')):
 print('channel 0: open failed: administratively prohibited: open failed',file=sys.stderr)
 print('kex_exchange_identification: Connection closed by remote host',file=sys.stderr);sys.exit(255)
sys.exit(0)
'''


class Harness:
    """model_transfer between two saved ranks of TOPOLOGY, with every external
    command a double. Returns the calls, in order, with the fixture's own paths
    written as <root>, and the files the destination received."""

    def __init__(self, test, topology, *, overrides=None, env=None):
        self.test, self.topology, self.overrides, self.env = test, topology, overrides or {}, env or {}

    def run(self, source_rank, destination_rank):
        root = Path(self.test.enterContext(tempfile.TemporaryDirectory()))
        (root / 'topology.json').write_text(json.dumps(self.topology))
        answers = {**routes(self.topology), **self.overrides}
        (root / 'routes.json').write_text(json.dumps(answers))
        source = root / 'source'
        source.mkdir()
        contents = {f'part-{index}.bin': str(index).encode() * 10 for index in range(3)}
        for name, data in contents.items():
            (source / name).write_bytes(data)
        (root / 'manifest.json').write_text(json.dumps(manifest_for(contents)))
        destination = root / ('.pending-' + 'b' * 32) / 'snapshots' / ('a' * 40)
        destination.mkdir(parents=True)
        for name, body in (('rsync', RSYNC), ('ip', IP), ('ssh', SSH)):
            (root / name).write_text(body)
            (root / name).chmod(0o755)
        nodes = self.topology['nodes']
        hosts = ' '.join(shlex.quote(node['ssh_host']) for node in nodes)
        names = ' '.join(shlex.quote(node['hostname']) for node in nodes)
        body = f'''
set -euo pipefail
. {shlex.quote(str(ROOT / 'scripts/lib.sh'))}
. {shlex.quote(str(ROOT / 'scripts/model-library-common.sh'))}
. {shlex.quote(str(ROOT / 'scripts/model-transfer.sh'))}
CLUSTER_TOPOLOGY_FILE={shlex.quote(str(root / 'topology.json'))}
CLUSTER_TOPOLOGY_ID={self.topology['topology_id']}
CLUSTER_TOPOLOGY_COUNT={len(nodes)}
CLUSTER_NODE_SSH_HOSTS=({hosts})
CLUSTER_NODE_HOSTNAMES=({names})
require_topology_ssh_trust() {{ :; }}
model_node() {{ printf '%s' "$2" | python3 -m model_library.node; }}
ssh_node() {{
  python3 -c 'import json,sys; open(sys.argv[1],"a").write(json.dumps(["ssh_node",int(sys.argv[2]),sys.argv[3]])+"\\n")' \\
    "$TRANSFER_FIXTURE/calls.jsonl" "$1" "$2"
  if [[ "$2" = ip* ]]; then
    python3 -c 'import json,sys; a=json.load(open(sys.argv[1])).get(sys.argv[2]); print(json.dumps([a])) if a else sys.exit(2)' \\
      "$TRANSFER_FIXTURE/routes.json" "$1:${{2##* }}"
  else bash -c "$2"; fi
}}
model_transfer {source_rank} {shlex.quote(str(source if source_rank == 0 else source))} {destination_rank} {shlex.quote(str(destination))} "$(cat {shlex.quote(str(root / 'manifest.json'))})"
'''
        environment = {**os.environ, 'TRANSFER_FIXTURE': str(root), 'PULSAR_RSYNC': str(root / 'rsync'),
                       'PULSAR_IP': str(root / 'ip'), 'PULSAR_SSH': str(root / 'ssh'), **self.env}
        result = subprocess.run(['bash', '-c', body], env=environment, cwd=ROOT, text=True, capture_output=True,
                                timeout=120)
        log = root / 'calls.jsonl'
        calls = [json.loads(line.replace(str(root), '<root>')) for line in log.read_text().splitlines()] \
            if log.exists() else []
        received = sorted(path.name for path in destination.rglob('*') if path.is_file())
        return result, calls, received


def summary(calls):
    """The route checks, proofs and copies in order, without the per-stream file lists."""
    shown = []
    for call in calls:
        if call[0] == 'rsync':
            args = [re.sub(r'=[^=]*/pulsar-transfer\.\w+/stream-\d+\.list$', '=<temp>/stream-N.list', arg)
                    for arg in call[1]]
            if ['rsync', args] not in shown:
                shown.append(['rsync', args])
        else:
            shown.append(call)
    return shown


class PinnedCopiesWithoutAHop(unittest.TestCase):
    """The argument lists, route checks and their order of a copy between nodes
    that share a link, recorded from the transfer code before routes existed."""

    def test_a_mesh_copy(self):
        result, calls, received = Harness(self, mesh_topology(3)).run(0, 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received, ['part-0.bin', 'part-1.bin', 'part-2.bin'])
        self.assertEqual(summary(calls), MESH_PUSH)

    def test_a_ring_neighbour_copy(self):
        result, calls, received = Harness(self, ring_topology(self)).run(0, 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary(calls), RING_NEIGHBOUR_PUSH)

    def test_a_ring_neighbour_pull(self):
        result, calls, received = Harness(self, ring_topology(self)).run(3, 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary(calls), RING_NEIGHBOUR_PULL)


# Recorded from the transfer code before routes existed (tests/fixtures/transfer).
PINNED = json.loads((ROOT / 'tests/fixtures/transfer/copies-without-a-hop.json').read_text())
MESH_PUSH, RING_NEIGHBOUR_PUSH, RING_NEIGHBOUR_PULL = (
    PINNED['MESH_PUSH'], PINNED['RING_NEIGHBOUR_PUSH'], PINNED['RING_NEIGHBOUR_PULL'])


class TheRoute(unittest.TestCase):
    """The shortest chain of cables between two nodes; on a tie, through the lower ranks."""

    def test_neighbours_need_no_hop(self):
        ring = ring_topology(self)
        self.assertEqual(copy_route(ring, 0, 1), [0, 1])
        self.assertEqual(copy_route(ring, 3, 0), [3, 0])

    def test_the_opposite_node_of_a_four_ring_is_one_hop_through_rank_1(self):
        ring = ring_topology(self)
        self.assertEqual(copy_route(ring, 0, 2), [0, 1, 2])
        self.assertEqual(copy_route(ring, 2, 0), [2, 1, 0])

    def test_a_six_node_ring_takes_two_hops(self):
        ring = ring_topology(self, 6)
        self.assertEqual(copy_route(ring, 0, 3), [0, 1, 2, 3])
        self.assertEqual(copy_route(ring, 3, 0), [3, 2, 1, 0])
        self.assertEqual(copy_route(ring, 0, 4), [0, 5, 4])
        self.assertEqual(copy_route(ring, 1, 4), [1, 0, 5, 4])

    def test_the_tie_rule_gives_the_same_nodes_both_ways(self):
        ring = ring_topology(self)
        # From rank 1 to rank 3 the chains pass through rank 0 or rank 2: rank 0 wins.
        self.assertEqual(copy_route(ring, 1, 3), [1, 0, 3])
        self.assertEqual(copy_route(ring, 3, 1), [3, 0, 1])

    def test_a_full_mesh_never_needs_a_hop(self):
        mesh = mesh_topology(3)
        for first in range(3):
            for second in range(3):
                if first != second:
                    self.assertEqual(copy_route(mesh, first, second), [first, second])

    def test_no_chain_is_refused_with_the_two_machines(self):
        ring = ring_topology(self)
        broken = {**ring, 'links': [link for link in ring['links'] if link['ranks'] not in ([1, 2], [2, 3])]}
        with self.assertRaisesRegex(StorageError, 'no chain of cables joins fixture-ring-dorado and fixture-ring-echo'):
            copy_route(broken, 0, 2)
        with self.assertRaises(StorageError):
            copy_route(ring, 1, 1)


class Dump(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('COPY_ROUTE_DUMP'), 'writes the recorded calls only when asked')
    def test_dump(self):
        out = {}
        for name, (topology, source, destination) in {
                'MESH_PUSH': (mesh_topology(3), 0, 2), 'RING_NEIGHBOUR_PUSH': (ring_topology(self), 0, 1),
                'RING_NEIGHBOUR_PULL': (ring_topology(self), 3, 0)}.items():
            result, calls, _ = Harness(self, topology).run(source, destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            out[name] = summary(calls)
        Path(os.environ['COPY_ROUTE_DUMP']).write_text(json.dumps(out, indent=1, sort_keys=True) + '\n')


if __name__ == '__main__':
    unittest.main()
