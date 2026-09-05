# Operate the inference stack

An operator selects a **spec**: an exact model snapshot, serving recipe,
container image and hardware geometry frozen together. Its complete spec ID
is the argument to serving and model-storage commands. A recipe belongs in
the catalog only after qualification and repository review.

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

Withdrawn recipes remain visible with their reason. They are not recommended,
but their exact configuration can still run when operational checks pass.
Withdrawal never stops services or removes model files or archives.

## Configure topology and storage

Confirmed topology determines membership and physical node identity. Start
with `scripts/detect-fabric.sh --help`, inspect discovery, then use its explicit
`--write-topology` flow to confirm membership. Use `./pulsar topology --help` for
lower-level topology inspection and validation, and `./pulsar ssh-trust --help`
for explicit SSH trust enrollment. Host and fabric diagnostics are available
through `./pulsar doctor`. Catalog browsing works before topology is configured.

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

The same workflow is available through the main menu's **Archive storage
configuration** entry. `PULSAR_COLD_ROOT` is the explicit configuration
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
Control SSH, inference traffic and model transfer use distinct configured
paths. Multi-node preparation preserves the selected transfer contract and
does not silently change networks.

Start immediately rechecks the actual model files, image, recipe, geometry,
placement, capacity and ownership. Recipe or image changes require another
spec. Deployment settings such as API port, served name and placement remain
separately configurable through the deployment overlay.

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
archive health. Use **Verify archive** to perform a fresh integrity check.

## Stop and reclaim storage

```sh
./pulsar stop <spec-id> --node <confirmed-node-id>
./pulsar model pin <spec-id> --yes
./pulsar model unpin <spec-id> --yes
./pulsar model purge <spec-id> --yes
./pulsar model remove <spec-id> --yes
```

Stop retains files, pins and evidence. Purge refuses active or pinned prepared
copies and preserves homes and archives. Before removing a catalog model's
home, clear its dependent prepared copies and verify its recovery archive.
An unpromoted model may be explicitly discarded without an archive after
dependencies are cleared; private recipes and experiment results remain.
All protections apply to the shared snapshot, not only the selected recipe.

There is no archive-deletion command. Catalog edits and ordinary cleanup never
delete archives; permanent removal belongs to deliberate storage administration.

## Maintainer experiments and migration

The private workbench may pass an explicit `--spec-file` candidate to the
same storage and serving boundaries without adding a catalog entry. The
maintainer approves each variant before launch. Candidates that fail or leave
baseline criteria incomplete stay private. Publication supplies the qualifying
spec, six compact measurements, run record and summary through a reviewed PR.

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
