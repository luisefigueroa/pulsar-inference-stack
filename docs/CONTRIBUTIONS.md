# Review a catalog contribution

The private workbench exports a local package. Export does not publish.
Publication proposes the spec and compact evidence through a reviewed pull
request against this public stack. A separate contribution worktree protects
the experiment checkout.

A contribution contains exactly one `releases/<spec-id>.json` and the six
baseline measurement documents, `run.json`, and `summary.json` under
`results/baseline-v1/<spec-id>/`. Raw captures, detailed logs, topology, local
paths and credentials do not belong in the public contribution.

Run these checks on the proposed checkout:

```sh
python3 scripts/check-catalog.py
python3 scripts/check_publishable_privacy.py
scripts/selftest.sh
```

The catalog checker independently verifies file hashes, complete gate coverage,
the approved baseline policy and thresholds, run/spec consistency, the closed
summary, and current consumer compatibility. It does not access the private
workbench and does not claim to reproduce physical execution. The named private
lab commit is provenance for maintainers with access, not publicly inspectable
source evidence.

The six minimum requirements are exact model-file identity; serving smoke;
strict repeatability within one server boot; the pinned 100-question GSM8K
subset with its fixed accuracy floor; a 60-minute error-free soak; and complete
performance measurements at concurrency 1, 2, 4 and 8. Performance is measured
without a minimum speed threshold. No deep or maximum-context qualification is
implied. The deeper `validated` suite remains deferred.

The workbench must verify a matching recovery archive before export and before
publication. Public checks validate its compact declaration; they cannot access
a maintainer's private archive. Review and merge, rather than an exporter flag,
establish the catalog contribution.

An existing recipe may later be withdrawn by a reviewed metadata change with a
clear reason. Preserve its identity, measurements and archive. The catalog warns
and removes recommendations; exact serving remains possible when operational
checks pass. No new baseline or archive deletion is implied by withdrawal.
