"""``python3 -m release_spec`` entry point (standard library only).

``spec_id`` uses ``ensure_ascii=False``; snapshot ``manifest_id`` does not.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
