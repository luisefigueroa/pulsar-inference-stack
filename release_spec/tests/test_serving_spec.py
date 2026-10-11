"""Identity, overrides and historical boundaries for portable serving specs."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from release_spec import serving
from release_spec.schema import ReleaseSpecError, STATES

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts"


class ServingSpec(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads((FIXTURES / "spec.json").read_text())
        self.draft = json.loads((FIXTURES / "draft.json").read_text())
        self.manifest = json.loads((FIXTURES / "manifest.json").read_text())

    def test_named_manifests_and_engine_references_are_bound(self):
        from release_spec.normalize import snapshot_manifest_id
        self.draft['schema_version']=2
        second=copy.deepcopy(self.manifest);second['snapshot_revision']='e'*40
        second['manifest_id']=snapshot_manifest_id(second)
        self.draft['recipe']['required_snapshots']={'draft':{'model_id':second['model_id'],'model_commit':second['snapshot_revision']}}
        self.draft['recipe']['engine_args'] += ['--speculative-config',json.dumps({'model':'pulsar-snapshot:draft','num_speculative_tokens':3})]
        spec=serving.freeze(self.draft,{'target':self.manifest,'draft':second})
        self.assertEqual(spec['schema_version'],3)
        self.assertEqual(spec['state'],'candidate')
        self.assertIsNone(spec['review'])
        self.assertEqual(serving.verify_spec(spec),spec)
        for manifests in ({'target':self.manifest},{'target':self.manifest,'draft':self.manifest},
                          {'target':self.manifest,'draft':second,'extra':second}):
            with self.assertRaises(ValueError): serving.freeze(self.draft,manifests)
        bad_args=[['--speculative_config.model','example/unbound'],
                  ['--speculative_config.model','pulsar-snapshot:missing'],
                  ['--speculative_config.model','pulsar-snapshot:draft','--speculative-config.model','pulsar-snapshot:draft'],
                  ['--speculative-config','{"model":"pulsar-snapshot:draft","model":"example/unbound"}'],
                  ['--speculative-config','{"model":"pulsar-snapshot:draft"}','--speculative_config.num_speculative_tokens','3'],
                  ['--other','pulsar-snapshot:draft']]
        for args in bad_args:
            with self.subTest(args=args),self.assertRaises(ValueError):
                serving.apply_overrides(spec,{'engine_args':['--gpu-memory-utilization','0.8',*args]})
        changed=copy.deepcopy(spec['recipe']);changed['required_snapshots']['draft']['model_commit']='f'*40
        changed['required_snapshots']['draft']['snapshot_manifest']['snapshot_revision']='f'*40
        changed['required_snapshots']['draft']['snapshot_manifest']['manifest_id']=snapshot_manifest_id(changed['required_snapshots']['draft']['snapshot_manifest'])
        self.assertNotEqual(serving.spec_id(changed),spec['spec_id'])
        self.assertEqual(changed['model'],spec['recipe']['model'])

    def test_existing_non_checkpoint_speculation_keeps_exact_argument_tokens(self):
        for method in ('ngram', 'mtp'):
            with self.subTest(method=method):
                args=['--gpu-memory-utilization','0.8','--speculative-config',
                      '{ "num_speculative_tokens": 3, "method": "'+method+'" }']
                self.draft['recipe']['engine_args']=args
                spec=serving.freeze(self.draft,self.manifest)
                self.assertEqual(spec['schema_version'],2)
                self.assertEqual(serving.snapshot_engine_args(spec['recipe'],{'target':'/pulsar-test/target'}),args)
                recipe=copy.deepcopy(self.spec['recipe']);recipe['engine_args']=args
                payload=json.dumps({'schema_version':2,'recipe':recipe},sort_keys=True,
                                   separators=(',',':'),ensure_ascii=False).encode()
                self.assertEqual(spec['spec_id'],hashlib.sha256(payload).hexdigest())

    def checkpoint_subdirectory(self, locator, *, named=False, dotted=False):
        from release_spec import build_snapshot_manifest
        manifest = build_snapshot_manifest(model_id=self.manifest['model_id'],
            snapshot_revision=self.manifest['snapshot_revision'], files=[
                *self.manifest['files'],
                {'path': 'dflash/config.json', 'sha256': 'd'*64, 'size': 3},
                {'path': 'dflash/checkpoint/model.safetensors', 'sha256': 'e'*64, 'size': 5}])
        draft = copy.deepcopy(self.draft)
        draft['schema_version'] = 2
        draft['recipe']['required_snapshots'] = {'draft': copy.deepcopy(draft['recipe']['model'])} if named else {}
        draft['recipe']['engine_args'] += (['--speculative_config.model', locator] if dotted else
            ['--speculative-config', json.dumps({'model': locator, 'method': 'dflash'})])
        manifests = {'target': manifest, **({'draft': manifest} if named else {})}
        return draft, manifests

    def test_checkpoint_subdirectories_use_complete_bound_manifests(self):
        for named in (False, True):
            name = 'draft' if named else 'target'
            for dotted in (False, True):
                for suffix in ('dflash', 'dflash/checkpoint'):
                    with self.subTest(named=named, dotted=dotted, suffix=suffix):
                        locator = 'pulsar-snapshot:' + name + '/' + suffix
                        draft, manifests = self.checkpoint_subdirectory(locator, named=named, dotted=dotted)
                        spec = serving.freeze(draft, manifests)
                        self.assertEqual(serving.verify_spec(spec), spec)
                        self.assertEqual(spec['recipe']['model']['snapshot_manifest'], manifests['target'])
                        paths = {'target': '/bound/target', 'draft': '/bound/draft'}
                        resolved = serving.snapshot_engine_args(spec['recipe'], paths)
                        actual = resolved[-1] if dotted else json.loads(resolved[-1])['model']
                        self.assertEqual(actual, paths[name] + '/' + suffix)
                        if named:
                            self.assertEqual(spec['recipe']['required_snapshots']['draft']['snapshot_manifest'], manifests['target'])

    def test_checkpoint_subdirectories_reject_unsafe_or_unbound_paths(self):
        suffixes = ('', '/dflash', 'dflash/', 'dflash//checkpoint', '.', '..',
                    'dflash/.', 'dflash/..', '../dflash', 'dflash/../checkpoint',
                    'dflash\\checkpoint', 'dflash\x00', 'dflash\n', 'dflash\x7f',
                    'dflash/\u00e9', '%2e%2e', 'dflash/%2e%2e', '%2Fdflash',
                    'missing', 'dflash-other', 'config.json', 'dflash/config.json')
        for suffix in suffixes:
            for dotted in (False, True):
                with self.subTest(suffix=suffix, dotted=dotted):
                    draft, manifests = self.checkpoint_subdirectory('pulsar-snapshot:target/' + suffix, dotted=dotted)
                    with self.assertRaises(ValueError):
                        serving.freeze(draft, manifests)
        draft, manifests = self.checkpoint_subdirectory('pulsar-snapshot:missing/dflash')
        with self.assertRaisesRegex(ValueError, 'unknown required snapshot'):
            serving.freeze(draft, manifests)

    def test_checkpoint_subdirectory_is_not_a_manifest_subset_or_snapshot_alias(self):
        draft, manifests = self.checkpoint_subdirectory('pulsar-snapshot:target/dflash', named=True)
        with self.assertRaisesRegex(ValueError, 'snapshot has no supported engine reference'):
            serving.freeze(draft, manifests)
        draft, manifests = self.checkpoint_subdirectory('pulsar-snapshot:target/dflash')
        spec = serving.freeze(draft, manifests)
        changed = copy.deepcopy(draft)
        changed['recipe']['engine_args'][-1] = json.dumps({'model': 'pulsar-snapshot:target/dflash/checkpoint', 'method': 'dflash'})
        self.assertNotEqual(serving.freeze(changed, manifests)['spec_id'], spec['spec_id'])
        del changed['recipe']['engine_args'][-2:]
        changed['recipe']['engine_args'] += ['--other', 'pulsar-snapshot:target/dflash']
        with self.assertRaisesRegex(ValueError, 'outside a supported model field'):
            serving.freeze(changed, manifests)

    def test_golden_identity_binds_only_schema_and_complete_recipe(self):
        payload = json.dumps({"schema_version": 2, "recipe": self.spec["recipe"]},
                             sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertEqual(self.spec["spec_id"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(serving.freeze(self.draft, self.manifest), {**self.spec, "state": "candidate"})
        changed = copy.deepcopy(self.spec)
        changed.update(state="released", review={})
        changed["source"]["image_repository"] = "example/mirror"
        self.assertEqual(serving.verify_spec(changed)["spec_id"], self.spec["spec_id"])

    def test_current_states_are_independent_of_review_and_recipe_identity(self):
        review = {"status": "stable", "reviewer": "example-reviewer",
                  "reviewed_at": "2026-09-02T00:00:00Z"}
        for version in (1, 2):
            draft = copy.deepcopy(self.draft)
            draft['schema_version'] = version
            if version == 2:
                draft['recipe']['required_snapshots'] = {}
            spec = serving.freeze(draft, self.manifest if version == 1 else {'target': self.manifest})
            self.assertEqual(spec['schema_version'], version + 1)
            self.assertEqual(spec['state'], 'candidate')
            self.assertIsNone(spec['review'])
            for state in (None, 'candidate', 'measured', 'released'):
                self.assertIn(state, STATES)
                for metadata in (None, {}, review):
                    with self.subTest(schema=version + 1, state=state, review=metadata):
                        selected = {**spec, 'state': state, 'review': metadata}
                        self.assertEqual(serving.verify_spec(selected), selected)
                        self.assertEqual(selected['spec_id'], serving.spec_id(selected['recipe']))
                        for overrides in ({}, {'engine_args': selected['recipe']['engine_args']}):
                            self.assertEqual(serving.apply_overrides(selected, overrides), selected)
                        changed = serving.apply_overrides(selected, {'container_env': ['EXAMPLE_FLAG=1']})
                        self.assertNotEqual(changed['spec_id'], selected['spec_id'])
                        self.assertEqual(changed['state'], 'candidate')
                        self.assertIsNone(changed['review'])
                        self.assertEqual(selected['state'], state)
                        self.assertEqual(selected['review'], metadata)
            for state in ('', 'Candidate', 'testing', 0, False, [], {}):
                with self.subTest(schema=version + 1, invalid_state=state):
                    with self.assertRaisesRegex(ReleaseSpecError, 'state'):
                        serving.verify_spec({**spec, 'state': state})

    def test_execution_changes_drop_selected_metadata(self):
        self.spec.update(state="measured", review={})
        patches = [{"network_mode": "host"}, {"ipc_mode": "private", "shm_size_bytes": 67108864},
                   {"memory_limit_bytes": 1073741824}, {"cpu_limit_nanos": 1000000000},
                   {"ulimits": {"memlock": {"soft": 65536, "hard": 65536}}},
                   {"ulimits": {"nofile": {"soft": 4096, "hard": 4096}}},
                   {"devices": ["infiniband"]}, {"restart_policy": "always"},
                   {"healthcheck": {"interval_seconds": 60}}, {"healthcheck": None}]
        for patch in patches:
            with self.subTest(patch=patch):
                effective = serving.apply_overrides(self.spec, {"container": patch})
                self.assertNotEqual(effective["spec_id"], self.spec["spec_id"])
                self.assertEqual(effective["state"], "candidate")
                self.assertIsNone(effective["review"])
                self.assertTrue(serving.compare(self.spec, effective)["recipe_changed"])
        self.assertEqual(serving.apply_overrides(self.spec, {}), self.spec)

    def test_override_arrays_replace_and_other_identity_fields_cannot_be_overridden(self):
        effective = serving.apply_overrides(self.spec, {"container_env": ["EXAMPLE_FLAG=1"]})
        self.assertEqual(effective["recipe"]["container_env"], ["EXAMPLE_FLAG=1"])
        for patch in ({"model": {}}, {"geometry": {}}, {"image_digest": "x"}):
            with self.assertRaises(ReleaseSpecError):
                serving.apply_overrides(self.spec, patch)

    def test_unknown_or_incoherent_container_values_fail(self):
        patches = [{"network_mode": "none"}, {"memory_limit_bytes": True}, {"cpu_limit_nanos": -1},
                   {"ipc_mode": "private"}, {"privileged": True}, {"devices": ["infiniband", "infiniband"]},
                   {"ulimits": {"stack": {"soft": -1, "hard": 1024}}}, {"restart_max_retries": 3},
                   {"healthcheck": {"path": "/health;uname"}}]
        for patch in patches:
            with self.subTest(patch=patch), self.assertRaises(ReleaseSpecError):
                serving.apply_overrides(self.spec, {"container": patch})

    def test_reserved_bindings_and_duplicate_arguments_fail(self):
        for env in (["HF_TOKEN=example"], ["VLLM_HOST_IP=example"], ["A=1", "A=2"]):
            with self.subTest(env=env), self.assertRaises(ReleaseSpecError):
                serving.apply_overrides(self.spec, {"container_env": env})
        for args in (["--port", "8000"], ["--gpu-memory-utilization", "NaN"],
                     ["--gpu-memory-utilization", "0.8", "--gpu-memory-utilization", "0.9"]):
            with self.subTest(args=args), self.assertRaises(ReleaseSpecError):
                serving.apply_overrides(self.spec, {"engine_args": args})

    def test_normalization_and_mutation_detection(self):
        self.draft["recipe"]["engine_args"] = ["--gpu-memory-utilization=0.800"]
        self.assertEqual(serving.freeze(self.draft, self.manifest), {**self.spec, "state": "candidate"})
        tampered = copy.deepcopy(self.spec)
        tampered["recipe"]["container"]["network_mode"] = "host"
        with self.assertRaises(ReleaseSpecError):
            serving.verify_spec(tampered)
        self.manifest["snapshot_revision"] = "b" * 40
        with self.assertRaises(ReleaseSpecError):
            serving.freeze(self.draft, self.manifest)

    def test_input_is_json_never_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "draft.conf"
            for data in ('exit 0\n', '{"schema_version":1,"schema_version":2}'):
                path.write_text(data)
                with self.assertRaises(ReleaseSpecError):
                    serving.load_json(path)

    def test_multi_rank_defaults_remain_explicit(self):
        draft = serving.example(2)
        draft["recipe"]["model"] = self.draft["recipe"]["model"]
        draft["recipe"]["image_digest"] = self.draft["recipe"]["image_digest"]
        spec = serving.freeze(draft, self.manifest)
        self.assertIn("NCCL_IB_QPS_PER_CONNECTION=4", spec["recipe"]["container_env"])
        custom=serving.apply_overrides(spec,{'container_env':[
            value.replace('NCCL_IB_QPS_PER_CONNECTION=4','NCCL_IB_QPS_PER_CONNECTION=8')
            for value in spec['recipe']['container_env']]})
        self.assertNotEqual(custom['spec_id'],spec['spec_id'])
        self.assertIn('NCCL_IB_QPS_PER_CONNECTION=8',custom['recipe']['container_env'])
        with self.assertRaises(ReleaseSpecError):
            serving.apply_overrides(spec, {"container": {"network_mode": "bridge"}})

    def test_image_repository_components_are_docker_compatible(self):
        for repository in ('vllm/','vllm//openai','/vllm','vllm/.openai','vllm/openai-',
                           'vllm/openai___test','vllm/OpenAI','registry_bad.example/model',
                           'vllm/openai:latest','vllm/openai@sha256:'+'a'*64,'a'*250):
            self.draft['source']['image_repository']=repository
            with self.subTest(repository=repository),self.assertRaises(ReleaseSpecError):
                serving.freeze(self.draft,self.manifest)
            value=copy.deepcopy(self.spec);value['source']['image_repository']=repository
            with self.assertRaises(ReleaseSpecError): serving.verify_spec(value)
        for repository in ('vllm','vllm/openai','ghcr.io/example/model','example/model__variant',
                           'example/model--variant','Registry.example/model'):
            self.draft['source']['image_repository']=repository
            self.assertEqual(serving.freeze(self.draft,self.manifest)['spec_id'],self.spec['spec_id'])


if __name__ == "__main__":
    unittest.main()
