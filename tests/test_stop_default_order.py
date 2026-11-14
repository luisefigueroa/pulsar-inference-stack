"""Pins: Stop, stop --all, stop SPEC --node, inventory and the pages for
services on the default node order, and for a guarded trial off it.

Each test drives the public command through the real stop and inventory code
on a saved four-node ring, with Docker and SSH doubles (tests/support/
service_cluster.py). They record what these paths do, so that teaching Stop
the nodes a service's record names leaves them unchanged.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from support.service_cluster import ServiceCluster  # noqa: E402

KEPT = 'Stopped. Model files, pins, archives and experiment evidence are retained.\n'


class Scenario(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.cluster = ServiceCluster(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.names = {}

    def service(self, nodes, positions, *, variant=0, catalog=True, record=True, name=None, guarded=False, **run):
        """A service of a NODES-node spec on POSITIONS; NAME names its spec ID,
        its 12-character prefix and its containers' IDs in pinned output."""
        c = self.cluster
        spec, manifest = c.spec(nodes, variant, catalog=catalog, guarded=guarded)
        plan = c.plan(spec, manifest, positions, record=record)
        containers = c.run(plan, **run)
        if name:
            self.names[name] = spec['spec_id']
            self.names[f'{name}:prefix'] = spec['spec_id'][:12]
            for rank, container in enumerate(containers):
                self.names[f'{name}:{rank}'] = container['id']
                self.names[f'{name}:{rank}:short'] = container['id'][:12]
        return spec, plan, containers

    def named(self, text):
        for name, value in sorted(self.names.items(), key=lambda item: -len(item[1])):
            text = text.replace(value, f'<{name}>')
        return text

    def shown(self, result):
        """The run's exit status and output, with named IDs replaced so a pin reads."""
        return self.named(f'rc={result.returncode}\n--- stdout\n{result.stdout}--- stderr\n{result.stderr}')


class NamedStop(Scenario):
    """`./pulsar stop SPEC` on the default node order."""

    def test_a_one_node_service_on_this_node(self):
        c = self.cluster
        spec, plan, (container,) = self.service(1, [0], name='one')
        result = c.pulsar('stop', spec['spec_id'])
        self.assertEqual(self.shown(result), 'rc=0\n--- stdout\n'
                         'removing stack-managed id=<one:0:short> spec <one:prefix> rank=single on fixture-ring-dorado\n'
                         'done\n' + KEPT + '--- stderr\n')
        self.assertEqual(c.removed(), [(c.node_id(0), container['id'])])
        self.assertFalse(c.recorded(plan))

    def test_a_one_node_service_on_another_node_is_found_without_node(self):
        c = self.cluster
        spec, plan, (container,) = self.service(1, [2], name='one')
        result = c.pulsar('stop', spec['spec_id'])
        self.assertEqual(self.shown(result), 'rc=0\n--- stdout\n'
                         'removing stack-managed id=<one:0:short> spec <one:prefix> rank=single on fixture-ring-echo\n'
                         'done\n' + KEPT + '--- stderr\n')
        self.assertEqual(c.removed(), [(c.node_id(2), container['id'])])
        self.assertFalse(c.recorded(plan))

    def test_a_one_node_service_selected_by_node(self):
        c = self.cluster
        spec, plan, (container,) = self.service(1, [2], name='one')
        result = c.pulsar('stop', spec['spec_id'], '--node', c.hostname(2))
        self.assertEqual(self.shown(result), 'rc=0\n--- stdout\n'
                         'removing stack-managed id=<one:0:short> spec <one:prefix> rank=single on fixture-ring-echo\n'
                         'done\n' + KEPT + '--- stderr\n')
        self.assertEqual(c.removed(), [(c.node_id(2), container['id'])])
        self.assertFalse(c.recorded(plan))

    def test_two_and_three_node_services_and_another_spec_untouched(self):
        c = self.cluster
        _, other_plan, (kept,) = self.service(1, [3], name='other')
        hosts = ['fixture-ring-dorado', 'fixture-ring-bravo', 'fixture-ring-echo']
        for nodes in (2, 3):
            with self.subTest(nodes=nodes):
                spec, plan, containers = self.service(nodes, list(range(nodes)), name=f'multi{nodes}')
                result = c.pulsar('stop', spec['spec_id'])
                self.assertEqual(self.shown(result), 'rc=0\n--- stdout\n' + ''.join(
                    f'removing stack-managed id=<multi{nodes}:{rank}:short> spec <multi{nodes}:prefix> rank={rank} '
                    f'on {hosts[rank]}\n' for rank in range(nodes)) + 'done\n' + KEPT + '--- stderr\n')
                self.assertEqual(sorted(c.removed()[-nodes:]),
                                 sorted((c.node_id(rank), item['id']) for rank, item in enumerate(containers)))
                self.assertFalse(c.recorded(plan))
        self.assertEqual([item['id'] for item in c.remaining()], [kept['id']])
        self.assertTrue(c.recorded(other_plan))

    def test_a_candidate_outside_the_catalog_stops_from_its_labels(self):
        c = self.cluster
        spec, plan, containers = self.service(2, [0, 1], catalog=False, name='candidate')
        result = c.pulsar('stop', spec['spec_id'])
        self.assertEqual(self.shown(result), 'rc=0\n--- stdout\n'
                         'removing stack-managed id=<candidate:0:short> spec <candidate:prefix> rank=0 '
                         'on fixture-ring-dorado\n'
                         'removing stack-managed id=<candidate:1:short> spec <candidate:prefix> rank=1 '
                         'on fixture-ring-bravo\n'
                         'done\n' + KEPT + '--- stderr\n')
        self.assertEqual(len(c.removed()), 2)
        self.assertFalse(c.recorded(plan))

    def test_node_cannot_select_one_node_of_a_multi_node_service(self):
        c = self.cluster
        spec, plan, _ = self.service(2, [0, 1], name='pair')
        result = c.pulsar('stop', spec['spec_id'], '--node', c.hostname(1))
        self.assertEqual(self.shown(result), 'rc=2\n--- stdout\n--- stderr\n'
                         'warning: --node is only valid for one-node services\n')
        self.assertEqual(c.removed(), [])
        self.assertTrue(c.recorded(plan))

    def test_a_record_with_no_container_left(self):
        c = self.cluster
        spec, plan, _ = self.service(2, [0, 1], name='pair', ranks=())
        result = c.pulsar('stop', spec['spec_id'])
        self.assertEqual(self.shown(result), 'rc=0\n--- stdout\n'
                         'no stack-managed service is running for spec <pair>; nothing to stop\n'
                         'Nothing was stopped. Model files, pins, archives and experiment evidence are unchanged.\n'
                         '--- stderr\n')
        # The spec-wide retire removes the record of a default-order service it found no container of.
        self.assertFalse(c.recorded(plan))
        c.plan(*c.spec(2), [0, 1])
        result = c.pulsar('stop', spec['spec_id'], '--json')
        self.assertEqual(json.loads(result.stdout)['result'],
                         {'completed': True, 'spec_id': spec['spec_id'], 'stopped': False})
        self.assertFalse(c.recorded(plan))

    def test_an_unobservable_node_removes_nothing(self):
        c = self.cluster
        spec, plan, _ = self.service(2, [0, 1], name='pair')
        result = c.pulsar('stop', spec['spec_id'], SERVICE_UNREACHABLE=c.node_id(3))
        self.assertEqual(self.shown(result), 'rc=1\n--- stdout\n--- stderr\n'
                         'error: stop did not run: fixture-ring-alpha cannot be observed, so no container of spec '
                         '<pair:prefix> was removed. Stopping only the reachable nodes could strand a live rank. '
                         'Next: ./pulsar topology check, then rerun ./pulsar stop <pair:prefix>\n')
        self.assertEqual(c.removed(), [])
        self.assertTrue(c.recorded(plan))


class StopAll(Scenario):
    """`./pulsar stop --all --yes`."""

    def test_every_default_order_service(self):
        c = self.cluster
        _, one, _ = self.service(1, [3], name='one')
        _, pair, _ = self.service(2, [0, 1], name='pair')
        _, candidate, _ = self.service(3, [0, 1, 2], catalog=False, name='candidate')
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(self.shown(result), 'rc=0\n--- stdout\n'
                         'removing stack-managed containers across 4 confirmed ranks\n'
                         'removing stack-managed on fixture-ring-alpha id=<one:0:short> spec <one:prefix> rank=single\n'
                         'removing stack-managed on fixture-ring-echo id=<candidate:2:short> spec <candidate:prefix> rank=2\n'
                         'removing stack-managed on fixture-ring-bravo id=<pair:1:short> spec <pair:prefix> rank=1\n'
                         'removing stack-managed on fixture-ring-bravo id=<candidate:1:short> spec <candidate:prefix> rank=1\n'
                         'removing stack-managed id=<pair:0:short> spec <pair:prefix> rank=0 on fixture-ring-dorado\n'
                         'removing stack-managed id=<candidate:0:short> spec <candidate:prefix> rank=0 '
                         'on fixture-ring-dorado\n'
                         'clean — no stack-managed containers remain\n'
                         '--- stderr\n')
        self.assertEqual(c.remaining(), [])
        self.assertEqual([c.recorded(plan) for plan in (one, pair, candidate)], [False] * 3)

    def test_a_container_off_the_default_order_with_no_record_is_refused(self):
        c = self.cluster
        _, pair, _ = self.service(2, [0, 1], name='pair')
        _, _, stray = self.service(2, [2, 3], variant=1, record=False, name='stray')
        result = c.pulsar('stop', '--all', '--yes')
        self.assertEqual(self.shown(result), 'rc=1\n--- stdout\n'
                         'removing stack-managed containers across 4 confirmed ranks\n'
                         'removing stack-managed on fixture-ring-bravo id=<pair:1:short> spec <pair:prefix> rank=1\n'
                         'removing stack-managed id=<pair:0:short> spec <pair:prefix> rank=0 on fixture-ring-dorado\n'
                         '--- stderr\n'
                         'warning: refusing managed candidate id=<stray:1:short> on fixture-ring-alpha: placement mismatch '
                         'on 3: spec <stray:prefix> nodes=2 rank=1\n'
                         'warning: refusing managed candidate id=<stray:0:short> on fixture-ring-echo: placement mismatch '
                         'on 2: spec <stray:prefix> nodes=2 rank=0\n'
                         'warning: some candidates were refused (unknown spec or rank placement)\n')
        self.assertEqual(sorted(item['id'] for item in c.remaining()), sorted(item['id'] for item in stray))
        self.assertTrue(c.recorded(pair))


def summary(document):
    """What inventory says about each service and rank, without its age."""
    return {service['conf'][:12]: {
        'state': service['state'], 'ownership': service['ownership'], 'safe_to_stop': service['safe_to_stop'],
        'complete': service['complete'], 'observability': service['observability'],
        'required_remote_probes': service['required_remote_probes'], 'reasons': service['reasons'],
        'ranks': [[rank['rank'], rank['node'], rank['expected_node'], rank['ownership'], rank['safe_to_stop'],
                   rank['reasons']] for rank in service['ranks']]} for service in document['services']}


CANDIDATE = 'candidate is outside the released catalog; recipe was not verified by this inventory'


def probe(node, rank):
    return {'node': node, 'rank': rank, 'reason': None, 'status': 'ok'}


def running(ranks, *, probes, reasons=(), complete=True):
    return {'complete': complete, 'observability': 'complete' if complete else 'degraded', 'ownership': 'managed',
            'ranks': ranks, 'reasons': list(reasons), 'required_remote_probes': probes, 'safe_to_stop': True,
            'state': 'running'}


class Inventory(Scenario):
    """`./pulsar inventory --json` through the live collection, on the doubles."""

    def inventory(self):
        result = self.cluster.pulsar('inventory', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.named(json.dumps(summary(json.loads(result.stdout)))))

    def test_default_order_services(self):
        self.service(1, [2], name='one')
        self.service(2, [0, 1], name='pair')
        self.service(3, [0, 1, 2], catalog=False, name='candidate')
        self.assertEqual(self.inventory(), {
            '<candidate:prefix>': running(
                [['0', 'head', 'head', 'managed', True, [CANDIDATE]], ['2', 'rank-2', 'rank-2', 'managed', True, []],
                 ['1', 'worker', 'worker', 'managed', True, []]],
                probes=[probe('worker', '1'), probe('rank-2', '2')], reasons=[f'head/rank=0: {CANDIDATE}']),
            '<one:prefix>': running([['single', 'rank-2', 'rank-2', 'managed', True, []]],
                                    probes=[probe('rank-2', 'single')]),
            '<pair:prefix>': running([['0', 'head', 'head', 'managed', True, []],
                                      ['1', 'worker', 'worker', 'managed', True, []]],
                                     probes=[probe('worker', '1')])})

    def test_a_service_off_the_default_order_without_a_record(self):
        self.service(2, [2, 3], record=False, name='pair')
        mismatch = ["rank '0' expected on head, observed on rank-2", "rank '1' expected on worker, observed on rank-3"]
        self.assertEqual(self.inventory(), {'<pair:prefix>': {
            'complete': False, 'observability': 'degraded', 'ownership': 'mismatch',
            'ranks': [['0', 'rank-2', 'head', 'mismatch', False, mismatch[:1]],
                      ['1', 'rank-3', 'worker', 'mismatch', False, mismatch[1:]]],
            'reasons': [f'rank-2/rank=0: {mismatch[0]}', f'rank-3/rank=1: {mismatch[1]}'],
            'required_remote_probes': [probe('worker', '1')], 'safe_to_stop': False, 'state': 'degraded'}})

    def test_a_guarded_trial_off_the_default_order(self):
        self.service(2, [2, 1], catalog=False, guarded=True, name='trial')
        mismatch = "rank '0' expected on head, observed on rank-2"
        self.assertEqual(self.inventory(), {'<trial:prefix>': {
            'complete': False, 'observability': 'degraded', 'ownership': 'mixed',
            'ranks': [['0', 'rank-2', 'head', 'mismatch', False, [mismatch]],
                      ['1', 'worker', 'worker', 'managed', True, [CANDIDATE]]],
            'reasons': [f'worker/rank=1: {CANDIDATE}', f'rank-2/rank=0: {mismatch}'],
            'required_remote_probes': [probe('worker', '1')], 'safe_to_stop': False, 'state': 'degraded'}})


class Pages(Scenario):
    """The Running services card and Home's Serving row, from the live inventory."""

    def pages(self, spec_id):
        pages = self.cluster.pages(spec_id)
        return dict(pages, screen=self.named(pages['screen']), question=self.named(pages['question']))

    CHOICES = [('check', 'Check it now'), ('connect', 'How to connect (a first request to copy)'),
               ('logs', 'Logs'), ('restart', 'Restart…'), ('switch', 'Switch to another model…'),
               ('stop', 'Stop…'), ('stop-all', 'Stop every service…'), ('back', 'Back')]

    def test_a_two_node_default_order_service(self):
        spec, _, _ = self.service(2, [0, 1], name='pair')
        pages = self.pages(spec['spec_id'])
        self.assertEqual(pages['screen'],
                         'example/service-synthetic running on fixture-ring-dorado, fixture-ring-bravo\n'
                         '  · port 8000\n'
                         'Seen      by the service inventory <age>\n'
                         'Model     example/service-synthetic, the model name requests use\n'
                         'API       http://127.0.0.1:8000/v1 · seen, only from fixture-ring-dorado\n'
                         'Key       not recorded: add the key header if the service was started with one\n'
                         'Health    not checked here; choose Check it now\n')
        self.assertEqual(pages['choices'], self.CHOICES)
        self.assertEqual(pages['question'],
                         'Stop and remove the service containers of example/service-synthetic [<pair:prefix>] on '
                         'fixture-ring-dorado, fixture-ring-bravo? Model files, pins and archives are kept.')
        self.assertEqual(pages['home'], [
            'example/service-synthetic running on fixture-ring-dorado, fixture-ring-bravo ·\xa0port\xa08000 '
            '·\xa0seen less than a minute ago; **Running services…** has the details'])

    def test_a_one_node_service_on_another_node(self):
        spec, _, _ = self.service(1, [2], name='one')
        pages = self.pages(spec['spec_id'])
        self.assertEqual(pages['choices'], self.CHOICES)
        self.assertEqual(pages['home'], [
            'example/service-synthetic running on fixture-ring-echo ·\xa0port\xa08000 ·\xa0seen less than a '
            'minute ago; **Running services…** has the details'])


if __name__ == '__main__':
    unittest.main()
