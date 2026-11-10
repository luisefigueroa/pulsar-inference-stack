"""The launch check on the chosen nodes: what the recipe's link requirement
reaches, and how each caller judges a placement on a ring and on a full mesh.
Synthetic fixtures and doubles only; nothing here contacts a node."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'scripts')]

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


if __name__ == '__main__':
    unittest.main()
