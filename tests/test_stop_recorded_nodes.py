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
        """Whether each caller would remove the containers IDS: Stop's label
        sweep, stop --all and inventory. Containers and records are put back
        between them."""
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
        result = c.bash(f'stop_named_service_by_labels {spec}')
        self.said['stop'] = result.stderr
        answers['stop'] = sorted(item for _, item in c.removed()) == sorted(ids), result.returncode
        restore()
        result = c.script('cluster/stop-cluster.sh', '--all')
        self.said['stop --all'] = result.stderr
        answers['stop --all'] = sorted(item for _, item in c.removed()) == sorted(ids), result.returncode
        restore()
        result = c.pulsar('inventory', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        (service,) = json.loads(result.stdout)['services']
        safe = service['safe_to_stop'] and all(rank['safe_to_stop'] for rank in service['ranks'])
        self.reasons = service['reasons']
        self.said['inventory'] = ' '.join(service['reasons'])
        answers['inventory'] = safe, 0 if safe else 2
        return answers

    def assert_said(self, reason):
        """Each caller gives the same reason for a refusal of the ownership test."""
        for caller, text in self.said.items():
            self.assertIn(f'does not match its saved service record: {reason}', text, caller)

    def assert_agree(self, containers, removable):
        answers = self.callers([item['id'] for item in containers])
        rc = 0 if removable else 2
        expected = {'stop': (removable, rc), 'stop --all': (removable, 0 if removable else 1),
                    'inventory': (removable, rc)}
        self.assertEqual(answers, expected, self.reasons)

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


if __name__ == '__main__':
    unittest.main()
