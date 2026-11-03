# Decision references carried into this repository

This repository has fresh history, but some source comments retain identifiers
from the predecessor stack. The current rules represented by those identifiers
are summarized here so operators do not need the predecessor repository.

- **ADR 0004 — evidence is scoped.** A measurement or review label applies to
  one exact model-serving recipe. It does not prove current storage or serving
  availability and does not grant catalog membership.
- **ADR 0005 — no live NFS serving.** Homes and prepared views use supported
  node-local serving filesystems. Operator-selected network storage may hold
  archives but is never mounted into serving containers by Pulsar.
- **ADR 0006 — one model-library path.** Acquisition, preparation, and runtime
  model mounts use the model library; launch commands do not select a second
  weight-distribution mode.
- **ADR 0008 — status is display metadata.** State and review labels do not gate
  catalog membership or serving. Removed force/validation flags must not return
  as aliases for status-based authorization.
- **ADR 0012 — manifests establish expected identity.** Old expected-identity
  files are not a live authority. Complete snapshot manifests and actual byte
  verification establish model identity.
- **ADR 0017 — exact spec identity.** Model snapshot, image digest, recipe
  arguments, container environment, and hardware geometry form immutable recipe
  identity. Deployment-only settings remain separate. The current public CLI contract replaces private schema/projector imports.

The operative contracts are the current code, [architecture](ARCHITECTURE.md),
and [operations guide](OPERATIONS.md). These summaries preserve terminology;
they are not evidence that predecessor implementation or physical validation was
imported.

## Plain status words, explained in place

The catalog's status words are written for someone who has not used Pulsar
before. Each one is explained in this order: a word that explains itself
(`not checked yet`, `every serving node`), then a short hint on the same line
naming the choice that resolves it, then a row under
[Catalog labels](OPERATIONS.md#catalog-labels), shown by `pulsar help labels`.
The legend is the reference, not a substitute for clear words, following the
NN/g usability heuristics #2, #6 and #10 and [clig.dev](https://clig.dev/#output).
Meaning never rests on color or dim text
([WCAG 1.4.1](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html),
[1.4.3](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)).
Rewording changes only human output; JSON fields and values stay stable. The
[terminal-output skill](../skills/terminal-output/SKILL.md#words) holds the
working rules, and a test fails when a printable status word has no legend row.

## Spec-contract refactor

Schema 2 extends immutable recipe identity to supported container settings.
Explicit operator overrides produce effective specs without changing catalog
entries. The public CLI replaces private Workbench imports and runtime Git pins.
Git commits remain campaign provenance; observations compare actual execution
with the stored effective spec. New measurements live in separate run directories.

Old specs/evidence are historical only for future operations. Inventory and safe
stop still recognize existing services. No service is restarted or evidence
rewritten merely because Stack code changes. See [CONTRACT.md](CONTRACT.md).

## Historical planning

The [spec-contract implementation proposal](SPEC_CONTRACT_IMPLEMENTATION_PLAN.md)
preserves the September 9, 2026 design and sequencing discussion. Its baseline,
future-tense instructions, and review checkpoints describe that planning task.
Use [the public contract](CONTRACT.md) for current interfaces and
[Operations](OPERATIONS.md) for current procedures. The proposal is not a
completion record or evidence of physical validation.
