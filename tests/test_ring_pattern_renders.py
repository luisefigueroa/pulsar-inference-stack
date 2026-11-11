"""The screens the ring pattern changed, at 80, 60 and 44 columns, for a
synthetic four-node recipe that passes data around a loop (tensor
parallelism 4, NCCL_ALGO=Ring) and the same recipe on three nodes, in a
fixture catalog written to a temporary folder. Doubles only; nothing here
contacts a node."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'scripts')]
from model_library.catalog import entries as catalog_entries
from model_library.state import Store
from scripts import serve_plan, start_reasons
from scripts import topology_manifest as tm
from support import ring_recipe
from tests import test_ring_pattern as pattern, test_ring_readers as readers, test_start_blockers
from tests.test_catalog_menu import inventory
from tests.test_ring_saved_readers import ring_names

WIDTHS = (80, 60, 44)


def flat(text):
    return ' '.join(text.split())


class Renders(unittest.TestCase):
    def setUp(self):
        # The fixture catalog: never under the checkout's releases/.
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.repo / 'releases').mkdir()
        self.whole, self.three = ring_recipe.recipe(4), ring_recipe.recipe(3)
        for spec in (self.whole, self.three):
            (self.repo / 'releases' / f"{spec['spec_id']}.json").write_text(json.dumps(spec))
        self.entries = catalog_entries(self.repo, Store(Path(self.enterContext(tempfile.TemporaryDirectory()))))
        self.names = ring_names(readers.ring_fixture(self))
        self.renders = {}

    def tearDown(self):
        target = os.environ.get('RING_PATTERN_RENDERS')
        if target and self.renders:
            target = Path(target).resolve()
            self.assertFalse(target.is_relative_to(ROOT), 'write renders outside the checkout')
            target.mkdir(parents=True, exist_ok=True)
            for name, text in self.renders.items():
                (target / f'{name}.txt').write_text(text)

    def fits(self, name, text, width, *wanted):
        self.renders[f'{name}-{width}'] = text
        self.assertTrue(all(len(line) <= width for line in text.splitlines()),
                        '\n'.join(line for line in text.splitlines() if len(line) > width))
        for words in wanted:
            self.assertIn(words, flat(text))

    def row(self, spec):
        return next(entry for entry in self.entries if entry['spec_id'] == spec['spec_id'])

    def test_the_serve_list(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                lines = serve_plan.list_lines(self.entries, width=width - 2, inventory=None,
                                              archive_location='disabled', order=self.names.order, repo=self.repo,
                                              ring=True)
                text = '\n'.join(line.split('\t')[-1] for line in lines if line != 'gap') + '\n'
                self.fits('serve-list', text, width, '4 nodes · needs downloading',
                          'needs 3 nodes cabled in a closed loop; the ring has 4 nodes')

    def test_the_serve_plan(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                whole = serve_plan.compose(self.row(self.whole), self.entries, names=self.names,
                                           inventory=inventory(), sizes={})
                self.assertFalse([line for line in whole['blocked'] if 'cabled' in line])
                self.fits('serve-plan-whole-ring', serve_plan.render(whole, width=width), width,
                          'fixture-ring-dorado, fixture-ring-bravo, fixture-ring-echo and fixture-ring-alpha')
                three = serve_plan.compose(self.row(self.three), self.entries, names=self.names,
                                           inventory=inventory(), sizes={})
                self.fits('serve-plan-three-nodes', serve_plan.render(three, width=width), width,
                          f'Start refuses it: {tm.RING_LOOP}; on this ring only all 4 nodes form one.')

    def test_why_will_it_not_start(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                text = start_reasons.screen(pattern.why_document(), 1,
                                            'Why will example/ring-synthetic not start?', width=width)
                self.fits('why-three-nodes', text, width,
                          f'Blocked: {pattern.LOOP_REFUSAL}. No menu choice resolves it on this cluster.')

    def test_start_s_refusal(self):
        scenario = test_start_blockers.StartScenarios()
        scenario.setUp()
        self.addCleanup(scenario.doCleanups)
        refusal = tm.ring_loop_refusal('fixture-ring-echo', 'fixture-ring-dorado')
        result, codes = scenario.start(nodes=3, topology=4, topology_class='roce-ring', fabric=refusal,
                                       extra_env={'FIXTURE_REFUSAL': 'cabling'})
        self.assertEqual(codes, ['fabric_incomplete'])
        record = scenario.records[0]
        # Start's own lines are not wrapped to the terminal, at any width, as before.
        text = result.stdout + result.stderr + json.dumps(record, indent=1) + '\n'
        self.renders['start-three-nodes'] = text
        self.assertIn(f'FAIL  fabric    {refusal}', text)
        self.assertIn('Note: On this ring no 3 machines form a closed loop of cables; only all 4 do, in cable order.',
                      text)
        self.assertIsNone(record['fix'])


if __name__ == '__main__':
    unittest.main()
