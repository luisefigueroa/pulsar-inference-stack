"""The clean break applies to changed Git blobs, without rewriting history."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location('changed_catalog', ROOT/'scripts/check-new-specs.py')
checker = importlib.util.module_from_spec(loader)
loader.loader.exec_module(checker)


class ChangedCatalog(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.root/'releases').mkdir()
        historical=json.loads((ROOT/'release_spec/tests/fixtures/golden_measured.json').read_text())
        self.old = self.root/'releases'/(historical['spec_id']+'.json')
        self.old.write_text(json.dumps(historical))
        self.git('add', '.')
        self.git('commit', '-qm', 'Historical fixture')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.spec = json.loads((ROOT/'tests/fixtures/contracts/spec.json').read_text())
        self.path = self.root/'releases'/(self.spec['spec_id']+'.json')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True)

    def stage_current(self):
        self.path.write_text(json.dumps(self.spec))
        self.git('add', '.')

    def test_unchanged_history_is_ignored_and_index_is_authoritative(self):
        self.stage_current()
        self.path.write_text('broken worktree copy')
        self.assertEqual(checker.check(self.root, staged=True), 1)

    def test_selected_head_is_authoritative_not_worktree(self):
        self.stage_current()
        self.git('commit', '-qm', 'Current recipe')
        head = self.git('rev-parse', 'HEAD').strip()
        self.path.unlink()
        self.assertEqual(checker.check(self.root, base=self.base, head=head), 1)

    def test_changed_history_is_rejected(self):
        self.old.write_text('{"schema_version":1,"edited":true}')
        self.git('add', '.')
        with self.assertRaises(ValueError):
            checker.check(self.root, staged=True)

    def assert_removal_rejected(self, base):
        self.git('add','-A')
        with self.assertRaisesRegex(ValueError,'deletion or renaming'):
            checker.check(self.root,staged=True)
        self.git('commit','-qm','Removal fixture')
        with self.assertRaisesRegex(ValueError,'deletion or renaming'):
            checker.check(self.root,base=base)

    def test_historical_deletion_is_rejected_in_index_and_commit(self):
        self.old.unlink()
        self.assert_removal_rejected(self.base)

    def test_current_deletion_is_also_rejected(self):
        self.stage_current();self.git('commit','-qm','Current fixture')
        base=self.git('rev-parse','HEAD').strip()
        self.path.unlink()
        self.assert_removal_rejected(base)

    def test_renaming_history_outside_catalog_cannot_hide_removal(self):
        self.old.rename(self.root/'historical.json')
        self.assert_removal_rejected(self.base)

    def test_readme_removal_is_not_a_catalog_record_removal(self):
        path=self.root/'releases/README.md';path.write_text('Catalog documentation\n')
        self.git('add','.');self.git('commit','-qm','Documentation fixture')
        base=self.git('rev-parse','HEAD').strip()
        path.unlink();self.git('add','-A')
        self.assertEqual(checker.check(self.root,staged=True),0)
        self.git('commit','-qm','Remove documentation fixture')
        self.assertEqual(checker.check(self.root,base=base),0)

    def test_wrong_filename_and_symlink_are_rejected(self):
        self.stage_current()
        self.git('mv', str(self.path), str(self.old.with_name('e'*64+'.json')))
        with self.assertRaisesRegex(ValueError, 'filename'):
            checker.check(self.root, staged=True)
        self.old.with_name('e'*64+'.json').unlink()
        self.path.symlink_to(self.old.name)
        self.git('add', '.')
        with self.assertRaisesRegex(ValueError, 'regular file'):
            checker.check(self.root, staged=True)

    def test_pr_ignores_base_only_additions_but_rejects_topic_deletions(self):
        self.git('checkout','-qb','advanced-base')
        self.stage_current();self.git('commit','-qm','Base-only catalog addition')
        base=self.git('rev-parse','HEAD').strip()
        self.git('checkout','-qb','topic',self.base)
        (self.root/'topic.txt').write_text('Unrelated topic change\n')
        self.git('add','.');self.git('commit','-qm','Topic change')
        self.assertEqual(checker.check(self.root,base=base,merge_base=True),0)
        # Pushes compare actual before/after trees, including rewritten history.
        with self.assertRaisesRegex(ValueError,'deletion or renaming'):
            checker.check(self.root,base=base)
        self.old.unlink();self.git('add','-A');self.git('commit','-qm','Topic deletion')
        with self.assertRaisesRegex(ValueError,'deletion or renaming'):
            checker.check(self.root,base=base,merge_base=True)

    def write_ledger(self, *entries):
        ledger={'schema_version':1,'kind':'pulsar-catalog-removals','removals':list(entries)}
        (self.root/'catalog-removals.json').write_text(json.dumps(ledger))

    def removal(self, reason='Superseded by a faster recipe'):
        return {'spec_id':self.old.stem,'removed_at':'2026-09-26','reason':reason}

    def commit_evidence(self):
        evidence=self.root/'results/baseline-v2'/self.old.stem/'run-001'
        evidence.mkdir(parents=True);(evidence/'run.json').write_text('{}')
        self.git('add','.');self.git('commit','-qm','Evidence fixture')
        return self.git('rev-parse','HEAD').strip(),self.root/'results/baseline-v2'/self.old.stem

    def test_recorded_removal_with_its_evidence_is_accepted(self):
        base,evidence=self.commit_evidence()
        self.old.unlink();shutil.rmtree(evidence);self.write_ledger(self.removal())
        self.git('add','-A')
        self.assertEqual(checker.check(self.root,staged=True),0)
        self.git('commit','-qm','Remove recorded spec')
        self.assertEqual(checker.check(self.root,base=base),0)
        self.assertEqual(checker.check(self.root,base=base,merge_base=True),0)

    def test_recorded_removal_must_remove_evidence(self):
        base,_=self.commit_evidence()
        self.old.unlink();self.write_ledger(self.removal())
        self.git('add','-A')
        with self.assertRaisesRegex(ValueError,'still has results/ evidence'):
            checker.check(self.root,staged=True)
        self.git('commit','-qm','Remove spec but keep evidence')
        with self.assertRaisesRegex(ValueError,'still has results/ evidence'):
            checker.check(self.root,base=base)

    def test_ledger_for_another_spec_does_not_permit_removal(self):
        self.old.unlink()
        self.write_ledger({**self.removal(),'spec_id':'e'*64})
        self.assert_removal_rejected(self.base)

    def test_removal_entries_are_append_only(self):
        self.old.unlink();self.write_ledger(self.removal())
        self.git('add','-A');self.git('commit','-qm','Remove recorded spec')
        base=self.git('rev-parse','HEAD').strip()
        self.write_ledger(self.removal('Rewritten reason'));self.git('add','-A')
        with self.assertRaisesRegex(ValueError,'append-only'):
            checker.check(self.root,staged=True)
        (self.root/'catalog-removals.json').unlink();self.git('add','-A')
        with self.assertRaisesRegex(ValueError,'append-only'):
            checker.check(self.root,staged=True)
        self.git('commit','-qm','Drop ledger')
        with self.assertRaisesRegex(ValueError,'append-only'):
            checker.check(self.root,base=base)

    def test_malformed_ledger_is_rejected(self):
        self.old.unlink()
        for entry,message in (({**self.removal(),'removed_at':'2026-02-30'},'calendar date'),
                              ({**self.removal(),'reason':' '},'reason'),
                              ({**self.removal(),'extra':True},'exactly')):
            with self.subTest(message=message):
                self.write_ledger(entry);self.git('add','-A')
                with self.assertRaisesRegex(ValueError,message):
                    checker.check(self.root,staged=True)
        self.write_ledger(self.removal(),self.removal());self.git('add','-A')
        with self.assertRaisesRegex(ValueError,'more than once'):
            checker.check(self.root,staged=True)


if __name__ == '__main__':
    unittest.main()
