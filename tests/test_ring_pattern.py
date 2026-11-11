"""A multi-node recipe that passes data around a loop of its nodes (the ring
pattern): what declares it, what it needs of the cables, and what the
operator reads. Synthetic fixtures and doubles only; nothing here contacts a
node."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'scripts')]
from release_spec import verify_spec
from scripts import release_consumer

# Recorded from the code before the ring pattern existed: each catalog spec's
# node count and launch requirement (TOPOLOGY_CLASS, MIN_RAILS_PER_PAIR). No
# catalog spec declares the ring pattern, so every multi-node one still needs
# every pair of its nodes cabled.
CATALOG = {
    '139908cf23bbee82400df7eee3ec40e452d96ca223f6577661a7c07f40a6974d': ('1', 'single', '0'),
    '154ea1726af4dd9f4a9e4ef14be4d476d0c7082ff0851b6e5ab19e8921dd8b3f': ('3', 'roce-full-mesh', '2'),
    '1559076fd5af3001369b871b3ce983bf199ac0053c674e119a4a6acabd2ec435': ('2', 'roce-full-mesh', '2'),
    '27dc06de6e33042b164c8a4a2400cd02ec43e642e7ba8e116b905a5e72dd68b2': ('2', 'roce-full-mesh', '2'),
    '6c1066bd2007917b90c586dc56fba5fe0e4b52ca877b3b7e8c11c266a5cd8d9a': ('1', 'single', '0'),
    'b13b30c418f35c812dfbb69de48a01ef82b5545b1de290d2d9a03906164d257b': ('3', 'roce-full-mesh', '2'),
    'e5957c46d433180640165d888f7543ee31cb81dfb725e93b6259a4c83827dc60': ('3', 'roce-full-mesh', '2'),
}


def requirement(spec):
    """The launch requirement the loader derives for SPEC: NODES, TOPOLOGY_CLASS, MIN_RAILS_PER_PAIR."""
    entry = release_consumer.overlay_for_spec(release_consumer.default_overlay(), spec)
    values = release_consumer.spec_shell_values(spec, entry, 'example/image')
    return values['NODES'], values['TOPOLOGY_CLASS'], values['MIN_RAILS_PER_PAIR']


class TheCatalogIsUnchanged(unittest.TestCase):
    def test_every_catalog_spec_keeps_its_spec_id_and_its_launch_requirement(self):
        found = {}
        for path in sorted((ROOT / 'releases').glob('*.json')):
            spec = json.loads(path.read_text())
            with self.subTest(spec=path.stem[:12]):
                self.assertEqual(verify_spec(spec)['spec_id'], path.stem)
                found[path.stem] = requirement(spec)
        self.assertEqual(found, CATALOG)


if __name__ == '__main__':
    unittest.main()
