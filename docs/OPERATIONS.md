# Operate the inference stack

Start with the [README walkthrough](../README.md#serve-a-catalog-recipe) for the
short path from a fresh checkout to a service. This guide explains each task,
its prerequisites, and what to inspect when it cannot complete. Run commands
from the Stack checkout; replace angle-bracket placeholders before using them.

## Contents

- [Installation prerequisites](#installation-prerequisites)
- [Terms](#terms)
- [Configure topology and storage](#configure-topology-and-storage)
- [Browse and check](#browse-and-check)
- [Obtain, prepare and start](#obtain-prepare-and-start)
- [Send your first API request](#send-your-first-api-request)
- [Inspect a running service](#inspect-a-running-service)
- [Move, archive and restore](#move-archive-and-restore)
- [Stop and reclaim storage](#stop-and-reclaim-storage)
- [Troubleshooting](#troubleshooting)
- [Menu reference](#menu-reference)
- [Verify model files](#verify-model-files)
- [Output and messages](#output-and-messages)
- [Historical specs and current observations](#historical-specs-and-current-observations)
- [Recipes requiring a draft checkpoint](#recipes-requiring-a-draft-checkpoint)
- [Maintainer experiments and migration](#maintainer-experiments-and-migration)

## Installation prerequisites

Run commands from the public Stack checkout on the controller machine. The
[README walkthrough](../README.md#serve-a-catalog-recipe) starts with obtaining
that checkout. Catalog browsing and document commands need no configured cluster.

| Where | Required tools and access |
| --- | --- |
| Controller | Python 3.11 or newer, Bash, Git, util-linux `flock`, and curl for service checks and requests |
| Serving nodes | Python 3.11 or newer, Bash, util-linux `flock`, NVIDIA DGX Spark GB10, Docker with NVIDIA GPU access, and node-local storage for homes and prepared copies |
| Inter-node operations | OpenSSH, rsync, iproute2, working key-based SSH login, and confirmed control and RoCE endpoints as required by the recipe |
| Node receiving a download | A modern `hf` CLI and local Hub authentication when the source requires it |

Pulsar enrolls SSH host identities; it does not install remote login keys.
Choose a recipe whose hardware geometry matches the available confirmed nodes.
Inspect storage with `./pulsar model budget` after setup and with each operation's
preview before copying files.

### Interactive and noninteractive commands

Menus and confirmation prompts draw with Gum: the repository bundles it for the
arm64 Linux nodes (`third_party/gum/`); elsewhere install `gum` from the
package manager or point `GUM_BIN` at it. `NO_COLOR` or
`PULSAR_COLOR=never` draws them without color. Without Gum or an interactive
terminal, a menu opens nothing: it names the equivalent commands and exits
with status 2, and a command that would ask for confirmation names its `--yes`
form. Diagnostic output reports missing prerequisites instead of silently
choosing other tools or transport.

### Storage locations

Operator overrides `PULSAR_HOME_ROOT` and `PULSAR_HOT_ROOT` select storage roots;
a spec's deployment-overlay `cache_root` selects its home acquisition root.
Default roots are under each node user's `.cache/pulsar-inference-stack/`.
Known homes remain explicit records, including homes created with another
root. Changing a configured path does not migrate existing files. Use a shared
controller state directory for one cluster; contribution worktrees do not
operate serving resources.

Live homes and prepared copies must be on node-local serving storage. Runtime
checks reject known network filesystems such as NFS for those paths. Recovery
archives may use the operator's mounted storage; archive configuration never
prescribes mount options or claims failure-domain independence. Archive locations
must not overlap directories managed as removable homes or working copies.

## Terms

Commands and guides use these terms. Public flags, environment variables,
JSON fields, and container labels retain their existing names.

| Term | Meaning |
| --- | --- |
| spec | The serving specification you select: a catalog spec published under `releases/`, or a candidate passed with `--spec-file`. Shown as its spec ID or a 12-character prefix. |
| recipe | The spec's execution configuration: image, engine arguments, container and geometry. An override produces a modified recipe. |
| model files | The model's bytes, verified against the spec's snapshot manifest. |
| snapshot | An exact set of model files from one model commit. Its manifest lists the files, sizes, and hashes used to verify that set. |
| home | The one verified source copy of a snapshot, on a confirmed member. |
| prepared copies | The per-rank copies or bindings that serving reads. |
| archive, archive location | The verified recovery copy, and the directory that holds archives. |
| node | A confirmed machine, named by its hostname. |
| rank | A serving slot in the spec's geometry, always shown with its node, such as `spark-2 (rank 1)`. |

## Configure topology and storage

### Confirm membership and SSH trust

Confirmed topology determines membership and physical node identity. On a
terminal, `./pulsar` offers the next setup step (**Set up cluster membership
and SSH trust**, which runs `./pulsar topology setup`; **Enroll SSH trust**
when membership is already saved), read-only catalog browsing
(`./pulsar models menu --read-only`), diagnostics, saved topology, Help and Exit.
Archives are optional and do not restrict navigation after cluster setup.
A setup step that fails reports that it did not complete before the menu returns.
Entering that menu uses saved local files; it does not probe nodes. After
cluster setup, open `./pulsar` and choose **Cluster topology**. The
same actions are available directly:

```sh
./pulsar topology setup
./pulsar topology show
./pulsar topology check --json
./pulsar topology detect --json
./pulsar topology detect --candidate HOST
./pulsar topology configure
```

Start with `setup` on a fresh checkout. It guides membership configuration,
checks SSH enrollment, offers missing enrollment with a separate key-confirmation
prompt, and finishes with a topology readiness check. Healthy existing setup is
reused. Cancellation or failed enrollment cannot report success. This applies
to single-node clusters too. The first-run menu's setup step and the topology
menu's **First-use setup** run this same command.
Initial key-based SSH login must already work; this flow verifies and records
host identities, rather than installing remote login keys. No step starts or
stops a model. For noninteractive agents, use configuration and SSH enrollment
as separate explicitly approved commands; guided setup itself requires an
interactive terminal with Gum.

### Inspect or change membership

`show` reads saved membership without probing. `check` checks every saved node's
identity, confirmed control endpoint, platform readiness and pairwise fabric
connectivity. Its JSON distinguishes missing, invalid and blocked state from
ready; a saved row alone does not establish current readiness. These observations
do not prepare files, choose model geometry or promise a model can start.

`detect` probes local, advertised, explicitly supplied and previously confirmed
nodes without saving membership or enrolling new SSH trust. Repeat `--candidate`
for more addresses. Missing confirmed nodes remain visible and make discovery
incomplete; a partial scan cannot silently remove them. Invalid saved state also
blocks automatic replacement. Investigate identity, trust or connectivity before
retrying. The saved configuration remains untouched.

`configure` displays discovered membership and changes, then asks before saving.
Noninteractive use requires explicit `--yes`. Active services or an unobservable
required node block replacement; the check repeats after confirmation. It never
stops services for you. Saving a discovery establishes membership, not enrolled
SSH trust; use the menu's separate SSH-trust enrollment action or
`./pulsar ssh-trust enroll` afterward. Explicit `--accept-new-host-keys` is
available only on configuration through this CLI, not on read-only detection.
Before the prompt, configuration reports homes, prepared views, and pins affected
by the proposed topology. The report is advisory and read-only; saving membership
does not silently move, purge, unpin, or rewrite model-library records.

Show, check and detection support `--json`. `./pulsar topology menu` opens just
this menu; opening either menu performs no probes. Every menu choice runs one of
the commands above. Low-level manifest utilities remain available under
`pulsar topology` with their existing arguments. From the private workbench,
invoke the configured public Stack executable by absolute path; Workbench creates no
second topology implementation.

Topology and SSH files stay private in the selected stack checkout. Host and
fabric diagnostics are also available through `./pulsar doctor`. Catalog
browsing works before topology is configured.

### Archive location

A **home** is the complete local copy of one exact snapshot on a confirmed member. Serving
nodes use prepared working copies. The library records their explicit
locations; a snapshot manifest supplies the expected file list and hashes.
Files on every participating node must match before serving starts.

Select an existing archive location with:

```sh
./pulsar configure archive-root
./pulsar configure archive-root show
./pulsar configure archive-root set <existing-directory> --yes
```

The same workflow is available through **Archive storage configuration** after
cluster setup. `PULSAR_COLD_ROOT` is the explicit configuration
variable: process value first, then the repository's `.env`; empty disables
archives. Pulsar does not create, mount or administer the selected directory.
The operator owns its access controls and choice of independent storage.
Existing non-Pulsar content stays untouched. Archive storage is never mounted
into serving containers. Disabling this configuration does not delete archives.

## Browse and check

Browse the catalog before or after configuring a cluster. A schema-valid spec
under `releases/` is a catalog member; state and review metadata do not gate
membership or serving. Reading the catalog does not probe nodes.

Commands typed by a person accept a unique catalog spec ID prefix of at least
12 characters, as `models list` shows it, and print the complete ID they
selected. An ambiguous or unknown prefix fails and names the matching IDs.
`--json`, `--spec-file`, `model purge` and `model remove` require the complete
64-character ID, so scripts and the workbench never depend on a prefix. Human
output names machines by their saved hostname; JSON keeps the stable `node_id`.

Image commands require the complete ID too. Using complete IDs throughout a
workflow avoids changing selectors when moving between command families.
`models show` accepts the prefix from the listing and prints a complete
`Spec ID` field to copy for subsequent operations.

```sh
./pulsar models list
./pulsar models list --json
./pulsar models show <spec-id>
./pulsar models results <spec-id>
```

### Read recipe details

The selected-spec menu separates **Selected recipe**, **Current observations
(saved)** and **Published results**. Recipe geometry and explicitly recorded
context, sequence and quantization settings describe intended execution; the
menu does not infer engine defaults or resolve repeated arguments. The image
digest is abbreviated here; **Show details** retains the complete identity and
arguments. Maintainer reviews are advisory and keep their dates separate from
measurement dates. Saved file checks retain their age, and the summary explicitly says
that it has not observed the live service.

### Read published results and compare recipes

**Published results** (also `models results SPEC`) reads only this catalog's
`results/` records for the selected spec and uses the existing evidence verifier.
It shows recorded run times, run and policy outcomes, benchmark token counts and
concurrency, accuracy sample scope, soak results, and the command for the full
public evidence summary. The compact view shows the most recent dated verified
run regardless of outcome and counts undated or unverified records separately.
Missing, inconsistent or unsupported evidence is explicit and never removes a
spec or blocks its operations. Historical schema-1 evidence retains its format;
the results view gives the command to inspect its bound references.

These are historical results for the exact published recipe and workloads, not
a current health or suitability score. Run dates are measurement dates, not
publication dates. Workbench continues to own authoring, qualification and
publication. Private experiments and approvals are not read by these views.
The existing `models list --json`, `models show --json` and integration contract
are unchanged; `models results` is a human report with a separate JSON evidence
command for each verified run.

**Compare catalog specs** invokes the existing `pulsar spec compare` for the
selected spec and another published schema-2 or schema-3 spec. The output names
both identities and every changed recipe field. Comparison leaves the current
selection unchanged and makes no claim that their results are comparable across
different workloads. Both inspection actions are available in the read-only
catalog menu before cluster setup.

### Refresh saved file and archive observations

```sh
./pulsar models check <spec-id> --node <node>
```

Omit `--node` for a multi-node recipe. This explicitly checks known managed
locations and saves an observation; ordinary browsing reads those saved results.

The catalog includes every released recipe even when no local files are
present. Each spec is one block that leads with its saved state: recipe
geometry, files, archive and, when saved records point to one, the
**Suggested** next step as a command to paste. State and review rows appear
only when the spec sets them. The archive line reconciles the last check with
the archive verification record and shows the strongest fact with its age,
such as `verified 13 hours ago` or `not found at last check (verified 19 days
ago before that)`. Details add exact identity, the image digest, engine
arguments, the home, per-rank prepared copies, pins and blockers. A **rank** is
this job's slot in the serving group; rank 0 provides the API and need not
hold the home.

Recovery suggestions use those same archive facts for the next snapshot that
needs a home. An archive observed as present is not described as verified;
older verification keeps its age visible, and a newer missing or unavailable
observation takes precedence. When timestamps cannot establish their order,
the display says so. A Restore suggestion always notes that Restore rechecks
contents before copying. With several required snapshots, the suggestion names
one snapshot and does not imply that the other archives are available.

Compact catalog labels prioritize withdrawal and unsupported-start warnings
over model/file details. The selected-spec menu repeats a withdrawal's reason
and recorded review date, when supplied, before its suggested action. Start
and the memory-warning retry show the same wrapped notice before their existing
confirmation. These are advisory maintainer metadata: they do not change
catalog membership, launch checks or the number of confirmation steps.

Normal browsing reads saved observations and shows their age. Unobserved
state is unknown. A saved successful check is not a promise that the next
launch will work. `models check` (**Check now** in the menu) checks the
selected managed locations; it does not search arbitrary cache trees, download
files or start a server. Files prepared on every rank does not mean a service
is running. Use [live service inspection](#inspect-a-running-service) to check
the service itself.

### Withdrawal and catalog removal

Withdrawn recipes remain visible with their reason. They are not recommended,
but their exact configuration can still run when operational checks pass.
Withdrawal never stops services or removes model files or archives. A spec
removed from the catalog is listed in `catalog-removals.json`; manage any
remaining local storage for it with `--spec-file` and its retained document.

## Obtain, prepare and start

These procedures use a current catalog spec with ordinary launch support.
Replace `<spec-id>` with its complete ID and `<node>` with a confirmed hostname
or node ID. Run one action at a time, and continue only after its result is
understood. A spec with a guard uses [guarded serving](GUARDED_SERVING.md).

For a one-node recipe, `--node` selects the serving node. For a multi-node
recipe, omit it from preparation, image, start, status, and stop commands;
ordinary serving uses the first required number of confirmed nodes. Acquisition
and restoration still use `--node` to choose the home, which may be outside the
serving group. Geometry comes from the spec, not from all discovered machines.

### Acquire or restore every required snapshot

Download the exact target snapshot to its home:

```sh
./pulsar model acquire <spec-id> --snapshot target --node <node> --yes
```

If a verified archive is available, [configure its location](#archive-location)
and restore instead:

```sh
./pulsar model restore <spec-id> --snapshot target --node <node> --plan
./pulsar model restore <spec-id> --snapshot target --node <node> --yes
```

`--snapshot target` works for spec schemas 2 and 3. For schema 3, repeat the
selected operation for every additional named snapshot in the spec before
preparing. See [recipes requiring a draft checkpoint](#recipes-requiring-a-draft-checkpoint).
A checkpoint bundled inside the target requires only that complete target copy.
Successful acquisition or restoration establishes a verified home; neither
operation prepares the other ranks or launches a service.

Acquisition resolves the exact source commit, checks the complete upstream
Git/LFS inventory, downloads into private staging and verifies content before
publishing a home without replacing another directory. Matching verified
bytes can be reused. Missing files or a different revision do not trigger
an alternate download path.

### Prepare all serving nodes

```sh
./pulsar model prepare <spec-id> --node <node> --plan
./pulsar model prepare <spec-id> --node <node> --yes
```

Read the preview's reuse, copy, and capacity results before applying it. The
completed operation makes every required snapshot available on every selected
serving rank. It does not start a model. See [prepared copies and storage
estimates](#prepared-copies-and-storage-estimates) for sharing and retry behavior.

Control SSH, inference traffic and model transfer use distinct configured
paths. Multi-node preparation preserves the selected transfer contract and
does not silently change networks.

### Check and stage the image

Image commands require the complete spec ID. First inspect the exact pinned
image on the selected serving nodes:

```sh
./pulsar image check <spec-id> --node <node>
```

If it is missing, select a registry pull explicitly, review its preview, and
then apply the same selection:

```sh
./pulsar image stage <spec-id> --node <node> --pull --plan
./pulsar image stage <spec-id> --node <node> --pull --yes
```

Staging finishes by checking the exact image reference on every selected node.
It does not start or replace a service. For a verified image held on the
controller, `--export-tag TAG` instead of `--pull` selects a named export to
the other serving nodes. The tag must be in the pinned image's repository and
resolve to that exact digest and image ID. A destination tag conflict fails;
the export never falls back to a registry pull. See the [image contract](CONTRACT.md#documents-and-identity).

### Check prerequisites and start

If the service should require authentication, configure `VLLM_API_KEY` (or
the `API_KEY` fallback) in its private launch environment before starting.
Use the same key for clients and subsequent authenticated checks. Keys belong
in private configuration, never a published spec or example. See
[your first API request](#send-your-first-api-request).

```sh
./pulsar start <spec-id> --node <node> --dry-run
./pulsar start <spec-id> --node <node>
```

The dry run checks prerequisites without launching or staging an image. It
identifies checks deferred to actual start, including the multi-node preflight.
Actual start rechecks its prerequisites; ordinary start prints `READY` only
after the service answers a test completion. Its output includes the served
model name and API URL. A refusal or failed launch needs inspection before a
retry; see [troubleshooting](#troubleshooting).

Start does not replace an existing exact-name service and does not pull a
missing image by implication. After inspecting the current service, pass
`--replace` only with explicit replacement approval. Pass `--pull-image` only
when staging the selected digest-pinned image is also approved. Generic `--yes`
does not grant either permission.

### Recipe identity and deployment settings

Start immediately rechecks the actual model files, image, recipe, geometry,
placement, capacity and ownership. Recipe or image changes require another
spec. Deployment settings such as API port, served name and placement remain
separately configurable through the deployment overlay.

Schema-2 recipes explicitly freeze container parameters and literal environment,
including multi-node `NCCL_IB_QPS_PER_CONNECTION`. Supported execution changes
use `--override-file` and produce a new effective spec ID, displayed as a modified
recipe. The selected catalog entry and its historical measurements are unchanged.
The immutable private launch plan records site bindings. Observation verifies
actual configuration against that plan; a Stack commit change does not require
restarting a container. See [the public contract](CONTRACT.md) for exact fields.

## Send your first API request

Use the `url` and `served` values printed by a successful ordinary start.
`./pulsar status <spec-id> --node <node>` also reports the observed API URL.
The URL already includes `/v1`; the model name is the served name, not the spec
ID. Use a client machine that can reach that endpoint. The loopback URL below
is an example for a client running on the API node; replace it with the reported
URL for your service, and replace `SERVED_MODEL` inside the JSON.

For a service without API authentication:

```sh
PULSAR_API_URL='http://127.0.0.1:8000/v1'
curl --fail-with-body --silent --show-error \
  "$PULSAR_API_URL/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"SERVED_MODEL","prompt":"Write one sentence about the Moon.","max_tokens":32}'
```

For an authenticated service, make the key configured at launch available in
the client's `VLLM_API_KEY` environment variable. Use this request instead,
again replacing the URL and `SERVED_MODEL`:

```sh
PULSAR_API_URL='http://127.0.0.1:8000/v1'
curl --fail-with-body --silent --show-error \
  "$PULSAR_API_URL/completions" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${VLLM_API_KEY:?Set the service API key in this shell}" \
  -d '{"model":"SERVED_MODEL","prompt":"Write one sentence about the Moon.","max_tokens":32}'
```

If the client uses `API_KEY` instead, substitute that variable in the header. A key
loaded by Pulsar from its private `.env` is not automatically loaded into the
shell running curl. Changing a client variable does not change the running
service's configured key.

A successful request returns JSON with generated text in `choices[0].text`.
This small completion demonstrates API use; it is not an accuracy benchmark or
qualification result. A connection failure calls for checking the reported
endpoint and live status; an HTTP authentication error calls for checking that
the client and running service use the same key. See [troubleshooting](#troubleshooting).

## Inspect a running service

```sh
./pulsar status <spec-id> --node <node> --json
./pulsar inventory
./pulsar inventory menu
```

`status` leads with its answer, for example `spec 139908cf23bb: running and
healthy on spark-1; recipe and files verified`. It asks the service's API
`/health` once. When the complete observation is unavailable, such as for a
service started before launch records existed, it reports the inventory's view
and says it is not verified, and why. It tells "no service exists" apart from
"a node could not be observed".

**Live service inventory** in the root menu opens `inventory menu`. Select an
observed service whose spec is published in the catalog, then choose
**Detailed status** or **Stop service**. These invoke the existing status and
stop commands. One-node services use the node ID recorded in the inventory;
multi-node services retain the commands' participating-node resolution.
The inventory timestamp remains visible, and **Refresh inventory** obtains a
new observation. **Show full inventory** retains the existing read-only view
of all detected workloads, including those outside the catalog.

Stop refreshes the inventory and shows the current scope before confirmation.
It is unavailable when inventory has not established safe ownership and
observability, or a one-node service has ambiguous placement. The stop command
still rechecks ownership and can refuse; the menu does not bypass its guards.
Model files, pins and archives are retained. The ordinary `inventory` and
`inventory --json` commands remain read-only reports.

### Verify the recorded service and files

`pulsar observe --service-id ID --json` verifies the effective recipe, container
configuration and continuity on every rank, using normal metadata-based file
verification. Add `--full` to request a fresh full content audit. Archive
verification and verification of newly copied bytes remain full audits.
Prepared-copy inspection in `model info`, `model check` and `observe` uses up to
three verification workers, with at most one per physical node. The batch covers
homes and working copies for every required snapshot; shared references to the
same physical copy are verified once. `--verification-jobs N` sets the bound;
use `--verification-jobs 1` for serial inspection. The mode is independent of
`--full`: normal calls still reuse valid metadata, and full audits still hash
every file. Final preparation checks use the same prepared-set inspection.
Copies, archive audits and publication keep their existing sequence.

A failed verification stops queued work and cancels active peer workers through
the same owned-worker cleanup used for interruption. Stack retains the original
failure and reports unconfirmed cleanup separately. Caller cancellation is tracked
separately from worker signal failures and node lease expiry, whose diagnostics
remain in the command's error response. Successful individual verifications may
refresh their records, but incomplete snapshot/rank coverage
cannot produce a ready prepared set. Results use a fixed snapshot and rank order,
independent of worker completion order.

Qualification uses normal Stack verification; legacy `files_verified` fields
remain in serialized records for compatibility but do not gate current
qualification or determine whether the serving runtime changed.

### Sample resources

`pulsar resources --service-id ID --jsonl` samples private node/container metrics.
To begin before launch, use `--spec-file FILE` and, for a one-node recipe,
optional `--node NODE`; it never starts the model. Container metrics remain
unavailable until the matching owned recipe appears. Stop the stream to end
sampling; model services are unaffected.

## Move, archive and restore

Choose the operation needed; these commands are not a required sequence.
The examples select `target`; for schema 3, repeat archive or recovery work
for each required snapshot using its declared name.

### Move a home or create an archive

```sh
./pulsar model move <spec-id> --snapshot target --node <destination-node> --yes
./pulsar model archive create <spec-id> --snapshot target --yes
./pulsar model archive verify <spec-id>
```

Recipes using identical model bytes share one snapshot archive. Movement
verifies existing bytes or a transfer before changing the home record.
Archives and restoration run in the foreground; failures remain explicit.
No background archive-job or receipt-recovery service is required.

### Restore after controller loss

After controller loss, use a fresh public stack checkout, configure the
archive location and confirmed topology, select the catalog spec and restore.
The restored bytes must match the spec manifest before local records are
rebuilt. Prepare and start are subsequent explicit operations. Restoration
requires neither the private workbench nor access to Hugging Face.

```sh
./pulsar model restore <spec-id> --snapshot target --node <node> --plan
./pulsar model restore <spec-id> --snapshot target --node <node> --yes
```

`restore --plan` and the menu's Restore preview show the selected snapshot's
file count and full payload size, the destination home root and observed free
space there. Restore plans a full snapshot copy with no existing bytes reused.
The displayed disk space after copying is an estimate, without filesystem
overhead or a reservation. Failure to observe free space leaves capacity
unknown; it does not bypass or replace archive verification, home-absence checks
or the explicit confirmation before restoration.

### Check archive integrity

The catalog's last archive-verification time is saved information, not current
archive health. Use **Verify archive** to perform a fresh, read-only integrity
check. Use **Check now** with full verification when the resulting observation
should also be recorded in local catalog state.

## Stop and reclaim storage

Stop a service independently of storage cleanup:

```sh
./pulsar stop <spec-id> --node <node>
```

Omit `--node` for a multi-node service. For a guarded trial, follow its
[owned stop procedure](GUARDED_SERVING.md#observe-and-stop).

### Pin or reclaim prepared copies

The commands below are alternatives, not a sequence to run together. Pinning
protects prepared copies; unpinning permits their later removal. Use the complete
spec ID for `purge` and `remove`. Preview the intended cleanup before applying it.

```sh
./pulsar model pin <spec-id> --yes
./pulsar model unpin <spec-id> --yes
./pulsar model purge <spec-id> --plan
./pulsar model purge <spec-id> --yes
```

Stop retains files, pins and evidence. Purge refuses active or pinned prepared
copies and preserves homes and archives. It removes only the selected recipe's
bindings; working-copy files remain while any other recipe has a binding, even
an unpinned one. The final binding can delete the working copy only after the
usual ownership, container and incomplete-operation checks. Container references
to a shared path, including stopped containers, still block purge. Before
removing a catalog model's home, clear its dependent prepared copies and verify
its archive.
An unpromoted model may be explicitly discarded without an archive after
dependencies are cleared; private recipes and experiment results remain.
All protections apply to the shared snapshot, not only the selected recipe.

### Remove an unused home

Removal acts on a home, rather than its prepared-copy bindings. Clear the
dependencies and satisfy the archive requirements described above first:

```sh
./pulsar model remove <spec-id> --snapshot target --plan
./pulsar model remove <spec-id> --snapshot target --yes
```

For schema 3, name the snapshot whose home you intend to remove. These operations
preserve archives.

### Shared-record compatibility

Sharing upgrades the affected private working-copy records to schema 3 during
the approved preparation. Their keys, paths and pins are preserved, and retries
reconcile partial controller/node upgrades. Older Stack code rejects these
records. Finish older model-library operations before the first shared prepare
and use the updated Stack for subsequent storage operations; downgrading its
code does not downgrade shared ownership. A preview never converts records.

There is no archive-deletion command. Catalog edits and ordinary cleanup never
delete archives; permanent removal belongs to deliberate storage administration.

## Troubleshooting

Read the affected node, blocker, and `Next:` command in the failed operation's
output. Investigate the reported condition before retrying. The table starts
with inspection; flags that allow downloads, replacement, or a memory warning
remain explicit decisions.

| Symptom | First check | Next action |
| --- | --- | --- |
| Setup incomplete or a node unreachable | `./pulsar topology show`, then `./pulsar topology check` | Repair membership, SSH trust, or connectivity using [topology setup](#configure-topology-and-storage). |
| Docker unavailable | `./pulsar doctor` | Resolve the reported Docker or GPU-access prerequisite on the affected node. |
| Pinned image missing | `./pulsar image check <spec-id> --node <node>` | Review [image staging](#check-and-stage-the-image) for the same spec and nodes. |
| Model files missing or not prepared | `./pulsar model info <spec-id> --node <node>` | Acquire or restore missing homes, then prepare every required snapshot and rank. |
| Too little storage | `./pulsar model budget` and the operation's `--plan` | Review [storage cleanup](#stop-and-reclaim-storage); a preview does not reserve capacity. |
| Insufficient memory or a memory warning | `./pulsar inventory` and the failed start's memory report | Insufficient memory blocks start. `--accept-memory-warn` acknowledges only a warning; see [memory estimates](MEMORY_ESTIMATES.md) for the optional exact-spec estimate. |
| Existing service or port in use | `./pulsar inventory` and `./pulsar status <spec-id>` | Identify the owner; stopping, replacement, and a deployment-port change are separate choices. |
| `guard_unsupported` | `./pulsar models show <spec-id>` | Use the separately scoped [guarded serving](GUARDED_SERVING.md) procedure. |
| `historical_spec` | `./pulsar models show <spec-id>` | Read [historical compatibility](#historical-specs-and-current-observations); use a current spec for a new launch. |
| Launch or test completion failed | `./pulsar status <spec-id>` and `./pulsar inventory` | Inspect the reported remaining containers and logs before retrying; failure does not always mean nothing is running. |
| API request fails | Reported API URL, served name, authentication, and `./pulsar status <spec-id>` | Follow [the request example](#send-your-first-api-request); distinguish endpoint, authentication, and service failures. |
| Interrupted operation or `cleanup_incomplete` | Retained command output; live status for serving, model info for files, image check for staging | Establish what remains before a conflicting retry. Unconfirmed cleanup is not proof that remote workers stopped. |

Replace placeholders and omit one-node placement flags for multi-node service
checks. A check that cannot observe a node does not establish absence. Catalog
observations are saved information; use the live checks above when state matters.
For JSON codes and exit statuses, see [the contract](CONTRACT.md#error-codes-and-exit-statuses).

### What start reports after a refusal or failure

Start checks the topology first, then runs every independent check (image,
existing service, model files, memory, port) before it reports. It prints one
line per blocker with the affected node and one next step that can be pasted as
written, for example
`BLOCKED node_unreachable: spark-2 (rank 1): the node is unreachable over SSH. Next: ./pulsar topology check`.
Suggested start commands keep the flags you used. An incomplete topology or
fabric, an unreachable node, unavailable Docker or an existing service ends the
checks early, because later checks need every node or would be distorted by
the running service. A check that could not run is reported as such
(`*_check_failed`), never as the condition it checks. With `--pull-image`, the
image is staged only after every other check passes, so a start that is
blocked anyway changes nothing.

With `--replace`, start rechecks the image, removes the previous service
(ownership still proven), then rechecks memory and the port before launching.
If a recheck or the launch then fails, nothing is running for the spec, and
start says so. After launch, start releases its locks while the service loads,
so `stop`, `status` and model-file work are not blocked; the containers'
references protect their files. READY is printed only after a test completion
succeeds. A launch failure prints a `FAILED` line (`container_start_failed`,
`smoke_test_failed`, `health_timeout`, `container_exited` or
`service_stopped`) that says what remains: a failed test completion leaves the
service running, a one-node service keeps its container for its logs, and a
multi-node start that never became healthy removes its containers. It says
they were removed only after confirming it on every node; otherwise it names
the nodes left and suggests `./pulsar stop`. The same blockers appear in `start --json`
error details; see [the public contract](CONTRACT.md). A spec with a serving
guard is refused by ordinary start before any check as `guard_unsupported`.
Use the separately owned [bounded guarded serving](GUARDED_SERVING.md) path
for an explicitly scoped foreground trial. Read-only `start --dry-run` remains
available for its prerequisite planning.

## Menu reference

### Before cluster setup

The home menu reads saved configuration without probing nodes. Cluster setup
requires confirmed membership and, for multiple nodes, enrolled SSH trust.
An unset archive location is optional configuration, not an incomplete cluster
setup. **Archive storage configuration** is available when archive actions are
needed; entering or browsing the menu never saves or disables that setting.

During cluster setup, **Browse the catalog (read-only)** lets you select a
published spec, inspect its summary, details and published results, or compare
it with another catalog spec, then return with **Back** or Esc.
It offers no storage or serving operations. **Host diagnostics**, saved
**Cluster topology (read-only)** and **Help** are also available. Diagnostics
run only when selected. The same catalog browser is available directly as
`./pulsar models menu --read-only`. Command-level prerequisites and confirmations
still apply to all operational actions after setup.

### Catalog operations and confirmations

The interactive menu (`./pulsar`, then **Catalog and storage**) stays open
until **Back** or **Exit**; each operation returns to the same recipe with its
result and any recovery guidance. It lists operations in lifecycle order and keeps storage
maintenance under **Storage and archive**. An operation is left out only when
saved records rule it out, and a **Not shown** line names it and the reason.
**Download** remains available when a home is recorded, and **Restore** remains
available when an archive location is configured. A home record can outlive its
files; these operations' live previews decide whether recovery or reuse is
possible. For a recipe with required snapshots, every snapshot remains
selectable for recovery, with unregistered homes listed first.
**Start** is also left out for a spec with a serving guard, which this Stack
cannot run through ordinary start. `models show` marks it
`Start not supported by this Stack`, and no suggested step leads toward Start.
One **suggested** next step comes from the same saved records and this menu
session; it is a starting point, not a readiness check. `models list` and
`models show` print the same step as a command. A download or restore names
the node the saved records already use; without one it leaves `--node` out and
goes to the default destination (the deployment overlay's placement, or this
node), while the menu asks for a node. The recipe
list labels show the same short saved state. Storage mutations show
the operation's own `--plan` preview before a confirmation that names the
model, nodes and consequence; a blocked plan ends without a question. **Back**
or Esc leaves the current menu or cancels an action prompt to its containing
menu. Cancelling a launch or storage sub-action keeps its submenu open. Archive
configuration also stays open after a saved or declined change until **Back**.
Ctrl-C at a choose, input or confirmation prompt exits the menu with status
`130`, including membership and SSH enrollment confirmations.

### Interruptions and recorded checks

During a catalog operation, Ctrl-C requests interruption from the existing
command; the menu resumes when that command exits. The menu does not infer
confirmed cleanup, absence of a service, or unchanged files from the exit code.
It labels the interruption even when a concurrent command completion returned
zero, retains the command's output, and gives an explicit next inspection:
**Check now** for storage, **Live status** for Start/Stop, or **Check pinned image**
for image staging. These are suggestions, not automatic actions. Existing
backend cleanup and retry behavior continues to own retained staging and service
resources. Other command menus retain their command's interrupt exit status.

A nonzero **Check now** that saved an observation is reported as recorded
findings, rather than an execution failure. Missing or changed files can be a
completed check's result; an interrupted or unrecorded check still needs a retry.

A recent check that found missing or changed files suggests acquisition,
restoration or preparation even when it recorded blockers. Unknown or stale
file observations still suggest **Check now**. A check that records a result
clears the previous operation's pending check; one that could not record a
result does not turn earlier readiness into a new Start suggestion.

### Launch options

**Launch options** provides shortcuts for the selected published catalog spec:

- **Check launch prerequisites** runs the existing `start --dry-run` checks.
  It does not launch or stage images. Its output identifies checks, such as the
  multi-node preflight, that will run only during an actual Start.
- **Check pinned image** runs `image check` on the selected serving nodes.
- **Stage pinned image** previews `image stage` before confirmation. Choose
  between pulling the exact catalog digest from its registry and copying the
  pinned image from this node. Copying never silently falls back to a pull.
  Staging does not start or replace a service or select a different image.

**Start** continues to run its existing checks. If it refuses the start only
because of a memory warning, the menu offers one separately confirmed retry
with `--accept-memory-warn`, for the same spec and node selection. Insufficient
memory and other blockers still prevent start. This acknowledgement is not
remembered for later starts and does not authorize image pulls or replacement.
These menu choices operate on catalog specs; they do not author or override
recipes. Historical schema-1 specs do not offer the new launch shortcuts.

The catalog menu exposes **Download**, **Restore**, **Move home**, **Prepare**,
**Start**, pinning and cleanup through the same command boundaries. It asks
for confirmation before mutations and never chains restoration into preparation
or launch. The menu needs Gum; every action is also a command.

## Verify model files

### Prepared copies and storage estimates

**Prepare** copies or links verified files to the required ranks; it does not
start the server. Prepared files retain metadata stamps so unchanged files
can avoid unnecessary full rehashing. A changed tree requires verification.
When another recipe already has the same manifest on a selected physical node,
preparation can bind the new recipe to that working copy. Its existing path,
files and verification stamp are reused; recipe, rank and topology bindings
remain distinct. Earlier recipes keep their bindings and pins. The home rank
still uses its registered home directly. Only nodes missing suitable content
need a transfer, and the complete required snapshot set must be ready.
An owned incomplete transfer resumes its staging rather than silently discarding
it to create a shared binding.
`prepare --plan` reports which ranks will reuse a binding, bind existing files,
or copy files. A recipe change still requires preparation of its own bindings.

Preparation previews also show the unique snapshot payload and, for each node,
the new copy size, existing files used, prepared-copy root, observed disk free
space, reserve and copy allowance. Named snapshots with the same manifest are
counted once per physical node. The projected headroom subtracts all new copies
from the smaller of disk space after reserve and unused copy allowance. If
snapshot checks observed different budgets, the preview retains those readings
and uses the lowest observed headroom. Unknown readings stay **not observed**;
negative headroom is shown as a deficit. These estimates exclude filesystem
overhead and do not reserve space. Existing preparation checks remain authoritative.
Copy amounts cover full snapshot payloads; already written partial staging is
not deducted from this conservative estimate.

**Storage and archive → Storage budget (all nodes)** runs the existing
`./pulsar model budget` inspection for every confirmed node. It shows the same
reserve and allowance settings used by preparation, requires no confirmation,
and does not change the selected recipe's saved file-check state. This budget
describes prepared-copy storage; home and archive locations may use other filesystems.
The same choice is available in the catalog's top-level menu, even when no
specs are published.

### Verification reuse and full audits

Routine acquisition of registered copies, preparation re-entry, inspection and
serving observation reuse earlier verification while the complete file set,
directory identity and file metadata still match. A new invocation does not
invalidate that proof. Each new working copy is fully hashed before accepting
staging; planning, source checks, the final rename and all-rank readiness can
reuse valid results. Changed metadata triggers full hashing; corruption fails
verification. Reuse preserves the timestamp of the last actual full hash. The
temporary preparation cache is removed on exit. A publication fallback that
changes directory identity also requires another full scan. Metadata reuse
does not detect silent corruption that leaves metadata unchanged; use
`./pulsar model info <spec-id> --full` for a full content audit.

### Cancellation and worker cleanup

Cancelling a verification command stops its owned workers and waits for cleanup.
The node renews its controller lease over the existing SSH connection every two
seconds; a disconnected or silent control channel expires after 30 seconds.
Workers receive two seconds to terminate gracefully before forced cleanup.
These are liveness and cleanup limits, not an audit-duration limit: a quiet,
healthy hash may run as long as needed. Earlier valid verification remains;
cancelled work cannot publish a partial verification as complete.

The public JSON wrapper reports `cancelled` when cleanup of its local command
and tracked node workers is confirmed, and
`cleanup_incomplete` when it cannot establish completion. A disconnected node
also performs its own lease cleanup. An unconfirmed response is not evidence
that every remote worker has already stopped; reconcile it before retrying a
conflicting operation. Cancellation never invokes the model service's stop
command or changes confirmation of cluster membership.

## Output and messages

Stdout carries a command's result and what it is doing: tables, check lines,
steps and final state. Stderr carries `warning: …` and `error: …` lines, each
leading with the affected object, and the phase lines of long storage
operations. With `--json`, stdout carries only JSON. Start reports
refusals as `BLOCKED <code>: … Next: …` lines. `start --verbose`, or
`PULSAR_VERBOSE=1` for any command, adds the internal script name to step,
warning and error lines for debugging; check rows keep their layout. Deprecated aliases (`pulsar gum`, `pulsar wizard`,
`pulsar release list`) print one warning naming the replacement.

Long storage operations report each phase on stderr, such as
`[acquire 3/4] verifying SHA-256 of every downloaded file`. Previews stay quiet
and stdout, including `--json` results, is unchanged.

## Historical specs and current observations

Old catalog specs remain readable. New preparation/start operations require
schema 2 or 3; reauthoring does not relabel old measurements. Existing containers
remain running through code updates and retain ownership-based inventory and
stop support. Status reports inventory only when complete new-contract
observation is unavailable, rather than claiming recipe verification.

Schema-2 recipes with a bare speculative checkpoint ID or path can no longer
freeze or start. Reauthor them with draft schema 2, declare the checkpoint and
its exact model commit in `required_snapshots`, and freeze with every named
manifest to produce a schema-3 spec. Keep earlier specs and measurements intact.
See the [compatibility boundary and reauthoring steps](CONTRACT.md#required-snapshots-and-speculative-decoding).

See [live inspection](#inspect-a-running-service) for current observations and
[verification behavior](#verify-model-files) for file audits.

## Recipes requiring a draft checkpoint

The target and draft are snapshots used by one serving recipe. Acquire each
exact commit through the existing acquisition command and retain separate source
manifests. After freeze, schema-3 home operations select a named snapshot:

```sh
./pulsar model acquire <spec-id> --snapshot draft --node <node> --yes
./pulsar model archive create <spec-id> --snapshot target --yes
./pulsar model archive create <spec-id> --snapshot draft --yes
./pulsar model archive verify <spec-id>
./pulsar model restore <spec-id> --snapshot draft --node <node> --yes
```

For private specs, add `--spec-file <file>` to the same commands. `move` and
`remove` also require `--snapshot` for schema 3. The model-storage menu provides
the same snapshot choice. No command silently selects only the target for these
operations. Archive creation, acquisition, and restoration remain individual
operations; sequence them as required. Verification without `--snapshot` covers
all declared archives, is read-only, and fails if any member does not verify.

`prepare`, `info`, `check`, `pin`, `unpin`, and `purge` cover the complete recipe.
Preparation checks combined storage capacity before mutation. Each snapshot's
home must belong to confirmed membership; different snapshots may have different
homes. Every selected rank receives every required snapshot. A failed transfer
retains its owned staging record; retry can reuse that staging and completed
copies. Inconsistent or replaced staging requires explicit inspection.

Pinning remains explicit and covers the recipe's known prepared copies. Neither
freeze nor launch silently changes retention policy. Purge preserves homes and
archives and respects references to either target or draft, including stopped
containers. `check` records per-snapshot preparation and archive observations;
aggregate readiness requires every member. These observations are separate from
catalog membership and from a running service.

## Maintainer experiments and migration

The private workbench may pass an explicit `--spec-file` candidate to the
same storage and serving boundaries without adding a catalog entry. The
maintainer approves each variant before launch. Candidates that fail or leave
baseline criteria incomplete remain available to the maintainer. Publication
adds the maintainer-selected schema-valid spec and any packaged compact evidence
through a reviewed PR. State, review, and evidence do not authorize or block
catalog membership or serving.

Existing bytes may be brought forward with the explicit verified-reuse
migration tool, `scripts/migrate-model-storage.sh --help`. Preview first.
Previous records locate candidates; complete manifests and actual bytes must
agree before new records are created. Migration never automatically stops
services, downloads replacements or deletes unmatched files. Previous metadata
remains available until acceptance; normal operation uses only the new records.
