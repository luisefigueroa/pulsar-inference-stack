"""Catalog specs declare tool-choice and parser engine arguments."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release_spec.catalog_policy import CatalogPolicyError, check_required_engine_args

COMPLIANT = ['--gpu-memory-utilization', '0.8', '--enable-auto-tool-choice',
             '--tool-call-parser', 'example_tools', '--reasoning-parser', 'example_reasoning']


def spec_with(engine_args):
    return {'schema_version': 2, 'recipe': {'engine_args': engine_args}}


class RequiredEngineArgs(unittest.TestCase):
    def assert_missing(self, engine_args, missing):
        with self.assertRaisesRegex(CatalogPolicyError, r'^example\.json: .*missing: ' + missing + '$'):
            check_required_engine_args(spec_with(engine_args), label='example.json')

    def test_compliant_spec_passes(self):
        check_required_engine_args(spec_with(COMPLIANT), label='example.json')

    def test_each_missing_flag_is_named(self):
        for flag, width in (('--enable-auto-tool-choice', 1), ('--tool-call-parser', 2), ('--reasoning-parser', 2)):
            with self.subTest(flag=flag):
                index = COMPLIANT.index(flag)
                self.assert_missing(COMPLIANT[:index] + COMPLIANT[index + width:], flag)

    def test_all_missing_flags_are_reported_together(self):
        self.assert_missing(['--gpu-memory-utilization', '0.8'],
                            '--enable-auto-tool-choice, --tool-call-parser, --reasoning-parser')

    def test_parser_without_value_fails(self):
        base = ['--enable-auto-tool-choice', '--reasoning-parser', 'example_reasoning']
        for tail in (['--tool-call-parser'], ['--tool-call-parser', ''], ['--tool-call-parser=']):
            with self.subTest(tail=tail):
                self.assert_missing(base + tail, '--tool-call-parser value')
        self.assert_missing(['--tool-call-parser', '--enable-auto-tool-choice',
                             '--reasoning-parser', 'example_reasoning'], '--tool-call-parser value')

    def test_tool_choice_switch_takes_no_value(self):
        # vLLM's --enable-auto-tool-choice is a switch; a stray value breaks its argument parsing.
        parsers = ['--tool-call-parser', 'example_tools', '--reasoning-parser', 'example_reasoning']
        for switch in (['--enable-auto-tool-choice=false'], ['--enable-auto-tool-choice=true'],
                       ['--enable-auto-tool-choice='], ['--enable-auto-tool-choice', 'false']):
            with self.subTest(switch=switch):
                self.assert_missing(switch + parsers, '--enable-auto-tool-choice as a switch without a value')
        check_required_engine_args(spec_with(parsers + ['--enable-auto-tool-choice']), label='example.json')

    def test_assignment_form_is_accepted(self):
        check_required_engine_args(spec_with([
            '--enable-auto-tool-choice', '--tool-call-parser=example_tools',
            '--reasoning-parser=example_reasoning']), label='example.json')

    def test_spec_without_recipe_token_list_is_rejected(self):
        with self.assertRaises(CatalogPolicyError):
            check_required_engine_args({'schema_version': 1, 'identity': {'engine_args': COMPLIANT}},
                                       label='example.json')


if __name__ == '__main__':
    unittest.main()
