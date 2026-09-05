"""``python3 -m release_spec verify|id|show FILE`` (standard library only).

``spec_id`` uses ``ensure_ascii=False``; snapshot ``manifest_id`` does not.
ASCII snapshot paths make the two encodings agree. Missing or invalid JSON
is reported as ``error:`` on stderr with exit status 2, not a traceback.
"""

from __future__ import annotations

import sys
from typing import Sequence

from .normalize import pretty_json_bytes
from .schema import ReleaseSpecError
from .verify import load_spec

COMMANDS = frozenset({"verify", "id", "show"})
USAGE = "usage: python3 -m release_spec verify|id|show FILE"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in COMMANDS:
        print(f"error: {USAGE}", file=sys.stderr)
        return 2
    command, path = args
    try:
        spec = load_spec(path)
    except ReleaseSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — CLI must not traceback
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if command == "verify":
        print(f"spec_id={spec['spec_id']} state={spec['state']}")
        return 0
    if command == "id":
        print(spec["spec_id"])
        return 0
    sys.stdout.buffer.write(pretty_json_bytes(spec))
    return 0
