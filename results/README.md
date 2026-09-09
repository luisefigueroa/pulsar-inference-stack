# Compact qualification evidence

Catalog contributions may include compact baseline measurements, `run.json`,
and `summary.json` under `baseline-v1/<spec-id>/`. The corresponding spec names
any declared evidence hashes. Evidence is optional catalog metadata and never a
membership gate. Raw captures and detailed experiments remain in the private
workbench. This repository imports no historical results.

Run `python3 scripts/verify-evidence.py --help` to assess optional compact
evidence independently of catalog membership.
