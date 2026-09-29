"""Launch help keeps image staging and service replacement explicit."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LaunchPermissions(unittest.TestCase):
    def help(self, path):
        result = subprocess.run(
            ['bash', str(path), '--help'], cwd=ROOT,
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_up_separates_yes_pull_and_replace(self):
        output = self.help(ROOT / 'scripts/up.sh')
        self.assertIn('--pull-image', output)
        self.assertIn('--replace', output)
        self.assertIn('--yes', output)
        self.assertIn('never implies', output)

    def test_low_level_launchers_expose_replace(self):
        self.assertIn('--replace', self.help(ROOT / 'serve.sh'))
        self.assertIn('--replace', self.help(ROOT / 'cluster/start-cluster.sh'))

    def test_single_node_existing_service_requires_replace_before_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'scripts').mkdir()
            shutil.copyfile(ROOT / 'serve.sh', root / 'serve.sh')
            marker = root / 'removed'
            (root / 'scripts/lib.sh').write_text(r'''
die() { echo "$*" >&2; exit 2; }
acquire_model_library_lifecycle_lock() { :; }
load_conf() { NODES=1; CONF_SOURCE=spec; MODEL=example/model; IMAGE=example/image; PORT=8000; SERVED_NAME=example; }
require_spec_launch_admission() { :; }
acquire_model_library_hot_lock() { :; }
resolve_spec_decode() { SPEC_DECODE_ENABLED=0; }
loaded_launch_contract_id() { printf '%064d\n' 0; }
model_source_kind() { echo hf; }
resolve_single_node_placement() { SINGLE_NODE_REMOTE=0; SINGLE_NODE_INDEX=0; SINGLE_NODE_KEY=head; SINGLE_NODE_HOSTNAME=fixture; SINGLE_NODE_TOPOLOGY_ID="$(printf '%064d' 0)"; }
load_cluster_topology() { CLUSTER_TOPOLOGY_ID="$(printf '%064d' 0)"; return 0; }
resolve_library_hot_for_profile() { LIBRARY_VIEW_CONTAINER_MODEL_PATH=/tmp/model; LIBRARY_VIEW_IDENTITY_STATUS=manifest-verified; LIBRARY_VIEW_REVISION=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; }
container_name_for() { echo fixture-container; }
write_launch_plan_file() { :; }
load_docker_argv_from_plan() { local -n out=$3; out=(docker run fixture); }
require_launch_image_check() { :; }
require_launch_memory_check() { :; }
port_free() { return 0; }
persist_launch_plan_file() { :; }
release_model_library_locks() { :; }
container_ownership_inspect_local() { return 0; }
single_node_display() { echo fixture; }
start_blocker() { echo "BLOCKED $1"; }
log() { echo "$*"; }
error_line() { echo "error: $*" >&2; }
remove_stack_owned_single_at_resolved_node() { touch "$REMOVE_MARKER"; return 2; }
PULSAR_MANAGED_LABEL=managed PULSAR_CONF_LABEL=conf PULSAR_RANK_LABEL=rank PULSAR_NODE_ID_LABEL=node
''')
            env={**os.environ,'REMOVE_MARKER':str(marker),'PYTHONDONTWRITEBYTECODE':'1'}
            refused=subprocess.run(
                ['bash',str(root/'serve.sh'),'a'*64],env=env,text=True,capture_output=True)
            self.assertNotEqual(refused.returncode,0)
            self.assertIn('--replace',refused.stderr)
            self.assertIn('BLOCKED service_exists',refused.stdout)
            self.assertFalse(marker.exists())
            replacing=subprocess.run(
                ['bash',str(root/'serve.sh'),'a'*64,'--replace'],env=env,
                text=True,capture_output=True)
            self.assertNotEqual(replacing.returncode,0)
            self.assertTrue(marker.exists(),replacing.stderr+replacing.stdout)

    def test_docker_run_failure_after_replacement_is_recorded(self):
        import json, re
        def real(name):
            text = (ROOT/'scripts/lib.sh').read_text()
            return re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M).group(0)
        note = re.search(r'^AFTER_REPLACE_NOTE=.*$', (ROOT/'scripts/lib.sh').read_text(), re.M).group(0)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'scripts').mkdir()
            shutil.copyfile(ROOT/'serve.sh', root/'serve.sh')
            shutil.copyfile(ROOT/'scripts/start_blockers.py', root/'scripts/start_blockers.py')
            docker = root/'docker'
            docker.write_text('#!/usr/bin/env bash\necho "docker: Error response from daemon: fixture failure" >&2\nexit 125\n')
            docker.chmod(0o700)
            (root/'scripts/lib.sh').write_text(r'''
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
die() { echo "error: $1" >&2; exit "${2:-1}"; }
acquire_model_library_lifecycle_lock() { :; }
load_conf() { NODES=1; CONF_SOURCE=spec; MODEL=example/model; IMAGE=example/image; PORT=8000; SERVED_NAME=example; }
require_spec_launch_admission() { :; }
acquire_model_library_hot_lock() { :; }
resolve_spec_decode() { SPEC_DECODE_ENABLED=0; }
loaded_launch_contract_id() { printf '%064d\n' 0; }
model_source_kind() { echo hf; }
resolve_single_node_placement() { SINGLE_NODE_REMOTE=0; SINGLE_NODE_INDEX=0; SINGLE_NODE_KEY=head; SINGLE_NODE_ID=node-0; SINGLE_NODE_HOSTNAME=spark-1; SINGLE_NODE_TOPOLOGY_ID="$(printf '%064d' 0)"; }
load_cluster_topology() { CLUSTER_TOPOLOGY_ID="$(printf '%064d' 0)"; return 0; }
resolve_library_hot_for_profile() { LIBRARY_VIEW_CONTAINER_MODEL_PATH=/tmp/model; LIBRARY_VIEW_IDENTITY_STATUS=manifest-verified; LIBRARY_VIEW_REVISION=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; }
container_name_for() { echo fixture-container; }
write_launch_plan_file() { printf '{"service_id": "%s"}\n' "$FIXTURE_SERVICE_ID" >"$1"; }
load_docker_argv_from_plan() { local -n out=$3; out=(docker run fixture); }
require_launch_image_check() { :; }
require_launch_memory_check() { :; }
port_free() { return 0; }
persist_launch_plan_file() { :; }
release_model_library_locks() { touch "$FIXTURE_DIR/released"; }
container_ownership_inspect_local() { return 0; }
remove_stack_owned_single_at_resolved_node() { return 0; }
single_node_display() { echo spark-1; }
log() { echo "$*"; }
warn() { echo "warning: $*" >&2; }
error_line() { echo "error: $*" >&2; }
PULSAR_MANAGED_LABEL=managed PULSAR_CONF_LABEL=conf PULSAR_RANK_LABEL=rank PULSAR_NODE_ID_LABEL=node
''' + note + '\n' + real('refuse_launch') + real('start_blocker') + real('launch_plan_service_id'))
            blockers = root/'blockers.jsonl'
            env = {**os.environ, 'PULSAR_DOCKER': str(docker), 'FIXTURE_DIR': str(root), 'FIXTURE_SERVICE_ID': '5e'*32,
                   'PULSAR_START_BLOCKERS_FILE': str(blockers), 'PYTHONDONTWRITEBYTECODE': '1'}
            env.pop('START_BLOCKER_PLACEMENT', None)
            result = subprocess.run(['bash', str(root/'serve.sh'), 'a'*64, '-d', '--replace'], env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('error: docker run failed on spark-1. The previous service was already removed, so '
                          'nothing is running for this spec now.', result.stderr)
            (record,) = [json.loads(line) for line in blockers.read_text().splitlines()]
            self.assertEqual((record['blocker'], record['stage'], record['node'], record['node_id'], record['service_id']),
                             ('container_start_failed', 'launch', 'spark-1', 'node-0', '5e'*32))
            self.assertEqual(record['fix'], './pulsar doctor')
            self.assertIn('(docker run exit 125)', record['message'])
            self.assertTrue(record['note'].endswith('nothing is running for this spec now.'))
            self.assertFalse((root/'released').exists())
            # In the foreground docker run returns when the container stops; its exit passes through.
            blockers.unlink()
            result = subprocess.run(['bash', str(root/'serve.sh'), 'a'*64, '--replace'], env=env, text=True,
                                    capture_output=True)
            self.assertEqual(result.returncode, 125, result.stderr)
            self.assertFalse(blockers.exists())

    def test_yes_does_not_imply_image_pull(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);scripts=root/'scripts';scripts.mkdir()
            shutil.copyfile(ROOT/'scripts/up.sh',scripts/'up.sh')
            shutil.copyfile(ROOT/'scripts/start_blockers.py',scripts/'start_blockers.py')
            marker=root/'image-pulled'
            (scripts/'lib.sh').write_text(r'''
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
die() { echo "$*" >&2; exit 2; }
acquire_model_library_lifecycle_lock() { :; }
acquire_model_library_hot_lock() { :; }
load_conf() { NODES=1; CONF_SOURCE=spec; CONF_NAME="$1"; MODEL=example/model; IMAGE=example/image; PORT=8000; SERVED_NAME=example; }
require_spec_launch_admission() { :; }
spec_overlay_node_selector() { echo "$1"; }
resolve_single_node_placement() { SINGLE_NODE_ID=node-0; SINGLE_NODE_KEY=head; SINGLE_NODE_CONTROL_IP=127.0.0.1; SINGLE_NODE_REMOTE=0; }
single_node_display() { echo fixture; }
single_node_api_base_url() { echo http://127.0.0.1:8000; }
resolve_spec_decode() { SPEC_DECODE_ENABLED=0; }
load_release_spec_projection() { :; }
release_spec_enabled_cell() { echo -; }
human_node_name() { echo fixture; }
start_blocker() { local code="$1"; shift; python3 "$REPO_DIR/scripts/start_blockers.py" record "$code" "$@"; }
require_cluster_nodes() { CLUSTER_TOPOLOGY_COUNT=1; CLUSTER_TOPOLOGY_ID=fixture; }
container_name_for() { echo fixture-container; }
container_ownership_inspect_local() { return 3; }
port_free() { return 0; }
''')
            (scripts/'check-image.sh').write_text(r'''#!/usr/bin/env bash
case " $* " in *" --json "*) echo '{"state":"missing-on-head"}' ;; *) echo 'FAIL image missing' ;; esac
exit 1
''')
            (scripts/'sync-image.sh').write_text(r'''#!/usr/bin/env bash
touch "$IMAGE_PULL_MARKER"
''')
            # Files and memory pass, so --pull-image may stage the image.
            for name in ('check-weights.sh','check-memory.sh'):
                (scripts/name).write_text('#!/usr/bin/env bash\nexit 0\n')
            for path in (scripts/'check-image.sh',scripts/'sync-image.sh',scripts/'check-weights.sh',scripts/'check-memory.sh'):
                path.chmod(0o700)
            env={**os.environ,'IMAGE_PULL_MARKER':str(marker),
                 'PULSAR_MODEL_LIBRARY_DIR':str(root/'state'),
                 'PYTHONDONTWRITEBYTECODE':'1'}
            implicit=subprocess.run(
                ['bash',str(scripts/'up.sh'),'a'*64,'--yes'],env=env,
                text=True,capture_output=True)
            self.assertNotEqual(implicit.returncode,0)
            self.assertFalse(marker.exists())
            explicit=subprocess.run(
                ['bash',str(scripts/'up.sh'),'a'*64,'--pull-image'],env=env,
                text=True,capture_output=True)
            self.assertNotEqual(explicit.returncode,0)
            self.assertTrue(marker.exists(),explicit.stderr+explicit.stdout)


if __name__ == '__main__':
    unittest.main()
