"""What the menus and pages say about a saved ring, under doubles only."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tests'), str(ROOT/'scripts')]
import topology_manifest as tm
from model_library.node_names import NodeNames
from scripts import serve_plan as serve, start_reasons
from tests.test_catalog_menu import inventory, row
from support.topology_fixture import Fixture, as_mesh, ring_probes, shuffled, use_probes
from scripts import cluster_overview, cluster_status
from scripts.terminal_format import TerminalWriter
from tests import test_cluster_overview as overview_sources

REFUSAL = tm.ring_refusal('spark-1', 'spark-3')


def cabling_record():
    """Start's record of a cabling refusal, as scripts/start_blockers.py writes it."""
    from scripts import start_blockers
    return start_blockers.blocker('fabric_incomplete', spec='ab' * 32, detail=REFUSAL, no_command=True,
                                 note='On this cluster no 3 machines are all cabled to each other.')


RING_NAMES = NodeNames({f'n{i}': f'spark-{i + 1}' for i in range(4)}, [f'n{i}' for i in range(4)], 'roce-ring')


class RingRefusalWords(unittest.TestCase):
    """The Serve list, its plan and Why can't I start… use the launch check's words."""

    def test_the_serve_list_says_the_same(self):
        def phrase(nodes, ring):
            state = row(geometry={'nodes': nodes, 'tp': nodes, 'pp': 1})
            return serve.readiness(state, order=RING_NAMES.order, archive_location='disabled', sizes={},
                                   service=None, ring=ring)
        self.assertEqual(phrase(3, True), 'needs every one of its nodes cabled to the others; the cluster is a ring')
        self.assertEqual(phrase(4, True), phrase(3, True))
        self.assertNotIn('ring', phrase(2, True))
        self.assertEqual(phrase(2, True), phrase(2, False))
        self.assertNotIn('ring', phrase(1, True))
        self.assertNotIn('ring', phrase(3, False))

    def test_the_serve_plan_is_blocked_with_the_same_words(self):
        state = row()
        plan = serve.compose(state, [state], names=RING_NAMES, inventory=inventory(), sizes={})
        self.assertEqual(plan['blocked'], [f'Start refuses it: {tm.RING_CABLING}.'])
        self.assertEqual(plan['steps'], [])
        pair = row(geometry={'nodes': 2, 'tp': 2, 'pp': 1})
        self.assertNotIn(f'Start refuses it: {tm.RING_CABLING}.',
                         serve.compose(pair, [pair], names=RING_NAMES, inventory=inventory(), sizes={})['blocked'])

    def test_why_cant_i_start_names_the_cabling_and_offers_no_fix(self):
        document = {'ok': False, 'error': {'code': 'prerequisite_failed', 'message': 'start is blocked',
                    'checks': [{'id': 'topology', 'outcome': 'pass'},
                               {'id': 'fabric', 'outcome': 'fail', 'message': REFUSAL}],
                    'details': [cabling_record()]}}
        text = start_reasons.screen(document, 1, 'Why will example/model not start?', width=80)
        flat = ' '.join(text.split())
        self.assertIn(f'✗ links between nodes: {REFUSAL} ', flat)
        self.assertIn('Blocked: spark-1 and spark-3 are not cabled to each other: the cluster is a ring, and this '
                      'recipe needs every one of its nodes cabled to the others. No menu choice resolves it on this '
                      'cluster.', flat)
        self.assertNotIn('Check the cluster now', flat)
        self.assertEqual(start_reasons.choices(document, 1), [('again', 'Check again'), ('back', 'Back')])


def flat(text):
    return ' '.join(text.split())


class Scenario:
    """A saved cluster on synthetic nodes, what discovery and the check record
    about it, and the screens drawn from those records."""

    def __init__(self, test, saved_probes, live_probes=None, enrolled=True):
        self.fixture = Fixture(Path(test.enterContext(tempfile.TemporaryDirectory())),
                               probes=saved_probes, enrolled=enrolled)
        if live_probes is not None:
            use_probes(self.fixture, live_probes)
        aliases = [alias for alias in json.loads((self.fixture.root/'map.json').read_text()) if alias != 'local']
        self.fixture.env.update(CLUSTER_CANDIDATES=' '.join(aliases), COLUMNS='80')
        self.test = test

    def run(self, *args):
        return self.fixture.run(*args)

    def records(self):
        environ = {key: self.fixture.env[key] for key in ('CLUSTER_TOPOLOGY_FILE', 'PULSAR_CLUSTER_CHANGES_FILE',
                                                          'PULSAR_SERVICE_INVENTORY_FILE', 'PULSAR_STORAGE_BUDGET_FILE')}
        self.test.enterContext(mock.patch.dict(os.environ, environ))
        return cluster_status.Records(self.fixture.root, environ={'PULSAR_COLD_ROOT': ''})

    def detect(self):
        result = self.run('topology', 'detect', '--json')
        return json.loads(result.stdout)

    def check(self):
        return json.loads(self.run('topology', 'check', '--json').stdout)

    def overview(self, width=80):
        directory = Path(self.test.enterContext(tempfile.TemporaryDirectory()))
        overview_sources.write_sources(directory, discovery=(self.detect(), 0, ''), topology=(self.check(), 0, ''))
        out = io.StringIO()
        clean = {key: value for key, value in os.environ.items()
                 if key not in ('PULSAR_COLOR', 'NO_COLOR', 'PULSAR_ACCENT', 'TERM')}
        with mock.patch.dict(os.environ, {**clean, 'TERM': 'dumb'}, clear=True):
            cluster_overview.render(directory, 1_700_000_000, 1_700_000_019, TerminalWriter(width=width, stream=out))
        return out.getvalue()


def healthy_ring(test):
    return Scenario(test, shuffled(ring_probes(4)))


def mesh_became_ring(test):
    ring = ring_probes(4)
    return Scenario(test, as_mesh(ring[:3]), ring)


def dead_cable(test):
    return Scenario(test, shuffled(ring_probes(4)), shuffled(ring_probes(4, dead_cables={1})))


class ClusterFrame(unittest.TestCase):
    def test_the_frame_leads_with_the_count_shape_and_the_loop(self):
        scenario = healthy_ring(self)
        scenario.detect()
        text = cluster_status.frame(scenario.records(), width=80)
        self.assertTrue(text.startswith('Nodes      4 · ring · fixture-ring-dorado → fixture-ring-bravo →\n'
                                        '           fixture-ring-echo → fixture-ring-alpha → fixture-ring-dorado\n'),
                        text)
        self.assertIn('Changes    none noticed', text)

    def test_a_mesh_names_its_shape_too(self):
        scenario = Scenario(self, [__import__('support.topology_fixture', fromlist=['node_probe']).node_probe(rank)
                                   for rank in range(3)])
        scenario.detect()
        self.assertIn('Nodes      3 · full mesh · fixture-rank-0, fixture-rank-1, fixture-rank-2\n',
                      cluster_status.frame(scenario.records(), width=80))


class ReviewWhatChanged(unittest.TestCase):
    def test_a_mesh_that_became_a_ring_can_be_saved(self):
        scenario = mesh_became_ring(self)
        scenario.detect()
        text, choices = cluster_status.review(scenario.records(), width=80)
        words = flat(text)
        self.assertIn('Shape full mesh of 3 → ring of 4; fixture-ring-alpha joins, and the cables close the loop.',
                      words)
        self.assertIn('Ring order fixture-ring-dorado → fixture-ring-bravo → fixture-ring-echo → fixture-ring-alpha '
                      '→ fixture-ring-dorado', words)
        self.assertIn('Cable fixture-ring-dorado and fixture-ring-echo share no link now; the saved cluster has them '
                      'cabled.', words)
        self.assertIn('Cable fixture-ring-alpha and fixture-ring-dorado are cabled now.', words)
        self.assertIn('fixture-ring-alpha new → 3', words)
        self.assertIn('Serving Machines that are not neighbours share no direct link. A ring allows serving on one '
                      'machine, a neighbouring pair or the whole ring', words)
        self.assertEqual(choices[0], ('save', 'Save the cluster as it is now'))
        self.assertNotIn('forget', words.lower())

    def test_a_machine_that_answers_but_no_longer_fits_blocks_the_save_and_says_why(self):
        scenario = dead_cable(self)
        scenario.detect()
        text, choices = cluster_status.review(scenario.records(), width=80)
        words = flat(text)
        self.assertIn('Cable fixture-ring-bravo and fixture-ring-echo share no link now', words)
        self.assertIn('Not usable fixture-ring-echo (position 2) and fixture-ring-alpha (position 3) answered, but '
                      'are not cabled to every other machine in the cluster. Pulsar does not save the cluster without '
                      'a machine that answered: restore the cables and choose Check the cluster now. To set the '
                      'cluster up anew instead, Start over: forget this cluster… under Cluster… removes the saved '
                      'cluster first.', words)
        self.assertEqual(words.count('Not usable'), 1)
        self.assertNotIn('largest directly connected', words)
        self.assertNotIn('save', [action for action, _ in choices])
        self.assertNotIn('drop', [action for action, _ in choices])

    def test_a_changed_cable_order_is_shown_as_the_new_loop(self):
        ring = ring_probes(4)
        recabled = ring_probes(4)
        for key in ('node_id', 'hostname', 'ssh_host', 'control', 'ssh_host_keys'):
            recabled[2][key], recabled[3][key] = recabled[3][key], recabled[2][key]
        scenario = Scenario(self, shuffled(ring), shuffled(recabled))
        scenario.detect()
        words = flat(cluster_status.review(scenario.records(), width=80)[0])
        self.assertIn('Ring order fixture-ring-dorado → fixture-ring-bravo → fixture-ring-alpha → fixture-ring-echo '
                      '→ fixture-ring-dorado', words)
        self.assertIn('Cable fixture-ring-bravo and fixture-ring-echo share no link now', words)
        self.assertIn('Cable fixture-ring-alpha and fixture-ring-bravo are cabled now.', words)
        frame = flat(cluster_status.frame(scenario.records(), width=80))
        self.assertIn('fixture-ring-bravo and fixture-ring-echo share no link now', frame)


class CheckTheClusterNow(unittest.TestCase):
    def test_a_healthy_ring(self):
        text = healthy_ring(self).overview()
        self.assertIn('  Nodes     4 saved · ring · ready\n'
                      '  Order     fixture-ring-dorado → fixture-ring-bravo → fixture-ring-echo →\n'
                      '            fixture-ring-alpha → fixture-ring-dorado\n'
                      '  Serving   Machines that are not neighbours share no direct link. A ring allows\n', text)
        self.assertIn('  Links     verified on every cable between neighbours\n', text)
        self.assertIn('none: every member answered as saved', text)

    def test_a_link_that_does_not_answer_names_its_cable(self):
        scenario = healthy_ring(self)
        nodes = scenario.fixture.topology['nodes']
        rail = next(link for link in scenario.fixture.topology['links'] if link['ranks'] == [1, 2])['rails'][0]
        scenario.fixture.env['TOPOLOGY_PING_FAIL'] = rail['b']['ip']
        words = flat(scenario.overview())
        self.assertIn('Links failed between some nodes', words)
        self.assertIn(f"The cable between {nodes[1]['hostname']} and {nodes[2]['hostname']}: {nodes[2]['hostname']} "
                      f"did not answer at {rail['b']['ip']} on link {rail['network']}, pinged from "
                      f"{nodes[1]['hostname']}'s {rail['a']['netdev']}.", words)


if __name__ == '__main__':
    unittest.main()
