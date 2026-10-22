# Pulsar Inference Stack

Run exact, inspectable vLLM serving configurations on NVIDIA DGX Spark GB10.

Pulsar manages model files, prepares them on the selected machines, checks the
serving configuration, and starts or stops the service. Verified archives
provide a separate recovery path.

You select a **spec** from the catalog: a serving specification that fixes the
model files, container image, execution settings, and required hardware together.
Its **recipe** is the execution configuration. You can browse, restore, prepare,
and serve published specs using this public repository alone. The separate
private `pulsar-inference-workbench` owns model onboarding and experiments.

[Get started](#before-you-start) ·
[Browse recipes](docs/README.md#browse-recipes-and-evidence) ·
[Get help](SUPPORT.md) · [Contribute](CONTRIBUTING.md)

See the [documentation home](docs/README.md) for guides by task.

[Third-party software, images, and credits](THIRD_PARTY_NOTICES.md) documents
upstream sources, adapted patches, and license notices across the catalog.

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
./pulsar
```

The interactive menu offers read-only catalog browsing before cluster setup.
Browsing reads saved information; it does not download files or check whether a
model is running. When you are ready to operate a recipe, choose **Set up cluster
membership and SSH trust**, then use **Catalog and storage** after setup.
Archive storage is optional.

See the [menu reference](docs/OPERATIONS.md#menu-reference) for interactive
actions. The walkthrough below gives the corresponding direct commands.

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

The [documentation home](docs/README.md) groups guides for operators,
contributors, and integrators, with links to provenance and design history.
For day-to-day work, start with [Operations](docs/OPERATIONS.md) or
[Troubleshooting](docs/OPERATIONS.md#troubleshooting).

## Catalog and evidence

The maintainer publishes schema-valid specs under `releases/`, with filenames
equal to their complete spec IDs. Nullable `state` and `review` values,
measurements, archive observations, and launch compatibility do not determine
catalog membership. A catalog entry does not promise that its files are present
or that it can run on this installation.

`./pulsar models results SPEC` shows historical published results for the exact
recipe. Those results, saved file checks, and live service health are distinct.
Changing execution settings with an override creates a modified recipe with its
own identity. It does not inherit the selected recipe's measurements.

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
