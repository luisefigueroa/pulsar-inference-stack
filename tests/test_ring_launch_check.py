"""The launch check on the chosen nodes: what the recipe's link requirement
reaches, and how each caller judges a placement on a ring and on a full mesh.
Synthetic fixtures and doubles only; nothing here contacts a node."""
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


if __name__ == '__main__':
    unittest.main()
