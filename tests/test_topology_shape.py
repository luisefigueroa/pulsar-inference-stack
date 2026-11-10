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
from support.topology_fixture import node_probe
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


if __name__ == '__main__':
    unittest.main()
