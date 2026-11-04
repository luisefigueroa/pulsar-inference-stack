# Testing and validation

## Select checks for the change

Choose tests for changed behavior, its callers and the decision the result can
support. Run directly affected tests during iteration, then the relevant
subsystem checks after a coherent change. State the selection in the normal
review or handoff; do not create a new checklist artifact per command.

| Work | Validation to select |
| --- | --- |
| Read-only investigation | Existing evidence and targeted inspection. |
| Prose-only documentation | Links, consistency, privacy and diff checks; no full suite or tests that assert wording. |
| Recipe, catalog or evidence data | Canonical spec/package/evidence validation and affected behavior checks; preserve publication safeguards and separate physical authority. |
| Bounded implementation change | Affected tests and callers' relevant subsystem checks. |
| CI or check-runner change | Workflow/script syntax, event and step selection, argument handling, and execution of affected checks. |
| Broad implementation or dependency change | The full suite when the impact spans subsystem contracts or cannot be bounded by selected coverage. |
| Commit, push or PR with unchanged tested inputs | Reuse the selected results; run the required privacy, staged-content, commit-metadata and diff checks. |

## Run the selected tests quickly

Match the run to the stage of the work:

| Stage | Run |
| --- | --- |
| Fixing one behavior | The affected test IDs, such as `python3 -m unittest tests.test_file_inventory.FileInventory.test_internal_node_state_symlink_is_never_read`, or `-k PATTERN` on one module. Rerun only what failed. |
| After a coherent change | The modules that import or call the changed code: search `tests/` for the changed functions, operations and scripts, not for a subsystem name. Run several at once with `python3 scripts/parallel_unittest.py MODULE...`. A selection that includes `tests.test_model_library_cli` takes as long as the full suite, so run that instead. |
| Before opening a pull request | `scripts/selftest.sh --jobs "$(nproc)"`, once. CI runs the same suite on the pull request. |

`tests.test_model_library_cli` takes most of the serial suite: about 50 of its
60 minutes when measured in November 2026. Select it when the change reaches
the model-library commands it drives, and run it through
`scripts/parallel_unittest.py`.

`scripts/parallel_unittest.py` runs each test in its own process; a class or
module that defines fixtures runs in one process, so its fixtures run once.
Tests without a saved duration start first, then the slowest, using durations
saved in `~/.cache/pulsar/selftest-durations.json` (under `XDG_CACHE_HOME` when
set). Failures print as they happen, then each traceback, the slowest tests and
the summary. Without `--jobs`, `scripts/selftest.sh` runs the same discovery
passes serially.

## Full-suite triggers and evidence reuse

Run the full suite, `scripts/selftest.sh --jobs "$(nproc)"`, once before
opening a pull request; no reason is needed, and CI runs it again on the pull
request. During iteration, selected tests answer most questions in seconds; run
the full suite then only for a broad cross-subsystem behavior change, a
dependency change that affects execution, an interaction risk that selected
tests cannot cover, or an explicitly requested regression check, and say which
applies. Editing a shared helper is not alone a reason: inspect the affected
callers and their coverage first. A review comment, approval exchange or new
agent is not a test trigger by itself.

Before repeating a check, identify what changed, which prior evidence it
invalidated, and what decision the rerun can affect. Reuse results while the
relevant source, fixtures, dependencies and execution environment match. A change
invalidates affected coverage, not automatically every test in the repository.
Check the combined result when integrating branches with interacting changes.

Keep the commands, outcomes and relevant input identities in the existing review
or private handoff. Distinguish focused checks, checks-only mode and a completed
full suite; a passing fast check is not evidence that unit tests ran. Previous
failures remain visible until resolved. None of this replaces public-command
validation, contribution integrity, or live readiness at a physical-action boundary.

## Commands and CI

For example, a storage-verification change can select its affected methods and
these subsystem tests as appropriate:

```sh
python3 -m unittest tests.test_storage_integrity tests.test_verification_process
scripts/selftest.sh --checks-only
```

`--checks-only` runs the existing Python/Bash syntax, catalog and source privacy
checks without unittest discovery. The command without that flag retains the
complete regression suite. Staged privacy, commit-metadata and contribution
checks remain separate publication requirements.

CI runs two kinds of job on every pull request and on `workflow_dispatch`: the
checks job (checks-only mode, then the diff, changed-catalog and commit-metadata
checks of a pull request) and the unit tests, split into shards that each run
`scripts/parallel_unittest.py --shard INDEX/COUNT` with one process per runner
CPU. A shard is a fixed slice of the sorted test list, so every shard of one run
sees the same partition whatever durations its runner has saved. Each shard
restores the durations it saved last time, from the pull request's own earlier
run or else from a run on `main`, and saves what it measured at the end, so its
slowest tests start first from the second run on. Run the workflow on `main` by
hand once to seed the durations that new pull requests start from. The `selftest`
job passes only when the checks and every shard passed; it is the check the
`main` ruleset requires, so keep its name. There is no push or scheduled
trigger, and a new push to a pull request cancels the run for its previous
commit. Workflow enablement is an operator setting, separate from these event
rules.

## Existing regression coverage

`scripts/selftest.sh` runs canonical-spec tests, standalone Stack subsystem
tests, Python/Bash syntax checks, catalog checks, and publication privacy.
Use `shellcheck --severity=error` for affected shell scripts as well. Fixtures
are synthetic and do not authorize real downloads or service mutations.

`python3 -m unittest tests.test_ui_confirm` checks confirmation defaults,
explicit choices, and Escape/Ctrl-C for choose, input and confirmation prompts
against the bundled Gum in a pseudo-terminal.
These tests run on Linux arm64 and are skipped on other platforms. They only
answer fixture prompts and never perform operator actions.
Archive and topology caller tests preserve cancellation status without saving
configuration. Catalog tests cover returning to the containing submenu, recorded
check findings, interrupted operations and a signal arriving with a zero exit;
their operation doubles never contact serving hardware.

The refactor removes tests that required a shell profile projector, launch
commit labels, or runtime commit equality. Their replacement checks recipe
identity, direct container bindings, actual configuration drift, stored launch
plans, all-rank boot continuity, and schema-only catalog admission. Storage,
archive, topology, ownership, privacy, and historical-reader coverage remains.

New catalog tests distinguish unchanged historical blobs from newly contributed
or edited specs and inspect the selected Git commit/index, not unrelated dirty
working files. Pure document-command tests run without a cluster. Compact
evidence tests re-evaluate measurements independently and reject forged claims.

Resource tests cover confirmed pre-launch placement, unavailable container
metrics, attachment only to the expected owned recipe, and diagnostic cleanup.
Workbench separately tests its private stream retention and summary workflow.

The checked-in contract vectors live in `tests/fixtures/contracts/`. Additional
synthetic Docker inspect vectors and CLI scenarios are generated by the focused
runtime, evidence, resource, and integration tests. Baseline criteria and
existing golden measurement documents are unchanged.

Physical acceptance remains separate: launch a new spec with explicit authority,
inspect its actual configuration, observe the unchanged service through another
compatible checkout, and exercise explicit overrides. Run each needed geometry;
passing synthetic one-node and multi-node tests is not physical qualification.


Required-snapshot cases extend the existing spec, container-runtime, model-library
CLI, current-evidence, and integration-contract suites. Synthetic two-rank cases
cover different homes, combined storage budgets, interrupted preparation, exact
mounts, retention, and complete archive/evidence coverage. Schema-2 golden
fixtures remain compatibility checks; these tests do not qualify a physical
speculative-decoding engine or image.

Compatibility regressions cover schema-2 recipes with bare speculative model
locators: freezing, launch compatibility, and launch planning reject them while
existing frozen specs remain readable. N-gram and MTP configurations without a
model field retain their exact argument tokens through freezing and launch
planning. Reauthoring with named manifests produces a distinct schema-3 spec.
