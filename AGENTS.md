# Working on Pulsar Inference Stack

This public repository owns the catalog, model files, archives, preparation,
and serving lifecycle. The private sibling `pulsar-inference-workbench` owns
model experiments, qualification attempts, and contribution export. Do not
require private workbench code to browse, restore, prepare, serve, or verify a
public contribution. The operator command is `./pulsar`.

## Implementation boundaries

- Use Bash for operator commands, confirmed topology, SSH, process orchestration,
  and transport. Use Python 3 for schemas, planning, identity, state, and tests.
- `release_spec/` owns canonical immutable specs, snapshot manifests, the public
  JSON draft compiler, measurement contracts, the baseline policy evaluator, and
  catalog-spec schema verification.
  Preserve model bytes, image, engine arguments, container settings, and hardware
  geometry in the effective spec. Explicit execution overrides create a distinct
  effective spec and are displayed as a modified recipe. Site settings do not
  change recipe identity. Containers name the selected/effective specs and their
  immutable launch plan; Git commits are provenance, never runtime identity.
- `model_library/` owns manifest verification, explicit homes, prepared copies,
  pins, archives, migration, and local records. Records locate bytes; manifests
  and actual verification establish their identity. Reuse the shared schema.
- Reuse `scripts/lib.sh` and shared adapters for confirmed node identity,
  management SSH, rank placement, and ownership. Keep control traffic, model
  transfer, and inference traffic distinct. Never infer serving geometry from
  spare discovered nodes or silently change a transport.
- The home belongs on one of the spec's selected serving nodes. Home movement is
  explicit. A prepared home view references that home; other ranks have working
  copies. Stop preserves files; purge respects pins and container references.
- Archives are separate verified copies shared by recipes using identical bytes.
  Restore uses the selected spec and archive, without old controller receipts.
  There is no archive-deletion command. Preserve unrelated storage contents.
- `PULSAR_COLD_ROOT` is the operator's archive-directory choice: process value
  including empty, then the repository `.env`, then not configured. Access is
  observed health, not a rule prescribing mounts, modes, ownership, or storage
  failure domains. Hold the shared configuration lock during archive operations.

## Evidence and publication

The workbench maintainer decides what is published. A schema-valid spec under
`releases/`, with a filename equal to its complete `spec_id`, is a catalog
member. `state` and `review` are nullable metadata and never catalog or serving
gates. Do not infer, promote, or rewrite either value. Baseline results, archive
proof, and current launch compatibility are independent optional assessments;
their outcome does not add or remove catalog membership. Withdrawal metadata
does not stop services or delete archives automatically.

New operations and contributions use spec schemas 2 and 3 through the public
`pulsar` CLI contract. Schema-1 catalog records remain historical and readable; do not
migrate their evidence or retain a legacy launch compiler. Preserve safe
inventory/stop support for existing services. Workbench must not import this
checkout's Python modules or source its shell libraries.

Keep topology files, hostnames, addresses, SSH identity, user paths, credentials,
and raw experiment outputs out of tracked public files. Privacy remains a
publication-safety check even though evidence is not a catalog gate. Use the
existing privacy scanner before publication and its `--staged` mode before committing. Retain
licenses and attribution for carried-forward code. Fresh repository history does
not mean importing former experiment evidence or serving catalog entries.

Do not perform physical downloads, launches, service replacement, destructive
cleanup, or remote publication outside the maintainer's explicitly agreed scope.
Starting is non-replacing by default; image staging and replacement require
their own explicit flags and authority.
Do not claim physical serving results from mocked tests. Resolve significant
changes to the agreed plan with the maintainer before implementing them.

## Terminology and naming

Use plain, specific names that tell a reader what an object is or what an
operation does. Use the same term for the same concept in Stack and Workbench,
including function names, variables, CLI output, schema fields, documentation,
tests, and diagrams. Qualify ambiguous names by their subject: `stack_commit`,
`model_commit`, `schema_version`, `spec_id`, or `container_id`.

Call a Git commit a `commit`; do not call it a release, version, or build.
Reserve `version` for an explicitly versioned format or software release,
`spec` for a serving specification, `recipe` for its execution configuration,
and `publication` for adding reviewed content to the catalog. Distinguish
intended configuration from observed state and from historical measurements.

During a refactor, replace misleading internal names in the affected code and
update explanations and tests together. Rename persisted fields, public commands,
or container labels only through an explicit compatibility or migration plan;
do not rewrite historical evidence merely to improve its terminology. Avoid
unrelated rename sweeps and do not introduce multiple aliases without a defined
compatibility purpose and retirement condition.

## Verification and operator experience

Run directly affected tests while iterating and relevant subsystem checks after
a coherent change. Select coverage by changed behavior and callers; a commit,
push, PR, or new approval is not itself a full-suite trigger. Reuse passing
validation while its relevant code, fixtures, dependencies and environment match.
Run `scripts/selftest.sh` for broad cross-subsystem changes, dependencies that
affect execution, uncertainty that selected coverage cannot resolve, or an
explicit regression check. Explain that reason before running it.

Before commit or publication, review the selected coverage and run fast privacy
and diff checks, including the required staged checks. Prose-only changes need
link, consistency and privacy checks; workflow changes need syntax, event/step
selection and affected-command checks. `scripts/selftest.sh --checks-only`
runs the existing syntax, catalog and privacy checks without unit tests. See
[validation selection](docs/TESTING.md#select-checks-for-the-change).

Public CI uses fast PR checks and a manual full-suite run, with no duplicate push
trigger. CI is standalone and must not depend on private repository access.
Tests use synthetic fixtures and parameterized doubles, with no real cluster mutations.
Keep Bash scenarios thin and data-heavy fixtures in Python.

Lead human output with the action, affected object, and blocker. Keep JSON
separate and stable, use hanging indentation for long paths, and check narrow
terminal output. Saved catalog observations show their age; unknown is not
absent, and prepared files do not imply a running or guaranteed launch.
