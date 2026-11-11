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
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
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


class StopAll(Cluster):
    """`./pulsar stop --all --yes` with a pair recorded on saved ranks 2 and 3."""

    def test_both_kinds_are_stopped_and_an_unrecorded_container_is_refused_and_named(self):
        c = self.cluster
        spec, plan, containers = self.pair()
        _, default, default_containers = self.pair((0, 1), variant=1)
        stray_spec, _, stray = self.pair((1, 0), variant=2, record=False)
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"Stopped spec {spec['spec_id'][:12]} on fixture-ring-echo (rank 0), fixture-ring-alpha (rank 1): "
                      "removed 2 container(s) and retired its service record.", result.stdout)
        for item in stray:
            self.assertIn(f"refusing managed candidate id={item['id'][:12]}", result.stderr)
        self.assertIn(f"spec {stray_spec['spec_id'][:12]} nodes=2 rank=0", result.stderr)
        self.assertIn('warning: some candidates were refused (unknown spec or rank placement)', result.stderr)
        self.assertEqual(sorted(item for _, item in c.removed()),
                         sorted(item['id'] for item in containers + default_containers))
        self.assertEqual(sorted(item['id'] for item in c.remaining()), sorted(item['id'] for item in stray))
        self.assertFalse(c.recorded(plan))
        # A refused candidate stops the spec-wide retire, as it always has.
        self.assertTrue(c.recorded(default))

    def test_without_an_unrecorded_container_everything_is_stopped_and_retired(self):
        c = self.cluster
        _, plan, _ = self.pair()
        _, default, _ = self.pair((0, 1), variant=1)
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(c.remaining(), [])
        self.assertEqual([c.recorded(item) for item in (plan, default)], [False, False])

    def test_a_record_with_no_container_left_is_retired(self):
        c = self.cluster
        spec, plan, _ = self.pair(run=False)
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Spec {spec['spec_id'][:12]} had no container left", result.stdout)
        self.assertFalse(c.recorded(plan))

    def test_a_failed_recorded_stop_removes_nothing_else(self):
        c = self.cluster
        spec, plan, _ = self.pair(labels={PREFIX + 'topology': 'e' * 64})
        _, default, _ = self.pair((0, 1), variant=1)
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"error: stop --all stopped before removing the other services' containers, because spec "
                      f"{spec['spec_id'][:12]}'s service could not be stopped on its recorded nodes.", result.stderr)
        self.assertEqual(c.removed(), [])
        self.assertTrue(c.recorded(plan) and c.recorded(default))

    def test_a_guarded_trial_off_the_default_order_still_refuses_stop_all(self):
        c = self.cluster
        self.pair()
        spec, manifest = c.spec(2, catalog=False, guarded=True)
        trial = c.plan(spec, manifest, [3, 2])
        c.run(trial)
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"stop --all does not stop guarded trial {trial['guard_run_id'][:12]}", result.stderr)
        self.assertEqual(c.removed(), [])


def summary(service):
    return {'state': service['state'], 'ownership': service['ownership'], 'safe_to_stop': service['safe_to_stop'],
            'complete': service['complete'], 'observability': service['observability'],
            'required_remote_probes': [(item['rank'], item['node'], item['status'])
                                       for item in service['required_remote_probes']],
            'reasons': service['reasons'],
            'ranks': [(rank['rank'], rank['node'], rank['expected_node'], rank['ownership'], rank['safe_to_stop'])
                      for rank in service['ranks']]}


class Inventory(Cluster):
    """Inventory reads a pair recorded on saved ranks 2 and 3 as running there."""

    def inventory(self, **env):
        result = self.cluster.pulsar('inventory', '--json', **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_it_runs_on_its_recorded_nodes_and_is_safe_to_stop(self):
        self.pair()
        (service,) = self.inventory()['services']
        self.assertEqual(summary(service), {
            'state': 'running', 'ownership': 'managed', 'safe_to_stop': True, 'complete': True,
            'observability': 'complete', 'required_remote_probes': [('0', 'rank-2', 'ok'), ('1', 'rank-3', 'ok')],
            'reasons': [], 'ranks': [('0', 'rank-2', 'rank-2', 'managed', True), ('1', 'rank-3', 'rank-3', 'managed', True)]})

    def test_it_probes_no_more_nodes_than_without_its_record(self):
        c = self.cluster
        _, plan, _ = self.pair()

        def probed():
            calls = []
            for name in ('docker-calls.jsonl', 'ssh-calls.jsonl'):
                path = c.root / name
                calls += [json.loads(line)[0] for line in path.read_text().splitlines()] if path.exists() else []
                path.unlink(missing_ok=True)
            return sorted(set(calls)), len(calls)

        self.inventory()
        recorded = probed()
        c.store.remove('services', plan['service_id'])
        self.inventory()
        self.assertEqual(recorded, probed())

    def test_a_recorded_node_that_does_not_answer(self):
        c = self.cluster
        self.pair()
        (service,) = self.inventory(SERVICE_UNREACHABLE=c.node_id(3))['services']
        self.assertEqual((service['state'], service['safe_to_stop'], service['observability']),
                         ('degraded', False, 'worker_unreachable'))
        self.assertEqual([(item['rank'], item['node'], item['status']) for item in service['required_remote_probes']],
                         [('0', 'rank-2', 'ok'), ('1', 'rank-3', 'unreachable')])

    def test_a_node_outside_the_service_that_does_not_answer(self):
        # Stop needs every confirmed node to answer, so Stop is not offered,
        # but the service itself is complete on the nodes it needs.
        c = self.cluster
        self.pair()
        (service,) = self.inventory(SERVICE_UNREACHABLE=c.node_id(1))['services']
        self.assertEqual((service['state'], service['safe_to_stop'], service['complete']), ('running', False, True))

    def test_a_record_that_does_not_match_is_a_mismatch_naming_the_recorded_node(self):
        self.pair(labels={PREFIX + 'topology': 'e' * 64})
        (service,) = self.inventory()['services']
        self.assertEqual((service['state'], service['ownership'], service['safe_to_stop']),
                         ('degraded', 'mismatch', False))
        self.assertEqual([rank['expected_node'] for rank in service['ranks']], ['rank-2', 'rank-3'])
        self.assertIn('rank-2/rank=0: does not match its saved service record: its topology label names another '
                      'saved cluster', service['reasons'])

    def test_a_snapshot_without_records_reads_as_before(self):
        c = self.cluster
        self.pair()
        raw = c.snapshot()
        self.assertNotIn('service_records', raw)
        (service,) = c.inventory(raw)['services']
        self.assertEqual((service['state'], service['ownership'], service['safe_to_stop']),
                         ('degraded', 'mismatch', False))


class Pages(Cluster):
    """Running services and Home for a pair recorded on saved ranks 2 and 3."""

    def test_it_reads_as_running_on_its_nodes_and_offers_stop(self):
        spec, _, _ = self.pair()
        pages = self.cluster.pages(spec['spec_id'])
        prefix = spec['spec_id'][:12]
        # Stop, and Switch, which stops it before Start, are offered; so is
        # Restart, now that Start runs a pair on neighbouring nodes of a ring.
        self.assertEqual([action for action, _ in pages['choices']],
                         ['check', 'connect', 'logs', 'restart', 'switch', 'stop', 'stop-all', 'back'])
        self.assertEqual(pages['page'].command(spec['spec_id'], 'restart'),
                         ['--restart', spec['spec_id'], '--node',
                          f'{self.cluster.node_id(2)},{self.cluster.node_id(3)}'])
        self.assertEqual(pages['question'],
                         f'Stop and remove the service containers of example/service-synthetic [{prefix}] on '
                         'fixture-ring-echo, fixture-ring-alpha? Model files, pins and archives are kept.')
        self.assertEqual(pages['home'], [
            'example/service-synthetic running on fixture-ring-echo, fixture-ring-alpha ·\xa0port\xa08000 '
            '·\xa0seen less than a minute ago; **Running services…** has the details'])
        # Its API is on its first node; Restart and Stop are offered, so the
        # card has neither row.
        self.assertEqual(pages['screen'],
                         'example/service-synthetic running on fixture-ring-echo, fixture-ring-alpha\n'
                         '  · port 8000\n'
                         'Seen      by the service inventory <age>\n'
                         'Model     example/service-synthetic, the model name requests use\n'
                         'API       http://192.0.2.32:8000/v1 · seen\n'
                         'Key       not recorded: add the key header if the service was started with one\n'
                         'Health    not checked here; choose Check it now\n')

    def test_restart_stays_not_offered_where_start_still_refuses(self):
        # Saved ranks 1 and 3 are not neighbours: Start does not run a pair there.
        spec, _, _ = self.pair((1, 3))
        pages = self.cluster.pages(spec['spec_id'])
        self.assertNotIn('restart', [action for action, _ in pages['choices']])
        # In the launch check's words, as Start would refuse it.
        self.assertIn('Restart   not offered: fixture-ring-bravo and fixture-ring-alpha are not cabled\n'
                      '          to each other: the cluster is a ring, and this recipe needs every one\n'
                      '          of its nodes cabled to the others', pages['screen'])


WIDTHS = (80, 60)


def folded(text, width):
    """TEXT as a terminal of WIDTH columns shows it: stop's lines are not
    wrapped by Pulsar, so the terminal breaks them at the last column."""
    lines = []
    for line in text.splitlines():
        lines += [line[start:start + width] for start in range(0, max(len(line), 1), width)]
    return '\n'.join(lines) + '\n'


class Renders(Cluster):
    """What a person reads, at 80 and 60 columns with NO_COLOR=1: the stop
    output, Running services' row, card and Stop question, and Home's
    Serving row. STOP_RECORDED_RENDERS names a folder outside the checkout
    that receives them for review."""

    def setUp(self):
        super().setUp()
        self.renders = {}

    def tearDown(self):
        target = os.environ.get('STOP_RECORDED_RENDERS')
        if target and self.renders:
            target = Path(target).resolve()
            self.assertFalse(target.is_relative_to(ROOT), 'write renders outside the checkout')
            target.mkdir(parents=True, exist_ok=True)
            for name, text in self.renders.items():
                (target / f'{name}.txt').write_text(text)

    def keep(self, name, text, width):
        self.renders[f'{name}-{width}'] = text
        self.assertTrue(all(len(line) <= width for line in text.splitlines()),
                        '\n'.join(line for line in text.splitlines() if len(line) > width))

    def shown(self, result):
        return f'$ ./pulsar {self.command}\n{result.stdout}{result.stderr}'

    def test_the_stop_output(self):
        c = self.cluster
        spec, _, _ = self.pair()
        before = (c.root / 'containers.json').read_text()
        records = c.store.records('services')
        self.command = f"stop {spec['spec_id']}"
        for width in WIDTHS:
            for name, env in (('stop', {}), ('stop-unobservable', {'SERVICE_UNREACHABLE': c.node_id(3)})):
                (c.root / 'containers.json').write_text(before)
                for row in records:
                    c.store.put('services', row['service_id'], row)
                result = c.pulsar('stop', spec['spec_id'], COLUMNS=str(width), **env)
                self.assertEqual(result.returncode, 0 if name == 'stop' else 1, result.stderr)
                self.keep(name, folded(self.shown(result), width), width)

    def test_stop_all_output(self):
        c = self.cluster
        self.pair()
        self.pair((0, 1), variant=1)
        self.pair((1, 0), variant=2, record=False)
        before = (c.root / 'containers.json').read_text()
        records = c.store.records('services')
        self.command = 'stop --all --yes'
        for width in WIDTHS:
            (c.root / 'containers.json').write_text(before)
            for row in records:
                c.store.put('services', row['service_id'], row)
            result = c.pulsar('stop', '--all', '--yes', COLUMNS=str(width))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.keep('stop-all', folded(self.shown(result), width), width)

    def test_running_services_and_home(self):
        c = self.cluster
        spec, _, _ = self.pair()
        for width in WIDTHS:
            pages = c.pages(spec['spec_id'], width=width)
            page = pages['page']
            row = page.label(spec['spec_id'], width).replace('\x1f', '\n')
            self.keep('running-services-row', row + '\n', width)
            self.keep('running-services-card', pages['screen'], width)
            question = textwrap.fill(pages['question'], width) + '\n'
            self.keep('running-services-stop-question', question, width)
            self.keep('home', pages['home_block'], width)
            self.assertIn('running on fixture-ring-echo, fixture-ring-alpha', ' '.join(pages['home_block'].split()))
            self.assertNotIn('degraded', pages['home_block'] + pages['screen'])


class OtherPathsKeepTheirOutput(Cluster):
    """A guarded trial's stop and a default-order stop read as they did."""

    def test_a_guarded_trial_off_the_default_order_through_the_real_label_stop(self):
        c = self.cluster
        spec, manifest = c.spec(2, catalog=False, guarded=True)
        trial = c.plan(spec, manifest, [2, 1])
        c.run(trial)
        result = c.pulsar('stop', spec['spec_id'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         f"Stopped guarded trial {trial['guard_run_id'][:12]} of spec {spec['spec_id'][:12]} on "
                         "fixture-ring-echo (rank 0), fixture-ring-bravo (rank 1): removed 2 container(s) and retired "
                         "its service record. The trial's run directory keeps its own result.\n"
                         f"no stack-managed service is running for spec {spec['spec_id']}; nothing to stop\n"
                         "Stopped. Model files, pins, archives and experiment evidence are retained.\n")
        self.assertEqual(c.remaining(), [])
        self.assertFalse(c.recorded(trial))

    def test_another_specs_unreadable_record_does_not_block_a_default_order_stop(self):
        c = self.cluster
        _, broken, _ = self.pair()
        c.store.put('service-plans', broken['plan_id'], dict(broken, port=9000))
        spec, manifest = c.spec(2, variant=1)
        plan = c.plan(spec, manifest, [0, 1])
        containers = c.run(plan)
        result = c.pulsar('stop', spec['spec_id'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(item for _, item in c.removed()), sorted(item['id'] for item in containers))
        self.assertFalse(c.recorded(plan))
        # Inventory leaves that record out: its containers are judged by saved position.
        result = c.pulsar('inventory', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        (service,) = json.loads(result.stdout)['services']
        self.assertEqual((service['ownership'], service['safe_to_stop']), ('mismatch', False))


class RecordOfAnEarlierCluster(Cluster):
    """A record off the default order saved under an earlier saved cluster.
    Its containers' labels name that cluster, so Pulsar never removes them:
    Stop looks on every node of this cluster for one, retires the record when
    there is none, and otherwise says how to clear it. (Until this, both stops
    refused such a record with no way out.)"""

    def setUp(self):
        super().setUp()
        c = self.cluster
        self.spec, manifest = c.spec(2)
        self.plan = c.plan(self.spec, manifest, [2, 3], topology_id='e' * 64)

    def test_no_container_left_anywhere_retires_it(self):
        c = self.cluster
        result = c.pulsar('stop', self.spec['spec_id'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Spec {self.spec['spec_id'][:12]}'s service was recorded under an earlier saved cluster and "
                      'has no container on any node of this one; retired its stale service record.', result.stdout)
        self.assertFalse(c.recorded(self.plan))
        self.assertEqual(c.removed(), [])

    def test_stop_all_retires_it_too(self):
        c = self.cluster
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(c.recorded(self.plan))

    def test_its_container_still_there_is_named_and_left(self):
        c = self.cluster
        (first,) = c.run(self.plan, positions=[1, 0], ranks=[0])
        prefix = self.spec['spec_id'][:12]
        result = c.pulsar('stop', self.spec['spec_id'])
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"error: spec {prefix}'s service was recorded under an earlier saved cluster, and its container "
                      f"{first['id'][:12]} on {c.hostname(1)} is still there. Pulsar does not remove containers of an "
                      'earlier saved cluster, so nothing was removed and the record is kept. Next: remove that '
                      f"container with Docker on its node (docker rm -f {first['id'][:12]}), then rerun ./pulsar stop "
                      f'{prefix}', result.stderr)
        self.assertEqual(c.removed(), [])
        self.assertTrue(c.recorded(self.plan))
        # stop --all reports it, keeps the record and goes on to its sweep, which
        # refuses that container by its labels (tests/test_stop_review_fixes.py).
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"then rerun ./pulsar stop --all --yes", result.stderr)
        self.assertIn(f"warning: refusing managed candidate id={first['id'][:12]} on fixture-ring-bravo: does not match its "
                      'saved service record: the record was saved under another saved cluster', result.stderr)
        self.assertEqual(c.removed(), [])
        self.assertTrue(c.recorded(self.plan))

    def test_a_node_that_does_not_answer_changes_nothing(self):
        c = self.cluster
        result = c.pulsar('stop', self.spec['spec_id'], SERVICE_UNREACHABLE=c.node_id(3))
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'error: stop did not run: {c.hostname(3)} cannot be observed', result.stderr)
        self.assertTrue(c.recorded(self.plan))


class GuardedRecordOfAnEarlierCluster(Cluster):
    """A guarded trial's record saved under an earlier saved cluster gets the
    same way out as an ordinary one, once no guarded controller is running:
    Stop looks on every node of this cluster for the trial's container (its
    name, guard-run and launch-plan labels), retires the record when there is
    none, and otherwise names it and removes nothing. (Until this, Stop said it
    was recorded under a different confirmed membership and nothing cleared it.)"""

    def setUp(self):
        super().setUp()
        c = self.cluster
        self.spec, self.manifest = c.spec(2, catalog=False, guarded=True)
        self.plan = c.plan(self.spec, self.manifest, [2, 3], topology_id='e' * 64)
        self.run = self.plan['guard_run_id'][:12]

    def stop(self, **env):
        return self.cluster.pulsar('stop', self.spec['spec_id'], **env)

    def test_no_container_left_anywhere_retires_it(self):
        c = self.cluster
        for positions in ([2, 3], [0, 1]):
            with self.subTest(positions=positions):
                plan = c.plan(self.spec, self.manifest, positions, topology_id='e' * 64)
                result = self.stop()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Guarded trial {plan['guard_run_id'][:12]} of spec {self.spec['spec_id'][:12]} was "
                              'recorded under an earlier saved cluster and has no container on any node of this one; '
                              "retired its stale service record. The trial's run directory keeps its own result.",
                              ' '.join(result.stdout.split()))
                self.assertFalse(c.recorded(plan))
                self.assertEqual(c.removed(), [])

    def test_its_container_still_there_is_named_and_left(self):
        c = self.cluster
        (first,) = c.run(self.plan, positions=[1, 0], ranks=[0])
        spec_id = self.spec['spec_id']
        result = self.stop()
        self.assertEqual(result.returncode, 1)
        # The spec is a candidate, not a catalog file, so the command names its complete ID.
        self.assertIn(f"error: guarded trial {self.run} of spec {spec_id[:12]} was recorded under an earlier saved "
                      f"cluster, and its container {first['id'][:12]} on {c.hostname(1)} is still there. Pulsar does "
                      'not remove containers of an earlier saved cluster, so nothing was removed and the record is '
                      f"kept. Next: remove that container with Docker on its node (docker rm -f {first['id'][:12]}), "
                      f'then rerun ./pulsar stop {spec_id}', ' '.join(result.stderr.split()))
        self.assertEqual(c.removed(), [])
        self.assertTrue(c.recorded(self.plan))

    def test_a_container_of_another_run_is_not_the_trials(self):
        c = self.cluster
        c.run(self.plan, positions=[1, 0], labels={'io.pulsar.gb10.guard-run': 'd' * 64})
        result = self.stop()
        self.assertIn('retired its stale service record', result.stdout)
        self.assertFalse(c.recorded(self.plan))
        # The label sweep that follows refuses those containers as before: their
        # labels name the earlier cluster.
        self.assertEqual(result.returncode, 2)
        self.assertIn('ownership not proven', result.stderr)
        self.assertEqual(c.removed(), [])
        self.assertEqual(len(c.remaining()), 2)

    def test_a_node_that_does_not_answer_changes_nothing(self):
        c = self.cluster
        result = self.stop(SERVICE_UNREACHABLE=c.node_id(3))
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'error: stop did not run: {c.hostname(3)} cannot be observed', result.stderr)
        self.assertTrue(c.recorded(self.plan))
        self.assertEqual(c.removed(), [])

    def test_a_running_controller_is_still_refused_first(self):
        c = self.cluster
        lock = Path(c.env['PULSAR_MODEL_LIBRARY_DIR']) / 'lifecycle.lock'
        holder_script = c.root / 'guarded-serving.sh'
        holder_script.write_text(f'exec {{fd}}>"{lock}"\nflock -s "$fd"\nsleep 30\n')
        holder = subprocess.Popen(['bash', str(holder_script)])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        deadline = time.monotonic() + 5
        while subprocess.run(['flock', '-n', '-x', str(lock), 'true']).returncode == 0 and time.monotonic() < deadline:
            time.sleep(.05)
        c.forget_calls()
        result = self.stop()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"has guarded trial {self.run} on {c.hostname(2)}, {c.hostname(3)}, and a guarded run's "
                      'controller is running', result.stderr)
        self.assertEqual(c.trace(), [])
        self.assertTrue(c.recorded(self.plan))

    def test_stop_all_clears_it_in_its_own_pass(self):
        # On other nodes, and on the nodes of the default order too: stop --all
        # retires a trial record of an earlier saved cluster with no container
        # left, instead of refusing as it does for a trial of this one.
        c = self.cluster
        for positions in ([2, 3], [0, 1]):
            with self.subTest(positions=positions):
                self.plan = c.plan(self.spec, self.manifest, positions, topology_id='e' * 64)
                result = c.pulsar('stop', '--all', '--yes')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Guarded trial {self.plan['guard_run_id'][:12]} of spec {self.spec['spec_id'][:12]} "
                              'was recorded under an earlier saved cluster and has no container on any node of this '
                              'one; retired its stale service record.', ' '.join(result.stdout.split()))
                self.assertFalse(c.recorded(self.plan))
                self.assertEqual(c.removed(), [])


class DefaultOrderRetire(Cluster):
    """No stop path that finds containers by saved position retires the record
    of a service recorded off the default order: it never looked there."""

    def test_the_spec_wide_retire_keeps_an_off_default_record(self):
        from scripts.service_state import retire
        c = self.cluster
        spec, pair, _ = self.pair(run=False)
        _, default, _ = self.pair((0, 1), variant=1, run=False)
        _, reversed_pair, _ = self.pair((1, 0), variant=2, run=False)
        everyone = [c.node_id(index) for index in range(4)]
        self.assertEqual(retire(c.store, topology_id=c.topology_id, node_ids=everyone,
                                selected_spec_id=spec['spec_id']), [])
        self.assertEqual(retire(c.store, topology_id=c.topology_id, node_ids=everyone), [default['service_id']])
        self.assertEqual([c.recorded(plan) for plan in (pair, default, reversed_pair)], [True, False, True])

    def test_a_direct_cluster_stop_leaves_the_record_and_its_containers(self):
        c = self.cluster
        spec, plan, containers = self.pair()
        result = c.script('cluster/stop-cluster.sh', spec['spec_id'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"clean — {plan['container_name']} absent from all 2 active ranks", result.stdout)
        self.assertEqual(c.removed(), [])
        self.assertEqual(len(c.remaining()), 2)
        # Before, this retired the record while its containers kept running.
        self.assertTrue(c.recorded(plan))


if __name__ == '__main__':
    unittest.main()
