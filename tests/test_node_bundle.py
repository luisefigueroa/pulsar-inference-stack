"""Node programs must travel on stdin; argv cannot hold a real snapshot bundle."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def bundled_program(request):
    return subprocess.check_output(
        [sys.executable, str(ROOT / "scripts/node-bundle.py")],
        input=json.dumps(request).encode(), cwd=str(ROOT))


class NodeBundleTransport(unittest.TestCase):
    def test_oversize_program_runs_on_stdin_not_argv(self):
        request = {"operation": "roots", "pad": "x" * 20000}
        program = bundled_program(request)
        self.assertGreater(len(program), 131071)
        try:
            subprocess.run([sys.executable, "-c", program.decode()], check=True,
                           capture_output=True, cwd=str(ROOT))
        except OSError as exc:
            self.assertEqual(exc.errno, errno.E2BIG)
        result = subprocess.run([sys.executable, "-"], input=program, capture_output=True,
                                cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        payload = json.loads(result.stdout)
        self.assertIn("home_root", payload)
        self.assertIn("view_root", payload)

    def test_model_node_invokes_python_on_stdin(self):
        with tempfile.TemporaryDirectory() as temp:
            python = Path(temp) / "python3"
            observed = Path(temp) / "argv.json"
            python.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                f"json.dump(sys.argv[1:], open({str(observed)!r}, 'w'))\n"
                "sys.stdout.write(sys.stdin.read()[:24])\n")
            python.chmod(0o700)
            script = r'''
set -euo pipefail
REPO_DIR=$1
. "$REPO_DIR/scripts/lib.sh"
. "$REPO_DIR/scripts/model-library-common.sh"
require_cluster_nodes() { CLUSTER_TOPOLOGY_COUNT=1; CLUSTER_NODE_IDS=(node-0); }
model_node 0 '{"operation":"roots"}'
'''
            env = {**os.environ, "PULSAR_NODE_PYTHON": str(python), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run(["bash", "-c", script, "test", str(ROOT)], env=env,
                                    cwd=str(ROOT), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(observed.read_text()), ["-"])
            self.assertTrue(result.stdout.startswith("import base64,io,json"))
