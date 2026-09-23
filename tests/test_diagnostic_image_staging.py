"""Image-ID staging over real local transports and synthetic Docker stores."""

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from diagnostics import images, schema
from tests import test_model_free_diagnostics as diagnostic_tests
from tests.test_model_free_diagnostics import context, request
from model_library.verification_process import process_identity


class ImagePlanning(unittest.TestCase):
    def rows(self):
        value = context()
        return [
            {
                "kind": "pulsar-diagnostic-image-observation",
                "rank": rank,
                "request_id": value["request_id"],
                "run_id": value["run_id"],
                "image_id": value["request"]["image_id"],
                "present": rank == 0,
                "image_size_bytes": 10 * schema.GIB if rank == 0 else None,
                "docker_available_bytes": 100 * schema.GIB,
                "host_available_bytes": 100 * schema.GIB,
                "idle": True,
            }
            for rank in range(3)
        ]

    def test_missing_rank_space_and_source_are_explicit(self):
        rows = self.rows()
        plan = images.make_plan(context(), rows)
        self.assertEqual(plan["missing_ranks"], [1, 2])
        self.assertEqual(plan["required_available_bytes_per_receiver"], 21 * schema.GIB)
        self.assertTrue(plan["ready"])
        rows[1]["docker_available_bytes"] = 20 * schema.GIB
        self.assertFalse(images.make_plan(context(), rows)["ready"])
        rows[0].update(present=False, image_size_bytes=None)
        plan = images.make_plan(context(), rows)
        self.assertIn("controller lacks", plan["blockers"][0]["reason"])

    def test_invalid_observation_does_not_become_absence(self):
        for update in (
            {"present": None},
            {"docker_available_bytes": True},
            {"host_available_bytes": -1},
            {"idle": False},
        ):
            row = {**self.rows()[1], **update}
            with self.assertRaises(ValueError):
                images.validate_observation(row)

    def test_public_dispatch_uses_existing_sync_boundary(self):
        from scripts.public_cli import dispatch

        with patch(
            "scripts.public_cli.execute", return_value={"preview": True}
        ) as call:
            dispatch("diagnostic", ["stage-image", "--plan"])
            call.assert_called_once_with(
                "scripts/sync-image.sh", ["--diagnostic", "--plan"], json_result=True
            )
        from scripts.integration_contract import contract

        self.assertIn("diagnostic.stage-image", contract()["operations"])


class ImageStaging(unittest.TestCase):
    setUp = diagnostic_tests.DiagnosticTransport.setUp

    def stage(self, flags=("--plan",), extra=None, request_id=None):
        output = self.root / "evidence"
        result = subprocess.run(
            [
                "bash",
                str(self.root / "scripts/sync-image.sh"),
                "--diagnostic",
                "--request",
                str(self.root / "request.json"),
                "--payload-dir",
                str(self.root / "payload"),
                "--request-id",
                request_id or schema.digest(request()),
                "--output-dir",
                str(output),
                *flags,
            ],
            env={**self.env, "PULSAR_TEST_MISSING_IMAGE_RANKS": "1,2", **(extra or {})},
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=40,
        )
        name = "image-plan.json" if "--plan" in flags else "result.json"
        path = output / name
        record = json.loads(path.read_text()) if path.exists() else None
        return result, record

    def mutations(self):
        path = self.root / "docker-state/mutations"
        return path.read_text().splitlines() if path.exists() else []

    def test_preview_observes_all_nodes_without_transfer(self):
        result, plan = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["missing_ranks"], [1, 2])
        self.assertFalse(plan["image_transfer_performed"])
        self.assertEqual(self.mutations(), [])

    def test_site_mode_setting_cannot_turn_preview_into_gpu_execution(self):
        with (self.root / "scripts/lib.sh").open("a") as stream:
            stream.write("\nMODE=run\n")
        result, plan = self.stage(extra={"PULSAR_TEST_MISSING_IMAGE_RANKS": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(plan, "site configuration changed the selected operation")
        self.assertFalse(list((self.root / "docker-state").glob("*.started")))
        self.assertEqual(self.mutations(), [])

    def test_explicit_stage_copies_only_missing_images_and_reads_back_all_ranks(self):
        result, record = self.stage(("--yes",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(record["successful"])
        self.assertEqual(record["verified_ranks"], [0, 1, 2])
        self.assertEqual(record["transfer_attempted_ranks"], [1, 2])
        self.assertCountEqual(
            self.mutations(), ["save 0", "save 0", "load 1", "load 2"]
        )
        self.assertFalse(list((self.root / "docker-state").glob("*.started")))
        self.assertFalse(record["gpu_execution"])

    def test_untagged_imports_are_verified_without_reload_or_pull(self):
        result, record = self.stage(
            ("--yes",), {"PULSAR_TEST_HIDE_UNTAGGED_IMPORTS": "1"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(record["successful"])
        self.assertEqual(record["verified_ranks"], [0, 1, 2])
        self.assertCountEqual(
            self.mutations(), ["save 0", "save 0", "load 1", "load 2"]
        )

    def test_existing_images_are_not_reloaded(self):
        result, record = self.stage(
            ("--yes",), {"PULSAR_TEST_MISSING_IMAGE_RANKS": "2"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record["transfer_attempted_ranks"], [2])
        self.assertCountEqual(self.mutations(), ["save 0", "load 2"])

    def test_all_present_is_an_observed_noop(self):
        result, record = self.stage(("--yes",), {"PULSAR_TEST_MISSING_IMAGE_RANKS": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(record["successful"])
        self.assertEqual(self.mutations(), [])

    def test_stage_and_preview_cannot_be_combined(self):
        result, _ = self.stage(("--yes", "--plan"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "evidence").exists())
        self.assertEqual(self.mutations(), [])

    def test_no_flags_and_pull_are_refused_before_observation(self):
        for flags in ((), ("--pull", "--yes")):
            with self.subTest(flags=flags):
                result, _ = self.stage(flags)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "evidence").exists())
                self.assertEqual(self.mutations(), [])

    def test_changed_request_is_refused(self):
        result, _ = self.stage(("--yes",), request_id="d" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "evidence").exists())
        self.assertEqual(self.mutations(), [])

    def test_wrong_source_image_never_streams(self):
        result, record = self.stage(("--yes",), {"PULSAR_TEST_WRONG_IMAGE_RANK": "0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(record["successful"])
        self.assertEqual(self.mutations(), [])

    def test_unavailable_daemon_is_not_treated_as_missing_image(self):
        result, record = self.stage(
            ("--yes",), {"PULSAR_TEST_DOCKER_UNAVAILABLE_RANK": "2"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(record["successful"])
        self.assertEqual(self.mutations(), [])

    def test_insufficient_space_is_visible_in_preview_and_blocks_apply(self):
        result, plan = self.stage(extra={"PULSAR_TEST_DISK_LOW_RANK": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["blockers"][0]["rank"], 1)
        self.assertEqual(self.mutations(), [])

    def test_insufficient_space_blocks_apply(self):
        result, record = self.stage(("--yes",), {"PULSAR_TEST_DISK_LOW_RANK": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(record["successful"])
        self.assertEqual(self.mutations(), [])

    def test_unresolved_containerd_storage_is_not_approved_using_docker_root(self):
        result, record = self.stage(
            ("--yes",), {"PULSAR_TEST_STORAGE_DRIVER": "overlayfs"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(record["successful"])
        self.assertEqual(self.mutations(), [])

    def test_resolved_containerd_roots_allow_staging(self):
        result, record = self.stage(
            ("--yes",),
            {
                "PULSAR_TEST_STORAGE_DRIVER": "overlayfs",
                "PULSAR_TEST_CONTAINERD_RESOLVED": "1",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(record["successful"])

    def test_separate_containerd_store_requires_its_own_capacity(self):
        result, plan = self.stage(
            extra={
                "PULSAR_TEST_STORAGE_DRIVER": "overlayfs",
                "PULSAR_TEST_CONTAINERD_RESOLVED": "1",
                "PULSAR_TEST_CONTAINERD_DISK_LOW_RANK": "2",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["blockers"][0]["rank"], 2)
        self.assertEqual(self.mutations(), [])

    def test_receiver_failure_is_retained_and_stops_remaining_transfers_without_pull(
        self,
    ):
        result, record = self.stage(("--yes",), {"PULSAR_TEST_LOAD_FAIL_RANK": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(record["successful"])
        self.assertEqual(record["transfer_attempted_ranks"], [1])
        self.assertCountEqual(self.mutations(), ["save 0", "load 1"])
        transfer = json.loads(
            (self.root / "evidence/transfer-1.finished.json").read_text()
        )
        self.assertNotEqual(transfer["returncode"], 0)

    def test_source_failure_is_retained_without_pull(self):
        result, record = self.stage(("--yes",), {"PULSAR_TEST_SOURCE_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(record["successful"])
        self.assertEqual(record["transfer_attempted_ranks"], [1])
        self.assertNotIn("pull", " ".join(self.mutations()))

    def test_zero_loader_exit_is_not_image_readback(self):
        result, record = self.stage(("--yes",), {"PULSAR_TEST_LOAD_NO_IMAGE_RANK": "2"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(record["successful"])
        self.assertIn("not present", record["error"])

    def test_topology_change_stops_next_transfer(self):
        result, record = self.stage(("--yes",), {"PULSAR_TEST_TOPOLOGY_CHANGE": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(record["successful"])
        self.assertEqual(record["transfer_attempted_ranks"], [1])
        self.assertCountEqual(self.mutations(), ["save 0", "load 1"])

    def test_public_cancellation_stops_both_stream_clients(self):
        wrapper = """import sys
from model_library.verification_process import run_command,Cancelled
try:
 result=run_command(sys.argv[1:])
 raise SystemExit(result.returncode)
except Cancelled as exc:
 raise SystemExit(exc.exit_code)
"""
        command = [
            sys.executable,
            "-c",
            wrapper,
            "bash",
            str(self.root / "scripts/sync-image.sh"),
            "--diagnostic",
            "--request",
            str(self.root / "request.json"),
            "--payload-dir",
            str(self.root / "payload"),
            "--request-id",
            schema.digest(request()),
            "--output-dir",
            str(self.root / "evidence"),
            "--yes",
        ]
        process = subprocess.Popen(
            command,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **self.env,
                "PULSAR_TEST_MISSING_IMAGE_RANKS": "1,2",
                "PULSAR_TEST_SLOW_STREAM": "1",
            },
        )
        records = []
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                paths = list((self.root / "docker-state").glob("client-*.json"))
                if len(paths) == 2:
                    records = [json.loads(path.read_text()) for path in paths]
                    break
                if process.poll() is not None:
                    self.fail(process.communicate()[1].decode())
                time.sleep(0.05)
            self.assertEqual(len(records), 2)
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            for row in records:
                self.assertNotEqual(
                    process_identity(row["identity"][0]),
                    row["identity"],
                    "stream client escaped cancellation",
                )
            result = self.root / "evidence/result.json"
            if result.exists():
                self.assertFalse(json.loads(result.read_text())["successful"])
            self.assertNotIn("load 2", self.mutations())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            for row in records:
                if (
                    process_identity(row["identity"][0]) == row["identity"]
                    and row["pgid"] != os.getpgrp()
                ):
                    try:
                        os.killpg(row["pgid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
