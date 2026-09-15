# Explicit estimates of resident weights

The default memory preflight uses the complete checkpoint's disk size as its
weight estimate. That can differ from resident weights for offload, conditional
components or runtime expansion. An operator may select an explicit estimate
for an exact effective spec. It is admission guidance, not a measurement,
qualification result, allocation cap or promise that the model will fit.

The input is a version-1 JSON document with these fields:

- `schema_version`: integer `1`.
- `kind`: `pulsar-memory-estimate`.
- `spec_id`: the complete effective spec ID, including any execution overrides.
- `basis`: a nonempty, single-line explanation, at most 1024 characters.
- `ranks`: every logical serving rank, ordered from zero. Each object has
  `rank` and `resident_weights_bytes`, a positive integer no greater than
  `2^63-1`. Specify weights only; exclude KV and other runtime allowances.

Example rank entry for a 12 GiB weight estimate:

```json
{"rank": 0, "resident_weights_bytes": 12884901888}
```

Validate the complete document against its spec:

```sh
pulsar memory verify --file memory-estimate.json --spec-file candidate.json --json
```

The result contains the normalized `estimate` and its canonical content digest,
`estimate_id`. Review both the basis and numbers. Supplying the ID on subsequent
operations ensures the selected document still matches that review:

```sh
pulsar start SPEC_ID --spec-file candidate.json --dry-run \
  --memory-estimate-file memory-estimate.json --memory-estimate-id ESTIMATE_ID
```

The input is read through the existing stable, descriptor-rooted file reader.
Duplicate JSON keys, nonstandard numbers, symlinks, wrong identities, missing or
duplicate ranks, and invalid sizes are refused. The normalized document is
frozen once for that invocation. Later source-file edits cannot change the
selected value. Every admission stage receives and validates the same frozen
document and digest; a different effective spec is refused.

The preflight replaces only the weight estimate for each rank. It retains the
fixed or estimated KV reservation, runtime overhead, launch spike, preferred
free-memory buffer, absolute available-memory floor and cold-start slack.
Lowering context does not reduce an explicitly fixed KV reservation.
For nonuniform estimates, decisions use each rank's own footprint; the legacy
scalar summary fields report maxima, and JSON rank entries show the individual
weight, footprint, startup and projected-residual values.

Warnings remain warnings. A dry run may report them without starting anything.
An actual launch still requires separately authorized `--accept-memory-warn`
when a warning remains. That option never accepts a hard failure, invalid
estimate, missing image or unavailable prepared rank. `--skip-preflight` does
not bypass the memory gate. No ambient estimate environment override is accepted
by the launch entrypoints.

## Plan and compatibility contract

Launches without an estimate retain their existing schema-3 or schema-4 plan
format and default calculations. Selecting an estimate produces a schema-5
launch plan containing `memory_estimate: {estimate_id, estimate}`. The plan ID
covers this data. Plan validation, Docker label binding, service retention and
observation accept this format through the shared runtime validator.

Serving-spec schemas 2 and 3, spec identity, image/model bytes, engine arguments,
container settings and service identity are unchanged. The estimate is a
recorded operational input. Old launch plans remain readable; they are not
migrated. A Stack that does not support schema 5 cannot inspect a new plan.
The public integration contract advertises `memory.verify` and
`memory_estimate_schema_versions: [1]`; callers must check those capabilities
before selecting this input.

The public launch result additionally reports `memory_estimate_id` when used.
This lets an orchestrator retain the selected estimate identity alongside the
service reference. It does not grant recipe, launch, replacement, staging or
publication authority.
