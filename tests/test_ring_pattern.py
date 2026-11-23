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
from support.topology_fixture import Fixture, ring_probes, shuffled
import topology_manifest as tm
from tests import test_ring_launch_check as launch, test_start_blockers
from tests.test_catalog_menu import inventory, row as catalog_row
from model_library.node_names import NodeNames
from scripts import serve_plan as serve, start_reasons

# Recorded from the code before the ring pattern existed: each catalog spec's
# node count and launch requirement (TOPOLOGY_CLASS, MIN_RAILS_PER_PAIR). These
# specs do not declare the ring pattern, so every multi-node one still needs
# every pair of its nodes cabled. The MiniMax-M3.1-Flash spec (949f8d18…),
# added later, is the first catalog spec that declares it; the four-node
# DeepSeek-V4.1-Flash-1017 spec (027160dd…) is the second.
CATALOG = {
    '027160dde72321a97215eccac326144e273b9bfc8601c719abf365b2e6f2d419': ('4', 'roce-ring', '2'),
    '139908cf23bbee82400df7eee3ec40e452d96ca223f6577661a7c07f40a6974d': ('1', 'single', '0'),
    '154ea1726af4dd9f4a9e4ef14be4d476d0c7082ff0851b6e5ab19e8921dd8b3f': ('3', 'roce-full-mesh', '2'),
    '1559076fd5af3001369b871b3ce983bf199ac0053c674e119a4a6acabd2ec435': ('2', 'roce-full-mesh', '2'),
    '27dc06de6e33042b164c8a4a2400cd02ec43e642e7ba8e116b905a5e72dd68b2': ('2', 'roce-full-mesh', '2'),
    '369d30ce8d6ba5f5ab5f0503c4db423f8cb8ccc28c6950546cb6d15a6c27b0a5': ('1', 'single', '0'),
    '3f30050631c3b605cef34763f8def7a5e79a64ea7e9f047eaaeff71ed4873857': ('1', 'single', '0'),
    '6c1066bd2007917b90c586dc56fba5fe0e4b52ca877b3b7e8c11c266a5cd8d9a': ('1', 'single', '0'),
    '949f8d185c94b3ba15a78ad9e6151a22d9b8b70ef20d036c5f18ef38cd9a1d8f': ('4', 'roce-ring', '2'),
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


def gate(fixture, *placements, topology_class='roce-ring', min_rails=2):
    """The launch check, sourced alone, for each PLACEMENT: saved positions in
    role order, or a node count for the default order. Returns
    [(status, refusal kind, stderr)]."""
    script = r'''
        . "$1/cluster/topology.sh"; shift; topology_class="$1" min_rails="$2"; shift 2
        errors=$(mktemp); trap 'rm -f "$errors"' EXIT
        for placement in "$@"; do
          SERVING_NODE_INDEXES=()
          case "$placement" in *,*) IFS=, read -r -a SERVING_NODE_INDEXES <<<"$placement" ;; esac
          count="${#SERVING_NODE_INDEXES[@]}"; [ "$count" -gt 0 ] || count="$placement"
          require_profile_topology "$count" "$topology_class" "$min_rails" 2>"$errors" >/dev/null; status=$?
          error=$(cat "$errors")
          printf '%s\t%s\t%s\n' "$status" "$CLUSTER_TOPOLOGY_REFUSAL" "${error//$'\n'/|}"
        done
    '''
    result = subprocess.run(['bash', '-c', script, 'gate', str(ROOT), topology_class, str(min_rails),
                             *map(str, placements)], env=fixture.env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [tuple(line.split('\t', 2)) for line in result.stdout.splitlines()]


def ring_fixture(test, count):
    fixture = Fixture(Path(test.enterContext(tempfile.TemporaryDirectory())), probes=shuffled(ring_probes(count)))
    return fixture, [node['hostname'] for node in fixture.topology['nodes']]


class TheLaunchCheckForARingPattern(unittest.TestCase):
    """A recipe that passes data around a loop needs each of its nodes cabled to
    the next in role order, and the last to the first; nothing else."""

    def refused(self, names, first, second):
        return ('1', 'cabling', f'topology: {tm.ring_loop_refusal(names[first], names[second])}')

    def test_a_ring_of_four(self):
        fixture, names = ring_fixture(self, 4)
        passing = [4, '0,1,2,3', '1,2,3,0', '3,2,1,0', 2, '1,2', '3,0', '0,3']
        self.assertEqual(gate(fixture, *passing), [('0', '', '')] * len(passing))
        self.assertEqual(gate(fixture, 3, '1,2,3', '0,2,1,3', '0,1,3,2', '0,2'), [
            # Fewer nodes than the ring: the loop does not close.
            self.refused(names, 2, 0), self.refused(names, 3, 1),
            # Out of cable order: the first pair in role order that is not cabled.
            self.refused(names, 0, 2), self.refused(names, 1, 3),
            # Two nodes that are not neighbours.
            self.refused(names, 0, 2)])
        self.assertEqual(tm.ring_loop_refusal('alpha', 'bravo'), 'alpha and bravo are not cabled to each other: this '
                         'recipe passes data around a closed loop of its nodes, each cabled to the next and the last '
                         'to the first')

    def test_a_ring_of_six(self):
        fixture, names = ring_fixture(self, 6)
        self.assertEqual(gate(fixture, 6, '0,1,2,3,4,5', '5,0,1,2,3,4', '4,5'), [('0', '', '')] * 4)
        self.assertEqual(gate(fixture, 4, 5, '0,1,2,4,3,5'), [
            self.refused(names, 3, 0), self.refused(names, 4, 0), self.refused(names, 2, 4)])

    def test_a_cable_with_too_few_links_names_the_pair_and_both_counts(self):
        fixture = Fixture(Path(self.enterContext(tempfile.TemporaryDirectory())),
                          probes=shuffled(ring_probes(4, single_link_cables={0})))
        names = [node['hostname'] for node in fixture.topology['nodes']]
        pair = sorted((names.index('fixture-ring-dorado'), names.index('fixture-ring-bravo')))
        (status, kind, error), = gate(fixture, 4)
        self.assertEqual((status, kind), ('1', 'links'))
        self.assertRegex(error, r'^topology: fixture-ring-\w+ and fixture-ring-\w+ share 1 link on their cable; '
                                r'this recipe needs 2$')
        self.assertEqual(sorted(names.index(name) for name in error.split(': ', 1)[1].split(' share')[0]
                                .split(' and ')), pair)
        self.assertEqual(gate(fixture, 4, min_rails=1), [('0', '', '')])

    def test_a_full_mesh_passes_since_every_pair_is_cabled(self):
        fixture = Fixture(Path(self.enterContext(tempfile.TemporaryDirectory())), nodes=3)
        self.assertEqual(gate(fixture, 2, 3, '2,0,1', min_rails=1), [('0', '', '')] * 3)
        # The fixture mesh has one link per pair: too few, in the mesh's own words.
        self.assertEqual(gate(fixture, 3), [
            ('1', 'links', 'topology: ranks 0/1 expose 1 shared RoCE rail(s); the spec requires 2')])

    def test_a_recipe_without_the_declaration_is_judged_as_before(self):
        fixture, names = ring_fixture(self, 4)
        self.assertEqual(gate(fixture, 2, 4, '0,2', topology_class='roce-full-mesh'), [
            ('0', '', ''),
            ('1', 'cabling', f'topology: {tm.ring_refusal(names[0], names[2])}'),
            ('1', 'cabling', f'topology: {tm.ring_refusal(names[0], names[2])}')])


class FromTheSpecToTheLaunchCheck(unittest.TestCase):
    """load_conf of a spec file, then the real launch check with what it set, on a saved ring of four."""

    def check(self, fixture, spec):
        spec_file = fixture.root / f"{spec['spec_id']}.json"
        spec_file.write_text(json.dumps(spec))
        script = r'''
            . "$1/scripts/lib.sh"
            load_conf "$2" || exit 9
            require_profile_topology "$NODES" "$TOPOLOGY_CLASS" "$MIN_RAILS_PER_PAIR" 2>"$3"; status=$?
            printf '%s\t%s\t%s\n' "$status" "$TOPOLOGY_CLASS" "$(cat "$3")"
        '''
        result = subprocess.run(['bash', '-c', script, 'loaded', str(ROOT), spec['spec_id'],
                                 str(fixture.root / 'gate.err')],
                                env={**fixture.env, 'PULSAR_SPEC_FILE': str(spec_file),
                                     'PULSAR_OVERLAY_PATH': str(fixture.root / 'no-overlay.json')},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return tuple(result.stdout.rstrip('\n').split('\t', 2))

    def test_the_declaration_reaches_the_launch_check(self):
        fixture, names = ring_fixture(self, 4)
        self.assertEqual(self.check(fixture, ring_recipe.recipe(4)), ('0', 'roce-ring', ''))
        self.assertEqual(self.check(fixture, ring_recipe.recipe(3)),
                         ('1', 'roce-ring', f'topology: {tm.ring_loop_refusal(names[2], names[0])}'))
        self.assertEqual(self.check(fixture, ring_recipe.recipe(4, env=())),
                         ('1', 'roce-full-mesh', f'topology: {tm.ring_refusal(names[0], names[2])}'))


class AdaptersAndPreflightForTheWholeRing(unittest.TestCase):
    """Each rank of a ring-pattern recipe on the whole ring gets the adapters on
    both of its cables; preflight pings and judges only the loop's pairs."""

    def test_each_rank_gets_the_adapters_on_both_of_its_cables(self):
        for count in (4, 6):
            fixture, _names = ring_fixture(self, count)
            with self.subTest(count=count):
                self.assertEqual(launch.profile(fixture.topology, *range(count)),
                                 {rank: launch.PORT0 | launch.PORT1 for rank in range(count)})

    def test_the_launch_check_gives_them_to_each_rank(self):
        fixture, _names = ring_fixture(self, 4)
        script = r'''
            . "$1/cluster/topology.sh"
            require_profile_topology 4 roce-ring 2 || exit 9
            for rank in 0 1 2 3; do printf '%s\n' "${CLUSTER_PROFILE_HCAS[$rank]}"; done
        '''
        result = subprocess.run(['bash', '-c', script, 'fabric', str(ROOT)], env=fixture.env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([set(line.split(',')) for line in result.stdout.split()], [launch.PORT0 | launch.PORT1] * 4)

    def test_the_rdma_state_check_covers_all_four_adapters(self):
        selected = ','.join(sorted(launch.PORT0 | launch.PORT1))
        active = launch.listing(*((hca, 'ACTIVE') for hca in sorted(launch.PORT0 | launch.PORT1)))
        self.assertEqual(launch.adapters_active(selected, active), ['4', '4', ''])
        one_down = launch.listing(('rocep0a', 'ACTIVE'), ('rocep0b', 'ACTIVE'), ('rocep1a', 'DOWN'), ('rocep1b', 'ACTIVE'))
        self.assertEqual(launch.adapters_active(selected, one_down), ['3', '4', 'rocep1a'])

    def ping_pairs(self, fixture, count, loop):
        script = r'''
            . "$1/scripts/lib.sh"; . "$1/scripts/topology-probes.sh"
            load_cluster_topology || exit 9
            topology_ping_links "$CLUSTER_TOPOLOGY_FILE" "$2" "$3"
        '''
        result = subprocess.run(['bash', '-c', script, 'ping', str(ROOT), str(count), loop], env=fixture.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [line.split('\t') for line in result.stdout.splitlines()]
        return ([tuple(row[1:3]) for row in rows if row[0] == 'pair'],
                {frozenset(row[1:3]) for row in rows if row[0] != 'pair'})

    def test_preflight_pings_the_loops_pairs_only(self):
        loop = [('0', '1'), ('0', '3'), ('1', '2'), ('2', '3')]
        fixture, _names = ring_fixture(self, 4)
        # On a ring the loop's pairs are its cables, which are all its links.
        self.assertEqual(self.ping_pairs(fixture, 4, 'loop')[0], loop)
        self.assertEqual(self.ping_pairs(fixture, 4, '')[0], loop)
        mesh = Fixture(Path(self.enterContext(tempfile.TemporaryDirectory())), nodes=4)
        pairs, pinged = self.ping_pairs(mesh, 4, 'loop')
        self.assertEqual(pairs, loop)
        self.assertEqual(pinged, {frozenset(pair) for pair in loop})
        # Without the declaration a mesh still pings and judges every pair.
        self.assertEqual(len(self.ping_pairs(mesh, 4, '')[0]), 6)
        # Two nodes are their own loop.
        self.assertEqual(self.ping_pairs(mesh, 2, 'loop')[0], [('0', '1')])

    def test_preflight_asks_for_the_loop_for_a_ring_pattern_and_for_a_saved_ring_without_a_recipe(self):
        text = (ROOT / 'cluster/preflight.sh').read_text()
        self.assertIn('topology_ping_links "$CLUSTER_TOPOLOGY_FILE" "$EXPECTED_NODES" '
                      '"$([ "$TOPOLOGY_CLASS" != roce-ring ] || echo loop)"', text)
        self.assertIn('[ "$CLUSTER_TOPOLOGY_CLASS" != roce-ring ] || TOPOLOGY_CLASS=roce-ring', text)


class TheLaunchPlanForFourRanks(unittest.TestCase):
    """The launch plan and each rank's container for the synthetic four-node
    recipe on the whole ring: nothing beyond the launch check limits it to
    three ranks. Plan and arguments only; nothing is started."""

    def test_four_ranks_in_the_default_order(self):
        from scripts import container_runtime as runtime
        fixture, _names = ring_fixture(self, 4)
        topology = fixture.topology
        spec = ring_recipe.recipe(4)
        adapters = launch.profile(topology, *range(4))
        nodes = topology['nodes']
        commit = spec['recipe']['model']['model_commit']
        facts = dict(port=8000, served_name='synthetic', topology_id=topology['topology_id'], ranks=[
            dict(rank=i, node_id=nodes[i]['node_id'], hostname=nodes[i]['hostname'], ssh_host=nodes[i]['ssh_host'],
                 control_ip=nodes[i]['control']['ip'], control_if=nodes[i]['control']['interface'],
                 hcas=','.join(sorted(adapters[i]))) for i in range(4)])
        prepared = dict(schema_version=1, kind='pulsar-prepared-set', spec_id=spec['spec_id'],
                        topology_id=topology['topology_id'],
                        snapshot_manifest_id=spec['recipe']['model']['snapshot_manifest']['manifest_id'],
                        revision=commit, home_node_id=nodes[0]['node_id'],
                        ranks=[dict(rank=i, node_id=nodes[i]['node_id'], hub_path=f'/var/tmp/fixture-rank-{i}',
                                    path=f'/var/tmp/fixture-rank-{i}/snapshots/{commit}') for i in range(4)])
        with mock.patch.dict(os.environ, {'API_KEY': '', 'VLLM_API_KEY': ''}):
            plan = runtime.build_plan(spec, spec['spec_id'], facts, prepared)
        runtime.validate_plan(plan)
        for rank in range(4):
            with self.subTest(rank=rank):
                argv = runtime.docker_argv(plan, rank, include_secrets=False)
                self.assertEqual(argv[argv.index('--nnodes') + 1], '4')
                self.assertEqual(argv[argv.index('--node-rank') + 1], str(rank))
                self.assertEqual(argv[argv.index('--tensor-parallel-size') + 1], '4')
                environment = runtime.environment(plan, rank)
                self.assertEqual(environment['NCCL_ALGO'], 'Ring')
                self.assertEqual(set(environment['NCCL_IB_HCA'].split(',')), launch.PORT0 | launch.PORT1)
                for setting in ring_recipe.RING_ENV:
                    self.assertIn(setting, [*argv, *(f'{key}={value}' for key, value in environment.items())])


class StartRefusesARingPatternThatDoesNotFit(unittest.TestCase):
    """Start records a broken loop as a cabling refusal with no command, as for
    any recipe whose nodes are not cabled as it needs."""
    setUp = test_start_blockers.StartScenarios.setUp
    start = test_start_blockers.StartScenarios.start

    def test_three_of_four_nodes_leave_the_loop_open(self):
        refusal = tm.ring_loop_refusal('fixture-ring-alpha', 'fixture-ring-dorado')
        result, codes = self.start(nodes=3, topology=4, topology_class='roce-ring', fabric=refusal,
                                   extra_env={'FIXTURE_REFUSAL': 'cabling'})
        self.assertEqual(codes, ['fabric_incomplete'])
        self.assertIn(f'FAIL  fabric    {refusal}', result.stdout)
        self.assertIn(f'({refusal})', self.records[0]['message'])
        self.assertIsNone(self.records[0]['fix'])
        self.assertEqual(self.records[0]['note'],
                         'On this ring no 3 machines form a closed loop of cables; only all 4 do, in cable order.')

    def test_the_whole_ring_out_of_cable_order(self):
        refusal = tm.ring_loop_refusal('fixture-ring-dorado', 'fixture-ring-echo')
        # The Start fixture has three node slots: as many recipe nodes as saved ones.
        result, codes = self.start(nodes=3, topology=3, topology_class='roce-ring', fabric=refusal,
                                   extra_env={'FIXTURE_REFUSAL': 'cabling'})
        self.assertEqual(codes, ['fabric_incomplete'], result.stdout + result.stderr)
        self.assertIsNone(self.records[0]['fix'])
        self.assertEqual(self.records[0]['note'], "Serve it on the ring's machines in cable order, the default order.")

    def test_a_recipe_without_the_declaration_keeps_its_note(self):
        refusal = tm.ring_refusal('fixture-ring-dorado', 'fixture-ring-echo')
        self.start(nodes=3, topology=4, fabric=refusal, extra_env={'FIXTURE_REFUSAL': 'cabling'})
        self.assertEqual(self.records[0]['note'], 'On this cluster no 3 machines are all cabled to each other.')


RING_NAMES = NodeNames({f'n{i}': f'spark-{i + 1}' for i in range(4)}, [f'n{i}' for i in range(4)], 'roce-ring')
MESH_NAMES = NodeNames({f'n{i}': f'spark-{i + 1}' for i in range(4)}, [f'n{i}' for i in range(4)], 'roce-full-mesh')


def loop_row(nodes, env=ring_recipe.RING_ENV):
    return catalog_row(geometry={'nodes': nodes, 'tp': nodes, 'pp': 1}, container_env=sorted(env))


class WhatTheOperatorReads(unittest.TestCase):
    """The Serve list, its plan and Why will … not start? for a ring-pattern recipe."""

    def phrase(self, state, ring=True):
        return serve.readiness(state, order=RING_NAMES.order, archive_location='disabled', sizes={}, service=None,
                               ring=ring)

    def test_the_serve_list_reads_as_usual_when_it_fits_the_ring(self):
        ordinary = self.phrase(loop_row(4), ring=False)
        self.assertEqual(self.phrase(loop_row(4)), ordinary)
        self.assertNotRegex(ordinary, 'ring|loop|cabled')
        self.assertEqual(self.phrase(loop_row(2)), self.phrase(loop_row(2), ring=False))

    def test_the_serve_list_says_why_it_does_not_fit(self):
        self.assertEqual(self.phrase(loop_row(3)), 'needs 3 nodes cabled in a closed loop; the ring has 4 nodes')
        # On a full mesh every pair is cabled.
        self.assertNotRegex(self.phrase(loop_row(3), ring=False), 'ring|loop|cabled')
        # Without the declaration: as after the launch check judged the chosen nodes.
        self.assertEqual(self.phrase(loop_row(4, env=())),
                         'needs every one of its nodes cabled to the others; the cluster is a ring')
        self.assertEqual(self.phrase(loop_row(4, env=('NCCL_ALGO=Ring,Tree',))),
                         'needs every one of its nodes cabled to the others; the cluster is a ring')

    def test_the_serve_plan(self):
        whole = loop_row(4)
        plan = serve.compose(whole, [whole], names=RING_NAMES, inventory=inventory(), sizes={})
        self.assertFalse([line for line in plan['blocked'] if 'cabled' in line], plan['blocked'])
        three = loop_row(3)
        plan = serve.compose(three, [three], names=RING_NAMES, inventory=inventory(), sizes={})
        self.assertEqual(plan['blocked'], [f'Start refuses it: {tm.RING_LOOP}; on this ring only all 4 nodes form '
                                           'one.'])
        self.assertEqual(plan['steps'], [])
        plain = loop_row(3, env=())
        self.assertEqual(serve.compose(plain, [plain], names=RING_NAMES, inventory=inventory(), sizes={})['blocked'],
                         [f'Start refuses it: {tm.RING_CABLING}.'])
        mesh = serve.compose(three, [three], names=MESH_NAMES, inventory=inventory(), sizes={})
        self.assertFalse([line for line in mesh['blocked'] if 'cabled' in line], mesh['blocked'])

    def test_the_catalog_row_carries_the_recipe_s_container_env(self):
        from model_library.catalog import project
        from model_library.state import Store
        spec = ring_recipe.recipe(4)
        state = project(spec, Store(Path(self.enterContext(tempfile.TemporaryDirectory()))))
        self.assertEqual(state['container_env'], spec['recipe']['container_env'])
        self.assertTrue(serve.ring_pattern(state))

    def test_why_will_it_not_start_gives_the_check_s_reason(self):
        text = start_reasons.screen(why_document(), 1, 'Why will example/ring-synthetic not start?', width=80)
        words = ' '.join(text.split())
        self.assertIn(f'Blocked: {LOOP_REFUSAL}. No menu choice resolves it on this cluster.', words)
        self.assertNotIn('Check the cluster now', words)
        self.assertEqual(start_reasons.choices(why_document(), 1), [('again', 'Check again'), ('back', 'Back')])


LOOP_REFUSAL = tm.ring_loop_refusal('spark-3', 'spark-1')


def why_document():
    """The dry run's answer for the ring-pattern recipe on three of four nodes."""
    from scripts import start_blockers
    record = start_blockers.blocker('fabric_incomplete', spec='ab' * 32, detail=LOOP_REFUSAL, no_command=True,
                                    note='On this ring no 3 machines form a closed loop of cables; only all 4 do, '
                                         'in cable order.')
    return {'ok': False, 'error': {'code': 'prerequisite_failed', 'message': 'start is blocked',
                                   'checks': [{'id': 'topology', 'outcome': 'pass'},
                                              {'id': 'fabric', 'outcome': 'fail', 'message': LOOP_REFUSAL}],
                                   'details': [record]}}


if __name__ == '__main__':
    unittest.main()
