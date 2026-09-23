"""Containerd capacity checks bind configuration to Docker's running daemon."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from diagnostics import storage


class ContainerdStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {
            "root": "/container-data",
            "grpc": {"address": "/run/fixture.sock"},
        }

    def test_explicit_cli_root_and_snapshot_paths_are_measured(self):
        self.config["plugins"] = {
            "io.containerd.snapshotter.v1.overlayfs": {"root_path": "/snapshot-data"}
        }
        self.config["temp"] = "/temporary-data"
        roots = storage.roots_from_config(
            self.config, {"root": "/override-data"}, "/run/fixture.sock", "/docker-data"
        )
        self.assertEqual(
            roots,
            list(
                map(
                    Path,
                    [
                        "/docker-data",
                        "/override-data",
                        "/snapshot-data",
                        "/temporary-data",
                    ],
                )
            ),
        )

    def test_separate_content_mount_is_not_hidden_by_parent_capacity(self):
        mountinfo = "1 0 0:1 / / rw - ext4 /dev/fixture rw\n2 1 0:2 / /container-data/io.containerd.content.v1.content rw - ext4 /dev/store rw\n"
        roots = storage.roots_from_config(
            self.config, {}, "/run/fixture.sock", "/docker-data", mountinfo
        )
        self.assertIn(Path("/container-data/io.containerd.content.v1.content"), roots)
        self.assertEqual(len(roots), 3)

    def test_wrong_endpoint_and_relative_root_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "endpoint"):
            storage.roots_from_config(
                self.config, {}, "/run/wrong.sock", "/docker-data"
            )
        with self.assertRaisesRegex(ValueError, "absolute"):
            storage.roots_from_config(
                self.config, {"root": "relative"}, "/run/fixture.sock", "/docker-data"
            )

    def test_daemon_options_reject_subcommands_and_unknown_flags(self):
        value = storage.daemon_options(
            [
                "/usr/bin/containerd",
                "-c",
                "/etc/fixture.toml",
                "--root=/data",
                "-a",
                "/run/fixture.sock",
            ]
        )
        self.assertEqual(value["root"], "/data")
        for argv in (
            ["/usr/bin/containerd", "config", "dump"],
            ["/usr/bin/containerd", "--unknown", "value"],
            ["/usr/bin/containerd", "--root=relative"],
        ):
            with self.assertRaises(ValueError):
                storage.daemon_options(argv)

    def test_imports_and_config_must_predate_startup(self):
        imported = self.root / "import.toml"
        imported.write_text('root="/store"\n')
        main = self.root / "config.toml"
        main.write_text("imports=[" + json.dumps(str(imported)) + "]\n")
        started = time.time() + 10
        observed = storage.audit_config(main, {}, started)
        self.assertIn(str(imported), observed)
        os.utime(imported, (started + 5, started + 5))
        with self.assertRaisesRegex(ValueError, "changed after"):
            storage.audit_config(main, {}, started)

    def test_changed_import_directory_and_symlinks_are_rejected(self):
        directory = self.root / "imports"
        directory.mkdir()
        main = self.root / "config.toml"
        main.write_text("imports=[" + json.dumps(str(directory / "*.toml")) + "]\n")
        started = time.time() + 10
        storage.audit_config(main, {}, started)
        os.utime(directory, (started + 5, started + 5))
        with self.assertRaises(ValueError):
            storage.audit_config(main, {}, started)
        link = self.root / "link.toml"
        link.symlink_to(main)
        with self.assertRaises(ValueError):
            storage.unchanged(link, started + 20)

    def daemon(self):
        proc = self.root / "proc"
        proc.mkdir()
        (proc / "self").mkdir()
        (proc / "self/mountinfo").write_text("1 0 0:1 / / rw - ext4 /dev/fixture rw\n")
        (proc / "stat").write_text("btime 0\n")
        process = proc / "123"
        process.mkdir()
        (process / "comm").write_text("containerd\n")
        binary = self.root / "containerd"
        binary.write_text("fixture binary")
        config = self.root / "config.toml"
        config.write_text('root="/container-data"\n')
        command = [str(binary), "--config", str(config)]
        (process / "cmdline").write_bytes(("\0".join(command) + "\0").encode())
        fields = (
            ["S"]
            + ["0"] * 18
            + [str(int((time.time() + 60) * os.sysconf("SC_CLK_TCK")))]
        )
        (process / "stat").write_text("123 (containerd) " + " ".join(fields))
        info = {
            "DockerRootDir": "/docker-data",
            "Containerd": {"Address": "/run/fixture.sock"},
        }
        result = subprocess.CompletedProcess(
            [],
            0,
            'version=3\nroot="/container-data"\n[grpc]\naddress="/run/fixture.sock"\n',
        )
        return proc, process, info, result

    def test_one_stable_matching_daemon_resolves_roots_without_rpc_or_sudo(self):
        proc, process, info, result = self.daemon()
        with patch.object(storage.subprocess, "run", return_value=result) as run:
            roots = storage.containerd_roots(info, proc)
        self.assertEqual(roots, [Path("/docker-data"), Path("/container-data")])
        self.assertEqual(run.call_args.args[0][-2:], ["config", "dump"])

    def test_restart_during_observation_is_not_accepted(self):
        proc, process, info, result = self.daemon()

        def changed(*args, **kwargs):
            fields = (process / "stat").read_text().split()
            fields[-1] = str(int(fields[-1]) + 1)
            (process / "stat").write_text(" ".join(fields))
            return result

        with patch.object(storage.subprocess, "run", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "cannot verify"):
                storage.containerd_roots(info, proc)


if __name__ == "__main__":
    unittest.main()
