"""ADR 0017 release spec schema, spec_id, and verifier.

This package is standard-library-only and importable from the repository
root. It imports nothing from ``scripts/``. ``spec_id`` hashes the identity
block with ``json.dumps(..., sort_keys=True, separators=(",", ":"),
ensure_ascii=False)``. Nested snapshot ``manifest_id`` copies the model-library
algorithm and omits ``ensure_ascii=False``. ASCII snapshot paths make the
two encodings agree.
"""

from .identity import identity_block, snapshot_file_lists_equal, spec_id_for
from .normalize import (
    build_snapshot_manifest,
    canonical_json_digest,
    normalize_container_env,
    normalize_engine_args,
    normalize_snapshot_files,
    pretty_json_bytes,
    snapshot_manifest_id,
)
from .schema import (
    KIND,
    REVIEW_STATUSES,
    SCHEMA_VERSION,
    SNAPSHOT_MANIFEST_KIND,
    STATES,
    ReleaseSpecError,
)
from .verify import load_spec, verify_spec

__all__ = [
    "KIND",
    "REVIEW_STATUSES",
    "SCHEMA_VERSION",
    "SNAPSHOT_MANIFEST_KIND",
    "STATES",
    "ReleaseSpecError",
    "build_snapshot_manifest",
    "canonical_json_digest",
    "identity_block",
    "load_spec",
    "normalize_container_env",
    "normalize_engine_args",
    "normalize_snapshot_files",
    "pretty_json_bytes",
    "snapshot_file_lists_equal",
    "snapshot_manifest_id",
    "spec_id_for",
    "verify_spec",
]

from .manifest import load_snapshot_manifest, verify_snapshot_manifest
__all__ += ["load_snapshot_manifest", "verify_snapshot_manifest"]

from .recipe import (
    DEFAULT_NCCL_IB_QPS,
    NCCL_QPS_ENV,
    build_profile_identity,
    nccl_qps_from_identity,
    profile_image_digest,
)
__all__ += [
    "DEFAULT_NCCL_IB_QPS",
    "NCCL_QPS_ENV",
    "build_profile_identity",
    "nccl_qps_from_identity",
    "profile_image_digest",
]


def runtime_contract_id(spec):
    """Identity of an exact runtime recipe, independent of deployment settings.

    New multi-node specs include NCCL QPs in ``identity.container_env``, so the
    nested ``spec_id`` binds that recipe-affecting value.
    """
    document = verify_spec(spec)
    return canonical_json_digest({"kind": "pulsar-launch-contract",
                                  "spec_id": document["spec_id"],
                                  "argv": document["launch_contract"]["argv"]})


__all__ += ["runtime_contract_id"]
