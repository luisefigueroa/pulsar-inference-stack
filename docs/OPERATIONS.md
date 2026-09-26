# Operate the inference stack

An operator selects a **spec**: an exact serving target and its required snapshots,
serving recipe, container image and hardware geometry frozen together. Its complete spec ID
is the argument to serving and model-storage commands. A schema-valid spec in
`releases/` is a catalog member; nullable state and review metadata do not gate
membership or serving.

## Browse and check

```sh
./pulsar models
./pulsar models list --json
./pulsar models show <spec-id>
./pulsar models check <spec-id> --node <confirmed-node-id>
```

The catalog includes every released recipe even when no local files are
present. It separates the spec's review status, local file preparation and
archive observations. Details include exact identity, known home location,
per-rank prepared copies, pins and blockers. A **rank** is this job's slot in
the serving group; rank 0 provides the API and need not hold the home.

Normal browsing reads saved observations and shows their age. Unobserved
state is unknown. A saved successful check is not a promise that the next
launch will work. **Check now** checks the selected managed locations; it
does not search arbitrary cache trees, download files or start a server.
**Files prepared** does not mean a service is running. Live service checks
are separate:

```sh
./pulsar status <spec-id> --node <confirmed-node-id> --json
./pulsar inventory
```

Commands typed by a person accept a unique catalog spec ID prefix of at least
12 characters, as `models list` shows it, and print the complete ID they
selected. An ambiguous or unknown prefix fails and names the matching IDs.
`--json`, `--spec-file`, `model purge` and `model remove` require the complete
64-character ID, so scripts and the workbench never depend on a prefix. Human
output names machines by their saved hostname; JSON keeps the stable `node_id`.

Withdrawn recipes remain visible with their reason. They are not recommended,
but their exact configuration can still run when operational checks pass.
Withdrawal never stops services or removes model files or archives. A spec
removed from the catalog is listed in `catalog-removals.json`; manage any
remaining local storage for it with `--spec-file` and its retained document.

## Configure topology and storage

Confirmed topology determines membership and physical node identity. On a
terminal, `./pulsar` offers only the next bind step (confirm membership, enroll
SSH trust, or select an archive directory) until this checkout is bound to the
cluster. That menu uses saved local files; it does not probe nodes. After the
checkout is bound, open `./pulsar` and choose **Cluster topology**. The same
actions are available directly:

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
to single-node clusters too. The menu exposes the same **First-use setup** action.
Initial key-based SSH login must already work; this flow verifies and records
host identities, rather than installing remote login keys. No step starts or
stops a model. For noninteractive agents, use configuration and SSH enrollment
as separate explicitly approved commands; guided setup itself requires a terminal.

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
this menu; opening either menu performs no probes. Gum and plain terminal modes
use the same commands. Low-level manifest utilities remain available under
`pulsar topology` with their existing arguments. From the private workbench,
invoke the configured public Stack executable by absolute path; Workbench creates no
second topology implementation.

Topology and SSH files stay private in the selected stack checkout. Host and
fabric diagnostics are also available through `./pulsar doctor`. Catalog
browsing works before topology is configured.

A **home** is the complete local copy of one exact snapshot. Other serving
nodes use prepared working copies. The library records their explicit
locations; a snapshot manifest supplies the expected file list and hashes.
Files on every participating node must match before serving starts.

Select an existing recovery directory with:

```sh
./pulsar configure archive-root
./pulsar configure archive-root show
./pulsar configure archive-root set <existing-directory> --yes
```

The same workflow is available through the menu's archive step (during setup)
or **Archive storage configuration** after the checkout is bound. `PULSAR_COLD_ROOT` is the explicit configuration
variable: process value first, then the repository's `.env`; empty disables
archives. Pulsar does not create, mount or administer the selected directory.
The operator owns its access controls and choice of independent storage.
Existing non-Pulsar content stays untouched. Archive storage is never mounted
into serving containers. Disabling this configuration does not delete archives.

## Obtain, prepare and start

Each command below is an independent action. Select the confirmed physical
node for single-node recipes; multi-node geometry comes from the spec and
confirmed topology, never from inventing a recipe to use all discovered nodes.

```sh
./pulsar model acquire <spec-id> --node <confirmed-node-id> --yes
./pulsar model prepare <spec-id> --node <confirmed-node-id> --yes
./pulsar start <spec-id> --node <confirmed-node-id>
```

Acquisition resolves the exact source commit, checks the complete upstream
Git/LFS inventory, downloads into private staging and verifies content before
publishing a home without replacing another directory. Matching verified
bytes can be reused. Missing files or a different revision do not trigger
an alternate download path.

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

Control SSH, inference traffic and model transfer use distinct configured
paths. Multi-node preparation preserves the selected transfer contract and
does not silently change networks.

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

Start does not replace an existing exact-name service and does not pull a
missing image by implication. After inspecting the current service, pass
`--replace` only with explicit replacement approval. Pass `--pull-image` only
when staging the selected digest-pinned image is also approved. Generic `--yes`
does not grant either permission.

The catalog menu exposes **Download**, **Restore**, **Move home**, **Prepare**,
**Start**, pinning and cleanup through the same command boundaries. It asks
for confirmation before mutations and never chains restoration into preparation
or launch. Gum and plain-terminal menus use the same actions.

## Move, archive and restore

```sh
./pulsar model move <spec-id> --node <destination-node-id> --yes
./pulsar model archive create <spec-id> --yes
./pulsar model archive verify <spec-id>
./pulsar model restore <spec-id> --node <confirmed-node-id> --yes
```

Recipes using identical model bytes share one snapshot archive. Movement
verifies existing bytes or a transfer before changing the home record.
Archives and restoration run in the foreground; failures remain explicit.
No background archive-job or receipt-recovery service is required.

After controller loss, use a fresh public stack checkout, configure the
archive location and confirmed topology, select the catalog spec and restore.
The restored bytes must match the spec manifest before local records are
rebuilt. Prepare and start are subsequent explicit operations. Restoration
requires neither the private workbench nor access to Hugging Face.

The catalog's last archive-verification time is saved information, not current
archive health. Use **Verify archive** to perform a fresh, read-only integrity
check. Use **Check now** with full verification when the resulting observation
should also be recorded in local catalog state.

## Stop and reclaim storage

```sh
./pulsar stop <spec-id> --node <confirmed-node-id>
./pulsar model pin <spec-id> --yes
./pulsar model unpin <spec-id> --yes
./pulsar model purge <spec-id> --yes
./pulsar model remove <spec-id> --yes
```

Stop retains files, pins and evidence. Purge refuses active or pinned prepared
copies and preserves homes and archives. It removes only the selected recipe's
bindings; working-copy files remain while any other recipe has a binding, even
an unpinned one. The final binding can delete the working copy only after the
usual ownership, container and incomplete-operation checks. Container references
to a shared path, including stopped containers, still block purge. Before removing a catalog model's
home, clear its dependent prepared copies and verify its recovery archive.
An unpromoted model may be explicitly discarded without an archive after
dependencies are cleared; private recipes and experiment results remain.
All protections apply to the shared snapshot, not only the selected recipe.

Sharing upgrades the affected private working-copy records to schema 3 during
the approved preparation. Their keys, paths and pins are preserved, and retries
reconcile partial controller/node upgrades. Older Stack code rejects these
records. Finish older model-library operations before the first shared prepare
and use the updated Stack for subsequent storage operations; downgrading its
code does not downgrade shared ownership. A preview never converts records.

There is no archive-deletion command. Catalog edits and ordinary cleanup never
delete archives; permanent removal belongs to deliberate storage administration.

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

## Installation prerequisites

Use Python 3.11 or newer, Bash, Git and util-linux `flock`. Serving nodes need
Docker with NVIDIA GPU access. Inter-node operations use OpenSSH, rsync and
iproute2 with confirmed control and RoCE endpoints. Acquisition requires a
modern `hf` CLI on the selected node; its own local authentication is used.
The optional Gum executable is included for the terminal menus; `GUM=0` uses
plain prompts. Diagnostic output reports missing prerequisites instead of
silently choosing other tools or transport.

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
`pulsar resources --service-id ID --jsonl` samples private node/container metrics.
To begin before launch, use `--spec-file FILE` and, for a one-node recipe,
optional `--node NODE`; it never starts the model. Container metrics remain
unavailable until the matching owned recipe appears. Stop the stream to end
sampling; model services are unaffected.


## Recipes requiring a draft checkpoint

The target and draft are snapshots used by one serving recipe. Acquire each
exact commit through the existing acquisition command and retain separate source
manifests. After freeze, schema-3 home operations select a named snapshot:

```sh
./pulsar model acquire <spec-id> --snapshot draft --node <confirmed-node> --yes
./pulsar model archive create <spec-id> --snapshot target --yes
./pulsar model archive create <spec-id> --snapshot draft --yes
./pulsar model archive verify <spec-id>
./pulsar model restore <spec-id> --snapshot draft --node <confirmed-node> --yes
```

For private specs, add `--spec-file <file>` to the same commands. `move` and
`remove` also require `--snapshot` for schema 3. The model-storage menu provides
the same snapshot choice. No command silently selects only the target for these
operations. Archive creation, acquisition, and restoration remain individual
operations; sequence them as required. Verification without `--snapshot` covers
all declared archives, is read-only, and fails if any member does not verify.

`prepare`, `info`, `check`, `pin`, `unpin`, and `purge` cover the complete recipe.
Preparation checks combined storage capacity before mutation. Each snapshot's
home must be on a selected serving node; different snapshots may have different
homes. Every selected rank receives every required snapshot. A failed transfer
retains its owned staging record; retry can reuse that staging and completed
copies. Inconsistent or replaced staging requires explicit inspection.

Pinning remains explicit and covers the recipe's known prepared copies. Neither
freeze nor launch silently changes retention policy. Purge preserves homes and
archives and respects references to either target or draft, including stopped
containers. `check` records per-snapshot preparation and archive observations;
aggregate readiness requires every member. These observations are separate from
catalog membership and from a running service.
