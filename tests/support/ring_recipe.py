"""A synthetic multi-node recipe for tests: frozen exactly as a candidate
would be, never written under releases/. With the ring pattern it carries the
four NCCL settings a recipe that passes data around a loop of its nodes uses."""
import hashlib

from release_spec import build_snapshot_manifest
from release_spec.serving import example, freeze

RING_ENV = ('NCCL_ALGO=Ring', 'NCCL_IB_MERGE_NICS=0', 'NCCL_IB_SUBNET_AWARE_ROUTING=1', 'NCCL_NET_PLUGIN=none')
MODEL_ID = 'example/ring-synthetic'


def manifest():
    data = b'synthetic ring weights'
    return build_snapshot_manifest(model_id=MODEL_ID, snapshot_revision='c' * 40, files=[
        dict(path='weights.bin', size=len(data), sha256=hashlib.sha256(data).hexdigest())])


def recipe(nodes=4, env=RING_ENV):
    """A frozen spec on NODES nodes with tensor parallelism NODES and ENV
    added to the example's container_env (by default the ring pattern)."""
    draft = example(nodes)
    draft['source']['image_repository'] = 'example/image'
    draft['recipe']['model'] = {'model_id': MODEL_ID, 'model_commit': 'c' * 40}
    draft['recipe']['image_digest'] = 'sha256:' + 'd' * 64
    draft['recipe']['engine_args'] += ['--max-model-len', '1024']
    draft['recipe']['container_env'] = sorted([*draft['recipe']['container_env'], *env])
    return freeze(draft, manifest())
