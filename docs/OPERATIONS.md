# Operate the inference stack

Start with the [README walkthrough](../README.md#serve-a-catalog-recipe) for the
short path from a fresh checkout to a service. This guide explains each task,
its prerequisites, and what to inspect when it cannot complete. Run commands
from the Stack checkout; replace angle-bracket placeholders before using them.

## Contents

- [Installation prerequisites](#installation-prerequisites)
- [Terms](#terms)
- [Catalog labels](#catalog-labels)
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

`./pulsar doctor` checks these prerequisites on this machine and on every
confirmed node, each in its node's section: Python 3.11 or newer, `flock`,
`rsync`, and the `hf` CLI. A missing Python 3.11, `flock` or `rsync` is a
blocking issue. It finds `hf` as Download does: `hf` on the login user's
`PATH`, else `~/.hf-cli/venv/bin/hf`. It then asks the Hub, once and with a
short timeout, whether that node's own `hf` login is accepted, using the
Python of that `hf` installation; nothing is forwarded between nodes. `hf`
matters only on a node that receives a download, so every `hf` finding is a
warning that says what it affects, never a blocking issue. Without a usable
`hf` (missing, only the older `huggingface-cli`, unable to load its library,
or lacking what Download uses), models cannot be downloaded to that node; it
can still serve and hold prepared copies, and the row names the fix. Without
a login, public models still download; `hf auth login` on that node adds one.
A login the Hub rejected may make downloads to that node fail until you log in
there again. A Hub that cannot be reached is reported as not checked, never as
failed. The rows never print the token or the account name. These facts are
only reported: they never decide whether a machine can be a member, and
discovery, `topology check` and start never contact the Hub. Pulsar installs
nothing.

### Interactive and noninteractive commands

Menus and confirmation prompts draw with Gum: the repository bundles it for the
arm64 Linux nodes (`third_party/gum/`); elsewhere install `gum` from the
package manager or point `GUM_BIN` at it. `NO_COLOR` or
`PULSAR_COLOR=never` draws them without color; see
[output and messages](#output-and-messages) for text styles. A menu lists every option on one
page when the terminal has room for it, and its labels use the whole row after
the cursor; a repeated label gets a numbered suffix such as `(2)`. The
catalog's spec list gives each spec several lines and one cursor stop; when the
specs do not fit, it pages so the menu title stays on screen, into pages of
even length that each start with a model's name where the models allow it. A
key line under the menu title reads `↑/↓ move · enter choose · esc back`; on
the home menu Esc exits. Without Gum
or an interactive terminal, a menu opens nothing: it lists the equivalent
commands, one `./pulsar …` command per line with a `#` comment saying what it
does, and exits with status 2. A command that would ask for confirmation names
its `--yes` form. `pulsar models` without a subcommand shows the catalog list instead, with
a warning when the terminal lacks Gum. Diagnostic output reports missing prerequisites instead of silently
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
must not overlap directories managed as removable homes or prepared copies.

## Terms

Commands and guides use these terms. Public flags, environment variables,
JSON fields, and container labels retain their existing names. The menu's
**Help and troubleshooting… → What the words mean** and `./pulsar help terms` show this table.

| Term | Meaning |
| --- | --- |
| spec | The serving specification you select: a catalog spec published under `releases/`, or a candidate passed with `--spec-file`. Shown as its spec ID or a 12-character prefix. |
| recipe | The spec's execution configuration: image, engine arguments, container and geometry. An override produces a modified recipe. |
| model files | The model's bytes, verified against the spec's snapshot manifest. |
| snapshot | An exact set of model files from one model commit. Its manifest lists the files, sizes, and hashes used to verify that set. |
| home | The one verified source copy of a snapshot, on a confirmed member. |
| prepared copies | The per-rank copies or bindings that serving reads. |
| pin | Protection of prepared copies from purge, until they are unpinned. |
| serving image | The recipe's container image, identified by its exact digest. Image command output calls it the pinned image. |
| service | The model server Pulsar started for a spec: one container per rank, answering requests at its API URL. |
| archive, archive location | The verified recovery copy, and the directory that holds archives. |
| node | A confirmed machine, named by its hostname. |
| rank | A serving slot in the spec's geometry, always shown with its node, such as `spark-2 (rank 1)`. |
| controller | The machine that runs `./pulsar` from this checkout and keeps its saved records. It may also be a node. |
| service inventory | What `./pulsar inventory` observed on every node: Pulsar services, their containers and memory, at one time. The home menu's Serving line shows the last saved one with its age; Running services… and Check the cluster now save a new one. |
| storage budget | What `./pulsar model budget` observed where each node keeps prepared copies: disk free, the reserve, Pulsar files and what it can still copy. The home menu's Disk line shows the last saved one with its age; Models and disk space… and Check the cluster now save a new one. |
| membership | The nodes you confirmed for this checkout and saved. A saved node may not be reachable now; `./pulsar topology check` tests each one. |
| changes noticed | What the last discovery saw against saved membership: machines found that are not members, members that did not answer, and changed addresses or links. Every discovery saves it with its time, and the last Check the cluster now keeps its findings beside it; it changes nothing by itself. Cluster… → Review what changed… shows it and saves a change after one question. |
| SSH trust | The SSH host identities Pulsar records for each member after you confirm them. Pulsar does not install login keys; key-based SSH login must already work. |
| fabric | The RoCE (RDMA) links between nodes, which multi-node recipes need. `./pulsar topology check` tests every pair of nodes. |

## Catalog labels

The catalog list, a spec's frame and `./pulsar models list` describe each spec
with these words; the **Model files and disk space** page and `./pulsar model
files` describe each model's files with the *Model files* rows. The home
screen's status uses the *Cluster*, *Serving*, *Disk* and *Attention* rows, and
the **Cluster…** frame the *Checked*, *Changes* and *Issues* rows. Each label is
written as `Field: words`; the field is where the words appear. The menu's **Help and troubleshooting… → What the labels mean**, the catalog list's
**What the labels mean** and `./pulsar help labels` show this table. JSON output
keeps its own values (`local_state`, `archive_state`, `state`); only the words
below are for reading.

| Label | Meaning |
| --- | --- |
| Files: not checked yet | Pulsar has no saved check of this spec's model files. It does not mean they are missing. Choose **Check now** to look; it only reads the nodes. |
| Files: prepared on every serving node (list: files prepared) | The last saved check found the model files on every node the spec serves from. The age in brackets says when; **Check now** refreshes it. |
| Files: not prepared on every serving node (list: files not prepared) | The last saved check found a serving node without its copy of the files. **Prepare** copies them from the *home*; **Download** comes first when there is no home. |
| Files: the same files are prepared for … on … | Each named node holds other specs' prepared copies of every snapshot this spec requires, and no saved check has found this spec prepared. A node holding only some of them is not named, because **Prepare** would still copy the rest there. Where this spec serves from the named nodes, **Prepare** binds it to those files and copies nothing there; serving nodes without them still receive a copy. A catalog spec is named first where one holds them, and `and … more` counts the rest; a spec this checkout does not publish, for example one an experiment started with `--spec-file`, is only counted, as `outside the catalog`. |
| Files: changed since they were verified (list: files changed) | The last saved check found files that differ from what was verified. **Prepare** replaces them with verified copies. |
| Files: unknown (list: files unknown) | The last check could not see the files, for example because a node was unreachable. Fix the access, then choose **Check now**. |
| Files: checked … ago | How old the saved check is. Files can change after a check; **Check now** replaces it. |
| Files: prepared in this session | **Prepare** finished in this menu session. Prepare saves no check, so the older saved check is shown after it. |
| Archive: not checked yet | No saved check has looked for an *archive* of this spec. Choose **Check now**. Archives are optional. |
| Archive: verified … ago | An archive copy was checked against the files' hashes at that time. **Restore** can rebuild the home from it. |
| Archive: present at last check, not verified | The last check found an archive but did not compare its hashes. **Verify archive** compares them. |
| Archive: not found at last check | The archive location has no copy of this spec's files. **Create archive** makes one if you want a recovery copy. |
| Archive: unavailable at last check | The archive location could not be read, or the copy there was incomplete. Check that the location is reachable. |
| Archive: archive location not configured at last check | No archive location was set. Archives are optional; **Cluster… → Settings… → Archive location…** sets one. |
| Archive: unknown | The last check could not establish anything about the archive. |
| List: not checked yet | The short form of **Files: not checked yet** in the catalog list. |
| List: start unsupported | This Stack cannot start the spec, for example because it needs a serving guard or is a historical schema-1 spec. The spec's **Not shown** line says why. |
| List: candidate, measured or released | The spec's **State**, after the saved state. See the State rows. |
| Running?: not checked here | This menu has not looked for a running *service* of the spec. **Live status** looks. |
| Running?: running, partly running or degraded | A service was seen with all, some, or unhealthy containers. **Live status** shows each node; **Service logs** shows output. |
| Running?: exited containers or stopped containers | The service's containers exist but are not running. **Stop** removes them; model files are kept. |
| Running?: service exists, service seen … ago | A service of this spec was seen by **Live service inventory**; the age says when. |
| Running?: last seen … ago | A service observation older than ten minutes. It may have changed since; **Live status** looks again. |
| Lifecycle: ✓ | That step is done according to saved records: Download → Prepare → Start. The bracketed step is the next one. |
| Lifecycle: files not checked | The files have a home, but no check says whether every serving node is prepared. Choose **Check now**. |
| Lifecycle: not offered here | The files are ready, but this Stack cannot start the spec. See **List: start unsupported**. |
| Lifecycle: running | The service was seen running in this menu session. |
| Cluster: … nodes, all answered … ago | The saved membership's node count, and how many nodes answered the last **Check the cluster now**, or the last discovery when it is newer, with its age. Opening the menu checks nothing; **Cluster…** → **Check the cluster now** checks again. |
| Cluster: … of … answered | Some nodes did not answer the last check or discovery. Each one is named under **Attention**; **Cluster…** → **Review what changed…** keeps it or saves the cluster without it. |
| Cluster: not observed | The last check could not tell whether a node answered, for example because the membership check itself did not run. It does not mean the node is down; **Check the cluster now** looks again. |
| Cluster: not checked yet | No check or discovery has been saved for this membership. It does not mean the nodes are down. **Cluster…** → **Check the cluster now** looks. |
| Cluster: not checked since membership was saved | The saved check describes the membership before its last save, so it is not shown as current. **Check the cluster now** checks the membership as it is. |
| Cluster: the last discovery did not finish | Discovery stopped before it could compare the machines with saved membership, for the reason given. Nothing was saved from it; **Check the cluster now** tries again. |
| Cluster: saved check unreadable | The saved changes record could not be read. The next **Check the cluster now** replaces it. |
| Checked: links verified, links failed | Whether the last check found working RoCE links between every pair of nodes. Failed links name their fix in the check's report. |
| Checked: SSH identities verified, SSH identities did not match | Whether each node presented the SSH identity Pulsar recorded for it at the last check. A mismatch means a reinstalled machine or a changed host key; the report says how to verify and record it again. |
| Checked: SSH identities recorded, SSH identities not recorded | Pulsar has recorded the nodes' SSH identities (*SSH trust*), but no check has verified them yet; or it has not recorded them, and **Set up this cluster…** does. |
| Changes: none noticed | The last discovery found the machines exactly as saved, with its age. |
| Changes: did not answer | A node in saved membership did not answer the last discovery. Pulsar cannot tell whether it is down or gone; **Review what changed…** keeps it or saves the cluster without it. |
| Changes: kept as only down | You chose to keep a node that did not answer. It stays under **Attention** until it answers, and Pulsar asks again only when a later check sees different changes. |
| Changes: found …, not a member | Discovery found a machine that qualifies and is not in saved membership. **Review what changed…** can add it. A machine with the hostname of a node that did not answer may be that node reinstalled: Pulsar knows a node by its OS machine ID, so it joins as a new node. |
| Changes: answered but cannot be a member | A node in saved membership answered but no longer qualifies, for the reasons given. Fix it on that machine; Pulsar never leaves out a node that answered. |
| Changes: address, links or position changed | A node's control address, its RoCE links or its position in the saved order differs from saved membership. **Review what changed…** shows old against new before it saves. |
| Changes: SSH on this machine does not trust it yet | A machine answered, but OpenSSH on this machine does not trust its host key, so Pulsar could not check it. Verify the key with `ssh -o StrictHostKeyChecking=ask HOST`; Pulsar never accepts a host key for you. |
| Issues: … | A blocking issue the last check found on a node, with its fix. Warnings, such as a missing `hf` CLI, are in the check's report, not here. |
| Serving: not observed yet | No *service inventory* is saved on this controller yet, so home cannot say what runs. It does not mean nothing runs. **Running services…** looks and saves what it sees. |
| Serving: running on … · port … | Each Pulsar *service* in the last saved service inventory, in the words of the **Running?** rows, with the nodes it runs on and its port. The saved inventory holds no API URL; **Running services…** has the details. |
| Serving: outside the catalog | A Pulsar-managed service whose spec this checkout does not publish, for example one an experiment started or one started with `--spec-file`. **Running services…** shows it and offers Stop. |
| Serving: seen … ago | When the saved service inventory was taken. Past ten minutes the service may have started, stopped or changed since; **Running services…** looks again. |
| Serving: exited containers of … remain | No container of that service is running, but its exited or stopped containers are still on the node. **Stop** removes them; model files are kept. |
| Serving: none running | Every node answered the last saved service inventory, and none ran a Pulsar service. Exited or stopped containers that remain are named after it. |
| Serving: unknown; … not observed | A node did not answer the last saved service inventory, so its services are unknown, not absent. |
| Serving: saved inventory unreadable | The saved inventory could not be read, so it is left out. **Running services…** replaces it. |
| Serve: running on … | The service inventory **Serve a model…** took on entry saw this recipe running on those nodes. Choosing it says there is nothing to do. |
| Serve: ready to start | The last saved check found its files prepared on every serving node. Its plan is Start alone, after whatever is in the way is stopped. |
| Serve: needs copying to … · … | Its files are on the cluster, but not on every serving node yet: the plan copies them to that many nodes, about that much in all. A copy already there that still verifies is taken over, not copied again. |
| Serve: needs its files checked | Its files are where it serves, but no saved check has found them prepared. The plan checks and records them; nothing is copied. |
| Serve: needs restoring from its archive | No home (the copy Pulsar keeps) is recorded, and its archive is there: the plan restores the files from it, without the internet. |
| Serve: needs downloading · … | No home is recorded and no archive can restore it: the plan downloads that much from Hugging Face. **Change…** on the plan can pick the node it goes to. |
| Serve: needs …; the cluster has … | The recipe serves on more nodes than the cluster has, so it cannot be served here. |
| Serve: size unknown | The recipe's spec file could not be read, so how much would be copied or downloaded is not known. |
| Services: by the service inventory … ago | Where **Running services…** got the service's model, state, nodes, port and served model name, and how old that is. The page takes one inventory on entry and reuses it while it is under a minute old; a Stop or a Start makes it look again. |
| Services: not checked here; choose Check it now | The service inventory holds no health, and for this service no API port or launch record either. **Check it now** asks the service (`./pulsar status`) and fills them in with its age; nothing is guessed before it. |
| Services: from its launch record | Whether the service needs a key, read from the launch plan its containers name, as the Connect card reads it. **Check it now** confirms it with the service. |
| Services: only from … | The API address is a loopback address (`127.0.0.1`), because the service's first node is this machine. It works from that node only; from another machine use an address that reaches it. |
| Services: not in the service inventory; choose Check it now | The inventory did not record the name requests use for this service. **Check it now** asks the service. |
| Services: the model name requests use | The served model name: what a client puts in the request's `model` field. **How to connect** shows a first request with it. |
| Services: none: the API accepts requests without a key | The service was started without an API key, according to its launch record. |
| Services: required: requests send the key the service was started with | The service was started with an API key, according to its launch record. The key itself is never shown; **Cluster… → Settings…** says where it is set. |
| Services: not recorded: add the key header if the service was started with one | The service has no launch record that says whether it needs a key, for example one started before launch records existed. |
| Services: answered /health, did not answer /health | What the service's API said to one health request at **Check it now**, with its age. A service that did not answer may still be loading; **Logs** shows its output. |
| Services: running, with no API address to ask | Check it now found the service running but no API address to send the health request to. |
| Services: Check it now found no service, Check it now could not tell | The check found no container of the service on any node, or a node did not answer, so whether it runs is unknown, not absent. |
| Services: did not answer the service inventory | A node did not answer the inventory, so what runs there is unknown, not absent. Stop and Restart need every node to answer. |
| Services: No Pulsar service is running, No Pulsar service was seen | Every node answered and none runs a Pulsar service; or none was seen on the nodes that answered, and the others are named as unknown. |
| Services: processes Pulsar did not start, read-only | GPU work and containers on the nodes that Pulsar did not start. The page names them and offers nothing for them. |
| Services: Started outside this catalog, for example by an experiment | A Pulsar-managed service whose spec this checkout does not publish. The page can show its logs and stop it; it has no recipe to check, restart or switch from. |
| Services: not offered | **Restart…** or **Stop…** is left out for the reason given, for example a service started from a modified recipe, or on other nodes than Start would use. |
| Services: Pulsar cannot see everything the service runs | **Stop…** is not offered while the named node does not answer the service inventory: Stop removes the service on every node, and Pulsar cannot see what runs on that one. Choose **Back**, then the page again once it answers. |
| Why: not run: an earlier blocker ended the checks | **Why will a model not start?** shows each of Start's checks; Start stops at the first blocking group, so later checks did not run. It does not mean they passed. After the fix, **Check again** runs them. |
| Why: Nothing blocks it | Start's checks all passed without starting. **Serve a model…** starts it, after its plan. |
| Why: Start's checks could not run | The dry run gave no answer Pulsar could read, for the reason given. **Run diagnostics** checks Docker, the GPUs and every node's prerequisites. |
| Models: free, room for … of copies | On **Models and disk space…**, a node's free disk space from the model files scan, then what it can still copy from the storage budget: the smaller of disk free after the reserve and the copy budget left. |
| Models: past its reserve | The node's disk is below its storage budget's reserve by that amount. **Free up space…** offers to move a home off it when another node has room. |
| Models: not checked, so its files and room are unknown | The node did not answer the scan, so what is on it is unknown, not absent. Removing, moving or keeping files needs every node, so **Free up space…** offers nothing until it answers; choose **Look again**. |
| Models: home on, (also its copy there) | Where the model's *home* is: the copy Pulsar downloaded or restored and keeps, from which serving nodes are given copies. *also its copy there* means that node serves straight from the home. |
| Models: copies on, no copies on serving nodes | The serving nodes that hold a *prepared copy* of the model, the files a service reads there; or none yet. **Get a model ready ahead of time…** makes them. |
| Models: kept, not kept | Whether the model's copies are kept ready (pinned): **Free up space…** never offers a kept model and Purge refuses it. **Keep a model ready…** keeps it or stops keeping it. |
| Models: archived, verified … ago | Every required set of the model's files has an archive, with the age of its last verification. A removed model comes back from it without the internet. |
| Models: partly archived, archive seen, not verified, no archive recorded | Only some of its files have a verified archive; an archive was seen at the last check but not verified; or Pulsar has no record of an archive. **Back up a model…** makes the missing ones; **Check the archives…** verifies them. |
| Models: in use by a container | A container mounts the model's files, even a stopped one. **Free up space…** does not offer them; **Running services…** stops a Pulsar service, and Pulsar never removes a container it did not start. |
| Models: no recipe in this catalog uses these files | Files on the nodes that no catalog recipe needs. Pulsar has no command that removes them, so the page names them read-only. |
| Models: size unknown | The size could not be read, for example of a copy that never finished. It is not zero. |
| Disk: room for … on … | What each node can still copy in the last saved storage budget, the smaller of disk free after the reserve and the copy budget left, with the budget's age. Nodes with the same room share a part. **Models and disk space…** shows the whole table. |
| Disk: past its reserve or copy budget by, headroom not observed | The node is past its reserve or its copy budget by that amount, or the budget could not tell what it can still copy. |
| Disk: not observed yet, saved budget unreadable | No storage budget is saved yet, or it could not be read. **Models and disk space…** reads one. |
| Attention: the archive location setting could not be read | The archive location setting in `.env` or the environment is not a readable literal path. **Cluster…** → **Settings…** says where it is set. |
| Attention: and … more | More items need attention than home shows; **Cluster…** shows them all. |
| State: candidate | A label the maintainer sets: the spec is published with no claim yet about its quality or readiness. It is not a test result and does not affect serving. |
| State: measured or released | A label the maintainer set by hand after review. It is not a test result itself; **Published results** shows the measurements. |
| Review: experimental, stable, validated or failed | An advisory review the maintainer recorded, with its date. Launch checks still apply. |
| Review: withdrawn | The maintainer advises against this spec and gives a reason. Serving remains possible when the checks pass. |
| Model files: home | The node has this model's *home*, the downloaded files that prepared copies are made from. Pulsar compared file names and sizes, not contents. |
| Model files: copy (detail: prepared copy) | The node has a *prepared copy*: the files a *service* reads there. |
| Model files: home missing or copy missing (detail: home not found, prepared copy not found) | A saved record says the files should be on this node, but the folder is gone. **Prepare** recreates a missing copy; a missing home needs **Download** or **Restore**. |
| Model files: home unknown or copy unknown | The check found the folder but could not confirm its files, or could not read the node. Choose **Refresh** to look again. |
| Model files: shared | More than one spec uses these files, so they are stored once. Clearing one spec's copies keeps them for the others. |
| Model files: pinned | Kept from Purge until unpinned. **Unpin prepared copies** removes the protection. |
| Model files: in use (detail: in use by a container) | A container mounts the files, even a stopped one. **Stop** removes the containers and keeps the files. |
| Model files: pins unknown | The node's pin records could not be read, so it is not known whether the files are pinned or shared. Choose **Refresh** when the node is reachable. |
| Model files: use unknown (detail: container use unknown) | The containers on the node could not be listed, so it is not known whether the files are in use. |
| Model files: shared (saved record), pinned (saved record) | Pulsar's saved records say so, but the node could not confirm it now. Every cleanup rechecks before it removes anything. |
| Model files: — (detail: none in Pulsar's folders on this node) | No copy of this model is in the homes or copies folder on that node. It does not mean the files are absent from the machine. |
| Model files: ? | The node, or part of its folders, could not be checked, so it is not known whether the model is there. |
| Model files: not in Pulsar's records | The files are in a Pulsar folder, but no saved record owns them. The menu offers no cleanup for them. |
| Model files: checked | Pulsar read the folder and compared the names and sizes of what it found. |
| Model files: none on this node | The folder does not exist on that node, so nothing is stored there yet. |
| Model files: partly checked | Pulsar read the folder, but some items in it were not recognized; see the **Not recognized** lines. |
| Model files: could not be checked | Pulsar could not read the folder; the reason follows the line. Fix the access, then choose **Refresh**. |
| Model files: Unfinished copy | A download or preparation that has not finished: still running, or interrupted. Pulsar doesn't serve from it. When the line names a spec, the node recorded that preparation and Purge on that spec removes it; otherwise no Pulsar command removes it. |
| Model files: Not recognized | An item in a Pulsar folder that is not a model snapshot Pulsar can identify; the reason follows its path. Pulsar changes nothing. |
| Model files: view only | No catalog spec uses these files, so Pulsar has no cleanup for them. Nothing was changed. |
| Model files: Size per copy | The model's listed size for one complete copy. Shared copies are stored once, so it is not what deleting would free. |
| Model files: would free | On a snapshot's page, for each spec that can clean it up: the listed size of the prepared copies that spec's **Purge** deletes, added up across nodes. A shared copy and a home used as a prepared copy are only released, so they free nothing. Purge removes nothing while any of the spec's copies, in any of its snapshots, or an unfinished copy it recorded, is pinned or in use, so the line then says it is blocked and names what clears it. A confirmed node whose files, records or containers could not be read makes the figure a minimum; a node outside confirmed membership counts only where it holds the spec's copies. A snapshot choice shows `frees N GiB` when one spec can clean it up and the whole figure is known. |
| Model files: Storage budget | Under the nodes, one row per node from `./pulsar model budget`: disk free, the reserve, Pulsar files, and what the node can still copy. **not observed** gives the reason the budget could not be read; the files above are still current. Each budget read is also saved on the controller for the home screen's **Disk** rows. |

## Configure topology and storage

### Confirm membership and SSH trust

Confirmed topology determines membership and physical node identity. On a
terminal, `./pulsar` before setup offers **Set up this cluster…**, which runs
`./pulsar topology setup` (and also records the SSH identities when membership
is already saved), read-only catalog browsing (`./pulsar models menu
--read-only`), **Help and troubleshooting…** and Exit. When saved membership is
invalid, setup cannot replace it and the first choice is **Start over: forget
this cluster…** (`./pulsar topology forget`) instead. Archives are optional and
do not restrict navigation after cluster setup. A setup step that fails
reports that it did not complete before the menu returns. Entering the menu
uses saved local files; it does not probe nodes. After cluster setup, open
`./pulsar` and choose **Cluster…** (see [Cluster…](#cluster)); today's topology
menu is under **Help and troubleshooting… → Advanced: single operations… →
Cluster topology**. The same actions are available directly:

```sh
./pulsar topology setup
./pulsar topology show
./pulsar topology check --json
./pulsar topology detect --json
./pulsar topology detect --candidate HOST
./pulsar topology configure
./pulsar topology configure --remove-missing
./pulsar topology forget --plan
```

`show` and `check` name each node by hostname with its position, the saved
order that start uses for ranks; JSON keeps the stable `node_id` and `rank`.

Start with `setup` on a fresh checkout. It shows five numbered steps and checks
every prerequisite before it can fail later; it installs nothing:

1. **This machine**: Docker and GPU access, its RoCE links, and its
   prerequisites (Python 3.11 or newer, `flock`, `rsync`, the `hf` CLI and its
   Hub login), each missing one with its fix.
2. **Find the machines**: the discovery review below. A machine that answers
   with a host key SSH on this machine does not trust is listed with the check
   to run (`ssh -o StrictHostKeyChecking=ask HOST`); it is not added.
3. **Check each machine**: each machine discovery found, whose Docker, GPU and
   links to every other machine discovery verified, is asked for the same
   prerequisites over SSH with discovery's strict host-key policy.
4. **Save**: configuration's fresh checks and its question.
5. **Identities**: the SSH identity check, and recording them
   (`./pulsar ssh-trust enroll`) with its own question, `Record these SSH
   identities?`, after the fingerprints, when identities are missing.

Setup then finishes with a topology readiness check and lists what is still to
fix on each machine. A missing prerequisite never blocks saving; only the
platform facts discovery checks decide whether a machine can be a member. A
missing `hf` CLI or Hub login is a warning that says what it affects: models
cannot be downloaded to that node, and it can still serve. The `hf` login check
asks the Hub once from each machine, with that machine's own login; a Hub that
cannot be reached is reported as not checked, never as failed. Healthy existing
setup is reused: with membership already saved, setup checks this machine,
says it uses the saved membership, and goes on to step 5. Cancellation or
failed enrollment cannot report success. This applies to single-node clusters
too. The first-run menu's **Set up this cluster…** and the topology menu's
**Change membership… → First-use setup** run this same command.

After discovery, review the machines found. **Add a machine by hostname or IP**
accepts one management address at a time; add more as needed. The review lists
them ("Added: …"), an address already listed is not added twice, and **Remove an
added address** drops one and reruns discovery without it. **Retry discovery**
keeps those candidates for this interaction. Inside the review, discovery's own
hints name these choices instead of `topology configure` or `--candidate`.
**Back** leaves setup without
saving and exits with status 1; Escape from address entry returns to the review,
and Ctrl-C exits setup.
A failed discovery still offers Add, Retry, SSH help and Back, but cannot continue
to saving. **Continue to membership review** runs configuration's fresh checks,
then asks whether to save the actual number of machines shown, with No selected
by default. Only you know which machines you intend to include: if fewer appear,
add the missing addresses or explicitly confirm the smaller membership. Declining
a fresh setup's save returns to discovery review with your candidates retained.

**Machine still missing? SSH help** explains how to check the address, network
and normal SSH access (`ssh -o StrictHostKeyChecking=ask HOST`). Discovery does not reliably distinguish these
failures. Before accepting a new or changed OpenSSH host key, verify its fingerprint
independently using the machine's console or its administrator, then retry.
This initial OpenSSH trust is separate from Pulsar's later fingerprint review and
identity confirmation (step 5); the menu never accepts new host keys for you.

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

Every discovery that runs against valid saved membership (`detect`, `configure`
and setup's review) saves what it noticed, so a later screen can show it
without probing: the **changes noticed** record, `.pulsar/cluster-changes.json`
beside the saved inventory and storage budget. It holds when it was observed,
the saved membership's `topology_id`, and a result: `unchanged`, `changed`, or
`not-checked` when discovery itself failed (with why). It lists machines found
that are not members; members that did not answer, and members that answered
but were rejected, with the reasons; members whose control address, links or
position changed, old against new; and candidates that answered with a host key
SSH on this machine does not trust yet. Such candidates are listed but do not
make a change by themselves, since discovery cannot tell whether they are GB10
machines. Pulsar identifies a node by its OS machine ID, so a reinstalled
machine is one node gone and one new: a found machine or untrusted address with
the hostname or address of a member that did not answer is marked
`possible_reinstall`; a candidate that is the other mDNS name or address of a
member that answered is not listed. A successful save rewrites the record as
unchanged. Recording SSH identities changes the `topology_id` but not the
nodes, so it carries an `unchanged` record that names the membership from
before enrollment forward to the new `topology_id`, keeping its time; it leaves
any other record as it is. A record that names another `topology_id` than the
saved membership describes an earlier membership.
`detect --json` carries the same body as `changes_noticed`. Saving the record
never changes discovery's output or exit status; it holds hostnames and
addresses and stays private under the ignored `.pulsar/` folder.

`configure` displays discovered membership and changes, then asks before saving.
Noninteractive use requires explicit `--yes`. Active services or an unobservable
required node block replacement; the check repeats after confirmation. It never
stops services for you. Saving a discovery establishes membership, not enrolled
SSH trust; **Review what changed…** records the identities after a save, or run
`./pulsar ssh-trust enroll` afterward. For an existing cluster, a machine that
discovery finds appears under **Cluster… → Review what changed…**; to add one by
address, choose **Help and troubleshooting… → Advanced: single operations… →
Cluster topology → Change membership… → Configure membership or add a
machine** to use the same
candidate review. It runs `topology configure --candidate HOST` with your added
addresses, then returns to the menu for separate enrollment after saving.
`topology setup` reuses valid saved membership and never adds machines to it.
Candidates apply only to a fresh setup or to diagnostic discovery for invalid
saved membership. With valid saved membership, `setup --candidate HOST` exits
with status 2 before running anything, says that HOST was not added, and names
`topology configure --candidate HOST`. Cancelling configuration does not add a
machine or start enrollment. Explicit `--accept-new-host-keys` is
available only on configuration through this CLI, not on read-only detection.
Before the prompt, configuration reports homes, prepared views, and pins affected
by the proposed topology. The report is advisory and read-only; saving membership
does not silently move, purge, unpin, or rewrite model-library records.

Any save changes the membership's `topology_id`, and prepared-copy records bind
the `topology_id` and position they were made under, so start does not use them
until Prepare records them again. Prepare takes over a copy made before the
membership changed when it is on a node that still serves the spec and its
files still verify: the plan shows the action `rebind` ("take over verified
files prepared before the cluster's membership changed"), and only the records
change, on the node and on this controller. Nothing is copied or deleted, the
copy needs no free space, and a pin stays. A copy whose files no longer verify,
or that a container still mounts, is reported as before (`changed-copy`,
`pinned` or `container-reference`), to be purged before it is prepared again.
A copy on a node that no longer serves the spec is left as it is; purge removes
it with the spec's other copies. A save also writes membership without recorded
SSH identities, so every node, not only a new one, is enrolled again with
`./pulsar ssh-trust enroll`.

A confirmed node that does not answer keeps membership from being saved, because
Pulsar cannot tell a node that is down from one that is gone. When it is gone,
`./pulsar topology configure --remove-missing` saves membership without it. The
flag leaves out only confirmed nodes that did not answer discovery; a member
that answered but no longer qualifies still blocks the save. Each node left out
is asked once more who it is, at its saved address: if it answers with its own
machine ID, the save is refused (run discovery again); if another machine
answers there, as after a reinstall, that machine is checked for running
services like any node, and joins as a new node after the saved ones.
Nodes that answer are checked for running Pulsar services as before, and a
running one still blocks. The output names each node left out and says that it
could not be asked whether a Pulsar service runs there. The storage impact
prints first, naming the homes recorded on the node left out and the prepared
copies and pins to reconcile; then the question names what is left out and
starts on No, such as `Save cluster membership with 2 machines, without
<node-3>, which did not answer?`. `--yes` answers it for automation. The
remaining nodes keep their relative order. Without the flag nothing changes,
except that the refusal names it. There is no manual removal of a node that
answers.

`./pulsar topology forget` starts this checkout's cluster state over, for
example when the nodes are gone, the checkout should point at another cluster,
or the saved membership is invalid. `--plan` lists what it would remove and
keep and changes nothing. It removes the saved membership, the generated SSH
configuration and the saved observations that describe the cluster (service
inventory, storage budget and changes noticed). It keeps `.env`, the site
overlay, the model-library records, model files on the nodes, archives and
OpenSSH's own `known_hosts`. The site overlay can name a node for placement;
the plan says which nodes it names. Before removing anything, forget copies the
files into a private folder under `.pulsar/` (owner-only permissions, named by
time, such as `forgotten-cluster-20261109T120000Z`) and prints where. It
refuses while a Pulsar service runs on a node that answers; a node that cannot
be asked is named as not observed and does not block, since forget is the way
out when a node is gone. With invalid saved membership, or an SSH configuration
that cannot be used, it asks only this machine and says so. On a terminal it
asks first, starting on No, and asks the nodes again after a yes, so a service
started meanwhile still refuses; off a terminal it needs `--yes`, and `--json`
prints the plan or the result. Afterwards the setup status reads as before
first use; run `./pulsar topology setup`.

Show, check, detection and forget support `--json`. `./pulsar topology menu` opens just
this menu; opening either menu performs no probes. The topology menu opens with
a summary of saved membership, read from `topology show --json`: the nodes by
hostname, when the discovery behind it ran and when enrolled host keys were
read. Both are stamped before the save is confirmed. Setup and configuration
never replace invalid saved membership, so the summary says to inspect it or
start over with `./pulsar topology forget`. The summary is drawn again only after an action
changes saved membership or SSH trust. Where the terminal is wide enough, each
choice says what it does to the nodes: *(saved, no probes)* reads saved files,
*(checks nodes now)* contacts the nodes, and *(asks before saving)* asks before
changing anything. The changing actions are grouped under **Change
membership…**. Every menu choice runs one of the commands above or `./pulsar
ssh-trust check` or `enroll`. Low-level manifest utilities remain available
under `pulsar topology` with their existing arguments. From the private
workbench, invoke the configured public Stack executable by absolute path;
Workbench creates no second topology implementation.

Topology and SSH files stay private in the selected stack checkout. Host and
fabric diagnostics are also available through `./pulsar doctor`. Catalog
browsing works before topology is configured.

### Archive location

A **home** is the complete local copy of one exact snapshot on a confirmed member. Serving
nodes use prepared copies. The library records their explicit
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
Unless a process value overrides the saved one, the catalog menu then leaves
out **Restore**, **Create archive** and **Verify archive** until a location is
set again. If the setting cannot be read, such as a malformed value or an
unreadable `.env`, the home screen says so with the reason and where to fix it,
instead of reporting archives as not configured.

## Browse and check

Browse the catalog before or after configuring a cluster. A schema-valid spec
under `releases/` is a catalog member; state and review metadata do not gate
membership or serving. Reading the catalog does not probe nodes.

Commands typed by a person accept a unique catalog spec ID prefix of at least
12 characters, as `models list` shows it, and print the complete ID they
selected. An ambiguous or unknown prefix fails and names the matching IDs.
`--json`, `--spec-file`, `model purge` and `model remove` require the complete
64-character ID, so scripts and the workbench never depend on a prefix. Under
`--json`, a shortened ID fails as a `usage_error` before any lock or node
contact. Human
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

`pulsar status` without a spec prints the same list, then how to check one
spec live. `pulsar release show <spec-id>` prints a catalog spec's complete
JSON and `pulsar release verify <spec-id>` its state and review; both need the
complete ID.

### Read recipe details

The selected-spec menu shows a frame of aligned fields: **Recipe**,
**Settings** and **Image** describe the spec; **Files**, **Archive** and
**Running?** give saved and session observations with their age;
**Lifecycle** marks where the spec stands in Download → Prepare → Start from
those records (only a current running observation marks Start done);
**Results** summarizes published results on one line; **Next** is always the
last line. A value that needs explaining says what to do on the same line, such
as `Files  not checked yet; choose Check now (read-only)`, and the
[catalog labels](#catalog-labels) explain every status word.
On a terminal too short for the frame and every choice, the frame keeps only
its first line and Next. Recipe geometry and explicitly recorded
context, sequence and quantization settings describe intended execution; the
menu does not infer engine defaults or resolve repeated arguments. The image
digest is abbreviated here; **Show details** retains the complete identity and
arguments. Maintainer reviews are advisory and keep their dates separate from
measurement dates. Saved file checks retain their age, and the summary explicitly says
that it has not observed the live service. Each recorded home and prepared copy
shows when it was last verified, and a blocker naming a rank's recorded copy
gives that age, so an old record can be weighed against the check.

### Read published results and compare recipes

**Published results** (also `models results SPEC`) reads only this catalog's
`results/` records for the selected spec and uses the existing evidence verifier.
It shows recorded run times, run and policy outcomes, benchmark token counts and
concurrency, accuracy sample scope, soak results, and the command for the full
public evidence summary. Each gate's outcome is followed by one sentence saying
what it checked, rendered from the thresholds the run recorded, and benchmark
levels form a rounded table: concurrency, requests, total and median decode
tok/s, and p95 time to first token in ms. `policy show NAME` explains the same
gates for a fixed policy; its `--json` form is unchanged. The compact view shows the most recent dated verified
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
only when the spec sets them; State says it is a maintainer label, not a test
result. The archive line reconciles the last check with
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

In the catalog list, a withdrawal or unsupported-start warning leads its
spec's line, right after the spec prefix. The selected-spec menu repeats a
withdrawal's reason and recorded review date, when supplied, before its
suggested action. Start
and the memory-warning retry show the same wrapped notice before their existing
confirmation. These are advisory maintainer metadata: they do not change
catalog membership, launch checks or the number of confirmation steps.

Normal browsing reads saved observations and shows their age. Unobserved
state is unknown. A saved successful check is not a promise that the next
launch will work. `models check` (**Check now** in the menu) checks the
selected managed locations; it does not search arbitrary cache trees, download
files or start a server. Files prepared on every serving node does not mean a
service is running. Use [live service inspection](#inspect-a-running-service) to check
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

The menu's **Serve a model…** composes these steps into one plan for a chosen
model and runs them in this order, each after its own preview; see
[Serve a model…](#serve-a-model). The commands below are the same steps one at
a time.

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

Before fetching anything, acquisition looks on every confirmed node for a home
folder of the same snapshot. When it finds one, `acquire --plan` shows
`reuse matching existing files`, and `--yes` verifies the folder against the
spec manifest and records it as the home without downloading; the menu asks
`Register the existing verified files of … Nothing is downloaded.` This
discovery has limits:

- It reads one home root on each node: the spec's home acquisition root (its
  overlay `cache_root`, else `PULSAR_HOME_ROOT`, else the default). A folder
  under another root, which the Model files page can still show, is not found,
  and acquisition downloads instead.
- When no record names the folder, it hashes every file, which can take many
  minutes for a large model.
- It refuses when the folder is on another node than the one chosen with
  `--node`; choose the node that holds it.
- It stops when a second matching folder exists on any node.

### Prepare all serving nodes

```sh
./pulsar model prepare <spec-id> --node <node> --plan
./pulsar model prepare <spec-id> --node <node> --yes
```

Read the preview's reuse, copy, and capacity results before applying it. The
completed operation makes every required snapshot available on every selected
serving rank. It does not start a model. See [prepared copies and storage
estimates](#prepared-copies-and-storage-estimates) for sharing and retry behavior.

Each node keeps its own records of the prepared copies on it, beside the
controller's. After the controller's records are lost, Prepare finds the copies
through the nodes' records and reuses them. When a node's records are lost as
well, a prepared copy left on that node has no record, and Pulsar never adopts
or deletes files it did not record. The preview is then blocked: it names the
node and the folder where Prepare would place the copy, and `prepare --yes`
stops before it copies or records anything. Purge cannot help, since it removes
only what records name. Inspect the folder; if it is not needed, remove it
outside Pulsar, then prepare again, which copies and verifies the files.

Control SSH, inference traffic and model transfer use distinct configured
paths. Multi-node preparation preserves the selected transfer contract and
does not silently change networks.

### Check and stage the image

Image commands require the complete spec ID. First inspect the exact pinned
image on the selected serving nodes:

```sh
./pulsar image check <spec-id> --node <node>
```

A node can hold the pinned content under another repository name, such as a
local tag, without the pinned name; the check then names that tag and still
fails, since start needs the pinned reference. Where the image store makes the
image ID the manifest digest, as Docker's containerd store does, the check
finds it by digest. A pull adds the pinned name and reuses the content already
there. If the image is missing, or present only under another name, select a
registry pull explicitly, review its preview, and then apply the same
selection:

```sh
./pulsar image stage <spec-id> --node <node> --pull --plan
./pulsar image stage <spec-id> --node <node> --pull --yes
```

Without `--plan` or `--yes`, staging checks the nodes and changes nothing: an
image already present on every node is reported as such, and a missing one is
refused with the nodes that lack it and the `--plan` command to run next.
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
when staging the selected digest-pinned image is also approved. Start asks no
confirmation: `start --yes` is deprecated, grants neither permission and is
removed in CLI contract 2.

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
./pulsar logs <spec-id>
./pulsar inventory
./pulsar inventory menu
```

`status` leads with its answer, for example `spec 139908cf23bb: running and
healthy on spark-1; recipe and files verified`. It asks the service's API
`/health` once. When the complete observation is unavailable, such as for a
service started before launch records existed, it reports the inventory's view
and says it is not verified, and why. It tells "no service exists" apart from
"a node could not be observed".

`logs` prints the recent output of the spec's containers, the last 200 lines
of each by default (`--tail N`), under a heading such as `spark-2 (rank 1) ·
exited`. It reads every rank, including containers that have exited, but only
containers the Stack owns, and it changes nothing. `--node` or `--rank` selects
one container, even of a multi-node service, and `--follow` keeps printing one
container's new output until Ctrl-C. It exits 1 when no container of the spec
exists and 3 when a node could not be observed; the other nodes' logs are still
printed. It has no `--json` form; `status --json` reports the service's state.
A failed multi-node start removes its containers, so only the logs that start
printed remain.

In the menu, **Running services…** on home shows what runs and offers these
commands by what they do: **Check it now** (`status`), **How to connect**,
**Logs** (`logs`), **Restart…**, **Switch to another model…**, **Stop…** and
**Stop every service…**; see [Running services…](#running-services). Its card
states each fact with where it came from and its age: the service inventory
holds the model, state, nodes, port and served model name, and **Check it
now** adds the API address, whether a key is required and the health answer.

**Live service inventory**, under **Help and troubleshooting… → Advanced:
single operations…**, opens `inventory menu`, unchanged. Select an
observed service whose spec is published in the catalog, then choose
**Detailed status**, **Service logs** or **Stop service**. These invoke the
existing status, logs and stop commands. One-node services use the node ID recorded in the inventory;
multi-node services retain the commands' participating-node resolution.
The frame shows how long ago the inventory was taken and its UTC time, and
**Refresh inventory** obtains a new observation. Each service row names its
observed node count and the API port its rank 0 container runs with; nodes are
listed in rank order.

Catalog services come first. A Pulsar-managed service whose spec is not in
this catalog, for example one an experiment started, follows as its own row
ending `outside the catalog`. Selecting it offers **Service logs** and **Stop
service**, but no **Detailed status** (status needs a catalog spec) and no
**Open in catalog**. Its Stop runs `./pulsar stop` with the full spec ID,
which finds the containers by their Pulsar labels on every node, and its
question adds `This service was started outside this catalog.` It is offered
only when the inventory established safe ownership and every confirmed node
answered.

The frame names, one line each, the containers this menu cannot act on, by
what they serve, their state and their owner, such as `vllm-port80 · running ·
not started by Pulsar · read-only`, and the unmanaged GPU processes with
their memory on each node. These stay read-only. **Stop every Pulsar service…** runs
`stop --all --yes` after a fresh inventory: its question lists each service
with the Pulsar-managed label and its nodes, says containers Pulsar does not
manage are untouched and files are kept, and defaults to No. **Open in catalog** opens the service's recipe in the catalog
menu with that inventory, so its frame and suggestion already know the
service; **Back** returns to the service with a refreshed inventory. **Show full inventory** retains the existing read-only view
of all detected workloads, including those outside the catalog.

Stop refreshes the inventory and shows the current scope before confirmation.
The catalog menu's **Stop** does the same, and both ask one question that names
the nodes the service was seen on and says model files, pins and archives are
kept. For a one-node recipe the catalog menu stops on one machine: where the
refreshed inventory saw the service, without asking, when it saw it on exactly
one confirmed machine; otherwise the machine chosen next. Its question names
that machine, and adds where the inventory saw the service when that is
elsewhere. Outcomes name the model and its spec.
It is unavailable when inventory has not established safe ownership and
observability, or a one-node service has ambiguous placement. The stop command
still rechecks ownership and can refuse; the menu does not bypass its guards.
After stop, the menu prints the outcome and how long it took, then waits for
**Continue**, as the catalog menu does (see
[operation outcomes](#operation-outcomes)). **Detailed status** and **Show full
inventory** open in the pager when their output is long. Detailed status adds
an outcome only when status answers no (exit 1) or cannot tell (exit 3).
Model files, pins and archives are retained. The ordinary `inventory` and
`inventory --json` commands remain read-only toward the cluster and change
nothing on a node; each run saves its observation on the controller for the
home menu's Serving line.

### Verify the recorded service and files

`pulsar observe --service-id ID --json` verifies the effective recipe, container
configuration and continuity on every rank, using normal metadata-based file
verification. Add `--full` to request a fresh full content audit. Archive
verification and verification of newly copied bytes remain full audits.
Prepared-copy inspection in `model info`, `model check` and `observe` uses up to
three verification workers, with at most one per physical node. The batch covers
homes and prepared copies for every required snapshot; shared references to the
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
sampling; model services are unaffected. Each node's sampler runs until then:
if one ends on its own, such as when its SSH session drops, the stream writes
an error record naming its rank, says which node it was on stderr and exits 1.

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

In the menu, **Models and disk space… → Back up a model…** covers every
required snapshot of a model in one plan: one `archive create` step per
snapshot that has no verified archive, each with its own preview at its turn.
Snapshots already archived are named and left alone, since an existing archive
is never replaced, and a snapshot with no home blocks the plan. **Check the
archives…** is one plan of `archive verify` steps, one per archived snapshot.
Both need the archive location; without it the page says where it is set. A
home moves from the menu only as **Free up space…**'s `Move the files of …`
line, when a node is past its reserve and another has room; see
[Models and disk space…](#models-and-disk-space).

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

The nodes' homes can outlive the controller's records: a separate controller
machine, a deleted checkout, or control moved to another node. When they
survived, **Download** registers them without fetching anything: its plan
finds the home folder, and `--yes` verifies it against the spec manifest and
records it, offline. **Restore** always copies the whole snapshot from the
archive, so try Download's preview first. Download's discovery has the
[limits listed under acquisition](#acquire-or-restore-every-required-snapshot):
one home root per node, every file hashed when no record names the folder, a
refusal when the folder is on another node than the one chosen, and a stop at
a second matching folder.

```sh
./pulsar model acquire <spec-id> --snapshot target --node <node> --plan
```

In the catalog menu, choose **Model files and disk space (checks nodes now)**
in the catalog list first. When that scan found the home folder of a
required snapshot with no recorded home, the recipe's Files field reads `files
found on <host>, not in Pulsar's records; Download checks them`, and the
suggestion is **Download**, ahead of **Restore**. It needs no saved or current
check, since **Check now** reads homes from records only and cannot see that
folder; an outcome from this session, such as an interrupted operation, still
comes first.
Download's machine question then starts on that machine, labelled `files found
here, not in Pulsar's records`. The choice stays **Download**: its preview
says whether the files are reused. Only a scan from this catalog session
counts; **Models and disk space…** on the home menu runs on its own.

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

Stopping one spec asks no confirmation. To stop every Stack-managed service on
all confirmed nodes, run `./pulsar stop --all --yes`; without `--yes` it warns
and still runs until CLI contract 2 requires it.

In the menu, **Running services… → Stop…** and **Stop every service…** run
these commands after the same questions as before: Stop's names the nodes the
service was seen on and says model files, pins and archives are kept, and
Stop every service's names each service it removes. Both start on No.
**Restart…** there is Stop then Start of the same recipe on the same nodes,
shown as a two-step plan that starts on **Cancel**; neither it nor **Switch to
another model…** passes `--replace`. Stopping removes containers only; no
model files are deleted.

To reclaim storage from the menu, choose **Models and disk space… → Free up
space…**. It lists what can go as outcomes, largest gain first: removing a
model from its serving nodes (Purge), removing it from the cluster (Purge, then
Remove home, with its archive state said and an offer to back it up first),
moving a home off a node past its reserve, and clearing unfinished copies.
What cannot go is listed with its reason: a serving model, a kept model, a
shared copy, or a container Pulsar did not start. **Keep a model ready…** is
Pin and Unpin by that name. See [Models and disk space…](#models-and-disk-space).

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
copies and preserves homes and archives. It releases only the selected spec's
prepared copies: a copy another spec also uses is a shared copy, and its files
stay while any other spec uses it, even unpinned. A home used as a prepared copy
is released the same way. Only a prepared copy no other spec uses is deleted with
its files, after the usual ownership, container and incomplete-operation checks.
The menu's preview and question say how many prepared copies Purge deletes, with
their size when the plan records it, how many shared copies it releases, and how
many unfinished copies it clears, leaving out what it does not do. The result
counts the prepared copies released, the prepared copies whose files were deleted
and the unfinished copies removed (`bindings_removed`, `copies_removed` and
`preparations_removed` in JSON); when all three are zero it says **Nothing to
purge**. Container references
to a shared path, including stopped containers, still block purge. A container
that mounts any directory containing the copy, such as a whole home directory,
counts. The plan names each such container, its node, whether it is running,
and the mount, with a fix: Stop for a service Pulsar started, otherwise remove
the container or its mount. Pulsar never removes a container it did not start.
In the catalog menu, a blocked plan offers **Stop** for that Pulsar service
(asked first) and **Open Live service inventory**.

### Model files across the nodes

Choose **Help and troubleshooting… → Advanced: single operations… → Model files
and disk space (checks nodes now)**, or the same choice in the catalog list;
both open the same page. (**Models and disk space…** on the home menu is the
page by outcome above it.) It
lists model snapshots across every confirmed node,
including managed downloads that have no catalog recipe. You do not need to open
each recipe to see placement. Entering this menu always checks nodes and their
managed filesystems now; it does not present saved placement as current. The equivalent commands are:

```sh
./pulsar models files       # interactive overview
./pulsar model files        # read-only report
./pulsar model files --json
```

The overview shows each node's free space and folders, then the storage budget
table (see [prepared copies and storage estimates](#prepared-copies-and-storage-estimates)),
then one model-by-node table when it fits; narrow screens use stacked entries.
The budget is read after the file scan because it takes the shared storage lock,
as the cluster overview does; when it cannot be read, its line says **not
observed** with the reason and the files stay shown. The cells use words
(`home`, `copy`, `shared`, `pinned`, `in use`). Only when the words do not fit
do short codes (`H`, `P`, `used`) take their place, with a key under the table
that explains just what is shown. Pins, shared copies and container use are
marked separately; `in use` includes stopped containers. `?` means the node
could not be checked and `—` means the model is not in Pulsar's folders on that
node, never that it is absent from the whole machine. The
[Model files rows](#catalog-labels) of `./pulsar help labels` explain every
word. A node failure keeps observations from the other nodes. If only container
inspection fails, file placement remains visible and container use is unknown.
When a node or its pin records cannot be read, pins are unknown; saved pins or
shared copies in details are explicitly labeled as saved records.

Discovery reads immediate children of the configured home and prepared-copy
namespaces, explicitly configured overlay home roots, and locations named by
saved records. It does not search the filesystem broadly, follow internal
symlinks, register discovered files, or change model-library records; a home
folder no record names is a reason to try **Download**, whose plan registers it
(see [Restore after controller loss](#restore-after-controller-loss)). A download
or preparation that has not finished (still running, or interrupted) appears as
an **Unfinished copy** under its node, and an unidentified managed path as
**Not recognized**; this page changes neither. When the node recorded an
unfinished preparation, its line names the spec, and Purge on that spec removes
it. When two disks hold the same kind of folder, each **Free space** line gives
its paths. Declared file names and sizes are checked without hashing model
contents; presence is not proof of complete verified model identity or
readiness to serve. The **Checked** line gives the
age of the observations and each node's folder lines say what was read.
**Refresh** collects new observations; viewing details reuses the captured
result.

**Size per copy** is the model's listed size from its manifest for one complete
copy, not allocated disk usage or promised reclaimable space. A home used as a prepared copy and a
prepared copy shared by several specs each remain one physical location.
Archive information comes from saved verification records, with its time; archive
storage is not scanned by this overview and is never a cleanup target.

Close the page, then choose a snapshot for its paths and cleanup. That page
says, for each spec that can clean it up, what its Purge would free: the listed
size of the copies it deletes, leaving out shared copies and the home; or that
Purge is blocked, with what clears it: **Stop** for this spec's own service,
stopping another service from its own recipe or **Live service inventory**,
removing a container Pulsar did not start outside Pulsar, or **Unpin prepared
copies**. Purge covers every snapshot the spec has copies of and the unfinished
copies it recorded, so a pin or a container on another of its snapshots, or a
container that mounts its unfinished copy, blocks it too, and the line names
where. Each node is named once. The figure is a minimum when a confirmed node, or its records or
containers, could not be fully checked; a node outside confirmed membership
counts only where it holds the spec's copies. The snapshot
choice adds `frees N GiB` when one spec can clean it up and the figure is
complete. Purge's own preview rechecks every node before anything is deleted. A registered
snapshot with one catalog spec opens that spec's existing **Stop**, **Unpin
prepared copies**, **Purge prepared copies** and **Remove home** menu. With
several specs, **Clean up through which spec?** asks first. Purge still covers
the selected spec's prepared copies; **Remove home** selects a snapshot. Every
mutation keeps its existing plan, confirmation and protections. Uncataloged or
unregistered files with no catalog spec are *view only*: the page is all the
menu shows for them, and discovery does not authorize deletion. A prepared copy
whose records were lost appears as a copy marked *not in Pulsar's records*.
Pulsar does not record or delete it; if Prepare for its spec is blocked by that
folder, inspect it and remove it outside Pulsar when it is not needed (see
[Prepare all serving nodes](#prepare-all-serving-nodes)).

To discard a downloaded model after trying it, stop its service, unpin and purge
its prepared copies, then choose **Remove home** and confirm. Both catalog and
lab homes can be removed without an archive. All protections apply to the shared
snapshot, not only the selected recipe; dependent copies from other recipes must
also be cleared. Recipes and experiment results remain.

### Remove an unused home

Removal deletes the downloaded model files in a home after its prepared-copy
dependencies are cleared. An archive is neither required nor checked, so removal
also works when archive storage is unconfigured, disabled or unavailable.
Review the preview and explicitly confirm:

```sh
./pulsar model remove <spec-id> --snapshot target --plan
./pulsar model remove <spec-id> --snapshot target --yes
```

For schema 3, name the snapshot whose home you intend to remove. Removal still
refuses container references (including stopped containers), unknown node state,
incomplete preparations and files that do not match managed ownership. Existing
archives are preserved. Recovery requires a usable archive or an available
download source; without either, the deleted files cannot be recovered through
Stack. The former lab-only flag `--discard-unpromoted` remains accepted for
compatibility but is no longer required; `--yes` confirms removal for every home.

### Shared-record compatibility

Sharing upgrades the affected private prepared-copy records to schema 3 during
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
| A model will not start | **Help and troubleshooting… → Why will a model not start?**, or `./pulsar start SPEC --dry-run` | Follow the fix each blocker names; in the menu each is a choice. |
| Setup incomplete or a node unreachable | `./pulsar topology show`, then `./pulsar topology check` | Repair membership, SSH trust, or connectivity using [topology setup](#configure-topology-and-storage). |
| A confirmed node is gone for good | `./pulsar topology detect`, which names it as missing | Save membership without it with `./pulsar topology configure --remove-missing`; see [Inspect or change membership](#inspect-or-change-membership). |
| Saved membership is invalid, or this checkout should start over | `./pulsar topology forget --plan` | `./pulsar topology forget` keeps a private backup copy; then run `./pulsar topology setup`. |
| Download to a node fails, or doctor warns about `hf` there | `./pulsar doctor`, in that node's section | Install the `hf` CLI, or run `hf auth login`, on that node as the login user. These are warnings: the node can still serve. A Hub that could not be reached is not checked, not failed. |
| Docker unavailable | `./pulsar doctor` | Resolve the reported Docker or GPU-access prerequisite on the affected node. |
| Pinned image missing | `./pulsar image check <spec-id> --node <node>` | Review [image staging](#check-and-stage-the-image) for the same spec and nodes. |
| Model files missing or not prepared | `./pulsar model info <spec-id> --node <node>` | Acquire or restore missing homes, then prepare every required snapshot and rank. |
| Too little storage | `./pulsar model budget` and the operation's `--plan` | Review [storage cleanup](#stop-and-reclaim-storage); a preview does not reserve capacity. |
| Prepare blocked by a folder that no Pulsar record names | `./pulsar model prepare <spec-id> --plan`, which names the node and the folder | Inspect the folder on that node; if it is not needed, remove it outside Pulsar, then prepare again. Purge cannot remove it; see [Prepare all serving nodes](#prepare-all-serving-nodes). |
| Insufficient memory or a memory warning | `./pulsar inventory` and the failed start's memory report | Insufficient memory blocks start. `--accept-memory-warn` acknowledges only a warning; see [memory estimates](MEMORY_ESTIMATES.md) for the optional exact-spec estimate. |
| Existing service or port in use | `./pulsar inventory` and `./pulsar status <spec-id>` | Identify the owner; stopping, replacement, and a deployment-port change are separate choices. |
| `guard_unsupported` | `./pulsar models show <spec-id>` | Use the separately scoped [guarded serving](GUARDED_SERVING.md) procedure. |
| `historical_spec` | `./pulsar models show <spec-id>` | Read [historical compatibility](#historical-specs-and-current-observations); use a current spec for a new launch. |
| Launch or test completion failed | `./pulsar status <spec-id>`, `./pulsar logs <spec-id>` and `./pulsar inventory` | Inspect the reported remaining containers and their logs before retrying; failure does not always mean nothing is running. |
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
multi-node start that never became healthy removes its containers. Where a
container remains, the note names `./pulsar logs <spec-id>` for more than the
last lines printed. It says
they were removed only after confirming it on every node; otherwise it names
the nodes left and suggests `./pulsar stop`. The same blockers appear in `start --json`
error details; see [the public contract](CONTRACT.md). A spec with a serving
guard is refused by ordinary start before any check as `guard_unsupported`.
Use the separately owned [bounded guarded serving](GUARDED_SERVING.md) path
for an explicitly scoped foreground trial. Read-only `start --dry-run` remains
available for its prerequisite planning.

### Interrupt a start

Ctrl-C (or a stop signal) ends start and removes nothing. Before launch, start
says `Interrupted before launch; nothing was started.` After launch, an
`INTERRUPTED start_interrupted` line names the service's node and what start
was doing, for example
`INTERRUPTED start_interrupted: spark-1: start was interrupted while the service was loading; it keeps loading. Next: ./pulsar status <spec-id> --node spark-1`,
followed by the stop command that removes it. A multi-node start interrupted
while its ranks were starting names the rank; that cluster is incomplete and
cannot serve until it is stopped and started again. Removing a service is
`stop`'s job: it takes the lock, probes every rank and confirms the containers
are gone. `start --json` reports the same record in its `cancelled` error.

## Menu reference

### Home

The home menu reads saved records without probing nodes. After setup it shows a
status block, then six choices:

| Choice | Opens | Same as |
| --- | --- | --- |
| **Serve a model…** | A model's plan: the steps it still needs, approved once; see [Serve a model…](#serve-a-model). The catalog menu is under **Help and troubleshooting… → Advanced: single operations…**. | The commands each plan lists; its outcome names those that ran |
| **Running services…** | What runs, how to connect, and Restart, Switch and Stop; see [Running services…](#running-services). Today's inventory menu is under **Help and troubleshooting… → Advanced: single operations…**. | `./pulsar inventory`, then the commands each choice names |
| **Models and disk space…** | Room on each node and each model on the cluster, then Free up space, Get ready, Keep, Back up and Check, each as a plan; see [Models and disk space…](#models-and-disk-space). Today's Model files page is under **Help and troubleshooting… → Advanced: single operations…**. | `./pulsar model files` and `./pulsar model budget`, then the commands each plan lists |
| **Cluster…** | The cluster's one place; see [Cluster…](#cluster). | `./pulsar topology show`, then the commands below |
| **Help and troubleshooting…** | Diagnostics, the guides and Advanced; see [Help and troubleshooting…](#help-and-troubleshooting). | `./pulsar help` |
| **Exit** | Leaves the menu; Esc does too. | |

The status block has at most four labelled lines, with their wrapped
continuations, so it fits on an 80×24 terminal with the menu (see the
[Cluster, Serving, Disk and Attention labels](#catalog-labels)):

- **Cluster**: the saved membership's node count and how many nodes answered
  the last **Check the cluster now**, or the last discovery when that is newer,
  with its age. Before any check it reads `not checked yet`; a check saved for
  the membership before its last save reads `not checked since membership was
  saved` and is not shown as current.
- **Serving**: each Pulsar service in the last saved [service
  inventory](#terms), with its nodes, port and age. The saved inventory holds
  no API URL, so the line names **Running services…**, which has the details.
- **Disk**: what each node can still copy in the last saved [storage
  budget](#terms), with its age.
- **Attention**, only when there is something: changes noticed, a node kept as
  only down, a blocking issue the last check found, or an archive location
  setting that cannot be read. It shows two items and counts the rest; the
  last one names where to look.

Every `./pulsar inventory` and `./pulsar model budget` run, including those
from **Running services…**, **Models and disk space…** and **Check the cluster
now**, saves its observation on the controller in
`.pulsar/service-inventory.json` and `.pulsar/storage-budget.json`; every
discovery and cluster check saves `.pulsar/cluster-changes.json`. None is ever
tracked. A record that is missing reads `not observed yet` or `not checked
yet`, never as fine, and a saved file that cannot be read says so and is
replaced by the next check. The ages do not tick while the menu stays open;
the block prints again only when a saved record changed or the inventory
grew older than ten minutes. The Serving line says its age once, at its end:
`seen 30 minutes ago`. With no service running it leads with `none running`,
then names any exited containers that remain. Saves of one record take turns under a
lock file beside it, so an older inventory never replaces a newer one.

Without Gum the menu does not open; `./pulsar` names the commands behind its
choices instead.

### Serve a model…

**Serve a model…** answers with a plan: the steps the chosen model still
needs, which the operator approves once.

1. **The cluster change check.** On entry it runs one discovery (`./pulsar
   topology detect --json`, which saves the changes noticed) unless the saved
   record is for the saved membership and under ten minutes old. With no
   change, or only nodes kept as only down, the list follows at once. A change
   opens [Review what changed…](#cluster) with two more choices: **Serve on
   the cluster as saved**, and, while a Pulsar service runs and refuses the
   save, **Stop the running service, save the change, then continue**. The
   second lists its steps (Stop, save the cluster, record the SSH
   identities), starts on **Cancel**, says that nothing serves from the Stop
   until the chosen model starts, and then opens the list; that model's plan
   copies its files again where needed, taking over copies that still verify.
   A plan whose serving nodes include one that did not answer the last
   discovery says so and does not run.
2. **The list.** Each recipe of each model, told apart by what differs
   (context, node count, an 8-character image digest, or `recipe 1 of 2` when
   nothing differs), with one readiness phrase: `running on …`, `ready to
   start`, `needs copying to … · …`, `needs its files checked`, `needs
   restoring from its archive`, `needs downloading · …` or `needs …; the
   cluster has …` (see the [Serve labels](#catalog-labels)). The phrases come
   from saved records and one service inventory, which the menu reuses while
   it is under a minute old.
3. **The machine.** A one-node recipe asks which machine serves it, starting
   on the one its files are on. A multi-node recipe serves on the saved node
   order, as `./pulsar start` does; serving nodes are never chosen from
   discovery.
4. **The plan.** The previews that can run now, in order: a model files scan
   when a required snapshot has no home, the restore plan, the `hf`
   prerequisites of the node a download goes to (one Hub request; a missing or
   unusable CLI blocks that node and **Change…** offers another, a rejected
   login warns, an unreachable Hub is not checked), the prepare plan once
   every snapshot has a home, the image plan and Start's dry run. Missing
   files come from files found on a node that no record names, then the
   archive, then a download to the first serving node. The steps follow in a
   fixed order: files, copies (Prepare takes over copies that still verify and
   counts them as not copied), the serving image, then **Stop** of whatever is
   in the way, then **Start**, so a failed download or copy leaves the running
   service running. Stopping a service and pulling an image from its registry
   are always their own steps; nothing passes `--replace` or `--pull-image`.
   Steps whose preview cannot run yet (Prepare before a home exists, Start
   after a blocking check) are named as estimates. A memory warning that
   Start's dry run accepted is shown before approval: Start itself refuses on
   it, and no plan passes `--accept-memory-warn`. The screen says what a failure leaves
   and whether the API will require a key. Commands name the spec by its
   complete ID; the outcome shows a catalog spec's 12-character prefix where
   the command accepts one. Choices: **Cancel**
   (where the cursor starts), **Go ahead**, **Get it ready, but do not start**
   (the same plan without Stop and Start), **Change…** (the serving machine,
   the archive or a download, the node that keeps the files, and the serving
   image's source where both are possible: **Pull the serving image from its
   registry instead** when this machine was seen holding it, and **Copy the
   serving image from this machine instead** to go back) and **Details**
   (recipe details, published results, and **Compare with another recipe**,
   which lists the other recipes as the list does and shows `./pulsar spec
   compare` of the two in the pager).
5. **The run.** Each step runs its own preview when its turn comes, then its
   command. A step that is blocked, or would do what the plan did not say,
   stops the run before it changes anything and offers **Plan again from the
   state as it is now**. A failed step says what is done and what still
   serves; after the Stop, a failed Start says nothing is serving and offers
   to start the stopped service again. A Start refused for nothing but its
   memory warning says what the warning was and also offers **Start MODEL
   with less free memory than recommended**, which runs `./pulsar start SPEC
   --accept-memory-warn`; that choice appears on no other outcome and is never
   part of a plan approved beforehand. An interrupted step is inspected before
   any retry: **Check its files now (read-only)** or **Check the service now
   (read-only)**. The outcome lists the commands that ran, in order, under
   `Same as:`.

### Running services…

**Running services…** takes one service inventory on entry (`./pulsar
inventory --json`) and reuses it while it is under a minute old, so no choice
probes twice in a row; **Restart…**, **Switch to another model…** and **Serve
a model…** opened from it use the same one. A Stop or a Start makes it look
again. It does not reuse the inventory saved on disk, since a Stop or a Start
run from another menu or the command line does not change that record. It opens on the
service when there is one, on a list of their names when there are several,
and says when there is none: `No Pulsar service is running` only when every
node answered, otherwise what was seen on the nodes that answered and which
node did not, so what runs there is unknown, not absent (see the [Services
labels](#catalog-labels)).

Each service has one name on every screen, as home's Serving line has it:
`MODEL running on NODES · port PORT`, or `exited containers of MODEL remain on
NODES`, with `· outside the catalog` for a service this checkout does not
publish. A name too long for the line keeps its end. Its card:

```
<model> running on <node-1>, <node-2> · port 8000
  Seen      by the service inventory 2 minutes ago
  Model     <served name>, the model name requests use
  API       http://<address>:8000/v1
  Key       none: the API accepts requests without a key · from its launch record
  Health    not checked here; choose Check it now
```

The API address comes from the inventory the Seen row dates, the way `./pulsar
status` falls back to it: the port the first serving node's container runs
with, at that node's address. When that node is this machine the address is a
loopback address, and the card says it works only from there. Whether a key is
required comes from the launch plan the service's containers name, as the
Connect card reads it; with no plan named it waits for **Check it now**. Health
needs the service itself, so it waits for **Check it now** too, which also
replaces the API and key with what it found, each with its age. GPU
work and containers Pulsar did not start are named under the card, read-only,
and offered nothing. The choices:

| Choice | Does | Same as |
| --- | --- | --- |
| **Check it now** | Asks the service: its output, then the card with its API, key and health. | `./pulsar status SPEC` |
| **How to connect (a first request to copy)** | The Connect card from the last check, checking first when there is none. | `./pulsar status SPEC` |
| **Logs** | Its containers' output in the pager. | `./pulsar logs SPEC` |
| **Restart…** | Stop then Start of the same recipe on the same nodes, as a two-step plan that starts on **Cancel** and runs as **Serve a model…**'s plans do. Not offered for a modified recipe, or when Start would use other nodes; the card says why. | `./pulsar stop SPEC`, then `./pulsar start SPEC` |
| **Switch to another model…** | The Serve list; the plan it builds stops this service directly before Start, even when the new recipe uses other nodes. | the plan's commands |
| **Stop…** | Today's question, then Stop; model files, pins and archives are kept. | `./pulsar stop SPEC` |
| **Stop every service…** | Today's question naming each service it removes, then Stop. | `./pulsar stop --all --yes` |

A service outside the catalog offers **Logs** and **Stop…** only, and its
question adds `This service was started outside this catalog.` Stop and
Restart are left out while the inventory has not established that Pulsar can
stop the service; the card says why, naming a node that did not answer:
`Stop not offered while <node-3> does not answer, because Pulsar cannot see
everything the service runs`.
None of these choices passes `--replace`, `--pull-image` or
`--accept-memory-warn`. With nothing running it offers **Serve a model…**,
**Look again** and **Back**.

### Models and disk space…

**Models and disk space…** reads the model files (`./pulsar model files
--json`) and the storage budget (`./pulsar model budget --json`) once on entry,
as the Model files page does, and reuses them on every screen below until a
plan runs or **Look again (checks nodes now)** is chosen. Its page:

```
Models and disk space
Checked   2 minutes ago · 3 nodes

Room on each node
  <node-1>  1.44 TiB free · 10.0 GiB past its reserve
  <node-2>  1.99 TiB free · room for 1.80 TiB of copies
  <node-3>  not checked, so its files and room are unknown

Models on the cluster
  <model>
    212 GiB · home on <node-1> (also its copy there) · copies on <node-2>
      ready to start · kept · archived, verified 4 days ago
```

A model's recipes are told apart by what differs, as the Serve list does. Its
facts come from the scan and the catalog's saved records: its size, where its
*home* is (the copy Pulsar downloaded or restored and keeps) and its *copies*
(what each serving node loads), Serve's readiness phrase, whether it is kept,
and its archive state (see the [Models labels](#catalog-labels)). Files no
catalog recipe uses are named read-only. A node that did not answer is unknown,
not absent, and since Purge, Remove home, Move home and Pin need every node,
nothing that changes files is offered while one does not answer. Without an
archive location the page says that **Cluster… → Settings…** sets it instead of
offering **Back up a model…** and **Check the archives…**.

| Choice | Does | Same as |
| --- | --- | --- |
| **Free up space…** | What can go, largest gain first, each with what it frees and what it costs to get back; then what cannot go and why. Each line opens its plan. | `./pulsar model purge SPEC`, `model remove SPEC`, `model move SPEC --node NODE` |
| **Get a model ready ahead of time…** | **Serve a model…**'s change check, list and plan, composed without Stop and Start: the files, the copies and the serving image. | `./pulsar model acquire`/`restore`/`prepare`, `image stage` |
| **Keep a model ready…** | **Keep MODEL ready** marks its copies as kept so **Free up space…** never offers them; **Stop keeping MODEL ready** lifts it. | `./pulsar model pin SPEC`, `./pulsar model unpin SPEC` |
| **Back up a model…** | One plan with a step for each required set of the model's files that has no verified archive; an archive already there is verified, never replaced. | `./pulsar model archive create SPEC --snapshot NAME` |
| **Check the archives…** | One plan with a step for each archived set of the model's files, read and compared with their expected hashes; nothing is changed. | `./pulsar model archive verify SPEC --snapshot NAME` |
| **Look again (checks nodes now)** | A new scan and budget. | `./pulsar model files`, `./pulsar model budget` |

**Free up space…** lists outcomes, not operations:

- `Remove MODEL from NODES: frees SIZE` deletes the copies only that recipe
  uses (Purge). Its home stays, so serving it again copies from there with no
  download. The size is the Model files page's estimate: copies another recipe
  shares, and the home used as a copy, free nothing. Unfinished copies the
  recipe recorded are cleared with them.
- `Remove MODEL from the cluster: frees SIZE` deletes the copies of every
  recipe that uses those files, then each home (Purge, then Remove home). It
  says whether a verified archive brings the model back without the internet;
  without one it says a download would be needed, and its plan offers **Back it
  up first, then remove it**, which adds the archive steps before the others.
  It is offered only when every recipe that uses the files can be purged and
  needs no other files.
- `Move the files of MODEL from NODE to NODE: frees SIZE on NODE` is offered
  only when a node is past its storage budget's reserve, another node's homes
  folder has room for the files after its reserve, and nothing depends on the
  home: Move home refuses a home with copies. The move is a named step the
  operator approves; no plan moves a home on its own.
- `Clear N unfinished copies of MODEL on NODES: frees an unknown amount`, for a
  recipe whose only records are copies that never finished. The scan gives no
  size for them.

Under them, what cannot go, with its reason and the choice that lifts it: a
serving model (stop it through **Running services…**), a kept model (**Stop
keeping MODEL ready**, its own plan), copies another recipe shares (their files
stay), a container Pulsar did not start that mounts the files (named,
read-only), unfinished downloads no record names, and files no catalog recipe
uses. Nothing is ever unpinned on its own, no archive is deleted, and no file
Pulsar has no record of is deleted.

Every plan has the form **Serve a model…**'s has: the steps and what each
changes, a sentence on what stays and what a failure leaves, **Cancel** where
the cursor starts, then **Go ahead**. The previews that can run now run
before the screen; Remove home's waits for the Purge before it and is named
as an estimate. At its turn each step runs its own `--plan --json` again and
stops the run before it changes anything when it is blocked or would do more
than the plan said, for example delete copies on another node. The outcome
names the commands that ran under `Same as:`, and the page then reads the
files again.

Without Gum the page does not open; it names the commands behind its choices.

### Before cluster setup

Before setup, home prints the setup status and offers **Set up this cluster…**
(`./pulsar topology setup`; see [Confirm membership and SSH
trust](#confirm-membership-and-ssh-trust)), or **Start over: forget this
cluster…** (`./pulsar topology forget`) when saved membership is invalid, then
**Browse the catalog (read-only)**, **Help and troubleshooting…** and **Exit**.
An unset archive location is optional configuration, not an incomplete cluster
setup.

**Browse the catalog (read-only)** lets you select a published spec, inspect
its summary, details and published results, or compare it with another
catalog spec, then return with **Back** or Esc. It offers no storage or
serving operations. The same catalog browser is available directly as
`./pulsar models menu --read-only`. Command-level prerequisites and
confirmations still apply to all operational actions after setup.

### Cluster…

**Cluster…** opens on a frame from saved records only: the nodes, how many
answered the last check and when, whether the links and SSH identities were
verified, and the changes noticed with their age, then the blocking issues the
last check found. Opening it probes nothing. The frame is drawn again only
when a saved record changed. Its choices:

- **Check the cluster now** runs one read-only report, each source under its
  own spinner: discovery (`./pulsar topology detect`, which saves the changes
  noticed), the membership check (`./pulsar topology check`), the SSH identity
  check (`./pulsar ssh-trust check`), diagnostics with every node's
  prerequisites (`./pulsar doctor`), the service inventory (`./pulsar
  inventory`) and the storage budget (`./pulsar model budget`, last, because
  it takes the shared storage lock). The report shows the changes noticed, then
  every saved node by hostname: its status, SSH identity, its checks (each
  warning and blocking issue with its fix; `hf` findings are warnings),
  memory, disk and services, then the archive and API key settings, the
  sources that could not be observed, and a verdict. A long report opens in
  the pager. Its findings are saved beside the changes noticed, so home and
  this frame show them later. Under the report: **Review what changed…** when
  discovery noticed a change, **Show the report again**, **Check again** and
  **Back**. It replaces the earlier home menu's **Cluster overview**.
- **Review what changed…**, shown when the saved changes hold a change, shows
  saved membership against what the last discovery saw, printed above its
  choices when both fit the terminal and in the pager when they do not, then
  one question:
  - every member answered: **Save the cluster as it is now** (`./pulsar
    topology configure`);
  - a member did not answer: **Keep NODE; it is only down**, which records
    the answer beside the changes so the question is asked again only when a
    later discovery sees different changes (the node stays under Attention),
    or **Save the cluster without NODE** (`./pulsar topology configure
    --remove-missing`), whose question lists the files recorded on that node
    first;
  - a member that answered but cannot be a member is never left out: fix it
    on that machine;
  - while the saved inventory shows a Pulsar service, saving is refused with
    the service named and how to stop it (`./pulsar stop SPEC`).

  When a save would change the nodes' positions, as when a reinstalled machine
  joins after the saved nodes, the screen shows each node's old and new
  position: multi-node recipes use the first nodes in that order. Configure
  asks its own question; this menu's choice leads there. After a save, Pulsar
  records the nodes' SSH identities with their own question and fingerprints
  (`./pulsar ssh-trust enroll`), and says that Prepare takes over each copy
  that still verifies, with nothing copied again.
- **Settings… (archive location, API key)**: **Archive location…** opens the
  archive menu (`./pulsar configure archive-root menu`); **API key** says
  whether `VLLM_API_KEY` (or `API_KEY`) is configured and where: this
  checkout's `.env` file or the environment that started Pulsar. The menu
  never reads out or takes the key.
- **Start over: forget this cluster…** runs `./pulsar topology forget`: its
  plan of what is removed and kept, then a question that starts on No. After a
  forget, the menu returns home, which offers setup again.

### Help and troubleshooting…

**Help and troubleshooting…**, before and after setup, offers:

| Choice | Same as |
| --- | --- |
| **Why will a model not start?**, after setup only: see below | `./pulsar start SPEC --dry-run` |
| **Run diagnostics (checks nodes now)** | `./pulsar doctor` |
| **What the words mean**: the [terms](#terms) above | `./pulsar help terms` |
| **What the labels mean**: the [catalog labels](#catalog-labels) | `./pulsar help labels` |
| **How serving works**: one sentence for each step from Download to Purge | `./pulsar help workflow` |
| **Command reference** | `./pulsar help` |
| **Advanced: single operations…** | today's menus, below |

Each page opens in the pager; closing it returns here, and **Back** or Esc
returns home.

**Why will a model not start?** lists the recipes as **Serve a model…** does,
from saved records only, and asks a one-node recipe's machine. It then runs
Start's checks without starting (`./pulsar start SPEC --dry-run --json`) and
shows each check: passed, failed with what it found, or not run because an
earlier blocker ended the checks (see the [Why labels](#catalog-labels)). Each
blocker is said in plain words with its fix, and each fix is a choice that
names an intent, not a single operation: **Serve a model…** (its plan gets the
files, the copies and the serving image, and stops a Pulsar service in the
way), **Running services…**, **Check the cluster now** or **Run diagnostics**.
**Check again** runs the checks once more. It starts nothing and passes no
flag a plan would need authority for. **Advanced: single operations…** keeps the menus from before
this layout, unchanged: **Catalog and storage** (`./pulsar models menu`),
**Model files and disk space (checks nodes now)** (`./pulsar models files`),
**Live service inventory** (`./pulsar inventory menu`), **Cluster topology**
(`./pulsar topology menu`) and **Archive storage configuration**
(`./pulsar configure archive-root menu`). Before setup it offers only **Cluster
topology (read-only)** (`./pulsar topology show`).

### Catalog operations and confirmations

The catalog menu (`./pulsar`, then **Catalog and storage** under **Help and
troubleshooting… → Advanced: single operations…**, or `./pulsar models menu`) stays open
until **Back** or **Exit**; each operation returns to the same recipe with its
result and any recovery guidance. It lists operations in lifecycle order and keeps storage
maintenance under **Storage and archive**. Where the terminal is wide enough,
each choice adds what it does, such as **Prepare — copy files to every serving
node** or **Pin prepared copies — protect from purge**. **Verify running
service**, after **Live status**, opens `observe` for the spec in the pager: it
rechecks the running service's recipe, container configuration and prepared
files on every rank. **Service logs** opens `logs` for the spec. **Storage and
archive → Full file audit (slow; hashes every file)** opens `model info --full`,
which hashes every prepared file and can take many minutes. All three only read,
so they ask nothing; a one-node recipe first asks which machine. An operation is left out only when
saved records rule it out, and a **Not shown** line names it, the reason and
what makes it available: a location set in **Cluster… → Settings…** on
the home menu, or a home that **Download** or **Restore** records. A serving
guard points to a guarded trial (`./pulsar guarded --help` and
[guarded serving](GUARDED_SERVING.md)); a historical spec has no such fix. **Check now** records file
observations only, so it never changes what is shown.
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
node), while the menu asks for a node. Its question says what the machine is
for, such as "Which machine should hold the model files?", and each machine
is named by hostname with what saved records place there: "home recorded
here", "prepared copy recorded here", or "same files prepared here for
<spec>" where another spec's prepared copy of the same snapshot is recorded.
Nothing is probed to build the list.
For a one-node recipe the cursor starts on the machine saved records name
(the home, else a prepared copy); **Download**, **Restore** and **Move home**
start on no machine, because choosing where the files go is the decision.
**Live status** asks no machine question: status uses the service's recorded
node. **Stop** takes its service inventory first and asks only when that
inventory did not see the one-node service on exactly one confirmed machine,
because Stop without a node removes the service's containers on every node.
The catalog list groups the specs by model, in catalog order: the model's
name once, in bold, then a line for each spec that starts with its 12-character
prefix, and a blank line after each model. A withdrawn spec stays at the end,
under a second heading of its model. A spec's line names, in this order, any
warning (`withdrawn`, `start unsupported`), the facts that tell recipes of one
model apart, the spec's node count, a service seen in this menu session, the
same short saved state as `models list` and the spec's state. Another spec's
prepared copies of the same files are named in the recipe instead, with the
nodes that hold them. The node count is left out only where a fact or the
service's words give the same count: a 3-node spec seen on all its nodes reads
`running on 3 nodes`, and one seen on a single node reads
`3 nodes · running on 1 node`. State is metadata, never a gate. No part is
cut: a part that does not fit on the line starts the next one, two columns
further in. With the cursor on a model's
first spec, the model name belongs to that choice and is selected with it.
When the catalog has several recipes of one model, each of their lines names,
after the spec prefix, the first recorded fact that differs: the node count
(`2 nodes`), the context (`context 1M` for `--max-model-len 1048576`, `context
500K`), `max sequences`, `quantization`, or else a short image digest; where
that leaves recipes alike, the next fact that differs is added. No fact is ever
cut. Only a model name longer than its line is cut: without its organization
where no other model reads the same, then in the middle so its end stays. The
Start question names the same fact after the model,
such as "Start deepseek-ai/DeepSeek-V4.1-Flash-1017 (context 1M) on …?", except
a node count, since it already names the nodes. Any other model name that does
not fit is cut the same way. Storage mutations run the
operation's own `--plan` preview and show a one-to-three-line summary of it:
what changes, where, how much, and the headroom left, or that nothing would be
copied, deleted or changed. A question then names the model, nodes and
consequence, and says so when nothing would change. Choose **Yes**, **Show full
preview** or **No, change nothing**; No is selected first, and Esc also changes
nothing. **Show full preview** opens the complete rendered plan, with exact
byte counts, in the pager, then returns to the question. A blocked plan names
its first blockers with their fixes and ends without a question; it offers
**Show full preview**, **Stop** and **Open Live service inventory** when a
Pulsar service's container blocks it, and **Back**. **Back**
or Esc leaves the current menu or cancels an action prompt to its containing
menu. Cancelling a launch or storage sub-action keeps its submenu open. Archive
configuration also stays open after a saved or declined change until **Back**.
Long read-only output opens in a pager when it runs past a few lines: **Show
details**, **Published results**, a comparison, a full storage preview,
the **Help and troubleshooting…** pages, **Run diagnostics**, **Host diagnostics**, **Detailed status**, the full inventory, and
saved membership or its check.
↑/↓ scroll, `/` searches, and q or Esc returns to the menu; shorter output
prints in place. Ctrl-C at a choose, input, confirmation, **Continue** or pager
prompt exits the menu with status `130`, including membership and SSH
enrollment confirmations.

Opening the catalog probes no node. The spec list always ends with **What the
labels mean**, which opens the [catalog labels](#catalog-labels) and also
appears in read-only browsing. **Check running services (service
inventory)** in the spec list runs `inventory --json` once and keeps the
result while the catalog menu is open; like every inventory run, it is also
saved for the home menu's Serving line. Leaving it for the root menu starts
over. The list then says when it was observed and which nodes were not, and
each spec's line names its service, such as `running on 3 nodes`. The recipe frame's **Running?** field gives the state, nodes, source
and age; **Live status**, **Start** and **Stop** in the same catalog menu
update it, and the newest observation wins. Live status exit 0 means a service exists, not
that it is running. A node that was not observed, or a failed node probe,
makes the state unknown, never "not running". Stop for a one-node recipe acts
on the selected node only, so other nodes read as not observed since. A
request rejected before it ran (exit 2) keeps the earlier observation. After 10 minutes an observation reads "last seen", never
as current. A spec with a service seen in the session suggests **Live
status**; while its service state is unknown, or not observed since a Start
or Stop, Live status is suggested before Start. Before setup, the read-only
catalog does not offer the check.

After a successful **Start** or **Live status** in the catalog menu, and
after inventory's **Detailed status**, a **Connect** card follows the
command's output when the service is running. It shows the API URL with
`/v1`, the served model name, and whether the service was started with an API
key, read from its recorded launch plan ("not recorded" when Live status saw
the service only in the inventory). It ends with a copyable request like the
ones in [Send your first API request](#send-your-first-api-request), with the
URL and model filled in. The key is never printed: the request reads it from
the client's `VLLM_API_KEY`. A loopback URL works only on the API node, and
the card says so.

### Operation outcomes

Each operation ends with an outcome worded by its
[exit status](CONTRACT.md#error-codes-and-exit-statuses), then an indented line
with the status, the time taken and where to look. The outcome stays on screen
until **Continue** (Esc also continues), so the command's output can be read
before the menu redraws. A preview that fails waits the same way; a blocked
plan waits at its own choices, and a declined confirmation returns at once.
`✓` is success. A "no"
(exit 1) reads by action: after **Live status**, `▲ … is not running` means no
node runs it, so starting it won't conflict; a refused Start, a failed check or
a blocked launch check is marked `✗` and its reasons are in the output above.
`?` marks an outcome Stack could not establish (exit 3). After Live status it
means a node could not be observed, so do not start the model yet; after an
action it names the inspection to run before retrying. A request rejected before
it ran (exit 2) changed nothing. A refused `--plan` preview says the operation
is not possible now. In the catalog menu, a command ended by a signal
(128+signal) is reported as an interruption, as described next.

Every outcome ends with the command that does the same, such as `Same as:
./pulsar model restore 139908cf23bb --node spark-1 --yes`, so it can be
repeated or scripted. It is the command the menu ran, in its operator form:
the spec as a 12-character prefix where the command accepts one, and nodes by
hostname. `--yes` and `--accept-memory-warn` appear only when the menu passed
them; a failed preview names its `--plan` command.

### Interruptions and recorded checks

During a catalog operation, Ctrl-C requests interruption from the existing
command; the menu resumes when that command exits. The menu does not infer
confirmed cleanup, absence of a service, or unchanged files from the exit code.
It labels the interruption even when a concurrent command completion returned
zero, retains the command's output, and gives an explicit next inspection:
**Check now** for storage, **Live status** for Start/Stop, or **Check serving image**
for image staging. These are suggestions, not automatic actions. Existing
backend cleanup and retry behavior continues to own retained staging and service
resources. Other command menus retain their command's interrupt exit status.

A nonzero **Check now** that saved an observation is reported as recorded
findings, rather than an execution failure. Missing or changed files can be a
completed check's result; an interrupted or unrecorded check still needs a retry.

A recent check that found missing or changed files suggests acquisition,
restoration or preparation even when it recorded blockers. A check that found
the files prepared, with no blocker, suggests **Start** at any age, because
Start rechecks the files, image, nodes and memory before launching; the Files
line shows how old that check is. Unknown file observations, and missing,
changed or blocked ones more than a day old, still suggest **Check now**,
except where this session's Model files scan found the home folder of a
snapshot with no recorded home: that suggests **Download** with or without a
check (see [Restore after controller loss](#restore-after-controller-loss)). A
check that records a result clears the previous operation's pending check; one
that could not record a result does not turn earlier readiness into a new
Start suggestion.

Operations that verify what they record move straight to the next step. After
a successful **Download** or **Restore**, which verify the files before
recording the home (a Download that reuses a recorded home checks it as Check
now does), the suggestion is the next required snapshot without a home, then
**Prepare** once every snapshot has one. After a successful
**Prepare**, which ends by verifying the prepared copy on every serving rank,
the suggestion is **Start**, which rechecks its prerequisites. Prepare saves no
observation, so the frame's Files field reads `prepared in this session (…);
saved check: …` until the next Check now. Other storage changes still suggest
**Check now**, and so does any storage change that fails, since it may have
partial effects; one rejected before it ran (exit 2) changes nothing. Each
operation keeps its own preview and confirmation.

### Launch options

**Launch options** provides shortcuts for the selected published catalog spec:

- **Check launch prerequisites** runs the existing `start --dry-run` checks.
  It does not launch or stage images. Its output identifies checks, such as the
  multi-node preflight, that will run only during an actual Start. After it,
  and after **Start**, one line sums up the checks: `✓` passed, `✗` failed,
  `•` shown but not blocking, and `–` not run because an earlier blocker ended
  the checks. `start --json` reports the same list as `checks`.
- **Check serving image** runs `image check` on the selected serving nodes.
- **Stage serving image** previews `image stage` before confirmation. Choose
  between pulling the exact catalog digest from its registry and copying the
  serving image from this node. Copying never silently falls back to a pull.
  Staging does not start or replace a service or select a different image.

**Start** continues to run its existing checks. If it refuses the start only
because of a memory warning, the menu offers one separately confirmed retry
with `--accept-memory-warn`, for the same spec and node selection. Insufficient
memory and other blockers still prevent start. This acknowledgement is not
remembered for later starts and does not authorize image pulls or replacement.

For any other refusal or failure, and when **Check launch prerequisites**
finds a blocker, the menu offers a choice for each menu operation that
addresses a recorded blocker code. The choices are built from the recorded
codes, not the printed text. A choice is offered once, however many blockers
lead to it:

| Blocker | Choice |
| --- | --- |
| `topology_incomplete` | Open Cluster topology, where membership can be set up or changed |
| `fabric_incomplete`, `node_unreachable` | Check saved nodes and fabric |
| `docker_unavailable`, `container_start_failed` | Host diagnostics |
| `image_missing` | Stage serving image |
| `model_files_not_ready` | Download, Restore or Prepare, as the saved records indicate |
| `memory_insufficient`, `port_in_use` | Stop, for each Pulsar-managed service on the serving nodes, named with its nodes, port and GPU memory from one service inventory the menu takes first; then Open Live service inventory |
| `service_exists` | Live status; Stop |
| `smoke_test_failed`, `health_timeout`, `container_exited` | Live status; Service logs; Stop |
| A failed launch whose containers may remain (`cleanup` is `unconfirmed`) | Live status; Stop, added to the choices above |
| `image_check_failed`, `model_files_check_failed`, `memory_check_failed`, `preflight_failed` | Rerun the checks with full diagnostics (`start --dry-run --verbose`) |

Start cannot say what holds a busy port or the memory, so for
`port_in_use` and `memory_insufficient` the menu takes one service inventory
before the choices (Ctrl-C there leaves the menu). It offers **Stop** for
each Pulsar-managed service with a container on the refused spec's serving
nodes: the selected node of a one-node spec, or the first saved nodes, which
ordinary start uses. A choice names the catalog model, or the served name of a
service started outside this catalog, then its nodes, port and the most GPU
memory one of its containers uses, such as `Stop org/model on spark-1,
spark-2 (port 8000 · 89 GiB per node)`; a service on the busy port comes
first. Where a choice does not fit, its nodes become a count and the model
name is cut from its start, keeping its end, before the port and memory give
way. That Stop takes the same refresh and question as any Stop and removes
the service's containers on every node; for a service outside the catalog,
the question says so. The refused spec's own service is `service_exists`,
with its own choices. Containers Pulsar does not manage, managed services the
inventory could not establish as safe to stop, and GPU processes Pulsar did
not start are named above the choices, read-only, with no choice. When the
inventory cannot be taken or read, the choices are the ones in the table.

Each choice runs the existing operation with its own selections, preview and
confirmation, and none adds `--replace` or `--pull-image`. A failed
multi-node launch whose containers were all seen removed offers no Live
status, Service logs or Stop. The inventory, Cluster topology, topology check,
host diagnostics, Service logs and a declined confirmation return to the same
choices. The diagnostics rerun replaces them with its own. After a finished
Stop, Stage serving image, Download, Restore or Prepare, and after the
inventory menu, the choices return with **Start again** first (**Start now**
after **Check launch prerequisites**); after a Stop or the inventory menu,
the menu takes the service inventory again. Start again asks Start's own
question for the same spec, node and snapshot, so Stop and Start keep
separate confirmations. Live status, a failed or interrupted operation, and
Back return to the recipe after the outcome. An interrupted Start offers no
choices; its outcome names the next inspection. `memory_warning` keeps its own
retry above. `guard_unsupported`, `historical_spec` and `service_stopped` have
no menu choice; their printed note gives the supported path.

These menu choices operate on catalog specs and the services in their way;
they do not author or override recipes. Historical schema-1 specs do not offer
the new launch shortcuts.

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
preparation can bind the new recipe to that prepared copy. Its existing path,
files and verification stamp are reused; recipe, rank and topology bindings
remain distinct. Earlier recipes keep their bindings and pins. The home rank
still uses its registered home directly. Only nodes missing suitable content
need a transfer, and the complete required snapshot set must be ready.
An owned incomplete transfer resumes its staging rather than silently discarding
it to create a shared binding.
`prepare --plan` reports which ranks will reuse a binding, bind existing files,
or copy files. Before Prepare runs, the recipe's Files field says from saved
records which nodes hold other specs' prepared copies of every required
snapshot: `the same files are prepared for <spec> on <hosts>; Prepare binds
them there without copying`. The named spec is a
catalog member where one holds the files; others may be outside the catalog,
such as experiment specs started with `--spec-file`, and are only counted.
A recipe change still requires preparation of its own bindings.

The full preparation preview also shows the unique snapshot payload and, for each node,
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

The **Model files and disk space** page shows the `./pulsar model budget`
inspection for every confirmed node under the nodes. It shows one row
per node in human units: disk free, the reserve, Pulsar files, and what the
node can still copy, which is the smaller of disk free after the reserve and the
copy budget left. A node past either limit is named with the amount. Narrow
terminals drop the reserve column first. The command uses the same reserve and
allowance settings as preparation, requires no confirmation, and changes no
spec's saved file-check state. It changes nothing on a node; each complete run
saves the budget on the controller, stamped with the time it finished, for the
home menu's Disk line. This budget describes prepared-copy storage;
home and archive locations may use other filesystems. The page's payload
estimates for [model files](#model-files-across-the-nodes) are separate from
this prepared-copy budget.

### Verification reuse and full audits

Routine acquisition of registered copies, preparation re-entry, inspection and
serving observation reuse earlier verification while the complete file set,
directory identity and file metadata still match. A new invocation does not
invalidate that proof. Each new prepared copy is fully hashed before accepting
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

On a terminal, `./pulsar help` and its pages style their text: headings in
bold and the menu accent, menu choices in bold, [terms](#terms) in the accent,
commands, options and paths in cyan, and placeholders such as `SPEC`
underlined. Each command's `--help` styles its usage lines and option names
the same way and leaves descriptions plain; help that Python's argparse
formats, for `contract`, `resources` and contributor subcommands such as
`evidence verify`, stays plain. Check results mark what needs attention:
`warn` in yellow, `FAIL` and `NOT READY` in bold red, `READY` in bold green;
`ok` stays plain. `./pulsar doctor` groups its checks under this node, cluster
membership, each other node by hostname and the catalog, then counts warnings
and blocking issues in its verdict; its `--json` output keeps its checks
without that grouping. Each node's section ends with its
[prerequisites](#installation-prerequisites). Every
styled part also reads without color. `NO_COLOR` removes the colors and keeps
bold and underline; `PULSAR_COLOR=never` prints no styles;
`PULSAR_COLOR=always` keeps them in output that is not a terminal. The menu's
pager runs its pages with `always`, so they keep their styles there. Without
`always`, piped output stays plain; `--json` output never carries styles.
Menus set `PULSAR_COLOR=always` for the commands they run, unless it is
`never`, so the home status, catalog views, previews, inventory, topology
reports and outcomes keep their styles; the recipe frame's text stays plain
because Gum draws it, and in the catalog list only the model names are bold.
`error:` and `warning:` labels are styled only on a terminal.

Long storage operations report each phase on stderr, such as
`[acquire 3/4] verifying SHA-256 of every downloaded file`. Previews stay quiet
and stdout, including `--json` results, is unchanged.

While a started service loads, Start says how many health checks it allows
and how far apart they are. On a terminal, one stderr line then updates after
each check, for example `loading: 2:14 elapsed · health check 14 of 90 ·
container running`. For a multi-node service it shows each rank's container
state, or `all 3 ranks running` when they agree. Each check also waits for its
probes, so the limit is a number of checks, not a time. The line is cleared
before any later message. Captured or piped output gets the same text as a
plain line about once a minute, and `start --json` results are unchanged.

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
the same snapshot choice. Each choice says what the snapshot is for (the served
model or the speculative-decoding checkpoint) and where its home is recorded,
with the model ID and commit where the row has room; snapshots without a home
come first, then the target. No command silently selects only the target for these
operations. Archive creation, acquisition, and restoration remain individual
operations; sequence them as required. The menu suggests the next snapshot
without a home after each successful download or restore. Verification without `--snapshot` covers
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
