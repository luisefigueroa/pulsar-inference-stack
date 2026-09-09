# Public spec and execution contract

Workbench and other clients invoke the `pulsar` executable. They do not import
Stack modules, source its shell library, or require a matching Git commit.
`pulsar contract --json` lists implemented operations and supported document
versions. Check the operations needed for an action before performing it.

## Documents and identity

A schema-2 `pulsar-serving-spec` contains `schema_version`, `kind`, `spec_id`,
`recipe`, `source`, `state`, and `review`. The spec ID is SHA-256 over canonical
JSON containing the schema version and complete recipe. The source image
repository locator and nullable catalog metadata do not affect identity.

Image repository locators must have valid Docker repository components; empty
components, invalid separators, and uppercase repository paths are rejected.
The contract continues to exclude tags, digests, and registry ports from this
field. Component validation follows the
[Distribution reference grammar](https://github.com/distribution/reference/blob/main/regexp.go).

The recipe contains the model ID and model commit, complete snapshot manifest,
image digest, engine argument tokens, literal container environment, hardware
geometry, and explicit container settings. Snapshot manifests retain schema 1
and their original `snapshot_revision` field and hashing for storage reuse.

Generate an editable JSON draft rather than guessing defaults:

```sh
./pulsar spec example --nodes 1 --json
./pulsar spec freeze --draft draft.json --manifest snapshot.json --json
./pulsar spec verify --file spec.json --json
./pulsar spec compare --before previous.json --after spec.json --json
```

The JSON envelope's `result` is the draft/spec document. Save that document,
not the response envelope, as the input to the next document command. Examples
leave model identity and image digest unset until the maintainer supplies them.
Unknown fields, conflicting arguments, and missing execution settings fail.
Geometry owns TP/PP flags; site bindings own model paths, ports, addresses,
served names, and credentials. Literal recipe environment cannot override those
reserved bindings. No draft is executable Bash.

## Container binding inventory

This is the schema-2 binding rule, not a set of mutable launcher defaults.
Changing its meaning requires a contract change and new conformance vectors.

| Input | Docker/engine binding |
| --- | --- |
| `container.network_mode` | Explicit `--network bridge` or `host`; bridge publishes the selected port |
| `container.ipc_mode`, `shm_size_bytes` | Explicit `--ipc`; private IPC also supplies an explicit shared-memory size |
| `container.ulimits` | Explicit soft/hard memlock and stack limits, optionally nofile |
| `container.memory_limit_bytes` | Zero means no cap; positive values are at least 6 MiB and supply `--memory` |
| Memory/swap binding | A positive memory cap supplies combined memory-plus-swap allowance of twice that cap; this preserves Docker's documented default explicitly |
| `container.cpu_limit_nanos` | Zero means no quota; otherwise `--cpus` with the corresponding decimal CPU count |
| CPU binding | Equivalent default-period CFS quota representations normalize to the same cap; additional affinity, shares, reservations, and unsupported constraints are rejected |
| `container.accelerator_access` | `all`, for the current one-accelerator-per-node platform |
| `container.devices` | The `infiniband` identifier exposes the existing device directory for multi-node recipes |
| `container.restart_policy`, `restart_max_retries` | Explicit restart policy; retry counts apply only to `on-failure` |
| `container.healthcheck` | Explicit HTTP path and timing; null explicitly disables inherited image healthchecks |
| Model manifest and prepared set | Read-only mount of the exact verified model snapshot |
| `engine_args` and geometry | Frozen engine tokens plus exactly one TP/PP binding and required rank arguments |
| `container_env` | Frozen literal values, including offline/logging/NCCL settings |
| Confirmed rank placement | Rank addresses, fabric devices, and control-interface environment bindings |
| Deployment settings | API/rendezvous ports and served model name; recorded in the private launch plan |
| Credentials | Applied from the configured environment at launch; never frozen in a public spec or returned as plaintext provenance |
| Lifecycle mechanics | Container name, detach mode, and ownership/plan labels; no Git-commit label |

Docker documents the CPU quota equivalence and default combined swap allowance
in [resource constraints](https://docs.docker.com/engine/containers/resource_constraints/).
The contract fixes these bindings; it does not claim identical timing across
hosts or prove kernel enforcement through synthetic tests. Private observations
record host architecture, kernel, GPU driver, Docker version, resolved environment,
and model mounts when available.

## Operator overrides

`pulsar start SPEC --override-file overrides.json` accepts a typed partial object
over `engine_args`, `container_env`, and `container`. Arrays replace arrays;
container object fields merge by name. Model, image, and geometry changes require
another spec. There is no arbitrary Docker-argument escape hatch.

Stack freezes the effective spec before launch, shows its ID, and marks changed
recipes as `Modified recipe`. A no-op override keeps the original identity.
Selected-catalog measurements are reference only for a modified recipe. The
selected spec and its metadata remain unchanged; derived metadata starts null.
`--replace` and `--pull-image` retain independent meanings. Neither an override
nor `--yes` grants them.

The private launch record contains selected/effective specs and resolved site
settings. Containers name that immutable record. Later observation uses the
recorded configuration, not today's checkout defaults or deployment overlay.
Compatible code updates alone do not require a container restart. A different
checkout must explicitly use the intended confirmed topology and state location;
a missing launch record is not reconstructed by guessing current defaults.

Observation passes the recorded deployment settings through model-file
verification, so an overlay edited for future launches cannot redirect an
existing service check. Ulimit comparison includes the complete configured map;
extra limits cannot be dropped from compact evidence.

A successful stop retires active service indexes only for the selected spec,
confirmed topology, and fully stopped nodes. Failed stops retain those indexes.
Immutable launch plans remain as history, while subsequent starts get an
unambiguous active service selector.

## Public operations

Document operations need no Docker, topology, model files, or clean checkout.
Execution operations independently check their live prerequisites.

| Command family | Purpose |
| --- | --- |
| `spec example/freeze/verify/show/compare` | Author, validate, read, and compare serving specs |
| `model acquire/prepare/info/restore/archive` | Existing explicit model-file lifecycle; use `--model-commit` for first acquisition |
| `start`, `status`, `stop` | Serve, inspect, or stop an owned service |
| `observe --service-id ID` | Verify all ranks and actual container configuration without mutation |
| `resources --service-id ID --jsonl` | Stream private diagnostics for a recorded service |
| `resources --spec-file FILE [--node NODE] [--override-file FILE] --jsonl` | Start node sampling before launch; attach only to the matching owned recipe |
| `policy show baseline-v1` | Read the unchanged policy and digest |
| `evidence measurement/evaluate/verify/summary` | Construct and assess compact evidence, independently of catalog membership |
| `contribution verify --package DIR` | Validate the exact package, identities, hashes, and publication privacy |
| `privacy check`, `privacy commits` | Check publication files or commit metadata |
| `selftest` | Run deterministic repository checks |

For non-streaming `--json` operations, stdout is one envelope with schema version
1, `ok`, and either `result` or `error` (`code`, `message`, `details`). Human
logging goes to stderr. Errors have nonzero exit status. Do not parse human
messages to make lifecycle decisions.

Resource streams have a versioned header, then rank samples or explicit error
records. Unavailable workload measurements are null, never zero. Stopping a
stream terminates only diagnostic processes, not model services. Resource
sampling does not acquire, prepare, launch, or enroll nodes.

Docker's inspect configuration and OCI process configuration are distinct.
The environment comparison reads `Config.Env`; Docker-generated `HOSTNAME` and
GPU environment additions are applied to the OCI process environment. Do not
ignore arbitrary extra `Config.Env` values to accommodate those additions.
Docker also expands a device-directory mapping into its child device nodes.
See Moby's [environment construction](https://github.com/moby/moby/blob/master/daemon/container/container.go),
[GPU process configuration](https://github.com/moby/moby/blob/master/daemon/devices_nvidia_linux.go),
and [device-directory expansion](https://github.com/moby/moby/blob/master/daemon/pkg/oci/devices_linux.go).

## Measurements and history

New run schema 3 binds the effective spec ID, fixed policy digest, dataset and
policy input hashes, measurement hashes, ordered producers, and before/after
rank configuration and boot witnesses. Commits identify measurement producers
and observers, not recipes. Missing Stack commit metadata is reported as a
provenance limitation rather than a new runtime gate.

Evidence is stored separately at `results/baseline-v1/<spec_id>/<run_id>/`.
A later run does not replace an earlier one. Catalog metadata edits do not
invalidate runs for the unchanged recipe. Optional timestamped archive
observations can be attached to summaries; export does not verify archives
implicitly or manufacture an observation time.

Historical schema-1 specs and schema-2 runs retain their original IDs and claims.
`spec show --historical` reads old specs. Existing services remain inspectable
and stoppable, but a new-contract observation can be unavailable for them.
New operations and changed catalog contributions require schema 2; unchanged
historical catalog files are retained. No automatic migration or qualification
of old evidence occurs.
