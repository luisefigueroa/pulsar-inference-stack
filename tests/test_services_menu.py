"""Running services…: the page, its card and its choices, driven with doubles
for ui.sh, ./pulsar and the node tools (tests/test_serve_menu.build_root).
No command here reaches Docker, SSH, a GPU or the Hugging Face Hub."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.node_names import NodeNames  # noqa: E402
from scripts import services_page  # noqa: E402
from tests.test_catalog_menu import HOME, checked  # noqa: E402
from tests.test_cluster_menu import FORBIDDEN  # noqa: E402
from tests.test_serve_menu import Harness  # noqa: E402
from tests.test_serve_plan import OTHER, SPEC, envelope, image_plan, prepare_plan, start_blocked  # noqa: E402

THIRD = "ef" * 32
NODES = {"head": ("n0", "spark-1", "192.0.2.1"), "w1": ("n1", "spark-2", "192.0.2.2"), "w2": ("n2", "spark-3", "192.0.2.3")}
MANAGED = "io.pulsar.gb10.managed"
PLAN_LABEL = "io.pulsar.gb10.launch-plan"
PLAN_ID = "7a" * 32


def utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def service(conf, state="running", keys=("head", "w1", "w2"), *, served="served-model", port=8000,
            ownership="managed", safe=True, matches=True, plan=None):
    """One service of a service inventory, as ./pulsar inventory --json reports it; PLAN is the
    launch plan its containers name by label."""
    labels = {MANAGED: "true", **({PLAN_LABEL: plan} if plan else {})} if ownership == "managed" else {}
    return {"service_id": conf, "conf": conf, "state": state, "ownership": ownership, "safe_to_stop": safe,
            "matches_selected_spec": matches, "served_name": served, "api_port": port,
            "reasons": [] if safe else ["not every confirmed node is observable for cleanup"],
            "ranks": [{"rank": str(index), "node": key, "observed_api_port": port if index == 0 else None,
                       "labels": labels} for index, key in enumerate(keys)]}


def inventory(*services, minutes=0, unobserved=(), gpu=()):
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    nodes = {key: {"node_id": node, "hostname": host, "control_ip": address, "local": key == "head",
                   "confirmed": True, "probe_status": "unreachable" if key in unobserved else "ok"}
             for key, (node, host, address) in NODES.items()}
    return {"schema_version": 1, "generated_at": utc(moment), "nodes": nodes, "worker": {"status": "ok"},
            "unmanaged_gpu_processes": [{"node": key, "pid": 4242, "process_name": "/usr/bin/python3",
                                         "used_memory_mib": mib} for key, mib in gpu],
            "services": list(services)}


STATUS_OK = {"schema_version": 1, "kind": "pulsar-service-status", "selected_spec_id": SPEC, "state": "running",
             "verified": True, "healthy": True, "api_url": "http://192.0.2.1:8000", "served_name": "served-model",
             "api_auth": False, "service_id": "svc-1"}

CARD_CHOICES = ["Running services", "Check it now", "How to connect (a first request to copy)", "Logs", "Restart…",
                "Switch to another model…", "Stop…", "Stop every service…", "Back"]


class Page(Harness):
    MENUS = ("services-menu.sh", "serve-menu.sh", "cluster-review.sh", "connect_card.py")
    MENU = "services-menu.sh"

    def setUp(self):
        super().setUp()
        self.entries(checked(home=HOME, local_state="ready"),
                     checked(spec_id=OTHER, model_id="org/other", home=HOME, local_state="ready"))
        self.inventory(inventory(service(SPEC)))

    def inventories(self):
        return self.commands().count("inventory --json")

    def text(self, result):
        return " ".join(result.stdout.split())

    def questions(self):
        return self.confirm_log.read_text().splitlines() if self.confirm_log.exists() else []

    def assert_plain_choices(self):
        super().assert_plain_choices()
        # A hostname or a model is the operator's own data, not a word the menu chose.
        for menu in self.menus():
            for label in menu[1:]:
                self.assertNotIn("--replace", label)


class OneService(Page):
    def test_it_opens_on_the_service_with_what_the_inventory_holds_and_one_capture(self):
        result = self.run_menu(["Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus(), [CARD_CHOICES])
        text = self.text(result)
        # One name, as home's Serving line has it.
        self.assertIn("org/model running on spark-1, spark-2, spark-3 · port 8000", text)
        self.assertIn("Seen by the service inventory less than a minute ago", text)
        self.assertIn("Model served-model, the model name requests use", text)
        # The API comes from the inventory the Seen row names, as status itself has it: rank 0's port
        # on its node, here this machine, so the Connect card's loopback note applies. With no launch
        # plan named, the key and health need Check it now.
        self.assertIn("API http://127.0.0.1:8000/v1, only from spark-1", text)
        self.assertIn("Key not checked here; choose Check it now", text)
        self.assertIn("Health not checked here; choose Check it now", text)
        self.assertNotIn("192.0.2.1", text)
        self.assertEqual(self.commands(), ["inventory --json"])
        self.assert_plain_choices()

    def test_the_key_requirement_comes_from_the_launch_plan_its_containers_name(self):
        from model_library.state import Store
        store = Store(self.root / "model-library")
        for api_auth, words in ((True, "Key required: requests send the key the service was started with · "
                                       "from its launch record"),
                                (False, "Key none: the API accepts requests without a key · from its launch record")):
            with self.subTest(api_auth=api_auth):
                for path in (self.choose_log, self.answers.with_suffix(".n"), self.log):
                    path.unlink(missing_ok=True)
                store.put("service-plans", PLAN_ID, {"ranks": [], "api_auth": api_auth})
                # Rank 0 on another node: its control address, with no loopback note.
                self.inventory(inventory(service(SPEC, keys=("w1", "w2", "head"), plan=PLAN_ID)))
                result = self.run_menu(["Back"])
                self.assertEqual(result.returncode, 0, result.stderr)
                text = self.text(result)
                self.assertIn(words, text)
                self.assertIn("API http://192.0.2.2:8000/v1 Key", text)
                self.assertNotIn("only from", text)
                # Still one probe: the card reads the saved record, not the service.
                self.assertEqual(self.commands(), ["inventory --json"])

    def test_entering_takes_its_own_inventory_even_when_one_was_saved_just_now(self):
        # A Stop run elsewhere, such as Serve a model…'s before a refused Start, leaves the saved
        # record as it was: the service it shows may be gone.
        (self.root / "service-inventory.json").write_text(json.dumps(inventory(service(SPEC))))
        self.inventory(inventory())
        result = self.run_menu(["Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["inventory --json"])
        self.assertIn("No Pulsar service is running.", result.stdout)
        self.assertNotIn("org/model running", result.stdout)

    def test_check_it_now_fills_in_the_api_the_key_and_health_without_another_inventory(self):
        (self.fixtures / "status.result").write_text(json.dumps(STATUS_OK))
        result = self.run_menu(["Check it now", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["inventory --json", f"status {SPEC}"])
        text = self.text(result)
        self.assertIn("Same as: ./pulsar status abababababab", text)
        self.assertIn("API http://192.0.2.1:8000/v1 · checked less than a minute ago", text)
        self.assertIn("Key none: the API accepts requests without a key · checked", text)
        self.assertIn("Health answered /health less than a minute ago", text)

    def test_check_it_now_says_what_it_could_not_tell(self):
        for rc, words in (("1", "Health Check it now found no service less than a minute ago; it may have stopped"),
                          ("3", "Health Check it now could not tell less than a minute ago: a node did not answer")):
            with self.subTest(rc=rc):
                for path in (self.choose_log, self.answers.with_suffix(".n"), self.log,
                             self.fixtures / "status.count", self.fixtures / "inventory.count"):
                    path.unlink(missing_ok=True)
                self.fixture("status", rc=int(rc))
                result = self.run_menu(["Check it now", "Back"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(words, self.text(result))
                self.assertIn("API not found by Check it now", self.text(result))

    def test_how_to_connect_checks_once_then_reuses_that_check(self):
        (self.fixtures / "status.result").write_text(json.dumps(STATUS_OK))
        result = self.run_menu(["How to connect (a first request to copy)", "Continue",
                                "How to connect (a first request to copy)", "Continue", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["inventory --json", f"status {SPEC}"])
        text = self.text(result)
        self.assertEqual(text.count("API: http://192.0.2.1:8000/v1"), 2)
        self.assertIn("Auth: none; the API accepts requests without a key", text)
        self.assertIn("From Check it now, less than a minute ago.", text)

    def test_logs_names_its_command(self):
        result = self.run_menu(["Logs", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["inventory --json", f"logs {SPEC}"])
        self.assertIn("Same as: ./pulsar logs abababababab", self.text(result))

    def test_stop_keeps_todays_question_then_looks_again(self):
        (self.fixtures / "stop.inventory").write_text(json.dumps(inventory()))
        result = self.run_menu(["Stop…", "yes", "Continue", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.questions(), [f"Stop and remove the service containers of org/model [{SPEC[:12]}] on "
                                            "spark-1, spark-2, spark-3? Model files, pins and archives are kept."])
        # The Stop forgets the capture: the page looks again, once, and finds nothing running.
        self.assertEqual(self.commands(), ["inventory --json", f"stop {SPEC}", "inventory --json"])
        text = self.text(result)
        self.assertIn("✓ Stop finished for org/model. Model files, pins and archives are kept.", text)
        self.assertIn("Same as: ./pulsar stop abababababab", text)
        self.assertIn("No Pulsar service is running.", text)
        self.assertEqual(self.menus()[-1], ["Running services", "Serve a model…", "Look again", "Back"])
        self.assert_plain_choices()

    def test_a_declined_stop_changes_nothing_and_does_not_look_again(self):
        result = self.run_menu(["Stop…", "no", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), ["inventory --json"])
        self.assertIn("Nothing was stopped.", result.stdout)

    def test_restart_is_a_two_step_plan_that_starts_on_cancel(self):
        (self.fixtures / "stop.inventory").write_text(json.dumps(inventory()))
        result = self.run_menu(["Restart…", "Go ahead", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1], ["Restart a service", "Cancel", "Go ahead", "Details"])
        self.assertIn("Restart a service\t0", self.default_log.read_text().splitlines())
        text = self.text(result)
        self.assertIn("Restart org/model on spark-1, spark-2 and spark-3", text)
        self.assertIn("1. Stop org/model on spark-1, spark-2 and spark-3. Its model files are kept.", text)
        self.assertIn("2. Start it again on the same nodes and wait until it answers.", text)
        self.assertIn("org/model keeps serving until step 1. If step 2 fails, nothing is serving", text)
        # The page's capture serves the plan and Stop's own check; Start runs its own checks.
        self.assertEqual(self.commands(), ["inventory --json", f"stop {SPEC}", f"start {SPEC}", "inventory --json"])
        self.assert_no_unapproved_authority()
        self.assert_plain_choices()

    def test_restart_of_a_one_node_service_starts_it_on_the_node_it_ran_on(self):
        self.entries(checked(home=HOME, local_state="ready", geometry={"nodes": 1, "tp": 1, "pp": 1}))
        self.inventory(inventory(service(SPEC, keys=("w1",))))
        result = self.run_menu(["Restart…", "Go ahead", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ran()[:2], [f"stop {SPEC}", f"start {SPEC} --node spark-2"])

    def test_restart_is_not_offered_where_start_would_use_other_nodes_or_another_recipe(self):
        for document, reason in (
                (inventory(service(SPEC, keys=("w1", "w2", "head"))),
                 "Restart not offered: it runs on spark-2, spark-3 and spark-1, but Start serves it on spark-1, "
                 "spark-2 and spark-3"),
                (inventory(service(SPEC, matches=False)),
                 "Restart not offered: it was started from a modified recipe")):
            with self.subTest(reason=reason):
                for path in (self.choose_log, self.answers.with_suffix(".n"), self.log):
                    path.unlink(missing_ok=True)
                self.inventory(document)
                result = self.run_menu(["Back"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("Restart…", self.menus()[0])
                self.assertIn("Switch to another model…", self.menus()[0])
                self.assertIn(reason, self.text(result))

    def test_switch_opens_the_serve_list_and_its_plan_stops_this_service_directly_before_start(self):
        # org/other serves on one machine; org/model runs on all three, so it is stopped wherever it runs.
        self.entries(checked(home=HOME, local_state="ready"),
                     checked(spec_id=OTHER, model_id="org/other", home={**HOME, "node_id": "n2"},
                             local_state="ready", geometry={"nodes": 1, "tp": 1, "pp": 1}))
        self.fixture("prepare-plan", envelope(prepare_plan(("n2", "reuse"))))
        self.fixture("image-plan", envelope(image_plan((2, "ok"))))
        self.fixture("start-plan", envelope({"checks": []}))
        (self.fixtures / "stop.inventory").write_text(json.dumps(inventory()))
        result = self.run_menu(["Switch to another model…", "1", "2", "Go ahead", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[1][0], "Switch to another model")
        self.assertEqual(self.menus()[3], ["Switch to another model", "Cancel", "Go ahead", "Change…", "Details"])
        text = self.text(result)
        self.assertIn("1. Stop org/model on spark-1, spark-2 and spark-3; you are switching from it. It holds port "
                      "8000. Its model files are kept. 2. Start the service", text)
        self.assertIn("org/model keeps serving until step 1.", text)
        self.assertEqual(self.ran(), [f"stop {SPEC}", f"start {OTHER} --node spark-3"])
        # No second inventory before the run: the page's capture is shared.
        self.assertEqual(self.commands()[:2], ["inventory --json", f"model prepare {OTHER} --node spark-3 --plan --json"])
        self.assert_no_unapproved_authority()
        self.assert_plain_choices()


class SeveralOrNone(Page):
    def test_several_services_are_a_list_of_their_names(self):
        self.inventory(inventory(service(SPEC), service(OTHER, keys=("w2",), port=8001), service(THIRD, "stale")))
        result = self.run_menu(["1", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        # A name too long for the row is a card of two lines, cut where words break.
        self.assertEqual(self.menus()[0], [
            "Running services", "org/model running on spark-1, spark-2, spark-3 · port 8000",
            "org/other running on spark-3 · port 8001",
            "  exited containers of served-model remain on spark-1, spark-2, spark-3",
            "    · outside the catalog", "Stop every service…", "Back"])
        self.assertIn("3 Pulsar services", result.stdout)
        # Back from a service returns to the list; one capture serves both.
        self.assertEqual(self.menus()[1][0], "Running services")
        self.assertIn("org/other running on spark-3 · port 8001", self.text(result))
        self.assertEqual(self.menus()[2], self.menus()[0])
        self.assertEqual(self.commands(), ["inventory --json"])
        self.assert_plain_choices()

    def test_stop_every_service_keeps_todays_question_naming_each(self):
        self.inventory(inventory(service(SPEC), service(OTHER, keys=("w2",), port=8001)))
        (self.fixtures / "stop-all.inventory").write_text(json.dumps(inventory()))
        result = self.run_menu(["Stop every service…", "yes", "Continue", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        question = self.questions()[0]
        self.assertIn(f"org/model [{SPEC[:12]}] on spark-1, spark-2, spark-3", question)
        self.assertIn(f"org/other [{OTHER[:12]}] on spark-3", question)
        self.assertIn("Containers Pulsar does not manage are untouched.", question)
        self.assertEqual(self.ran(), ["stop --all --yes"])
        self.assertIn("Same as: ./pulsar stop --all --yes", self.text(result))

    def test_serve_a_model_from_an_empty_page_reuses_its_capture(self):
        self.inventory(inventory())
        result = self.run_menu(["Serve a model…", "Back", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([menu[0] for menu in self.menus()], ["Running services", "Serve a model",
                                                              "Running services"])
        # The Serve list and the page again: one inventory serves all three.
        self.assertEqual(self.commands(), ["inventory --json"])

    def test_none_running_is_said_only_when_every_node_answered(self):
        self.inventory(inventory())
        result = self.run_menu(["Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No Pulsar service is running.", result.stdout)
        self.assertEqual(self.menus(), [["Running services", "Serve a model…", "Look again", "Back"]])
        for path in (self.choose_log, self.answers.with_suffix(".n")):
            path.unlink()
        self.inventory(inventory(unobserved=("w2",)))
        result = self.run_menu(["Look again", "Back"])
        text = self.text(result)
        self.assertNotIn("No Pulsar service is running", text)
        self.assertIn("No Pulsar service was seen on the nodes that answered. spark-3 did not answer the service "
                      "inventory, so what runs there is unknown, not absent.", text)
        # Look again takes a new capture.
        self.assertEqual(self.inventories(), 3)

    def test_a_node_that_did_not_answer_leaves_others_unknown_on_the_card(self):
        self.inventory(inventory(service(SPEC, keys=("head", "w1"), safe=False), unobserved=("w2",)))
        self.entries(checked(home=HOME, local_state="ready", geometry={"nodes": 2, "tp": 2, "pp": 1}))
        result = self.run_menu(["Back"])
        text = self.text(result)
        self.assertIn("Not seen spark-3 did not answer the service inventory, so what runs there is unknown, not "
                      "absent", text)
        # Stop needs every node to answer: not offered, and why.
        self.assertNotIn("Stop…", self.menus()[0])
        self.assertIn("Stop not offered while spark-3 does not answer, because Pulsar cannot see everything the "
                      "service runs", text)
        self.assertNotIn("Inventory has not established", text)

    def test_a_service_outside_the_catalog_can_be_stopped_and_gets_nothing_else(self):
        self.inventory(inventory(service(THIRD, served="experiment-model")))
        result = self.run_menu(["Stop…", "no", "Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.menus()[0], ["Running services", "Logs", "Stop…", "Stop every service…", "Back"])
        self.assertIn("experiment-model running on spark-1, spark-2, spark-3 · port 8000 · outside the catalog",
                      self.text(result))
        self.assertTrue(self.questions()[0].endswith("This service was started outside this catalog."))

    def test_work_pulsar_did_not_start_is_named_read_only_and_offered_nothing(self):
        container = {"service_id": "vllm-port80", "conf": None, "state": "running", "ownership": "unknown",
                     "container_name": "vllm-port80", "ranks": [{"rank": "0", "node": "head"}]}
        self.inventory(inventory(service(SPEC), container, gpu=(("head", 9216), ("w1", 1024))))
        result = self.run_menu(["Back"])
        text = self.text(result)
        self.assertIn("GPUs also used by 2 processes Pulsar did not start · 9.00 GiB on spark-1 · 1.00 GiB on spark-2 "
                      "(read-only)", text)
        self.assertIn("Others vllm-port80 · running · not started by Pulsar · read-only", text)
        self.assertEqual(self.menus(), [CARD_CHOICES])

    def test_an_inventory_that_could_not_run_is_not_read_as_nothing_running(self):
        self.fixture("inventory", rc=3)
        (self.fixtures / "inventory.json").unlink()
        result = self.run_menu(["Back"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("✗ Running services could not be checked: the service inventory exited 3", self.text(result))
        self.assertEqual(self.menus(), [["Running services", "Look again", "Back"]])


class WithoutGum(unittest.TestCase):
    def test_each_new_screen_names_the_commands_behind_its_choices(self):
        import os
        import subprocess
        import tempfile
        from tests.test_serve_menu import build_root, root_environment
        expected = {"services-menu.sh": ["./pulsar inventory", "./pulsar status SPEC", "./pulsar logs SPEC",
                                         "./pulsar stop SPEC", "./pulsar start SPEC", "./pulsar stop --all --yes"],
                    "why-not-start.sh": ["./pulsar models list", "./pulsar start SPEC --dry-run",
                                         "./pulsar topology check", "./pulsar doctor", "./pulsar inventory"]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_root(root, tuple(expected), gum=True)
            env = {key: value for key, value in os.environ.items() if not key.startswith(("PULSAR_", "GUM_"))}
            env.update(root_environment(root))
            for name, commands in expected.items():
                with self.subTest(name=name):
                    result = subprocess.run(["bash", str(root / "scripts" / name)], env=env, text=True,
                                            capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("needs an interactive terminal with Gum", result.stderr)
                    for command in commands:
                        self.assertIn(f"  {command}", result.stderr)
                    self.assertFalse((root / "pulsar.log").exists())


class Widths(unittest.TestCase):
    """The card and the list's names at 80, 60 and 44 columns, from the Python projection."""
    NAMES = NodeNames({"n0": "spark-1", "n1": "spark-2", "n2": "spark-3"}, ["n0", "n1", "n2"])

    def page(self, document, entries):
        return services_page.Page(document, entries, names=self.NAMES)

    def test_every_line_fits_and_a_long_model_keeps_its_end(self):
        long = "organization-with-a-long-name/model-family-instruct-2607-fp8"
        entries = [checked(model_id=long, home=HOME, local_state="ready")]
        page = self.page(inventory(service(SPEC), service(THIRD, "stale"), unobserved=("w2",),
                                   gpu=(("head", 9216),)), entries)
        for width in (80, 60, 44):
            with self.subTest(width=width):
                for text in (page.screen(SPEC, width=width), page.screen(None, width=width)):
                    self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
                label = page.label(SPEC, width)
                for line in label.split("\x1f"):
                    self.assertLessEqual(len(line), width)
                self.assertIn("fp8", label)
                self.assertIn("port 8000", label.replace("\x1f", " "))

    def test_why_stop_is_not_offered_is_said_in_the_operators_words(self):
        entries = [checked(home=HOME, local_state="ready")]
        for document, words in (
                (inventory(service(SPEC, safe=False), unobserved=("w1", "w2")),
                 "Stop not offered while spark-2 and spark-3 do not answer, because Pulsar cannot see everything "
                 "the service runs"),
                (inventory(service(SPEC, safe=False)),
                 "Stop not offered: the service inventory could not confirm Pulsar can stop all of it (not every "
                 "confirmed node is observable for cleanup)")):
            with self.subTest(words=words):
                text = " ".join(self.page(document, entries).screen(SPEC, width=200).split())
                self.assertIn(words, text)
                self.assertNotIn("Inventory has not established", text)

    def test_every_choice_uses_plain_words(self):
        entries = [checked(home=HOME, local_state="ready")]
        page = self.page(inventory(service(SPEC), service(THIRD)), entries)
        for conf in (SPEC, THIRD, None):
            for _, label in page.choices(conf):
                self.assertIsNone(FORBIDDEN.search(label), label)
        empty = self.page(inventory(), entries)
        for _, label in empty.choices(None):
            self.assertIsNone(FORBIDDEN.search(label), label)


if __name__ == "__main__":
    unittest.main()
