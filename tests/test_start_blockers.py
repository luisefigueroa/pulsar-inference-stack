"""Start reports every independent blocker once, with a node and one next step."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import integration_contract
from scripts import public_cli, start_blockers

# Parameterized doubles for everything up.sh reaches before launch. No Docker,
# SSH, topology or model files are touched.
LIB = r'''
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
die() { echo "$*" >&2; exit 1; }
log() { echo "$*"; }
acquire_model_library_lifecycle_lock() { :; }
acquire_model_library_hot_lock() { :; }
load_conf() { NODES=1; CONF_SOURCE=spec; CONF_NAME="$1"; MODEL=example/model; IMAGE=example/image@sha256:abc; PORT=8000; SERVED_NAME=example; }
require_spec_launch_admission() { :; }
spec_overlay_node_selector() { echo "$1"; }
resolve_single_node_placement() { SINGLE_NODE_ID=node-0; SINGLE_NODE_HOSTNAME=spark-1; SINGLE_NODE_KEY=head; SINGLE_NODE_CONTROL_IP=127.0.0.1; SINGLE_NODE_REMOTE=0; }
single_node_display() { echo fixture-host; }
single_node_api_base_url() { echo http://127.0.0.1:8000; }
resolve_spec_decode() { SPEC_DECODE_ENABLED=0; }
human_node_name() { echo "fixture-host-$1"; }
start_blocker() {
  local code="$1"; shift
  python3 "$REPO_DIR/scripts/start_blockers.py" record "$code" --spec "${START_BLOCKER_SPEC:-${NAME:-}}" \
    --placement "${START_BLOCKER_PLACEMENT-${PLACEMENT_ARGS[*]:-}}" "$@"
}
'''
IMAGE = r'''#!/usr/bin/env bash
state="$FIXTURE_IMAGE"; [ ! -e "$SYNC_MARKER" ] || state=ok
if [ "$state" = ok ]; then rank_state=ok; elif [ "$state" = rank-unreachable ]; then rank_state=unreachable; else rank_state=missing; fi
case " $* " in *" --json "*) printf '{"state":"%s","ranks":[{"rank":0,"topology_index":0,"state":"%s"}]}\n' "$state" "$rank_state" ;; *) echo "IMAGE $state" ;; esac
[ "$state" = ok ]
'''
WEIGHTS = '#!/usr/bin/env bash\ntouch "$WEIGHTS_MARKER"\nexit "$FIXTURE_WEIGHTS_RC"\n'
MEMORY = r'''#!/usr/bin/env bash
case " $* " in *" --json "*) echo '{"reason":"fixture-host: available 10 GiB << footprint 80 GiB (cannot fit); "}'; exit 0 ;; esac
exit "$FIXTURE_MEMORY_RC"
'''


class Catalog(unittest.TestCase):
    def test_every_blocker_names_one_next_step(self):
        for code in start_blockers.BLOCKERS:
            record = start_blockers.blocker(code, spec="ab" * 32, placement="--node spark-2")
            self.assertEqual(record["blocker"], code)
            self.assertTrue(record["fix"].startswith("./pulsar "), record)
            self.assertNotIn("{", record["fix"])

    def test_record_names_node_rank_and_fills_spec_and_placement(self):
        record = start_blockers.blocker("model_files_not_ready", spec="ab" * 32, placement="--node spark-2")
        self.assertEqual(record["fix"], "./pulsar model prepare abababababab --node spark-2 --yes "
                                        "(acquire or restore first if no home exists)")
        record = start_blockers.blocker("node_unreachable", node="spark-2", rank=1)
        self.assertEqual(start_blockers.human(record), "BLOCKED node_unreachable: spark-2 (rank 1): the node is "
                                                       "unreachable over SSH. Next: ./pulsar topology check")

    def test_contract_publishes_the_codes(self):
        self.assertEqual(integration_contract.contract()["start_blocker_codes"], sorted(start_blockers.BLOCKERS))


class StartScenarios(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); scripts = self.root / "scripts"; scripts.mkdir()
        shutil.copyfile(ROOT / "scripts/up.sh", scripts / "up.sh")
        shutil.copyfile(ROOT / "scripts/start_blockers.py", scripts / "start_blockers.py")
        (scripts / "lib.sh").write_text(LIB)
        for name, body in (("check-image.sh", IMAGE), ("check-weights.sh", WEIGHTS), ("check-memory.sh", MEMORY),
                           ("sync-image.sh", '#!/usr/bin/env bash\ntouch "$SYNC_MARKER"\n')):
            (scripts / name).write_text(body); (scripts / name).chmod(0o700)
        self.blockers = self.root / "blockers.jsonl"
        self.sync = self.root / "synced"; self.weights = self.root / "weights-checked"

    def start(self, *flags, image="missing-on-head", weights=0, memory=0):
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
               "PULSAR_START_BLOCKERS_FILE": str(self.blockers), "SYNC_MARKER": str(self.sync),
               "WEIGHTS_MARKER": str(self.weights), "FIXTURE_IMAGE": image,
               "FIXTURE_WEIGHTS_RC": str(weights), "FIXTURE_MEMORY_RC": str(memory)}
        result = subprocess.run(["bash", str(self.root / "scripts/up.sh"), "ab" * 32, *flags],
                                env=env, text=True, capture_output=True, timeout=60)
        return result, [row["blocker"] for row in start_blockers.read(self.blockers)]

    def test_independent_blockers_are_reported_together(self):
        result, codes = self.start(weights=1, memory=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(codes, ["image_missing", "model_files_not_ready", "memory_insufficient"])
        self.assertEqual(result.stdout.count("BLOCKED "), 3)
        self.assertIn("Next: ./pulsar start abababababab --node spark-1 --pull-image", result.stdout)
        self.assertIn("fixture-host: available 10 GiB << footprint 80 GiB", result.stdout)
        self.assertIn("blocked by 3 blocker(s)", result.stderr)

    def test_pull_image_waits_until_every_other_check_passes(self):
        result, codes = self.start("--pull-image", weights=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(codes, ["model_files_not_ready"])
        self.assertFalse(self.sync.exists())
        result, codes = self.start("--pull-image")
        self.assertEqual(codes, ["model_files_not_ready"])  # from the first run only
        self.assertTrue(self.sync.exists(), result.stderr + result.stdout)

    def test_unreachable_node_ends_the_checks_and_names_the_node(self):
        result, codes = self.start(image="rank-unreachable")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(codes, ["node_unreachable"])
        self.assertIn("BLOCKED node_unreachable: fixture-host-0 (rank 0)", result.stdout)
        self.assertFalse(self.weights.exists())


class JsonEnvelope(unittest.TestCase):
    def test_start_json_lists_blockers_in_details(self):
        record = start_blockers.blocker("image_missing", spec="ab" * 32)

        def refuse(command, env, cwd):
            Path(env["PULSAR_START_BLOCKERS_FILE"]).write_text(json.dumps(record) + "\n")
            return type("Completed", (), {"returncode": 1, "stdout": "", "stderr": "start is blocked\n"})()
        output = io.StringIO()
        with patch.object(public_cli, "run_command", side_effect=refuse), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = public_cli.main(["start", "ab" * 32, "--json"])
        error = json.loads(output.getvalue())["error"]
        self.assertEqual(status, 3)
        self.assertEqual(error["code"], "prerequisite_failed")
        self.assertEqual(error["details"], [record])


if __name__ == "__main__":
    unittest.main()
