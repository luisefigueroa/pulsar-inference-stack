"""Start reports every independent blocker once, with a node and one next step."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
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

SPEC = "ab" * 32
SERVICE_ID = "5e" * 32


def lib_function(name, path="scripts/lib.sh"):
    """A real shell function (from lib.sh by default), so the doubles keep its wiring."""
    text = (ROOT / path).read_text()
    return re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M).group(0)


# Parameterized doubles for everything up.sh reaches. No Docker, SSH, topology
# or model files are touched; FIXTURE_* variables select each outcome.
LIB = r'''
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
die() { echo "error: $*" >&2; exit 1; }
usage_die() { die "$1"; }
log() { echo "$*"; }
warn() { echo "warning: $*" >&2; }
error_line() { echo "error: $*" >&2; }
acquire_model_library_lifecycle_lock() { :; }
acquire_model_library_hot_lock() { :; }
release_model_library_locks() { touch "$FIXTURE_DIR/locks-released"; }
load_conf() { NODES="${FIXTURE_NODES:-1}"; CONF_SOURCE=spec; CONF_NAME="$1"; MODEL=example/model; IMAGE=example/image@sha256:abc; PORT=8000; SERVED_NAME=example; TOPOLOGY_CLASS=mesh; MIN_RAILS_PER_PAIR=2; }
require_spec_launch_admission() { :; }
spec_overlay_node_selector() { echo "$1"; }
resolve_single_node_placement() {
  # A selector needs a confirmed topology, whose only node is spark-1.
  if [ -n "$1" ] && { [ "${FIXTURE_TOPOLOGY:-1}" = 0 ] || [ "$1" != spark-1 ]; }; then return 1; fi
  SINGLE_NODE_INDEX=0; SINGLE_NODE_ID=node-0; SINGLE_NODE_HOSTNAME=spark-1; SINGLE_NODE_KEY=head; SINGLE_NODE_CONTROL_IP=127.0.0.1; SINGLE_NODE_REMOTE=0
}
select_memory_estimate() { :; }
single_node_display() { echo fixture-host; }
single_node_api_base_url() { echo http://127.0.0.1:8000; }
resolve_spec_decode() { SPEC_DECODE_ENABLED=0; }
human_node_name() { echo "fixture-host-$1"; }
CLUSTER_NODE_IDS=(node-0 node-1 node-2)
CLUSTER_NODE_SSH_HOSTS=(local alias-1 alias-2)
require_cluster_nodes() { CLUSTER_TOPOLOGY_COUNT="${FIXTURE_TOPOLOGY:-$NODES}"; CLUSTER_TOPOLOGY_ID=cccccccccccc; [ "$CLUSTER_TOPOLOGY_COUNT" -ge "$1" ]; }
require_profile_topology() { [ -z "${FIXTURE_FABRIC:-}" ] || { echo "topology: $FIXTURE_FABRIC" >&2; return 1; }; }
container_name_for() { echo fixture-container; }
container_ownership_inspect_local() { return "${FIXTURE_EXISTING:-3}"; }
container_ownership_inspect_remote() { return "${FIXTURE_EXISTING:-3}"; }
port_free() { [ "$1" != "${FIXTURE_BUSY_PORT:-}" ]; }
resolve_library_hot_for_profile() { :; }
write_launch_plan_file() { printf '{"schema_version": 5, "service_id": "%s"}\n' "$FIXTURE_SERVICE_ID" >"$1"; }
api_auth_curl_args() { :; }
container_state_exact() { echo "${FIXTURE_CONTAINER:-running}"; }
'''
# Run the owning selector while keeping topology/transport boundaries synthetic.
_source_lib = (ROOT/'scripts/lib.sh').read_text()
LIB += _source_lib[_source_lib.index('resolve_serving_placement() {'):
                   _source_lib.index('resolve_single_node_placement() {')]
IMAGE = r'''#!/usr/bin/env bash
touch "$FIXTURE_DIR/image-checked"
state="$FIXTURE_IMAGE"; [ ! -e "$FIXTURE_DIR/synced" ] || state=ok
# "crash": the check itself failed and printed no report.
if [ "$state" = crash ]; then echo "error: cannot read the selected spec" >&2; exit 3; fi
if [ "$state" = ok ]; then rank_state=ok; elif [ "$state" = rank-unreachable ]; then rank_state=unreachable; else rank_state=missing; fi
ranks='{"rank":0,"topology_index":0,"state":"'"$rank_state"'"}'
# "mixed": one rank unreachable and another known to lack the image.
if [ "$state" = mixed ]; then state=rank-unreachable; ranks='{"rank":0,"topology_index":0,"state":"unreachable"},{"rank":1,"topology_index":1,"state":"missing"}'; fi
case " $* " in *" --json "*) printf '{"state":"%s","ranks":[%s]}\n' "$state" "$ranks" ;; *) echo "IMAGE $state" ;; esac
[ "$state" = ok ]
'''
WEIGHTS = r'''#!/usr/bin/env bash
touch "$FIXTURE_DIR/weights-checked"
[ "$FIXTURE_WEIGHTS_RC" != 3 ] || echo "error: cannot resolve the prepared copies" >&2
exit "$FIXTURE_WEIGHTS_RC"
'''
MEMORY = r'''#!/usr/bin/env bash
touch "$FIXTURE_DIR/memory-checked"
case " $* " in *" --json "*) echo '{"reason":"fixture-host: available 10 GiB << footprint 80 GiB (cannot fit); "}'; exit 0 ;; esac
[ "$FIXTURE_MEMORY_RC" != 3 ] || echo "error: cannot read available memory on spark-2" >&2
exit "$FIXTURE_MEMORY_RC"
'''
LAUNCHER = ('#!/usr/bin/env bash\necho "${0##*/} $*" >>"$FIXTURE_DIR/launched"\n'
            'env | grep "^START_BLOCKER_MEMORY_ESTIMATE" | sort >>"$FIXTURE_DIR/launch-env" || true\n')
CURL = r'''#!/usr/bin/env bash
url="${*: -1}"
case "$url" in
  */health) [ "${FIXTURE_HEALTH:-ok}" = ok ] ;;
  */v1/completions)
    out=""
    while [ $# -gt 0 ]; do [ "$1" != -o ] || out="$2"; shift; done
    case "${FIXTURE_SMOKE:-200}" in
      timeout) echo "curl: (28) Operation timed out after 120001 milliseconds with 0 bytes received" >&2; exit 28 ;;
      200) printf '{"choices":[{"text":" 4"}]}' >"$out"; printf 200 ;;
      *) printf '{"error":"engine failure"}' >"$out"; printf '%s' "$FIXTURE_SMOKE" ;;
    esac ;;
  *) exit 2 ;;
esac
'''
DOCKER = '#!/usr/bin/env bash\necho "$*" >>"$FIXTURE_DIR/docker-calls"\n[ "$1" != logs ] || echo "fixture engine log line"\n'


# Blockers that no ./pulsar command resolves; their note says why. Adding a
# code here needs the same justification.
NO_COMMAND = {"guard_unsupported": "docs/SERVING_GUARD_SCHEMA.md", "historical_spec": "docs/OPERATIONS.md",
              "service_stopped": None}
ALL_START_FLAGS = "--dry-run --pull-image --accept-memory-warn --replace --verbose --skip-preflight"


class Catalog(unittest.TestCase):
    help_text = {}

    def assert_runnable(self, command):
        """One command that parses as written, using only flags its help lists."""
        self.assertTrue(command.startswith("./pulsar "), command)
        self.assertEqual(subprocess.run(["bash", "-n", "-c", command]).returncode, 0, command)
        words = shlex.split(command)[1:]
        path = []
        for word in words:
            if word.startswith("-") or word.startswith(SPEC[:12]):
                break
            path.append(word)
        flags = [word for word in words if word.startswith("--")]
        if not flags:
            return
        key = tuple(path)
        if key not in self.help_text:
            self.help_text[key] = subprocess.run([str(ROOT / "pulsar"), *path, "--help"], cwd=ROOT, text=True,
                                                 capture_output=True, timeout=60).stdout
        for flag in flags:
            self.assertIn(flag, self.help_text[key], f"{command}: {flag}")

    def test_every_fix_is_one_runnable_command_or_null(self):
        for code in start_blockers.BLOCKERS:
            record = start_blockers.blocker(code, spec=SPEC, placement="--node spark-2", start_flags=ALL_START_FLAGS)
            with self.subTest(code=code):
                self.assertEqual(record["blocker"], code)
                if code in NO_COMMAND:
                    self.assertIsNone(record["fix"])
                    self.assertTrue(record["note"])
                    if NO_COMMAND[code]:
                        self.assertIn(NO_COMMAND[code], record["note"])
                        self.assertTrue((ROOT / NO_COMMAND[code]).is_file())
                else:
                    self.assert_runnable(record["fix"])

    def test_guard_blocker_requires_explicit_foreground_ownership(self):
        record = start_blockers.blocker("guard_unsupported", spec=SPEC, placement="--node spark-2",
                                        detail="added by --override-file")
        self.assertIsNone(record["node"])
        self.assertEqual(start_blockers.human(record),
                         "BLOCKED guard_unsupported: this spec requires serving-guard enforcement "
                         "(recipe.container.guard), which ordinary start cannot enforce (added by --override-file). "
                         "Note: Use an explicitly scoped pulsar guarded run; see docs/SERVING_GUARD_SCHEMA.md.")

    def test_record_names_node_rank_and_fills_spec_and_placement(self):
        record = start_blockers.blocker("model_files_not_ready", spec=SPEC, placement="--node spark-2")
        self.assertEqual(record["fix"], "./pulsar model prepare abababababab --node spark-2 --yes")
        record = start_blockers.blocker("node_unreachable", node="spark-2", node_id="node-1", rank=1)
        self.assertEqual((record["node"], record["node_id"], record["rank"]), ("spark-2", "node-1", 1))
        self.assertEqual(start_blockers.human(record), "BLOCKED node_unreachable: spark-2 (rank 1): the node is "
                                                       "unreachable over SSH. Next: ./pulsar topology check")

    def test_fixes_keep_the_effective_recipe(self):
        record = start_blockers.blocker("image_missing", spec=SPEC, placement="--node spark-1",
                                        spec_file="/tmp/candidate spec.json", override_file="/tmp/override.json",
                                        memory_estimate_file="/tmp/estimate.json")
        self.assertEqual(record["fix"], "./pulsar start " + SPEC + " --spec-file '/tmp/candidate spec.json' "
                         "--override-file /tmp/override.json --memory-estimate-file /tmp/estimate.json "
                         "--node spark-1 --pull-image")
        prepare = start_blockers.blocker("model_files_not_ready", spec=SPEC, override_file="/tmp/o.json")
        self.assertNotIn("--override-file", prepare["fix"])

    def test_suggestions_repeat_the_operator_flags(self):
        def fix(code, flags):
            return start_blockers.blocker(code, spec=SPEC, placement="--node spark-1", start_flags=flags)["fix"]
        # A dry run stays a dry run; unknown and non-start flags are not carried.
        self.assertEqual(fix("image_missing", "--dry-run --yes"),
                         "./pulsar start abababababab --node spark-1 --dry-run --pull-image")
        # Each fix adds only its own flag and keeps permissions already granted.
        self.assertEqual(fix("memory_warning", "--pull-image --accept-memory-warn"),
                         "./pulsar start abababababab --node spark-1 --pull-image --accept-memory-warn")
        self.assertEqual(fix("image_missing", "--accept-memory-warn"),
                         "./pulsar start abababababab --node spark-1 --accept-memory-warn --pull-image")
        self.assertEqual(fix("port_in_use", "--dry-run"), "./pulsar inventory")

    def test_a_missing_home_suggests_the_download(self):
        def files_fix(row, placement="--node spark-1", home_node="spark-1"):
            with tempfile.TemporaryDirectory() as state, patch("model_library.catalog.entries", return_value=[row]):
                record = start_blockers.blocker("model_files_not_ready", spec=SPEC, placement=placement,
                                                home_node=home_node, state_root=state)
            return record["fix"], record["note"]
        self.assertEqual(files_fix({"home": {"node_id": "node-0"}, "archive": None}),
                         ("./pulsar model prepare abababababab --node spark-1 --yes", None))
        # A multi-node spec keeps its home on rank 0.
        self.assertEqual(files_fix({"home": None, "archive": None}, placement="", home_node="spark-3"),
                         ("./pulsar model acquire abababababab --node spark-3 --yes", None))
        fix, note = files_fix({"snapshots": {"target": {"home": {"node_id": "node-0"}, "archive": None},
                                             "draft": {"home": None, "archive": {"verified": True}}}})
        self.assertEqual(fix, "./pulsar model acquire abababababab --snapshot draft --node spark-1 --yes")
        self.assertEqual(note, "A verified archive exists; restoring it also works: "
                               "./pulsar model restore abababababab --snapshot draft --node spark-1 --yes")
        self.assert_runnable(fix)
        self.assert_runnable(note.split("works: ", 1)[1])
        with patch("model_library.catalog.entries", side_effect=OSError("unreadable")):
            record = start_blockers.blocker("model_files_not_ready", spec=SPEC, placement="--node spark-1")
        self.assertEqual(record["fix"], "./pulsar model prepare abababababab --node spark-1 --yes")

    def test_refusals_after_a_replacement_say_nothing_is_running(self):
        memory = start_blockers.blocker("memory_insufficient", spec=SPEC, after_replace=True)
        self.assertEqual(memory["note"], "Stop GPU services it lists that are no longer needed, or choose a "
                                         "smaller spec. " + start_blockers.AFTER_REPLACE_NOTE)
        warning = start_blockers.blocker("memory_warning", spec=SPEC, after_replace=True)
        self.assertEqual(warning["note"], start_blockers.AFTER_REPLACE_NOTE)
        # A launch-stage record describes the new service, which is still running,
        # unless the container never started.
        timeout = start_blockers.blocker("health_timeout", spec=SPEC, after_replace=True)
        self.assertEqual(timeout["note"], "It is still running; its logs are shown above.")
        failed = start_blockers.blocker("container_start_failed", spec=SPEC, after_replace=True)
        self.assertEqual(failed["note"], "Docker's error is shown above. " + start_blockers.AFTER_REPLACE_NOTE)
        self.assertIn(f'AFTER_REPLACE_NOTE="{start_blockers.AFTER_REPLACE_NOTE}"',
                      (ROOT / "scripts/lib.sh").read_text())

    def test_unconfirmed_cleanup_suggests_stop(self):
        record = start_blockers.blocker("health_timeout", spec=SPEC, service_id=SERVICE_ID, no_command=True,
                                        note="The cluster's containers were removed; the logs are shown above.",
                                        unconfirmed="spark-2 (rank 1)", after_replace=True)
        self.assertEqual(record["fix"], "./pulsar stop abababababab")
        self.assertEqual(record["note"], "Removing its containers could not be confirmed on spark-2 (rank 1); "
                                         "stop removes what remains.")

    def test_launcher_suggestions_keep_the_operator_estimate(self):
        # The launchers reset their own estimate arguments; start exports the operator's.
        script = (f"REPO_DIR={shlex.quote(str(ROOT))} NAME={SPEC} MEMORY_ESTIMATE_FILE='' MEMORY_ESTIMATE_ID=''\n"
                  + lib_function("start_blocker") + "start_blocker memory_warning\n")
        env = {**os.environ, "START_BLOCKER_MEMORY_ESTIMATE_FILE": "/tmp/estimate.json",
               "START_BLOCKER_MEMORY_ESTIMATE_ID": "e" * 64}
        env.pop("PULSAR_START_BLOCKERS_FILE", None)
        result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, timeout=60)
        self.assertIn("Next: ./pulsar start abababababab --memory-estimate-file /tmp/estimate.json "
                      f"--memory-estimate-id {'e' * 64} --accept-memory-warn", result.stdout)

    def test_every_launch_record_names_its_service(self):
        launch = {code for code, kind in start_blockers.BLOCKERS.items() if kind.stage == "launch"}
        found = 0
        for path in ("scripts/up.sh", "serve.sh", "cluster/start-cluster.sh"):
            lines = (ROOT / path).read_text().splitlines()
            for index, line in enumerate(lines):
                match = re.search(r"\b(?:start_blocker|blocked|cluster_failure) (\w+)", line)
                if not match or match.group(1) not in launch:
                    continue
                call, end = line, index
                while call.rstrip().endswith("\\"):
                    end += 1
                    call = call.rstrip()[:-1] + lines[end]
                found += 1
                with self.subTest(path=path, call=line.strip()):
                    self.assertTrue("--service-id" in call or "cluster_failure" in call, call)
        self.assertGreaterEqual(found, 12)
        # cluster_failure passes the launch plan's service ID itself.
        self.assertIn('--service-id "${SERVICE_ID:-}"', lib_function("cluster_failure", "cluster/start-cluster.sh"))

    def test_launch_failures_are_labeled_failed_not_blocked(self):
        for code, kind in start_blockers.BLOCKERS.items():
            record = start_blockers.blocker(code, spec=SPEC, service_id=SERVICE_ID)
            with self.subTest(code=code):
                self.assertEqual(start_blockers.human(record).split()[0],
                                 "FAILED" if kind.stage == "launch" else "BLOCKED")
                self.assertEqual(record["service_id"], SERVICE_ID)

    def test_contract_publishes_the_codes(self):
        self.assertEqual(integration_contract.contract()["start_blocker_codes"], sorted(start_blockers.BLOCKERS))
        self.assertIn("guard_unsupported", integration_contract.contract()["start_blocker_codes"])


class StartScenarios(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("scripts", "cluster", "bin"):
            (self.root / directory).mkdir()
        shutil.copyfile(ROOT / "scripts/up.sh", self.root / "scripts/up.sh")
        shutil.copyfile(ROOT / "scripts/start_blockers.py", self.root / "scripts/start_blockers.py")
        (self.root / "scripts/lib.sh").write_text(LIB + lib_function("start_blocker")
                                                   + lib_function("launch_plan_service_id"))
        for name, body in (("scripts/check-image.sh", IMAGE), ("scripts/check-weights.sh", WEIGHTS),
                           ("scripts/check-memory.sh", MEMORY),
                           ("scripts/sync-image.sh", '#!/usr/bin/env bash\ntouch "$FIXTURE_DIR/synced"\n'),
                           ("serve.sh", LAUNCHER), ("cluster/start-cluster.sh", LAUNCHER),
                           ("cluster/preflight.sh", "#!/usr/bin/env bash\nexit 0\n"),
                           ("bin/curl", CURL), ("bin/docker", DOCKER)):
            path = self.root / name
            path.write_text(body); path.chmod(0o700)
        self.blockers = self.root / "blockers.jsonl"

    def start(self, *flags, image="missing-on-head", weights=0, memory=0, **fixture):
        env = {**os.environ, "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}", "PYTHONPATH": str(ROOT),
               "PYTHONDONTWRITEBYTECODE": "1", "PULSAR_START_BLOCKERS_FILE": str(self.blockers),
               "PULSAR_MODEL_LIBRARY_DIR": str(self.root / "library"), "PULSAR_DOCKER": str(self.root / "bin/docker"),
               "FIXTURE_DIR": str(self.root), "FIXTURE_IMAGE": image, "FIXTURE_WEIGHTS_RC": str(weights),
               "FIXTURE_MEMORY_RC": str(memory), "FIXTURE_SERVICE_ID": SERVICE_ID,
               "WAIT_ATTEMPTS": "2", "WAIT_SECONDS": "0"}
        for key in ("PULSAR_SPEC_FILE", "PULSAR_VERBOSE", "PULSAR_LAUNCH_PLAN_OUT"):
            env.pop(key, None)
        env.update({f"FIXTURE_{key.upper()}": str(value) for key, value in fixture.items()})
        result = subprocess.run(["bash", str(self.root / "scripts/up.sh"), SPEC, *flags],
                                env=env, text=True, capture_output=True, timeout=60)
        self.records = start_blockers.read(self.blockers)
        return result, [row["blocker"] for row in self.records]

    def ran(self, marker):
        return (self.root / marker).exists()

    def reset(self):
        for marker in ("blockers.jsonl", "docker-calls", "launched", "locks-released"):
            (self.root / marker).unlink(missing_ok=True)

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
        self.assertFalse(self.ran("synced"))
        result, codes = self.start("--pull-image")
        self.assertEqual(codes, ["model_files_not_ready"])  # from the first run only
        self.assertTrue(self.ran("synced"), result.stderr + result.stdout)

    def test_known_missing_image_is_reported_with_an_unreachable_node(self):
        result, codes = self.start(image="mixed")
        self.assertEqual(codes, ["node_unreachable", "image_missing"])
        self.assertIn("on fixture-host-1", result.stdout)
        self.assertEqual(self.records[0]["node_id"], "node-0")
        self.assertFalse(self.ran("weights-checked"))

    def test_pull_image_covers_a_known_missing_image_behind_an_unreachable_node(self):
        for flags in (("--pull-image",), ("--pull-image", "--dry-run")):
            with self.subTest(flags=flags):
                self.reset()
                result, codes = self.start(*flags, image="mixed")
                self.assertEqual(codes, ["node_unreachable"])
                self.assertIn("INFO  image     missing on fixture-host-1; --pull-image stages it once the other "
                              "blockers are resolved", result.stdout)
                self.assertFalse(self.ran("synced"))

    def test_unreachable_node_ends_the_checks_and_names_the_node(self):
        result, codes = self.start(image="rank-unreachable")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(codes, ["node_unreachable"])
        self.assertIn("BLOCKED node_unreachable: fixture-host-0 (rank 0)", result.stdout)
        self.assertFalse(self.ran("weights-checked"))

    def test_missing_topology_is_the_only_blocker(self):
        result, codes = self.start(topology=0)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(codes, ["topology_incomplete"])
        self.assertIn("(needs 1, confirmed 0). Next: ./pulsar topology setup", result.stdout)
        for check in ("image-checked", "weights-checked", "memory-checked"):
            self.assertFalse(self.ran(check), check)

    def test_a_node_selector_without_a_topology_reports_the_topology(self):
        result, codes = self.start("--node", "spark-1", topology=0)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(codes, ["topology_incomplete"])
        self.assertIn("Next: ./pulsar topology setup", result.stdout)
        self.assertIn("start is blocked by 1 blocker(s) above", result.stderr)
        self.assertFalse(self.ran("image-checked"))
        # With a topology, a selector that matches no node is the operator's to fix.
        result, codes = self.start("--node", "elsewhere")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(codes, ["topology_incomplete"])  # from the first run only
        self.assertIn("--node 'elsewhere' does not select exactly one confirmed node; use a hostname or node ID "
                      "from ./pulsar topology show", result.stderr)

    def test_fabric_shortfall_names_its_reason(self):
        reason = "spark-1/spark-3 expose 1 shared RoCE rail; the spec requires 2"
        result, codes = self.start(nodes=2, fabric=reason)
        self.assertEqual(codes, ["fabric_incomplete"])
        self.assertIn(f"({reason}). Next: ./pulsar topology check", result.stdout)
        self.assertFalse(self.ran("image-checked"))

    def test_a_check_that_could_not_run_is_not_a_failed_condition(self):
        result, codes = self.start(image="crash", weights=3, memory=3)
        self.assertEqual(codes, ["image_check_failed", "model_files_check_failed", "memory_check_failed"])
        self.assertEqual([row["message"].rsplit(" (", 1)[1] for row in self.records],
                         ["cannot read the selected spec)", "cannot resolve the prepared copies)",
                          "cannot read available memory on spark-2)"])
        self.assertEqual({row["fix"] for row in self.records},
                         {"./pulsar start abababababab --node spark-1 --verbose"})

    def test_dry_run_suggestions_stay_dry(self):
        result, codes = self.start("--dry-run", memory=1)
        self.assertEqual(codes, ["image_missing", "memory_insufficient"])
        self.assertIn("Next: ./pulsar start abababababab --node spark-1 --dry-run --pull-image", result.stdout)

    def test_dry_run_with_pull_image_reports_the_staging_it_would_do(self):
        result, codes = self.start("--dry-run", "--pull-image")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(codes, [])
        self.assertIn("INFO  image     missing on fixture-host-0; --pull-image would stage it", result.stdout)
        self.assertIn("DRY-RUN OK", result.stdout)
        self.assertFalse(self.ran("synced"))
        self.assertFalse(self.ran("launched"))

    def test_existing_service_ends_the_checks_before_memory(self):
        result, codes = self.start(image="ok", existing=0)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(codes, ["service_exists"])
        self.assertIn("Next: ./pulsar status abababababab --node spark-1\n  To replace it, rerun start with "
                      "--replace, only with explicit replacement approval.", result.stdout)
        self.assertEqual(self.records[0]["node_id"], "node-0")
        self.assertFalse(self.ran("weights-checked"))
        self.assertFalse(self.ran("memory-checked"))

    def test_replace_defers_memory_and_port_to_the_launch_recheck(self):
        result, codes = self.start("--replace", "--dry-run", image="ok", existing=0, busy_port=8000)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(codes, [])
        self.assertIn("--replace removes it after the image recheck", result.stdout)
        self.assertIn("SKIP  memory", result.stdout)
        self.assertIn("SKIP  port", result.stdout)
        self.assertFalse(self.ran("memory-checked"))

    def test_port_in_use_names_the_node_and_port(self):
        result, codes = self.start(image="ok", busy_port=8000)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(codes, ["port_in_use"])
        self.assertIn("BLOCKED port_in_use: fixture-host-0: the service port is already in use (port 8000). "
                      "Next: ./pulsar inventory\n  Free the port, or change the deployment port.", result.stdout)
        self.assertFalse(self.ran("launched"))
        result, codes = self.start("--skip-preflight", image="ok", nodes=2, busy_port=29500)
        self.assertEqual(codes, ["port_in_use", "port_in_use"])
        self.assertIn("(port 29500)", self.records[1]["message"])

    def test_ready_only_after_the_test_completion_answers(self):
        result, codes = self.start(image="ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(codes, [])
        self.assertIn("\nREADY\n", result.stdout)
        self.assertTrue(self.ran("locks-released"))
        self.assertEqual((self.root / "launched").read_text(), f"serve.sh {SPEC} -d --node node-0\n")

    def test_failed_test_completion_leaves_the_service_running(self):
        for smoke, detail in (("500", 'HTTP 500: {"error":"engine failure"}'),
                              ("timeout", "curl: Operation timed out after 120001 milliseconds with 0 bytes received")):
            with self.subTest(smoke=smoke):
                self.reset()
                result, codes = self.start(image="ok", smoke=smoke)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("READY", result.stdout)
                self.assertEqual(codes, ["smoke_test_failed"])
                record = self.records[0]
                self.assertEqual((record["stage"], record["node"], record["node_id"], record["service_id"]),
                                 ("launch", "spark-1", "node-0", SERVICE_ID))
                self.assertEqual(record["fix"], "./pulsar stop abababababab --node spark-1")
                self.assertIn(f"({detail})", record["message"])
                self.assertIn("FAILED smoke_test_failed: spark-1: the service started and passed its health check",
                              result.stdout)
                # Its logs are shown; nothing stops or removes it.
                self.assertEqual((self.root / "docker-calls").read_text(), "logs --tail 80 fixture-container\n")

    def test_multi_node_start_runs_the_same_test_completion(self):
        result, codes = self.start("--skip-preflight", image="ok", nodes=2, smoke="500")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(codes, ["smoke_test_failed"])
        self.assertEqual((self.records[0]["node"], self.records[0]["node_id"]), ("fixture-host-0", "node-0"))
        self.assertIn("start-cluster.sh", (self.root / "launched").read_text())
        self.reset()
        result, codes = self.start("--skip-preflight", image="ok", nodes=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\nREADY\n", result.stdout)

    def test_an_unobservable_container_at_the_timeout_is_not_called_running(self):
        result, codes = self.start(image="ok", health="fail", container="unknown")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(codes, ["health_timeout"])
        self.assertEqual(self.records[0]["note"],
                         "Its state could not be observed when the wait ended, so it may still be running.")
        self.assertEqual(self.records[0]["fix"], "./pulsar stop abababababab --node spark-1")
        self.assertIn("could not be observed", result.stderr)
        self.assertFalse(self.ran("docker-calls"))  # no logs are read from an unobservable node

    def test_launchers_receive_the_operator_estimate_for_suggestions(self):
        result, codes = self.start("--memory-estimate-file", "/tmp/estimate.json", "--memory-estimate-id", "e" * 64,
                                   image="ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "launch-env").read_text().splitlines(),
                         ["START_BLOCKER_MEMORY_ESTIMATE_FILE=/tmp/estimate.json",
                          "START_BLOCKER_MEMORY_ESTIMATE_ID=" + "e" * 64])

    def test_health_wait_outcomes_are_recorded(self):
        for container, code, fix in (("running", "health_timeout", "./pulsar stop abababababab --node spark-1"),
                                     ("exited", "container_exited", "./pulsar stop abababababab --node spark-1"),
                                     ("absent", "service_stopped", None)):
            with self.subTest(container=container):
                self.reset()
                result, codes = self.start(image="ok", health="fail", container=container)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("READY", result.stdout)
                self.assertEqual(codes, [code])
                self.assertEqual((self.records[0]["fix"], self.records[0]["service_id"]), (fix, SERVICE_ID))
                self.assertIn(f"FAILED {code}: spark-1: ", result.stdout)


class ClusterCleanup(unittest.TestCase):
    """A multi-node launch failure says its containers were removed only when it
    confirmed that on every node."""

    def failure(self, states, *args, replaced=False):
        with tempfile.TemporaryDirectory() as temp:
            blockers = Path(temp) / "blockers.jsonl"
            script = "\n".join([
                f"REPO_DIR={shlex.quote(str(ROOT))} NODES=3 CONTAINER=fixture-container MODEL_NAME={SPEC}"
                f" SERVICE_ID={SERVICE_ID}",
                "CLUSTER_NODE_IDS=(node-0 node-1 node-2) CLUSTER_NODE_SSH_HOSTS=(local alias-1 alias-2)",
                'human_node_name() { echo "spark-$(( $1 + 1 ))"; }',
                # States by rank; rank 0 is observed without an SSH host.
                'container_state_exact() { local states=($FIXTURE_STATES); echo "${states[${2#alias-}]:-${states[0]}}"; }',
                lib_function("start_blocker"),
                lib_function("cluster_failure", "cluster/start-cluster.sh"),
                "cluster_failure " + " ".join(shlex.quote(arg) for arg in args),
            ])
            env = {**os.environ, "FIXTURE_STATES": states, "PULSAR_START_BLOCKERS_FILE": str(blockers),
                   **({"LAUNCH_AFTER_REPLACE": "1"} if replaced else {})}
            env.pop("START_BLOCKER_PLACEMENT", None)
            result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            (record,) = start_blockers.read(blockers)
            return record

    def test_removal_confirmed_on_every_node_keeps_the_removed_note(self):
        note = "The cluster's containers were removed; the logs are shown above."
        record = self.failure("absent absent absent", "health_timeout", "0", "--no-command", "--note", note)
        self.assertEqual((record["fix"], record["note"]), (None, note))
        self.assertEqual((record["node"], record["node_id"], record["rank"], record["service_id"]),
                         ("spark-1", "node-0", 0, SERVICE_ID))

    def test_an_unconfirmed_node_is_named_and_stop_is_suggested(self):
        record = self.failure("absent unknown running", "container_exited", "1", "--no-command", "--note",
                              "The cluster's containers were removed; the logs are shown above.")
        self.assertEqual(record["fix"], "./pulsar stop abababababab")
        self.assertEqual(record["note"], "Removing its containers could not be confirmed on spark-2 (rank 1), "
                                         "spark-3 (rank 2); stop removes what remains.")
        self.assertEqual(record["service_id"], SERVICE_ID)

    def test_a_start_failure_after_a_replacement_says_nothing_runs_only_when_confirmed(self):
        note = "Docker's error is shown above. The cluster's containers were removed."
        record = self.failure("absent absent absent", "container_start_failed", "2", "--note", note, replaced=True)
        self.assertEqual(record["fix"], "./pulsar doctor")
        self.assertEqual(record["note"], note + " " + start_blockers.AFTER_REPLACE_NOTE)
        record = self.failure("running absent absent", "container_start_failed", "2", "--note", note, replaced=True)
        self.assertEqual(record["fix"], "./pulsar stop abababababab")
        self.assertNotIn(start_blockers.AFTER_REPLACE_NOTE, record["note"])


class StatusWithoutService(unittest.TestCase):
    def status(self, worker):
        with tempfile.TemporaryDirectory() as temp:
            scripts = Path(temp) / "scripts"; scripts.mkdir()
            shutil.copyfile(ROOT / "scripts/status.sh", scripts / "status.sh")
            shutil.copyfile(ROOT / "scripts/service_status.py", scripts / "service_status.py")
            (scripts / "observe-serving.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
            head = {"hostname": "spark-1", "node_id": "node-0", "local": True, "confirmed": True, "probe_status": "ok"}
            (scripts / "inventory.sh").write_text("#!/usr/bin/env bash\necho '" + json.dumps(
                {"services": [], "worker": worker, "nodes": {"head": head}}) + "'\n")
            for name in ("observe-serving.sh", "inventory.sh"):
                (scripts / name).chmod(0o700)
            return subprocess.run(["bash", str(scripts / "status.sh"), SPEC], text=True, capture_output=True,
                                  env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=30)

    def test_start_is_suggested_only_when_absence_is_established(self):
        complete = self.status({"status": "ok"})
        self.assertEqual(complete.returncode, 1)
        self.assertIn("Start it with ./pulsar start abababababab", " ".join(complete.stderr.split()))
        unknown = self.status({"status": "unreachable", "reason": "spark-2 · SSH unreachable"})
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("is unknown: spark-2 · SSH unreachable", " ".join(unknown.stderr.split()))
        self.assertNotIn("pulsar start", unknown.stderr)


class JsonEnvelope(unittest.TestCase):
    def test_start_json_lists_blockers_in_details(self):
        records = [start_blockers.blocker("image_missing", spec=SPEC),
                   start_blockers.blocker("smoke_test_failed", spec=SPEC, service_id=SERVICE_ID)]

        def refuse(command, env, cwd):
            Path(env["PULSAR_START_BLOCKERS_FILE"]).write_text("".join(json.dumps(r) + "\n" for r in records))
            return type("Completed", (), {"returncode": 1, "stdout": "", "stderr": "start is blocked\n"})()
        output = io.StringIO()
        with patch.object(public_cli, "run_command", side_effect=refuse), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = public_cli.main(["start", SPEC, "--json"])
        error = json.loads(output.getvalue())["error"]
        self.assertEqual(status, 3)
        self.assertEqual(error["code"], "prerequisite_failed")
        self.assertEqual(error["details"], records)


if __name__ == "__main__":
    unittest.main()
