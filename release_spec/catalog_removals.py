"""Maintainer-recorded removals from the published catalog.

A removed spec leaves ``releases/`` and ``results/`` together. Its ledger entry
keeps the identity, date and reason readable after the files are gone. Entries
are append-only; Git history retains the removed documents.
"""
from __future__ import annotations

import datetime
import re

from .immutable_io import parse_strict_json

LEDGER_PATH = 'catalog-removals.json'
LEDGER_KIND = 'pulsar-catalog-removals'
ENTRY_KEYS = {'spec_id', 'removed_at', 'reason'}
MAX_REASON_LENGTH = 500


class CatalogRemovalError(ValueError):
    pass


def parse_removals(raw: bytes | None) -> dict[str, dict]:
    """Return removal entries by spec ID; a missing ledger records none."""
    if raw is None:
        return {}
    ledger = parse_strict_json(raw, label=LEDGER_PATH)
    if not isinstance(ledger, dict) or set(ledger) != {'schema_version', 'kind', 'removals'}:
        raise CatalogRemovalError(f'{LEDGER_PATH} must contain exactly schema_version, kind and removals')
    if ledger['schema_version'] != 1 or type(ledger['schema_version']) is not int or ledger['kind'] != LEDGER_KIND:
        raise CatalogRemovalError(f'{LEDGER_PATH} must be schema_version 1 of kind {LEDGER_KIND}')
    if not isinstance(ledger['removals'], list):
        raise CatalogRemovalError(f'{LEDGER_PATH} removals must be a list')
    entries = {}
    for entry in ledger['removals']:
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            raise CatalogRemovalError(f'{LEDGER_PATH} entries must contain exactly spec_id, removed_at and reason')
        spec_id, removed_at, reason = entry['spec_id'], entry['removed_at'], entry['reason']
        if not isinstance(spec_id, str) or re.fullmatch(r'[0-9a-f]{64}', spec_id) is None:
            raise CatalogRemovalError(f'{LEDGER_PATH} spec_id must be a complete spec id')
        if spec_id in entries:
            raise CatalogRemovalError(f'{LEDGER_PATH} records {spec_id} more than once')
        if not isinstance(removed_at, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}', removed_at) is None:
            raise CatalogRemovalError(f'{LEDGER_PATH} removed_at must be a YYYY-MM-DD date')
        try:
            datetime.date.fromisoformat(removed_at)
        except ValueError:
            raise CatalogRemovalError(f'{LEDGER_PATH} removed_at is not a calendar date') from None
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON_LENGTH:
            raise CatalogRemovalError(f'{LEDGER_PATH} reason must be non-empty text of at most {MAX_REASON_LENGTH} characters')
        entries[spec_id] = entry
    return entries


def check_append_only(before: dict[str, dict], after: dict[str, dict]) -> None:
    """Earlier removal entries remain unchanged in a later ledger."""
    for spec_id, entry in before.items():
        if after.get(spec_id) != entry:
            raise CatalogRemovalError(f'{LEDGER_PATH} entries are append-only; {spec_id} was changed or dropped')
