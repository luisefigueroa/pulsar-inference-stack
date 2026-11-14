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
# rsync splits -e itself (do_cmd in rsync's main.c): words at spaces; a single
# or double quote opens a quoted part, the same quote doubled inside it is a
# literal quote; a backslash is an ordinary character. Its first word must run.
command=args[args.index('-e')+1];words=[];i=0
while i<len(command):
 if command[i]==' ':i+=1;continue
 word='';quote=None
 while i<len(command) and (command[i]!=' ' or quote):
  c=command[i]
  if c in '"\'':
   if not quote:quote=c;i+=1;continue
   if c==quote:
    if command[i+1:i+2]==quote:word+=c;i+=2;continue
    quote=None;i+=1;continue
  word+=c;i+=1
 words.append(word)
if not os.access(words[0],os.X_OK):
 print(f'rsync: Failed to exec {words[0]}: No such file or directory (2)',file=sys.stderr);sys.exit(14)
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
# The remote command runs here, as the node would run it (the relay's streams, a proof's true).
import shlex,subprocess
raise SystemExit(subprocess.run(shlex.split(args[-1])).returncode if '--' in args else 0)
'''


class Harness:
    """model_transfer between two saved ranks of TOPOLOGY, with every external
    command a double. Returns the calls, in order, with the fixture's own paths
    written as <root>, and the files the destination received."""

    def __init__(self, test, topology, *, overrides=None, env=None, configured=False):
        self.test, self.topology, self.overrides, self.env = test, topology, overrides or {}, env or {}
        # CONFIGURED adds the enrolled cluster's SSH options, -F and ProxyCommand=none among them.
        self.configured = configured

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
{'CLUSTER_TOPOLOGY_SSH_TRUSTED=1 CLUSTER_SSH_CONFIG_FILE=' + shlex.quote(str(root / 'ssh-config')) + '; _pulsar_configure_topology_ssh' if self.configured else ''}
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
    # Streams run side by side: SSH calls in a run of them are compared in sorted order.
    ordered, run = [], []
    for call in shown + [None]:
        if call is not None and call[0] == 'ssh':
            run.append(call)
            continue
        ordered += sorted(run, key=json.dumps)
        run = []
        if call is not None:
            ordered.append(call)
    return ordered


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

    def test_a_relay_between_two_remote_nodes_of_a_mesh(self):
        result, calls, received = Harness(self, mesh_topology(3)).run(1, 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received, ['part-0.bin', 'part-1.bin', 'part-2.bin'])
        self.assertEqual(summary(calls), RELAY['MESH_RELAY'])

    def test_a_ring_neighbour_pull(self):
        result, calls, received = Harness(self, ring_topology(self)).run(3, 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary(calls), RING_NEIGHBOUR_PULL)


# Recorded from the transfer code before routes existed (tests/fixtures/transfer).
PINNED = json.loads((ROOT / 'tests/fixtures/transfer/copies-without-a-hop.json').read_text())
MESH_PUSH, RING_NEIGHBOUR_PUSH, RING_NEIGHBOUR_PULL = (
    PINNED['MESH_PUSH'], PINNED['RING_NEIGHBOUR_PUSH'], PINNED['RING_NEIGHBOUR_PULL'])
RELAY = json.loads((ROOT / 'tests/fixtures/transfer/relay-without-a-hop.json').read_text())


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


def proofs(calls):
    """The argv of each proof SSH ran through the route, in order."""
    return [call[1] for call in calls if call[0] == 'ssh' and call[1][-2:] in (['ring-bravo', 'true'],
            ['ring-echo', 'true'], ['ring-alpha', 'true'], ['ring-foxtrot', 'true'], ['ring-charlie', 'true'])]


def option(argv, name):
    """The value of the first -o NAME=… in ARGV, which is the one ssh uses."""
    for index, arg in enumerate(argv):
        if index and argv[index - 1] == '-o' and arg.startswith(name + '='):
            return arg.split('=', 1)[1]
    return None


class CopyThroughANeighbour(unittest.TestCase):
    """The opposite node of a four-node ring: one rsync over SSH, through rank 1."""

    def setUp(self):
        self.ring = ring_topology(self)
        self.nodes = self.ring['nodes']
        link = {tuple(item['ranks']): item['rails'][0] for item in self.ring['links']}
        self.cable_01, self.cable_12 = link[(0, 1)], link[(1, 2)]

    def test_a_push_proves_every_hop_then_copies_through_the_neighbour(self):
        result, calls, received = Harness(self, self.ring).run(0, 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received, ['part-0.bin', 'part-1.bin', 'part-2.bin'])
        self.assertIn('Copies to fixture-ring-echo pass through fixture-ring-bravo.', result.stderr)
        b01, b12 = self.cable_01, self.cable_12
        # Route checks over control SSH, both ends of each cable, in hop order.
        self.assertEqual([call for call in calls if call[0] in ('ip', 'ssh_node') and 'route' in str(call)], [
            ['ip', 0, ['-j', 'route', 'get', b01['b']['ip']]],
            ['ssh_node', 1, f"ip -j route get {b01['a']['ip']}"],
            ['ssh_node', 1, f"ip -j route get {b12['b']['ip']}"],
            ['ssh_node', 2, f"ip -j route get {b12['a']['ip']}"]])
        first, second = proofs(calls)
        # This machine to bravo, on their cable, from this machine's own address on it.
        self.assertEqual(option(first, 'HostName'), b01['b']['ip'])
        self.assertEqual(option(first, 'HostKeyAlias'), 'ring-bravo')
        self.assertEqual(option(first, 'BindAddress'), b01['a']['ip'])
        self.assertIsNone(option(first, 'ProxyCommand'))
        # Echo through bravo: bravo opens the connection to echo's address on their cable.
        self.assertEqual(option(second, 'HostName'), b12['b']['ip'])
        self.assertEqual(option(second, 'HostKeyAlias'), 'ring-echo')
        self.assertIsNone(option(second, 'BindAddress'))
        inner = shlex.split(option(second, 'ProxyCommand'))
        self.assertEqual(inner[-3:], ['-W', f"{b12['b']['ip']}:%p", 'ring-bravo'])
        # The proxy reaches bravo exactly as the first proof did, then forwards to echo.
        self.assertEqual(inner[1:-3], first[:-4])
        for argv in (first, second, inner):
            self.assertEqual(option(argv, 'ForwardAgent'), 'no')
            self.assertEqual(option(argv, 'StrictHostKeyChecking'), 'yes')
            self.assertNotIn('-A', argv)
            self.assertFalse([arg for arg in argv if '192.0.2.' in arg], argv)
        # One rsync per stream over the wrapper that reaches echo; no control address anywhere.
        copies = [call[1] for call in calls if call[0] == 'rsync']
        self.assertTrue(copies)
        for args in copies:
            # shell_join_q leaves a plain path as it is: rsync reads it as one word (its double checks).
            self.assertRegex(args[args.index('-e') + 1], r"^/[A-Za-z0-9_./-]*/route/route-ssh-1$")
            self.assertEqual(args[-1].split(':', 1)[0], 'ring-echo')

    def test_a_temporary_folder_rsync_cannot_name_in_its_command_is_refused_plainly(self):
        # rsync splits -e at spaces and quotes and ignores the backslashes that
        # shell_join_q writes, so a wrapper in this folder could not run.
        odd = Path(self.enterContext(tempfile.TemporaryDirectory())) / "pulsar a b'c"
        odd.mkdir()
        result, calls, received = Harness(self, self.ring, env={'TMPDIR': str(odd)}).run(0, 2)
        self.assertEqual(result.returncode, 1, result.stderr)
        words = ' '.join(result.stderr.split())
        self.assertIn(f'the copy route cannot use the temporary folder {odd}/pulsar-transfer.', words)
        self.assertIn('rsync cannot run a program whose path holds a space, a quote or another character the '
                      'shell must escape; set TMPDIR to a folder without them; nothing was copied', words)
        self.assertEqual([call for call in calls if call[0] in ('ip', 'ssh', 'ssh_node', 'rsync')], [])
        self.assertEqual(received, [])
        self.assertEqual(list(odd.iterdir()), [])
        # A copy with no node in between keeps today's transfer, whatever the folder.
        result, calls, received = Harness(self, self.ring, env={'TMPDIR': str(odd)}).run(0, 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received, ['part-0.bin', 'part-1.bin', 'part-2.bin'])

    def test_a_pull_passes_through_the_same_neighbour(self):
        result, calls, received = Harness(self, self.ring).run(2, 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Copies from fixture-ring-echo pass through fixture-ring-bravo.', result.stderr)
        self.assertEqual(len(proofs(calls)), 2)

    def test_the_cluster_options_never_override_the_jump(self):
        result, calls, _ = Harness(self, self.ring, configured=True).run(0, 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        first, second = proofs(calls)
        for argv in (first, second):
            self.assertIn('-F', argv)
            self.assertIn('ProxyCommand=none', argv)
        # ssh keeps the first value: the jump's ProxyCommand comes before the cluster's none.
        self.assertLess(second.index(next(a for a in second if a.startswith('ProxyCommand=ssh') or
                                          a.startswith('ProxyCommand=<root>') or
                                          (a.startswith('ProxyCommand=') and a != 'ProxyCommand=none'))),
                        second.index('ProxyCommand=none'))
        self.assertEqual(option(second, 'ProxyCommand').split()[-3:-1], ['-W', f"{self.cable_12['b']['ip']}:%p"])
        self.assertEqual(option(first, 'ProxyCommand'), 'none')

    def test_a_neighbour_that_refuses_to_forward_stops_the_copy_and_says_so(self):
        result, calls, received = Harness(self, self.ring, env={'TRANSFER_SSH_REFUSE': 'ring-bravo'}).run(0, 2)
        self.assertNotEqual(result.returncode, 0)
        words = ' '.join(result.stderr.split())
        self.assertIn("the copy route is not proven: SSH to fixture-ring-echo through fixture-ring-bravo did not "
                      "connect: kex_exchange_identification: Connection closed by remote host. fixture-ring-bravo's "
                      'SSH server must allow it to forward the connection', words)
        self.assertIn('nothing was copied', words)
        self.assertEqual([call for call in calls if call[0] == 'rsync'], [])
        self.assertEqual(received, [])

    def test_a_hop_whose_route_leaves_its_cable_stops_before_any_ssh(self):
        wrong = {f"1:{self.cable_12['b']['ip']}": {'dev': 'mgmt0', 'prefsrc': '192.0.2.31'}}
        result, calls, received = Harness(self, self.ring, overrides=wrong).run(0, 2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"on fixture-ring-bravo, fixture-ring-echo's address {self.cable_12['b']['ip']}",
                      ' '.join(result.stderr.split()))
        self.assertEqual([call for call in calls if call[0] in ('ssh', 'rsync')], [])
        self.assertEqual(received, [])

    def test_a_six_node_ring_takes_two_jumps(self):
        ring = ring_topology(self, 6)
        result, calls, received = Harness(self, ring).run(0, 3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Copies to fixture-ring-alpha pass through fixture-ring-bravo and fixture-ring-echo.',
                      result.stderr)
        first, second, third = proofs(calls)
        link = {tuple(item['ranks']): item['rails'][0] for item in ring['links']}
        self.assertEqual([option(argv, 'HostName') for argv in (first, second, third)],
                         [link[(0, 1)]['b']['ip'], link[(1, 2)]['b']['ip'], link[(2, 3)]['b']['ip']])
        outer = shlex.split(option(third, 'ProxyCommand'))
        self.assertEqual(outer[-3:], ['-W', f"{link[(2, 3)]['b']['ip']}:%p", 'ring-echo'])
        # The inner ProxyCommand is written for the ssh that runs it: its token is escaped once more.
        nested = option(outer, 'ProxyCommand')
        self.assertEqual(shlex.split(nested)[-3:], ['-W', f"{link[(1, 2)]['b']['ip']}:%%p", 'ring-bravo'])


class RelayThroughANeighbour(unittest.TestCase):
    def test_a_relay_leg_without_a_shared_link_passes_through_a_neighbour(self):
        ring = ring_topology(self)
        result, calls, received = Harness(self, ring).run(1, 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received, ['part-0.bin', 'part-1.bin', 'part-2.bin'])
        self.assertIn('Copies to fixture-ring-echo pass through fixture-ring-bravo.', result.stderr)


class Dump(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('COPY_ROUTE_DUMP'), 'writes the recorded calls only when asked')
    def test_dump(self):
        out = {}
        target = Path(os.environ['COPY_ROUTE_DUMP']).resolve()
        # The pins are recorded once, from the code before routes; never over them.
        self.assertFalse(target.is_relative_to(ROOT), 'write the recording outside the checkout')
        for name, (topology, source, destination) in {
                'MESH_PUSH': (mesh_topology(3), 0, 2), 'RING_NEIGHBOUR_PUSH': (ring_topology(self), 0, 1),
                'RING_NEIGHBOUR_PULL': (ring_topology(self), 3, 0), 'MESH_RELAY': (mesh_topology(3), 1, 2)}.items():
            result, calls, _ = Harness(self, topology).run(source, destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            out[name] = summary(calls)
        Path(os.environ['COPY_ROUTE_DUMP']).write_text(json.dumps(out, indent=1, sort_keys=True) + '\n')


if __name__ == '__main__':
    unittest.main()
