"""The summary of saved inventory, status and doctor output that the
pulsar-cluster-state-summary skill uses: it must never read an unobserved
node as an empty one."""
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "pulsar-cluster-state-summary" / "summarize_state.py"
_spec = importlib.util.spec_from_file_location("summarize_state", SCRIPT)
summarize_state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summarize_state)

NOW = datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)


def inventory(*services, unobserved=(), gpu=()):
    nodes = {key: {"hostname": f"host-{index}", "node_id": f"node-{index}", "mem_total_gib": 121.69,
                   "mem_available_gib": None if key in unobserved else 90.5,
                   "probe_status": "unreachable" if key in unobserved else "ok",
                   "probe_reason": "no answer" if key in unobserved else None}
             for index, key in enumerate(("head", "worker", "rank-2"))}
    return {"schema_version": 1, "generated_at": "2026-01-01T00:00:00Z", "topology_id": "topology-1", "nodes": nodes,
            "services": list(services),
            "unmanaged_gpu_processes": [{"node": node, "pid": 1, "process_name": "/usr/bin/trainer",
                                         "used_memory_mib": mib} for node, mib in gpu]}


def service(state="running", observed=("0", "1", "2"), *, runs_with=8000, matches=True):
    return {"service_id": "a" * 64, "spec_id": "a" * 64, "selected_spec_id": "a" * 64 if matches else "b" * 64,
            "served_name": "org/example-model", "state": state,
            "ownership": "managed", "safe_to_stop": state == "running", "api_port": 8000,
            "matches_selected_spec": matches, "expected_ranks": ["0", "1", "2"], "observed_ranks": list(observed),
            "reasons": [], "ranks": [{"rank": rank, "node": "head", "running": True,
                                      "observed_api_port": runs_with if rank == "0" else None,
                                      "gpu_memory": {"measured_mib": 90000}} for rank in observed]}


def one_node_inventory():
    """A cluster of one node, as inventory writes it: the head, and a placeholder for no other node."""
    document = inventory()
    document["nodes"] = {"head": {**document["nodes"]["head"], "confirmed": True},
                         "worker": {"hostname": None, "node_id": None, "confirmed": False, "probe_status": "unset",
                                    "probe_reason": "no other nodes confirmed", "mem_available_gib": None,
                                    "mem_total_gib": None}}
    return document


class Summary(unittest.TestCase):
    def lines(self, document):
        return "\n".join(summarize_state.summarize(document, NOW))

    def test_an_observed_cluster_with_no_service_says_none_on_any_node(self):
        text = self.lines(inventory())
        self.assertIn("inventory taken 2026-01-01T00:00:00Z (10 min ago)", text)
        self.assertIn("services: none observed on any node", text)
        self.assertIn("unmanaged GPU processes: none observed on any node", text)
        self.assertNotIn("NOT OBSERVED", text)

    def test_an_unobserved_node_is_named_and_limits_what_none_means(self):
        text = self.lines(inventory(unobserved=("rank-2",)))
        self.assertIn("node host-0: observed · memory available 90.5 of 121.69 GiB", text)
        self.assertIn("node host-2: not observed (unreachable: no answer) · memory available not observed", text)
        self.assertIn("NOT OBSERVED: host-2. Services and GPU processes there are unknown, not absent.", text)
        self.assertIn("services: none observed on the nodes that answered", text)
        self.assertNotIn("on any node", text)

    def test_a_service_shows_its_state_ranks_and_stop_safety(self):
        text = self.lines(inventory(service("partial", observed=("0", "1")), gpu=(("worker", 2048),)))
        self.assertIn("service org/example-model: partial, managed, ranks 2/3 observed", text)
        self.assertIn("API port: runs with 8000 · configured now 8000", text)
        self.assertIn("safe to stop no · matches selected spec yes", text)
        self.assertNotIn("selected spec a", text)
        self.assertIn("rank 1 on host-0: running, GPU memory 90000 MiB", text)
        self.assertIn("unmanaged GPU process on host-1: /usr/bin/trainer, 2048 MiB", text)

    def test_the_placeholder_for_no_other_node_is_not_an_unobserved_node(self):
        text = self.lines(one_node_inventory())
        self.assertIn("node host-0: observed", text)
        self.assertNotIn("worker", text)
        self.assertNotIn("NOT OBSERVED", text)
        self.assertIn("services: none observed on any node", text)

    def test_an_unconfirmed_node_that_did_not_answer_still_limits_what_none_means(self):
        document = one_node_inventory()
        document["nodes"]["worker"].update(probe_status="unreachable", probe_reason="no answer")
        text = self.lines(document)
        self.assertIn("NOT OBSERVED: worker.", text)
        self.assertIn("services: none observed on the nodes that answered", text)

    def test_the_port_a_service_runs_with_is_apart_from_the_port_configured_now(self):
        text = self.lines(inventory(service(runs_with=8010)))
        self.assertIn("API port: runs with 8010 · configured now 8000", text)
        text = self.lines(inventory(service(runs_with=None)))
        self.assertIn("API port: runs with not observed · configured now 8000", text)

    def test_a_service_of_another_spec_names_the_selected_spec(self):
        text = self.lines(inventory(service(matches=False)))
        self.assertIn("safe to stop yes · matches selected spec no", text)
        self.assertIn("  effective spec " + "a" * 64, text)
        self.assertIn("  selected spec " + "b" * 64, text)

    def test_status_that_could_not_tell_names_the_nodes(self):
        text = self.lines({"ok": False, "error": {"code": "service_state_unknown", "message": "a node did not answer",
                                                  "details": [{"node": "rank-2", "node_id": "node-2"}]}})
        self.assertEqual(text, "error service_state_unknown: a node did not answer\n"
                               "  not observed: node rank-2 (node-2)")

    def test_a_node_that_status_could_not_name_keeps_its_message(self):
        text = self.lines({"ok": False, "error": {"code": "service_state_unknown", "message": "state is unknown",
                                                  "details": [{"field": "node", "node": None, "node_id": None,
                                                               "message": "the service inventory could not run"},
                                                              {"field": "node", "node": "host-2", "node_id": None,
                                                               "message": "probe unreachable"}]}})
        self.assertIn("  not observed: a node that the inventory could not name: "
                      "the service inventory could not run", text)
        self.assertIn("  not observed: node host-2 (node ID not known): probe unreachable", text)
        self.assertNotIn("not observed (not observed)", text)

    def test_a_verified_status_of_a_modified_recipe_says_so(self):
        result = {"kind": "pulsar-serving-observation", "state": "running", "verified": True, "healthy": True,
                  "reason": None, "api_url": "http://192.0.2.1:8000", "selected_spec_id": "a" * 64,
                  "spec_id": "b" * 64, "matches_selected_spec": False}
        text = self.lines({"ok": True, "result": result})
        self.assertIn("  selected spec " + "a" * 64, text)
        self.assertIn("  effective spec " + "b" * 64, text)
        self.assertIn("  matches selected spec no", text)
        self.assertIn("MODIFIED RECIPE", text)
        text = self.lines({"ok": True, "result": {**result, "spec_id": "a" * 64, "matches_selected_spec": True}})
        self.assertIn("  matches selected spec yes", text)
        self.assertNotIn("MODIFIED RECIPE", text)
        self.assertNotIn("effective spec", text)
        del result["matches_selected_spec"]  # the two spec IDs alone are enough to say so
        self.assertIn("MODIFIED RECIPE", self.lines({"ok": True, "result": result}))

    def test_a_verified_status_gives_state_health_and_no_reason(self):
        text = self.lines({"ok": True, "result": {"kind": "pulsar-serving-observation", "state": "running",
                                                  "verified": True, "healthy": None, "reason": None,
                                                  "api_url": "http://192.0.2.1:8000"}})
        self.assertEqual(text, "pulsar-serving-observation: state running · verified yes · healthy not observed\n"
                               "  API http://192.0.2.1:8000")

    def test_doctor_shows_only_the_checks_that_are_not_ok(self):
        text = self.lines({"kind": "pulsar-doctor", "result": "warn", "fail": 0, "warn": 1, "mem_available_gib": 18.2,
                           "checks": [{"level": "ok", "id": "python", "message": "found"},
                                      {"level": "warn", "id": "rank_2_hf_login", "message": "no login"}]})
        self.assertIn("doctor result warn: 0 fail, 1 warn", text)
        self.assertIn("warn rank_2_hf_login: no login", text)
        self.assertNotIn("python", text)

    def test_another_document_is_refused(self):
        for document in ([1, 2], {"ok": True, "result": None}, {"ok": True, "result": {"state": "running"}},
                         {"ok": True, "result": {"kind": "pulsar-topology-observation", "result": "changed"}},
                         {"ok": False}, {"ok": False, "error": {"message": "no code"}},
                         {"ok": True, "result": {"kind": "pulsar-service-status", "services": ["text"]}},
                         {"schema_version": 1, "nodes": {"head": "text"}, "services": []},
                         {"schema_version": 1, "nodes": {}, "services": [{"ranks": "text"}]},
                         {"kind": "pulsar-doctor", "checks": ["text"]}):
            with self.subTest(document=document), self.assertRaises(ValueError):
                summarize_state.summarize(document, NOW)
        self.assertEqual(summarize_state.main(["/nonexistent/state.json"]), 2)


if __name__ == "__main__":
    unittest.main()
