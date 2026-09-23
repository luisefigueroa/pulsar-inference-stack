# Optional serving guard policy

`recipe.container.guard` records the guard policy used by a serving recipe.
It is optional in spec schemas 2 and 3 and their corresponding draft formats.
Omitting it preserves existing spec identities. Its complete content contributes
to the recipe's `spec_id`; changing or removing it creates a different recipe.
Explicit null, unknown fields and malformed values are rejected.

| Field | Accepted value |
| --- | --- |
| `schema_version` | Integer 1 or 2 |
| `program_sha256` | 64 lowercase hexadecimal characters identifying the recorded guard program |
| `entrypoint` | 1–16 nonempty strings, at most 1,024 characters each, without Unicode control characters (category `Cc`, including DEL and C1) |
| `min_host_available_bytes` | Integer from 8 through 128 GiB, expressed in bytes |
| `startup_timeout_seconds` | Integer from 10 through 14,400; cannot exceed the session timeout |
| `timeout_seconds` | Integer from 10 through 21,600 |
| `max_host_swap_growth_bytes` | Required in schema 2 only; integer from 0 through 268,435,456 |

The containing recipe requires host networking, no automatic restart, a null
Docker healthcheck and an explicit memory limit from 1 through 112 GiB.
Boolean values are not accepted as integer limits. Schema 1 retains its original
document shape without the explicit host-swap-growth field.

This example is data to include in a complete draft:

```json
{
  "schema_version": 2,
  "program_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "entrypoint": ["python3", "/opt/example/entrypoint.py"],
  "min_host_available_bytes": 25769803776,
  "startup_timeout_seconds": 7200,
  "timeout_seconds": 18000,
  "max_host_swap_growth_bytes": 268435456
}
```

The example hash is synthetic. A real recipe records the exact program hash
and entrypoint used by its separately reviewed execution implementation.
Schema validation checks the hash's format; it does not fetch a program, verify
installed code, or execute the declared policy.

## Catalog and execution support

The public document commands can freeze, verify and compare recipes containing
a valid guard, and the contribution verifier can check their saved evidence
packages. Catalog checks still require a schema-valid regular spec file,
matching filename/spec ID, and publication privacy. No catalog classification
follows from a guard or from passing measurements.

This Stack version supports the guard document but has no guard execution
implementation. `pulsar contract` advertises `serving_guard_schema_versions`
without adding guarded execution operations. The static launch-compatibility
check reports guarded recipes unsupported. Ordinary launch entrypoints reject
them, including guards introduced by explicit overrides, before image staging
or service replacement. This execution-only refusal does not apply to observation
or pre-launch resource sampling; their schema, platform and ownership checks
still apply. Status, stop, storage and document validation retain their existing
boundaries.

Experiment planning, probe selection, stage budgets, measurement sequencing,
observation cadence and interpretation belong to Workbench skill guidance.
A future execution implementation must enforce the recorded policy in tested
code; skill instructions alone cannot provide memory limits or cleanup after
the agent or controller disappears. Publishing the schema does not select that
implementation's ownership or introduce another runner into Stack.
