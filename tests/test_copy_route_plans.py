"""Plans of copies between nodes that share no cable: the plan names the nodes
a copy passes through, the copy runs through them, and a route that cannot be
proven blocks the plan before anything starts. Synthetic nodes, files and
doubles only; nothing here contacts a node or runs real SSH, rsync or ip."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'tests/support'), str(ROOT / 'scripts')]
from model_library_fixture import Fixture as LibraryFixture
from model_library.integrity import StorageError
from model_library.node_names import NodeNames
from model_library.planning import with_copy_route, with_copy_routes
from model_library.render import copy_route_lines, render
from model_library.transfer import NoCopyRoute, copy_legs
from scripts.terminal_format import TerminalWriter
from tests.test_copy_route import ring_topology
from tests.test_ring_renumbering import save_ring


def through(legs, hostnames):
    """The sentence a copy's log prints for each leg of a plan's route."""
    lines = []
    for leg in legs:
        names = [hostnames[node] for node in leg['through']]
        joined = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]
        lines.append(f"Copies {leg['copies']} {hostnames[leg['node_id']]} pass through {joined}.")
    return lines


class TheLegsOfACopy(unittest.TestCase):
    """copy_legs: the legs a copy runs from this machine, with the nodes between."""

    def setUp(self):
        self.ring = ring_topology(self)
        self.ids = [node['node_id'] for node in self.ring['nodes']]

    def test_a_neighbour_and_this_machine_have_no_leg_with_a_node_between(self):
        self.assertEqual(copy_legs(self.ring, 0, 1), [])
        self.assertEqual(copy_legs(self.ring, 3, 0), [])

    def test_the_opposite_node_passes_through_the_lower_neighbour(self):
        self.assertEqual(copy_legs(self.ring, 0, 2), [{'copies': 'to', 'node_id': self.ids[2], 'through': [self.ids[1]]}])
        self.assertEqual(copy_legs(self.ring, 2, 0), [{'copies': 'from', 'node_id': self.ids[2], 'through': [self.ids[1]]}])

    def test_a_copy_between_two_other_nodes_runs_two_legs_from_this_machine(self):
        self.assertEqual(copy_legs(self.ring, 1, 3), [])
        self.assertEqual(copy_legs(self.ring, 2, 3), [{'copies': 'from', 'node_id': self.ids[2], 'through': [self.ids[1]]}])
        self.assertEqual(copy_legs(self.ring, 3, 2), [{'copies': 'to', 'node_id': self.ids[2], 'through': [self.ids[1]]}])

    def test_no_chain_of_cables_is_a_refusal_that_names_both_ends(self):
        broken = {**self.ring, 'links': [link for link in self.ring['links'] if link['ranks'] not in ([1, 2], [2, 3])]}
        with self.assertRaises(NoCopyRoute) as caught:
            copy_legs(broken, 0, 2)
        hostnames = [node['hostname'] for node in self.ring['nodes']]
        self.assertEqual(str(caught.exception), f'no chain of cables joins {hostnames[0]} and {hostnames[2]}')
        self.assertIsInstance(caught.exception, StorageError)


PLAN = {'kind': 'pulsar-preparation-plan', 'eligible': True, 'blockers': [], 'blocker_details': [],
        'actions': [{'rank': 0, 'node_id': 'n0', 'action': 'home-view'},
                    {'rank': 1, 'node_id': 'n2', 'action': 'copy'}]}
LEG = {'copies': 'to', 'node_id': 'n2', 'through': ['n1']}


class PlanFields(unittest.TestCase):
    def test_a_route_with_a_node_between_is_named_on_its_copy(self):
        plan = with_copy_routes(PLAN, [{'slot': 1, 'passes_through': [LEG], 'refusal': ''}])
        self.assertEqual(plan['actions'][1], {'rank': 1, 'node_id': 'n2', 'action': 'copy', 'passes_through': [LEG]})
        self.assertTrue(plan['eligible'])

    def test_a_copy_with_no_node_between_is_unchanged(self):
        self.assertEqual(with_copy_routes(PLAN, [{'slot': 1, 'passes_through': [], 'refusal': ''}]), PLAN)
        move = {'kind': 'pulsar-home-move-plan', 'transfer_route': 'direct-ssh-roce'}
        self.assertEqual(with_copy_route(move, 'n1', []), move)

    def test_a_route_that_cannot_be_proven_blocks_the_plan_and_says_why(self):
        reason = 'the copy route is not proven: SSH to e through b did not connect: refused'
        plan = with_copy_routes(PLAN, [{'slot': 1, 'passes_through': [LEG], 'refusal': reason}])
        self.assertFalse(plan['eligible'])
        self.assertEqual(plan['blockers'], [f'n2: {reason}'])
        self.assertEqual([(d['code'], d['node_id']) for d in plan['blocker_details']], [('copy-route', 'n2')])
        move = with_copy_route({'kind': 'pulsar-home-move-plan'}, 'n2', [LEG], reason)
        self.assertEqual((move['eligible'], move['blockers'], move['passes_through']), (False, [f'n2: {reason}'], [LEG]))

    def test_only_a_copy_has_a_route(self):
        with self.assertRaises(StorageError):
            with_copy_routes(PLAN, [{'slot': 0, 'passes_through': [LEG]}])

    def test_the_sentence_names_every_node_between_in_order(self):
        names = NodeNames({'n2': 'echo', 'n1': 'bravo', 'n3': 'charlie'})
        self.assertEqual(copy_route_lines({'passes_through': [LEG]}, names), ['copies to echo pass through bravo'])
        self.assertEqual(copy_route_lines({'passes_through': [{**LEG, 'copies': 'from', 'through': ['n1', 'n3']}]}, names),
                         ['copies from echo pass through bravo and charlie'])
        self.assertEqual(copy_route_lines(PLAN, names), [])


class PlanAndCopyAgree(unittest.TestCase):
    """The opposite node of a four-node ring, for a one-node recipe whose home
    is on this machine: Prepare's plan and its copy name the same route."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.f = LibraryFixture(self.root, 4)
        save_ring(self.f, [0, 1, 2, 3])
        self.assertEqual(self.f.acquire(0).returncode, 0)
        self.f.candidate(1)
        self.ids = [node['node_id'] for node in self.f.topology['nodes']]
        self.hostnames = {node['node_id']: node['hostname'] for node in self.f.topology['nodes']}
        self.opposite = self.ids[2]

    def prepare(self, *args):
        return self.f.run('prepare', '--node', self.opposite, *args, spec=True)

    def test_the_plan_names_the_route_and_the_copy_runs_through_it(self):
        result = self.prepare('--plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertTrue(plan['eligible'], plan['blockers'])
        (action,) = plan['actions']
        self.assertEqual(action['action'], 'copy')
        self.assertEqual(action['passes_through'], [{'copies': 'to', 'node_id': self.opposite, 'through': [self.ids[1]]}])
        # The plan proved the route without copying: no stage, no transfer.
        self.assertEqual(self.f.events('transfer'), [])
        self.assertNotIn('begin-view', [row['operation'] for row in self.f.events('node-operation')])
        result = self.prepare('--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        # The copy's log names the machines the plan named, in the same words.
        for line in through(action['passes_through'], self.hostnames):
            self.assertIn(line, result.stderr)
        transfers = self.f.events('transfer')
        self.assertTrue(transfers)
        for transfer in transfers:
            self.assertTrue(transfer['destination'].startswith(str(self.root / 'rank-2')), transfer)
        # The proof's one short command through the jump reached the opposite node.
        self.assertIn(2, [row['target_rank'] for row in self.f.events('relay')])

    def test_a_route_that_cannot_be_proven_blocks_the_plan_and_nothing_starts(self):
        self.f.cfg['wrong_route'] = True
        self.f.save()
        result = self.prepare('--plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertFalse(plan['eligible'])
        (detail,) = plan['blocker_details']
        self.assertEqual((detail['code'], detail['node_id']), ('copy-route', self.opposite))
        self.assertIn(f"the copy route is not proven: on {self.hostnames[self.ids[0]]}, "
                      f"{self.hostnames[self.ids[1]]}'s address", detail['message'])
        result = self.prepare('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.f.events('transfer'), [])
        self.assertNotIn('begin-view', [row['operation'] for row in self.f.events('node-operation')])

    def test_a_home_move_archive_and_restore_name_the_route_and_copy_through_it(self):
        leg = {'node_id': self.opposite, 'through': [self.ids[1]]}
        move = json.loads(self.f.run('move', '--node', self.opposite, '--plan', spec=True).stdout)
        self.assertEqual(move['passes_through'], [{'copies': 'to', **leg}])
        self.assertEqual(move['transfer_route'], 'direct-ssh-roce')
        self.assertNotIn('eligible', move)
        result = self.f.run('move', '--node', self.opposite, '--yes', spec=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(through(move['passes_through'], self.hostnames)[0], result.stderr)
        # The home is on the opposite node now: archiving it copies from there.
        archive = json.loads(self.f.run('archive', 'create', '--plan', spec=True).stdout)
        self.assertEqual(archive['passes_through'], [{'copies': 'from', **leg}])
        result = self.f.run('archive', 'create', '--yes', spec=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(through(archive['passes_through'], self.hostnames)[0], result.stderr)
        # With the home removed, restoring the archive to the opposite node copies through the neighbour.
        result = self.f.run('remove', '--yes', spec=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        restore = json.loads(self.f.run('restore', '--node', self.opposite, '--plan', spec=True).stdout)
        self.assertEqual(restore['passes_through'], [{'copies': 'to', **leg}])
        result = self.f.run('restore', '--node', self.opposite, '--yes', spec=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(through(restore['passes_through'], self.hostnames)[0], result.stderr)


class Renders(unittest.TestCase):
    """The Prepare plan and a move plan with a route, at 80 and 60 columns."""

    PREPARE = {'kind': 'pulsar-preparation-plan', 'spec_id': 'ab' * 32, 'snapshot_manifest_id': 'cd' * 32,
               'topology_id': 'ef' * 32, 'eligible': True, 'blockers': [], 'blocker_details': [],
               'actions': [{'rank': 0, 'node_id': 'node-2', 'action': 'copy',
                            'passes_through': [{'copies': 'to', 'node_id': 'node-2', 'through': ['node-1']}]}],
               'total_bytes': 17, 'file_count': 2,
               'budgets': {'node-2': {'available': 10 * 1024**3, 'reserve': 1024**3, 'used': 0, 'limit': 8 * 1024**3}}}
    NAMES = NodeNames({'node-0': 'fixture-ring-dorado', 'node-1': 'fixture-ring-bravo',
                       'node-2': 'fixture-ring-echo', 'node-3': 'fixture-ring-alpha'})

    def page(self, document, operation, width):
        stream = io.StringIO()
        render(document, operation=operation, writer=TerminalWriter(width=width, stream=stream), names=self.NAMES)
        return stream.getvalue()

    def test_the_prepare_plan_names_the_route_under_its_copy(self):
        self.assertIn('Rank 0    fixture-ring-echo: copy and verify local files\n'
                      '  Route   copies to fixture-ring-echo pass through fixture-ring-bravo\n',
                      self.page(self.PREPARE, 'prepare', 80))
        for width in (80, 60):
            text = self.page(self.PREPARE, 'prepare', width)
            # A narrow terminal wraps the route under its own value column.
            self.assertIn('Route copies to fixture-ring-echo pass through fixture-ring-bravo Storage',
                          ' '.join(text.split()))
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
            self.dump(f'prepare-{width}', text)

    def test_a_blocked_route_says_why(self):
        reason = ("the copy route is not proven: SSH to fixture-ring-echo through fixture-ring-bravo did not "
                  "connect: kex_exchange_identification: Connection closed by remote host. fixture-ring-bravo's "
                  "SSH server must allow it to forward the connection to 198.51.100.6 (AllowTcpForwarding)")
        plan = with_copy_routes(self.PREPARE, [{'slot': 0, 'refusal': reason,
                                                'passes_through': self.PREPARE['actions'][0]['passes_through']}])
        for width in (80, 60):
            text = self.page(plan, 'prepare', width)
            self.assertIn('Result    blocked', text)
            self.assertIn('AllowTcpForwarding', ' '.join(text.split()))
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
            self.dump(f'prepare-blocked-{width}', text)

    def test_a_move_through_a_neighbour_is_not_called_direct(self):
        move = {'kind': 'pulsar-home-move-plan', 'snapshot_manifest_id': 'cd' * 32, 'source_node': 'node-0',
                'destination_node': 'node-2', 'transfer_route': 'direct-ssh-roce',
                'passes_through': [{'copies': 'to', 'node_id': 'node-2', 'through': ['node-1']}]}
        for width in (80, 60):
            text = self.page(move, 'move', width)
            self.assertNotIn('Direct copy', text)
            self.assertIn('copies to fixture-ring-echo pass through fixture-ring-bravo', ' '.join(text.split()))
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
            self.dump(f'move-{width}', text)
        direct = {key: value for key, value in move.items() if key != 'passes_through'}
        self.assertIn('Direct copy over the confirmed RoCE rail', self.page(direct, 'move', 80))

    def dump(self, name, text):
        target = os.environ.get('COPY_ROUTE_RENDERS')
        if not target:
            return
        target = Path(target).resolve()
        self.assertFalse(target.is_relative_to(ROOT), 'write renders outside the checkout')
        target.mkdir(parents=True, exist_ok=True)
        (target / f'{name}.txt').write_text(text)


if __name__ == '__main__':
    unittest.main()
