# Pulsar Inference Stack

Serve exact model recipes on NVIDIA DGX Spark GB10 systems. This public
repository owns the catalog, verified model files, recovery archives,
preparation and vLLM lifecycle commands. The separate private
`pulsar-inference-workbench` owns model onboarding and experiments.

The catalog begins empty. New entries require fresh baseline qualification,
a verified recovery archive and a reviewed contribution. Deterministic tests
of this stack do not establish physical serving results for any model.

```sh
./pulsar help
./pulsar models
./pulsar release list
```

The menu also supports direct commands. Browsing reads saved observations;
select **Check now** to refresh the chosen entry. Acquiring files, restoring
an archive, preparing copies and starting a service are separate operations.

Use [Operations](docs/OPERATIONS.md) for the complete operator workflow,
storage configuration and recovery. [Architecture](docs/ARCHITECTURE.md)
explains the boundaries between specs, files, serving and qualification.
[Contribution review](docs/CONTRIBUTIONS.md) describes the independent public
checks and the limits of compact evidence.

Run `scripts/selftest.sh` for deterministic tests without starting models.
Hardware, Docker, confirmed topology and SSH trust are required for actual
serving operations. Review the selected spec's exact image and hardware
geometry before preparing or launching it.

Useful code and tests carry forward under their existing licenses and
attribution. This repository has a fresh history and imports no previous
experiment results or serving recipes.
