"""A multi-node recipe that passes data around a loop of its nodes (the ring
pattern): what declares it, what it needs of the cables, and what the
operator reads. Synthetic fixtures and doubles only; nothing here contacts a
node."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'scripts')]
from release_spec import verify_spec
from scripts import release_consumer
from support import ring_recipe

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


# Each container_env and whether it declares the ring pattern: exactly one
# NCCL_ALGO entry whose whole value is Ring, in any case.
VARIANTS = [
    (['NCCL_ALGO=Ring'], True), (['NCCL_ALGO=ring'], True), (['NCCL_ALGO=RING'], True),
    (['NCCL_ALGO=Ring', 'NCCL_NET_PLUGIN=none'], True),
    (['NCCL_ALGO=Tree'], False), (['NCCL_ALGO=Ring,Tree'], False), (['NCCL_ALGO=Tree,Ring'], False),
    (['NCCL_ALGO=^Tree'], False), (['NCCL_ALGO=^Ring'], False), (['NCCL_ALGO=allreduce:ring'], False),
    (['NCCL_ALGO=allreduce:ring;broadcast:ring'], False), (['NCCL_ALGO= Ring'], False), (['NCCL_ALGO=Ring '], False),
    (['NCCL_ALGO='], False), (['NCCL_ALGO'], False), (['NCCL_ALGO=Ring', 'NCCL_ALGO=Ring'], False),
    (['NCCL_ALGO=Ring', 'NCCL_ALGO=Tree'], False), (['nccl_algo=Ring'], False), (['NCCL_ALGOS=Ring'], False),
    (['XNCCL_ALGO=Ring'], False), (['NCCL_PROTO=Ring'], False), ([], False),
]


def shell_declares(env):
    """recipe_ring_pattern from scripts/lib.sh for CONTAINER_ENV=ENV."""
    script = '. "$1/scripts/lib.sh"; shift; CONTAINER_ENV=("$@"); recipe_ring_pattern && echo yes || echo no'
    result = subprocess.run(['bash', '-c', script, 'ring', str(ROOT), *env], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip() == 'yes'


class TheDeclaration(unittest.TestCase):
    def test_every_variant_in_the_loader_and_in_load_conf(self):
        for env, expected in VARIANTS:
            with self.subTest(env=env):
                self.assertIs(release_consumer.ring_pattern(env), expected)
                self.assertIs(shell_declares(env), expected)

    def test_a_frozen_recipe_cannot_repeat_the_key(self):
        with self.assertRaises(Exception) as caught:
            ring_recipe.recipe(env=('NCCL_ALGO=Ring', 'NCCL_ALGO=ring'))
        self.assertIn('more than once', str(caught.exception))


class TheRequirement(unittest.TestCase):
    def test_a_multi_node_recipe_that_declares_it_needs_the_ring_of_its_nodes(self):
        self.assertEqual(requirement(ring_recipe.recipe(4)), ('4', 'roce-ring', '2'))
        self.assertEqual(requirement(ring_recipe.recipe(2)), ('2', 'roce-ring', '2'))
        self.assertEqual(requirement(ring_recipe.recipe(1, env=('NCCL_ALGO=Ring',))), ('1', 'single', '0'))

    def test_every_other_value_keeps_the_every_pair_requirement(self):
        for env, expected in VARIANTS:
            if expected or len({item.split('=', 1)[0] for item in env}) < len(env) or any('=' not in item for item in env):
                continue
            with self.subTest(env=env):
                self.assertEqual(requirement(ring_recipe.recipe(4, env=env)), ('4', 'roce-full-mesh', '2'))

    def test_the_same_key_in_the_engine_arguments_does_not_declare_it(self):
        spec = ring_recipe.recipe(4, env=())
        spec['recipe']['engine_args'] = [*spec['recipe']['engine_args'], '--env', 'NCCL_ALGO=Ring']
        self.assertEqual(requirement(spec), ('4', 'roce-full-mesh', '2'))

    def test_the_requirement_follows_the_effective_spec_of_an_override(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ring, plain = ring_recipe.recipe(4), ring_recipe.recipe(4, env=())
        for selected, env, expected in ((ring, [e for e in ring['recipe']['container_env'] if e != 'NCCL_ALGO=Ring']
                                         + ['NCCL_ALGO=Tree'], 'roce-full-mesh'),
                                        (plain, sorted([*plain['recipe']['container_env'], 'NCCL_ALGO=Ring']),
                                         'roce-ring')):
            with self.subTest(expected=expected):
                spec_file, override = root / f'{expected}.json', root / f'{expected}-override.json'
                spec_file.write_text(json.dumps(selected))
                override.write_text(json.dumps({'container_env': sorted(env)}))
                output = io.StringIO()
                with mock.patch.dict(os.environ, {'PULSAR_OVERRIDE_FILE': str(override)}), \
                        contextlib.redirect_stdout(output):
                    release_consumer.cmd_shell_values(ROOT, selected['spec_id'], spec_file=spec_file,
                                                      overlay_path=root / 'no-overlay.json')
                values = dict(line.split('=', 1) for line in output.getvalue().splitlines() if '=' in line)
                self.assertEqual(values['TOPOLOGY_CLASS'].strip("'"), expected)
                # An override that changes container_env is a distinct effective spec.
                self.assertNotEqual(values['PULSAR_EFFECTIVE_SPEC_ID'].strip("'"), selected['spec_id'])
                self.assertEqual(values['CONF_NAME'].strip("'"), selected['spec_id'])

    def test_load_conf_accepts_only_the_requirement_the_declaration_gives(self):
        script = r'''
            . "$1/scripts/lib.sh"; shift
            NODES=4 PROFILE_PURPOSE=serving CONF_NAME=synthetic MIN_RAILS_PER_PAIR=2
            ENGINE_ARGS=(--tensor-parallel-size 4 --pipeline-parallel-size 1 --distributed-executor-backend mp)
            TOPOLOGY_CLASS="$1"; shift; CONTAINER_ENV=("$@")
            ( validate_profile_contract ) && echo valid
        '''
        def check(topology_class, *env):
            return subprocess.run(['bash', '-c', script, 'contract', str(ROOT), topology_class, *env],
                                  capture_output=True, text=True, timeout=60)
        self.assertIn('valid', check('roce-ring', 'NCCL_ALGO=Ring').stdout)
        self.assertIn('valid', check('roce-full-mesh', 'NCCL_ALGO=Tree').stdout)
        refused = check('roce-full-mesh', 'NCCL_ALGO=ring')
        self.assertNotIn('valid', refused.stdout)
        self.assertIn('container_env sets NCCL_ALGO=Ring requires TOPOLOGY_CLASS=roce-ring', refused.stderr)
        refused = check('roce-ring', 'NCCL_ALGO=Ring,Tree')
        self.assertNotIn('valid', refused.stdout)
        self.assertIn('multi-node spec requires TOPOLOGY_CLASS=roce-full-mesh', refused.stderr)


if __name__ == '__main__':
    unittest.main()
