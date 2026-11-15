---
name: pulsar-start-failure-triage
description: Find out why a `pulsar start` was refused, failed or was interrupted (a BLOCKED, FAILED or INTERRUPTED line, a nonzero exit, or an engine error in a rank's log) before any retry. Keep the evidence, read what Stack already reports, establish what still runs, and separate what was observed from what is inferred. Use when a start does not reach READY, or when asked why a model will not start.
---

# Start failure triage

Use this skill in the public inference stack when `./pulsar start` does not
print `READY`. It gives the order of work only. The codes, their meaning and
each one's next step live in `scripts/start_blockers.py` and are explained
under [what start reports](../../docs/OPERATIONS.md#what-start-reports-after-a-refusal-or-failure)
and [troubleshooting](../../docs/OPERATIONS.md#troubleshooting); change them
there, not here. This skill never starts, stops or removes anything. A retry
is a launch and needs the same approval as the first start (see `AGENTS.md`).

## 1. Keep the evidence

- Record the command as it ran, its real exit status, and every `BLOCKED`,
  `FAILED` or `INTERRUPTED` line with its node. A wrapper or background job
  must exit with the command's status; report from that recorded status.
- Keep the whole output of the failed start. A multi-node start that never
  became healthy removes its containers, so the log lines it printed for each
  failed rank are then the only copy of the engine's log. Where a container
  remains, `./pulsar logs <spec-id>` shows more.
- Search `docs/`, `tests/` and any notes you hold for the exact code and the
  exact error text before you propose a cause. An earlier finding outranks a
  new guess.

## 2. Read what Stack already says

- The [exit status](../../docs/CONTRACT.md#error-codes-and-exit-statuses): 1
  is a known no, 2 means nothing was attempted, 3 means Stack could not tell.
  Exit 3 and every `*_check_failed` code mean the condition is not
  established. Never report one as the condition it checks. A refused start
  exits 1 whatever its blocker is, so read the code as well as the status.
- The first word: `BLOCKED` is a refusal by a check, before any container of
  this start exists; with `--replace` the previous service can be gone by
  then, and the line says so. `FAILED` comes after the launch, and its line
  says what remains. `INTERRUPTED` removes nothing.
- The order of the checks, in `scripts/up.sh` and in the paragraph linked
  above. Some blockers end the checks early, and a check that was not reached
  is reported as not run (`not_run` under `--json`), never as passed, so a fix
  can uncover a later blocker. Read that order before you predict which
  refusal a state gives: a second start of a spec whose containers exist is
  refused by the existing-service check, which runs before the launcher is
  reached.
- `./pulsar start <spec-id> --dry-run`, with the same placement flags, repeats
  the checks and launches nothing. It does not run the multi-node preflight.
  A `preflight_failed` line follows the last lines of the preflight's output
  (all of it with `--verbose`). Read them before you ask for another start.

## 3. Establish what still runs

Take the failure line's own words on what remains, then confirm with
`./pulsar status <spec-id>` and `./pulsar inventory`, one at a time.
`service_absent` (exit 1) means that status observed every node and found no
service. `service_state_unknown` (exit 3) means that it could not observe a
node, which is not absence. Start says containers were removed only after it
confirmed that on every node; otherwise it names the nodes left. Removing
what remains is `./pulsar stop <spec-id>`, a separate decision. Do not remove
containers with Docker.

## 4. Find the cause

A `BLOCKED` code names the cause, except `preflight_failed` and the
`*_check_failed` codes, which name only where to look. A `FAILED` code such
as `container_exited` or `health_timeout` is the symptom: the cause is the
first error in the log of the first rank that failed. A later line that
points at a root cause above it is not the cause. Quote the engine's error as
written and do not explain it from memory; Stack interprets one engine error
only, in the next section.

## 5. Retry or not

A retry without a change repeats a refusal for missing files or image, for
cabling, `guard_unsupported`, `historical_spec`, `service_exists` and
`service_recorded_elsewhere`. These can pass later:

- `start_in_progress`: wait for the other start; it reports its own outcome.
- `node_unreachable` and the `*_check_failed` codes: once the check that the
  code names can run. The troubleshooting table names the first check.
- The engine's free-memory refusal at startup, which Start quotes in a line
  that starts `engine on <node>` and holds `Free memory on device … on startup
  is less than desired GPU memory utilization`. First rule out another holder
  of memory on that node with `./pulsar inventory`. Start's memory check
  judges each node as the engine does
  ([the engine's own startup check](../../docs/MEMORY_ESTIMATES.md#the-engines-own-startup-check)),
  but it reads memory before Start's own processes run, so a pass does not
  guarantee the engine's reading; an `INFO  engine` line names a node that
  was within its startup margin. Do not suggest dropping the Linux page cache
  (for example `echo 3 > /proc/sys/vm/drop_caches`) as the fix: the same
  section explains why it is not one, and it is a root action on a node that
  needs the maintainer's decision. When retries keep failing and nothing else
  holds memory, the open question is the recipe's memory settings; changing
  them makes a different spec, not a start flag.

When you propose retries, say how many starts you ask for and why, and what
ends the attempt.

## 6. Authority and report

`--replace`, `--pull-image`, `--accept-memory-warn` and `--skip-preflight`
are explicit decisions of the maintainer; never add one on your own. A
`Next:` line that adds one is a suggestion to the operator, not that
approval. Use the first-check column of the troubleshooting table for
inspection.

Lead the report with the action, the spec, node and rank, and the code. Then
give what was observed, with its command and exit status; what is inferred,
named as such; what is not known; what still runs; and one next step with the
approval it needs. Do not call a retry that passed a fix for a failure that
varies, and do not say that nothing runs unless status observed every node.
