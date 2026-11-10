"""Every screen this ring work changed, rendered at 80 and 60 columns under
doubles: plain text through the renderers and commands, and the Cluster menu
and Review what changed… with the bundled Gum. Nothing here contacts a node."""
import json
import os
from pathlib import Path
import platform
import re
import shlex
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'scripts')]
from model_library.catalog import entries as catalog_entries
from model_library.state import Store
from scripts import cluster_status, serve_plan, start_reasons
from scripts import topology_manifest as tm
from tests import test_ring_pages as pages
from tests import test_ring_readers as readers
from tests import test_ui_confirm
from tests.test_ring_saved_readers import ring_names

WIDTHS = (80, 60)
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def flat(text):
    return ' '.join(text.split())


def why_document():
    checks = [{'id': 'recipe', 'outcome': 'pass'}, {'id': 'topology', 'outcome': 'pass'},
              {'id': 'fabric', 'outcome': 'fail', 'message': pages.REFUSAL}]
    checks += [{'id': name, 'outcome': 'not_run'} for name in ('image', 'service', 'model-files', 'memory', 'port',
                                                               'preflight', 'plan')]
    return {'ok': False, 'error': {'code': 'prerequisite_failed', 'message': 'start is blocked', 'checks': checks,
                                   'details': [pages.cabling_record()]}}


class Renders(unittest.TestCase):
    """Each changed screen fits 80 and 60 columns and says the same at both."""

    def fits(self, text, width, *wanted):
        self.assertTrue(all(len(line) <= width for line in text.splitlines()),
                        '\n'.join(line for line in text.splitlines() if len(line) > width))
        for words in wanted:
            self.assertIn(words, flat(text))

    def test_the_discovery_report(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                text = readers.discovery(readers.unsaved(self, readers.ring_probes(4)), columns=str(width)).stdout
                self.fits(text, width, 'Found 4 GB10 machines answered · ring · 2 links on every cable',
                          'Order fixture-ring-dorado → fixture-ring-bravo → fixture-ring-echo → fixture-ring-alpha '
                          '→ fixture-ring-dorado', 'Also seen 6 more addresses answered for machines already listed')
                text = readers.discovery(readers.unsaved(self, readers.ring_probes(4, dead_cables={1})),
                                         columns=str(width)).stdout
                self.fits(text, width, 'Found 4 GB10 machines answered · 2 in the cluster · full mesh',
                          'Left out fixture-ring-bravo answered, but is not cabled to every other machine')

    def test_topology_check_with_a_link_that_does_not_answer(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                fixture = readers.ring_fixture(self)
                rail = next(link for link in fixture.topology['links'] if link['ranks'] == [1, 2])['rails'][0]
                fixture.env.update(TOPOLOGY_PING_FAIL=rail['b']['ip'], COLUMNS=str(width))
                text = fixture.run('topology', 'check').stdout
                self.fits(text, width, 'The cable between fixture-ring-bravo and fixture-ring-echo')

    def test_check_the_cluster_now(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                self.fits(pages.healthy_ring(self).overview(width), width, 'Nodes 4 saved · ring · ready',
                          'Links verified on every cable between neighbours')
                scenario = pages.healthy_ring(self)
                rail = next(link for link in scenario.fixture.topology['links'] if link['ranks'] == [1, 2])['rails'][0]
                scenario.fixture.env['TOPOLOGY_PING_FAIL'] = rail['b']['ip']
                self.fits(scenario.overview(width), width, 'The cable between fixture-ring-bravo and fixture-ring-echo')

    def test_review_what_changed_and_the_cluster_frame(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                scenario = pages.mesh_became_ring(self)
                scenario.detect()
                records = scenario.records()
                self.fits(cluster_status.review(records, width=width)[0], width,
                          'Shape full mesh of 3 → ring of 4; fixture-ring-alpha joins')
                self.fits(cluster_status.frame(records, width=width), width, 'cluster shape changed: full mesh → ring')
                scenario = pages.dead_cable(self)
                scenario.detect()
                self.fits(cluster_status.review(scenario.records(), width=width)[0], width,
                          'Start over: forget this cluster…')

    def test_why_will_it_not_start_and_the_serve_list(self):
        fixture = readers.ring_fixture(self)
        names = ring_names(fixture)
        entries = catalog_entries(ROOT, Store(Path(self.enterContext(tempfile.TemporaryDirectory()))))
        for width in WIDTHS:
            with self.subTest(width=width):
                self.fits(start_reasons.screen(why_document(), 1, 'Why will example/model not start?', width=width),
                          width, 'Blocked: spark-1 and spark-3 are not cabled to each other: the cluster is a ring')
                lines = serve_plan.list_lines(entries, width=width - 2, inventory=None, archive_location='disabled',
                                              order=names.order, repo=ROOT, ring=True)
                self.fits('\n'.join(line.split('\t')[-1] for line in lines), width, serve_plan.READINESS['ring'])


@unittest.skipUnless(platform.system() == 'Linux' and platform.machine() == 'aarch64',
                     'the bundled Gum requires Linux arm64')
class BundledGumRing(test_ui_confirm.BundledGum):
    """The Cluster menu and Review what changed… with real Gum over a ring's records; Esc only."""

    def exports(self, scenario):
        fixture = scenario.fixture
        values = {'PULSAR_FORCE_MENU': '1', 'NO_COLOR': '1', 'CLUSTER_TOPOLOGY_FILE': str(fixture.path),
                  'PULSAR_CLUSTER_CHANGES_FILE': str(fixture.root / 'state/cluster-changes.json'),
                  'PULSAR_SERVICE_INVENTORY_FILE': str(fixture.root / 'state/service-inventory.json'),
                  'PULSAR_STORAGE_BUDGET_FILE': str(fixture.root / 'state/storage-budget.json'),
                  'PULSAR_MODEL_LIBRARY_DIR': str(fixture.root / 'model-library'), 'PULSAR_COLD_ROOT': ''}
        return ''.join(f'export {key}={shlex.quote(value)}\n' for key, value in values.items())

    def test_the_cluster_menu_draws_a_rings_frame(self):
        for columns in WIDTHS:
            with self.subTest(columns=columns):
                scenario = pages.healthy_ring(self)
                scenario.detect()
                code, output = self.drive(self.exports(scenario) + 'exec "$REPO_DIR/scripts/cluster-menu.sh"\n',
                                          b'\x1b', ready=b'Start over: forget this cluster', columns=columns)
                self.assertEqual(code, 0, output)
                plain = flat(ANSI.sub('', output))
                for text in ('Nodes 4 · ring · fixture-ring-dorado →', 'Check the cluster now', 'Back'):
                    self.assertIn(text, plain)

    def test_review_what_changed_draws_a_mesh_that_became_a_ring(self):
        for columns in WIDTHS:
            with self.subTest(columns=columns):
                scenario = pages.mesh_became_ring(self)
                scenario.detect()
                code, output = self.drive(self.exports(scenario) + 'exec "$REPO_DIR/scripts/cluster-review.sh"\n',
                                          b'\x1b', ready=b'Save the cluster as it is now', columns=columns,
                                          rows=60)
                self.assertEqual(code, 0, output)
                plain = flat(ANSI.sub('', output))
                for text in ('Shape full mesh of 3 → ring of 4', 'Ring order', 'Save the cluster as it is now'):
                    self.assertIn(text, plain)


if __name__ == '__main__':
    unittest.main()
