#!/usr/bin/env bash
# Deterministic local checks; no model services or hardware actions.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s release_spec/tests -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
python3 - "$ROOT" <<'PY'
import ast
from pathlib import Path
import subprocess
import sys
root = Path(sys.argv[1])
ignored = {'.git', '.venv', '.pulsar', '.model-library', 'experiments', 'exports', '__pycache__'}
for path in sorted(root.rglob('*')):
    if ignored.intersection(path.relative_to(root).parts) or not path.is_file():
        continue
    if path.suffix == '.py':
        ast.parse(path.read_text(), filename=str(path))
    elif path.suffix == '.sh' or path.name == 'pulsar':
        subprocess.run(['bash', '-n', str(path)], check=True)
print('Python and shell syntax checks passed')
PY
python3 scripts/check-catalog.py --repo-root "$ROOT"
python3 scripts/check_publishable_privacy.py --repo-root "$ROOT"
