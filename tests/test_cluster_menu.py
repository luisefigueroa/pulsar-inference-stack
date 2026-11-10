"""Cluster…: the frame from saved records, Check the cluster now, Review what
changed…, Settings… and Start over, driven with doubles; nothing probes a node."""
from datetime import datetime, timedelta, timezone
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import cluster_status, saved_observations as saved  # noqa: E402
from tests.test_inventory_menu import snapshot  # noqa: E402

# Words a choice outside Advanced must not use (design rule 1): the operator
# chooses by what they want; low-level nouns appear only in consequence lines.
FORBIDDEN = re.compile(r"\bhomes?\b|\bpin|\bpurge|prepared cop|\branks?\b|\bsnapshots?\b|\benrol|[0-9a-f]{12,}",
                       re.IGNORECASE)


def utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def node(rank, hostname, node_id):
    return {"rank": rank, "hostname": hostname, "node_id": node_id}


def enrolled_two_node(topology_id="a" * 64):
    return {"schema_version": 2, "topology_id": topology_id,
            "nodes": [node(0, "fixture-local", "node-0"), node(1, "fixture-remote", "node-1")],
            "validation": {"ssh_identity_enrolled": True}}


def saved_inventory():
    """A saved service inventory, taken now: a Pulsar service runs on fixture-remote."""
    document = snapshot()
    document["generated_at"] = utc(datetime.now(timezone.utc))
    return json.dumps(document)


CLUSTER =["Cluster", "Check the cluster now", "Settings… (archive location, API key)",
           "Start over: forget this cluster…", "Back"]
NOW = datetime.now(timezone.utc)


def three_nodes(topology_id="a" * 64):
    topology = enrolled_two_node(topology_id)
    topology["nodes"] = [node(index, f"fixture-rank-{index}", f"node-{index}") for index in range(3)]
    return topology


def body(topology_id="a" * 64, **fields):
    record = {"topology_id": topology_id, "result": "unchanged", "issues": [], "found": [], "missing": [],
              "changed": [], "untrusted": [], **fields}
    if record["found"] or record["missing"] or record["changed"]:
        record["result"] = "changed"
    return record


MISSING = [{"hostname": "fixture-rank-2", "node_id": "node-2", "position": 2, "answered": False}]
FOUND = [{"hostname": "fixture-new", "node_id": "node-9", "ssh_host": "fixture-new.local",
          "possible_reinstall": False}]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("cluster-menu.sh", "cluster-review.sh"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
            (scripts / name).chmod(0o755)
        self.choose_log = self.root / "choose.log"
        self.answers = self.root / "answers"
        self.log = self.root / "pulsar.log"
        (scripts / "ui.sh").write_text(r'''
emit_frame() { printf 'FRAME\n'; cat; printf 'END FRAME\n'; }
_ui_terminal_columns() { return 1; }
require_gum() { :; }
ui_keep_styles() { :; }
choose_index() {
  local n answer
  { printf '%s\n' "$@"; printf '\n'; } >> "$CHOOSE_LOG"
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
# ROWS is the terminal's height; unset, it is not known. PAGE_LOG records each page.
_ui_terminal_rows() { [ -n "${ROWS:-}" ] || return 1; printf '%s\n' "$ROWS"; }
page_output() { [ -z "${PAGE_LOG:-}" ] || echo paged >> "$PAGE_LOG"; shift; "$@"; }
wait_continue() {
  local rc=0
  choose_index "${1:-Review the output above}" "Continue" >/dev/null || rc=$?
  [ "$rc" -ne 130 ] || return 130
}
''')
        # pulsar: records each command. configure saves SAVED_TOPOLOGY when it
        # exits 0 and one is given (a declined save leaves the file); forget
        # removes membership; every command takes the next PULSAR_RC status.
        pulsar = self.root / "pulsar"
        pulsar.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$PULSAR_LOG"\n'
                          'n=$(wc -l < "$PULSAR_LOG")\n'
                          'rc=$(printf "%s" "${PULSAR_RC:-0}" | cut -d, -f"$n")\n'
                          'case "$*" in\n'
                          '  "topology configure"*) [ "${rc:-0}" != 0 ] || [ -z "${SAVED_TOPOLOGY:-}" ] '
                          '|| cp "$SAVED_TOPOLOGY" "$CLUSTER_TOPOLOGY_FILE" ;;\n'
                          '  "topology forget") [ "${rc:-0}" != 0 ] || [ -n "${KEEP_MEMBERSHIP:-}" ] '
                          '|| rm -f "$CLUSTER_TOPOLOGY_FILE" ;;\nesac\n'
                          'exit "${rc:-0}"\n')
        pulsar.chmod(0o755)
        overview = scripts / "cluster-overview.sh"
        overview.write_text('#!/usr/bin/env bash\necho "cluster check" >> "$PULSAR_LOG"\n'
                            '[ -z "${CHANGES_AFTER:-}" ] || cp "$CHANGES_AFTER" "$PULSAR_CLUSTER_CHANGES_FILE"\n'
                            'exit "${CHECK_RC:-0}"\n')
        overview.chmod(0o755)
        binary = self.root / "bin"
        binary.mkdir()
        for name in ("ssh", "docker", "curl", "nvidia-smi", "hf", "avahi-browse"):
            (binary / name).write_text('#!/usr/bin/env bash\necho "probe: $0" >> "$PULSAR_LOG"\nexit 1\n')
            (binary / name).chmod(0o755)
        self.topology = self.root / "topology.json"
        self.topology.write_text(json.dumps(three_nodes()))
        (self.root / "saved").mkdir()
        self.changes = self.root / "saved/cluster-changes.json"
        self.inventory = self.root / "saved/service-inventory.json"

    def save(self, record, minutes=2):
        saved.save_changes(record, self.changes, clock=lambda: utc(NOW - timedelta(minutes=minutes)))

    def run_script(self, name, answers, **extra):
        self.answers.write_text("".join(f"{answer}\n" for answer in answers))
        self.answers.with_suffix(".n").unlink(missing_ok=True)
        self.choose_log.unlink(missing_ok=True)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PULSAR_", "CLUSTER_", "GUM_")) and key not in ("VLLM_API_KEY", "API_KEY")}
        env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1", CHOOSE_LOG=str(self.choose_log),
                   ANSWERS=str(self.answers), PULSAR_LOG=str(self.log),
                   PATH=f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
                   CLUSTER_TOPOLOGY_FILE=str(self.topology), PULSAR_CLUSTER_CHANGES_FILE=str(self.changes),
                   PULSAR_SERVICE_INVENTORY_FILE=str(self.inventory),
                   PULSAR_STORAGE_BUDGET_FILE=str(self.root / "saved/storage-budget.json"),
                   PULSAR_COLD_ROOT="", **extra)
        result = subprocess.run(["bash", str(self.root / "scripts" / name)], env=env, cwd=str(self.root),
                                text=True, capture_output=True, timeout=60)
        self.assertNotIn("UNEXPECTED MENU", self.choose_log.read_text() if self.choose_log.exists() else "")
        return result

    def menus(self):
        text = self.choose_log.read_text() if self.choose_log.exists() else ""
        return [block.splitlines() for block in text.strip().split("\n\n") if block.strip()]

    def commands(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class ClusterMenu(Harness):
    def test_the_frame_comes_from_saved_records_and_opening_probes_nothing(self):
        self.save(body(), minutes=120)
        result = self.run_script("cluster-menu.sh", ["Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus(), [CLUSTER])
        self.assertEqual(self.commands(), [])
        frame = result.stdout.split("FRAME\n", 1)[1].split("END FRAME", 1)[0]
        self.assertEqual(frame.splitlines(), [
            "Nodes      3 · fixture-rank-0, fixture-rank-1, fixture-rank-2",
            "Checked    all answered 2 hours ago · SSH identities recorded",
            "Changes    none noticed (2 hours ago)"])

    def test_before_any_check_the_frame_says_so(self):
        result = self.run_script("cluster-menu.sh", ["Back"])
        self.assertIn("Checked    not checked yet; Check the cluster now looks", result.stdout)
        self.assertNotIn("Changes", result.stdout)

    def test_a_change_offers_review_and_the_frame_names_it_with_its_age(self):
        self.save(body(missing=MISSING))
        result = self.run_script("cluster-menu.sh", ["Review what changed…", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0], CLUSTER[:2] + ["Review what changed…"] + CLUSTER[2:])
        self.assertIn("Changes    fixture-rank-2 did not answer (2 minutes ago)", result.stdout)
        self.assertIn("Checked    2 of 3 answered 2 minutes ago", result.stdout)
        self.assertEqual(self.menus()[1][0], "Review what changed")

    def test_check_the_cluster_now_runs_the_check_and_the_frame_redraws_only_on_change(self):
        self.save(body())
        unchanged = self.run_script("cluster-menu.sh", ["Check the cluster now", "Back"])
        self.assertEqual(self.commands(), ["cluster check"])
        self.assertEqual(unchanged.stdout.count("END FRAME"), 1)
        self.log.unlink()
        after = self.root / "after.json"
        saved.save_changes(body(missing=MISSING), after)
        changed = self.run_script("cluster-menu.sh", ["Check the cluster now", "Back"], CHANGES_AFTER=str(after))
        self.assertEqual(changed.stdout.count("END FRAME"), 2)
        self.assertEqual(self.menus()[1][2], "Review what changed…")

    def test_settings_open_the_archive_menu_and_say_where_an_api_key_is_set(self):
        result = self.run_script("cluster-menu.sh", ["Settings… (archive location, API key)", "Archive location…",
                                                     "API key", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Settings", "Archive location…", "API key", "Back"])
        self.assertEqual(self.commands(), ["configure archive-root menu"])
        text = " ".join(result.stdout.split())
        self.assertIn("API key not configured: the API accepts requests without a key. To require one, set "
                      "VLLM_API_KEY in this checkout's .env file", text)
        result = self.run_script("cluster-menu.sh", ["1", "API key", "Back", "Back"], VLLM_API_KEY="fixture-secret")
        text = " ".join(result.stdout.split())
        self.assertIn("API key configured: VLLM_API_KEY is set in the environment that started Pulsar.", text)
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)

    def test_start_over_forgets_then_returns_home(self):
        result = self.run_script("cluster-menu.sh", ["Start over: forget this cluster…"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["topology forget"])
        # Membership is gone, so no menu is drawn again: home offers setup.
        self.assertEqual(len(self.menus()), 1)
        self.topology.write_text(json.dumps(three_nodes()))
        self.log.unlink()
        declined = self.run_script("cluster-menu.sh", ["Start over: forget this cluster…", "Back"],
                                   KEEP_MEMBERSHIP="1")
        self.assertEqual(declined.returncode, 0, declined.stderr)
        self.assertEqual([menu[0] for menu in self.menus()], ["Cluster", "Cluster"])

    def test_esc_returns_and_ctrl_c_stops(self):
        self.assertEqual(self.run_script("cluster-menu.sh", ["esc"]).returncode, 0)
        self.assertEqual(self.run_script("cluster-menu.sh", ["ctrl-c"]).returncode, 130)
        self.assertEqual(self.run_script("cluster-menu.sh", ["0"], CHECK_RC="130").returncode, 130)
        self.assertEqual(self.run_script("cluster-menu.sh", ["1", "ctrl-c"]).returncode, 130)

    def test_without_membership_the_menu_returns_home(self):
        self.topology.unlink()
        result = self.run_script("cluster-menu.sh", [])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus(), [])


class Review(Harness):
    def test_a_new_machine_is_saved_then_identities_are_recorded_with_their_own_question(self):
        self.save(body(found=FOUND))
        new = self.root / "saved-topology.json"
        new.write_text(json.dumps(three_nodes("c" * 64)))
        result = self.run_script("cluster-review.sh", ["Save the cluster as it is now", "Continue"],
                                 SAVED_TOPOLOGY=str(new))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0], ["Review what changed", "Save the cluster as it is now", "Back"])
        # The menu's choice leads to configure's own question; it asks nothing itself.
        self.assertEqual(self.commands(), ["topology configure", "ssh-trust enroll"])
        text = " ".join(result.stdout.split())
        self.assertIn("New machine fixture-new (fixture-new.local) answered and qualifies; it is not a member.", text)
        self.assertIn("Positions fixture-rank-0 0 → 0 · fixture-rank-1 1 → 1 · fixture-rank-2 2 → 2 · "
                      "fixture-new new → 3. Multi-node recipes use the first nodes in this order.", text)
        self.assertIn("Prepare takes over each copy that still verifies on the same node, and nothing is copied "
                      "again.", text)
        self.assertIn("Same as ./pulsar topology configure, then ./pulsar ssh-trust enroll", text)
        self.assertEqual(self.menus()[1], ["Cluster saved", "Continue"])

    def test_review_prints_above_its_choices_when_it_fits_and_pages_when_it_does_not(self):
        self.save(body(found=FOUND))
        pages = self.root / "pages.log"
        for rows, paged in (("60", False), (None, False), ("12", True)):
            with self.subTest(rows=rows):
                pages.unlink(missing_ok=True)
                extra = {"PAGE_LOG": str(pages), **({"ROWS": rows} if rows else {})}
                result = self.run_script("cluster-review.sh", ["Back"], **extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(pages.exists(), paged)
                # Either way the screen is shown once, before its choices.
                self.assertEqual(result.stdout.count("Review what changed"), 1)
                self.assertIn("New machine", result.stdout)

    def test_a_declined_or_failed_save_changes_nothing_and_records_no_identities(self):
        self.save(body(found=FOUND))
        declined = self.run_script("cluster-review.sh", ["Save the cluster as it is now"])
        self.assertEqual(declined.returncode, 0, declined.stderr)
        self.assertEqual(self.commands(), ["topology configure"])
        self.assertIn("Nothing was saved; the cluster is unchanged.", declined.stdout)
        self.log.unlink()
        failed = self.run_script("cluster-review.sh", ["Save the cluster as it is now", "Continue"], PULSAR_RC="1")
        self.assertEqual(self.commands(), ["topology configure"])
        self.assertIn("✗ The cluster was not saved; the reason is above. Nothing changed.", failed.stdout)
        self.assertEqual(self.menus()[1], ["Cluster not saved", "Continue"])

    def test_a_node_that_did_not_answer_can_be_kept_or_left_out(self):
        self.save(body(missing=MISSING))
        result = self.run_script("cluster-review.sh", ["Keep fixture-rank-2; it is only down"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0], ["Review what changed", "Keep fixture-rank-2; it is only down",
                                           "Save the cluster without fixture-rank-2", "Back"])
        self.assertEqual(self.commands(), [])
        self.assertEqual(saved.load_changes(self.changes)["kept"]["missing"], ["node-2"])
        self.assertIn("Pulsar asks again only when a later check sees different changes", " ".join(result.stdout.split()))
        # Kept: the question is not asked again, and saving without it stays possible.
        new = self.root / "saved-topology.json"
        new.write_text(json.dumps(enrolled_two_node("d" * 64)))
        result = self.run_script("cluster-review.sh", ["Save the cluster without fixture-rank-2", "Continue"],
                                 SAVED_TOPOLOGY=str(new))
        self.assertEqual(self.menus()[0], ["Review what changed", "Save the cluster without fixture-rank-2", "Back"])
        self.assertIn("You kept it as only down", " ".join(result.stdout.split()))
        self.assertEqual(self.commands(), ["topology configure --remove-missing", "ssh-trust enroll"])
        text = " ".join(result.stdout.split())
        self.assertIn("Positions fixture-rank-0 0 → 0 · fixture-rank-1 1 → 1 · fixture-rank-2 2 → left out.", text)

    def test_a_reinstalled_machine_joins_after_the_saved_nodes(self):
        reinstall = [{"hostname": "fixture-rank-1", "node_id": "node-8", "ssh_host": "fixture-rank-1.local",
                      "possible_reinstall": True}]
        missing = [{"hostname": "fixture-rank-1", "node_id": "node-1", "position": 1, "answered": False}]
        self.save(body(found=reinstall, missing=missing))
        result = self.run_script("cluster-review.sh", ["Back"])
        text = " ".join(result.stdout.split())
        self.assertIn("so it may be fixture-rank-1 reinstalled. Pulsar knows a node by its OS machine ID", text)
        self.assertIn("Positions fixture-rank-0 0 → 0 · fixture-rank-2 2 → 1 · fixture-rank-1 new → 2 · "
                      "fixture-rank-1 1 → left out.", text)

    def test_a_running_service_refuses_the_save_and_names_how_to_stop_it(self):
        self.save(body(missing=MISSING))
        self.inventory.write_text(saved_inventory())
        result = self.run_script("cluster-review.sh", ["Check the cluster now"])
        self.assertEqual(self.menus()[0], ["Review what changed", "Keep fixture-rank-2; it is only down",
                                           "Check the cluster now", "Back"])
        text = " ".join(result.stdout.split())
        self.assertIn("Refused Saving is refused while a Pulsar service runs:", text)
        self.assertIn("running on fixture-remote", text)
        self.assertIn("Stop it under Running services… (same as: ./pulsar stop SPEC), then Check the cluster now.",
                      text)
        self.assertEqual(self.commands(), ["cluster check"])

    def test_exited_containers_do_not_refuse_the_save(self):
        # The rewrite's idle rule asks for running containers only.
        self.save(body(found=FOUND))
        document = json.loads(saved_inventory())
        document["services"][0]["state"] = "stale"
        self.inventory.write_text(json.dumps(document))
        result = self.run_script("cluster-review.sh", ["Back"])
        self.assertEqual(self.menus()[0], ["Review what changed", "Save the cluster as it is now", "Back"])
        self.assertNotIn("Refused", result.stdout)

    def test_a_member_that_answered_but_cannot_be_a_member_is_never_left_out(self):
        rejected = [{**MISSING[0], "answered": True, "reasons": ["Docker NVIDIA runtime missing"]}]
        self.save(body(missing=rejected))
        result = self.run_script("cluster-review.sh", ["Back"])
        self.assertEqual(self.menus()[0], ["Review what changed", "Check the cluster now", "Back"])
        self.assertIn("Pulsar never leaves out a node that answered", " ".join(result.stdout.split()))

    def test_without_a_current_record_review_offers_the_check(self):
        for record in (None, body("e" * 64, missing=MISSING)):
            with self.subTest(stale=record is not None):
                self.changes.unlink(missing_ok=True)
                if record:
                    self.save(record)
                self.log.unlink(missing_ok=True)
                result = self.run_script("cluster-review.sh", ["Check the cluster now"])
                self.assertEqual(self.menus()[0], ["Review what changed", "Check the cluster now", "Back"])
                expected = "not checked since membership was saved" if record else "not checked yet"
                self.assertIn(expected, result.stdout)
                self.assertEqual(self.commands(), ["cluster check"])
        # Inside the check, the check runs again there.
        result = self.run_script("cluster-review.sh", ["Check the cluster now"], _PULSAR_REVIEW_FROM_CHECK="1")
        self.assertEqual(result.returncode, 10)


class Vocabulary(Harness):
    def test_no_choice_in_the_cluster_area_uses_a_low_level_term(self):
        labels = []
        for record, answers, script in (
                (body(missing=MISSING, found=FOUND), ["Review what changed…", "Back", "2", "Back", "Back"],
                 "cluster-menu.sh"),
                (body(missing=MISSING), ["Back"], "cluster-review.sh"),
                (body(found=FOUND), ["Back"], "cluster-review.sh")):
            self.save(record)
            result = self.run_script(script, answers)
            self.assertEqual(result.returncode, 0, result.stderr)
            labels += [label for menu in self.menus() for label in menu[1:]]
        self.assertIn("Settings… (archive location, API key)", labels)
        self.assertIn("Save the cluster without fixture-rank-2", labels)
        # A hostname is the operator's own data, not a word the menu chose.
        words = [re.sub(r"fixture-(rank-\d|new)", "HOST", label) for label in labels]
        self.assertEqual([label for label in words if FORBIDDEN.search(label)], [])


# Every menu in scripts/, by its script and the title its choose_index call is
# given as written. ADVANCED: a menu kept unchanged under Help and
# troubleshooting… → Advanced: single operations… (D11), where operation names
# stay. Every other menu names the tests that check its labels against FORBIDDEN.
ADVANCED = "advanced"
MENUS = {
    ("home.sh", '"Pulsar Inference Stack"'): ["tests.test_home_menu.Vocabulary"],
    ("home.sh", '"Help and troubleshooting"'): ["tests.test_home_menu.Vocabulary"],
    ("home.sh", '"Advanced: single operations"'): ADVANCED,
    ("cluster-menu.sh", '"Cluster"'): ["tests.test_cluster_menu.Vocabulary"],
    ("cluster-menu.sh", '"Settings"'): ["tests.test_cluster_menu.EveryMenu"],
    ("cluster-overview.sh", '"Cluster check"'): ["tests.test_cluster_overview"],
    ("cluster-review.sh", '"Review what changed"'): ["tests.test_cluster_menu.Vocabulary"],
    # Cluster… → Settings… → Archive location…: its labels are literals, checked below.
    ("configure-archive.sh", '"Archive storage"'): ["tests.test_cluster_menu.EveryMenu"],
    ("serve-menu.sh", '"$TITLE"'): ["tests.test_serve_menu", "tests.test_serve_plan"],
    ("serve-menu.sh", '"$title"'): ["tests.test_serve_menu"],
    ("serve-menu.sh", '"Stop, save, then continue"'): ["tests.test_serve_menu"],
    ("serve-menu.sh", '"Change the plan"'): ["tests.test_serve_menu"],
    ("serve-menu.sh", '"Which node should keep the files?"'): ["tests.test_serve_menu"],
    ("serve-menu.sh", '"Compare with another recipe"'): ["tests.test_serve_menu"],
    ("serve-menu.sh", '"Details"'): ["tests.test_serve_menu"],
    ("services-menu.sh", '"Running services"'): ["tests.test_services_menu"],
    ("models-menu.sh", '"Models and disk space"'): ["tests.test_models_page", "tests.test_models_menu"],
    ("models-menu.sh", '"$title"'): ["tests.test_models_page", "tests.test_models_menu"],
    ("why-not-start.sh", '"$title"'): ["tests.test_why_not_start"],
    ("why-not-start.sh", '"$TITLE"'): ["tests.test_why_not_start"],
    # Set up this cluster…'s review of the machines found (topology setup).
    ("topology.sh", '"Review discovered machines"'): ["tests.test_topology_commands"],
    ("topology.sh", '"Remove which added address?"'): ["tests.test_topology_commands"],
    ("topology.sh", '"Cluster topology"'): ADVANCED,
    ("topology.sh", '"Change membership"'): ADVANCED,
    ("inventory-menu.sh", '"$title"'): ADVANCED,
    ("inventory-menu.sh", '"Select a service"'): ADVANCED,
    # The catalog menu. Before setup, home's Browse the catalog (read-only) opens
    # it read-only, so its cards, which begin with a spec's 12-character prefix,
    # are reached outside Advanced too: an open question in the 4b report.
    **{("model-storage.sh", title): ADVANCED for title in (
        '"$title"', '"Stage the serving image (exact digest)"', '"Blocked plan"', '"Select a required snapshot"',
        '"Compare the selected spec with another catalog spec"', '"Launch options for the selected catalog spec"',
        '"Storage and archive"', '"Free disk space from this spec"', '"Clean up through which spec?"',
        '"Model files and disk space"')},
}
CALL = re.compile(r'choose_index\s+("(?:[^"\\]|\\.)*"|\S+)((?:\\\n|[^\n])*)')


def menu_calls():
    """(script, title as written, the literal labels on its line) for every choose_index call."""
    found = []
    for path in sorted((ROOT / "scripts").glob("*.sh")):
        if path.name == "ui.sh":
            continue
        for match in CALL.finditer(path.read_text(encoding="utf-8")):
            # with_effect "LABEL" "EFFECT": the label is its first argument.
            rest = re.sub(r'with_effect\s+("(?:[^"\\]|\\.)*")\s+"(?:[^"\\]|\\.)*"', r"\1", match.group(2))
            literals = [text for text in re.findall(r'"((?:[^"\\]|\\.)*)"', rest) if "$" not in text]
            found.append((path.name, match.group(1), literals))
    return found


class EveryMenu(unittest.TestCase):
    """The vocabulary test covers every menu outside Advanced, not only those a phase remembered."""

    def test_every_menu_is_either_advanced_or_checked_against_the_rule(self):
        import importlib
        import inspect
        calls = menu_calls()
        self.assertEqual(sorted({(script, title) for script, title, _ in calls}), sorted(MENUS),
                         "a menu was added or renamed: classify it in MENUS")
        for (script, title), tests in MENUS.items():
            if tests == ADVANCED:
                continue
            for name in tests:
                with self.subTest(menu=f"{script} {title}", test=name):
                    module_name, _, qualname = name.partition(".test_")[2].partition(".")
                    module = importlib.import_module("tests.test_" + module_name)
                    target = getattr(module, qualname) if qualname else module
                    source = inspect.getsource(target)
                    self.assertTrue("FORBIDDEN" in source or "assert_plain_choices" in source, name)

    def test_literal_labels_outside_advanced_use_plain_words(self):
        checked = []
        for script, title, literals in menu_calls():
            if MENUS.get((script, title)) == ADVANCED:
                continue
            for label in literals:
                checked.append(label)
                self.assertIsNone(FORBIDDEN.search(label), f"{script}: {label}")
        # Settings and the archive location menu have only literal labels.
        for label in ("Archive location…", "API key", "Set archive location", "Disable archives",
                      "Serve a model…", "Help and troubleshooting…", "Recipe details"):
            self.assertIn(label, checked)

    def test_the_rule_catches_each_forbidden_term(self):
        for label in ("Enroll SSH trust", "Remove home", "Purge prepared copies", "Pin prepared copies",
                      "rank 1", "Select a required snapshot", "Show 5e5e5e5e5e5e"):
            self.assertRegex(label, FORBIDDEN)


class ApiKey(unittest.TestCase):
    def test_where_a_key_is_set_is_said_and_the_key_never_is(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.assertEqual(cluster_status.api_key_source(repo, {}), (None, None))
            (repo / ".env").write_text("PULSAR_COLD_ROOT=/var/tmp/archives\nexport VLLM_API_KEY='fixture-secret'\n")
            self.assertEqual(cluster_status.api_key_source(repo, {}), ("VLLM_API_KEY", "dotenv"))
            # .env is sourced over the environment, as start reads the key: its assignment wins.
            self.assertEqual(cluster_status.api_key_source(repo, {"VLLM_API_KEY": "x"}), ("VLLM_API_KEY", "dotenv"))
            (repo / ".env").write_text("PULSAR_COLD_ROOT=/var/tmp/archives\n")
            self.assertEqual(cluster_status.api_key_source(repo, {"API_KEY": "x"}), ("API_KEY", "process"))
            (repo / ".env").write_text("VLLM_API_KEY=\nAPI_KEY=''\n")
            self.assertEqual(cluster_status.api_key_source(repo, {}), (None, None))
            # An empty assignment in .env clears the environment's key, as sourcing it does.
            self.assertEqual(cluster_status.api_key_source(repo, {"VLLM_API_KEY": "x", "API_KEY": "y"}), (None, None))
            (repo / ".env").write_text("API_KEY=fixture-secret # the fallback\n")
            text = cluster_status.api_key_text(repo, width=44, environ={})
            self.assertIn("API_KEY is set in this checkout's .env file", " ".join(text.split()))
            self.assertNotIn("fixture-secret", text)
            self.assertTrue(all(len(line) <= 44 for line in text.splitlines()), text)
            (repo / ".env").unlink()
            (repo / ".env").symlink_to(repo / "elsewhere")
            self.assertEqual(cluster_status.api_key_source(repo, {}), ("unreadable", "dotenv"))


class Screens(unittest.TestCase):
    """The renderers fit 80, 60 and 44 columns and strip back to plain text."""

    def records(self, record, inventory=None):
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (temporary / ".cluster-topology.json").write_text(json.dumps(three_nodes()))
        environ = {"PULSAR_CLUSTER_CHANGES_FILE": str(temporary / "changes.json"),
                   "PULSAR_SERVICE_INVENTORY_FILE": str(temporary / "inventory.json"),
                   "PULSAR_STORAGE_BUDGET_FILE": str(temporary / "budget.json")}
        self.enterContext(unittest.mock.patch.dict(os.environ, environ))
        os.environ.pop("CLUSTER_TOPOLOGY_FILE", None)
        if record:
            saved.save_changes(record, temporary / "changes.json")
        if inventory:
            (temporary / "inventory.json").write_text(inventory)
        return cluster_status.Records(temporary, environ={"PULSAR_COLD_ROOT": ""})

    def test_every_screen_fits_the_width(self):
        import unittest.mock  # noqa: F401
        records = self.records(body(missing=MISSING, found=FOUND, untrusted=[
            {"address": "printer.local", "possible_reinstall": False}]), saved_inventory())
        for width in (80, 60, 44):
            for text in (cluster_status.home_block(records, [], width=width), cluster_status.frame(records, width=width),
                         cluster_status.review(records, width=width)[0]):
                with self.subTest(width=width, text=text[:20]):
                    self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)

    def test_styles_strip_back_to_the_plain_text(self):
        import re
        import unittest.mock  # noqa: F401
        records = self.records(body(missing=MISSING))

        class Terminal(io.StringIO):
            def isatty(self):
                return True
        plain = cluster_status.frame(records, width=60)
        with unittest.mock.patch.dict(os.environ, {"PULSAR_COLOR": "always", "TERM": "xterm-256color"}):
            styled = cluster_status.frame(records, width=60)
        self.assertNotEqual(styled, plain)
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", styled), plain)


if __name__ == "__main__":
    unittest.main()
