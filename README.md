# Pulsar Inference Stack

Serve exact model recipes on NVIDIA DGX Spark GB10 systems. This public
repository owns the catalog, verified model files, archives,
preparation and vLLM lifecycle commands. The separate private
`pulsar-inference-workbench` owns model onboarding and experiments.

Specs appear when the maintainer publishes them under `releases/`. The
workbench maintainer decides which schema-valid specs are published; the
filename must equal the complete `spec_id`. Nullable `state` and `review`
metadata, evidence, archive state, and launch compatibility do not gate
membership. Deterministic tests of this stack do not establish physical
serving results for any model.

```sh
./pulsar help
./pulsar
./pulsar models
./pulsar topology show
./pulsar contract --json
```

On a terminal, `./pulsar` first offers the next setup step (cluster membership
and SSH trust, then an archive location), read-only catalog browsing, and Exit;
catalog and cluster actions follow once this checkout is bound. Specs appear
when the maintainer publishes them under `releases/`; browsing with
`./pulsar models` does not require topology. Direct commands still perform one
explicit action; `./pulsar models check SPEC` refreshes a chosen entry.
Acquiring files, restoring an archive, preparing copies and starting a service
remain separate operations. The menus draw with Gum, bundled for the arm64
nodes; every menu action is also a command.

For first use, choose **Set up cluster membership and SSH trust**, or run
`./pulsar topology setup`. It guides membership and SSH identity enrollment
with separate confirmations, then checks readiness. From the workbench, run
these stack commands by the configured absolute Stack executable path shown by
`./workbench check`.

After setup, the bare `./pulsar` menu adds **Catalog and storage** and
**Cluster topology**, which offers saved membership, live checks, discovery,
explicit configuration and SSH trust. `./pulsar topology detect` discovers
candidates without saving; `./pulsar topology configure` asks before saving
membership. Workbench invokes the same public commands through its configured
Stack executable.

To serve a spec, pick it from the catalog and run each step as its own command:

```sh
./pulsar models
./pulsar model prepare SPEC --node NODE --yes
./pulsar start SPEC --node NODE
./pulsar status SPEC
./pulsar stop SPEC
```

SPEC is a catalog spec ID or a unique prefix of at least 12 characters, as
`./pulsar models` shows it. A one-node spec names its node (hostname) with
`--node`; a multi-node spec takes its nodes from the confirmed topology and
omits it. Preparation needs the model files first: `./pulsar model acquire` or
`./pulsar model restore`, as the catalog's suggested step shows.

Use [Operations](docs/OPERATIONS.md) for the complete operator workflow,
storage configuration and recovery. [Bounded guarded serving](docs/GUARDED_SERVING.md)
describes foreground trials with enforced limits and owned cleanup.
[Architecture](docs/ARCHITECTURE.md)
explains the boundaries between specs, files, serving and qualification.
[Contribution review](docs/CONTRIBUTIONS.md) describes the independent public
checks and the limits of compact evidence.
[Decision references](docs/DECISIONS.md) explains legacy ADR identifiers still
present in source comments without requiring the predecessor repository.
`./pulsar contract --json` exposes the schema versions, policy digest, catalog
authority, and supported public commands. Clients check capabilities instead of
matching repository commits. See the [public contract](docs/CONTRACT.md).

New specs freeze container settings as well as model/image/engine identity.
Operators may explicitly override supported execution settings; Stack reports
the resulting effective recipe separately from the selected catalog spec's recipe.
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
