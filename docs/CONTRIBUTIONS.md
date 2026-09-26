# Review a catalog contribution

The workbench maintainer decides which specs are published to the catalog.
Publishing places one schema-valid `releases/<spec-id>.json` in this repository;
the filename must equal the document's complete `spec_id`. The stack does not
promote, classify, or infer catalog authority from state, review, evidence,
archive observations, or launch compatibility.

`state` may be `measured`, `released`, or null. `review` may be a valid review
object, an empty object, or null. Both are display metadata. A catalog spec is
selectable regardless of either value, while actual storage and serving actions
continue to enforce their live prerequisites.

Run the catalog and publication-safety checks on the proposed checkout:

```sh
python3 scripts/check-catalog.py
python3 scripts/check_publishable_privacy.py
git diff --check
```

`check-catalog.py` checks the release-spec schema, regular-file layout, and the
filename-to-`spec_id` binding. The privacy scanner remains a separate mandatory
publication safeguard.

Select additional tests for the changed behavior using
[validation guidance](TESTING.md#select-checks-for-the-change). A catalog-only
contribution needs its spec, package and applicable evidence checks; publication
alone does not require the full implementation suite. Reuse matching validation,
and check the exact staged content and commit metadata before publishing.

## Optional assessments

New publications support schema-2 and schema-3 specs. Historical files already in
the catalog remain readable; CI rejects adding or editing schema-1 specs for new operations.
Use the public package verifier for an exported contribution:

```sh
./pulsar contribution verify --package PACKAGE_DIRECTORY --json
```

Compact evidence is separate from the spec at
`results/<suite>/<spec-id>/<run-id>/`, with `baseline-v2` for new campaigns
and `baseline-v1` for retained historical campaigns. The directory must match
the saved policy. Baseline-v2 packages retain the greedy comparison as an
ungraded diagnostic alongside the five graded criteria. Verify a run explicitly:

```sh
./pulsar evidence verify --spec-file releases/SPEC_ID.json \
  --evidence-root . --run results/baseline-v2/SPEC_ID/RUN_ID/run.json --json
```

The result distinguishes document consistency from the baseline outcome. Failed
or incomplete outcomes do not remove catalog membership. A new run can be added
for an existing recipe without replacing old evidence or catalog metadata.
Optional timestamped archive observations belong to evidence summaries and do
not establish present archive availability.

Assess static launch support separately with
`python3 scripts/check-launch-compatibility.py --spec releases/SPEC_ID.json`.

These diagnostics report only what they checked. Evidence consistency does not
prove that physical measurements occurred, and static launch compatibility does
not prove that model files, the image, topology, memory, ports, or services are
currently ready. `./pulsar start` performs the authoritative live checks.

Raw captures, detailed logs, topology, local paths, credentials, and private
workbench state do not belong in the public contribution. Publication never
downloads weights, launches or stops a model, or changes archives.

A later metadata change, including withdrawal, does not alter spec identity and
does not automatically stop services or remove files or archives.

## Remove a catalog entry

Prefer withdrawal, which keeps the recipe visible with its reason. When the
maintainer explicitly decides that a spec should leave the catalog, remove it
in one change:

1. Delete `releases/SPEC_ID.json` and every `results/*/SPEC_ID/` directory.
2. Append an entry to `catalog-removals.json` with the complete `spec_id`, the
   `removed_at` date (`YYYY-MM-DD`) and a public `reason`. Keep the reason free
   of private paths, addresses, node identities and credentials.

```json
{"schema_version": 1, "kind": "pulsar-catalog-removals", "removals": [
  {"spec_id": "SPEC_ID", "removed_at": "2026-09-26", "reason": "Superseded by SPEC_ID"}
]}
```

`scripts/check-new-specs.py` rejects a deletion that is not recorded in the
ledger or that leaves evidence behind, and rejects a changed or dropped ledger
entry. `scripts/check-catalog.py` rejects a recorded spec that still has catalog
or evidence files. Git history retains the removed documents.

Removal does not stop services or remove model files, prepared copies or
archives. Installations that still hold the recipe's storage can manage it with
the retained spec document through `--spec-file`.
