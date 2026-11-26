"""Storage browsing, external changes, failed checks and bounded refresh."""
import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch

from scripts import storage_overview as so
from tests.test_models_page import budget, files, node


class StorageOverview(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        with patch.dict(os.environ, PULSAR_MODEL_LIBRARY_DIR=str(self.root)):
            self.cache = so.StorageCache(self.root)
        topology = self.root / ".cluster-topology.json"
        topology.write_text("{}")
        env = patch.dict(os.environ, CLUSTER_TOPOLOGY_FILE=str(topology))
        env.start()
        self.addCleanup(env.stop)
        self.scope = self.cache.scope()

    def test_event_during_scan_is_pending_and_service_change_only_rescans_files(self):
        def scan(repo, *args):
            if args[0] == "models":
                return {"entries": []}
            (self.cache.directory / "service").touch()
            return files([])
        with patch.object(so, "probe", side_effect=scan):
            so.refresh(self.cache, "files", self.scope)
        rec = self.cache.record("files", self.scope)
        self.assertTrue(so.due(rec, self.cache.generation("files"), time.time()))
        with patch.object(so, "probe", side_effect=[{"entries": []}, files([]), budget()]) as probe:
            for section in ("files", "budget", "files", "budget"):
                so.refresh(self.cache, section, self.scope)
            self.assertEqual(probe.call_count, 3)
        (self.cache.directory / "service").touch()
        for section in so.SECTIONS:
            rec = self.cache.record(section, self.scope)
            self.assertEqual(so.due(rec, self.cache.generation(section), time.time()), section == "files")
            self.assertTrue(so.due(rec, self.cache.generation(section), rec["attempt"] + so.MAX_AGE))

    def test_partial_and_failed_scan_keep_last_successful_time(self):
        for data in (files([]), files([], nodes=[node("n0", "spark-1", unread=True)])):
            (self.cache.directory / "storage").touch()
            with patch.object(so, "probe", side_effect=[{"entries": []}, data]):
                so.refresh(self.cache, "files", self.scope)
        previous = self.cache.record("files", self.scope)
        self.assertIsNotNone(previous["last_seen"]["n0"])
        self.assertEqual(previous["data"]["nodes"][0]["status"], "unknown")
        (self.cache.directory / "service").touch()
        with patch.object(so, "probe", side_effect=ValueError("check failed")):
            so.refresh(self.cache, "files", self.scope)
        failed = self.cache.record("files", self.scope)
        self.assertEqual(failed["checked"], previous["checked"])
        self.assertEqual(failed["last_seen"], previous["last_seen"])
        self.assertIn("unknown now", so.status("Files", failed, self.cache.generation("files"), time.time()))
        self.assertEqual(self.cache.record("files", "other"), {})

    def test_controls_join_a_refresh_that_already_holds_the_lock(self):
        started = threading.Event()
        joined = threading.Event()
        calls = []
        def probe(repo, *args):
            calls.append(args)
            if args == ("models", "list"):
                started.set()
                if not joined.wait(5):
                    raise AssertionError("controls did not join the running check")
                return {"entries": []}
            return files([]) if args == ("model", "files") else budget()

        try_refresh = so.try_refresh
        def attempt(*args, **kwargs):
            result = try_refresh(*args, **kwargs)
            if not result:
                joined.set()
            return result

        with patch.object(so, "probe", side_effect=probe), patch.object(so, "try_refresh", side_effect=attempt):
            worker = threading.Thread(target=try_refresh, args=(self.cache, "files", self.scope))
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(so.capture_for_menu(self.cache, self.root, since=time.time()), 0)
            finally:
                joined.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(so.capture_for_menu(self.cache, self.root), 0)
        self.assertEqual(calls, [("models", "list"), ("model", "files"), ("model", "budget")])
        self.assertEqual(so.read_json(self.root / "files.json")["nodes"], files([])["nodes"])

    def test_controls_do_not_export_a_previous_success_when_the_new_probe_failed(self):
        with patch.object(so, "probe", side_effect=[{"entries": []}, files([]), budget()]):
            so.capture_for_menu(self.cache, self.root)
        with patch.object(so, "probe", side_effect=[{"entries": []}, files([]), ValueError("budget unavailable")]):
            so.capture_for_menu(self.cache, self.root, since=time.time())
        self.assertIsNone(so.read_json(self.root / "budget.json"))
        (self.cache.directory / "service").touch()
        with patch.object(so, "probe", side_effect=ValueError("files unavailable")):
            with self.assertRaisesRegex(ValueError, "files unavailable"):
                so.capture_for_menu(self.cache, self.root)

    def test_change_during_budget_check_prevents_exporting_superseded_files(self):
        def scan(repo, *args):
            if args == ("model", "budget"):
                (self.cache.directory / "service").touch()
                return budget()
            return {"entries": []} if args == ("models", "list") else files([])
        with patch.object(so, "probe", side_effect=scan):
            with self.assertRaisesRegex(ValueError, "changed during the check"):
                so.capture_for_menu(self.cache, self.root)
        self.assertFalse((self.root / "files.json").exists())

    def test_menu_uses_newer_file_failure_published_while_waiting_for_budget(self):
        with patch.dict(os.environ, PULSAR_MODEL_LIBRARY_DIR=str(self.root)):
            other_session = so.StorageCache(self.root)
        with patch.object(so, "probe", side_effect=[{"entries": []}, files([])]):
            so.try_refresh(other_session, "files", self.scope)
        refresh = so.try_refresh

        def other_session_finishes_first(cache, section, scope, **kwargs):
            self.assertEqual(section, "budget")
            with patch.object(so, "probe", side_effect=ValueError("file check unavailable")):
                refresh(other_session, "files", scope, since=time.time())
            return refresh(cache, section, scope, **kwargs)

        with patch.object(so, "try_refresh", side_effect=other_session_finishes_first), \
                patch.object(so, "probe", return_value=budget()):
            with self.assertRaisesRegex(ValueError, "file check unavailable"):
                so.capture_for_menu(self.cache, self.root)
        self.assertFalse((self.root / "files.json").exists())

    def test_slow_successful_budget_opens_controls_on_entry_and_look_again(self):
        clock = [time.time()]
        calls = []
        def scan(repo, *args):
            calls.append(args)
            if args == ("model", "budget"):
                clock[0] += so.MAX_AGE + 1
                return budget()
            return {"entries": []} if args == ("models", "list") else files([])
        with patch.object(so.time, "time", side_effect=lambda: clock[0]), patch.object(so, "probe", side_effect=scan):
            self.assertEqual(so.capture_for_menu(self.cache, self.root), 0)
            clock[0] += 1
            self.assertEqual(so.capture_for_menu(self.cache, self.root, since=clock[0]), 0)
        self.assertEqual(calls, [("models", "list"), ("model", "files"), ("model", "budget")] * 2)
        report = so.read_json(self.root / "budget.json")
        self.assertEqual(report["nodes"], budget()["nodes"])
        self.assertEqual(report["observed_at"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock[0])))


class StorageMenuSession(unittest.TestCase):
    def test_interactive_entry_starts_one_requested_collector_and_reaps_it_on_exit(self):
        for args in ([], ["gum"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scripts = root / "scripts"
                scripts.mkdir()
                shutil.copy2(so.ROOT / "pulsar", root / "pulsar")
                shutil.copy2(so.ROOT / "scripts/menu_session.py", scripts)
                (scripts / "menu-settings.sh").touch()
                home = scripts / "home.sh"
                home.write_text('#!/bin/bash\nprintf "%s" "$PULSAR_MENU_SESSION" > session\n'
                                'read -r request && touch "$PULSAR_MENU_SESSION/storage-request"\nread -r quit\n')
                home.chmod(0o755)
                (scripts / "storage_overview.py").write_text(
                    'import os, signal, sys\nfrom pathlib import Path\n'
                    'assert sys.argv[1:] == ["collect"]\n'
                    'assert (Path(Path("session").read_text()) / "storage-request").is_file()\n'
                    'Path("collector").write_text(str(os.getpid()))\nsignal.pause()\n')
                master, slave = pty.openpty()
                proc = subprocess.Popen([str(root / "pulsar"), *args], cwd=root, stdin=slave, stdout=slave, stderr=slave)
                os.close(slave)
                try:
                    for name, key in (("session", b"storage\n"), ("collector", b"exit\n")):
                        deadline = time.monotonic() + 5
                        while not (root / name).exists() and proc.poll() is None and time.monotonic() < deadline:
                            time.sleep(.02)
                        self.assertTrue((root / name).exists(), name)
                        os.write(master, key)
                    self.assertEqual(proc.wait(timeout=5), 0)
                    self.assertFalse(Path((root / "session").read_text()).exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((root / "collector").read_text()), 0)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=5)
                    os.close(master)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux node fixture")
class ExternalStorageChange(unittest.TestCase):
    def test_open_page_observes_an_acquire_from_another_pulsar_process(self):
        from tests.support.model_library_fixture import Fixture
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = Fixture(root)
            shutil.copy2(so.ROOT / "pulsar", fixture.repo / "pulsar")
            env = dict(fixture.env, PULSAR_MENU_SESSION=tmp, TERM="xterm-256color")
            command = [sys.executable, str(fixture.repo / "scripts/storage_overview.py")]
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 100, 0, 0))
            self.addCleanup(os.close, master)
            view = subprocess.Popen([*command, "view"], env=env, stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            collector = None

            def wait_text(wanted):
                data = b""
                deadline = time.monotonic() + 30
                while wanted not in data and time.monotonic() < deadline:
                    if select.select([master], [], [], .2)[0]:
                        data += os.read(master, 65536)
                self.assertIn(wanted, data, data[-1500:])

            try:
                wait_text(b"waiting for first check")
                collector = subprocess.Popen([*command, "collect"], env=env, start_new_session=True)
                wait_text(b"No model in the catalog")
                result = subprocess.run([str(fixture.repo / "pulsar"), "model", "acquire", "--model-id",
                                         fixture.cfg["model_id"], "--revision", fixture.cfg["revision"],
                                         "--node", fixture.cfg["nodes"][0]["node_id"], "--yes", "--json"],
                                        cwd=fixture.repo, env=env, capture_output=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                wait_text(fixture.cfg["model_id"].encode())
                os.write(master, b"q")
                self.assertEqual(view.wait(timeout=3), 0)
            finally:
                if collector and collector.poll() is None:
                    os.killpg(collector.pid, signal.SIGTERM)
                    collector.wait(timeout=15)
                if view.poll() is None:
                    view.terminate()
                    view.wait(timeout=3)
