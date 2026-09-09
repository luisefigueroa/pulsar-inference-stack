"""Temporary rank fixtures and parameterized node/transport doubles."""
import base64
import json
import os
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


class Fixture:
    def __init__(self, root, nodes=2, enrolled=True):
        self.root = root
        self.probes = [node_probe(rank) for rank in range(nodes)]
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
            PYTHONDONTWRITEBYTECODE='1', GUM='0', COLUMNS='48', TERM='dumb')

    def run(self, *args, **kwargs):
        return subprocess.run([str(ROOT/'pulsar'), *args], env=self.env, text=True, capture_output=True, timeout=30, **kwargs)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


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
        return 0
    if tool == 'gum':
        lines = sys.stdin.read().splitlines()
        if 'choose' in args:
            header = args[args.index('--header')+1]
            choice = int(os.environ.get('TOPOLOGY_GUM_HOME' if header == 'Pulsar Inference Stack' else 'TOPOLOGY_GUM_CHOICE', '0'))
            print(lines[choice])
            return 0
        return int(os.environ.get('TOPOLOGY_CONFIRM_RC', '1'))
    rank, action = 0, tool
    if tool == 'ssh':
        separator = args.index('--'); alias = args[separator+1]
        mapping = json.loads((root/'map.json').read_text())
        if alias not in mapping:
            return 255
        rank = mapping[alias]
        if os.environ.get('TOPOLOGY_UNREACHABLE') == str(rank):
            return 255
        command = args[-1]
        if command.startswith('python3 '): action = 'python3'
        elif command.startswith('ping '): action = 'ping'
        elif command.startswith('docker ps '): action = 'docker'
        else: raise RuntimeError('unexpected remote operation')
    if action == 'python3':
        print((root/f'probe-{rank}.json').read_text())
    elif action == 'ping':
        return int(os.environ.get('TOPOLOGY_PING_RC', '0'))
    elif action == 'docker':
        counter = root / 'docker-count'
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        active_after = int(os.environ.get('TOPOLOGY_ACTIVE_AFTER', '1000000'))
        if os.environ.get('TOPOLOGY_ACTIVE') == str(rank) or count > active_after:
            print('fixture-container')
    return 0
