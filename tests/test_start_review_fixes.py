"""Start's launch, plan check and one service per spec, under the doubles of
tests/support/service_cluster.py: the launcher builds every rank's command
before the first container starts, the plan check fails closed, and the saved
records allow one service per spec even for two Starts at once. Nothing here
contacts a node."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from support.service_cluster import ServiceCluster  # noqa: E402


class Cluster(unittest.TestCase):
    maxDiff = None
    MESH = 4

    def setUp(self):
        self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())), mesh=self.MESH)

    def trace(self):
        return [line for line in self.cluster.trace() if 'docker info' not in line]


class EveryCommandBeforeTheFirstContainer(Cluster):
    """A rank whose command cannot be built starts nothing, even the last one."""

    def builds(self, spec, args, **env):
        c = self.cluster
        c.forget_calls()
        return c.start(spec['spec_id'], '--skip-preflight', *args, SERVICE_ARGV_RANK='2', **env)

    def test_a_failed_build_of_the_last_ranks_command_starts_no_container(self):
        c = self.cluster
        spec, _ = c.spec(3)
        for args, positions in (((), [0, 1, 2]),
                                (('--placement-nodes', ','.join(c.hostname(p) for p in (2, 0, 3))), [2, 0, 3])):
            with self.subTest(positions=positions):
                # How many times a Start builds rank 2's command; the last of those fails.
                result = self.builds(spec, args, SERVICE_HEALTHY='none')
                self.assertEqual(result.returncode, 1)
                self.assertEqual(c.remaining(), [])
                count = len((c.root / 'argv-builds').read_text().splitlines())
                for record in c.store.records('services'):
                    c.store.remove('services', record['service_id'])
                result = self.builds(spec, args, SERVICE_ARGV_FAILS_AT=str(count))
                self.assertEqual(result.returncode, 1)
                self.assertIn('fixture: the launch command of rank 2 could not be read', result.stderr)
                self.assertIn('argv of rank 2 fails', self.trace())
                self.assertFalse([line for line in self.trace() if 'docker run' in line], self.trace())
                self.assertEqual(c.remaining(), [])

    def test_an_unexpected_exit_after_the_containers_started_removes_them(self):
        # A step between the launch and the health wait fails: every container
        # this launch started is removed, on the default order and on named nodes.
        c = self.cluster
        spec, _ = c.spec(3)
        for args in ((), ('--placement-nodes', ','.join(c.hostname(p) for p in (2, 0, 3)))):
            with self.subTest(args=args):
                c.forget_calls()
                for record in c.store.records('services'):
                    c.store.remove('services', record['service_id'])
                result = c.start(spec['spec_id'], '--skip-preflight', *args, SERVICE_FAILS_LOADING='1')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('the launch stopped unexpectedly', result.stderr, (result.stdout, self.trace()))
                trace = self.trace()
                self.assertEqual(len([line for line in trace if 'docker run' in line]), 3, trace)
                self.assertEqual(len([line for line in trace if 'docker rm by id' in line]), 3, trace)
                self.assertEqual(c.remaining(), [])


class ThePlanCheckFailsClosed(Cluster):
    """Start hands the launcher the ID of the plan it checked; without it, nothing launches."""

    def test_a_plan_without_its_id_launches_nothing(self):
        c = self.cluster
        spec, _ = c.spec(3)
        for args in ((), ('--placement-nodes', ','.join(c.hostname(p) for p in (2, 0, 3)))):
            with self.subTest(args=args):
                c.forget_calls()
                result = c.start(spec['spec_id'], '--skip-preflight', *args, SERVICE_PLAN_WITHOUT_ID='1')
                self.assertEqual(result.returncode, 1)
                self.assertIn('error: the launch plan start checked cannot be read, so the launcher could not '
                              'confirm it builds the same one; nothing was launched', result.stderr)
                # The launcher never ran: it prints its spec line first.
                self.assertNotIn('exact spec:', result.stdout)
                self.assertFalse([line for line in self.trace() if 'docker run' in line])
                self.assertEqual(c.calls()['runs'], [])

    def test_a_plan_the_launcher_builds_differently_still_starts_nothing(self):
        # With the ID handed over, the launcher's own check refuses a different plan.
        c = self.cluster
        spec, _ = c.spec(3)
        result = c.start(spec['spec_id'], '--skip-preflight', SERVICE_PREPARED_DRIFT='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('error: the launch plan differs from the one start checked', result.stderr)
        self.assertEqual(c.calls()['runs'], [])


if __name__ == '__main__':
    unittest.main()
