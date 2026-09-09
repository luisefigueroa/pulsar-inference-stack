# Decision references carried into this repository

This repository has fresh history, but some source comments retain identifiers
from the predecessor stack. The current rules represented by those identifiers
are summarized here so operators do not need the predecessor repository.

- **ADR 0004 — evidence is scoped.** A measurement or review label applies to
  one exact model-serving recipe. It does not prove current storage or serving
  availability and does not grant catalog membership.
- **ADR 0005 — no live NFS serving.** Homes and prepared views use supported
  node-local serving filesystems. Operator-selected network storage may hold
  recovery archives but is never mounted into serving containers by Pulsar.
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

## Spec-contract refactor

Schema 2 extends immutable recipe identity to supported container settings.
Explicit operator overrides produce effective specs without changing catalog
entries. The public CLI replaces private Workbench imports and runtime Git pins.
Git commits remain campaign provenance; observations compare actual execution
with the stored effective spec. New measurements live in separate run directories.

Old specs/evidence are historical only for future operations. Inventory and safe
stop still recognize existing services. No service is restarted or evidence
rewritten merely because Stack code changes. See [CONTRACT.md](CONTRACT.md).
