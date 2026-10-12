# Catalog model recipes

The workbench maintainer publishes one schema-valid
`<spec-id>.json` per selected exact model, image, engine/container recipe, and hardware geometry.
The filename must equal the document's complete `spec_id`. State, review, and
evidence are metadata rather than membership gates. See
[contribution review](../docs/CONTRIBUTIONS.md).

From the repository root, use `./pulsar models list` for the catalog listing or
`./pulsar models` for the interactive catalog on a terminal. Specs
are the expected identity for acquisition, preparation and archive restoration;
a catalog entry is not a claim that its files are present on this installation.

A spec that the maintainer removed from the catalog is recorded in
[`catalog-removals.json`](../catalog-removals.json) with its date and reason.
See [removing a catalog entry](../docs/CONTRIBUTIONS.md#remove-a-catalog-entry).

New contributions and operations support serving-spec schemas 2 and 3. Schema 3
declares named required snapshots, including references to bundled checkpoints.
Existing schema-1 files remain historical and readable. The original filename
and spec ID are retained; new measurements are never attached to a reauthored
recipe as if they measured it. See the [public contract](../docs/CONTRACT.md)
for current formats and compatibility boundaries.
