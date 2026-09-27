# Public spec and execution contract

Workbench and other clients invoke the `pulsar` executable. They do not import
Stack modules, source its shell library, or require a matching Git commit.
`pulsar contract --json` lists implemented operations and supported document
versions. Check the operations needed for an action before performing it.

## Documents and identity

A schema-2 or schema-3 `pulsar-serving-spec` contains `schema_version`, `kind`,
`spec_id`, `recipe`, `source`, `state`, and `review`. The spec ID is SHA-256 over canonical
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

Container settings may include the optional [guard policy](SERVING_GUARD_SCHEMA.md).
`contract` advertises its supported document versions through
`serving_guard_schema_versions`. Guard metadata participates in recipe identity
and can be validated for catalog and evidence purposes. This does not advertise
guarded execution: ordinary launch rejects guarded recipes until enforcement
is supported, and static launch compatibility reports that limitation separately.

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

## Required snapshots and speculative decoding

Schema 3 retains `recipe.model` as the serving target and adds the required
`recipe.required_snapshots` object. Its keys are names matching
`[a-z][a-z0-9_-]{0,63}`; `target` is reserved for `recipe.model`. Each value has
the same `model_id`, `model_commit`, and complete `snapshot_manifest` fields as
the target. Every entry is required on every serving rank and contributes to
`spec_id`. Source manifests, homes, and archives retain their existing digests.

Start with `pulsar spec example --schema-version 2`. Draft schema 2 declares the
same names and model commits, without embedding manifests. Freeze with exactly
one named manifest for each declaration, including the target:

```sh
./pulsar spec freeze --draft draft.json \
  --manifest target=target.json --manifest draft=draft.json --json
```

Use `pulsar-snapshot:draft` as the speculative configuration's model value:

```json
["--speculative_config.model", "pulsar-snapshot:draft",
 "--speculative_config.num_speculative_tokens", "3"]
```

The JSON `--speculative-config` form is also supported, with the reference in
its `model` field. Underscore/hyphen spellings of the flag prefix are accepted.
Do not mix JSON and dotted forms, repeat fields, or supply a separate checkpoint
revision. Every additional declaration must have a supported engine reference.
References in unrelated arguments and bare speculative checkpoint locators are
rejected. Other model-loading argument families are not implemented by this
binding rule. Non-checkpoint speculation can still use ordinary engine args.

Stack resolves each reference to the exact local snapshot path. The target
keeps its existing mount convention; additional Hub directories mount read-only
under `/pulsar/snapshots/<manifest-id>`, and the engine receives the child
`snapshots/<model-commit>` path. Different commits from one Hub repository do
not collide. Actual mounts, resolved arguments, and files must agree on every
rank. Execution overrides cannot introduce undeclared checkpoints.

Freeze validates identity and references without contacting hardware. Complete
preparation and launch require all members. A passing target-only baseline
cannot qualify a speculative variant; the target still determines the served
model name and baseline accuracy policy.

Draft schema 1 and spec schema 2 remain supported with their existing identity
algorithm. Freezing and launch now reject bare Hub IDs or local paths in a
speculative configuration's `model` field, including schema-2 recipes. An
already-frozen schema-2 spec with such a locator remains readable and
schema-valid, but fails launch compatibility and cannot start. This restriction
also applies to explicit execution overrides. N-gram and MTP configurations
without a `model` field retain their exact argument tokens and schema-2 identity.

To reauthor an affected recipe:

1. Change the draft's `schema_version` from 1 to 2 and add
   `recipe.required_snapshots.draft` with the checkpoint's `model_id` and exact
   `model_commit`.
2. Replace the speculative `model` value with `pulsar-snapshot:draft`. Remove any
   separate speculative revision selector; the declared model commit owns it.
3. Freeze with both `--manifest target=target.json` and
   `--manifest draft=draft.json`, plus any other declared members. The result is
   a new schema-3 spec with a distinct `spec_id`.

Retain the earlier spec and evidence unchanged. Prepare and launch the new spec
only within the maintainer's approved scope, and collect new measurements for
that recipe; earlier measurements do not qualify the new spec.

Schema-3 execution uses prepared-set 2, launch-plan 4, observation 3,
identity measurement 2, and baseline run 4. Other measurements remain schema 1.
Existing launch-plan 3/observation 2/run 3 records keep their meaning. Named
snapshot archive verification uses schema 2 with a `snapshots` map of existing
schema-1 per-snapshot proofs. The timestamped archive observation and contribution
package envelopes keep their current formats. No historical evidence is upgraded.

## Container binding inventory

These are the base container binding rules, not a set of mutable launcher defaults.
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
| Required manifests and prepared set | Read-only mounts selecting every exact verified snapshot |
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
| `policy show baseline-v1` / `policy show baseline-v2` | Read the selected fixed policy and digest |
| `evidence measurement/evaluate/verify/summary` | Construct and assess compact evidence, independently of catalog membership |
| `contribution verify --package DIR` | Validate the exact package, identities, hashes, and publication privacy |
| `privacy check`, `privacy commits` | Check publication files or commit metadata |
| `selftest` | Run deterministic repository checks |

Cancelled Stack actions return the existing error envelope with a nonzero exit
status. `cancelled` confirms cleanup of the local command and tracked node workers;
`cleanup_incomplete` means worker exit could not be established. Neither result
authorizes stopping a model service. Internal lease/worker receipts do not alter
successful command results or snapshot, prepared-set and evidence schemas.

`model info`, `model check` and `observe` accept `--verification-jobs N` (a
positive integer, default 3). Prepared-file verification runs at most N jobs
and at most one per physical node. `1` selects serial execution; `--full`
independently requests full hashes. Every required snapshot and rank must
verify before a prepared set is ready. See [inspection behavior](OPERATIONS.md#historical-specs-and-current-observations).

For non-streaming `--json` operations, stdout is one envelope with schema version
1, `ok`, and either `result` or `error` (`code`, `message`, `details`). Human
logging goes to stderr. Errors have nonzero exit status. Do not parse human
messages to make lifecycle decisions.

### Error codes and exit statuses

Branch on `ok` and `error.code`. Treat an unknown code as a failure: new, more
specific codes may be added within CLI contract 1. `pulsar contract --json`
publishes this table as `error_codes` and `exit_statuses`.

| `error.code` | Exit | Meaning |
| --- | --- | --- |
| `usage_error` | 2 | The command line is invalid: an unknown command or missing or bad arguments. |
| `file_error` | 2 | A file or directory named by the caller could not be read or written. |
| `invalid_spec` | 2 | Spec or document content failed validation. |
| `unsupported_spec_version` | 2 | The document's schema version is not supported. |
| `invalid_stack_output` | 2 | A Stack script produced output that is not JSON. This is a Stack defect. |
| `prerequisite_failed` | 3 | A Stack action exited unsuccessfully; message and details hold its diagnostics. |
| `cancelled` | 128+signal | Interrupted; cleanup of the command and its node workers was confirmed. |
| `cleanup_incomplete` | 128+signal | Interrupted; worker exit could not be confirmed. |

Success exits 0. The table applies to `--json` output. Without `--json`,
`start`, `stop`, `status` and `model` run their operator scripts directly: a
failed action exits 1 and a usage error exits 2, while the same failure with
`--json` exits 3 as `prerequisite_failed`.

When `start --json` is refused, the error is `prerequisite_failed` and
`details` holds one record per start blocker: `field` is `blocker`, plus
`blocker` (a code from `start_blocker_codes` in the contract), `node` and `rank`
(null when the blocker is not node-specific), `message` and `fix`, the one
command to run next. `guard_unsupported` is the exception: no command resolves
it, so its `fix` names the serving guard documentation. Start runs every
independent check before it reports, so one refusal can list several blockers.
Branch on `blocker`; `message` and `fix` are for people.

`stop --json` returns `completed`, the `spec_id` and `stopped`: `true` when an
owned service was stopped, `false` when none was running. For `stop --all`,
`stopped` is `null` because the result is not established per spec.

Change note, 2026-09-26: `usage_error`, `file_error` and `invalid_stack_output`
were split out of `invalid_spec`, which previously covered every input failure.
Exit statuses did not change.

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

Run schema 3 for spec schema 2 binds the effective spec ID, fixed policy digest,
dataset and policy input hashes, measurement hashes, ordered producers, and
before/after rank configuration and boot witnesses. Run schema 4 for spec schema 3 also
requires complete named snapshot coverage on every rank. Commits identify
measurement producers and observers, not recipes. Missing Stack commit metadata
is reported as a provenance limitation rather than a new runtime gate.

Baseline-v2 grades five criteria: snapshot identity, serving smoke, GSM8K,
one-hour soak, and performance completeness. Their thresholds and measurement
parameters are unchanged from baseline-v1. Greedy repeatability is retained as
a diagnostic, including text differences, logprob differences, and source
capture hashes. Differences never affect the grade. Missing or unusable
captures prevent a complete passing campaign; malformed evidence is rejected.
All six measurement documents remain hash-bound to the run, and unchanged
before/after rank and snapshot coverage is still required.

`contract` advertises `baseline_policies`, a map from supported suite names to
fixed policy digests. The legacy `baseline_policy_digest` field continues to
identify baseline-v1. Policy document schema 1 supports either suite; spec and
run schemas are unchanged. Baseline-v1 evaluation remains schema 1 with its
original six outcomes. Baseline-v2 evaluation uses schema 2 and adds `suite`
and `diagnostics`; `diagnostics.compare-captures` contains the validated compact
measurement, or null when missing, without a pass/fail outcome.

Evidence is stored separately at `results/<suite>/<spec_id>/<run_id>/`, where
`suite` is `baseline-v1` or `baseline-v2` and must match the saved `policy.json`.
Only the exact fixed policy digest for that suite is supported.
A later run does not replace an earlier one. Catalog metadata edits do not
invalidate runs for the unchanged recipe. Optional timestamped archive
observations can be attached to summaries; export does not verify archives
implicitly or manufacture an observation time.

Historical schema-1 specs and schema-2 runs retain their original IDs and claims.
`spec show --historical` reads old specs. Existing services remain inspectable
and stoppable, but a new-contract observation can be unavailable for them.
New operations and changed catalog contributions require spec schema 2 or 3;
unchanged historical catalog files are retained. No automatic migration or
qualification of old evidence occurs.
