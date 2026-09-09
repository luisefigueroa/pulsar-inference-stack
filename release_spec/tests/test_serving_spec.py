"""Identity, overrides and historical boundaries for portable serving specs."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from release_spec import serving
from release_spec.schema import ReleaseSpecError

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts"


class ServingSpec(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads((FIXTURES / "spec.json").read_text())
        self.draft = json.loads((FIXTURES / "draft.json").read_text())
        self.manifest = json.loads((FIXTURES / "manifest.json").read_text())

    def test_golden_identity_binds_only_schema_and_complete_recipe(self):
        payload = json.dumps({"schema_version": 2, "recipe": self.spec["recipe"]},
                             sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertEqual(self.spec["spec_id"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(serving.freeze(self.draft, self.manifest), self.spec)
        changed = copy.deepcopy(self.spec)
        changed.update(state="released", review={})
        changed["source"]["image_repository"] = "example/mirror"
        self.assertEqual(serving.verify_spec(changed)["spec_id"], self.spec["spec_id"])

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
                self.assertIsNone(effective["state"])
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
        self.assertEqual(serving.freeze(self.draft, self.manifest), self.spec)
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
