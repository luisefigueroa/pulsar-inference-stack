"""Real command routing with synthetic nodes; never contacts serving hardware."""
from datetime import datetime, timezone
import json
import os
import pty
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tests'), str(ROOT/'scripts')]
from support.topology_fixture import Fixture, node_probe
import topology_actions


class TopologyCommands(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))

    def test_show_is_saved_only_and_root_gum_alias_is_available(self):
        f = self.fixture
        before = f.path.read_bytes()
        result = f.run('topology', 'show', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'saved')
        self.assertEqual(f.calls(), [])
        self.assertEqual(f.path.read_bytes(), before)
        # TUI-12: nodes by hostname; the saved order is a position, never a rank.
        human = f.run('topology', 'show')
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('fixture-rank-0 (position 0)', human.stdout)
        self.assertIn('fixture-rank-1 (position 1)', human.stdout)
        self.assertNotIn('Node 0', human.stdout)
        help_result = f.run('gum')  # non-TTY root menu prints help only
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('pulsar topology', help_result.stdout)
        self.assertEqual(f.calls(), [])

    def test_missing_and_malformed_state_have_json_and_no_probes(self):
        f = self.fixture
        for contents, expected in [(None, 'missing'), ('{broken', 'invalid'), ('{"nodes":[42]}', 'invalid')]:
            if contents is None: f.path.unlink()
            else: f.path.write_text(contents)
            result = f.run('topology', 'check', '--json')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['status'], expected)
            self.assertEqual(f.calls(), [])

    def test_check_all_saved_nodes_uses_control_endpoints_and_does_not_save(self):
        f = self.fixture
        before = f.path.read_bytes(), f.config.read_bytes()
        result = f.run('topology', 'check', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row['status'] for row in json.loads(result.stdout)['nodes']], ['ready', 'ready'])
        ssh = [args for tool, args in f.calls() if tool == 'ssh']
        self.assertTrue(ssh)
        self.assertTrue(all('HostName=192.0.2.11' in args and 'StrictHostKeyChecking=yes' in args for args in ssh))
        self.assertEqual(before, (f.path.read_bytes(), f.config.read_bytes()))

    def test_check_reports_unreachable_changed_identity_and_fabric_failure(self):
        f = self.fixture
        for mode in ('unreachable', 'identity', 'fabric'):
            with self.subTest(mode=mode):
                f.env.pop('TOPOLOGY_UNREACHABLE', None); f.env.pop('TOPOLOGY_PING_RC', None)
                probe = dict(f.probes[1])
                if mode == 'unreachable': f.env['TOPOLOGY_UNREACHABLE'] = '1'
                if mode == 'identity': probe['node_id'] = 'different-node'
                if mode == 'fabric': f.env['TOPOLOGY_PING_RC'] = '1'
                Path(f.files[1]).write_text(json.dumps(probe))
                result = f.run('topology', 'check', '--json')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                document = json.loads(result.stdout)
                self.assertEqual(document['status'], 'blocked')
                self.assertEqual(len(document['nodes']), 2)

    def test_single_node_and_missing_trust_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            single = Fixture(Path(temporary), nodes=1, enrolled=True)
            result = single.run('topology', 'check', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'ready')
        with tempfile.TemporaryDirectory() as temporary:
            multiple = Fixture(Path(temporary), nodes=2, enrolled=False)
            result = multiple.run('topology', 'check', '--json')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('SSH identity is not enrolled', result.stdout)

    def test_fabric_address_drift_and_failed_discovery_ping_are_explicit(self):
        f = self.fixture
        probe = dict(f.probes[1]); probe['rdma'] = []
        Path(f.files[1]).write_text(json.dumps(probe))
        result = f.run('topology', 'check', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('saved RDMA', result.stdout)
        Path(f.files[1]).write_text(json.dumps(f.probes[1]))
        f.env['TOPOLOGY_PING_RC'] = '1'
        result = f.run('topology', 'detect', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['result'], 'incomplete')
        self.assertTrue(json.loads(result.stdout)['discovery_issues'])

    def test_discovery_manual_candidates_is_non_saving_and_never_accepts_keys_from_environment(self):
        f = self.fixture
        before = f.path.read_bytes(), f.config.read_bytes()
        f.env['DETECT_FABRIC_ACCEPT_NEW'] = '1'
        result = f.run('topology', 'detect', '--candidate', 'alias-1', '--candidate', 'unrelated', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['topology']['nodes']), 2)
        self.assertEqual(before, (f.path.read_bytes(), f.config.read_bytes()))
        self.assertTrue(all('StrictHostKeyChecking=accept-new' not in args for _, args in f.calls()))

    def test_missing_saved_node_blocks_discovery_and_configuration(self):
        f = self.fixture
        before = f.path.read_bytes()
        f.env['TOPOLOGY_UNREACHABLE'] = '1'
        result = f.run('topology', 'detect', '--json')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        document = json.loads(result.stdout)
        self.assertEqual(document['result'], 'incomplete')
        self.assertEqual(document['saved_membership']['missing'][0]['node_id'], 'fixture-node-1')
        result = f.run('topology', 'configure', '--yes')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('Missing confirmed node: fixture-rank-1 (position 1)', ' '.join(result.stdout.split()))
        self.assertEqual(f.path.read_bytes(), before)

    def test_invalid_saved_state_allows_diagnostic_discovery_but_not_overwrite(self):
        f = self.fixture; f.path.write_text('{broken')
        result = f.run('topology', 'detect', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)['saved_membership']['issues'])
        self.assertEqual(f.path.read_text(), '{broken')

    def test_unusable_enrolled_ssh_config_never_falls_back_to_other_trust(self):
        f = self.fixture; f.config.unlink()
        result = f.run('topology', 'detect', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['result'], 'incomplete')
        self.assertIn('Saved SSH configuration is unusable', result.stdout)
        self.assertFalse(any(tool == 'ssh' for tool, _ in f.calls()))

    def test_configure_requires_confirmation_and_preserves_active_service_guard(self):
        f = self.fixture; before = f.path.read_bytes()
        result = f.run('topology', 'configure')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(f.calls(), [])
        f.env['TOPOLOGY_ACTIVE'] = '1'
        result = f.run('topology', 'configure', '--yes')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(f.path.read_bytes(), before)

    def test_explicit_configuration_saves_but_does_not_enroll_trust(self):
        f = self.fixture
        result = f.run('topology', 'configure', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(f.path.read_text())['schema_version'], 1)
        self.assertEqual(len(json.loads(f.path.read_text())['nodes']), 2)
        self.assertFalse(any(tool == 'docker' and args[:1] != ['ps'] for tool, args in f.calls()))

    def test_read_commands_reject_mutating_flags_and_legacy_utilities_still_work(self):
        f = self.fixture
        for action in ('show', 'check', 'detect'):
            result = f.run('topology', action, '--yes')
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(f.calls(), [])
        self.assertEqual(f.run('topology', 'validate', str(f.path)).returncode, 0)

    def interactive(self, args, responses, expected_prompt):
        f = self.fixture
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        process = subprocess.Popen([str(ROOT/'pulsar'), *args], env=f.env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        # A loaded CI runner takes several times longer than a workstation.
        output = b''; deadline = time.monotonic()+60
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try: chunk = os.read(master, 65536)
                    except OSError: break
                    if not chunk: break
                    output += chunk
                    if responses and expected_prompt.encode() in output:
                        os.write(master, responses.encode()); responses = ''
                if process.poll() is not None: break
            process.wait(timeout=max(2, deadline-time.monotonic()))
        finally:
            if process.poll() is None: process.kill(); process.wait()
        return process.returncode, output.decode(errors='replace')

    def use_gum(self, **answers):
        """Draw menus and confirmations with the fake Gum on the test terminal."""
        self.fixture.env.update(TERM='xterm-256color', GUM_BIN=str(self.fixture.root/'bin/gum'), **answers)

    def gum_prompts(self, kind):
        """Headers of Gum menus or questions of Gum confirmations, in order."""
        return [args[args.index('--header')+1].split('\n')[0] if kind == 'choose' else args[-1]
                for tool, args in self.fixture.calls() if tool == 'gum' and args[:1] == [kind]]

    def probes(self):
        return [tool for tool, _ in self.fixture.calls() if tool != 'gum']

    def test_menu_back_does_not_probe_and_cancel_does_not_save(self):
        f = self.fixture
        self.use_gum(TOPOLOGY_GUM_CHOICE='5', TOPOLOGY_CONFIRM_RC='1')
        result, output = self.interactive(['topology', 'menu'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual(self.gum_prompts('choose'), ['Cluster topology'])
        self.assertEqual(self.probes(), [])
        before = f.path.read_bytes()
        result, output = self.interactive(['topology', 'configure'], '', '')
        self.assertEqual(result, 0, output); self.assertEqual(f.path.read_bytes(), before)
        self.assertEqual(self.gum_prompts('confirm'), ['Save cluster membership with 2 machines?'])

    def test_saved_membership_opens_in_the_pager_without_probing(self):
        f = self.fixture
        self.use_gum(TOPOLOGY_GUM_CHOICE='0,5')
        result, output = self.interactive(['topology', 'menu'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual(self.probes(), [])
        self.assertEqual(self.gum_prompts('choose'), ['Cluster topology', 'Cluster topology'])
        self.assertIn('fixture-rank-1', (f.root/'pager.txt').read_text())
        # The frame is drawn on entry and not again while membership is unchanged.
        self.assertEqual(output.count('Saved membership (no probes)'), 1)

    def test_menus_without_gum_name_commands_and_do_not_probe(self):
        f = self.fixture  # TERM=dumb: Gum cannot draw
        for args, expected in ((['topology', 'menu'], 'the topology menu'), (['topology', 'setup'], 'guided setup')):
            result, output = self.interactive(args, '', '')
            self.assertEqual(result, 2, output)
            self.assertIn(f'error: {expected} needs an interactive terminal with Gum. Run the same steps as '
                          'commands:', ' '.join(output.split()))
            self.assertIn('./pulsar topology ', output)
        before = f.path.read_bytes()
        result, output = self.interactive(['topology', 'configure'], '', '')
        self.assertNotEqual(result, 0, output)
        self.assertIn('pulsar topology configure --yes', ' '.join(output.split()))
        self.assertEqual(f.calls(), [])
        self.assertEqual(f.path.read_bytes(), before)

    def test_gum_root_entry_and_back_does_not_probe(self):
        f = self.fixture
        f.env.pop('NO_COLOR', None)
        # Cluster topology, Back to the home menu, then Exit.
        self.use_gum(TOPOLOGY_GUM_HOME='6,8', TOPOLOGY_GUM_CHOICE='5', PULSAR_COLD_ROOT=str(f.root))
        result, output = self.interactive(['gum'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual(self.gum_prompts('choose'),
                         ['Pulsar Inference Stack', 'Cluster topology', 'Pulsar Inference Stack'])
        self.assertEqual(self.probes(), [])

    def summary(self, fixture):
        """The topology menu's frame for FIXTURE's saved membership."""
        shown = fixture.run('topology', 'show', '--json')
        result = subprocess.run([sys.executable, str(ROOT/'scripts/topology_actions.py'), 'summary', '-'],
                                input=shown.stdout, env=fixture.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], 'Saved membership (no probes)')
        self.assertLessEqual(max(map(len, result.stdout.splitlines())), 48)
        return ' '.join(result.stdout.split())

    def test_menu_frame_summarizes_saved_membership_without_probes(self):
        f = self.fixture
        text = self.summary(f)
        self.assertIn('Nodes fixture-rank-0, fixture-rank-1', text)
        self.assertIn('Membership saved from a discovery less than a minute ago', text)
        self.assertIn('SSH trust enrolled from host keys read less than a minute ago', text)
        for nodes in (1, 2):
            with self.subTest(unenrolled=nodes), tempfile.TemporaryDirectory() as temporary:
                # One node still needs enrollment: topology check blocks without it.
                text = self.summary(Fixture(Path(temporary), nodes=nodes, enrolled=False))
                self.assertIn('SSH trust not enrolled; choose Change membership… → Enroll SSH trust', text)
        f.path.write_text('{broken')
        # Configure cannot replace invalid saved state, so it is not offered as the fix;
        # forget is the way out.
        text = self.summary(f)
        self.assertIn('Membership saved but invalid, and setup cannot replace it; inspect it with '
                      'Show saved membership, or start over with ./pulsar topology forget', text)
        self.assertNotIn('Configure', text)
        self.assertNotIn('by hand', text)
        f.path.unlink()
        self.assertIn('Membership not saved; choose Change membership… → First-use setup', self.summary(f))
        self.assertEqual(f.calls(), [])

    def test_saved_age_counts_whole_units_and_admits_unknown_times(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        for written, expected in (('2026-10-20T11:59:30+00:00', 'less than a minute ago'),
                                  ('2026-10-20T11:59:00Z', '1 minute ago'),
                                  ('2026-10-20T09:00:00+00:00', '3 hours ago'),
                                  ('2026-10-18T12:00:00+00:00', '2 days ago'),
                                  ('2026-10-20T11:00:00', 'at an unknown time'),
                                  ('yesterday', 'at an unknown time'), (None, 'at an unknown time')):
            self.assertEqual(topology_actions.saved_age(written, now), expected)

    def menu_choices(self):
        """Each Gum menu's choices by header, as last drawn."""
        menus = (json.loads(line) for line in (self.fixture.root/'menus.jsonl').read_text().splitlines())
        return {header: [line.split('\t')[0] for line in lines] for header, lines in menus}

    def test_topology_choices_say_what_they_do_and_change_membership_returns(self):
        for answer in ('3', 'esc'):
            with self.subTest(change=answer):
                (self.fixture.root/'calls.jsonl').unlink(missing_ok=True)
                self.use_gum(TOPOLOGY_GUM_CHOICE='4,5', TOPOLOGY_GUM_CHANGE=answer, COLUMNS='80')
                result, output = self.interactive(['topology', 'menu'], '', '')
                self.assertEqual(result, 0, output)
                self.assertEqual(self.gum_prompts('choose'),
                                 ['Cluster topology', 'Change membership', 'Cluster topology'])
                self.assertEqual(self.probes(), [])
        choices = self.menu_choices()
        suffixes = ('(saved, no probes)', '(checks nodes now)', '(asks before saving)')
        self.assertEqual([choice for choice in choices['Cluster topology'] if not choice.endswith(suffixes)],
                         ['Change membership…', 'Back'])
        self.assertEqual(choices['Change membership'], ['First-use setup (asks before saving)',
                         'Configure membership or add a machine (asks before saving)',
                         'Enroll SSH trust (asks before saving)', 'Back'])

    def test_narrow_topology_choices_keep_only_effects_that_fit(self):
        # Gum cuts a long choice at the edge; an effect is shown whole or not at all.
        self.use_gum(TOPOLOGY_GUM_CHOICE='4,5', TOPOLOGY_GUM_CHANGE='3', COLUMNS='44')
        result, output = self.interactive(['topology', 'menu'], '', '')
        self.assertEqual(result, 0, output)
        choices = self.menu_choices()
        self.assertEqual(choices['Cluster topology'], ['Show saved membership (saved, no probes)',
                         'Check saved nodes and fabric', 'Check SSH trust (checks nodes now)',
                         'Detect cluster candidates', 'Change membership…', 'Back'])
        self.assertEqual(choices['Change membership'], ['First-use setup (asks before saving)',
                         'Configure membership or add a machine', 'Enroll SSH trust (asks before saving)', 'Back'])
        self.assertLessEqual(max(map(len, output.replace('\r', '').splitlines())), 44)

    def test_change_membership_routes_setup_and_enrollment(self):
        f = self.fixture
        self.use_gum(TOPOLOGY_GUM_CHOICE='4,5', TOPOLOGY_GUM_CHANGE='0')
        result, output = self.interactive(['topology', 'menu'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual(self.gum_prompts('confirm'), [])  # healthy membership: nothing to save
        self.assertFalse(any(tool == 'avahi-browse' for tool, _ in f.calls()))
        self.assertEqual(f.run('topology', 'configure', '--yes').returncode, 0)
        before = f.path.read_bytes()
        f.log.unlink()
        self.use_gum(TOPOLOGY_GUM_CHOICE='4,5', TOPOLOGY_GUM_CHANGE='2', TOPOLOGY_CONFIRM_RC='1')
        result, output = self.interactive(['topology', 'menu'], '', '')
        self.assertEqual(result, 0, output)
        self.assertIn('not enrolled; choose Change membership… → Enroll SSH trust', ' '.join(output.split()))
        self.assertEqual(self.gum_prompts('confirm'), ['Enroll these SSH identities?'])
        self.assertEqual(f.path.read_bytes(), before)

    def test_service_started_during_confirmation_blocks_save(self):
        f = self.fixture
        before = f.path.read_bytes()
        f.env['TOPOLOGY_ACTIVE_AFTER'] = '4'  # old + proposed two-node idle checks
        self.use_gum(TOPOLOGY_CONFIRM_RC='0')
        result, output = self.interactive(['topology', 'configure'], '', '')
        self.assertNotEqual(result, 0, output)
        self.assertEqual(self.gum_prompts('confirm'), ['Save cluster membership with 2 machines?'])
        self.assertEqual(f.path.read_bytes(), before)

    def test_first_use_requires_enrollment_even_for_one_node(self):
        with tempfile.TemporaryDirectory() as temporary:
            single = Fixture(Path(temporary), nodes=1, enrolled=False)
            result = single.run('topology', 'check', '--json')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('first-use setup', result.stdout)
            result = single.run('ssh-trust', 'enroll', '--yes')
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(single.run('topology', 'check', '--json').returncode, 0)

    def test_setup_refuses_noninteractive_use_and_skips_healthy_enrollment(self):
        f = self.fixture
        result = f.run('topology', 'setup')
        self.assertEqual(result.returncode, 2)
        self.assertIn('error: guided setup needs an interactive terminal with Gum', result.stderr)
        self.assertEqual(f.calls(), [])
        before = f.path.read_bytes(), f.config.read_bytes()
        self.use_gum()
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual(before, (f.path.read_bytes(), f.config.read_bytes()))
        self.assertEqual(self.gum_prompts('confirm'), [])
        self.assertFalse(any(tool == 'avahi-browse' for tool, _ in f.calls()))

    def test_setup_enrolls_missing_trust_only_after_confirmation(self):
        f = self.fixture
        self.assertEqual(f.run('topology', 'configure', '--yes').returncode, 0)
        before = f.path.read_bytes()
        self.use_gum(TOPOLOGY_CONFIRM_RC='1')
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertNotEqual(result, 0, output)
        self.assertEqual(self.gum_prompts('confirm'), ['Enroll these SSH identities?'])
        self.assertEqual(f.path.read_bytes(), before)
        self.use_gum(TOPOLOGY_CONFIRM_RC='0')
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual(json.loads(f.path.read_text())['schema_version'], 2)
        self.assertEqual(f.run('topology', 'check', '--json').returncode, 0)

    def test_fresh_setup_configures_and_enrolls_in_order_with_separate_confirmations(self):
        f = self.fixture
        f.path.unlink(); f.config.unlink()
        self.use_gum(TOPOLOGY_CONFIRM_RC='0')
        result, output = self.interactive(['topology', 'setup', '--candidate', 'alias-1'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual(self.gum_prompts('confirm'), ['Save cluster membership with 2 machines?', 'Enroll these SSH identities?'])
        self.assertEqual(json.loads(f.path.read_text())['schema_version'], 2)
        self.assertEqual(len(json.loads(f.path.read_text())['nodes']), 2)
        self.assertTrue(f.config.is_file())

    def test_fresh_setup_cancel_never_reaches_enrollment(self):
        f = self.fixture
        f.path.unlink(); f.config.unlink()
        self.use_gum(TOPOLOGY_CONFIRM_RC='1', TOPOLOGY_GUM_REVIEW='0,4')
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertEqual(result, 1, output)
        self.assertIn('Setup stopped before saving cluster membership.', output)
        self.assertEqual(self.gum_prompts('confirm'), ['Save cluster membership with 1 machine?'])
        self.assertFalse(f.path.exists()); self.assertFalse(f.config.exists())

    def test_ctrl_c_at_membership_confirmation_preserves_interrupt_and_saved_files(self):
        f = self.fixture
        before = f.path.read_bytes(), f.config.read_bytes()
        self.use_gum(TOPOLOGY_CONFIRM_RC='130')
        result, output = self.interactive(['topology', 'configure'], '', '')
        self.assertEqual(result, 130, output)
        self.assertEqual(before, (f.path.read_bytes(), f.config.read_bytes()))
        self.assertEqual(self.gum_prompts('confirm'), ['Save cluster membership with 2 machines?'])

    def test_ctrl_c_at_enrollment_confirmation_preserves_interrupt_and_saved_files(self):
        f = self.fixture
        before = f.path.read_bytes(), f.config.read_bytes()
        self.use_gum(TOPOLOGY_CONFIRM_RC='130')
        result, output = self.interactive(['ssh-trust', 'enroll'], '', '')
        self.assertEqual(result, 130, output)
        self.assertEqual(before, (f.path.read_bytes(), f.config.read_bytes()))
        self.assertEqual(self.gum_prompts('confirm'), ['Enroll these SSH identities?'])

    def test_fresh_setup_ctrl_c_never_reaches_enrollment(self):
        f = self.fixture
        f.path.unlink(); f.config.unlink()
        self.use_gum(TOPOLOGY_CONFIRM_RC='130')
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertEqual(result, 130, output)
        self.assertEqual(self.gum_prompts('confirm'), ['Save cluster membership with 1 machine?'])
        self.assertFalse(f.path.exists()); self.assertFalse(f.config.exists())

    def test_setup_detects_invalid_or_unready_saved_state_without_replacing_it(self):
        f = self.fixture
        f.path.write_text('{broken')
        before = f.path.read_bytes()
        self.use_gum()
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertNotEqual(result, 0, output)
        self.assertEqual(f.path.read_bytes(), before)
        self.assertIn('diagnostic discovery', output)
        self.assertTrue(any(tool == 'avahi-browse' for tool, _ in f.calls()))

        f.path.write_text(json.dumps(f.topology))
        f.env['TOPOLOGY_PING_RC'] = '1'
        before = f.path.read_bytes()
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertNotEqual(result, 0, output)
        self.assertEqual(f.path.read_bytes(), before)
        self.assertIn('without saving', output)


    def test_setup_refuses_candidates_only_with_valid_membership_and_before_any_probe(self):
        f = self.fixture
        before = f.path.read_bytes(), f.config.read_bytes()
        for args, hosts in ((['--candidate', 'alias-9'], 'alias-9 was'),
                            (['--candidate', 'alias-8', '--candidate', 'alias-9'], 'alias-8, alias-9 were')):
            with self.subTest(args=args):
                # No terminal: the refusal does not depend on Gum.
                result = f.run('topology', 'setup', *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                text = ' '.join(result.stderr.split())
                candidates = ' '.join(args)
                self.assertIn(f"error: setup doesn't add machines to saved membership; {hosts} not added.", text)
                self.assertIn(f'To add a machine, run ./pulsar topology configure {candidates}', text)
                self.assertIn('Cluster topology → Change membership… → Configure membership or add a machine', text)
                self.assertIn(f'To discover without saving, run ./pulsar topology detect {candidates}', text)
                self.assertNotIn('interactive terminal', text)
        self.assertEqual(f.calls(), [])
        self.assertEqual(before, (f.path.read_bytes(), f.config.read_bytes()))
        # The same refusal on a terminal with Gum: nothing is asked or probed.
        self.use_gum(TOPOLOGY_CONFIRM_RC='0')
        result, output = self.interactive(['topology', 'setup', '--candidate', 'alias-9'], '', '')
        self.assertEqual(result, 2, output)
        self.assertIn('alias-9 was not added', output)
        self.assertEqual(f.calls(), [])
        self.assertEqual(before, (f.path.read_bytes(), f.config.read_bytes()))
        # Invalid saved membership is not refused: its candidates join
        # diagnostic discovery, which never replaces the file.
        f.path.write_text('{broken')
        self.use_gum()
        result, output = self.interactive(['topology', 'setup', '--candidate', 'alias-1'], '', '')
        self.assertNotEqual(result, 0, output)
        self.assertNotEqual(result, 2, output)
        self.assertNotIn('not added', output)
        self.assertIn('diagnostic discovery', output)
        self.assertTrue(any(tool == 'avahi-browse' for tool, _ in f.calls()))
        # The explicit address is part of that discovery.
        self.assertTrue(any(tool == 'ssh' and 'alias-1' in args for tool, args in f.calls()))
        self.assertEqual(f.path.read_text(), '{broken')

    def fresh_fixture(self, nodes=2, enrolled=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = Fixture(Path(temporary.name), nodes=nodes, enrolled=enrolled)
        self.fixture.path.unlink()
        self.fixture.config.unlink(missing_ok=True)
        return self.fixture

    def test_setup_adds_repeated_candidates_and_retry_retains_them(self):
        f = self.fresh_fixture(nodes=3)
        mapping = json.loads((f.root/'map.json').read_text())
        mapping['192.0.2.12'] = 2
        (f.root/'map.json').write_text(json.dumps(mapping))
        # With an address added, Remove an added address follows Add.
        self.use_gum(TOPOLOGY_GUM_REVIEW='1,1,3,0',
                     TOPOLOGY_GUM_INPUT='alias-1,192.0.2.12', TOPOLOGY_CONFIRM_RC='0')
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertEqual(result, 0, output)
        saved = json.loads(f.path.read_text())
        self.assertEqual(len(saved['nodes']), 3)
        self.assertEqual(saved['schema_version'], 2)
        self.assertEqual(self.gum_prompts('confirm'), [
            'Save cluster membership with 3 machines?', 'Enroll these SSH identities?'])
        calls = f.calls()
        self.assertEqual(sum(tool == 'avahi-browse' for tool, _ in calls), 5)
        addresses = [args[args.index('--')+1] for tool, args in calls if tool == 'ssh']
        self.assertGreaterEqual(addresses.count('192.0.2.12'), 3)
        confirms = [args for tool, args in calls if tool == 'gum' and args[:1] == ['confirm']]
        self.assertTrue(all('--default=false' in args for args in confirms))
        self.assertFalse(any('StrictHostKeyChecking=accept-new' in args for _, args in calls))

    def test_setup_can_explicitly_save_the_smaller_discovered_membership(self):
        f = self.fresh_fixture()
        self.use_gum(TOPOLOGY_CONFIRM_RC='0')
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual(len(json.loads(f.path.read_text())['nodes']), 1)
        self.assertEqual(self.gum_prompts('confirm'), [
            'Save cluster membership with 1 machine?', 'Enroll these SSH identities?'])

    def test_review_and_address_cancellation_do_not_save_or_enroll(self):
        cases = [('4', '', 1), ('esc', '', 1), ('ctrl-c', '', 130),
                 ('1,4', 'esc', 1), ('1,4', '', 1), ('1', 'ctrl-c', 130)]
        for review, address, expected in cases:
            with self.subTest(review=review, address=address):
                f = self.fresh_fixture()
                self.use_gum(TOPOLOGY_GUM_REVIEW=review, TOPOLOGY_GUM_INPUT=address)
                result, output = self.interactive(['topology', 'setup'], '', '')
                self.assertEqual(result, expected, output)
                self.assertFalse(f.path.exists()); self.assertFalse(f.config.exists())
                self.assertEqual(self.gum_prompts('confirm'), [])
                self.assertEqual(sum(tool == 'avahi-browse' for tool, _ in f.calls()), 1)

    def test_declined_save_keeps_candidates_for_review_and_retry(self):
        f = self.fresh_fixture()
        self.use_gum(TOPOLOGY_CONFIRM_RC='1', TOPOLOGY_GUM_REVIEW='1,0,3,5',
                     TOPOLOGY_GUM_INPUT='alias-1')
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertEqual(result, 1, output)
        self.assertFalse(f.path.exists()); self.assertFalse(f.config.exists())
        self.assertEqual(self.gum_prompts('confirm'),
                         ['Save cluster membership with 2 machines?'])
        self.assertEqual(sum(tool == 'avahi-browse' for tool, _ in f.calls()), 5)
        peers = [args for tool, args in f.calls()
                 if tool == 'ssh' and args[args.index('--')+1] == 'alias-1']
        self.assertGreaterEqual(len(peers), 4)
        self.assertFalse(any('--include-ssh-host-keys' in ' '.join(args) for _, args in f.calls()))

    def test_declined_save_does_not_enroll_concurrently_created_membership(self):
        f = self.fresh_fixture(enrolled=False)
        concurrent = json.dumps(f.topology)
        (f.root/'concurrent-topology.json').write_text(concurrent)
        # The --candidate address is listed as added, so Back follows Remove.
        self.use_gum(TOPOLOGY_CONCURRENT_SAVE='1', TOPOLOGY_CONFIRM_RC='1',
                     TOPOLOGY_GUM_REVIEW='0,5')
        result, output = self.interactive(['topology', 'setup', '--candidate', 'alias-1'], '', '')
        self.assertEqual(result, 1, output)
        self.assertEqual(f.path.read_text(), concurrent)
        self.assertFalse(f.config.exists())
        self.assertEqual(self.gum_prompts('confirm'),
                         ['Save cluster membership with 2 machines?'])
        contexts = [json.loads(line)['setup_configure']
                    for line in (f.root/'discovery-context.jsonl').read_text().splitlines()]
        self.assertEqual(contexts, [None, '1', None])
        self.assertFalse(any('--include-ssh-host-keys' in ' '.join(args) for _, args in f.calls()))

    def test_failed_discovery_offers_retry_but_no_continue_to_save(self):
        f = self.fresh_fixture()
        probe = dict(f.probes[0], qualified=False, reject_reasons=['synthetic unavailable platform'])
        Path(f.files[0]).write_text(json.dumps(probe))
        self.use_gum(TOPOLOGY_GUM_REVIEW='1,3')
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertEqual(result, 1, output)
        self.assertFalse(f.path.exists()); self.assertFalse(f.config.exists())
        self.assertEqual(self.gum_prompts('confirm'), [])
        menus = [json.loads(line) for line in (f.root/'menus.jsonl').read_text().splitlines()]
        self.assertEqual(len(menus), 2)
        self.assertTrue(all(not any('Continue' in option for option in options) for _, options in menus))
        self.assertEqual(sum(tool == 'avahi-browse' for tool, _ in f.calls()), 2)

    def test_failed_configuration_returns_to_review_with_candidates(self):
        f = self.fresh_fixture()
        self.use_gum(TOPOLOGY_ACTIVE='1', TOPOLOGY_GUM_REVIEW='0,5')
        result, output = self.interactive(['topology', 'setup', '--candidate', 'alias-1'], '', '')
        self.assertEqual(result, 1, output)
        self.assertIn('Configuration did not complete', output)
        self.assertFalse(f.path.exists()); self.assertFalse(f.config.exists())
        self.assertEqual(self.gum_prompts('confirm'), [])
        contexts = [json.loads(line)['setup_configure']
                    for line in (f.root/'discovery-context.jsonl').read_text().splitlines()]
        self.assertEqual(contexts, [None, '1', None])
        peers = [args for tool, args in f.calls()
                 if tool == 'ssh' and args[args.index('--')+1] == 'alias-1']
        self.assertGreaterEqual(len(peers), 3)

    # TUI-82: inside the review, detect names the review's own choices; standalone
    # detect keeps its CLI next steps.
    def test_detect_inside_the_review_names_the_review_choices(self):
        f = self.fresh_fixture()
        self.use_gum(TOPOLOGY_UNREACHABLE='1', TOPOLOGY_GUM_REVIEW='5')
        result, output = self.interactive(['topology', 'setup', '--candidate', 'alias-1'], '', '')
        self.assertEqual(result, 1, output)
        flattened = ' '.join(output.split())
        self.assertIn('If a GB10 is missing, choose Add a machine by hostname or IP below.', flattened)
        self.assertNotIn('pulsar topology configure', flattened)
        self.assertNotIn('--candidate HOST', flattened)
        standalone = f.run('topology', 'detect', '--candidate', 'alias-1')
        text = ' '.join(standalone.stdout.split())
        self.assertIn('rerun with --candidate HOST', text)
        self.assertIn('Save this membership later with pulsar topology configure.', text)

    # TUI-83: the review lists added addresses, removes one on request, and
    # never adds one twice.
    def test_review_lists_removes_and_deduplicates_added_addresses(self):
        f = self.fresh_fixture(nodes=3)
        mapping = json.loads((f.root/'map.json').read_text())
        mapping['192.0.2.12'] = 2
        (f.root/'map.json').write_text(json.dumps(mapping))
        self.use_gum(TOPOLOGY_GUM_REVIEW='1,1,1,2,5', TOPOLOGY_GUM_CHOICE='0',
                     TOPOLOGY_GUM_INPUT='alias-1,alias-1,192.0.2.12')
        result, output = self.interactive(['topology', 'setup'], '', '')
        self.assertEqual(result, 1, output)
        flattened = ' '.join(output.split())
        self.assertIn('alias-1 is already added.', flattened)
        self.assertIn('Added: alias-1, 192.0.2.12', flattened)
        # After the removal only the other address is listed.
        self.assertIn('Added: 192.0.2.12 ', flattened.rsplit('Added: alias-1, 192.0.2.12', 1)[1] + ' ')
        # Gum receives LABEL<tab>VALUE; compare the labels.
        menus = [(header, [option.split('\t')[0] for option in options]) for header, options in
                 (json.loads(line) for line in (f.root/'menus.jsonl').read_text().splitlines())]
        self.assertIn(('Remove which added address?', ['alias-1', '192.0.2.12', 'Back']), menus)
        review = [options for header, options in menus if header == 'Review discovered machines']
        self.assertNotIn('Remove an added address', review[0])
        self.assertIn('Remove an added address', review[1])
        # The duplicate reran nothing: setup, two adds and one removal discover.
        self.assertEqual(sum(tool == 'avahi-browse' for tool, _ in f.calls()), 4)
        self.assertFalse(f.path.exists())

    def test_missing_peer_help_preserves_strict_ssh_and_does_not_guess_cause(self):
        f = self.fresh_fixture()
        self.use_gum(TOPOLOGY_UNREACHABLE='1', TOPOLOGY_GUM_REVIEW='4,3,5')
        result, output = self.interactive(['topology', 'setup', '--candidate', 'alias-1'], '', '')
        self.assertEqual(result, 1, output)
        flattened = ' '.join(output.split())
        self.assertIn('cannot reliably tell these apart', flattened)
        self.assertIn('ssh -o StrictHostKeyChecking=ask HOST', flattened)
        self.assertIn('independently trusted source', flattened)
        self.assertIn('does not establish initial OpenSSH trust', flattened)
        self.assertFalse(f.path.exists()); self.assertFalse(f.config.exists())
        ssh = [args for tool, args in f.calls() if tool == 'ssh']
        self.assertTrue(ssh)
        self.assertTrue(all('StrictHostKeyChecking=yes' in args for args in ssh))
        self.assertFalse(any('StrictHostKeyChecking=accept-new' in args for _, args in f.calls()))
        self.assertEqual(self.gum_prompts('confirm'), [])

    def test_existing_membership_add_uses_configure_and_never_auto_enrolls(self):
        for answer in ('0', '1'):
            with self.subTest(save=answer):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                self.fixture = f = Fixture(Path(temporary.name))
                before = f.path.read_bytes(), f.config.read_bytes()
                probe = node_probe(2)
                (f.root/'probe-2.json').write_text(json.dumps(probe))
                mapping = json.loads((f.root/'map.json').read_text())
                mapping['alias-2'] = 2
                (f.root/'map.json').write_text(json.dumps(mapping))
                self.use_gum(TOPOLOGY_GUM_CHOICE='4,5', TOPOLOGY_GUM_CHANGE='1', TOPOLOGY_GUM_REVIEW='1,0',
                             TOPOLOGY_GUM_INPUT='alias-2', TOPOLOGY_CONFIRM_RC=answer)
                result, output = self.interactive(['topology', 'menu'], '', '')
                self.assertEqual(result, 0, output)
                self.assertEqual(self.gum_prompts('confirm'),
                                 ['Save cluster membership with 3 machines?'])
                frames = output.replace('\r', '').count('Saved membership (no probes)')
                if answer == '0':
                    saved = json.loads(f.path.read_text())
                    self.assertEqual(len(saved['nodes']), 3)
                    self.assertEqual(saved['schema_version'], 1)
                    # The frame is drawn again because membership changed.
                    self.assertEqual(frames, 2)
                    self.assertIn('fixture-rank-2', output.rsplit('Saved membership (no probes)', 1)[1])
                else:
                    self.assertEqual(before, (f.path.read_bytes(), f.config.read_bytes()))
                    self.assertEqual(frames, 1)
                self.assertNotIn('topology-bound SSH identity enrolled', output)
                self.assertFalse(any('--include-ssh-host-keys' in ' '.join(args) for _, args in f.calls()))

    def test_only_doctor_asks_nodes_for_their_prerequisites(self):
        # Discovery, the membership check and configure never ask for the
        # prerequisites, so their Hub request never runs there.
        f = self.fixture
        for args in (('topology', 'check', '--json'), ('topology', 'detect', '--json'),
                     ('topology', 'configure', '--yes')):
            f.run(*args)
        self.assertTrue(f.calls())
        self.assertFalse(any('--prerequisites-only' in ' '.join(args) for _, args in f.calls()))
        # The shared probe helper passes the flag to a confirmed node only when asked.
        f.log.unlink()
        script = (f'. {ROOT}/scripts/lib.sh; load_cluster_topology; '
                  'probe_node_json_for_rank 1 --prerequisites-only >/dev/null; probe_node_json_for_rank 1 >/dev/null')
        result = subprocess.run(['bash', '-c', script], env=f.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [args[-1] for tool, args in f.calls() if tool == 'ssh']
        self.assertEqual(len(commands), 2)
        self.assertTrue(commands[0].startswith('python3 - --ssh-host alias-1 --expected-gpu'), commands[0])
        self.assertTrue(commands[0].endswith(' --prerequisites-only'), commands[0])
        self.assertEqual(commands[1], commands[0][:-len(' --prerequisites-only')])

    # Phase 1.2: every discovery against valid saved membership saves what it saw.
    def changes(self):
        path = self.fixture.root/'state/cluster-changes.json'
        record = json.loads(path.read_text())
        self.assertEqual((record['schema_version'], record['kind']), (1, 'pulsar-cluster-changes'))
        self.assertIsNotNone(datetime.fromisoformat(record['observed_at'].replace('Z', '+00:00')).tzinfo)
        return record

    def add_machine(self, rank, alias=None, probe=None):
        """A qualified machine discovery can reach at ALIAS that is not a member."""
        f = self.fixture
        probe = probe or node_probe(rank)
        (f.root/f'probe-{rank}.json').write_text(json.dumps(probe))
        mapping = json.loads((f.root/'map.json').read_text())
        mapping[alias or f'alias-{rank}'] = rank
        (f.root/'map.json').write_text(json.dumps(mapping))

    def test_unchanged_discovery_saves_an_unchanged_record_without_changing_its_output(self):
        f = self.fixture
        before = f.path.read_bytes()
        result = f.run('topology', 'detect', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.changes()
        self.assertEqual(record['result'], 'unchanged')
        self.assertEqual(record['topology_id'], f.topology['topology_id'])
        self.assertEqual([record[key] for key in ('issues', 'found', 'missing', 'changed', 'untrusted')], [[]] * 5)
        # detect --json carries the same body; nothing else changed.
        document = json.loads(result.stdout)
        self.assertEqual(document['changes_noticed'], {key: value for key, value in record.items()
                                                       if key not in ('schema_version', 'kind', 'observed_at')})
        self.assertEqual(document['membership_changes'], {'added': [], 'removed': [], 'changed': []})
        self.assertEqual(f.path.read_bytes(), before)
        self.assertFalse((ROOT/'.pulsar/cluster-changes.json').exists())

    def test_added_missing_changed_and_untrusted_machines_are_recorded(self):
        f = self.fixture
        self.add_machine(2)
        result = f.run('topology', 'detect', '--json', '--candidate', 'alias-2')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.changes()
        self.assertEqual(record['result'], 'changed')
        self.assertEqual(record['found'], [{'hostname': 'fixture-rank-2', 'node_id': 'fixture-node-2',
                                            'ssh_host': 'alias-2', 'possible_reinstall': False}])
        # A member's control address and links changed: old against new.
        probe = dict(f.probes[1], control=dict(interface='mgmt1', ip='192.0.2.99'),
                     rdma=[dict(hca='roce1', netdev='fabric1', cidrs=['198.51.100.11/24'])])
        Path(f.files[1]).write_text(json.dumps(probe))
        f.run('topology', 'detect', '--json')
        changed = self.changes()['changed']
        self.assertEqual([row['hostname'] for row in changed], ['fixture-rank-1'])
        self.assertEqual(changed[0]['changes']['control'], {'old': {'interface': 'mgmt0', 'ip': '192.0.2.11'},
                                                            'new': {'interface': 'mgmt1', 'ip': '192.0.2.99'}})
        self.assertEqual(changed[0]['changes']['links']['new'],
                         [{'hca': 'roce1', 'netdev': 'fabric1', 'cidrs': ['198.51.100.11/24']}])
        self.assertNotIn('position', changed[0]['changes'])
        # A member that did not answer, and a candidate SSH does not trust.
        Path(f.files[1]).write_text(json.dumps(f.probes[1]))
        f.env.update(TOPOLOGY_UNREACHABLE='1', TOPOLOGY_UNTRUSTED='host-9.local')
        result = f.run('topology', 'detect', '--json', '--candidate', 'host-9.local')
        self.assertNotEqual(result.returncode, 0)
        record = self.changes()
        self.assertEqual(record['result'], 'changed')
        self.assertEqual(record['missing'], [{'hostname': 'fixture-rank-1', 'node_id': 'fixture-node-1',
                                              'position': 1, 'answered': False}])
        self.assertEqual(record['untrusted'], [{'address': 'host-9.local', 'possible_reinstall': False}])
        self.assertEqual(json.loads(result.stdout)['saved_membership']['missing'][0]['answered'], False)

    def test_untrusted_candidates_alone_do_not_make_a_change(self):
        f = self.fixture
        f.env['TOPOLOGY_UNTRUSTED'] = 'printer.local'
        result = f.run('topology', 'detect', '--candidate', 'printer.local', '--candidate', 'gone.local')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.changes()
        self.assertEqual(record['result'], 'unchanged')
        # An address that did not answer at all is not listed: it was never seen.
        self.assertEqual(record['untrusted'], [{'address': 'printer.local', 'possible_reinstall': False}])
        # The human output is unchanged by the record.
        self.assertIn('SSH 2 advertised addresses could not be checked with saved keys', ' '.join(result.stdout.split()))

    def test_a_reinstalled_machine_is_one_node_gone_and_one_new(self):
        f = self.fixture
        # The member's machine ID changed: the old node did not answer, and a
        # new node answers at its address with its hostname.
        Path(f.files[1]).write_text(json.dumps(dict(f.probes[1], node_id='fixture-node-1-reinstalled')))
        f.run('topology', 'detect', '--json')
        record = self.changes()
        self.assertEqual(record['result'], 'changed')
        self.assertEqual([row['node_id'] for row in record['missing']], ['fixture-node-1'])
        self.assertEqual(record['missing'][0]['answered'], False)
        self.assertEqual(record['found'], [{'hostname': 'fixture-rank-1', 'node_id': 'fixture-node-1-reinstalled',
                                            'ssh_host': 'alias-1', 'possible_reinstall': True}])
        # Reinstalled with new host keys: SSH here refuses it under its mDNS name.
        Path(f.files[1]).write_text(json.dumps(f.probes[1]))
        f.env.update(TOPOLOGY_UNREACHABLE='1', TOPOLOGY_UNTRUSTED='fixture-rank-1.local,192.0.2.11')
        f.run('topology', 'detect', '--candidate', 'fixture-rank-1.local', '--candidate', '192.0.2.11')
        self.assertEqual(self.changes()['untrusted'], [
            {'address': '192.0.2.11', 'possible_reinstall': True},
            {'address': 'fixture-rank-1.local', 'possible_reinstall': True}])

    def test_failed_discovery_is_recorded_as_not_checked(self):
        f = self.fixture
        for mode in ('fabric', 'local', 'access'):
            with self.subTest(mode=mode):
                f.env.pop('TOPOLOGY_PING_RC', None)
                Path(f.files[0]).write_text(json.dumps(f.probes[0]))
                if mode == 'fabric':
                    f.env['TOPOLOGY_PING_RC'] = '1'
                    issue = 'Pairwise RoCE connectivity failed.'
                elif mode == 'local':
                    Path(f.files[0]).write_text(json.dumps(dict(f.probes[0], qualified=False,
                                                                reject_reasons=['synthetic reason'])))
                    issue = 'fixture-rank-0: synthetic reason'
                else:
                    f.config.write_text('# stale\n')
                    issue = 'Saved SSH configuration is unusable.'
                result = f.run('topology', 'detect', '--json')
                self.assertNotEqual(result.returncode, 0)
                record = self.changes()
                self.assertEqual(record['result'], 'not-checked')
                self.assertTrue(record['issues'][0].startswith(issue), record['issues'])
                self.assertEqual(json.loads(result.stdout)['changes_noticed']['result'], 'not-checked')
                if mode != 'fabric':
                    # Nothing was compared, so no member is listed as missing.
                    self.assertEqual(record['missing'], [])

    def test_no_record_without_valid_saved_membership_and_a_save_rewrites_it_unchanged(self):
        f = self.fixture
        path = f.root/'state/cluster-changes.json'
        for contents in (None, '{broken'):
            if contents is None: f.path.unlink()
            else: f.path.write_text(contents)
            f.run('topology', 'detect', '--json')
            self.assertFalse(path.exists())
        f.path.write_text(json.dumps(f.topology))
        self.add_machine(2)
        f.run('topology', 'detect', '--json', '--candidate', 'alias-2')
        self.assertEqual(self.changes()['result'], 'changed')
        result = f.run('topology', 'configure', '--yes', '--candidate', 'alias-2')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.changes()
        saved = json.loads(f.path.read_text())
        self.assertEqual((record['result'], record['topology_id']), ('unchanged', saved['topology_id']))
        self.assertEqual(record['found'], [])
        # A record that cannot be saved warns and changes nothing else.
        path.unlink(); (f.root/'state/cluster-changes.json').mkdir()
        result = f.run('topology', 'detect', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('the cluster changes noticed was not saved', result.stderr)
        self.assertEqual(json.loads(result.stdout)['changes_noticed']['result'], 'unchanged')

    # Phase 1.3: saving membership without a confirmed node that did not answer.
    def three_nodes(self, enrolled=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = Fixture(Path(temporary.name), nodes=3, enrolled=enrolled)
        return self.fixture

    def saved_nodes(self):
        return [(node['rank'], node['node_id']) for node in json.loads(self.fixture.path.read_text())['nodes']]

    def test_without_the_flag_a_missing_node_still_blocks_and_the_refusal_names_it(self):
        f = self.three_nodes()
        before = f.path.read_bytes()
        f.env['TOPOLOGY_UNREACHABLE'] = '1'
        result = f.run('topology', 'configure', '--yes')
        self.assertEqual(result.returncode, 1, result.stdout)
        text = ' '.join(result.stdout.split())
        self.assertIn('Missing confirmed node: fixture-rank-1 (position 1)', text)
        self.assertIn('To save membership without the nodes that did not answer, run '
                      './pulsar topology configure --remove-missing.', text)
        self.assertEqual(f.path.read_bytes(), before)
        # Only configure takes the flag.
        for action in ('detect', 'check', 'show', 'setup'):
            self.assertEqual(f.run('topology', action, '--remove-missing').returncode, 2)
        self.assertEqual(f.path.read_bytes(), before)

    def test_remove_missing_drops_only_the_node_that_did_not_answer_and_keeps_the_order(self):
        f = self.three_nodes()
        f.env['TOPOLOGY_UNREACHABLE'] = '1'
        result = f.run('topology', 'configure', '--yes', '--remove-missing')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        # The remaining nodes keep their relative order; positions close up.
        self.assertEqual(self.saved_nodes(), [(0, 'fixture-node-0'), (1, 'fixture-node-2')])
        text = ' '.join(result.stdout.split())
        self.assertIn('Left out fixture-rank-1, which did not answer.', text)
        self.assertIn('Pulsar cannot ask fixture-rank-1 whether a Pulsar service runs there. It saves without '
                      'it only because --remove-missing was given.', text)
        # The storage impact names the node that is left out, before the save.
        self.assertIn('Removed nodes fixture-rank-1', text)
        self.assertLess(text.index('Managed storage impact'), text.index('Left out'))
        # The answering nodes were asked for running services, before and after
        # the question; the node left out was asked too, and did not answer.
        docker = [args[args.index('--')+1] for tool, args in f.calls()
                  if tool == 'ssh' and args[-1].startswith('docker ps ')]
        self.assertEqual(docker.count('alias-2'), 4)
        self.assertEqual(docker.count('alias-1'), 2)
        self.assertEqual(self.changes()['result'], 'unchanged')

    def test_remove_missing_asks_a_question_that_names_the_node_and_starts_on_no(self):
        f = self.three_nodes()
        before = f.path.read_bytes()
        self.use_gum(TOPOLOGY_UNREACHABLE='1', TOPOLOGY_CONFIRM_RC='1')
        result, output = self.interactive(['topology', 'configure', '--remove-missing'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual(self.gum_prompts('confirm'),
                         ['Save cluster membership with 2 machines, without fixture-rank-1, which did not answer?'])
        confirms = [args for tool, args in f.calls() if tool == 'gum' and args[:1] == ['confirm']]
        self.assertIn('--default=false', confirms[0])
        self.assertEqual(f.path.read_bytes(), before)

    def test_remove_missing_never_drops_a_node_that_answered(self):
        f = self.three_nodes()
        before = f.path.read_bytes()
        # The member answered, but no longer qualifies: it is missing from the
        # proposed membership, yet the flag cannot leave it out.
        Path(f.files[1]).write_text(json.dumps(dict(f.probes[1], qualified=False, reject_reasons=['GPU is missing'])))
        result = f.run('topology', 'configure', '--yes', '--remove-missing')
        self.assertEqual(result.returncode, 1, result.stdout)
        text = ' '.join(result.stdout.split())
        self.assertIn('fixture-rank-1 answered but cannot be a member: GPU is missing. --remove-missing leaves out '
                      'only nodes that did not answer; fix it first.', text)
        self.assertNotIn('run ./pulsar topology configure --remove-missing', text)
        self.assertEqual(f.path.read_bytes(), before)
        # A node that did not answer discovery but answers the service query is
        # not left out either: discovery must see it again first.
        Path(f.files[1]).write_text(json.dumps(f.probes[1]))
        f.env['TOPOLOGY_PROBE_UNREACHABLE'] = '1'
        result = f.run('topology', 'configure', '--yes', '--remove-missing')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('fixture-rank-1 answers now; run discovery again', result.stderr)
        self.assertEqual(f.path.read_bytes(), before)

    def test_remove_missing_keeps_the_running_service_guard_on_nodes_that_answer(self):
        f = self.three_nodes()
        before = f.path.read_bytes()
        f.env.update(TOPOLOGY_UNREACHABLE='1', TOPOLOGY_ACTIVE='2')
        result = f.run('topology', 'configure', '--yes', '--remove-missing')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('has running stack-managed containers', result.stderr)
        self.assertEqual(f.path.read_bytes(), before)
        # A service started during the question still blocks the save.
        f.env.pop('TOPOLOGY_ACTIVE')
        (f.root/'docker-count').unlink(missing_ok=True)
        self.use_gum(TOPOLOGY_ACTIVE_AFTER='4', TOPOLOGY_CONFIRM_RC='0')
        result, output = self.interactive(['topology', 'configure', '--remove-missing'], '', '')
        self.assertNotEqual(result, 0, output)
        self.assertEqual(f.path.read_bytes(), before)

    def test_adding_a_machine_and_a_changed_address_save_when_every_member_answers(self):
        f = self.fixture
        self.add_machine(2)
        result = f.run('topology', 'configure', '--yes', '--candidate', 'alias-2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.saved_nodes(), [(0, 'fixture-node-0'), (1, 'fixture-node-1'), (2, 'fixture-node-2')])
        probe = dict(f.probes[1], control=dict(interface='mgmt0', ip='192.0.2.99'))
        Path(f.files[1]).write_text(json.dumps(probe))
        result = f.run('topology', 'configure', '--yes', '--candidate', 'alias-2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(f.path.read_text())['nodes'][1]['control']['ip'], '192.0.2.99')

    def test_a_membership_save_drops_every_recorded_ssh_identity(self):
        # Find-out (1.3): a save writes membership without SSH identities, so
        # every node, not only a new one, is enrolled again afterwards.
        f = self.fixture
        enrolled = json.loads(f.path.read_text())
        self.assertTrue(all(node.get('ssh_host_keys') for node in enrolled['nodes']))
        self.add_machine(2)
        self.assertEqual(f.run('topology', 'configure', '--yes', '--candidate', 'alias-2').returncode, 0)
        saved = json.loads(f.path.read_text())
        self.assertEqual(saved['schema_version'], 1)
        self.assertFalse(any(node.get('ssh_host_keys') for node in saved['nodes']))
        result = f.run('ssh-trust', 'enroll', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        states = [line.split()[-1] for line in result.stdout.splitlines() if line.strip().startswith('State')]
        self.assertEqual(states, ['new', 'new', 'new'])
        # Enrollment changes the topology ID again, so the changes record from
        # the save now names an earlier membership.
        self.assertNotEqual(json.loads(f.path.read_text())['topology_id'], saved['topology_id'])
        self.assertEqual(self.changes()['topology_id'], saved['topology_id'])

    # Phase 1.4: forget this cluster.
    def cluster_state(self):
        """Saved observations beside the fixture's membership and SSH config."""
        f = self.fixture
        state = f.root/'state'; state.mkdir(exist_ok=True)
        for name in ('service-inventory', 'storage-budget', 'cluster-changes'):
            (state/f'{name}.json').write_text(json.dumps({'fixture': name}))
        (f.root/'model-library').mkdir(exist_ok=True)
        (f.root/'model-library/record.json').write_text('{"kept": true}')
        (f.root/'overlay.json').write_text(json.dumps({'schema_version': 1, 'kind': 'pulsar-deployment-overlay',
            'defaults': {'port': 8000, 'placement': {'node_id': 'fixture-node-1'}}, 'specs': {}}))
        return {path: path.read_bytes() for path in (f.path, f.config, *sorted(state.glob('*.json')))}

    def assert_kept(self, files):
        for path, contents in files.items():
            self.assertEqual(path.read_bytes(), contents, path)
        self.assertFalse((self.fixture.root/'state/cluster-backups').exists())

    def test_forget_plan_lists_what_is_removed_and_kept_and_changes_nothing(self):
        f = self.fixture
        files = self.cluster_state()
        result = f.run('topology', 'forget', '--plan', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual((plan['kind'], plan['result'], plan['membership']), ('pulsar-topology-forget', 'planned', 'saved'))
        self.assertEqual([item['name'] for item in plan['remove']],
                         ['membership', 'ssh-config', 'service-inventory', 'storage-budget', 'cluster-changes'])
        self.assertTrue(all(item['present'] for item in plan['remove']))
        self.assertEqual([item['path'] for item in plan['remove']][:2], [str(f.path), str(f.config)])
        self.assertEqual(plan['services'], [{'hostname': 'fixture-rank-0', 'state': 'none'},
                                            {'hostname': 'fixture-rank-1', 'state': 'none'}])
        # The site overlay can name a node for placement; the plan says which.
        self.assertEqual(plan['overlay']['names'], [{'node_id': 'fixture-node-1', 'hostname': 'fixture-rank-1'}])
        human = f.run('topology', 'forget', '--plan')
        self.assertEqual(human.returncode, 0, human.stderr)
        text = ' '.join(human.stdout.split())
        for words in ('Membership saved: fixture-rank-0, fixture-rank-1',
                      'Services none running on fixture-rank-0 and fixture-rank-1',
                      'Removes Saved membership', 'Generated SSH configuration', 'Cluster changes noticed',
                      "Keeps .env settings", "OpenSSH's own known_hosts",
                      'The site overlay names fixture-rank-1 for placement; it still does after forgetting.',
                      'Result Plan only. No files changed.'):
            self.assertIn(words, text)
        self.assertLessEqual(max(map(len, human.stdout.splitlines())), 48)
        self.assert_kept(files)
        self.assertEqual(set(tool for tool, _ in f.calls()), {'ssh', 'docker'})

    def test_forget_refuses_while_a_service_runs_on_a_node_that_answers(self):
        f = self.fixture
        files = self.cluster_state()
        f.env['TOPOLOGY_ACTIVE'] = '1'
        result = f.run('topology', 'forget', '--yes')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('a Pulsar service runs on fixture-rank-1; stop it first', ' '.join(result.stdout.split()))
        self.assertIn('No files changed.', result.stderr)
        self.assert_kept(files)
        plan = f.run('topology', 'forget', '--plan')
        self.assertEqual(plan.returncode, 0)
        self.assertIn('Forget would refuse while a Pulsar service runs.', ' '.join(plan.stdout.split()))

    def test_forget_backs_up_then_removes_and_setup_starts_again(self):
        f = self.fixture
        files = self.cluster_state()
        # A member that cannot be asked is named and does not block.
        f.env['TOPOLOGY_UNREACHABLE'] = '1'
        result = f.run('topology', 'forget', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        text = ' '.join(result.stdout.split())
        self.assertIn('fixture-rank-1 not observed: it could not be asked; it does not block forgetting', text)
        for path in files:
            self.assertFalse(path.exists(), path)
        backups = list((f.root/'state/cluster-backups').iterdir())
        self.assertEqual(len(backups), 1)
        backup = backups[0]
        self.assertRegex(backup.name, r'^forgotten-cluster-\d{8}T\d{6}Z$')
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertIn(f'Backup{backup}', ''.join(result.stdout.split()))
        for path, contents in files.items():
            copy = backup/path.name
            self.assertEqual(copy.read_bytes(), contents)
            self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        listed = json.loads((backup/'forgotten.json').read_text())
        self.assertEqual({item['path'] for item in listed['files']}, {str(path) for path in files})
        # Kept: model-library records and the overlay.
        self.assertEqual((f.root/'model-library/record.json').read_text(), '{"kept": true}')
        self.assertTrue((f.root/'overlay.json').exists())
        self.assertIn('Next ./pulsar topology setup', text)
        # The setup status reads as before first use.
        status = subprocess.run([sys.executable, str(ROOT/'scripts/setup_status.py'), '--format', 'json'],
                                env=f.env, text=True, capture_output=True, timeout=30)
        document = json.loads(status.stdout)
        self.assertEqual((document['topology']['status'], document['next_action'], document['complete']),
                         ('missing', 'set-up-topology', False))
        # A second forget has nothing to do.
        again = f.run('topology', 'forget', '--yes')
        self.assertEqual(again.returncode, 0)
        self.assertIn('Nothing to forget', again.stdout)
        self.assertEqual(len(list((f.root/'state/cluster-backups').iterdir())), 1)

    def test_forget_asks_on_a_terminal_and_needs_yes_off_one(self):
        f = self.fixture
        files = self.cluster_state()
        result = f.run('topology', 'forget')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('./pulsar topology forget --yes', ' '.join(result.stderr.split()))
        self.assertEqual(f.calls(), [])
        self.assert_kept(files)
        self.use_gum(TOPOLOGY_CONFIRM_RC='1')
        code, output = self.interactive(['topology', 'forget'], '', '')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.gum_prompts('confirm'), ["Forget this cluster's saved membership and observations?"])
        confirms = [args for tool, args in f.calls() if tool == 'gum' and args[:1] == ['confirm']]
        self.assertIn('--default=false', confirms[0])
        self.assert_kept(files)
        self.use_gum(TOPOLOGY_CONFIRM_RC='1,0')
        code, output = self.interactive(['topology', 'forget'], '', '')
        self.assertEqual(code, 0, output)
        self.assertFalse(f.path.exists())

    def test_forget_with_unusable_saved_state_checks_only_this_machine(self):
        for broken in ('membership', 'ssh-config'):
            with self.subTest(broken=broken):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                self.fixture = f = Fixture(Path(temporary.name))
                self.cluster_state()
                (f.path if broken == 'membership' else f.config).write_text('{broken')
                result = f.run('topology', 'forget', '--plan', '--json')
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads(result.stdout)
                self.assertEqual(plan['membership'], 'invalid' if broken == 'membership' else 'saved')
                self.assertTrue(plan['checked_only_here'])
                self.assertEqual(len(plan['services']), 1)
                # Never another SSH trust: no node but this one is asked.
                self.assertFalse(any(tool == 'ssh' for tool, _ in f.calls()))
                human = f.run('topology', 'forget', '--yes')
                self.assertEqual(human.returncode, 0, human.stderr + human.stdout)
                self.assertIn('Only this machine was checked', ' '.join(human.stdout.split()))
                self.assertFalse(f.path.exists()); self.assertFalse(f.config.exists())
                self.assertEqual(f.run('topology', 'show', '--json').returncode, 1)

    def test_invalid_membership_names_forget_instead_of_a_hand_repair(self):
        f = self.fixture
        f.path.write_text('{broken')
        result = f.run('topology', 'show')
        text = ' '.join(result.stdout.split())
        self.assertIn('To start over, run ./pulsar topology forget; it keeps a private backup copy.', text)
        self.assertNotIn('by hand', text)

    def test_forget_in_a_test_needs_every_path_named_and_never_removes_a_changed_file(self):
        f = self.fixture
        files = self.cluster_state()
        env = dict(f.env); env.pop('PULSAR_CLUSTER_BACKUP_DIR')
        result = subprocess.run([str(ROOT/'pulsar'), 'topology', 'forget', '--yes'], env=env, text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assert_kept(files)
        sys.path.insert(0, str(ROOT))
        from scripts import topology_forget
        with unittest.mock.patch.dict(os.environ, f.env):
            plan = topology_forget.plan(ROOT, f.path, f.config, f.root/'overlay.json')
            f.path.write_text(json.dumps(f.topology) + '\n')
            with self.assertRaisesRegex(ValueError, 'changed since the plan'):
                topology_forget.apply(plan, f.root/'state/cluster-backups')
        self.assertTrue(f.config.exists())
        self.assertFalse((f.root/'state/cluster-backups').exists())

    def test_narrow_human_observation(self):
        f = self.fixture
        result = f.run('topology', 'show')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(max(map(len, result.stdout.splitlines())), 48)


if __name__ == '__main__': unittest.main()
