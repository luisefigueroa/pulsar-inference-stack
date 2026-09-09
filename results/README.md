# Compact measurement evidence

New optional evidence lives at `baseline-v1/<spec_id>/<run_id>/`, with a run
record, the recorded policy, compact measurements, and optional summaries.
The run binds the effective recipe ID and dataset/policy/measurement hashes;
measurements are not embedded in a mutable spec. Later runs do not replace
previous evidence or inherit another recipe's measurements.

Use `pulsar evidence verify` to assess document consistency and baseline outcome
independently of catalog membership. Raw captures, local endpoints, runtime
context, and detailed logs stay private. Old layouts remain historical; this
refactor does not rewrite their evidence or claim new physical validation.
