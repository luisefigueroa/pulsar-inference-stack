"""Temporary rank fixtures and parameterized node/transport doubles."""
import base64
import ipaddress
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import topology_manifest as tm


def node_probe(rank):
    algorithm = b'ssh-ed25519'
    key = base64.b64encode(len(algorithm).to_bytes(4, 'big') + algorithm + f'fixture-key-{rank}'.encode()).decode()
    return dict(probe_schema_version=2, local=rank == 0, ssh_host='local' if rank == 0 else f'alias-{rank}',
        node_id=f'fixture-node-{rank}', hostname=f'fixture-rank-{rank}', arch='aarch64', gpu='NVIDIA GB10',
        docker_ok=True, docker_nvidia=True, qualified=True, reject_reasons=[],
        control=dict(interface='mgmt0', ip=f'192.0.2.{10+rank}'),
        rdma=[dict(hca='roce0', netdev='fabric0', cidrs=[f'198.51.100.{10+rank}/24'])],
        ssh_host_keys=[dict(algorithm='ssh-ed25519', public_key=key)])


# Ring fixtures. Each node has two fabric ports; each port is two network
# devices with an RDMA adapter each. Port 0 of node i is cabled to port 1 of
# node i+1, and the last node's port 0 closes the loop to node 0's port 1. A
# cable carries two links, each on its own small subnet, so opposite nodes
# share no subnet. Hostnames do not sort in cable order, so rank order is
# exercised rather than inherited from names.
RING_NAMES = ('dorado', 'bravo', 'echo', 'alpha', 'foxtrot', 'charlie')
RING_PORTS = {0: (('rocep0a', 'fab0a'), ('rocep0b', 'fab0b')),
              1: (('rocep1a', 'fab1a'), ('rocep1b', 'fab1b'))}


def ring_subnet(index):
    """Link subnet INDEX: a /28 from the documentation ranges, 32 in all."""
    block, base = (index, '198.51.100') if index < 16 else (index - 16, '203.0.113')
    if not 0 <= block < 16:
        raise ValueError('the ring fixture has 32 link subnets')
    return base, block * 16


def ring_probes(count, *, local=0, dead_cables=(), single_link_cables=(), extra_links=()):
    """Probes of COUNT nodes cabled in a loop, listed in cable order.

    Cable c joins node c (port 0) to node c+1 (port 1); cable COUNT-1 closes
    the loop. DEAD_CABLES carry no link (both ends show no adapter on them);
    SINGLE_LINK_CABLES carry only their first link. EXTRA_LINKS are pairs of
    nodes given one more shared subnet, a stray link beside the ring. LOCAL is
    the node that runs discovery. An open chain is a loop with its closing
    cable dead."""
    probes = []
    for node in range(count):
        name = RING_NAMES[node]
        algorithm = b'ssh-ed25519'
        key = base64.b64encode(len(algorithm).to_bytes(4, 'big') + algorithm
                               + f'fixture-ring-key-{node}'.encode()).decode()
        probes.append(dict(probe_schema_version=2, local=node == local,
                           ssh_host='local' if node == local else f'ring-{name}',
                           node_id=f'fixture-ring-node-{node}', hostname=f'fixture-ring-{name}',
                           arch='aarch64', gpu='NVIDIA GB10', docker_ok=True, docker_nvidia=True,
                           qualified=True, reject_reasons=[],
                           control=dict(interface='mgmt0', ip=f'192.0.2.{30 + node}'),
                           rdma=[], ssh_host_keys=[dict(algorithm='ssh-ed25519', public_key=key)]))
    for cable in range(count):
        if cable in dead_cables:
            continue
        ends = ((cable, 0, 1), ((cable + 1) % count, 1, 2))
        for link in range(1 if cable in single_link_cables else 2):
            base, offset = ring_subnet(2 * cable + link)
            for node, port, host in ends:
                hca, netdev = RING_PORTS[port][link]
                probes[node]['rdma'].append(dict(hca=hca, netdev=netdev, cidrs=[f'{base}.{offset + host}/28']))
    for index, (first, second) in enumerate(extra_links):
        base, offset = ring_subnet(2 * count + index)
        for node, host in ((first, 1), (second, 2)):
            probes[node]['rdma'].append(dict(hca=f'rocex{index}', netdev=f'fabx{index}',
                                             cidrs=[f'{base}.{offset + host}/28']))
    for probe in probes:
        probe['rdma'].sort(key=lambda item: item['hca'])
    return probes


def shuffled(probes):
    """The local probe first, then the others in an order that is not cable order."""
    local = [probe for probe in probes if probe['local']]
    others = sorted((probe for probe in probes if not probe['local']), key=lambda probe: probe['hostname'])
    return local + others[1::2] + others[0::2]


def as_mesh(probes):
    """PROBES with their identities kept and one adapter each on a shared /24: a full mesh."""
    mesh = []
    for index, probe in enumerate(probes):
        probe = json.loads(json.dumps(probe))
        probe['rdma'] = [dict(hca='roce0', netdev='fabric0', cidrs=[f'203.0.113.{10 + index}/24'])]
        mesh.append(probe)
    return mesh


def use_probes(fixture, probes):
    """Make FIXTURE's nodes answer as PROBES from now on: the cluster changed after it was saved."""
    for index, probe in enumerate(probes):
        (fixture.root/f'probe-{index}.json').write_text(json.dumps(probe))
    (fixture.root/'map.json').write_text(json.dumps({probe['ssh_host']: index for index, probe in enumerate(probes)}))


def answer_on_fabric_addresses(fixture, per_machine=2):
    """Let each other machine also answer SSH on its first fabric addresses, as
    machines do on the real ring; returns the addresses, for CLUSTER_CANDIDATES."""
    mapping = json.loads((fixture.root/'map.json').read_text())
    added = []
    for alias, index in list(mapping.items()):
        probe = json.loads((fixture.root/f'probe-{index}.json').read_text())
        if probe['local']:
            continue
        for link in probe['rdma'][:per_machine]:
            address = link['cidrs'][0].split('/')[0]
            mapping[address] = index
            added.append(address)
    (fixture.root/'map.json').write_text(json.dumps(mapping))
    return added


def prerequisites_answer(rank, *, hf='ok', login='accepted', rsync=True):
    """A `probe-node.py --prerequisites-only` answer for fixture rank RANK."""
    return dict(probe_schema_version=1, kind='pulsar-node-prerequisites', local=rank == 0,
                ssh_host='local' if rank == 0 else f'alias-{rank}', hostname=f'fixture-rank-{rank}',
                prerequisites=dict(python=dict(version='3.12.3', ok=True), flock=True, rsync=rsync,
                                   hf=dict(state=hf, version='1.0.0' if hf == 'ok' else '', login=login,
                                           legacy_cli=False)))


class Fixture:
    def __init__(self, root, nodes=2, enrolled=True, probes=None):
        """PROBES, when given, replace the mesh probes; the local probe comes first."""
        self.root = root
        self.probes = [node_probe(rank) for rank in range(nodes)] if probes is None else list(probes)
        if not self.probes[0]['local']:
            raise ValueError('the local probe must be listed first')
        self.files = []
        for rank, probe in enumerate(self.probes):
            path = root / f'probe-{rank}.json'
            path.write_text(json.dumps(probe)); self.files.append(str(path))
        self.topology = tm.mark_verified(tm.assemble(self.files, None))['topology']
        if enrolled:
            self.topology = tm.enroll_ssh_trust(self.topology, self.probes)
        self.path = root / 'topology.json'
        self.path.write_text(json.dumps(self.topology))
        self.config = root / 'ssh-config'
        if enrolled:
            self.config.write_text(tm.render_ssh_config_text(self.topology, topology_path=str(self.path)))
        (root / 'map.json').write_text(json.dumps({probe['ssh_host']: rank for rank, probe in enumerate(self.probes)}))
        self.log = root / 'calls.jsonl'
        (root / 'local-identity-probe.py').write_text(
            'import os\nfrom pathlib import Path\nprint((Path(os.environ["TOPOLOGY_FIXTURE"])/"probe-0.json").read_text())\n')
        binary = root / 'bin'; binary.mkdir()
        for name in ('ssh', 'docker', 'ping', 'avahi-browse', 'python3', 'gum'):
            executable = binary / name
            executable.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ROOT / "tests")!r})\nfrom support.topology_fixture import double_main\nraise SystemExit(double_main())\n')
            executable.chmod(0o755)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('PULSAR_', 'CLUSTER_', 'DETECT_FABRIC_', '_PULSAR_'))
               and key not in ('BASH_ENV', 'ENV', 'PYTHONPATH', 'GUM_BIN')}
        self.env = dict(env, PATH=str(binary)+os.pathsep+os.environ['PATH'],
            TOPOLOGY_FIXTURE=str(root), CLUSTER_TOPOLOGY_FILE=str(self.path),
            CLUSTER_SSH_CONFIG_FILE=str(self.config), PULSAR_SSH=str(binary/'ssh'),
            PULSAR_DOCKER=str(binary/'docker'), PULSAR_SELFTEST='1',
            PULSAR_MODEL_LIBRARY_DIR=str(root/'model-library'),
            PULSAR_COLD_STORAGE_TEST_DOTENV=str(root/'absent-env'),
            # Saved observations and backups stay in the fixture, never the checkout.
            PULSAR_CLUSTER_CHANGES_FILE=str(root/'state/cluster-changes.json'),
            PULSAR_SERVICE_INVENTORY_FILE=str(root/'state/service-inventory.json'),
            PULSAR_STORAGE_BUDGET_FILE=str(root/'state/storage-budget.json'),
            PULSAR_CLUSTER_BACKUP_DIR=str(root/'state/cluster-backups'),
            PULSAR_OVERLAY_PATH=str(root/'overlay.json'),
            PYTHONDONTWRITEBYTECODE='1', COLUMNS='48', TERM='dumb')

    def run(self, *args, **kwargs):
        return subprocess.run([str(ROOT/'pulsar'), *args], env=self.env, text=True, capture_output=True, timeout=30, **kwargs)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


def ping_double(root, index, words):
    """A ping from the node of probe INDEX: answers only when bound to the
    interface whose own subnet holds the target, and another probe owns that
    address there. TOPOLOGY_PING_RC fails every ping; TOPOLOGY_PING_FAIL lists
    target addresses whose link is down."""
    override = int(os.environ.get('TOPOLOGY_PING_RC', '0'))
    if override:
        return override
    if '-I' not in words:
        return 2
    interface, target = words[words.index('-I')+1], ipaddress.ip_address(words[-1])
    if str(target) in os.environ.get('TOPOLOGY_PING_FAIL', '').split(','):
        return 1
    probes = {int(path.stem.removeprefix('probe-')): json.loads(path.read_text()) for path in root.glob('probe-*.json')
              if path.stem.removeprefix('probe-').isdigit()}
    networks = [ipaddress.ip_interface(cidr).network for link in probes[index].get('rdma') or []
                if link['netdev'] == interface for cidr in link.get('cidrs') or []]
    for other, probe in probes.items():
        if other == index:
            continue
        for link in probe.get('rdma') or []:
            for cidr in link.get('cidrs') or []:
                address = ipaddress.ip_interface(cidr)
                if address.ip == target and address.network in networks:
                    return 0
    return 1


def double_main():
    root = Path(os.environ['TOPOLOGY_FIXTURE'])
    # This fixture is never allowed to operate outside its temporary directory.
    if not root.is_absolute() or not root.is_relative_to(Path('/tmp')):
        raise RuntimeError('fixture requires an absolute temporary root')
    tool, args = Path(sys.argv[0]).name, sys.argv[1:]
    if tool == 'python3' and args and args[0] == str(ROOT/'scripts/topology_ssh_trust.py'):
        # Keep the real trust checker, but its sys.executable local subprocess
        # must use a synthetic identity probe, never inspect this host's keys.
        args[args.index('--probe')+1] = str(root/'local-identity-probe.py')
    if tool == 'python3' and (not args or args[0] != str(ROOT/'scripts/probe-node.py')):
        os.execv(sys.executable, [sys.executable, *args])
    with (root/'calls.jsonl').open('a') as stream:
        stream.write(json.dumps([tool, args])+'\n')
    if tool == 'avahi-browse':
        with (root/'discovery-context.jsonl').open('a') as stream:
            stream.write(json.dumps({'setup_configure': os.environ.get('_PULSAR_SETUP_CONFIGURE')})+'\n')
        return 0
    if tool == 'gum':
        calls = [json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
        if args[:1] == ['choose']:
            lines = sys.stdin.read().splitlines()
            # The header's last line is the key hint; the rows above it are the
            # title, which scripts/ui.sh wraps to the terminal's width.
            def title(logged):
                return ' '.join(logged[logged.index('--header')+1].rsplit('\n', 1)[0].split())
            header = title(args)
            # A comma-separated choice list answers successive visits to one menu.
            choice_env = {'Pulsar Inference Stack': 'TOPOLOGY_GUM_HOME',
                          'Cluster': 'TOPOLOGY_GUM_CLUSTER',
                          'Help and troubleshooting': 'TOPOLOGY_GUM_HELP',
                          'Advanced: single operations': 'TOPOLOGY_GUM_ADVANCED',
                          'Review discovered machines': 'TOPOLOGY_GUM_REVIEW',
                          'Change membership': 'TOPOLOGY_GUM_CHANGE'}.get(header, 'TOPOLOGY_GUM_CHOICE')
            choices = os.environ.get(choice_env, '0').split(',')
            with (root/'menus.jsonl').open('a') as stream:
                stream.write(json.dumps([header, lines])+'\n')
            visits = sum(1 for name, logged in calls if name == 'gum' and logged[:1] == ['choose']
                         and title(logged) == header) - 1
            answer = choices[min(visits, len(choices)-1)]
            if answer in ('esc', 'ctrl-c'):
                return 130 if answer == 'ctrl-c' else 1
            print(lines[int(answer)])
            return 0
        if args[:1] == ['input']:
            answers = os.environ.get('TOPOLOGY_GUM_INPUT', '').split(',')
            visits = sum(1 for name, logged in calls if name == 'gum' and logged[:1] == ['input']) - 1
            answer = answers[min(visits, len(answers)-1)]
            if answer in ('esc', 'ctrl-c'):
                return 130 if answer == 'ctrl-c' else 1
            print(answer)
            return 0
        if args[:1] == ['style']:
            sys.stdout.write(sys.stdin.read())
            return 0
        if args[:1] == ['pager']:
            # The pager keeps what it was given; q returns 0.
            with (root/'pager.txt').open('a') as stream:
                stream.write(sys.stdin.read())
            return 0
        if args[:1] == ['spin']:
            return subprocess.call(args[args.index('--')+1:])
        # Confirm reads keys from the terminal, never stdin. A comma-separated
        # exit-status list answers successive confirmations.
        if os.environ.get('TOPOLOGY_CONCURRENT_SAVE') == '1' and args[-1].startswith('Save cluster membership with '):
            (root/'topology.json').write_bytes((root/'concurrent-topology.json').read_bytes())
        answers = os.environ.get('TOPOLOGY_CONFIRM_RC', '1').split(',')
        visits = sum(1 for name, logged in calls if name == 'gum' and logged[:1] == ['confirm']) - 1
        return int(answers[min(visits, len(answers)-1)])
    rank, action = 0, tool
    if tool == 'ssh':
        separator = args.index('--'); alias = args[separator+1]
        mapping = json.loads((root/'map.json').read_text())
        # An address that answers with a host key SSH here does not trust.
        if alias in os.environ.get('TOPOLOGY_UNTRUSTED', '').split(','):
            print(f'No ED25519 host key is known for {alias} and you have requested strict checking.\n'
                  'Host key verification failed.', file=sys.stderr)
            return 255
        if alias not in mapping:
            return 255
        rank = mapping[alias]
        if str(rank) in os.environ.get('TOPOLOGY_UNREACHABLE', '').split(','):
            return 255
        command = args[-1]
        if command.startswith('python3 '): action = 'python3'
        elif command.startswith('ping '): action = 'ping'
        elif command.startswith('docker ps '): action = 'docker'
        else: raise RuntimeError('unexpected remote operation')
    if action == 'python3':
        # A node that did not answer discovery but answers later queries.
        if (tool == 'ssh' and str(rank) in os.environ.get('TOPOLOGY_PROBE_UNREACHABLE', '').split(',')
                and '--identity-only' not in args[-1]):
            return 255
        if '--prerequisites-only' in (args[-1].split() if tool == 'ssh' else args):
            # A node's prerequisites: prerequisites-RANK.json when a test wrote
            # one, else every prerequisite in place. The Hub is never asked.
            path = root/f'prerequisites-{rank}.json'
            print(path.read_text() if path.exists() else json.dumps(prerequisites_answer(rank)))
            return 0
        text = (root/f'probe-{rank}.json').read_text()
        if tool == 'ssh':
            # A machine reached at another of its addresses reports that address,
            # as probe-node.py does with --ssh-host.
            probe = json.loads(text)
            if not probe.get('local') and probe.get('ssh_host') not in (None, alias):
                text = json.dumps(dict(probe, ssh_host=alias))
        print(text)
    elif action == 'ping':
        words = args if tool == 'ping' else [word for word in shlex.split(args[-1])[1:] if not word.startswith(('>', '2>'))]
        return ping_double(root, rank, words)
    elif action == 'docker':
        counter = root / 'docker-count'
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        active_after = int(os.environ.get('TOPOLOGY_ACTIVE_AFTER', '1000000'))
        if os.environ.get('TOPOLOGY_ACTIVE') == str(rank) or count > active_after:
            print('fixture-container')
    return 0
