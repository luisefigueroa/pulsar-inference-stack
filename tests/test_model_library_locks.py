"""A busy model library names the command holding it; launch releases its locks."""
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PREPARE = "pulsar model prepare 139908cf23bb --yes"


class ModelLibraryLocks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "library"
        self.env = {**os.environ, "PULSAR_MODEL_LIBRARY_DIR": str(self.state), "PYTHONDONTWRITEBYTECODE": "1",
                    "PULSAR_MODEL_LIBRARY_LOCK_TIMEOUT_SECONDS": "0.3"}
        for key in ("PULSAR_VERBOSE", "PULSAR_LOCK_BUSY_EXIT", "PULSAR_COMMAND_LINE", "PULSAR_COMMAND_PID"):
            self.env.pop(key, None)

    def library(self, script, **env):
        return ["bash", "-c", f". {ROOT}/scripts/lib.sh\n{script}"], {**self.env, **env}

    def hold(self, script, **env):
        """Run SCRIPT in its own process group until it prints its first line."""
        command, environment = self.library(script, **env)
        process = subprocess.Popen(command, env=environment, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(process.wait)
        self.addCleanup(os.killpg, process.pid, signal.SIGKILL)
        self.addCleanup(process.stdout.close)
        self.assertTrue(process.stdout.readline().strip())
        return process

    def hold_as_command(self, body):
        """Hold a lock the way a ./pulsar command does: export the command, then exec."""
        inner = shlex.quote(f". {ROOT}/scripts/lib.sh\n{body}")
        return self.hold(f"exec env PULSAR_COMMAND_LINE={shlex.quote(PREPARE)} PULSAR_COMMAND_PID=$$ bash -c {inner}")

    def run_library(self, script, **env):
        command, environment = self.library(script, **env)
        return subprocess.run(command, env=environment, text=True, capture_output=True, timeout=60)

    def test_busy_lock_names_the_command_holding_it(self):
        # The holder's child inherits the locked descriptor; the command is named once.
        holder = self.hold_as_command("acquire_model_library_lifecycle_lock exclusive; echo held; exec sleep 60")
        waiter = self.run_library("acquire_model_library_lifecycle_lock shared")
        self.assertEqual(waiter.returncode, 1)
        self.assertEqual(waiter.stderr, f"error: the model library is busy: {PREPARE} (pid {holder.pid}, started "
                                        "less than a minute ago). Wait for it to finish, or interrupt it in its "
                                        "terminal, then retry.\n")
        # Check scripts report a busy library as a check that could not run.
        waiter = self.run_library("acquire_model_library_lifecycle_lock shared", PULSAR_LOCK_BUSY_EXIT="3")
        self.assertEqual(waiter.returncode, 3)

    def test_a_process_left_behind_by_a_finished_command_is_named_as_such(self):
        holder = self.hold("acquire_model_library_hot_lock exclusive; echo held; exec sleep 60",
                           PULSAR_COMMAND_LINE=PREPARE, PULSAR_COMMAND_PID="999999999")
        waiter = self.run_library("acquire_model_library_hot_lock shared")
        self.assertEqual(waiter.returncode, 1)
        self.assertEqual(waiter.stderr, f"error: the prepared model files are busy: pid {holder.pid} (sleep 60), "
                                        f"left behind by {PREPARE}. Stop it if it is no longer needed, then retry.\n")

    def test_other_holders_are_named_by_command_line_and_waiters_are_not(self):
        self.state.mkdir(parents=True)
        holder = self.hold(f"exec flock -x {self.state / 'lifecycle.lock'} -c 'echo held; sleep 60'")
        command, environment = self.library("acquire_model_library_lifecycle_lock shared",
                                            PULSAR_MODEL_LIBRARY_LOCK_TIMEOUT_SECONDS="30")
        other_waiter = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(other_waiter.wait)
        self.addCleanup(os.killpg, other_waiter.pid, signal.SIGKILL)
        waiter = self.run_library("acquire_model_library_lifecycle_lock shared")
        self.assertEqual(waiter.returncode, 1)
        self.assertEqual(waiter.stderr, "error: the model library is busy: flock -x lifecycle.lock -c echo held; "
                                        f"sleep 60 (pid {holder.pid}, started less than a minute ago). Wait for it to "
                                        "finish, or interrupt it in its terminal, then retry.\n")
        (self.state / "hot.lock").touch()
        unheld = self.run_library('_lock_busy_message "$PULSAR_MODEL_LIBRARY_DIR/hot.lock" '
                                  '"the prepared model files are busy"')
        self.assertEqual(unheld.stdout, "the prepared model files are busy. Wait for the other operation to finish, "
                                        "then retry.\n")

    def test_release_frees_the_lock_even_where_a_child_inherited_it(self):
        holder = self.hold("acquire_model_library_lifecycle_lock shared; acquire_model_library_hot_lock shared\n"
                           "sleep 60 &\nrelease_model_library_locks; echo released; wait")
        taker = self.run_library("acquire_model_library_lifecycle_lock exclusive\n"
                                 "acquire_model_library_hot_lock exclusive; echo acquired")
        self.assertEqual(taker.returncode, 0, taker.stderr)
        self.assertEqual(taker.stdout, "acquired\n")
        self.assertIsNone(holder.poll())  # the child that inherited the descriptors still runs
        # Locking writes nothing but the lock files themselves.
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), ["hot.lock", "lifecycle.lock"])


if __name__ == "__main__":
    unittest.main()
