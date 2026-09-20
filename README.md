# Pulsar Inference Stack

Serve exact model recipes on NVIDIA DGX Spark GB10 systems. This public
repository owns the catalog, verified model files, recovery archives,
preparation and vLLM lifecycle commands. The separate private
`pulsar-inference-workbench` owns model onboarding and experiments.

The catalog begins empty. The workbench maintainer decides which schema-valid
specs are published under `releases/`; the filename must equal the complete
`spec_id`. Nullable `state` and `review` metadata, evidence, archive state, and
launch compatibility do not gate membership. Deterministic tests of this stack
do not establish physical serving results for any model.

```sh
./pulsar help
./pulsar
./pulsar models
./pulsar release list
./pulsar topology show
./pulsar contract --json
```

On a terminal, `./pulsar` confirms cluster membership, SSH trust and archive
location before offering catalog actions. The catalog begins empty; browsing
with `./pulsar models` does not require topology. Direct commands still
perform one explicit action. Select **Check now** to refresh a chosen entry.
Acquiring files, restoring an archive, preparing copies and starting a
service remain separate operations.

Use `./pulsar gum` or bare `./pulsar` for the interactive menu. **Cluster
topology** offers saved membership, live checks, discovery, explicit
configuration and SSH trust. `./pulsar topology detect` discovers candidates
without saving; `./pulsar topology configure` asks before saving membership.
Workbench invokes the same public commands through its configured Stack executable.

For first use, choose **First-use setup** or run `./pulsar topology setup`.
It guides membership and SSH identity enrollment with separate confirmations,
then checks readiness. From the workbench, run these stack commands by the
configured absolute Stack executable path shown by `./workbench check`.

Use [Operations](docs/OPERATIONS.md) for the complete operator workflow,
storage configuration and recovery. [Architecture](docs/ARCHITECTURE.md)
explains the boundaries between specs, files, serving and qualification.
[Contribution review](docs/CONTRIBUTIONS.md) describes the independent public
checks and the limits of compact evidence.
[Decision references](docs/DECISIONS.md) explains legacy ADR identifiers still
present in source comments without requiring the predecessor repository.
`./pulsar contract --json` exposes the schema versions, policy digest, catalog
authority, and supported public commands. Clients check capabilities instead of
matching repository commits. See the [public contract](docs/CONTRACT.md).
The [diagnostic-container prototype](docs/DIAGNOSTIC.md) is retained for review
but is not an available public operation.

New specs freeze container settings as well as model/image/engine identity.
Operators may explicitly override supported execution settings; Stack reports
the resulting effective recipe separately from the selected catalog recipe.
Historical specs remain readable, and existing services are not restarted by
code updates. New operations support spec schemas 2 and 3; schema 3 binds required
draft checkpoints.

Select affected tests using [validation guidance](docs/TESTING.md).
Use `scripts/selftest.sh --checks-only` for fast syntax/catalog/privacy checks,
or `scripts/selftest.sh` when a full regression run is warranted; neither starts models.
Hardware, Docker, confirmed topology and SSH trust are required for actual
serving operations. Review the selected spec's exact image and hardware
geometry before preparing or launching it.

Useful code and tests carry forward under their existing licenses and
attribution. This repository has a fresh history and imports no previous
experiment results or serving recipes.
