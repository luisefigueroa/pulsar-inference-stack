"""What the menus and pages say about a saved ring, under doubles only."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tests'), str(ROOT/'scripts')]
import topology_manifest as tm
from model_library.node_names import NodeNames
from scripts import serve_plan as serve, start_reasons
from tests.test_catalog_menu import inventory, row

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
        self.assertEqual(phrase(2, True), 'starts on two nodes only on a full mesh; the cluster is a ring')
        self.assertNotIn('ring', phrase(1, True))
        self.assertNotIn('ring', phrase(3, False))

    def test_the_serve_plan_is_blocked_with_the_same_words(self):
        state = row()
        plan = serve.compose(state, [state], names=RING_NAMES, inventory=inventory(), sizes={})
        self.assertEqual(plan['blocked'], [f'Start refuses it: {tm.ring_refusal(3)}.'])
        self.assertEqual(plan['steps'], [])

    def test_why_cant_i_start_names_the_cabling_and_offers_no_fix(self):
        document = {'ok': False, 'error': {'code': 'prerequisite_failed', 'message': 'start is blocked',
                    'checks': [{'id': 'topology', 'outcome': 'pass'},
                               {'id': 'fabric', 'outcome': 'fail', 'message': tm.ring_refusal(3)}],
                    'details': [{'blocker': 'fabric_incomplete', 'detail': tm.ring_refusal(3)}]}}
        text = start_reasons.screen(document, 1, 'Why will example/model not start?', width=80)
        flat = ' '.join(text.split())
        self.assertIn(f'✗ links between nodes: {tm.ring_refusal(3)} ', flat)
        self.assertIn('Blocked: The cluster is a ring, and this recipe needs every one of its nodes cabled to the '
                      'others. No menu choice resolves it on this cluster.', flat)
        self.assertNotIn('Check the cluster now', flat)
        self.assertEqual(start_reasons.choices(document, 1), [('again', 'Check again'), ('back', 'Back')])


if __name__ == '__main__':
    unittest.main()
