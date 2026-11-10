"""Serve a model…: readiness phrases and the plan composer, from synthetic
records and previews only; nothing here runs a command or probes a node."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.node_names import NodeNames  # noqa: E402
from scripts import serve_plan as serve  # noqa: E402
from tests.test_catalog_menu import HOME, checked, inventory, row, scan, scan_location  # noqa: E402
from tests.test_cluster_menu import FORBIDDEN  # noqa: E402

NAMES = NodeNames({"n0": "spark-1", "n1": "spark-2", "n2": "spark-3"}, ["n0", "n1", "n2"])
GIB = 1024 ** 3
SPEC = "ab" * 32
OTHER = "cd" * 32
SIZES = {None: 212 * GIB}


def entries(*rows):
    return list(rows)


def other_running(nodes=("head", "w1", "w2")):
    """Another catalog recipe runs on the serving nodes."""
    return inventory((OTHER, "running", list(nodes)))


def envelope(result=None, *, error=None):
    if error is not None:
        return {"schema_version": 1, "ok": False, "error": error}
    return {"schema_version": 1, "ok": True, "result": result}


def prepare_plan(*actions, eligible=True, details=()):
    return {"kind": "pulsar-preparation-plan", "eligible": eligible, "snapshot_manifest_id": "e" * 64,
            "total_bytes": 212 * GIB, "budgets": {}, "blockers": [d["message"] for d in details],
            "blocker_details": list(details),
            "actions": [{"rank": rank, "node_id": node, "action": action} for rank, (node, action) in enumerate(actions)]}


def image_plan(*states, mode="stream-from-controller"):
    return {"kind": "pulsar-image-check", "operation": "stage-image", "mode": mode, "nodes": len(states),
            "ranks": [{"rank": rank, "topology_index": index, "state": state}
                      for rank, (index, state) in enumerate(states)]}


def start_blocked(*codes, checks=()):
    return envelope(error={"code": "prerequisite_failed", "message": "start refused",
                           "details": [{"field": "blocker", "blocker": code, "stage": "check",
                                        "message": f"{code} found", "fix": None} for code in codes],
                           "checks": list(checks)})


HF_OK = {"prerequisites": {"python": {"ok": True, "version": "3.12"}, "flock": True, "rsync": True,
                           "hf": {"state": "ok", "version": "1.0", "login": "accepted"}}}


def hf(state="ok", login="accepted"):
    document = json.loads(json.dumps(HF_OK))
    document["prerequisites"]["hf"].update(state=state, login=login)
    return document


def everything(state, other=OTHER):
    """Two catalog recipes: the one served and another one."""
    return [state, checked(spec_id=other, model_id="org/other", home=HOME, local_state="ready")]


class Readiness(unittest.TestCase):
    def phrase(self, state, *, service=None, archive="configured", order=("n0", "n1", "n2")):
        return serve.readiness(state, order=list(order), archive_location=archive, sizes=SIZES, service=service)

    def test_one_phrase_per_recipe_from_saved_records(self):
        self.assertEqual(self.phrase(checked(home=HOME, local_state="ready")), "ready to start")
        self.assertEqual(self.phrase(checked(home=HOME, local_state="missing")), "needs copying to 2 nodes · 424 GiB")
        self.assertEqual(self.phrase(row()), "needs downloading · 212 GiB")
        self.assertEqual(self.phrase(checked(archive_state="verified")), "needs restoring from its archive")
        # An archive is no source while archives are not configured.
        self.assertEqual(self.phrase(checked(archive_state="verified"), archive="disabled"),
                         "needs downloading · 212 GiB")
        self.assertEqual(self.phrase(row(), order=("n0", "n1")), "needs 3 nodes; the cluster has 2 nodes")
        self.assertEqual(self.phrase(row(start_supported=False, start_unsupported_reason="guard_unsupported")),
                         "start unsupported")
        self.assertEqual(self.phrase(row(geometry={"nodes": 1, "tp": 1, "pp": 1}, home=HOME)),
                         "needs its files checked")
        running = {"state": "running", "hosts": ["spark-1", "spark-2", "spark-3"]}
        self.assertEqual(self.phrase(row(), service=running), "running on spark-1, spark-2, spark-3")
        self.assertEqual(self.phrase(row(), service={**running, "state": "partial"}),
                         "partly running on spark-1, spark-2, spark-3")

    def test_unknown_sizes_are_said_to_be_unknown(self):
        self.assertEqual(serve.readiness(row(), order=["n0", "n1", "n2"], archive_location="disabled", sizes={},
                                         service=None), "needs downloading · size unknown")

    def test_the_list_tells_recipes_apart_without_a_spec_id_or_digest(self):
        rows = [checked(spec_id="a" * 64, home=HOME, local_state="ready",
                        engine_args=["--max-model-len", "1048576"], image={"digest": "sha256:" + "1" * 64}),
                checked(spec_id="b" * 64, home=HOME, local_state="ready",
                        engine_args=["--max-model-len", "262144"], image={"digest": "sha256:" + "1" * 64}),
                checked(spec_id="c" * 64, model_id="org/same", image={"digest": "sha256:" + "2" * 64}),
                checked(spec_id="d" * 64, model_id="org/same", image={"digest": "sha256:" + "3" * 64}),
                checked(spec_id="e" * 64, model_id="org/twin"), checked(spec_id="f" * 64, model_id="org/twin")]
        with tempfile.TemporaryDirectory() as repo:
            lines = serve.list_lines(rows, width=78, inventory=None, archive_location="configured",
                                     order=["n0", "n1", "n2"], repo=Path(repo))
        texts = [line.split("\t")[-1] for line in lines if line != "gap"]
        for text in texts:
            self.assertIsNone(FORBIDDEN.search(text), text)
            self.assertLessEqual(len(text), 78)
        flat = " ".join(" ".join(texts).split())
        self.assertIn("context 1M · 3 nodes · ready to start", flat)
        self.assertIn("context 256K · 3 nodes · ready to start", flat)
        # Only the image differs: 8 characters of its digest.
        self.assertIn("image 22222222", flat)
        # Nothing differs: numbered in catalog order.
        self.assertIn("recipe 1 of 2", flat)
        self.assertIn("recipe 2 of 2", flat)
        self.assertEqual([line.split("\t")[0] for line in lines].count("heading"), 3)

    def test_the_machine_question_starts_where_the_files_are_and_uses_no_low_level_noun(self):
        state = checked(geometry={"nodes": 1, "tp": 1, "pp": 1}, home={**HOME, "node_id": "n1"},
                        prepared_copies=[{"node_id": "n2", "rank": 0}])
        lines = serve.node_lines(state, NAMES.order, NAMES, width=80)
        self.assertEqual(lines[0], "title\tWhich machine should serve org/model?")
        self.assertIn("default\tn1", lines)
        self.assertIn("node\tn1\tspark-2 — its model files are here", lines)
        self.assertIn("node\tn2\tspark-3 — a copy of its files is here", lines)
        for line in lines:
            self.assertIsNone(FORBIDDEN.search(line.split("\t")[-1]))


class Sources(unittest.TestCase):
    def test_found_files_then_the_archive_then_a_download(self):
        state = checked(snapshot_manifest_id="e" * 64, archive_state="verified")
        files = {"e" * 64: ["n2"]}
        found = serve.file_sources(state, serving=["n0", "n1", "n2"], order=NAMES.order,
                                   archive_location="configured", files=files)
        self.assertEqual(found, [{"snapshot": None, "kind": "register", "node": "n2", "archive": True}])
        # Download's plan did not take the found files: the archive comes next.
        found = serve.file_sources(state, serving=["n0", "n1", "n2"], order=NAMES.order,
                                   archive_location="configured", files=files, not_found=frozenset({None}))
        self.assertEqual(found[0]["kind"], "restore")
        self.assertEqual(found[0]["node"], "n0")
        # Change… picks a download, to another serving node.
        found = serve.file_sources(state, serving=["n0", "n1", "n2"], order=NAMES.order,
                                   archive_location="configured", files=files, source="download", files_node="n1")
        self.assertEqual((found[0]["kind"], found[0]["node"]), ("download", "n1"))
        # Without an archive: download to the first serving node (D9).
        found = serve.file_sources(row(), serving=["n1", "n2", "n0"], order=NAMES.order,
                                   archive_location="disabled")
        self.assertEqual((found[0]["kind"], found[0]["node"]), ("download", "n1"))

    def test_a_multi_node_recipe_keeps_its_files_on_a_serving_node(self):
        state = row(geometry={"nodes": 2, "tp": 2, "pp": 1})
        found = serve.file_sources(state, serving=["n0", "n1"], order=NAMES.order, archive_location="disabled",
                                   files_node="n2")
        self.assertEqual(found[0]["node"], "n0")
        one = row(geometry={"nodes": 1, "tp": 1, "pp": 1})
        found = serve.file_sources(one, serving=["n0"], order=NAMES.order, archive_location="disabled",
                                   files_node="n2")
        self.assertEqual(found[0]["node"], "n2")

    def test_each_snapshot_without_a_home_gets_its_own_source(self):
        member = {"model_id": "org/model", "model_commit": "c" * 40, "archive": None}
        state = row(snapshots={"target": {**member, "home": None, "snapshot_manifest_id": "1" * 64},
                               "draft": {**member, "home": None, "snapshot_manifest_id": "2" * 64}})
        found = serve.file_sources(state, serving=["n0", "n1", "n2"], order=NAMES.order,
                                   archive_location="disabled", files={"2" * 64: ["n1"]})
        self.assertEqual([(item["snapshot"], item["kind"]) for item in found],
                         [("target", "download"), ("draft", "register")])


class Compose(unittest.TestCase):
    def plan(self, state, *, catalog=None, node=None, live=None, **fields):
        catalog = catalog or everything(state)
        fields.setdefault("inventory", live if live is not None else inventory())
        return serve.compose(state, catalog, names=NAMES, node=node, sizes=SIZES, **fields)

    def assert_no_unapproved_authority(self, plan):
        for step in plan["steps"]:
            for command in (step["command"], step.get("preview") or []):
                self.assertNotIn("--replace", command)
                self.assertNotIn("--pull-image", command)
                if "--pull" in command:
                    self.assertTrue(step["authority"], step)

    def test_a_model_that_needs_everything_lists_each_step_in_order(self):
        state = row()
        sources = serve.file_sources(state, serving=["n0", "n1", "n2"], order=NAMES.order,
                                     archive_location="disabled")
        plan = self.plan(state, live=other_running(), sources=sources, hf=HF_OK,
                         previews={"image": envelope(image_plan((0, "missing"), (1, "ok"), (2, "missing"))),
                                   "start": start_blocked("model_files_not_ready", "image_missing", "port_in_use")})
        self.assertEqual([step["kind"] for step in plan["steps"]], ["download", "prepare", "image", "stop", "start"])
        self.assertEqual(plan["blocked"], [])
        self.assertEqual([step["authority"] for step in plan["steps"]], [False, False, True, True, False])
        self.assert_no_unapproved_authority(plan)
        download, prepare, image, stop, start = plan["steps"]
        self.assertEqual(download["command"], ["./pulsar", "model", "acquire", SPEC, "--node", "spark-1", "--yes"])
        self.assertIn("Download the model files from Hugging Face to spark-1 (212 GiB). They become its home",
                      download["sentence"].replace(serve.KEEP, " "))
        self.assertEqual(prepare["sentence"].replace(serve.KEEP, " "), "Copy them to spark-2 and spark-3 (212 GiB each); "
                         "a copy there that still verifies is taken over, not copied again.")
        self.assertEqual(image["sentence"].replace(serve.KEEP, " "), "Pull the serving image on spark-1 and spark-3 (exact digest, from its "
                                            "registry).")
        self.assertIn("--pull", image["command"])
        self.assertEqual(stop["command"], ["./pulsar", "stop", OTHER])
        self.assertEqual(start["command"], ["./pulsar", "start", SPEC])
        self.assertIn("The API will accept requests without a key.", start["sentence"].replace(serve.KEEP, " "))
        # Prepare has no preview before a home exists; Download and Start are projected too.
        self.assertEqual(plan["projected"], [0, 1, 4])
        text = " ".join(serve.render(plan, width=80).split())
        self.assertIn("Nothing is deleted. org/other keeps serving until step 4. If step 5 fails, nothing is "
                      "serving; Pulsar says so and offers to start it again.", text)
        self.assertIn("Steps 1, 2 and 5 are estimates until they run", text)
        self.assertEqual(serve.choices(plan)[0], ("cancel", "Cancel"))
        self.assertIn(("go", "Go ahead"), serve.choices(plan))

    def test_a_ready_model_with_another_service_in_the_way_stops_it_last(self):
        state = checked(home=HOME, local_state="ready")
        plan = self.plan(state, live=other_running(),
                         previews={"prepare": envelope(prepare_plan(("n0", "reuse"), ("n1", "reuse"), ("n2", "reuse"))),
                                   "image": envelope(image_plan((0, "ok"), (1, "ok"), (2, "ok"))),
                                   "start": start_blocked("port_in_use")})
        self.assertEqual([step["kind"] for step in plan["steps"]], ["stop", "start"])
        self.assertEqual(plan["stopped"], [{"conf": OTHER, "name": "org/other", "hosts": ["spark-1", "spark-2",
                                                                                            "spark-3"],
                                            "in_catalog": True, "node": None,
                                            "restart": ["./pulsar", "start", OTHER],
                                            "restart_shown": f"./pulsar start {OTHER[:12]}"}])
        # A modified recipe, or one outside the catalog, is not offered to start again.
        live = other_running()
        live["services"][0]["matches_selected_spec"] = False
        modified = self.plan(state, live=live, previews={"start": start_blocked("port_in_use")})
        self.assertIsNone(modified["stopped"][0]["restart"])
        outside = self.plan(state, catalog=[state], live=other_running(), previews={"start": start_blocked("port_in_use")})
        self.assertEqual(outside["steps"][0]["command"], ["./pulsar", "stop", OTHER])
        self.assertFalse(outside["stopped"][0]["in_catalog"])
        self.assertEqual(plan["steps"][0]["sentence"].replace(serve.KEEP, " "), "Stop org/other on spark-1, spark-2 and spark-3. Its model "
                                                       "files are kept.")
        # Get it ready drops the Stop with the Start.
        ready = self.plan(state, live=other_running(), ready_only=True,
                          previews={"prepare": envelope(prepare_plan(("n0", "reuse"), ("n1", "reuse"),
                                                                     ("n2", "reuse")))})
        self.assertEqual(ready["steps"], [])

    def test_a_one_node_recipe_runs_on_the_node_chosen_for_it(self):
        state = checked(geometry={"nodes": 1, "tp": 1, "pp": 1}, home={**HOME, "node_id": "n1"}, local_state="missing")
        plan = self.plan(state, node="n1",
                         previews={"prepare": envelope(prepare_plan(("n1", "home-view"))),
                                   "image": envelope(image_plan((1, "ok"))), "start": envelope({"checks": []})})
        self.assertEqual(plan["title"], "Serve org/model on spark-2")
        self.assertEqual([step["kind"] for step in plan["steps"]], ["prepare", "start"])
        self.assertEqual(plan["steps"][0]["sentence"].replace(serve.KEEP, " "), "Check the files on spark-2 and record them for this recipe; "
                                                       "nothing is copied.")
        self.assertEqual(plan["steps"][1]["command"], ["./pulsar", "start", SPEC, "--node", "spark-2"])
        # A one-node recipe off this machine pulls: whether this machine holds the image is not observed.
        plan = self.plan(state, node="n1", previews={"image": envelope(image_plan((1, "missing")))})
        image = next(step for step in plan["steps"] if step["kind"] == "image")
        self.assertTrue(image["authority"])
        self.assertEqual(image["command"], ["./pulsar", "image", "stage", SPEC, "--node", "spark-2", "--pull",
                                            "--yes"])

    def test_a_copy_of_the_image_from_this_machine_needs_no_authority(self):
        state = checked(home=HOME, local_state="ready")
        plan = self.plan(state, previews={"image": envelope(image_plan((0, "ok"), (1, "missing"), (2, "other-name")))})
        image = next(step for step in plan["steps"] if step["kind"] == "image")
        self.assertFalse(image["authority"])
        self.assertNotIn("--pull", image["command"])
        self.assertEqual(image["sentence"].replace(serve.KEEP, " "), "Copy the serving image from this machine to spark-2 and spark-3 "
                                            "(exact digest).")

    def test_prepare_says_how_many_copies_it_takes_over_and_counts_them_as_not_copied(self):
        state = checked(home=HOME, local_state="missing")
        prepare = prepare_plan(("n0", "home-view"), ("n1", "rebind"), ("n2", "copy"))
        plan = self.plan(state, previews={"prepare": envelope(prepare)})
        step = plan["steps"][0]
        self.assertEqual(step["sentence"].replace(serve.KEEP, " "), "Copy the model files from spark-1 to spark-3 (212 GiB). "
                         "It takes over 1 copy made before the cluster's membership changed, whose files still verify; they are "
                                           "not copied again.")
        self.assertEqual(step["facts"], {"copy_to": ["n2"]})

    def test_hf_on_the_download_node_blocks_warns_or_is_not_checked(self):
        state = row()
        sources = serve.file_sources(state, serving=["n0", "n1", "n2"], order=NAMES.order,
                                     archive_location="disabled")
        missing = self.plan(state, sources=sources, hf=hf(state="missing", login="unknown"))
        self.assertTrue(any(reason.startswith("Downloads to spark-1 need the hf CLI: hf CLI not found")
                            for reason in missing["blocked"]))
        self.assertTrue(missing["change_node"])
        self.assertNotIn(("go", "Go ahead"), serve.choices(missing))
        self.assertIn(("change", "Change…"), serve.choices(missing))
        rejected = self.plan(state, sources=sources, hf=hf(login="rejected"))
        self.assertEqual(rejected["blocked"], [])
        self.assertIn("the Hub rejected the hf login saved on spark-1", rejected["warnings"][0])
        unreachable = self.plan(state, sources=sources, hf=hf(login="not-reached"))
        self.assertEqual(unreachable["blocked"], [])
        self.assertEqual(unreachable["warnings"], ["hf login not checked: the Hub could not be reached from spark-1"])

    def test_restoring_names_the_free_space_and_blocks_when_it_does_not_fit(self):
        state = checked(archive_state="verified")
        sources = serve.file_sources(state, serving=["n0", "n1", "n2"], order=NAMES.order,
                                     archive_location="configured")
        restore = {"kind": "pulsar-restore-plan", "selected_node": "n0", "total_bytes": 212 * GIB,
                   "destination_space": {"available": 1400 * GIB}}
        plan = self.plan(state, sources=sources, previews={"restore:None": envelope(restore)})
        self.assertIn("Restore the model files from the archive to spark-1 (212 GiB; 1.37 TiB free there).",
                      plan["steps"][0]["sentence"].replace(serve.KEEP, " "))
        self.assertFalse(plan["steps"][0]["projected"])
        restore["destination_space"]["available"] = 100 * GIB
        plan = self.plan(state, sources=sources, previews={"restore:None": envelope(restore)})
        self.assertIn("spark-1 has 100 GiB free; restoring the model files needs 212 GiB.", plan["blocked"])

    def test_start_blockers_no_step_resolves_block_the_plan_and_unrun_checks_are_said(self):
        state = checked(home=HOME, local_state="ready")
        checks = [{"id": "image", "outcome": "pass"}, {"id": "port", "outcome": "fail"},
                  {"id": "preflight", "outcome": "not_run"}]
        plan = self.plan(state, previews={"start": start_blocked("port_in_use", checks=checks)})
        # Nothing in the way to stop: the busy port blocks.
        self.assertEqual(plan["blocked"], ["port_in_use found."])
        # Its unrun checks make Start an estimate until it runs.
        self.assertEqual(plan["projected"], [0])
        plan = self.plan(state, live=other_running(), previews={"start": start_blocked("port_in_use")})
        self.assertEqual(plan["blocked"], [])

    def test_a_memory_warning_the_dry_run_accepted_is_said_before_approval(self):
        state = checked(home=HOME, local_state="ready")
        checks = [{"id": "memory", "outcome": "info", "node": None,
                   "message": "below the recommended headroom; accepted for the dry run"}]
        plan = self.plan(state, live=other_running(), previews={"start": start_blocked("port_in_use", checks=checks)})
        self.assertEqual(plan["blocked"], [])
        self.assertIn("Start's dry run found free memory below the recommended headroom.", plan["warnings"][0])
        self.assertIn("Warning: Start's dry run found free memory", " ".join(serve.render(plan, width=80).split()))
        for step in plan["steps"]:
            self.assertNotIn("--accept-memory-warn", step["command"])

    def test_a_partly_running_service_of_this_recipe_is_removed_as_its_own_step(self):
        state = checked(home=HOME, local_state="ready")
        plan = self.plan(state, live=inventory((SPEC, "partial", ["head", "w1"])))
        self.assertEqual(plan["steps"][0]["sentence"].replace(serve.KEEP, " "),
                         "Remove the containers of org/model, which is partly running, on spark-1 and spark-2; "
                         "Start does not replace a service. Model files are kept.")

    def test_the_outcome_names_commands_by_prefix_where_their_human_mode_takes_one(self):
        state = row()
        sources = serve.file_sources(state, serving=["n0", "n1", "n2"], order=NAMES.order,
                                     archive_location="disabled")
        plan = self.plan(state, live=other_running(), sources=sources, hf=HF_OK,
                         previews={"image": envelope(image_plan((0, "missing"), (1, "ok"), (2, "missing")))})
        self.assertEqual([step["shown"] for step in plan["steps"]], [
            f"./pulsar model acquire {SPEC[:12]} --node spark-1 --yes", f"./pulsar model prepare {SPEC[:12]} --yes",
            f"./pulsar image stage {SPEC} --pull --yes", f"./pulsar stop {OTHER[:12]}",
            f"./pulsar start {SPEC[:12]}"])

    def test_unknown_is_not_absent(self):
        state = checked(home=HOME, local_state="ready")
        plan = self.plan(state, live=inventory(unobserved=("w1",)))
        self.assertTrue(any("did not observe spark-2" in reason for reason in plan["blocked"]))
        plan = self.plan(state, inventory=None, inventory_error="exit 3")
        self.assertTrue(plan["blocked"][0].startswith("Running services could not be checked: exit 3."))
        plan = self.plan(state, unanswered=["n2"])
        self.assertTrue(plan["blocked"][0].startswith("spark-3 did not answer the last cluster check"))
        self.assertNotIn(("go", "Go ahead"), serve.choices(plan))

    def test_a_running_recipe_has_nothing_to_do_and_leftovers_are_their_own_step(self):
        state = checked(home=HOME, local_state="ready")
        plan = self.plan(state, live=inventory((SPEC, "running", ["head", "w1", "w2"])))
        self.assertEqual(plan["already"], "org/model is already running on spark-1, spark-2 and spark-3.")
        self.assertEqual(serve.choices(plan), [("cancel", "Cancel"), ("details", "Details")])
        plan = self.plan(state, live=inventory((SPEC, "stale", ["head", "w1", "w2"])),
                         previews={"start": start_blocked("service_exists")})
        self.assertEqual([step["kind"] for step in plan["steps"]], ["stop", "start"])
        self.assertTrue(plan["steps"][0]["authority"])
        self.assertIn("Remove the exited containers of org/model", plan["steps"][0]["sentence"].replace(serve.KEEP, " "))
        self.assertEqual(plan["stopped"], [])

    def test_what_cannot_be_stopped_blocks_and_gpu_processes_are_noted(self):
        state = checked(home=HOME, local_state="ready")
        live = other_running()
        live["services"][0]["safe_to_stop"] = False
        live["unmanaged_gpu_processes"] = [{"node": "head", "pid": 7, "process_name": "/usr/bin/xorg",
                                            "used_memory_mib": 100}]
        plan = self.plan(state, live=live)
        self.assertEqual([step["kind"] for step in plan["steps"]], ["start"])
        self.assertTrue(any("no Stop offered" in reason for reason in plan["blocked"]))
        self.assertTrue(any("GPU process xorg" in note for note in plan["notes"]))

    def test_a_key_is_named_where_it_is_set(self):
        plan = self.plan(checked(home=HOME, local_state="ready"), api_key="this checkout's .env file")
        self.assertIn("The API will require the key set in this checkout's .env file.",
                      plan["steps"][-1]["sentence"].replace(serve.KEEP, " "))

    def test_the_screen_fits_80_60_and_44_columns(self):
        state = row()
        sources = serve.file_sources(state, serving=["n0", "n1", "n2"], order=NAMES.order,
                                     archive_location="disabled")
        plan = self.plan(state, live=other_running(), sources=sources, hf=hf(login="rejected"),
                         previews={"image": envelope(image_plan((0, "missing"), (1, "ok"), (2, "missing")))})
        for width in (80, 60, 44):
            with self.subTest(width=width):
                text = serve.render(plan, width=width)
                self.assertTrue(all(len(line) <= width for line in text.splitlines()))
        self.assertLessEqual(len(serve.render(plan, width=80).splitlines()) + 1 + len(serve.choices(plan)) + 5, 34)


class CheckStep(unittest.TestCase):
    """A step's own preview, run when its turn comes, against the approved plan."""

    def plan(self, *steps, **fields):
        return {"kind": serve.PLAN_KIND, "spec_id": SPEC, "model_id": "org/model", "serving": ["n0", "n1", "n2"],
                "hosts": ["spark-1", "spark-2", "spark-3"], "steps": list(steps), "stopped": [], **fields}

    def step(self, step_kind, **facts):
        return {"kind": step_kind, "facts": facts, "sentence": f"{step_kind}.", "authority": step_kind == "stop",
                "command": ["./pulsar"], "preview": ["./pulsar"]}

    def check(self, plan, preview, rc=0, index=0):
        catalog = [{"spec_id": SPEC, "model_id": "org/model", "geometry": {"nodes": 3}},
                   {"spec_id": OTHER, "model_id": "org/other", "geometry": {"nodes": 3}}]
        return serve.check_step(plan, index, preview, rc, names=NAMES, entries=catalog)

    def test_found_files_hold_only_when_downloads_own_plan_reuses_them(self):
        plan = self.plan(self.step("register", kind="register", node="n2"))
        reuse = envelope({"kind": "pulsar-acquisition-plan", "action": "reuse", "home": {"node_id": "n2"}})
        self.assertEqual(self.check(plan, reuse), ("same", ""))
        fetch = envelope({"kind": "pulsar-acquisition-plan", "selected_node": "n2"})
        verdict, reason = self.check(plan, fetch)
        self.assertEqual(verdict, "differs")
        self.assertIn("Download would fetch them from Hugging Face instead", reason)

    def test_a_download_that_finds_files_or_another_node_differs(self):
        plan = self.plan(self.step("download", kind="download", node="n0"))
        self.assertEqual(self.check(plan, envelope({"kind": "pulsar-acquisition-plan", "selected_node": "n0"})),
                         ("same", ""))
        self.assertEqual(self.check(plan, envelope({"kind": "pulsar-acquisition-plan", "action": "reuse",
                                                    "home": {"node_id": "n1"}}))[0], "differs")
        self.assertEqual(self.check(plan, envelope({"kind": "pulsar-acquisition-plan", "selected_node": "n1"}))[0],
                         "differs")
        self.assertEqual(self.check(plan, envelope(error={"message": "cannot resolve source"}), rc=1),
                         ("blocked", "cannot resolve source"))

    def test_a_restore_to_another_node_differs(self):
        plan = self.plan(self.step("restore", kind="restore", node="n0"))
        self.assertEqual(self.check(plan, envelope({"kind": "pulsar-restore-plan", "selected_node": "n0"})),
                         ("same", ""))
        self.assertEqual(self.check(plan, envelope({"kind": "pulsar-restore-plan", "selected_node": "n2"}))[0],
                         "differs")

    def test_prepare_that_is_blocked_or_copies_to_an_unlisted_node_stops_the_run(self):
        plan = self.plan(self.step("prepare", copy_to=["n1"]))
        self.assertEqual(self.check(plan, envelope(prepare_plan(("n0", "home-view"), ("n1", "copy"),
                                                                ("n2", "reuse")))), ("same", ""))
        verdict, reason = self.check(plan, envelope(prepare_plan(("n0", "home-view"), ("n1", "copy"),
                                                                 ("n2", "copy"))))
        self.assertEqual((verdict, reason), ("differs", "it would also copy the files to spark-3"))
        detail = {"code": "pinned", "message": "n2: existing prepared copy is pinned or referenced",
                  "fix": "unpin the prepared copies first", "node_id": "n2"}
        verdict, reason = self.check(plan, envelope(prepare_plan(("n0", "home-view"), ("n1", "copy"),
                                                                 ("n2", "copy"), eligible=False, details=[detail])))
        self.assertEqual(verdict, "blocked")

    def test_the_image_is_skipped_when_already_there_and_differs_when_staged_elsewhere(self):
        plan = self.plan(self.step("image", mode="copy", nodes=["1"]))
        self.assertEqual(self.check(plan, envelope(image_plan((0, "ok"), (1, "missing")))), ("same", ""))
        self.assertEqual(self.check(plan, envelope(image_plan((0, "ok"), (1, "ok"))))[0], "skip")
        self.assertEqual(self.check(plan, envelope(image_plan((0, "missing"), (1, "missing"))))[0], "differs")
        pulled = envelope(image_plan((0, "ok"), (1, "missing"), mode="pull-exact-digest"))
        self.assertEqual(self.check(plan, pulled)[0], "differs")

    def test_stop_finds_what_runs_now(self):
        plan = self.plan(self.step("stop", conf=OTHER))
        self.assertEqual(self.check(plan, other_running()), ("same", ""))
        self.assertEqual(self.check(plan, inventory())[0], "skip")
        third = "ef" * 32
        both = inventory((OTHER, "running", ["head", "w1", "w2"]), (third, "running", ["w2"]))
        verdict, reason = self.check(plan, both)
        self.assertEqual(verdict, "differs")
        self.assertIn("the plan did not say to stop it", reason)
        self.assertEqual(self.check(plan, None)[0], "blocked")


class Outcome(unittest.TestCase):
    def plan(self, **fields):
        steps = [{"kind": "prepare", "sentence": "Copy them to spark-2 (212 GiB each).", "authority": False},
                 {"kind": "stop", "sentence": "Stop org/other on spark-1. Its model files are kept.",
                  "authority": True},
                 {"kind": "start", "sentence": "Start the service and wait until it answers. This can take "
                                               "several minutes.", "authority": False}]
        return {"kind": serve.PLAN_KIND, "spec_id": SPEC, "model_id": "org/model", "hosts": ["spark-1", "spark-2"],
                "serving": ["n0", "n1"], "steps": steps, "ready_only": False,
                "stopped": [{"conf": OTHER, "name": "org/other", "hosts": ["spark-1"], "in_catalog": True,
                             "node": None}], **fields}

    def test_a_finished_plan_names_the_commands_that_ran(self):
        text, choices = serve.outcome(self.plan(), done=3, ran=["./pulsar model prepare abababababab --yes",
                                                                "./pulsar stop cdcdcdcdcdcd",
                                                                "./pulsar start abababababab"], width=80)
        flat = " ".join(text.split())
        self.assertIn("✓ Serving: org/model started on spark-1 and spark-2.", flat)
        self.assertIn("Same as: ./pulsar model prepare abababababab --yes ./pulsar stop cdcdcdcdcdcd ./pulsar start "
                      "abababababab", flat)
        self.assertEqual(choices, [("back", "Back")])

    def test_a_failed_start_after_the_stop_says_nothing_is_serving_and_offers_the_old_service(self):
        text, choices = serve.outcome(self.plan(), done=2, failed=2, rc=1, stopped_ran=True, width=80)
        flat = " ".join(text.split())
        self.assertIn("✗ Step 3 did not finish: Start the service and wait until it answers.", flat)
        self.assertIn("Nothing is serving now: org/other was stopped before Start.", flat)
        self.assertEqual(choices, [("restart:0", "Start org/other again"), ("back", "Back")])

    def test_a_failed_copy_leaves_the_service_running(self):
        text, choices = serve.outcome(self.plan(), done=0, failed=0, rc=1, width=80)
        flat = " ".join(text.split())
        self.assertIn("Still serving: org/other; the plan stopped before stopping it.", flat)
        self.assertIn("Choose Serve a model… again to plan from the state as it is now", flat)

    def test_an_interrupted_start_after_the_stop_does_not_claim_nothing_serves(self):
        text, choices = serve.outcome(self.plan(), done=2, failed=2, rc=130, stopped_ran=True, width=80)
        flat = " ".join(text.split())
        self.assertIn("org/other was stopped before Start. Whether this model's service started is not confirmed.",
                      flat)
        self.assertNotIn("Nothing is serving now", flat)
        self.assertNotIn(("restart:0", "Start org/other again"), choices)

    def test_a_failed_stop_after_another_does_not_say_what_still_runs(self):
        plan = self.plan()
        plan["steps"].insert(2, {"kind": "stop", "sentence": "Stop org/third on spark-2. Its model files are kept.",
                                 "authority": True})
        text, _ = serve.outcome(plan, done=2, failed=2, rc=1, stopped_ran=True, width=80)
        self.assertIn("whether the one this step stops still runs is not confirmed", " ".join(text.split()))

    def test_an_interrupted_step_is_inspected_before_any_retry(self):
        text, choices = serve.outcome(self.plan(), done=0, failed=0, rc=130, width=80)
        self.assertIn("Its outcome is not confirmed; inspect before retrying.", " ".join(text.split()))
        self.assertEqual(choices, [("inspect", "Check its files now (read-only)"), ("back", "Back")])
        _, choices = serve.outcome(self.plan(), done=2, failed=2, rc=143, stopped_ran=True, width=80)
        self.assertEqual(choices, [("status", "Check the service now (read-only)"), ("back", "Back")])


class Authority(unittest.TestCase):
    def test_no_code_path_passes_replace_or_pull_image_and_a_pull_is_its_own_step(self):
        for path in ("scripts/serve_plan.py", "scripts/serve-menu.sh"):
            text = (ROOT / path).read_text()
            for flag in ("--replace", "--pull-image", "--accept-memory-warn"):
                self.assertNotIn(f'"{flag}"', text, path)
                self.assertNotIn(f"'{flag}'", text, path)
            code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
            if path.endswith(".sh"):
                self.assertFalse([line for line in code if "--replace" in line or "--pull-image" in line], path)
        # The one place --pull is added: the image step, which needs authority.
        source = (ROOT / "scripts/serve_plan.py").read_text()
        self.assertEqual(source.count('"--pull"'), 1)
        self.assertIn('pull = ["--pull"] if mode == "pull" else []', source)


class Cli(unittest.TestCase):
    """The menu's protocol: compose writes the plan; steps, command and choices read it."""

    def test_compose_then_read_steps_commands_and_choices(self):
        state = checked(home=HOME, local_state="ready")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            topology = tmp / "topology.json"
            topology.write_text(json.dumps({"schema_version": 2, "topology_id": "a" * 64, "nodes": [
                {"rank": index, "hostname": host, "node_id": node} for index, (node, host)
                in enumerate((("n0", "spark-1"), ("n1", "spark-2"), ("n2", "spark-3")))]}))
            (tmp / "inventory.json").write_text(json.dumps(other_running()))
            (tmp / "start.json").write_text(json.dumps(start_blocked("port_in_use")))
            out = tmp / "plan.json"
            env = {"CLUSTER_TOPOLOGY_FILE": str(topology), "PULSAR_CLUSTER_CHANGES_FILE": str(tmp / "changes.json")}
            with mock.patch.dict(os.environ, env), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps({"entries": everything(state)}))):
                self.assertEqual(serve.main(["compose", "--spec-id", SPEC, "--repo-root", str(tmp),
                                             "--inventory", str(tmp / "inventory.json"),
                                             "--preview", f"start={tmp / 'start.json'}", "--out", str(out)]), 0)
            printed = io.StringIO()
            with mock.patch("sys.stdout", printed):
                serve.main(["steps", "--plan", str(out)])
                serve.main(["command", "--plan", str(out), "--step", "0"])
                serve.main(["choices", "--plan", str(out)])
            lines = printed.getvalue().splitlines()
            self.assertEqual([line.split("\t")[:5] for line in lines[:2]],
                             [["0", "stop", "1", "0", "-"], ["1", "start", "0", "0", "-"]])
            self.assertEqual(lines[0].split("\t")[5], "Stop org/other on spark-1, spark-2 and spark-3. Its model "
                                                      "files are kept.")
            self.assertEqual(lines[2:], ["./pulsar", "stop", OTHER,
                                         "cancel\tCancel", "go\tGo ahead", "details\tDetails"])


if __name__ == "__main__":
    unittest.main()
