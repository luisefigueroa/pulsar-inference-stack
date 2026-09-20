# Diagnostic-container source proposal

This capability remains unaccepted and unavailable through `pulsar`. Its public
dispatcher, help and integration-contract advertisement are withdrawn. Internal
scripts and synthetic tests are development review material. A separately
authorized supervised experiment may use `bash scripts/diagnostic.sh` in its
reviewed isolated checkout. This is not installed/public feature acceptance or
physical authority; fresh all-rank prerequisites still apply.

The proposal runs one model-free diagnostic on one confirmed local node, using
one already-loaded immutable image and one consumed start. Stack owns admission,
execution, observation and cleanup. The caller supplies a numerical payload and
interprets its results. A diagnostic is not a serving spec or qualification.

## Components and formats

Bash in `scripts/diagnostic.sh` owns shared topology/trust/doctor/idle admission,
Docker, the operation lock, child lifetimes, signals and reconciliation.
`scripts/diagnostic_run.py` handles attempt-local documents and transitions.
`release_spec/diagnostic_state.py` reduces observations and selects the final
outcome. `scripts/diagnostic_kmsg.py` owns the one memory/observation producer;
`scripts/diagnostic_journal.py` parses the explicit journal capture option;
`scripts/diagnostic_runtime.py` compiles argv and validates effective controls.
Serving schemas and lifecycle behavior are unchanged.

Historical development definitions use schema 1. Definition schema 2 adds exact
base64 OCI manifest/config bytes and a nullable `capture_file` on each step.
The manifest hash and config descriptor bind the separate config digest;
admission compares actual manifest-addressed Docker Id/Descriptor, platform,
ordered rootfs diff IDs and image-owned configuration. It does not manufacture
a Docker ConfigDigest. Inherited environment is distinct from operator
overrides: PATH and LD_LIBRARY_PATH remain image-owned; HOME=/tmp is explicit.
The result file may be `/tmp/result.json`; its bounded contents are appended
after `PULSAR_OUTPUT_JSON` within the existing encoded step stdout, including
on fixture failure. No extra write mount is added. Plans, lifecycle records, observations and
results use diagnostic schema 2. Plan identity covers the canonical definition,
node, complete control profile, payload digest and emitted entrypoint digest.
The caller's input-directory location does not affect identity. Sealing copies
verified, uniquely linked regular inputs into the private attempt and removes
write permissions. Later helpers use that local plan and copy. Payload type,
mode and hashes are checked again before start.

The older reducers in `release_spec/diagnostic.py` and original
`tests/test_diagnostic_container.py` remain implementation history. The new
execution path uses `diagnostic_state.py`. Consolidating that historical API/test
surface remains review work; old fixture results do not prove the new path.

## Ownership and process closure

An exclusive directory and OS-held lock protect a durable nonce/plan/boot/
controller PID-start claim. Intended create identity and consumed start are
written durably before dispatch. Losers cannot change winner records. Uncertain
replies never permit retry or reset. An empty query after ambiguous create does
not itself prove clean closure of an unbound operation.

Cleanup-only acquires the same lock and retains separate nonce-bound claims,
lifecycle records, captures and results. It preserves original records, never
restarts, and cannot reconstruct a lost observation window.

Each bounded external CLI command runs in a dedicated session. Its Bash leader remains
alive after command completion, preserving PID/start/group identity until the
owner closes that group and confirms absence. Command exit, wrapper wait and
group closure are separate evidence. Bounded TERM/KILL targets only these owned
CLI processes. The wrapper retains the operation lock; its commands do not
inherit it. Owner loss leaves a consumed claim and a bounded wrapper deadline.

Container termination uses only `docker stop --timeout -1 EXACT_ID` with a
bounded client. There is no container hard-kill fallback. Every mutation needs
fresh exact ownership. Ordinary removal also requires complete, typed,
phase-correct stopped state, followed by a successful exact absence query.
Failed stop or removal remains a failure even if absence is later observed.

## Controls and workload

The inspected envelope requires runc, network none, read-only root, GPU device
request 0, 2 CPUs, 16 GiB memory and memory-plus-swap, 256 PIDs, cap-drop ALL,
enabled no-new-privileges, private IPC and 64 MiB shared memory. The requested
mounts are the sealed read-only input and a bounded executable `/tmp` tmpfs.
Restart and healthcheck are disabled. Actual command, argv, environment, mount
source/access, control types and values must match. Explicit proc protection
paths are part of the profile; different reported paths are refused. Synthetic
fixtures do not establish a Docker version's defaults or effective GPU access.

The hash-bound Bash entrypoint runs ordered argv steps with timeouts. Its TERM/
INT handler forwards cancellation and waits for the active step. First failure
or exhausted output capture prevents later steps. At most 16 steps each have
256 KiB per output stream. Encoded records retain actual step exit separately
from capture completeness. Logs, raw observer events and helper records have
bounded captures; the attempt budget is 64 MiB.

## Observation and results

The historical native backend tails one read-only, nonblocking `/dev/kmsg`
descriptor once before create.
Every post-tail whole record, including readiness records, is reduced before
classification. EPIPE, sequence gaps, EOF, malformed records, ambiguous fragments,
boot changes and read errors irreversibly fail coverage. Malformed raw bytes are
retained within a fixed bound. There is no journal fallback or EOF-as-EAGAIN rule.

The explicit development `journal` backend uses the existing system journal.
Bash queries one current-boot kernel anchor and starts a direct owned
`journalctl --follow --no-tail --cursor=...` child before create. The producer
requires the actual inclusive anchor record, validates boot/transport/cursor/
timestamps/message, then consumes one stream while using the same memory
sampler and reducer. The old anchor is a location, not a new trial error.
Source stderr, malformed data, a missing anchor, observed loss, record read lag
over one second, stale samples and driver/OOM errors reject the trial.

After exact container cleanup, Bash performs one bounded inclusive non-follow
query from the original cursor, then terminates and waits for its exact journal
child. The producer consumes the closed stream and final query to their actual
ends, compares cursor contents and requires the final query to retain followed
cursors. Missing cursors or query/closure failure remain unknown. Source stderr
is separate from the CLI wrapper's teardown messages. There is no fixed read
count, quiet-time completeness inference, kernel-sequence claim, or EOF-as-EAGAIN.
Captures are capped at 8 MiB each, queries at one second, and final reconciliation
at five seconds while resource sampling continues. Parent review must accept
the residual journal delivery/retention limits before any physical release.

The producer samples host memory and binds workload counters to inspected CID,
PID/start, boot and an open cgroup-directory identity. The current path and inode
are checked across reads. Missing counters remain unknown. Sampled memory floor,
swap and OOM breaches latch even after recovery. Target sampling is at most
250 ms; maximum observation age is one second.

For the native backend, a new cleanup request requires fresh EAGAIN and fresh memory. Flood
draining remains bounded while sampling continues. Terminal identity, phase,
sequence, counters, timestamps and irreversible failures are validated together.
Terminal publication is separate from descriptor closure and actual process wait.

Only the final reducer selects `succeeded`, `failed_clean`,
`cleanup_unconfirmed` or `preflight_failed`. Workload, safety, coverage, cleanup
and observer closure remain separate. A zero exit proves neither safety nor
clean closure. Journal coverage means current-boot records delivered and retained
by the journal, reconciled through the bounded post-cleanup query, plus sampled
resources. It cannot establish every committed kernel record, undelivered or
unlogged faults, instantaneous peaks or future errors. Unobserved journal loss
remains possible. A follow event arriving after the finite query may cause a
conservative window rejection. The known terminal consistency defect remains
deferred: supervised review must inspect raw extrema, counters, errors and
separate actual cleanup evidence instead of relying on the success bit.

## Source verification and remaining acceptance

Focused suites are `test_diagnostic_lifecycle.py`, `test_diagnostic_state.py`
and `test_diagnostic_native.py`. They intercept Docker, topology, hardware and
kernel I/O. The integrated harness derives container configuration from requested
argv, executes the actual emitted entrypoint against CPU payloads, and supplies
fake proc/cgroup files and controlled whole-record reads. Its temporary copy of
the public dispatcher changes only the repository path and proposed diagnostic
route. The real dispatcher remains quarantined.
`test_diagnostic_journal.py` selects only admission, journal lifecycle, queued
cleanup error, provenance/output ordering and journal-child cancellation proof.

The harness retains and reaps owned children, including adopted helper
grandchildren. A test requiring forced teardown fails. Passing source tests
cannot establish host kmsg access, a compatible image/config-digest adapter,
cgroup retention after real process exit, usable GPU access, numerical
correctness or serving performance. Independent review must assess the complete
contract before advertising this capability. Host and GPU execution require
separate authorization.
