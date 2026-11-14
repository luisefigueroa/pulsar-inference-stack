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


def service(state="running", observed=("0", "1", "2")):
    return {"service_id": "a" * 64, "spec_id": "a" * 64, "served_name": "org/example-model", "state": state,
            "ownership": "managed", "safe_to_stop": state == "running", "api_port": 8000,
            "matches_selected_spec": True, "expected_ranks": ["0", "1", "2"], "observed_ranks": list(observed),
            "reasons": [], "ranks": [{"rank": rank, "node": "head", "running": True,
                                      "gpu_memory": {"measured_mib": 90000}} for rank in observed]}


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
        self.assertIn("port 8000 · safe to stop no · matches selected spec yes", text)
        self.assertIn("rank 1 on host-0: running, GPU memory 90000 MiB", text)
        self.assertIn("unmanaged GPU process on host-1: /usr/bin/trainer, 2048 MiB", text)

    def test_status_that_could_not_tell_names_the_nodes(self):
        text = self.lines({"ok": False, "error": {"code": "service_state_unknown", "message": "a node did not answer",
                                                  "details": [{"node": "rank-2", "node_id": "node-2"}]}})
        self.assertEqual(text, "error service_state_unknown: a node did not answer\n"
                               "  not observed: node rank-2 (node-2)")

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
        with self.assertRaises(ValueError):
            summarize_state.summarize([1, 2], NOW)
        self.assertEqual(summarize_state.main(["/nonexistent/state.json"]), 2)


if __name__ == "__main__":
    unittest.main()
