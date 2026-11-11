"""Stop and inventory for an ordinary multi-node service whose saved record
places it off the default node order, for example a pair of a four-node ring
that does not include this machine.

Start cannot create such a service yet, so each test builds it as the
launcher leaves one: the launch plan and service record saved, and a
container labelled by rank_spec on each node the plan names
(tests/support/service_cluster.py). Docker and SSH are doubles.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from scripts.container_runtime import PLAN_LABEL, PREFIX  # noqa: E402
from scripts.service_state import binding, recorded_ownership  # noqa: E402
from support.service_cluster import ServiceCluster  # noqa: E402


class Cluster(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())))

    def pair(self, recorded_on=(2, 3), *, variant=0, record=True, run=True, **options):
        """A two-node service whose plan names saved ranks RECORDED_ON."""
        c = self.cluster
        spec, manifest = c.spec(2, variant)
        plan = c.plan(spec, manifest, list(recorded_on), record=record)
        containers = c.run(plan, **options) if run else []
        return spec, plan, containers


class OwnershipTest(Cluster):
    """The test itself, on one container: each condition, and when it does not apply."""

    def judge(self, container, position, plans, *, node_ids=None, topology_id=None):
        c = self.cluster
        return recorded_ownership(container, position=position, topology_id=topology_id or c.topology_id,
                                  node_ids=node_ids or [c.node_id(index) for index in range(4)],
                                  bindings=[binding(plan) for plan in plans])

    def test_each_condition(self):
        c = self.cluster
        _, plan, (first, second) = self.pair()
        self.assertEqual(self.judge(first, 2, [plan]), (True, ''))
        self.assertEqual(self.judge(second, 3, [plan]), (True, ''))
        refused = 'does not match its saved service record: '
        cases = {
            'managed': ({PREFIX + 'managed': 'yes'}, None, 2, "its managed label is not exactly 'true'"),
            'topology': ({PREFIX + 'topology': 'e' * 64}, None, 2, 'its topology label names another saved cluster'),
            'name': ({}, 'vllm-cluster-other', 2, f"its name is not {plan['container_name']}"),
            'conf': ({PREFIX + 'conf': 'e' * 64}, None, 2, 'its spec labels name another spec'),
            'spec': ({PREFIX + 'spec-id': 'e' * 64}, None, 2, 'its spec labels name another spec'),
            'rank': ({PREFIX + 'rank': '2'}, None, 2, "its rank label '2' is not a rank of the record"),
            'node-id': ({PREFIX + 'node-id': c.node_id(1)}, None, 2,
                        'its node-id label is not the node the record names for rank 0'),
            'position': ({}, None, 1, 'it runs on another node than the record names for rank 0'),
        }
        for name, (labels, other_name, position, reason) in cases.items():
            with self.subTest(name):
                container = copy.deepcopy(first)
                container['labels'].update(labels)
                container['name'] = other_name or container['name']
                self.assertEqual(self.judge(container, position, [plan]), (False, refused + reason))

    def test_a_record_of_another_saved_cluster(self):
        _, plan, (first, _) = self.pair()
        self.assertEqual(self.judge(first, 2, [plan], topology_id='e' * 64),
                         (False, 'does not match its saved service record: the record was saved under another '
                                 'saved cluster'))

    def test_a_changed_membership_moves_the_recorded_node(self):
        c = self.cluster
        _, plan, (first, _) = self.pair()
        swapped = [c.node_id(0), c.node_id(1), c.node_id(3), c.node_id(2)]
        self.assertEqual(self.judge(first, 2, [plan], node_ids=swapped)[0], False)

    def test_where_it_does_not_apply(self):
        c = self.cluster
        # No record names the container's plan: a retired record's plan is history.
        _, plan, (first, _) = self.pair()
        self.assertIsNone(self.judge(first, 2, []))
        other = dict(first, labels=dict(first['labels'], **{PLAN_LABEL: 'e' * 64}))
        self.assertIsNone(self.judge(other, 2, [plan]))
        # The default node order and one-node services keep the saved-position test.
        _, default, (head, _) = self.pair((0, 1), variant=1)
        self.assertIsNone(self.judge(head, 0, [default]))
        spec, manifest = c.spec(1)
        single = c.plan(spec, manifest, [2])
        (container,) = c.run(single)
        self.assertIsNone(self.judge(container, 2, [single]))
        # A guarded trial keeps its own stop path.
        spec, manifest = c.spec(2, catalog=False, guarded=True)
        trial = c.plan(spec, manifest, [2, 3])
        first_rank, _ = c.run(trial)
        self.assertIsNone(self.judge(first_rank, 2, [trial]))


class Agreement(Cluster):
    """Named Stop, stop --all and inventory's safe-to-stop answer the same for
    the same container, because they ask the same ownership test."""

    def callers(self, ids):
        """Whether each caller removes the containers IDS (inventory: offers to),
        and whether it succeeds: the public named stop, its label sweep alone,
        stop --all, and inventory. Containers and records are put back between
        them."""
        c = self.cluster
        before = (c.root / 'containers.json').read_text()
        records = c.store.records('services')

        def restore():
            (c.root / 'containers.json').write_text(before)
            (c.root / 'mutations.jsonl').unlink(missing_ok=True)
            for row in records:
                c.store.put('services', row['service_id'], row)

        spec = c.remaining()[0]['labels'][PREFIX + 'conf']
        answers = {}
        self.said = {}
        for caller, run in (('stop', lambda: c.pulsar('stop', spec)),
                            ('stop label sweep', lambda: c.bash(f'stop_named_service_by_labels {spec}')),
                            ('stop --all', lambda: c.pulsar('stop', '--all', '--yes'))):
            result = run()
            self.said[caller] = result.stderr
            removed = sorted(item for _, item in c.removed())
            answers[caller] = removed == sorted(ids), result.returncode == 0
            self.assertIn(removed, ([], sorted(ids)), caller)
            restore()
        result = c.pulsar('inventory', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        (service,) = json.loads(result.stdout)['services']
        safe = service['safe_to_stop'] and all(rank['safe_to_stop'] for rank in service['ranks'])
        self.reasons = service['reasons']
        self.said['inventory'] = ' '.join(service['reasons'])
        answers['inventory'] = safe, safe
        return answers

    def assert_said(self, reason):
        """Each caller gives the same reason for a refusal of the ownership test."""
        for caller, text in self.said.items():
            self.assertIn(f'does not match its saved service record: {reason}', ' '.join(text.split()), caller)

    def assert_agree(self, containers, removable):
        answers = self.callers([item['id'] for item in containers])
        self.assertEqual(answers, dict.fromkeys(('stop', 'stop label sweep', 'stop --all', 'inventory'),
                                                (removable, removable)), self.reasons)

    def test_a_matching_record(self):
        _, _, containers = self.pair()
        self.assert_agree(containers, True)

    def test_no_record(self):
        _, _, containers = self.pair(record=False)
        self.assert_agree(containers, False)

    def test_a_record_whose_plan_names_other_nodes(self):
        # The containers carry the recorded plan's labels, but run on 2 and 3
        # while the plan names 1 and 2.
        _, _, containers = self.pair((1, 2), positions=[2, 3])
        self.assert_agree(containers, False)
        self.assert_said('it runs on another node than the record names for rank 0')

    def test_a_wrong_launch_plan_label(self):
        _, _, containers = self.pair(labels={PLAN_LABEL: 'e' * 64})
        self.assert_agree(containers, False)

    def test_a_wrong_topology_label(self):
        _, _, containers = self.pair(labels={PREFIX + 'topology': 'e' * 64})
        self.assert_agree(containers, False)
        self.assert_said('its topology label names another saved cluster')


class RecordedStop(Cluster):
    """`./pulsar stop SPEC` of a pair recorded on saved ranks 2 and 3."""

    def setUp(self):
        super().setUp()
        self.spec, self.plan, self.containers = self.pair()
        self.ids = sorted(item['id'] for item in self.containers)

    def stop(self, *args, **env):
        return self.cluster.pulsar('stop', self.spec['spec_id'], *args, **env)

    def assert_untouched(self):
        c = self.cluster
        self.assertEqual(c.removed(), [])
        self.assertTrue(c.recorded(self.plan))
        self.assertTrue(set(self.ids) <= {item['id'] for item in c.remaining()})

    def test_it_stops_on_its_recorded_nodes_and_retires_only_its_record(self):
        c = self.cluster
        prefix = self.spec['spec_id'][:12]
        result = self.stop()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         f"Stopped spec {prefix} on fixture-ring-echo (rank 0), fixture-ring-alpha (rank 1): removed "
                         "2 container(s) and retired its service record.\n"
                         "Stopped. Model files, pins, archives and experiment evidence are retained.\n")
        self.assertEqual(result.stderr, '')
        self.assertEqual(sorted(c.removed()), sorted([(c.node_id(2), self.containers[0]['id']),
                                                      (c.node_id(3), self.containers[1]['id'])]))
        self.assertFalse(c.recorded(self.plan))
        self.assertEqual(c.store.get('service-plans', self.plan['plan_id']), self.plan)

    def test_json_says_it_stopped(self):
        result = self.stop('--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['result'],
                         {'completed': True, 'spec_id': self.spec['spec_id'], 'stopped': True})

    def test_other_services_are_untouched_and_the_reverse(self):
        c = self.cluster
        _, default, default_containers = self.pair((0, 1), variant=1)
        _, elsewhere, elsewhere_containers = self.pair((1, 0), variant=2)
        spec, manifest = c.spec(2, catalog=False, guarded=True)
        trial = c.plan(spec, manifest, [3, 2])
        trial_containers = c.run(trial)
        result = self.stop()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(item for _, item in c.removed()), self.ids)
        for plan in (default, elsewhere, trial):
            self.assertTrue(c.recorded(plan))
        # The reverse: stopping the default-order service and the guarded trial
        # leaves another pair recorded off the default order alone.
        _, again, again_containers = self.pair((3, 2), variant=3)
        for selector, containers in ((default['selected_spec_id'], default_containers),
                                     (trial['selected_spec_id'], trial_containers),
                                     (elsewhere['selected_spec_id'], elsewhere_containers)):
            with self.subTest(selector=selector[:12]):
                before = len(c.removed())
                result = c.pulsar('stop', selector)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sorted(item for _, item in c.removed()[before:]),
                                 sorted(item['id'] for item in containers))
        self.assertTrue(c.recorded(again))
        self.assertEqual(sorted(item['id'] for item in c.remaining()), sorted(item['id'] for item in again_containers))

    def test_an_unobservable_recorded_node_removes_nothing(self):
        c = self.cluster
        result = self.stop(SERVICE_UNREACHABLE=c.node_id(3))
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'error: stop did not run: {c.hostname(3)} cannot be observed', result.stderr)
        self.assert_untouched()

    def test_a_membership_change_during_the_stop_removes_nothing(self):
        c = self.cluster
        result = self.stop(SERVICE_SWAP_AFTER_PROBE=str(c.changed_membership()))
        self.assertEqual(result.returncode, 1)
        self.assertIn('error: nothing was removed: confirmed membership changed while stop was running; the service '
                      'record is kept.', result.stderr)
        self.assert_untouched()

    def test_two_matches_on_one_node_remove_nothing(self):
        c = self.cluster
        c.run(self.plan, ranks=[0])
        result = self.stop()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"error: more than one container matches spec {self.spec['spec_id'][:12]}'s service on "
                      f"{c.hostname(2)} (rank 0); nothing was removed. Next: ./pulsar inventory", result.stderr)
        self.assert_untouched()

    def test_a_container_of_its_plan_that_fails_the_ownership_test_removes_nothing(self):
        c = self.cluster
        c.containers = [dict(item, labels=dict(item['labels'], **{PREFIX + 'topology': 'e' * 64}))
                        if item['id'] == self.containers[1]['id'] else item for item in c.remaining()]
        c.write()
        result = self.stop()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"error: container {self.containers[1]['id'][:12]} on {c.hostname(3)} carries the launch plan "
                      f"of spec {self.spec['spec_id'][:12]}'s service, but it does not match its saved service "
                      "record: its topology label names another saved cluster; nothing was removed.", result.stderr)
        self.assert_untouched()

    def test_the_record_is_kept_until_absence_is_confirmed_on_every_node(self):
        c = self.cluster
        result = self.stop(SERVICE_RM_KEEPS=c.node_id(3))
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"error: the container of spec {self.spec['spec_id'][:12]}'s service on {c.hostname(3)} "
                      "(rank 1) could not be confirmed absent; the service record is kept.", result.stderr)
        self.assertTrue(c.recorded(self.plan))

    def test_a_record_with_no_container_left_is_retired_and_said_so(self):
        c = self.cluster
        c.containers = []
        c.write()
        result = self.stop('--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['result']['stopped'])
        self.assertIn(f"Spec {self.spec['spec_id'][:12]} had no container left on fixture-ring-echo (rank 0), "
                      "fixture-ring-alpha (rank 1); retired its stale service record.", result.stderr)
        self.assertFalse(c.recorded(self.plan))

    def test_same_name_with_another_plan_is_left_and_reported(self):
        c = self.cluster
        other = dict(c.remaining()[0], id='f' * 64,
                     labels=dict(self.containers[0]['labels'], **{PLAN_LABEL: 'e' * 64}))
        c.containers = [other if item['id'] == self.containers[0]['id'] else item for item in c.remaining()]
        c.write()
        result = self.stop()
        self.assertEqual(c.removed(), [(c.node_id(3), self.containers[1]['id'])])
        self.assertFalse(c.recorded(self.plan))
        self.assertIn(f"warning: left container ffffffffffff on {c.hostname(2)}: it has the name of spec "
                      f"{self.spec['spec_id'][:12]}'s service but another launch plan, so it is not that "
                      "service's. Next: ./pulsar inventory", result.stderr)
        # The label stop that follows refuses it too, and says why.
        self.assertIn(f'refusing spec {self.spec["spec_id"][:12]} id=ffffffffffff on {c.hostname(2)}', result.stderr)
        self.assertEqual(result.returncode, 2)
        self.assertEqual([item['id'] for item in c.remaining()], ['f' * 64])

    def test_its_plan_on_a_node_it_does_not_record_is_left_and_reported(self):
        c = self.cluster
        (extra,) = c.run(self.plan, positions=[0, 1], ranks=[0])
        result = self.stop()
        self.assertEqual(sorted(item for _, item in c.removed()), self.ids)
        self.assertEqual([item['id'] for item in c.remaining()], [extra['id']])
        self.assertIn(f'refusing spec {self.spec["spec_id"][:12]} id={extra["id"][:12]} on {c.hostname(0)}',
                      result.stderr)
        self.assertEqual(result.returncode, 2)

    def test_node_cannot_select_one_node(self):
        c = self.cluster
        result = self.stop('--node', c.hostname(2))
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"error: spec {self.spec['spec_id'][:12]} runs on fixture-ring-echo, fixture-ring-alpha as one "
                      "multi-node service; it stops on all its nodes together, so --node cannot select one. Nothing "
                      f"was stopped. Next: ./pulsar stop {self.spec['spec_id'][:12]}", result.stderr)
        self.assertFalse((c.root / 'docker-calls.jsonl').exists())
        self.assert_untouched()


if __name__ == '__main__':
    unittest.main()
