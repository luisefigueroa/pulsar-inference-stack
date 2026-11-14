"""Pins: an ordinary Start of two- and three-node recipes in the default node
order, through the real up.sh, launcher and their helpers, on a saved
four-node ring (three nodes: a full mesh) with Docker, SSH and curl doubles
(tests/support/service_cluster.py). Image, memory, model-file and
post-health checks are doubles that pass; these pins record where Start acts
and in which order, so that Start on named nodes leaves the default order
unchanged.
"""
import fcntl
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from support.service_cluster import ServiceCluster  # noqa: E402

PROBES = ['node-0 docker inspect by name', 'node-1 docker inspect by name over ssh']
CHECKED = PROBES + ['port 8000 on local', 'port 29500 on local']
# The launcher probes the remote ranks first, then this node, and rechecks the ports.
LAUNCHER_PROBES = ['node-1 docker inspect by name over ssh', 'node-0 docker inspect by name',
                   'port 8000 on local', 'port 29500 on local']
LAUNCH = ['node-1 docker run rank 1 over ssh', 'node-0 docker run rank 0']
SERVED = ['curl http://127.0.0.1:8000/health', 'curl http://127.0.0.1:8000/v1/completions']


class Scenario(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())))

    def trace(self):
        """Where Start acted, in order (Docker's read-only `info` left out)."""
        return [line for line in self.cluster.trace() if 'docker info' not in line]

    def lines(self, result, spec):
        text = (result.stdout + result.stderr).replace(spec['spec_id'], '<spec>')
        return [' '.join(line.split()) for line in text.splitlines()]


class DefaultOrder(Scenario):
    def test_two_nodes(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace(), CHECKED + LAUNCHER_PROBES + LAUNCH + SERVED)
        lines = self.lines(result, spec)
        for line in ('starting rank 1 on ring-bravo', 'starting rank 0 locally',
                     'waiting for http://127.0.0.1:8000/health (cold load can take ~10 min; up to 2 health checks, '
                     '0 s apart)', 'url: http://127.0.0.1:8000/v1', 'status: ./pulsar status <spec>',
                     'stop: ./pulsar stop <spec>'):
            self.assertIn(line, lines)
        (record,) = c.store.records('services')
        plan = c.store.get('service-plans', record['plan_id'])
        self.assertEqual([rank['node_id'] for rank in plan['ranks']], [c.node_id(0), c.node_id(1)])

    def test_three_nodes_on_a_mesh(self):
        c = self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())), mesh=3)
        spec, _ = c.spec(3)
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace(), [
            'node-0 docker inspect by name', 'node-1 docker inspect by name over ssh',
            'node-2 docker inspect by name over ssh', 'port 8000 on local', 'port 29500 on local',
            'node-1 docker inspect by name over ssh', 'node-2 docker inspect by name over ssh',
            'node-0 docker inspect by name', 'port 8000 on local', 'port 29500 on local',
            'node-1 docker run rank 1 over ssh', 'node-2 docker run rank 2 over ssh', 'node-0 docker run rank 0',
            *SERVED])

    def test_an_existing_service_is_not_replaced(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        c.run(c.plan(spec, manifest, [0, 1]))
        c.forget_calls()
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.trace(), PROBES)
        self.assertIn('BLOCKED service_exists: a service for this spec already exists (container vllm-cluster-<spec> '
                      'on fixture-ring-dorado, fixture-ring-bravo). Next: ./pulsar status e51939b8d97d',
                      self.lines(result, spec))

    def test_replace(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        c.run(c.plan(spec, manifest, [0, 1]))
        c.forget_calls()
        result = c.start(spec['spec_id'], '--skip-preflight', '--replace')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace(), PROBES + [
            'node-1 docker inspect by name over ssh', 'node-0 docker inspect by name',
            # remove_stack_owned_cluster: probe, revalidate by ID, remove high to low, confirm each.
            'node-1 docker inspect by name over ssh', 'node-0 docker inspect by name',
            'node-1 docker inspect by id over ssh', 'node-0 docker inspect by id',
            'node-1 docker rm by id over ssh', 'node-1 docker inspect by id over ssh',
            'node-0 docker rm by id', 'node-0 docker inspect by id',
            'port 8000 on local', 'port 29500 on local'] + LAUNCH + SERVED)

    def test_a_failed_launch_removes_what_it_started_and_retires_its_record(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_RUN_FAILS=c.node_id(0))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.trace(), CHECKED + LAUNCHER_PROBES + [
            'node-1 docker run rank 1 over ssh', 'node-0 docker run fails', 'node-1 docker rm by id over ssh',
            'node-0 docker state', 'node-1 docker state over ssh'])
        lines = self.lines(result, spec)
        self.assertIn('FAILED container_start_failed: fixture-ring-dorado (rank 0): the service container could not '
                      'be started. Next: ./pulsar doctor', lines)
        # The plan and record are saved just before the first docker run; a
        # failed launch removes its containers and, once every node shows
        # them gone, retires the record it saved (the plan stays as history).
        self.assertIn('No container this start launched is left on its nodes; retired its service record.', lines)
        self.assertEqual((len(c.store.records('services')), len(c.remaining())), (0, 0))
        self.assertEqual(len(c.store.records('service-plans')), 1)
        # A retry on the same nodes starts.
        c.forget_calls()
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual((result.returncode, len(c.store.records('services')), len(c.remaining())), (0, 1, 2))

    def test_a_failed_verification_after_health_retires_the_record_too(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_VERIFY_RC='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('all-rank verification failed after health', result.stderr)
        self.assertIn('No container this start launched is left on its nodes; retired its service record.',
                      self.lines(result, spec))
        self.assertEqual((c.store.records('services'), c.remaining()), ([], []))

    def test_a_container_left_by_a_failed_launch_keeps_the_record(self):
        c = self.cluster
        spec, _ = c.spec(2)
        # Rank 1's removal reports success but keeps the container.
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_RUN_FAILS=c.node_id(0),
                         SERVICE_RM_KEEPS=c.node_id(1))
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"The service record this start saved is kept, because removing its containers could not be "
                      f"confirmed on fixture-ring-bravo (rank 1). Next: ./pulsar stop {spec['spec_id'][:12]}, which "
                      'removes what remains and retires the record.', self.lines(result, spec))
        self.assertEqual((len(c.store.records('services')), len(c.remaining())), (1, 1))

    def test_a_retry_after_a_kept_record_retires_it_when_its_containers_are_gone(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        # The same service's record, left with no container (as a failed start
        # left one before failed launches retired their record).
        c.plan(spec, manifest, [0, 1])
        c.write()
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_RUN_FAILS=c.node_id(0))
        self.assertEqual(result.returncode, 1)
        self.assertIn('No container this start launched is left on its nodes; retired its service record.',
                      self.lines(result, spec))
        self.assertEqual((len(c.store.records('services')), len(c.remaining())), (0, 0))

    def test_a_failed_replace_retires_the_record_when_every_container_is_gone(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        # A running service of the spec on the default order; --replace removes
        # it, then the new launch fails on the first node.
        c.run(c.plan(spec, manifest, [0, 1]))
        result = c.start(spec['spec_id'], '--skip-preflight', '--replace', SERVICE_RUN_FAILS=c.node_id(0))
        self.assertEqual(result.returncode, 1)
        lines = self.lines(result, spec)
        self.assertTrue(any(line.startswith('FAILED container_start_failed') for line in lines), lines)
        self.assertIn('No container this start launched is left on its nodes; retired its service record.', lines)
        self.assertEqual((c.store.records('services'), c.remaining()), ([], []))

    def test_the_engines_memory_refusal_is_quoted_from_the_failed_ranks_log(self):
        c = self.cluster
        spec, _ = c.spec(2)
        refusal = ("ValueError: Free memory on device cuda:0 (86.66/121.69 GiB) on startup is less than desired "
                   "GPU memory utilization (0.75, 91.27 GiB)")
        # Rank 0's engine refuses at its startup and its container exits; the
        # API server's own traceback follows, past the 80 lines shown.
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_HEALTHY='none', SERVICE_RUN_EXITS=c.node_id(0),
                         SERVICE_LOG_TEXT=f'(EngineCore_DP0 pid=7) {refusal}', SERVICE_LOG_NODES=c.node_id(0),
                         SERVICE_LOG_AFTER='150')
        self.assertEqual(result.returncode, 1)
        lines = self.lines(result, spec)
        self.assertIn('FAILED container_exited: fixture-ring-dorado (rank 0): the service container exited before '
                      "it became healthy. Note: The cluster's containers were removed; the logs are shown above.",
                      lines)
        self.assertIn(f'engine on fixture-ring-dorado (rank 0): {refusal}', lines)
        self.assertIn('The engine measures free GPU memory when it starts, and that reading varies from one start '
                      'to the next, so a retry can pass.', lines)
        # The last 80 lines are shown, as before; the quote comes from the deeper read.
        self.assertIn('fixture API server traceback line 149', lines)
        self.assertNotIn('fixture API server traceback line 69', lines)
        self.assertEqual(sum(refusal in line for line in lines), 1)
        # The log was read before the clean-up removed the container, once.
        trace = self.trace()
        self.assertLess(trace.index('node-0 docker logs'), trace.index('node-0 docker rm by id'))
        self.assertEqual(trace.count('node-0 docker logs'), 1)
        # A failure whose log holds no such line says nothing of it.
        c.forget_calls()
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_HEALTHY='none', SERVICE_RUN_EXITS=c.node_id(0))
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('engine on', result.stdout + result.stderr)

    def test_a_health_timeout_removes_both_ranks_and_retires_the_record(self):
        c = self.cluster
        spec, _ = c.spec(2)
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_HEALTHY='none')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.trace(), CHECKED + LAUNCHER_PROBES + LAUNCH + [
            'curl http://127.0.0.1:8000/health', 'node-0 docker state', 'node-1 docker state over ssh',
            'curl http://127.0.0.1:8000/health', 'node-0 docker state', 'node-1 docker state over ssh',
            'node-0 docker logs', 'node-1 docker logs over ssh',
            'node-0 docker rm by id', 'node-1 docker rm by id over ssh',
            'node-0 docker state', 'node-1 docker state over ssh'])
        self.assertIn("FAILED health_timeout: fixture-ring-dorado (rank 0): the service did not become healthy in time. "
                      "Note: The cluster's containers were removed; the logs are shown above.",
                      self.lines(result, spec))
        self.assertIn('No container this start launched is left on its nodes; retired its service record.',
                      self.lines(result, spec))
        self.assertEqual((len(c.store.records('services')), len(c.remaining())), (0, 0))


class ConcurrentStart(Scenario):
    """Two Starts of one service at once: the same spec, saved cluster and
    nodes, so the same record and container name. One launcher at a time
    holds the service; another is refused before it probes or saves."""

    STARTING = ['BLOCKED start_in_progress: another start of this service is running. Next: ./pulsar status '
                'e51939b8d97d', 'Wait for that start to finish; it reports whether the service is ready. If it is '
                'no longer needed, interrupt it in its terminal.']

    def assert_refused(self, lines):
        index = lines.index(self.STARTING[0])
        self.assertEqual(lines[index:index + 2], self.STARTING)
        # The error line names the Start that holds the service.
        self.assertRegex(lines[index + 2], r'^error: another start of this service is running: .+ \(pid \d+, '
                                           r'started less than a minute ago\)\. Wait for it to finish')

    def wait_for(self, path, process):
        for _ in range(1200):
            if path.exists() or process.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(path.exists(), (self.cluster.root / 'start-output.log').read_text())

    def test_a_failed_second_start_cannot_retire_the_record_the_first_start_launches_on(self):
        c = self.cluster
        spec, _ = c.spec(2)
        # The first Start saved its record and pauses at its first docker run.
        first = c.start_in_background(spec['spec_id'], '--skip-preflight', SERVICE_RUN_PAUSE=c.node_id(1))
        self.addCleanup(lambda: first.poll() is not None or first.kill())
        # A failed test leaves no paused double behind.
        self.addCleanup((c.root / 'run-resume').touch)
        self.wait_for(c.root / 'run-paused', first)
        # The second Start's docker run would fail; without the lock it saved
        # the same record, then retired it as its own containers were gone.
        second = c.start(spec['spec_id'], '--skip-preflight', SERVICE_RUN_FAILS=c.node_id(1))
        (c.root / 'run-resume').touch()
        self.assertEqual(first.wait(timeout=180), 0, (c.root / 'start-output.log').read_text())
        self.assertEqual(second.returncode, 1)
        lines = self.lines(second, spec)
        self.assert_refused(lines)
        self.assertIn('pulsar start <spec> --skip-preflight (pid', lines[lines.index(self.STARTING[0]) + 2])
        # Only the first Start ran containers.
        self.assertEqual([line for line in self.trace() if 'docker run' in line], LAUNCH)
        (record,) = c.store.records('services')
        self.assertEqual(len(c.remaining()), 2)
        self.assertEqual({container['labels']['io.pulsar.gb10.launch-plan'] for container in c.remaining()},
                         {record['plan_id']})

    def test_a_held_service_refuses_the_launch_before_it_probes_or_saves(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        service_id = c.plan(spec, manifest, [0, 1], record=False)['service_id']
        locks = c.root / 'model-library' / 'start-locks'
        locks.mkdir(parents=True)
        with open(locks / f'{service_id}.lock', 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual(result.returncode, 1)
        self.assert_refused(self.lines(result, spec))
        # Start's own checks ran; the launcher stopped before its probes.
        self.assertEqual(self.trace(), CHECKED)
        self.assertEqual(c.store.records('services'), [])
        self.assertFalse((c.root / 'containers.json').exists())
        # Released, the same Start launches.
        c.forget_calls()
        result = c.start(spec['spec_id'], '--skip-preflight')
        self.assertEqual((result.returncode, len(c.store.records('services')), len(c.remaining())), (0, 1, 2))

    def test_replace_removes_nothing_while_another_start_holds_the_service(self):
        c = self.cluster
        spec, manifest = c.spec(2)
        # The service's containers run while the Start that launched them
        # still waits for health and holds the service.
        plan = c.plan(spec, manifest, [0, 1])
        running = c.run(plan)
        c.forget_calls()
        locks = c.root / 'model-library' / 'start-locks'
        locks.mkdir(parents=True)
        with open(locks / f"{plan['service_id']}.lock", 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = c.start(spec['spec_id'], '--skip-preflight', '--replace')
        self.assertEqual(result.returncode, 1)
        self.assert_refused(self.lines(result, spec))
        # Start's checks saw the service; the launcher removed nothing.
        self.assertEqual(self.trace(), PROBES)
        self.assertEqual(c.removed(), [])
        self.assertTrue(c.recorded(plan))
        self.assertEqual(c.remaining(), running)


if __name__ == '__main__':
    unittest.main()
