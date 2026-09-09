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
scripts/selftest.sh
```

`check-catalog.py` checks the release-spec schema, regular-file layout, and the
filename-to-`spec_id` binding. The privacy scanner remains a separate mandatory
publication safeguard.

## Optional assessments

A contribution may also contain compact measurements, `run.json`, and a
qualification summary. These files do not grant or remove catalog membership.
Assess complete compact evidence explicitly with:

```sh
python3 scripts/verify-evidence.py \
  --spec releases/<spec-id>.json \
  --evidence-root . \
  --run results/baseline-v1/<spec-id>/run.json \
  --summary results/baseline-v1/<spec-id>/summary.json
```

Assess whether the current stack can statically reproduce the recipe with:

```sh
python3 scripts/check-launch-compatibility.py \
  --spec releases/<spec-id>.json
```

These diagnostics report only what they checked. Evidence consistency does not
prove that physical measurements occurred, and static launch compatibility does
not prove that model files, the image, topology, memory, ports, or services are
currently ready. `./pulsar start` performs the authoritative live checks.

Raw captures, detailed logs, topology, local paths, credentials, and private
workbench state do not belong in the public contribution. Publication never
downloads weights, launches or stops a model, or changes archives.

A later metadata change, including withdrawal, does not alter spec identity and
does not automatically stop services or remove files or archives.
