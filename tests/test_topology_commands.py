"""Real command routing with synthetic nodes; never contacts serving hardware."""
import json
import os
import pty
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest

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
        self.assertIn('Missing confirmed node 1', result.stdout)
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
        output = b''; deadline = time.monotonic()+25
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
            process.wait(timeout=2)
        finally:
            if process.poll() is None: process.kill(); process.wait()
        return process.returncode, output.decode(errors='replace')

    def use_gum(self, **answers):
        """Draw menus and confirmations with the fake Gum on the test terminal."""
        self.fixture.env.update(TERM='xterm-256color', GUM_BIN=str(self.fixture.root/'bin/gum'), **answers)

    def gum_prompts(self, kind):
        """Headers of Gum menus or questions of Gum confirmations, in order."""
        return [args[args.index('--header')+1] if kind == 'choose' else args[-1]
                for tool, args in self.fixture.calls() if tool == 'gum' and args[:1] == [kind]]

    def probes(self):
        return [tool for tool, _ in self.fixture.calls() if tool != 'gum']

    def test_menu_back_does_not_probe_and_cancel_does_not_save(self):
        f = self.fixture
        self.use_gum(TOPOLOGY_GUM_CHOICE='7', TOPOLOGY_CONFIRM_RC='1')
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
        self.use_gum(TOPOLOGY_GUM_CHOICE='1,7')
        result, output = self.interactive(['topology', 'menu'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual(self.probes(), [])
        self.assertEqual(self.gum_prompts('choose'), ['Cluster topology', 'Cluster topology'])
        self.assertIn('fixture-rank-1', (f.root/'pager.txt').read_text())

    def test_menus_without_gum_name_commands_and_do_not_probe(self):
        f = self.fixture  # TERM=dumb: Gum cannot draw
        for args, expected in ((['topology', 'menu'], 'the topology menu'), (['topology', 'setup'], 'guided setup')):
            result, output = self.interactive(args, '', '')
            self.assertEqual(result, 2, output)
            self.assertIn(f'error: {expected} needs an interactive terminal with Gum; use: pulsar topology', output)
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
        self.use_gum(TOPOLOGY_GUM_HOME='4,6', TOPOLOGY_GUM_CHOICE='7', PULSAR_COLD_ROOT=str(f.root))
        result, output = self.interactive(['gum'], '', '')
        self.assertEqual(result, 0, output)
        self.assertEqual([tool for tool, _ in f.calls()], ['gum', 'gum', 'gum'])

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
                self.assertIn('Cluster topology → Configure membership or add a machine', text)
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
        self.use_gum(TOPOLOGY_GUM_REVIEW='1,1,2,0',
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
        self.use_gum(TOPOLOGY_CONFIRM_RC='1', TOPOLOGY_GUM_REVIEW='1,0,2,4',
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
        self.use_gum(TOPOLOGY_CONCURRENT_SAVE='1', TOPOLOGY_CONFIRM_RC='1',
                     TOPOLOGY_GUM_REVIEW='0,4')
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
        self.use_gum(TOPOLOGY_ACTIVE='1', TOPOLOGY_GUM_REVIEW='0,4')
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

    def test_missing_peer_help_preserves_strict_ssh_and_does_not_guess_cause(self):
        f = self.fresh_fixture()
        self.use_gum(TOPOLOGY_UNREACHABLE='1', TOPOLOGY_GUM_REVIEW='3,2,4')
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
                self.use_gum(TOPOLOGY_GUM_CHOICE='4,7', TOPOLOGY_GUM_REVIEW='1,0',
                             TOPOLOGY_GUM_INPUT='alias-2', TOPOLOGY_CONFIRM_RC=answer)
                result, output = self.interactive(['topology', 'menu'], '', '')
                self.assertEqual(result, 0, output)
                self.assertEqual(self.gum_prompts('confirm'),
                                 ['Save cluster membership with 3 machines?'])
                if answer == '0':
                    saved = json.loads(f.path.read_text())
                    self.assertEqual(len(saved['nodes']), 3)
                    self.assertEqual(saved['schema_version'], 1)
                else:
                    self.assertEqual(before, (f.path.read_bytes(), f.config.read_bytes()))
                self.assertNotIn('topology-bound SSH identity enrolled', output)
                self.assertFalse(any('--include-ssh-host-keys' in ' '.join(args) for _, args in f.calls()))

    def test_narrow_human_observation(self):
        f = self.fixture
        result = f.run('topology', 'show')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(max(map(len, result.stdout.splitlines())), 48)


if __name__ == '__main__': unittest.main()
