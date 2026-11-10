"""Saved cluster shape: full-mesh documents pinned byte for byte, rings, and their checks.

Synthetic probes only; nothing here contacts a node.
"""
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tests'), str(ROOT/'scripts')]
from support.topology_fixture import Fixture, node_probe, ring_probes, shuffled
import topology_manifest as tm

GOLDEN = ROOT/'tests/fixtures/topology'
FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_TIME if tz is not None else FIXED_TIME.replace(tzinfo=None)


def frozen_time():
    """generated_at and ssh_trust_enrolled_at at a fixed time, so every field is pinned."""
    return unittest.mock.patch.object(tm, 'datetime', _FrozenDatetime)


def saved_bytes(topology, directory):
    """The bytes `topology_manifest.py write` puts on disk."""
    path = Path(directory)/'saved.json'
    tm.atomic_write(topology, str(path))
    return path.read_bytes()


def masked(data):
    """Saved bytes with each SSH fingerprint replaced, as the golden files keep them.

    The privacy scanner rejects fingerprints in tracked files, even synthetic ones.
    Each is derived from its public key, which stays, and the pinned topology_id
    covers it."""
    return re.sub(rb'SHA256:[A-Za-z0-9+/]{43}', b'SHA256:<derived from public_key>', data)


def write_probes(directory, probes):
    paths = []
    for index, probe in enumerate(probes):
        path = Path(directory)/f'probe-{index}.json'
        path.write_text(json.dumps(probe))
        paths.append(str(path))
    return paths


def mesh_documents(probes, existing=None):
    """Discovery output, the verified membership and the enrolled membership, as saved."""
    with tempfile.TemporaryDirectory() as directory, frozen_time():
        paths = write_probes(directory, probes)
        existing_path = None
        if existing is not None:
            existing_path = str(Path(directory)/'existing.json')
            Path(existing_path).write_text(json.dumps(existing))
        discovery = tm.assemble(paths, existing_path)
        discovery_text = (json.dumps(discovery, indent=2, sort_keys=True) + '\n').encode()
        verified = tm.mark_verified(json.loads(json.dumps(discovery)))['topology']
        members = {node['node_id'] for node in verified['nodes']}
        enrolled = tm.enroll_ssh_trust(verified, [probe for probe in probes if probe['node_id'] in members])
        return discovery_text, saved_bytes(verified, directory), saved_bytes(enrolled, directory)


# Literal identities of the fixture meshes at the commit before ring support.
# The digest covers nodes and links only, so discovery and verified share an id.
MESH_IDS = {
    'mesh-2': {
        'discovery': 'c501310c2bcbf02cd3b71dd15fac335ea7ff35ce2afcd8b1d7a09b7234bb5b9b',
        'verified': 'c501310c2bcbf02cd3b71dd15fac335ea7ff35ce2afcd8b1d7a09b7234bb5b9b',
        'enrolled': 'ff94496874cdf8cf2dd96d9b1d43e326dfad91d7f31a6ae5bbf8d0baa07437e3',
    },
    'mesh-3': {
        'discovery': 'b399280224c4fa8e591049076e634eccd07333f8622b82fecdd3859ca3737781',
        'verified': 'b399280224c4fa8e591049076e634eccd07333f8622b82fecdd3859ca3737781',
        'enrolled': '2f37283fb409b7e9ebc733337908b44e48aef82d43114b1b8970ed61c21d4795',
    },
    'mesh-3-existing-order': {
        'discovery': 'b3120e36181e6a956632c62035d5bfb7762d947f15dfc4dc531f6368d7ff8f8e',
        'verified': 'b3120e36181e6a956632c62035d5bfb7762d947f15dfc4dc531f6368d7ff8f8e',
        'enrolled': 'fdece0c799af58f9c92d2c1cef87669e320b46c25ab5c9f5809591fdb8ee2246',
    },
    'mesh-2-isolated': {
        'discovery': 'c501310c2bcbf02cd3b71dd15fac335ea7ff35ce2afcd8b1d7a09b7234bb5b9b',
        'verified': 'c501310c2bcbf02cd3b71dd15fac335ea7ff35ce2afcd8b1d7a09b7234bb5b9b',
        'enrolled': 'ff94496874cdf8cf2dd96d9b1d43e326dfad91d7f31a6ae5bbf8d0baa07437e3',
    },
}


def isolated_probe(rank):
    """A qualified machine whose only RoCE address shares no network with the fixture mesh."""
    probe = node_probe(rank)
    probe['rdma'] = [dict(hca='roce0', netdev='fabric0', cidrs=[f'203.0.113.{10 + rank}/24'])]
    return probe


def mesh_cases():
    three = [node_probe(rank) for rank in range(3)]
    # A saved membership whose positions 1 and 2 are the other way round:
    # rediscovery keeps that order for a mesh.
    with tempfile.TemporaryDirectory() as directory, frozen_time():
        saved = tm.mark_verified(tm.assemble(write_probes(directory, three), None))['topology']
    for node in saved['nodes']:
        node['rank'] = {0: 0, 1: 2, 2: 1}[node['rank']]
    return {
        'mesh-2': ([node_probe(rank) for rank in range(2)], None),
        'mesh-3': (three, None),
        'mesh-3-existing-order': (three, saved),
        'mesh-2-isolated': ([node_probe(0), node_probe(1), isolated_probe(2)], None),
    }


class MeshDocumentsArePinned(unittest.TestCase):
    """A full mesh validates unchanged and keeps its topology_id, byte for byte."""

    def test_mesh_documents_match_the_pinned_bytes_and_ids(self):
        for name, (probes, existing) in mesh_cases().items():
            with self.subTest(name=name):
                documents = dict(zip(('discovery', 'verified', 'enrolled'), mesh_documents(probes, existing)))
                for form, data in documents.items():
                    golden = GOLDEN/f'{name}.{form}.json'
                    self.assertEqual(masked(data), golden.read_bytes(), f'{golden.name} changed')
                    document = json.loads(data)
                    topology = document.get('topology', document)
                    self.assertEqual(topology['topology_id'], MESH_IDS[name][form])
                    self.assertEqual(topology['topology_id'], tm.topology_digest(topology))
                    tm.validate_manifest(topology)

    def test_probe_order_does_not_change_a_mesh(self):
        three = [node_probe(rank) for rank in range(3)]
        shuffled = [three[0], three[2], three[1]]
        self.assertEqual(mesh_documents(shuffled), mesh_documents(three))


def cabled(probes):
    """Pairs of probe indexes that share at least one link, with the link count."""
    return {(a, b): len(tm.rails_between(probes[a], probes[b]))
            for a in range(len(probes)) for b in range(a + 1, len(probes))
            if tm.rails_between(probes[a], probes[b])}


class RingFixtures(unittest.TestCase):
    """The ring fixtures have the probe shape of a cabled ring."""

    def test_each_node_has_four_adapters_on_four_subnets(self):
        for count in (4, 6):
            probes = ring_probes(count)
            for probe in probes:
                networks = [cidr for link in probe['rdma'] for cidr in link['cidrs']]
                self.assertEqual(len(probe['rdma']), 4)
                self.assertEqual(len(set(tm.rdma_endpoints(probe))), 4)
                self.assertEqual(len(networks), 4)
            subnets = {network for probe in probes for network in tm.rdma_endpoints(probe)}
            self.assertEqual(len(subnets), 2 * count)

    def test_neighbours_share_two_links_and_opposite_nodes_none(self):
        for count in (4, 6):
            expected = {tuple(sorted((node, (node + 1) % count))): 2 for node in range(count)}
            self.assertEqual(cabled(ring_probes(count)), expected)

    def test_broken_rings_and_the_three_node_loop(self):
        self.assertEqual(cabled(ring_probes(4, dead_cables={3})), {(0, 1): 2, (1, 2): 2, (2, 3): 2})
        self.assertEqual(cabled(ring_probes(4, dead_cables={1})), {(0, 1): 2, (2, 3): 2, (0, 3): 2})
        self.assertEqual(cabled(ring_probes(4, single_link_cables={2})), {(0, 1): 2, (1, 2): 2, (2, 3): 1, (0, 3): 2})
        self.assertEqual(cabled(ring_probes(4, extra_links=((0, 2),))),
                         {(0, 1): 2, (1, 2): 2, (2, 3): 2, (0, 3): 2, (0, 2): 1})
        self.assertEqual(cabled(ring_probes(3)), {(0, 1): 2, (1, 2): 2, (0, 2): 2})

    def test_shuffled_probes_are_not_in_cable_or_name_order(self):
        for count in (4, 6):
            probes = ring_probes(count, local=1)
            listed = shuffled(probes)
            self.assertTrue(listed[0]['local'])
            self.assertEqual(sorted(p['node_id'] for p in listed), sorted(p['node_id'] for p in probes))
            positions = [probes.index(probe) for probe in listed]
            self.assertNotEqual(positions[1:], sorted(positions[1:]))
            names = [probe['hostname'] for probe in listed[1:]]
            self.assertNotEqual(names, sorted(names))
            cable_order = [probes[(1 + step) % count]['hostname'] for step in range(1, count)]
            self.assertNotEqual(sorted(names), cable_order)

    def test_addresses_stay_in_documentation_ranges(self):
        probes = ring_probes(6, extra_links=((0, 3),))
        for probe in probes:
            for link in probe['rdma']:
                for cidr in link['cidrs']:
                    self.assertTrue(cidr.startswith(('198.51.100.', '203.0.113.')), cidr)
            self.assertTrue(probe['control']['ip'].startswith('192.0.2.'))


def discover(probes, existing=None):
    """`assemble` over PROBES, listed in the given order."""
    with tempfile.TemporaryDirectory() as directory:
        existing_path = None
        if existing is not None:
            existing_path = str(Path(directory)/'existing.json')
            Path(existing_path).write_text(json.dumps(existing))
        return tm.assemble(write_probes(directory, probes), existing_path)


def saved_ring(count=4, **options):
    """A verified, enrolled ring membership."""
    probes = ring_probes(count, **options)
    verified = tm.mark_verified(discover(shuffled(probes)))['topology']
    return tm.enroll_ssh_trust(verified, probes)


def ring_pairs(count):
    return {(rank, rank + 1) for rank in range(count - 1)} | {(0, count - 1)}


class RingShape(unittest.TestCase):
    """Discovery decides between a full mesh, a ring and today's fallback."""

    def assert_ring(self, document, count, rails=2):
        self.assertEqual(document['result'], 'ok')
        topology = document['topology']
        self.assertEqual(len(topology['nodes']), count)
        self.assertTrue(topology['nodes'][0]['ssh_host'] == 'local')
        self.assertEqual(topology['validation']['class'], 'roce-ring')
        self.assertIs(topology['validation']['full_mesh'], False)
        self.assertEqual(topology['validation']['min_rails_per_pair'], rails)
        self.assertEqual([tuple(link['ranks']) for link in topology['links']], sorted(ring_pairs(count)))
        nodes = topology['nodes']
        for a, b in ring_pairs(count):
            self.assertTrue(tm.rails_between(nodes[a], nodes[b]))
        tm.validate_manifest(topology)
        return topology

    def test_four_and_six_node_rings_are_saved_as_rings(self):
        for count in (4, 6):
            for local in (0, count - 1):
                with self.subTest(count=count, local=local):
                    document = discover(shuffled(ring_probes(count, local=local)))
                    self.assertEqual(document['rejected'], [])
                    topology = self.assert_ring(document, count)
                    self.assertTrue(all(len(link['rails']) == 2 for link in topology['links']))
                    verified = tm.mark_verified(document)['topology']
                    enrolled = tm.enroll_ssh_trust(verified, ring_probes(count, local=local))
                    tm.validate_manifest(enrolled, require_verified=True, require_ssh_trust=True)

    def test_a_cable_with_one_link_keeps_the_ring_and_sets_the_minimum(self):
        topology = self.assert_ring(discover(shuffled(ring_probes(4, single_link_cables={2}))), 4, rails=1)
        self.assertEqual(sorted(len(link['rails']) for link in topology['links']), [1, 2, 2, 2])

    def test_three_nodes_in_a_loop_stay_a_full_mesh(self):
        document = discover(ring_probes(3))
        topology = document['topology']
        self.assertEqual(topology['validation']['class'], 'roce-full-mesh')
        self.assertIs(topology['validation']['full_mesh'], True)
        self.assertEqual([link['ranks'] for link in topology['links']], [[0, 1], [0, 2], [1, 2]])
        self.assertEqual(document['rejected'], [])

    def test_anything_else_falls_back_to_the_largest_mesh_and_names_who_was_left_out(self):
        cases = {
            'open chain': ring_probes(4, dead_cables={3}),
            'dead cable': ring_probes(4, dead_cables={1}),
            'stray link': ring_probes(4, extra_links=((0, 2),)),
            'open chain of six': ring_probes(6, dead_cables={2}),
        }
        for name, probes in cases.items():
            with self.subTest(name):
                document = discover(shuffled(probes))
                topology = document['topology']
                self.assertEqual(topology['validation']['class'], 'roce-full-mesh')
                kept = {node['hostname'] for node in topology['nodes']}
                left_out = {probe['hostname'] for probe in probes} - kept
                self.assertTrue(left_out)
                self.assertEqual({item['hostname'] for item in document['rejected']}, left_out)
                for item in document['rejected']:
                    self.assertEqual(item['reasons'], [tm.MESH_LEFT_OUT, tm.FALLBACK_LEFT_OUT])
                tm.validate_manifest(topology)

    def test_a_machine_cabled_to_no_ring_node_is_left_out_of_the_ring(self):
        stray = isolated_probe(5)
        document = discover([*shuffled(ring_probes(4)), stray])
        self.assert_ring(document, 4)
        self.assertEqual(document['rejected'], [dict(ssh_host='alias-5', hostname='fixture-rank-5',
                                                     node_id='fixture-node-5', reasons=[tm.RING_LEFT_OUT])])

    def test_a_mesh_with_a_machine_cabled_to_no_one_keeps_todays_reason(self):
        document = discover([node_probe(0), node_probe(1), isolated_probe(2)])
        self.assertEqual([item['reasons'] for item in document['rejected']], [[tm.MESH_LEFT_OUT]])


class SavedRingFormat(unittest.TestCase):
    """A saved ring validates; every malformed ring is refused with its reason."""

    def setUp(self):
        self.ring = saved_ring(4)

    def refused(self, change, message):
        topology = json.loads(json.dumps(self.ring))
        change(topology)
        topology['topology_id'] = tm.topology_digest(topology)
        with self.assertRaises(tm.TopologyError) as caught:
            tm.validate_manifest(topology)
        self.assertIn(message, str(caught.exception))

    def link(self, topology, a, b):
        return next(link for link in topology['links'] if link['ranks'] == [a, b])

    def test_four_and_six_node_rings_validate(self):
        for count in (4, 6):
            ring = saved_ring(count)
            tm.validate_manifest(ring, require_verified=True, require_ssh_trust=True)
            self.assertEqual(len(ring['links']), count)
            self.assertEqual(ring['schema_version'], 2)

    def test_a_cable_left_out_is_refused_and_named(self):
        self.refused(lambda t: t['links'].remove(self.link(t, 1, 2)),
                     'a ring of 4 nodes lists 4 cables in links; this one lists 3 and lacks '
                     'the cable between ranks 1 and 2')

    def test_a_missing_closing_cable_is_refused_and_named(self):
        self.refused(lambda t: t['links'].remove(self.link(t, 0, 3)),
                     'lacks the closing cable between ranks 0 and 3')

    def test_a_chord_is_refused(self):
        def chord(topology):
            topology['links'].append(dict(ranks=[0, 2], rails=self.link(topology, 0, 1)['rails']))
        self.refused(chord, 'links[4]: ranks 0 and 2 are not neighbours on the ring')

    def test_ranks_out_of_order_are_refused(self):
        def reverse(topology):
            self.link(topology, 0, 3)['ranks'] = [3, 0]
        self.refused(reverse, 'ranks must list the lower rank first')

    def test_a_rank_outside_the_ring_is_refused(self):
        self.refused(lambda t: self.link(t, 0, 3).update(ranks=[0, 4]), "rank outside the ring's 4 nodes")

    def test_a_cable_with_no_shared_link_is_refused(self):
        self.refused(lambda t: self.link(t, 1, 2).update(rails=[]), 'no shared RoCE rails')

    def test_a_cable_listed_twice_is_refused(self):
        self.refused(lambda t: t['links'].append(json.loads(json.dumps(self.link(t, 1, 2)))),
                     'invalid or duplicate rank pair')

    def test_a_ring_must_not_claim_a_full_mesh(self):
        self.refused(lambda t: t['validation'].update(full_mesh=True), 'validation.full_mesh must be false')

    def test_a_ring_saved_as_a_full_mesh_is_refused(self):
        self.refused(lambda t: t['validation'].update({'class': 'roce-full-mesh', 'full_mesh': True}),
                     'links do not cover every rank pair')

    def test_the_minimum_must_match_the_cables(self):
        self.refused(lambda t: t['validation'].update(min_rails_per_pair=1),
                     'min_rails_per_pair does not match links')

    def test_three_nodes_cannot_be_saved_as_a_ring(self):
        def three(topology):
            topology['nodes'] = topology['nodes'][:3]
            topology['links'] = [self.link(topology, 0, 1), self.link(topology, 1, 2)]
        self.refused(three, 'a ring needs at least 4 nodes; this one has 3')

    def test_an_unknown_class_is_refused(self):
        self.refused(lambda t: t['validation'].update({'class': 'roce-star'}), 'links do not cover every rank pair')


def order_of(document, probes):
    """The cable positions of PROBES, in the discovered rank order."""
    topology = document.get('topology', document)
    positions = {probe['node_id']: index for index, probe in enumerate(probes)}
    return [positions[node['node_id']] for node in topology['nodes']]


def with_port_names_swapped(probe):
    """PROBE with its port 0 and port 1 adapter names exchanged."""
    swap = {'rocep0a': 'rocep1a', 'rocep0b': 'rocep1b', 'rocep1a': 'rocep0a', 'rocep1b': 'rocep0b'}
    probe = json.loads(json.dumps(probe))
    for link in probe['rdma']:
        link['hca'] = swap.get(link['hca'], link['hca'])
    probe['rdma'].sort(key=lambda item: item['hca'])
    return probe


class RingRanks(unittest.TestCase):
    """Ranks follow the cables; a saved ring keeps its direction while its cables match."""

    def test_ranks_follow_the_cables_from_this_node_whatever_the_probe_order(self):
        for count, local, expected in ((4, 0, [0, 1, 2, 3]), (4, 2, [2, 3, 0, 1]),
                                       (6, 4, [4, 5, 0, 1, 2, 3]), (6, 1, [1, 2, 3, 4, 5, 0])):
            with self.subTest(count=count, local=local):
                probes = ring_probes(count, local=local)
                listed = shuffled(probes)
                self.assertEqual(order_of(discover(listed), probes), expected)
                self.assertEqual(order_of(discover([listed[0], *reversed(listed[1:])]), probes), expected)

    def test_discovery_through_the_command_saves_cable_order(self):
        with tempfile.TemporaryDirectory() as directory:
            probes = ring_probes(4)
            fixture = Fixture(Path(directory), probes=shuffled(probes))
            fixture.path.unlink()
            candidates = [argument for probe in shuffled(probes)[1:] for argument in ('--candidate', probe['ssh_host'])]
            result = fixture.run('topology', 'detect', *candidates, '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
            self.assertEqual(document['topology']['validation']['class'], 'roce-ring')
            self.assertEqual(order_of(document, probes), [0, 1, 2, 3])

    def test_rank_1_is_on_the_cable_of_the_first_adapter_by_name(self):
        # This node's port 1 adapters now sort first, so rank 1 is the node
        # cabled to port 1: the loop is walked the other way.
        probes = ring_probes(4)
        probes[0] = with_port_names_swapped(probes[0])
        self.assertEqual(order_of(discover(shuffled(probes)), probes), [0, 3, 2, 1])

    def test_an_adapter_that_carries_no_link_is_passed_over(self):
        probes = ring_probes(4)
        probes[0] = with_port_names_swapped(probes[0])
        # An adapter that sorts before every other one, on a subnet no other node has.
        probes[0]['rdma'].insert(0, dict(hca='rocep00', netdev='fab00', cidrs=['203.0.113.241/28']))
        self.assertEqual(order_of(discover(shuffled(probes)), probes), [0, 3, 2, 1])

    def test_a_saved_ring_keeps_its_direction_on_rediscovery(self):
        reversed_probes = ring_probes(4)
        reversed_probes[0] = with_port_names_swapped(reversed_probes[0])
        saved = tm.mark_verified(discover(shuffled(reversed_probes)))['topology']
        self.assertEqual(order_of(saved, reversed_probes), [0, 3, 2, 1])
        # Without the saved ring, the same cables would be walked the other way.
        probes = ring_probes(4)
        self.assertEqual(order_of(discover(shuffled(probes)), probes), [0, 1, 2, 3])
        self.assertEqual(order_of(discover(shuffled(probes), saved), probes), [0, 3, 2, 1])
        # A cable that lost one of its links is still the same cable.
        fewer = ring_probes(4, single_link_cables={0, 2})
        self.assertEqual(order_of(discover(shuffled(fewer), saved), fewer), [0, 3, 2, 1])
        enrolled = tm.enroll_ssh_trust(saved, reversed_probes)
        self.assertEqual(order_of(discover(shuffled(probes), {'topology': enrolled}), probes), [0, 3, 2, 1])

    def test_a_saved_ring_whose_cables_changed_takes_the_rule_again(self):
        reversed_probes = ring_probes(4)
        reversed_probes[0] = with_port_names_swapped(reversed_probes[0])
        saved = tm.mark_verified(discover(shuffled(reversed_probes)))['topology']
        # The machines at positions 2 and 3 exchange places in the loop.
        recabled = ring_probes(4)
        identity = ('node_id', 'hostname', 'ssh_host', 'control', 'ssh_host_keys')
        for key in identity:
            recabled[2][key], recabled[3][key] = recabled[3][key], recabled[2][key]
        document = discover(shuffled(recabled), saved)
        self.assertEqual(order_of(document, recabled), [0, 1, 2, 3])
        self.assertEqual([node['hostname'] for node in document['topology']['nodes']],
                         ['fixture-ring-dorado', 'fixture-ring-bravo', 'fixture-ring-alpha', 'fixture-ring-echo'])

    def test_a_saved_mesh_does_not_set_a_rings_direction(self):
        probes = ring_probes(4)
        saved = {'validation': {'class': 'roce-full-mesh'},
                 'nodes': [{'node_id': probes[index]['node_id'], 'rank': rank}
                           for rank, index in enumerate((0, 3, 2, 1))]}
        self.assertEqual(order_of(discover(shuffled(probes), saved), probes), [0, 1, 2, 3])


if __name__ == '__main__':
    unittest.main()
