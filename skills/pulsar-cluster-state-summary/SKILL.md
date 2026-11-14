---
name: pulsar-cluster-state-summary
description: Read what runs on the cluster and how much memory each node has from one saved JSON output of pulsar inventory, pulsar status or pulsar doctor, with one summary script and no new filter each time. Keeps the command's exit status, the time of the observation and each node that did not answer in view. Use before a decision that depends on what is running, on free memory or on whether a stop is safe.
---

# Cluster state summary

Use this skill when a decision depends on the state of the cluster: what
runs, on which nodes, how much memory is available, and whether Stack owns a
service. It replaces a filter written by hand for each question. It reads;
it starts, stops and changes nothing, and it approves nothing.

## 1. Save the output and the exit status first

Run one command at a time, with a time limit, and keep its output, its
errors and its exit status in three files:

```bash
timeout 120 ./pulsar inventory --json > "$OUT/inventory.json" 2> "$OUT/inventory.err"; echo "$?" > "$OUT/inventory.rc"
```

- Do not pipe the command into a filter. The status of a pipeline is the
  status of the filter, and a filter that fails hides what the command said.
- Each of these commands contacts every confirmed node. They change nothing
  on a node, but a live cluster is in use: do not run several at once.
- Put `$OUT` outside the repository. The output names hostnames and
  addresses, which must not reach a tracked file.

| Question | Command | What it prints |
| --- | --- | --- |
| What runs on each node, who owns it, and how much memory is available | `inventory --json` | The inventory document, schema version 1 |
| Does this spec have a service, and is it verified and healthy | `status --json SPEC` | An envelope: `ok`, then `result` or `error` |
| Are the host and the saved membership in order | `doctor --json` | The `pulsar-doctor` document |

## 2. Summarize the saved file

```bash
python3 skills/pulsar-cluster-state-summary/summarize_state.py "$OUT/inventory.json"
```

The script reads that one file and contacts nothing. It recognizes the three
documents above. It exits 2 for a file that is none of them, so an error
message in place of JSON, or the successful result of another command, does
not pass as a summary. It shows an error envelope by its code, whatever
command wrote it. Read the saved exit status beside it; see
[error codes and exit statuses](../../docs/CONTRACT.md#error-codes-and-exit-statuses).

For an inventory it prints the time of the observation and its age, each
node with whether it was observed and its available memory, each service
with its state, its owner, the ranks observed, its API port and whether a
stop is safe, each rank with its node and its measured GPU memory, and each
GPU process that Stack does not manage. For status it prints the state,
`verified`, `healthy`, the API address and the selected spec, or the error
code with the nodes that were not observed. For doctor it prints the result
and each check that is not `ok`.

## 3. Read the summary with these rules

- **An observation has an age.** State the time of the inventory when you
  report from it. A saved file does not show the cluster as it is now.
- **Unknown is not absent.** A node that was not observed appears on a
  `NOT OBSERVED` line. Nothing is known about services or GPU processes on
  that node, and `none observed on the nodes that answered` does not mean
  that nothing runs.
- **A cluster of one node has no second node to observe.** The inventory
  then holds a placeholder entry named `worker` with the probe status
  `unset`. It is not a node, and the summary leaves it out.
- **The port that a service runs with can differ from the port configured
  now.** `runs with` is the port of the running API container; use it to
  reach the service. `configured now` is today's configuration.
- **A verified service can run a modified recipe.** Status verifies the
  service against its own launch record. When `matches selected spec` is
  `no`, the summary gives the selected spec and the effective spec and says
  `MODIFIED RECIPE`; measurements of the selected recipe are then reference
  only.
- **Status has two different answers of "no service".** `service_absent`
  (exit 1) means that every node was observed and none has the service.
  `service_state_unknown` (exit 3) means that Stack could not tell.
- **`safe to stop` covers the observed ranks only.** It is not an approval.
  A stop is a separate decision of the maintainer.
- **Available memory is each node's reading at that time.** A service that
  loads or runs changes it. Do not carry a reading across a start or a stop.

## 4. When the decision needs another field

Read it from the saved file. Do not run the command again for one field.
These are the names in the inventory document:

- Top level: `generated_at`, `topology_id`, `nodes`, `services`,
  `unmanaged_gpu_processes`.
- Each node: `probe_status`, `probe_reason`, `mem_available_gib`,
  `mem_total_gib`, `hostname`, `node_id`.
- Each service: `state`, `ownership`, `safe_to_stop`, `complete`,
  `expected_ranks`, `observed_ranks`, `api_port`, `spec_id`,
  `selected_spec_id`, `matches_selected_spec`, `reasons`, `ranks`.
- Each rank: `rank`, `node`, `running`, `status`, `observed_api_port`, and
  `measured_mib` in `gpu_memory`.

The status result is described in
[the contract](../../docs/CONTRACT.md#public-operations). The doctor document
has `result`, the counts `fail` and `warn`, and `checks`; each check has
`level` (`ok`, `warn` or `fail`), `id` and `message`.

## Limits

- The contract describes the status result. For the inventory document and
  the doctor document it gives no field table. The script follows what
  `scripts/inventory.sh` and `scripts/doctor.sh` write at schema version 1,
  and `tests/test_skill_state_summary.py` covers it with synthetic documents.
- The script shows what a decision usually needs, not the whole document.
  Labels, container IDs, image names and model-file sources stay in the
  saved file.
