"""Model-library locks stay with every process started while they are held."""
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# The acquire function, lock file and descriptor variable of each model-library lock.
LOCKS = (("acquire_model_library_lifecycle_lock", "lifecycle.lock", "PULSAR_MODEL_LIBRARY_LOCK_FD"),
         ("acquire_model_library_hot_lock", "hot.lock", "PULSAR_MODEL_LIBRARY_HOT_LOCK_FD"))


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class ModelLibraryLockInheritance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / "library"
        # The absent test dotenv keeps a repository .env from choosing another library.
        self.env = {**os.environ, "PULSAR_MODEL_LIBRARY_DIR": str(self.state), "PULSAR_SELFTEST": "1",
                    "PULSAR_COLD_STORAGE_TEST_DOTENV": str(root / "absent-env"), "PYTHONDONTWRITEBYTECODE": "1"}
        for key in ("PULSAR_MODEL_LIBRARY_LOCK_FILE", "PULSAR_MODEL_LIBRARY_HOT_LOCK_FILE"):
            self.env.pop(key, None)

    def hold(self, script):
        """Run SCRIPT after lib.sh in its own process group; return it and its first output line."""
        process = subprocess.Popen(["bash", "-c", f". {ROOT}/scripts/lib.sh\n{script}"], env=self.env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(process.wait)
        self.addCleanup(kill_group, process.pid)
        self.addCleanup(process.stdout.close)
        line = process.stdout.readline().strip()
        self.assertTrue(line, "the lock holder exited before it was ready")
        return process, line

    def held(self, name):
        """Whether any process holds the lock file NAME."""
        descriptor = os.open(self.state / name, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(descriptor)
        return False

    def test_a_command_the_script_execs_into_keeps_the_lock(self):
        for acquire, name, _ in LOCKS:
            with self.subTest(lock=name):
                holder, _ = self.hold(f"{acquire} shared; exec sh -c 'echo ready; exec sleep 60'")
                self.assertTrue(self.held(name))
                kill_group(holder.pid); holder.wait()
                self.assertFalse(self.held(name))

    def test_a_child_keeps_the_lock_after_the_script_closes_its_descriptor(self):
        for acquire, name, variable in LOCKS:
            with self.subTest(lock=name):
                holder, child = self.hold(f"{acquire} exclusive\nsleep 60 &\nchild=$!\n"
                                          f'eval "exec ${variable}>&-"; echo "$child"; wait')
                self.assertTrue(self.held(name))
                os.kill(int(child), signal.SIGKILL); holder.wait(timeout=10)
                self.assertFalse(self.held(name))

    def test_flock_unlock_releases_the_lock_that_a_child_inherited(self):
        holder, _ = self.hold("acquire_model_library_lifecycle_lock shared; acquire_model_library_hot_lock shared\n"
                              "sleep 60 &\n"
                              'flock -u "$PULSAR_MODEL_LIBRARY_LOCK_FD"; flock -u "$PULSAR_MODEL_LIBRARY_HOT_LOCK_FD"\n'
                              "echo released; wait")
        for _, name, _ in LOCKS:
            self.assertFalse(self.held(name), name)
        self.assertIsNone(holder.poll())  # the script and its child still have both descriptors open


if __name__ == "__main__":
    unittest.main()
