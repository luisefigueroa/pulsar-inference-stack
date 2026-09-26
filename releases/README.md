# Catalog model recipes

The catalog starts empty. The workbench maintainer publishes one schema-valid
`<spec-id>.json` per selected exact model, image, engine/container recipe, and hardware geometry.
The filename must equal the document's complete `spec_id`. State, review, and
evidence are metadata rather than membership gates. See
[contribution review](../docs/CONTRIBUTIONS.md).

Use `./pulsar release list` or `./pulsar models` to inspect the catalog. Specs
are the expected identity for acquisition, preparation and archive restoration;
a catalog entry is not a claim that its files are present on this installation.

A spec that the maintainer removed from the catalog is recorded in
[`catalog-removals.json`](../catalog-removals.json) with its date and reason.
See [removing a catalog entry](../docs/CONTRIBUTIONS.md#remove-a-catalog-entry).

New contributions and operations use serving-spec schema 2. Existing schema-1
files remain historical and readable. The original filename/spec ID is retained;
new measurements are never attached to a reauthored recipe as if they measured it.
