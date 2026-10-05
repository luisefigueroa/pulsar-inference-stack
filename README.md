# Pulsar Inference Stack

Serve open-source AI models on NVIDIA DGX Spark GB10 systems using published, reproducible serving specifications and a common set of operational tools.

Pulsar brings model files, quantization, container images, execution settings, and hardware requirements together in a versioned **spec**. Its **recipe** defines how the model is served. The catalog gives operators a concrete configuration to inspect, prepare, and run, together with any published measurements for that exact recipe.

The goal is to make local serving a more informed choice: understand what a recipe was designed to achieve, the capabilities measured in testing, and the limits within which it was tested. Context length, concurrency, memory headroom, and performance are deliberate tradeoffs that can differ between recipes—even for the same model.

Pulsar manages model acquisition and serving copies, checks configuration and launch prerequisites, and starts or stops services. Verified archives provide a separate recovery path. Current serving support uses vLLM. You can browse, restore, prepare, and serve published specs using this public repository alone.

## Choosing a recipe

Start with the hardware requirements and configured operating limits, then review the published results for your intended workload.

Recipe development considers four priorities:

- **Stability:** whether the configuration operates reliably under the tested conditions.
- **Accuracy:** whether the served model preserves the capabilities measured by the selected evaluations.
- **Performance:** the throughput achieved for the tested workload and concurrency.
- **Latency:** the response times observed under those conditions.

These priorities guide experiments and iteration. Publication is a maintainer decision informed by the model, intended use, and available results; there is no universal benchmark score that every catalog entry must meet. Catalog membership itself is not a certification or a guarantee of performance on your installation.

### Model capability and recipe limits

A model’s advertised maximum context is distinct from the context configured in a serving recipe. A recipe may use a lower limit to leave memory headroom, support a particular concurrency, or improve operational reliability. Evaluate context and concurrency together: a large context window does not imply that every concurrent session can use that full capacity.

Review the selected spec’s node count, model files, image, execution settings, and any launch limitations before serving it. Published measurements apply to the configuration and conditions recorded with those results. Changes to hardware, settings, or workload can change the outcome.

### Interpreting measured results

Where comparable evaluations are available, results can be compared with model-provider or independent reference evaluations to assess whether specific capabilities are preserved in local serving. Such comparisons should identify the benchmark, configuration, scope, and limitations. A match on one evaluation supports that tested capability; it does not establish equivalence across all workloads.

Evaluation coverage is still evolving. Preliminary results should be identified as preliminary, and capabilities without published measurements should remain explicitly uncharacterized.

Use `./pulsar models results SPEC` to inspect historical published results for the exact recipe. Saved results, file verification, and current service health describe different things. An execution override creates a modified recipe with its own identity and does not inherit the original recipe’s measurements.

Model onboarding and experimentation are maintained separately in the private `pulsar-inference-workbench` repository. Approved serving specifications are contributed to this public stack through pull requests.


## Before you start

Use Python 3.11 or newer, Bash, Git, and util-linux `flock`. Serving nodes need
Docker with NVIDIA GPU access and node-local storage for model files. Downloads
need the `hf` CLI and any required Hub authentication on the node receiving the
files. Inter-node operations need OpenSSH, rsync, iproute2, and the confirmed
control and RoCE connections required by the recipe.

Interactive menus use Gum, bundled for arm64 Linux. Initial key-based SSH login
must already work before Pulsar enrolls host identities. See
[installation prerequisites](docs/OPERATIONS.md#installation-prerequisites)
for controller tools, storage requirements, and noninteractive use.

```sh
git clone https://github.com/luisefigueroa/pulsar-inference-stack.git
cd pulsar-inference-stack
./pulsar help
./pulsar models list
```

Catalog browsing needs no configured cluster and reads saved information. It
does not download files or check whether a model is running. Run `./pulsar` on
a terminal for the menu; it offers setup and read-only browsing before the
cluster is configured. Archive storage is optional.

## Serve a catalog recipe

Run each step separately and inspect its result before continuing. These
examples use a one-node recipe. For a multi-node recipe, keep `--node NODE`
when choosing the source copy's location during acquisition or restoration,
but omit it from preparation, image, start, status, and stop commands. Those
commands use the recipe's node count and the confirmed topology.

### 1. Set up the cluster and select a spec

```sh
./pulsar topology setup
./pulsar topology show
./pulsar models list
./pulsar models show SPEC
```

Setup guides membership and SSH trust through separate confirmations, then
checks readiness. For the initial `models show`, `SPEC` can be the unique
12-character prefix displayed by `models list`. Copy the complete ID from its
`Spec ID` field for subsequent steps; image commands require all 64 characters.
Replace `NODE` with a confirmed hostname from `topology show`.

Inspect the selected spec's image, node count, required snapshots, and any
launch limitation. This walkthrough uses ordinary `start`. A recipe with a
serving guard requires [bounded guarded serving](docs/GUARDED_SERVING.md).

### 2. Obtain the model files

Download the exact target snapshot to a confirmed node, creating its verified
source copy, called its **home**:

```sh
./pulsar model acquire SPEC --snapshot target --node NODE --yes
```

If you have an archive, [configure its location](docs/OPERATIONS.md#archive-location)
and restore instead of downloading:

```sh
./pulsar model restore SPEC --snapshot target --node NODE --plan
./pulsar model restore SPEC --snapshot target --node NODE --yes
```

For schema-3 specs, repeat acquisition or restoration for every additional
snapshot declared by the spec, replacing `target` with its name. A checkpoint
bundled within the target needs no second download. See
[required snapshots](docs/OPERATIONS.md#recipes-requiring-a-draft-checkpoint).
Continue when every required snapshot has a verified home.

### 3. Prepare the serving copies

```sh
./pulsar model prepare SPEC --node NODE --plan
./pulsar model prepare SPEC --node NODE --yes
```

The preview reports reuse, transfers, and storage capacity. Preparation makes
the complete recipe's files available on every serving node; it does not start
the model.

### 4. Check and stage the exact image

```sh
./pulsar image check SPEC --node NODE
```

If the pinned image is missing, review and explicitly apply a registry pull:

```sh
./pulsar image stage SPEC --node NODE --pull --plan
./pulsar image stage SPEC --node NODE --pull --yes
```

Staging verifies the image on the selected serving nodes. It does not launch
or replace a service. For an image already held locally, see
[image staging options](docs/OPERATIONS.md#check-and-stage-the-image).

### 5. Start, use, and stop the service

Configure any API authentication before starting; see
[your first API request](docs/OPERATIONS.md#send-your-first-api-request).

```sh
./pulsar start SPEC --node NODE --dry-run
./pulsar start SPEC --node NODE
./pulsar status SPEC --node NODE
```

The dry run reports prerequisites; start repeats its checks and performs the
launch. If a command reports a blocker, follow its next step before continuing.
Start neither pulls a missing image nor replaces an existing service without
the corresponding explicit flag.

Ordinary start prints `READY` only after a test completion succeeds. Use the
reported API URL and served model name for your requests. `status` checks the
live service separately from saved catalog observations.

When finished:

```sh
./pulsar stop SPEC --node NODE
```

Stop retains model files, pins, and archives. Storage cleanup is a
[separate operation](docs/OPERATIONS.md#stop-and-reclaim-storage).

## Documentation

| I want to… | Read |
| --- | --- |
| Set up, serve, inspect, or recover a model | [Operations](docs/OPERATIONS.md) |
| Resolve a blocked or failed operation | [Troubleshooting](docs/OPERATIONS.md#troubleshooting) |
| Understand specs, storage, and ownership | [Architecture](docs/ARCHITECTURE.md) |
| Integrate with commands, JSON, or spec formats | [Public contract](docs/CONTRACT.md) |
| Run a bounded trial with enforced limits | [Guarded serving](docs/GUARDED_SERVING.md) |
| Supply a reviewed estimate of resident weights | [Memory estimates](docs/MEMORY_ESTIMATES.md) |
| Review or publish a catalog contribution | [Contribution review](docs/CONTRIBUTIONS.md) |
| Choose checks for a code or documentation change | [Testing](docs/TESTING.md) |
| Understand inherited decisions and historical plans | [Decision references](docs/DECISIONS.md) |

## Catalog and evidence

The maintainer publishes schema-valid specs under `releases/`, with filenames
equal to their complete spec IDs. Nullable `state` and `review` values,
measurements, archive observations, and launch compatibility do not determine
catalog membership. A catalog entry does not promise that its files are present
or that it can run on this installation.

New operations support spec schemas 2 and 3. Historical specs remain readable;
code updates do not automatically restart existing services. Integrators can
inspect `./pulsar contract --json` for supported capabilities instead of matching
Git commits. See [the public contract](docs/CONTRACT.md).

## Development and attribution

Use [Testing](docs/TESTING.md) to select validation for the changed behavior.
`scripts/selftest.sh --checks-only` runs fast syntax, catalog, and privacy checks;
`scripts/selftest.sh` also runs the full deterministic regression suite when
warranted. Neither starts models, and neither establishes physical serving
results.

Carried-forward code retains its licenses and attribution. See [LICENSE](LICENSE)
and [third-party notices](THIRD_PARTY_NOTICES.md). This repository began with fresh
history, without importing predecessor experiment results or serving recipes.
