# Model-free GPU diagnostics

`pulsar diagnostic` runs approved Python diagnostics on confirmed GB10 nodes
without a serving spec or prepared model snapshot. It separates this evidence
from model serving and qualification. The initial implementation supports one
SM121 GPU per node and requires the complete confirmed membership; it never
silently selects a smaller cluster.

## Review and execute

Create a request and a directory containing its Python payload files. Validate
them without Docker, SSH or hardware access:

```sh
./pulsar diagnostic validate --request request.json --payload-dir payload --json
```

The response contains `request_id`, the SHA-256 of the canonical request,
including all payload hashes and resource limits. After reviewing that exact
request and separately authorizing physical execution, use that returned hash:

```sh
./pulsar diagnostic run --request request.json --payload-dir payload \
  --request-id "$REQUEST_ID" --output-dir "$NEW_EVIDENCE_DIR" --yes --json
```

The output directory must not exist; its parent must exist. `--yes` does not
authorize image pulls, image transfer, service replacement, model acquisition
or publication. All nodes must already contain the exact image. Image staging
uses the separate command below. These commands do not change catalog state,
review metadata, serving recipes or the configured Workbench executable.

## Stage the exact diagnostic image

After validation, preview confirmed-node image presence, host idleness/memory,
and image storage capacity. This contacts the nodes but does not transfer images:

```sh
./pulsar diagnostic stage-image --request request.json --payload-dir payload \
  --request-id "$REQUEST_ID" --output-dir "$NEW_PREVIEW_DIR" --plan --json
```

The preview reports the exact source image ID/size, missing ranks, required
space, and blockers. `ready: false` is a completed preview, not permission to
continue. Unknown observations fail rather than becoming missing images.
The controller must already hold the exact ARM64 Linux image. There is no pull
mode or registry fallback.

After separate authority for the transfer, repeat the same inputs with a new
evidence directory and `--yes` instead of `--plan`:

```sh
./pulsar diagnostic stage-image --request request.json --payload-dir payload \
  --request-id "$REQUEST_ID" --output-dir "$NEW_STAGING_DIR" --yes --json
```

Preview and apply cannot be combined. This reuses the existing Stack image-sync
boundary, confirmed SSH helpers and Docker save/load stream. It does not require
a model spec, change model files, launch a GPU workload, replace a container or
publish the image. Spec-oriented `sync-image.sh` behavior remains supported.

Only missing ranks receive the stream. Membership, image presence, idleness,
memory and storage are refreshed before each transfer; a new missing rank or
changed source size stops the action. Already-present images are skipped.
Transfers are sequential, with a 30-minute timeout per sender/receiver pair and
a short termination grace period. This bounds transfer clients, not a Docker
daemon's import after it has received data. A disconnected import may still
finish; failed or cancelled invocations therefore require fresh observation.

Space preflight supports `overlay2` and containerd's `overlayfs` image store.
The [containerd image store uses a separate storage location](https://docs.docker.com/engine/storage/containerd/),
so checking only `DockerRootDir` would be insufficient. Stack matches Docker's
containerd endpoint to one running daemon and uses its read-only `config dump`,
with explicit root/address flags applied. The binary, configuration and import
files/directories must predate daemon startup and remain unchanged during the
observation. Ambiguous daemons, changed configuration and unknown layouts stop
staging. This requires Python 3.11+ for TOML parsing and does not use sudo or the
protected containerd RPC socket.

Capacity is checked on the configured Docker/containerd filesystem roots, explicit
snapshot/temp roots and separately mounted content/snapshot stores. Standard
managed subdirectories are measured at their configured filesystem root without
reading their protected contents. Relocated stores must be declared in daemon
configuration or mounted explicitly; out-of-band plugin-directory symlink
relocation is outside this supported layout. Every measured filesystem on a
missing receiver needs twice the reported image size plus 1 GiB available.
This is a conservative preflight estimate; external disk consumers and quotas
can still affect the actual import.

A loader's zero exit status is not sufficient: every rank must expose the exact
image ID, architecture and size afterward. Failed streams remain failures even
if later inspection sees the image; there is no automatic retry or pull. Raw
before/after observations, per-transfer starts, status, stdout and stderr remain
in the fresh private evidence directory. Partial imports are not pruned or
rolled back because their layers may be shared. Staging success does not start
or qualify the prepared diagnostic.

Image presence includes untagged imports (`docker image ls --all`), followed by
exact-ID inspection. Copying by ID need not preserve repository tags or digest
references; neither is required for this local diagnostic identity.

## Request schema 1

The request is a closed JSON object with these fields. Unknown fields and
mutable image tags are rejected.

| Field | Value |
| --- | --- |
| `schema_version` | Integer `1` |
| `kind` | `pulsar-diagnostic-request` |
| `topology_id` | Exact confirmed topology identity obtained through `pulsar topology show --json` |
| `image_id` | Exact local Docker image ID, `sha256:` plus 64 lowercase hexadecimal characters |
| `geometry` | Object with `nodes`, `tp`, `pp`; integers 1–8, `nodes == tp * pp` |
| `limits.memory_bytes` | Integer 1–64 GiB; both container RAM limit and Torch allocator cap |
| `limits.min_host_available_bytes` | Integer 64–128 GiB; available host RAM floor |
| `limits.timeout_seconds` | Integer 10–1800; maximum container diagnostic duration |
| `rendezvous_port` | Explicit unused port, integer 1024–65535 |
| `files` | Mapping of flat Python filenames to their exact SHA-256 hashes; 1–32 files, at most 4 MiB total |
| `entrypoint` | One filename in `files` |

`limits` and `geometry` also reject extra fields. Booleans are not integers.
Payload files must be regular files, not symlinks; the entrypoint and its helpers
are frozen once and reverified on each node. Calculate file hashes from the
actual bytes; formatting changes produce a different request ID.

Use the exact ID returned by Docker; with its containerd image store, that ID can
identify an OCI index rather than the image configuration. ID presence alone
does not establish a repository/digest reference. Resolve it when staging an
image and require the same ID on every node.
The runner uses `--pull never` and checks ARM64 image identity before any rank
starts a container. The payload must use the runtime versions in that image;
Stack does not install Python dependencies during a diagnostic.

## Payload contract

The entrypoint runs after guard setup and actual Docker configuration readback.
It can import helper files from its own payload directory. It receives:

- `PULSAR_DIAGNOSTIC_CONTEXT`: read-only JSON containing the request, invocation
  identity, rank and confirmed rank mapping. Treat this file as private.
- `PULSAR_DIAGNOSTIC_RESULT`: a fresh container-local path for one JSON object.
- `RANK`, `WORLD_SIZE`, `LOCAL_RANK=0`, `MASTER_ADDR`, `MASTER_PORT`, and explicit
  confirmed NCCL/Gloo fabric settings.

The entrypoint must exit zero **and** write `{"successful": true, ...}` after its
own acceptance criteria pass. A missing, stale, oversized or unsuccessful result
fails the rank. Result JSON is limited to 64 KiB. The guard retains a 64 KiB log
tail; the driver allows for JSON escaping when bounding the complete report.
Stack verifies lifecycle and complete rank results; the payload owns its
numerical assertions. A successful diagnostic never means model qualification.

The child verifies exactly one SM121 GPU and applies the Torch allocator cap
before importing the entrypoint. Native allocations outside Torch are bounded
by the container/host guards, not by Torch's allocator setting. Payload code is
reviewed executable input, not an untrusted-code sandbox.

## Placement, limits and cleanup

Bash reuses Stack's confirmed topology, fabric selection, enrolled SSH command
builder and lifecycle lock. No SSH endpoint is accepted from the diagnostic
request. Model bytes and credentials are not mounted. Read-only mounts contain
the frozen payload/runner code and host memory information. Multi-node traffic
explicitly selects `NCCL_NET=IB` with the confirmed HCAs, preventing silent
fallback to the control LAN. The rendezvous port must be reserved by the operator;
the runner does not change it to work around a conflict.

Each selected HCA is resolved through host sysfs to its actual `uverbs` character
device. Preflight requires complete device coverage; the container receives
`rdma_cm` and those selected devices, and configuration readback verifies the
complete set before GPU release. The platform's single default verbs device is
insufficient for a multi-adapter job. Unselected adapters are not exposed.

All ranks pass image/idleness/memory preflight before the execution batch begins.
Each node rechecks prerequisites, creates one invocation-labeled container,
verifies its settings, then releases its GPU child. Partial startup or any failed
rank cancels peers. An occupied GPU or diagnostic container is refused, never
replaced. The lifecycle lock coordinates Stack operations from this installation;
it does not prevent unrelated external programs from racing with a preflight.

Containers use four CPU cores, serial compiler jobs, 512 PIDs, 1 GiB shared
memory, no restart, and RAM/swap limits of the same byte count (no container
swap). The guard samples every 250 ms. It stops on OOM, unavailable guard data,
more than 64 MiB host swap growth, or host available RAM below the greater of
the requested floor and initial available RAM minus the requested memory limit.

The existing framed node supervisor detects caller death, closed transport or
a 30-second missed-renewal lease. Separately, container PID 1 requires renewals
from its node driver and enforces its own 30-second lease and time limit. This
second boundary covers Docker continuing after its host-side client dies.
An expired lease cannot be renewed by a late heartbeat. The first current
resource/time/lease checks must pass before the GPU child is released.
Child process groups are terminated and reaped; Docker auto-removes the
container. Cleanup checks every rank, even when a peer cannot be reached, and
only removes containers with the exact invocation, request, rank, node and image
identity. Unrelated containers are preserved.

Only complete successful rank results **and** verified all-rank cleanup/idleness
produce success. A broken connection or Docker daemon can leave cleanup
unconfirmed; a process-exit receipt alone never proves container removal. There
is no automatic retry. After abrupt controller loss, allow the node/container
leases to expire and use `pulsar inventory --json` to inspect actual state before
proposing recovery. Do not replace or delete an unfamiliar container by name.

## Evidence and validation

The fresh evidence directory retains the request, frozen payload/Stack code,
private rank mapping, every preflight/execution/cleanup batch and their raw node
output. `result.json` summarizes completion and always sets `qualification` to
false. A failed or interrupted invocation is retained; absence of a final result
is incomplete evidence. Keep these files outside public Git history.

CPU regression tests exercise the real Bash boundary, framed transports, node
programs and container guard with synthetic Docker/SSH/resource responses.
They cover complete and missing ranks, changed images/payloads, occupied GPUs,
partial startup, peer failure, time/memory/lease conditions, controller death,
and cleanup that preserves unrelated containers. Physical Docker/NCCL behavior,
performance, numerical output and model serving require separately authorized
hardware validation.

Image-staging tests additionally cover preview/apply separation, missing or wrong
images, insufficient storage, unsupported storage layouts, unknown node state,
interrupted sender/receiver streams, topology changes, skipped existing images,
no pull fallback, complete post-transfer identity checks, containerd root overrides,
import/configuration changes, endpoint mismatches and separately mounted stores.
Parsed operation and approval flags are immutable across site-configuration loading;
a generic site `MODE` variable cannot change preview into execution. Transfer
timeouts remain in the public command's supervised process group so cancellation
reaps both local stream clients.
