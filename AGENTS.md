# Working on Pulsar Inference Stack

This public repository owns the catalog, model files, archives, preparation,
and serving lifecycle. The private sibling `pulsar-inference-workbench` owns
model experiments, qualification attempts, and contribution export. Do not
require private workbench code to browse, restore, prepare, serve, or verify a
public contribution. The operator command is `./pulsar`.

## Implementation boundaries

- Use Bash for operator commands, confirmed topology, SSH, process orchestration,
  and transport. Use Python 3 for schemas, planning, identity, state, and tests.
- `release_spec/` owns canonical immutable specs, snapshot manifests, measurement
  contracts, the baseline policy evaluator, and public contribution verification.
  Preserve the exact model bytes, image, recipe arguments, and hardware geometry
  in the selected spec. Deployment settings do not authorize recipe changes.
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

A passing baseline is evaluation, not catalog admission. Released specs in
`releases/` are catalog members and are serveable regardless of review status.
The operator may label a release `experimental`, `stable`, `validated`, `failed`,
or `withdrawn`. Claiming `stable` or `validated` still requires the unchanged
baseline-v1 criteria. Failed or incomplete private attempts remain in the
workbench until released. Withdrawal retains a spec with its reason and does
not stop services or delete archives automatically.

Keep topology files, hostnames, addresses, SSH identity, user paths, credentials,
and raw experiment outputs out of tracked public files. Use the existing privacy
scanner before publication and its `--staged` mode before committing. Retain
licenses and attribution for carried-forward code. Fresh repository history does
not mean importing former experiment evidence or serving catalog entries.

Do not perform physical downloads, launches, service replacement, destructive
cleanup, or remote publication outside the maintainer's explicitly agreed scope.
Do not claim physical serving results from mocked tests. Resolve significant
changes to the agreed plan with the maintainer before implementing them.

## Verification and operator experience

Run directly affected tests while iterating, relevant subsystem tests after a
coherent change, and `scripts/selftest.sh` before commit or publication. Public
CI is standalone and must not depend on private repository access. Tests use
synthetic fixtures and parameterized doubles, with no real cluster mutations.
Keep Bash scenarios thin and data-heavy fixtures in Python.

Lead human output with the action, affected object, and blocker. Keep JSON
separate and stable, use hanging indentation for long paths, and check narrow
terminal output. Saved catalog observations show their age; unknown is not
absent, and prepared files do not imply a running or guaranteed launch.
