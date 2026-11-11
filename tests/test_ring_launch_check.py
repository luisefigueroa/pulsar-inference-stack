"""The launch check on the chosen nodes: what the recipe's link requirement
reaches, and how each caller judges a placement on a ring and on a full mesh.
Synthetic fixtures and doubles only; nothing here contacts a node."""
import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'scripts')]
from support.topology_fixture import Fixture, ring_probes, shuffled
import topology_manifest as tm

# The recipe's link requirement: TOPOLOGY_CLASS and MIN_RAILS_PER_PAIR in shell,
# topology_class and min_rails_per_pair in the unused profile-contract builder.
REQUIREMENT = re.compile(r'TOPOLOGY_CLASS|MIN_RAILS_PER_PAIR')


def sources():
    for path in sorted([*ROOT.glob('scripts/*'), *ROOT.glob('cluster/*'), *ROOT.glob('model_library/*.py'),
                        *ROOT.glob('release_spec/*.py'), ROOT / 'serve.sh', ROOT / 'pulsar']):
        if path.is_file() and path.suffix in ('', '.sh', '.py'):
            yield path, path.read_text(encoding='utf-8', errors='replace')


class TheRequirementSteersOnlyTheGate(unittest.TestCase):
    """TOPOLOGY_CLASS and MIN_RAILS_PER_PAIR come from the recipe's node count
    (scripts/release_consumer.py), are checked by load_conf, and are passed to
    the launch check. They reach no container label, launch plan, service plan,
    saved record or hash, so changing what the check does with them changes no
    spec_id, plan_id or ownership proof."""

    def test_only_the_loader_and_the_launch_checks_name_it(self):
        named = {str(path.relative_to(ROOT)) for path, text in sources() if REQUIREMENT.search(text)}
        self.assertEqual(named, {
            'scripts/release_consumer.py',  # sets both from the node count
            'scripts/lib.sh',  # load_conf defaults and validate_profile_contract; the placement check
            'cluster/topology.sh',  # the launch check itself
            'scripts/up.sh', 'cluster/start-cluster.sh', 'cluster/preflight.sh', 'scripts/resources.sh',
            'scripts/guard-storage.sh', 'scripts/check-weights.sh', 'scripts/observe-serving.sh',
        })

    def test_no_label_plan_or_record_carries_it(self):
        for path, text in sources():
            if path.name in ('model_identity.py', 'topology_manifest.py', 'cluster_status.py',
                             'cluster_overview.py', 'release_consumer.py'):
                continue
            with self.subTest(path=path.name):
                self.assertNotRegex(text, r'topology_class|min_rails_per_pair|roce-full-mesh'
                                    if path.suffix == '.py' else r'io\.pulsar\.[a-z0-9.]*(class|rails)')

    def test_the_ring_pattern_requirement_reaches_nothing_else_either(self):
        # roce-ring also names a saved membership's shape; these files read that shape.
        shape_readers = {'topology_manifest.py', 'node_names.py', 'release_consumer.py'}
        for path, text in sources():
            if path.suffix == '.py' and path.name not in shape_readers:
                with self.subTest(path=path.name):
                    self.assertNotIn('roce-ring', text)
        # Only the loader reads the declaration for the launch; it reaches the rest as the
        # requirement alone. The Serve list reads it too, for its words only.
        readers = {str(path.relative_to(ROOT)) for path, text in sources()
                   if re.search(r'\bring_pattern\b|RING_PATTERN_KEY|recipe_ring_pattern', text)}
        self.assertEqual(readers, {'scripts/release_consumer.py', 'scripts/lib.sh', 'scripts/serve_plan.py'})

    def test_the_profile_contract_builder_that_names_it_has_no_caller(self):
        callers = [path.name for path, text in sources()
                   if re.search(r'append_loaded_profile_contract_args|build_profile_contract', text)]
        self.assertEqual(sorted(callers), ['lib.sh', 'model_identity.py'])
        lib = (ROOT / 'scripts/lib.sh').read_text()
        self.assertEqual(lib.count('append_loaded_profile_contract_args'), 1, 'defined, never called')


def gate(fixture, *placements, min_rails=2):
    """The launch check, sourced alone, for each PLACEMENT: saved positions in rank
    order, or a node count for the default order. Returns [(status, stderr)]."""
    script = r'''
        . "$1/cluster/topology.sh"; shift; min_rails="$1"; shift
        for placement in "$@"; do
          SERVING_NODE_INDEXES=()
          case "$placement" in *,*) IFS=, read -r -a SERVING_NODE_INDEXES <<<"$placement" ;; esac
          count="${#SERVING_NODE_INDEXES[@]}"; [ "$count" -gt 0 ] || count="$placement"
          error=$(require_profile_topology "$count" roce-full-mesh "$min_rails" 2>&1 >/dev/null); status=$?
          printf '%s\t%s\n' "$status" "${error//$'\n'/|}"
        done
    '''
    result = subprocess.run(['bash', '-c', script, 'gate', str(ROOT), str(min_rails), *map(str, placements)],
                            env=fixture.env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [tuple(line.split('\t', 1)) for line in result.stdout.splitlines()]


class GateOnAFullMesh(unittest.TestCase):
    """On a full mesh the launch check refuses and passes as before, word for word."""

    def test_a_mesh_prints_what_it_printed_before(self):
        fixture = Fixture(Path(self.enterContext(tempfile.TemporaryDirectory())), nodes=3)
        # The fixture mesh has one link per pair; a recipe needs two by default.
        self.assertEqual(gate(fixture, 1, 2, 3, '0,2', '2,1', '0,1,2'), [
            ('0', ''),
            ('1', 'topology: ranks 0/1 expose 1 shared RoCE rail(s); the spec requires 2'),
            ('1', 'topology: ranks 0/1 expose 1 shared RoCE rail(s); the spec requires 2'),
            ('1', 'topology: ranks 0/1 expose 1 shared RoCE rail(s); the spec requires 2'),
            ('1', 'topology: ranks 0/1 expose 1 shared RoCE rail(s); the spec requires 2'),
            ('1', 'topology: ranks 0/1 expose 1 shared RoCE rail(s); the spec requires 2')])
        self.assertEqual(gate(fixture, 2, 3, '2,0', min_rails=1), [('0', ''), ('0', ''), ('0', '')])


def ring(test, **options):
    fixture = Fixture(Path(test.enterContext(tempfile.TemporaryDirectory())), probes=shuffled(ring_probes(4, **options)))
    return fixture, [node['hostname'] for node in fixture.topology['nodes']]


class GateOnARing(unittest.TestCase):
    """The launch check judges the chosen nodes: neighbours pass, any two that
    share no link are refused by name."""

    def test_neighbouring_pairs_pass_and_the_opposite_pairs_and_every_triple_fail(self):
        fixture, names = ring(self)
        neighbours = ['0,1', '1,0', '1,2', '2,1', '2,3', '3,2', '0,3', '3,0']
        self.assertEqual(gate(fixture, 2, *neighbours), [('0', '')] * 9)
        for chosen, (first, second) in (('0,2', (0, 2)), ('2,0', (2, 0)), ('1,3', (1, 3)), ('3,1', (3, 1))):
            with self.subTest(placement=chosen):
                self.assertEqual(gate(fixture, chosen),
                                 [('1', f'topology: {tm.ring_refusal(names[first], names[second])}')])
        triples = ['0,1,2', '1,2,3', '2,3,0', '3,0,1', '0,1,3', '1,0,3', '0,2,3']
        for chosen, result in zip([3, 4, *triples], gate(fixture, 3, 4, *triples)):
            with self.subTest(placement=chosen):
                self.assertEqual(result[0], '1')
                self.assertIn(tm.RING_CABLING, result[1])
                self.assertRegex(result[1], r'^topology: fixture-ring-\w+ and fixture-ring-\w+ are not cabled')

    def test_a_cable_with_too_few_links_names_the_pair_and_both_counts(self):
        fixture, names = ring(self, single_link_cables={0})
        pair = sorted((names.index('fixture-ring-dorado'), names.index('fixture-ring-bravo')))
        chosen = f'{pair[0]},{pair[1]}'
        self.assertEqual(gate(fixture, chosen), [
            ('1', f'topology: {names[pair[0]]} and {names[pair[1]]} share 1 link on their cable; this recipe needs 2')])
        self.assertEqual(gate(fixture, chosen, min_rails=1), [('0', '')])

    def test_the_two_node_sentence_is_gone(self):
        for path in (ROOT / 'cluster/topology.sh', ROOT / 'scripts/topology_manifest.py', ROOT / 'scripts/serve_plan.py',
                     ROOT / 'docs/OPERATIONS.md'):
            self.assertNotIn('only on a full mesh', path.read_text(), path.name)


def placement(fixture, nodes, selection):
    """resolve_serving_placement for a recipe on NODES nodes and SELECTION, as
    --placement-nodes gives it, with scripts/lib.sh sourced."""
    script = r"""
        . "$1/scripts/lib.sh"
        NODES="$2" TOPOLOGY_CLASS=roce-full-mesh MIN_RAILS_PER_PAIR=2
        resolve_serving_placement "" "$3"; status=$?
        printf 'status=%s indexes=%s\n' "$status" "${SERVING_NODE_INDEXES[*]}"
    """
    return subprocess.run(['bash', '-c', script, 'placement', str(ROOT), str(nodes), selection],
                          env=fixture.env, capture_output=True, text=True, timeout=60)


class PlacementNodesAtArgumentTime(unittest.TestCase):
    def test_the_opposite_pair_is_refused_before_anything_runs(self):
        fixture, names = ring(self)
        result = placement(fixture, 2, f'{names[0]},{names[2]}')
        self.assertIn('status=1', result.stdout, result.stderr)
        self.assertIn(f'topology: {tm.ring_refusal(names[0], names[2])}', result.stderr)
        self.assertEqual(fixture.calls(), [])

    def test_a_neighbouring_pair_is_resolved_in_the_order_given(self):
        fixture, names = ring(self)
        result = placement(fixture, 2, f'{names[3]},{names[0]}')
        self.assertIn('status=0 indexes=3 0', result.stdout, result.stderr)
        self.assertEqual(fixture.calls(), [])


class EveryCallerAppliesTheSameRule(unittest.TestCase):
    """Each caller judges the placement it serves on with the same check and the
    recipe's own requirement; Stop judges none."""
    CALLERS = {'scripts/up.sh': True, 'scripts/resources.sh': True, 'scripts/check-weights.sh': True,
               'scripts/observe-serving.sh': True, 'cluster/start-cluster.sh': False, 'cluster/preflight.sh': False,
               'scripts/guard-storage.sh': False}

    def test_each_caller_checks_after_its_placement(self):
        for path, resolves in self.CALLERS.items():
            text = (ROOT / path).read_text()
            with self.subTest(path=path):
                checked = text.find('require_profile_topology')
                self.assertGreaterEqual(checked, 0)
                self.assertRegex(text[checked:checked + 120], r'"\$(NODES|EXPECTED_NODES)" +\\?\s*'
                                 r'"\$TOPOLOGY_CLASS" "\$MIN_RAILS_PER_PAIR"')
                if resolves:
                    # The placement it serves on is resolved first, so the check judges those nodes.
                    self.assertLess(text.find('resolve_serving_placement'), checked)
        # The others serve on the default order only: Start refuses another order
        # outside a guarded dry run, and guard-storage requires each rank on its own position.
        self.assertIn('ordinary start cannot launch a remote head', (ROOT / 'scripts/up.sh').read_text())
        self.assertIn('[ "$expected_rank" = "$rank" ]', (ROOT / 'scripts/guard-storage.sh').read_text())
        self.assertNotIn('require_profile_topology', (ROOT / 'cluster/stop-cluster.sh').read_text())


PORT0, PORT1 = {'rocep0a', 'rocep0b'}, {'rocep1a', 'rocep1b'}


def profile(topology, *ranks):
    """profile-fabric's rows for the recipe's nodes at saved RANKS, in that order."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        tm.profile_fabric(topology, len(ranks), [topology['nodes'][rank]['node_id'] for rank in ranks])
    # The adapters, as a set: their order follows the saved links' networks.
    return {int(line.split('\t')[0]): set(line.split('\t')[1].split(',')) for line in out.getvalue().splitlines()}


class AdaptersForTheChosenNodes(unittest.TestCase):
    """Each node of a neighbouring pair gets its own adapters on their cable,
    whichever order the pair is given in, the closing cable included."""

    def test_every_neighbouring_pair_in_both_orders(self):
        fixture, _names = ring(self)
        topology = fixture.topology
        # Saved ranks follow the cables: rank i's port 0 is cabled to rank i+1's port 1,
        # and the closing cable [0, 3] joins rank 3's port 0 to rank 0's port 1.
        for first, second in ((0, 1), (1, 2), (2, 3), (3, 0)):
            for ranks in ((first, second), (second, first)):
                with self.subTest(ranks=ranks):
                    self.assertEqual(profile(topology, *ranks), {first: PORT0, second: PORT1})

    def test_the_launch_check_keeps_each_nodes_adapters_by_its_saved_position(self):
        fixture, _names = ring(self)
        script = r"""
            . "$1/cluster/topology.sh"
            SERVING_NODE_INDEXES=(3 0)
            require_profile_topology 2 roce-full-mesh 2 || exit 9
            printf '%s|%s\n' "${CLUSTER_PROFILE_HCAS[3]}" "${CLUSTER_PROFILE_HCAS[0]}"
        """
        result = subprocess.run(['bash', '-c', script, 'fabric', str(ROOT)], env=fixture.env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        node3, node0 = result.stdout.strip().split('|')
        self.assertEqual((set(node3.split(',')), set(node0.split(','))), (PORT0, PORT1))


def ping_links(fixture, count, **env):
    """topology_ping_links for a recipe on COUNT nodes, with lib.sh sourced."""
    script = r"""
        . "$1/scripts/lib.sh"; . "$1/scripts/topology-probes.sh"
        load_cluster_topology || exit 9
        topology_ping_links "$CLUSTER_TOPOLOGY_FILE" "$2"
    """
    result = subprocess.run(['bash', '-c', script, 'ping', str(ROOT), str(count)], env={**fixture.env, **env},
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [line.split('\t') for line in result.stdout.splitlines()]


class PreflightPings(unittest.TestCase):
    """Preflight pings only the links among the recipe's ranks and judges each pair."""

    def test_a_two_node_recipe_on_a_ring_pings_only_its_cable(self):
        fixture, _names = ring(self)
        rows = ping_links(fixture, 2)
        pings = [row for row in rows if row[0] != 'pair']
        self.assertEqual(len(pings), 4)
        self.assertTrue(all(row[0] == 'ok' and {row[1], row[2]} == {'0', '1'} for row in pings), pings)
        self.assertEqual([row for row in rows if row[0] == 'pair'], [['pair', '0', '1', '2', '2']])
        self.assertEqual(len([args for tool, args in fixture.calls() if tool in ('ping', 'ssh')]), 4)

    def test_a_link_that_does_not_answer_lowers_its_pairs_count(self):
        fixture, _names = ring(self)
        rail = next(link for link in fixture.topology['links'] if link['ranks'] == [0, 1])['rails'][0]
        rows = ping_links(fixture, 2, TOPOLOGY_PING_FAIL=rail['b']['ip'])
        self.assertIn(['fail', '0', '1', rail['b']['ip'], rail['network'], rail['a']['netdev']], rows)
        self.assertEqual(rows[-1], ['pair', '0', '1', '1', '2'])

    def test_the_whole_ring_judges_only_its_cables(self):
        fixture, _names = ring(self)
        pairs = [row for row in ping_links(fixture, 4) if row[0] == 'pair']
        self.assertEqual(pairs, [['pair', '0', '1', '2', '2'], ['pair', '0', '3', '2', '2'],
                                 ['pair', '1', '2', '2', '2'], ['pair', '2', '3', '2', '2']])

    def test_a_mesh_pings_the_pairs_among_its_ranks(self):
        fixture = Fixture(Path(self.enterContext(tempfile.TemporaryDirectory())), nodes=3)
        rows = ping_links(fixture, 2)
        self.assertEqual([row[0] for row in rows], ['ok', 'ok', 'pair'])
        self.assertEqual(rows[-1], ['pair', '0', '1', '1', '1'])
        self.assertEqual(len([row for row in ping_links(fixture, 3) if row[0] == 'pair']), 3)

    def test_preflight_reads_the_rows_and_no_longer_counts_links_per_node(self):
        text = (ROOT / 'cluster/preflight.sh').read_text()
        self.assertIn('topology_ping_links "$CLUSTER_TOPOLOGY_FILE" "$EXPECTED_NODES"', text)
        self.assertNotIn('RDMA links ACTIVE', text)


def listing(*states):
    """`rdma link show` lines for (adapter, state) pairs."""
    return ''.join(f'link {hca}/1 state {state} physical_state LINK_UP netdev fab{index}\n'
                   for index, (hca, state) in enumerate(states))


def adapters_active(selected, text):
    script = '. "$1/scripts/topology-probes.sh"; topology_adapters_active "$2"'
    result = subprocess.run(['bash', '-c', script, 'rdma', str(ROOT), selected], input=text, capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.rstrip('\n').split('\t')


class PreflightRdmaState(unittest.TestCase):
    """Preflight checks that every adapter the launch gives a rank is an ACTIVE RDMA port."""

    def test_all_selected_adapters_active(self):
        text = listing(('rocep0a', 'ACTIVE'), ('rocep0b', 'ACTIVE'), ('rocep1a', 'ACTIVE'), ('rocep1b', 'ACTIVE'))
        self.assertEqual(adapters_active('rocep0a,rocep0b', text), ['2', '2', ''])

    def test_one_selected_adapter_down_is_named(self):
        text = listing(('rocep0a', 'ACTIVE'), ('rocep0b', 'DOWN'))
        self.assertEqual(adapters_active('rocep0a,rocep0b', text), ['1', '2', 'rocep0b'])

    def test_no_rdma_devices_fail_however_pings_answer(self):
        self.assertEqual(adapters_active('rocep0a,rocep0b', ''), ['0', '2', 'rocep0a,rocep0b'])

    def test_an_unselected_adapter_down_does_not_fail_a_pair_that_does_not_use_it(self):
        # Rank 0 of a neighbouring pair uses its port 0; its port 1 cable leads elsewhere.
        fixture, _names = ring(self)
        selected = ','.join(sorted(profile(fixture.topology, 0, 1)[0]))
        text = listing(('rocep0a', 'ACTIVE'), ('rocep0b', 'ACTIVE'), ('rocep1a', 'DOWN'), ('rocep1b', 'DOWN'))
        self.assertEqual(adapters_active(selected, text), ['2', '2', ''])

    def test_preflight_checks_the_adapters_selected_for_each_rank(self):
        text = (ROOT / 'cluster/preflight.sh').read_text()
        self.assertIn('selected="${CLUSTER_PROFILE_HCAS[$rank]:-}"', text)
        self.assertIn('node_exec "$rank" "rdma link show 2>/dev/null" 2>/dev/null | topology_adapters_active "$selected"',
                      text)
        self.assertIn('selected RDMA adapters ACTIVE; not ACTIVE: ', text)
        # The selection is made by the launch check, which runs first.
        self.assertLess(text.find('require_profile_topology'), text.find('check_node "$rank"'))


if __name__ == '__main__':
    unittest.main()
