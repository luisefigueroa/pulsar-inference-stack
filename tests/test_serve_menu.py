"""Serve a model…: the change check, the list, the plan screen and the run,
driven with doubles for ui.sh, ./pulsar and the node tools. No command here
reaches Docker, SSH, a GPU or the Hugging Face Hub."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import saved_observations as saved  # noqa: E402
from tests.test_catalog_menu import HOME, checked, inventory, row  # noqa: E402
from tests.test_cluster_menu import FORBIDDEN  # noqa: E402
from tests.test_serve_plan import (HF_OK, OTHER, SPEC, envelope, hf, image_plan, prepare_plan,  # noqa: E402
                                   start_blocked)

TOPOLOGY_ID = "a" * 64
NOW = datetime.now(timezone.utc)


def utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def topology():
    return {"schema_version": 2, "topology_id": TOPOLOGY_ID, "validation": {"ssh_identity_enrolled": True},
            "nodes": [{"rank": index, "hostname": f"spark-{index + 1}", "node_id": f"n{index}",
                       "ssh_host": f"spark-{index + 1}"} for index in range(3)]}


def changes(*, minutes=1, missing=(), kept=False):
    record = {"topology_id": TOPOLOGY_ID, "result": "changed" if missing else "unchanged", "issues": [],
              "found": [], "changed": [], "untrusted": [],
              "missing": [{"hostname": f"spark-{int(node[1:]) + 1}", "node_id": node, "position": int(node[1:]),
                           "answered": False} for node in missing]}
    return record, minutes, kept


OTHER_RUNNING = inventory((OTHER, "running", ["head", "w1", "w2"]))
NOTHING_RUNNING = inventory()


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        (self.root / "releases").mkdir()
        for name in ("serve-menu.sh", "cluster-review.sh"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
            (scripts / name).chmod(0o755)
        self.fixtures = self.root / "fixtures"
        self.fixtures.mkdir()
        self.choose_log = self.root / "choose.log"
        self.default_log = self.root / "default.log"
        self.answers = self.root / "answers"
        self.log = self.root / "pulsar.log"
        (scripts / "ui.sh").write_text(r'''
emit_frame() { cat; }
_ui_terminal_columns() { return 1; }
_ui_terminal_rows() { [ -n "${ROWS:-}" ] || return 1; printf '%s\n' "$ROWS"; }
require_gum() { :; }
ui_keep_styles() { :; }
spin() { shift; "$@"; }
choose_index() {
  local n answer
  { printf '%s\n' "$@"; printf '%s\n' '-- end of menu --'; } >> "$CHOOSE_LOG"
  printf '%s\t%s\n' "$1" "${PULSAR_CHOOSE_DEFAULT:-}" >> "$DEFAULT_LOG"
  n=$(cat "$ANSWERS.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$ANSWERS.n"
  answer=$(sed -n "$((n + 1))p" "$ANSWERS")
  case "$answer" in
    "") echo "UNEXPECTED MENU" >> "$CHOOSE_LOG"; return 1 ;;
    esc) return 1 ;;
    ctrl-c) return 130 ;;
    [0-9]*) printf '%s\n' "$answer" ;;
    *)
      local index=0 option
      shift
      for option in "$@"; do
        [ "$option" != "$answer" ] || { printf '%s\n' "$index"; return 0; }
        index=$((index+1))
      done
      echo "UNEXPECTED MENU ANSWER: $answer" >> "$CHOOSE_LOG"; return 1 ;;
  esac
}
# Like the real one, it runs an executable (through env), never a shell function.
page_output() { [ -z "${PAGE_LOG:-}" ] || echo paged >> "$PAGE_LOG"; shift; env "$@"; }
wait_continue() {
  local rc=0
  choose_index "${1:-Review the output above}" "Continue" >/dev/null || rc=$?
  [ "$rc" -ne 130 ] || return 130
}
''')
        # ./pulsar: records each command, prints FIXTURES/KEY.N.json (the Nth
        # call) or KEY.json, exits with KEY.N.rc or KEY.rc (default 0), and
        # after a successful call with KEY.inventory, that becomes the inventory.
        pulsar = self.root / "pulsar"
        pulsar.write_text(r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PULSAR_LOG"
case "$*" in
  "inventory --json") key=inventory ;;
  "topology detect --json") key=detect; [ -z "${CHANGES_AFTER:-}" ] || cp "$CHANGES_AFTER" "$PULSAR_CLUSTER_CHANGES_FILE" ;;
  "model files --json") key=files ;;
  "model restore "*"--plan --json") key=restore-plan ;;
  "model acquire "*"--plan --json") key=acquire-plan ;;
  "model prepare "*"--plan --json") key=prepare-plan ;;
  "image stage "*"--plan --json") key=image-plan ;;
  "start "*"--dry-run --json") key=start-plan ;;
  "model acquire "*) key=acquire ;;
  "model restore "*) key=restore ;;
  "model prepare "*) key=prepare ;;
  "image stage "*) key=image ;;
  "stop "*) key=stop ;;
  "start "*) key=start ;;
  "topology configure"*) key=configure ;;
  "ssh-trust enroll") key=enroll ;;
  "models check "*) key=check ;;
  *) key=other ;;
esac
echo >> "$FIXTURES/$key.count"
n=$(wc -l < "$FIXTURES/$key.count")
file="$FIXTURES/$key.$n.json"; [ -e "$file" ] || file="$FIXTURES/$key.json"
[ ! -e "$file" ] || cat "$file"
rcfile="$FIXTURES/$key.$n.rc"; [ -e "$rcfile" ] || rcfile="$FIXTURES/$key.rc"
rc=0; [ ! -e "$rcfile" ] || rc=$(cat "$rcfile")
if [ "$rc" = 0 ] && [ -e "$FIXTURES/$key.inventory" ]; then cp "$FIXTURES/$key.inventory" "$FIXTURES/inventory.json"; fi
# Start records its blockers where PULSAR_START_BLOCKERS_FILE says, as the real one does.
blockers="$FIXTURES/$key.$n.blockers"; [ -e "$blockers" ] || blockers="$FIXTURES/$key.blockers"
if [ -n "${PULSAR_START_BLOCKERS_FILE:-}" ] && [ -e "$blockers" ]; then cat "$blockers" >> "$PULSAR_START_BLOCKERS_FILE"; fi
if [ "$rc" = 0 ] && [ "$key" = configure ] && [ -n "${SAVED_TOPOLOGY:-}" ]; then
  cp "$SAVED_TOPOLOGY" "$CLUSTER_TOPOLOGY_FILE"
fi
exit "$rc"
''')
        pulsar.chmod(0o755)
        for name, body in (("node-prerequisites.sh", 'echo "prerequisites $1" >> "$PULSAR_LOG"; cat "$FIXTURES/hf.json"'),
                           ("with-connect-card.sh", 'exec "$@"'),
                           ("cluster-overview.sh", 'echo "cluster check" >> "$PULSAR_LOG"')):
            script = scripts / name
            script.write_text(f"#!/usr/bin/env bash\n{body}\n")
            script.chmod(0o755)
        (self.fixtures / "hf.json").write_text(json.dumps(HF_OK))
        # The catalog is a double: list --json prints CATALOG_FILE; show and results print a line.
        binary = self.root / "bin"
        binary.mkdir()
        python = binary / "python3"
        python.write_text('#!/usr/bin/env bash\n'
                          'if [ "${1:-}" = -m ] && [ "${2:-}" = model_library.catalog ]; then\n'
                          '  case "${3:-}" in list) exec cat "$CATALOG_FILE" ;; *) echo "catalog ${*:3}"; exit 0 ;; esac\n'
                          f'fi\nexec {sys.executable} "$@"\n')
        python.chmod(0o755)
        for name in ("ssh", "docker", "curl", "nvidia-smi", "hf"):
            (binary / name).write_text('#!/usr/bin/env bash\necho "probe: $0" >> "$PULSAR_LOG"\nexit 1\n')
            (binary / name).chmod(0o755)
        self.catalog = self.root / "catalog.json"
        self.topology = self.root / "topology.json"
        self.topology.write_text(json.dumps(topology()))
        self.changes = self.root / "cluster-changes.json"
        self.save_changes(*changes())
        self.inventory(OTHER_RUNNING)

    def save_changes(self, record, minutes, kept):
        saved.save_changes(record, self.changes, clock=lambda: utc(NOW - timedelta(minutes=minutes)))
        if kept:
            saved.keep_changes(self.changes)

    def entries(self, *rows):
        self.catalog.write_text(json.dumps({"entries": list(rows)}))

    def fixture(self, key, document=None, rc=None, nth=None):
        name = f"{key}.{nth}" if nth else key
        if document is not None:
            (self.fixtures / f"{name}.json").write_text(json.dumps(document))
        if rc is not None:
            (self.fixtures / f"{name}.rc").write_text(str(rc))

    def inventory(self, document):
        self.fixture("inventory", document)

    def run_menu(self, answers, **extra):
        self.answers.write_text("".join(f"{answer}\n" for answer in answers))
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PULSAR_", "CLUSTER_", "GUM_")) and key not in ("VLLM_API_KEY", "API_KEY")}
        env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1", CHOOSE_LOG=str(self.choose_log),
                   DEFAULT_LOG=str(self.default_log), ANSWERS=str(self.answers), PULSAR_LOG=str(self.log),
                   FIXTURES=str(self.fixtures), CATALOG_FILE=str(self.catalog),
                   PATH=f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
                   CLUSTER_TOPOLOGY_FILE=str(self.topology), PULSAR_CLUSTER_CHANGES_FILE=str(self.changes),
                   PULSAR_SERVICE_INVENTORY_FILE=str(self.root / "service-inventory.json"),
                   PULSAR_STORAGE_BUDGET_FILE=str(self.root / "storage-budget.json"),
                   PULSAR_COLD_ROOT="", TMPDIR=str(self.root))
        env.update(extra)
        result = subprocess.run(["bash", str(self.root / "scripts/serve-menu.sh")], env=env, cwd=str(self.root),
                                text=True, capture_output=True, timeout=120)
        self.assertNotIn("UNEXPECTED MENU", self.choose_log.read_text() if self.choose_log.exists() else "",
                         result.stdout + result.stderr)
        return result

    def menus(self):
        # A card is a multi-line choice and may end with a blank line, so menus end with a marker.
        text = self.choose_log.read_text() if self.choose_log.exists() else ""
        return [[line for line in block.splitlines() if line.strip()]
                for block in text.split("-- end of menu --\n") if block.strip()]

    def commands(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def ran(self):
        """The commands that change something: no preview, no inventory."""
        return [line for line in self.commands() if "--json" not in line and not line.startswith("prerequisites")]

    def assert_plain_choices(self):
        """No choice outside Advanced names a low-level noun or a spec ID (design rule 1)."""
        for menu in self.menus():
            for label in menu[1:]:
                self.assertIsNone(FORBIDDEN.search(label), label)

    def assert_no_unapproved_authority(self):
        for line in self.commands():
            self.assertNotIn("--replace", line)
            self.assertNotIn("--pull-image", line)
            if "--pull" in line:
                self.assertTrue(line.startswith("image stage "), line)
            # --json keeps the exact-ID contract, and image takes no shortened ID.
            if "--json" in line or line.startswith("image "):
                for word in line.split():
                    if len(word) >= 12 and all(char in "0123456789abcdef" for char in word):
                        self.assertEqual(len(word), 64, line)


class Entry(Harness):
    def setUp(self):
        super().setUp()
        self.entries(checked(home=HOME, local_state="ready"))

    def test_a_recent_unchanged_check_goes_straight_to_the_list_without_discovery(self):
        result = self.run_menu(["Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["inventory --json"])
        self.assertEqual(self.menus()[0][0], "Serve a model")
        self.assertIn("ready to start", " ".join(self.menus()[0]))
        self.assert_plain_choices()

    def test_an_old_check_runs_one_discovery_first(self):
        self.save_changes(*changes(minutes=20))
        record = self.root / "after.json"
        saved.save_changes(changes()[0], record)
        result = self.run_menu(["Back"], CHANGES_AFTER=str(record))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["topology detect --json", "inventory --json"])

    def test_a_discovery_that_saves_nothing_is_not_read_as_unchanged(self):
        self.save_changes(*changes(minutes=20))
        self.fixture("detect", rc=1)
        result = self.run_menu(["Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus(), [["Serve a model", "Serve on the cluster as saved", "Back"]])
        self.assertIn("✗ The cluster check did not finish", result.stdout)
        self.assertEqual(self.ran(), [])

    def test_a_kept_node_is_not_asked_about_again(self):
        self.save_changes(*changes(missing=["n2"], kept=True))
        result = self.run_menu(["Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0][0], "Serve a model")

    def test_a_change_opens_review_with_serve_as_saved_and_a_plan_on_a_silent_node_does_not_run(self):
        self.save_changes(*changes(missing=["n2"]))
        self.inventory(NOTHING_RUNNING)
        result = self.run_menu(["Serve on the cluster as saved", "0", "Cancel", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        review = self.menus()[0]
        self.assertEqual(review, ["Review what changed", "Keep spark-3; it is only down",
                                  "Save the cluster without spark-3", "Serve on the cluster as saved", "Back"])
        plan = self.menus()[2]
        self.assertNotIn("Go ahead", plan)
        self.assertIn("spark-3 did not answer the last cluster check", " ".join(result.stdout.split()))
        self.assert_plain_choices()

    def test_a_running_service_offers_stop_save_then_continue(self):
        self.save_changes(*changes(missing=["n2"]))
        (self.root / "service-inventory.json").write_text(json.dumps({**OTHER_RUNNING, "generated_at": utc(NOW)}))
        self.entries(checked(home=HOME, local_state="ready"),
                     checked(spec_id=OTHER, model_id="org/other", home=HOME, local_state="ready"))
        new = self.root / "saved-topology.json"
        two = topology()
        two["topology_id"] = "b" * 64
        two["nodes"] = two["nodes"][:2]
        new.write_text(json.dumps(two))
        self.fixture("stop", rc=0)
        (self.fixtures / "stop.inventory").write_text(json.dumps(NOTHING_RUNNING))
        result = self.run_menu(["Stop the running service, save the change, then continue", "Go ahead", "Back"],
                               SAVED_TOPOLOGY=str(new))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Stop the running service, save the change, then continue", self.menus()[0])
        self.assertEqual(self.menus()[1], ["Stop, save, then continue", "Cancel", "Go ahead"])
        self.assertEqual(self.ran(), [f"stop {OTHER}", "topology configure --remove-missing", "ssh-trust enroll"])
        text = " ".join(result.stdout.split())
        self.assertIn("Nothing serves from step 1 until that Start.", text)
        self.assertEqual(self.menus()[2][0], "Serve a model")


class Switch(Harness):
    """A ready model with another service in the way: Stop, then Start."""

    def setUp(self):
        super().setUp()
        self.entries(checked(home=HOME, local_state="ready"),
                     checked(spec_id=OTHER, model_id="org/other", home=HOME, local_state="ready"))
        self.fixture("prepare-plan", envelope(prepare_plan(("n0", "reuse"), ("n1", "reuse"), ("n2", "reuse"))))
        self.fixture("image-plan", envelope(image_plan((0, "ok"), (1, "ok"), (2, "ok"))))
        self.fixture("start-plan", start_blocked("port_in_use"), rc=1)
        (self.fixtures / "stop.inventory").write_text(json.dumps(NOTHING_RUNNING))

    def test_the_plan_starts_on_cancel_and_go_ahead_stops_then_starts(self):
        result = self.run_menu(["0", "Go ahead", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = self.menus()[1]
        self.assertEqual(plan, ["Serve a model", "Cancel", "Go ahead", "Details"])
        self.assertIn("Serve a model\t0", self.default_log.read_text().splitlines())
        # One inventory serves the list, the plan and Stop's own check.
        self.assertEqual(self.commands(), ["inventory --json", f"model prepare {SPEC} --plan --json",
                                           f"image stage {SPEC} --plan --json",
                                           f"start {SPEC} --dry-run --json",
                                           f"stop {OTHER}", f"start {SPEC}"])
        text = " ".join(result.stdout.split())
        self.assertIn("1. Stop org/other on spark-1, spark-2 and spark-3.", text)
        self.assertIn("org/other keeps serving until step 1. If step 2 fails, nothing is serving", text)
        self.assertIn("✓ Serving: org/model started on spark-1, spark-2 and spark-3.", text)
        self.assertIn("Same as: ./pulsar stop cdcdcdcdcdcd ./pulsar start abababababab", text)
        self.assert_plain_choices()
        self.assert_no_unapproved_authority()

    def test_cancel_changes_nothing(self):
        result = self.run_menu(["0", "Cancel", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [])

    def test_a_failed_start_after_the_stop_says_nothing_serves_and_offers_the_old_service(self):
        self.fixture("start", rc=1, nth=1)
        result = self.run_menu(["0", "Go ahead", "Start org/other again", "Continue"])
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("✗ Step 2 did not finish: Start the service and wait until it answers.", text)
        self.assertIn("Nothing is serving now: org/other was stopped before Start.", text)
        # The failed Start ran too, so it is named with the Stop.
        self.assertIn("Same as: ./pulsar stop cdcdcdcdcdcd ./pulsar start abababababab", text)
        self.assertEqual(self.menus()[2], ["Serve a model", "Start org/other again", "Back"])
        self.assertEqual(self.ran(), [f"stop {OTHER}", f"start {SPEC}", f"start {OTHER}"])

    def blockers(self, *codes, nth=1):
        (self.fixtures / f"start.{nth}.blockers").write_text("".join(
            json.dumps({"blocker": code, "stage": "check", "message": f"{code} found",
                        "detail": "spark-2 has 9 GiB free; 12 GiB recommended" if code == "memory_warning" else None})
            + "\n" for code in codes))

    def test_a_start_refused_only_for_a_memory_warning_offers_starting_anyway_by_name(self):
        self.fixture("start", rc=1, nth=1)
        self.blockers("memory_warning")
        result = self.run_menu(["0", "Go ahead", "Start org/model with less free memory than recommended",
                                "Continue"])
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("Start refused only because free memory is below the recommended headroom: spark-2 has "
                      "9 GiB free; 12 GiB recommended.", text)
        self.assertEqual(self.menus()[2], ["Serve a model", "Start org/other again",
                                           "Start org/model with less free memory than recommended", "Back"])
        # The acknowledgement runs only after that choice, never as part of the approved plan.
        self.assertEqual(self.ran(), [f"stop {OTHER}", f"start {SPEC}", f"start {SPEC} --accept-memory-warn"])
        self.assertIn("Same as: ./pulsar start abababababab --accept-memory-warn", text)
        self.assert_plain_choices()

    def test_starting_anyway_is_offered_for_no_other_blocker(self):
        for codes, rc in ((("memory_warning", "port_in_use"), 1), (("memory_insufficient",), 1),
                          (("memory_warning",), 130), (("memory_warning",), 3)):
            with self.subTest(codes=codes, rc=rc):
                for path in {*self.fixtures.glob("*.count"), *self.fixtures.glob("start.*")}:
                    path.unlink()
                for path in (self.log, self.choose_log, self.answers.with_suffix(".n")):
                    path.unlink(missing_ok=True)
                self.inventory(OTHER_RUNNING)
                self.fixture("start", rc=rc, nth=1)
                self.blockers(*codes)
                result = self.run_menu(["0", "Go ahead", "Back"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("less free memory", " ".join(" ".join(menu) for menu in self.menus()))
                self.assertFalse(any("--accept-memory-warn" in line for line in self.commands()))

    def test_a_service_that_appears_before_its_stop_stops_the_run(self):
        # This recipe's exited containers and org/other are in the way; after the
        # first Stop the next one takes a new inventory, which sees a third service.
        third = "ef" * 32
        self.inventory(inventory((SPEC, "stale", ["head", "w1", "w2"]), (OTHER, "running", ["head", "w1", "w2"])))
        self.fixture("inventory", inventory((OTHER, "running", ["head", "w1", "w2"]), (third, "running", ["w2"])),
                     nth=2)
        (self.fixtures / "stop.inventory").unlink()
        self.fixture("start-plan", start_blocked("service_exists", "port_in_use"), rc=1)
        result = self.run_menu(["0", "Go ahead", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [f"stop {SPEC}"])
        self.assertEqual(self.commands().count("inventory --json"), 2)
        text = " ".join(result.stdout.split())
        self.assertIn("✗ Step 2 did not run: served name not recorded now runs on a serving node too, and the plan "
                      "did not say to stop it.", text)
        self.assertIn("Still serving: org/other.", text)


class NeedsEverything(Harness):
    """No home, no archive: download, copy, image, Stop, Start."""

    def setUp(self):
        super().setUp()
        self.entries(row(), checked(spec_id=OTHER, model_id="org/other", home=HOME, local_state="ready"))
        self.fixture("files", {"schema_version": 1, "ok": True, "result": {
            "schema_version": 1, "kind": "pulsar-model-file-inventory", "observed_at": utc(NOW), "nodes": [],
            "snapshots": [], "issues": []}})
        self.fixture("image-plan", envelope(image_plan((0, "missing"), (1, "ok"), (2, "missing"))))
        # Its own check at its turn asks with --pull, as the plan says.
        self.fixture("image-plan", envelope(image_plan((0, "missing"), (1, "ok"), (2, "missing"),
                                                       mode="pull-exact-digest")), nth=2)
        self.fixture("start-plan", start_blocked("model_files_not_ready", "image_missing", "port_in_use"), rc=1)
        self.fixture("acquire-plan", envelope({"kind": "pulsar-acquisition-plan", "selected_node": "n0"}))
        self.fixture("prepare-plan", envelope(prepare_plan(("n0", "home-view"), ("n1", "copy"), ("n2", "copy"))))
        (self.fixtures / "stop.inventory").write_text(json.dumps(NOTHING_RUNNING))

    def test_each_step_checks_first_then_runs_in_order(self):
        result = self.run_menu(["0", "Go ahead", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Serve a model", "Cancel", "Go ahead", "Get it ready, but do not start",
                                           "Change…", "Details"])
        # hf is checked on the download node before the plan is shown; no download preview runs up front.
        self.assertEqual(self.commands()[:5], ["inventory --json", "model files --json", "prerequisites n0",
                                               f"image stage {SPEC} --plan --json",
                                               f"start {SPEC} --dry-run --json"])
        self.assertEqual(self.commands()[5:], [
            f"model acquire {SPEC} --node spark-1 --plan --json",
            f"model acquire {SPEC} --node spark-1 --yes",
            f"model prepare {SPEC} --plan --json", f"model prepare {SPEC} --yes",
            f"image stage {SPEC} --pull --plan --json", f"image stage {SPEC} --pull --yes",
            f"stop {OTHER}", f"start {SPEC}"])
        text = " ".join(result.stdout.split())
        self.assertIn("3. Pull the serving image on spark-1 and spark-3 (exact digest, from its registry).", text)
        self.assertIn("Steps 1, 2 and 5 are estimates until they run", text)
        self.assert_plain_choices()
        self.assert_no_unapproved_authority()

    def test_a_plan_that_does_not_fit_the_rows_opens_in_the_pager(self):
        pages = self.root / "pages.log"
        for rows, paged in (("60", False), ("24", True)):
            with self.subTest(rows=rows):
                pages.unlink(missing_ok=True)
                self.log.unlink(missing_ok=True)
                for count in self.fixtures.glob("*.count"):
                    count.unlink()
                self.answers.with_suffix(".n").unlink(missing_ok=True)
                self.choose_log.unlink(missing_ok=True)
                result = self.run_menu(["0", "Cancel", "Back"], ROWS=rows, PAGE_LOG=str(pages))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(pages.exists(), paged)
                # Either way the plan is shown once, before its choices.
                self.assertEqual(result.stdout.count("Pulsar will:"), 1)

    def test_every_choice_in_change_details_and_the_outcomes_uses_plain_words(self):
        result = self.run_menu(["0", "Change…", "Back", "Details", "Recipe details", "Published results", "Back",
                                "Go ahead", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[2], ["Change the plan", "Keep the files on another node…", "Back"])
        self.assertEqual(self.menus()[4], ["Details", "Recipe details", "Published results", "Back"])
        self.assert_plain_choices()
        self.fixture("acquire", rc=130)
        self.answers.with_suffix(".n").unlink()
        self.choose_log.unlink()
        self.run_menu(["0", "Go ahead", "Back"])
        self.assert_plain_choices()

    def test_get_it_ready_drops_the_stop_and_the_start(self):
        result = self.run_menu(["0", "Get it ready, but do not start", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [f"model acquire {SPEC} --node spark-1 --yes",
                                      f"model prepare {SPEC} --yes", f"image stage {SPEC} --pull --yes"])
        self.assertIn("✓ Ready: org/model has its files and image on", " ".join(result.stdout.split()))

    def test_a_step_whose_own_preview_differs_stops_the_run_before_it_changes_anything(self):
        self.fixture("prepare-plan", envelope(prepare_plan(("n0", "copy"), ("n1", "copy"), ("n2", "copy"))))
        result = self.run_menu(["0", "Go ahead", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [f"model acquire {SPEC} --node spark-1 --yes"])
        text = " ".join(result.stdout.split())
        self.assertIn("✗ Step 2 did not run: it would also copy the files to spark-1. Pulsar stopped before it, "
                      "so it can ask again.", text)
        self.assertIn("Still serving: org/other.", text)
        self.assertEqual(self.menus()[2], ["Serve a model", "Plan again from the state as it is now", "Back"])
        self.assert_plain_choices()

    def test_an_interrupted_step_is_inspected_before_any_retry(self):
        self.fixture("acquire", rc=130)
        result = self.run_menu(["0", "Go ahead", "Check its files now (read-only)", "Continue"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran(), [f"model acquire {SPEC} --node spark-1 --yes", "models check abababababab"])
        self.assertIn("Its outcome is not confirmed; inspect before retrying.", " ".join(result.stdout.split()))

    def test_without_a_usable_hf_cli_the_download_node_blocks_and_change_offers_another(self):
        (self.fixtures / "hf.json").write_text(json.dumps(hf(state="missing", login="unknown")))
        result = self.run_menu(["0", "Change…", "Keep the files on another node…", "spark-2", "Cancel", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Serve a model", "Cancel", "Change…", "Details"])
        self.assertIn("Downloads to spark-1 need the hf CLI", " ".join(result.stdout.split()))
        self.assertEqual(self.menus()[3], ["Which node should keep the files?", "spark-1 (now)", "spark-2", "spark-3",
                                           "Back"])
        self.assertIn("prerequisites n1", self.commands())
        self.assertEqual(self.ran(), [])
        self.assert_plain_choices()


class Sources(Harness):
    def test_an_archive_restores_without_asking_the_hub(self):
        archive = self.root / "archive"
        archive.mkdir()
        self.entries(checked(archive_state="verified"))
        self.inventory(NOTHING_RUNNING)
        self.fixture("files", envelope({"schema_version": 1, "kind": "pulsar-model-file-inventory",
                                        "observed_at": utc(NOW), "nodes": [], "snapshots": [], "issues": []}))
        self.fixture("restore-plan", envelope({"kind": "pulsar-restore-plan", "selected_node": "n0",
                                               "destination_space": {"available": 10 ** 13}}))
        result = self.run_menu(["0", "Cancel", "Back"], PULSAR_COLD_ROOT=str(archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"model restore {SPEC} --node spark-1 --plan --json", self.commands())
        self.assertFalse(any(line.startswith(("prerequisites", "model acquire")) for line in self.commands()))
        self.assertIn("1. Restore the model files from the archive to spark-1", " ".join(result.stdout.split()))

    def test_a_one_node_recipe_asks_its_machine_starting_where_its_files_are(self):
        self.entries(checked(geometry={"nodes": 1, "tp": 1, "pp": 1}, home={**HOME, "node_id": "n1"},
                             local_state="ready"))
        self.inventory(NOTHING_RUNNING)
        self.fixture("prepare-plan", envelope(prepare_plan(("n1", "reuse"))))
        self.fixture("image-plan", envelope(image_plan((1, "ok"))))
        self.fixture("start-plan", envelope({"checks": []}))
        result = self.run_menu(["0", "1", "Go ahead", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Which machine should serve org/model?", "spark-1",
                                           "spark-2 — its model files are here", "spark-3", "Back"])
        self.assertIn("Which machine should serve org/model?\t1", self.default_log.read_text().splitlines())
        self.assertIn(f"start {SPEC} --node spark-2 --dry-run --json", self.commands())
        self.assertEqual(self.ran(), [f"start {SPEC} --node spark-2"])
        self.assert_plain_choices()


if __name__ == "__main__":
    unittest.main()
