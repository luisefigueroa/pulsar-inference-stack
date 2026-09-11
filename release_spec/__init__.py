"""Current serving specs and unchanged model manifests.

Current recipes are defined in serving; schema-1 identity helpers remain for
historical reading only. Cross-repository consumers use the public pulsar CLI.
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
    HISTORICAL_SPEC_KIND,
    REVIEW_STATUSES,
    HISTORICAL_SPEC_SCHEMA_VERSION,
    SNAPSHOT_MANIFEST_KIND,
    STATES,
    ReleaseSpecError,
)
from .verify import load_spec, verify_spec

__all__ = [
    "KIND",
    "HISTORICAL_SPEC_KIND",
    "REVIEW_STATUSES",
    "SCHEMA_VERSION",
    "HISTORICAL_SPEC_SCHEMA_VERSION",
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



def runtime_contract_id(spec):
    """Identity of an exact runtime recipe, independent of deployment settings.

    New multi-node specs include NCCL QPs in ``identity.container_env``, so the
    nested ``spec_id`` binds that recipe-affecting value.
    """
    document = verify_spec(spec)
    if document['schema_version'] in (2, 3):
        return document['spec_id']
    return canonical_json_digest({"kind": "pulsar-launch-contract",
                                  "spec_id": document["spec_id"],
                                  "argv": document["launch_contract"]["argv"]})


__all__ += ["runtime_contract_id"]

from .serving import SPEC_KIND as KIND, SPEC_SCHEMA_VERSION as SCHEMA_VERSION
