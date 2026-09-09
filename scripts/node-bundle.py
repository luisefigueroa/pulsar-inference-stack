#!/usr/bin/env python3
"""Emit a self-contained node program from checked-in code and one JSON request.

Callers must run the program with `python3 -` and the source on stdin.
`python3 -c` hits Linux MAX_ARG_STRLEN once the inlined packages plus a
real snapshot request exceed ~128KiB.
"""
import base64
import io
import json
from pathlib import Path
import sys
import zipfile

root=Path(__file__).resolve().parent.parent
request=json.load(sys.stdin)
data=io.BytesIO()
with zipfile.ZipFile(data,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
    for package in ('release_spec','model_library'):
        for path in sorted((root/package).glob('*.py')):
            bundle.writestr(str(path.relative_to(root)),path.read_bytes())
encoded=base64.b64encode(data.getvalue()).decode('ascii')
argument=base64.b64encode(json.dumps(request,separators=(',',':')).encode()).decode('ascii')
# No code or shell fragments are taken from request fields. Temporary extraction
# carries code only; model bytes always use explicitly selected storage roots.
print('import base64,io,json,pathlib,sys,tempfile\n'
      'with tempfile.TemporaryDirectory(prefix="pulsar-node-") as work:\n'
      ' p=pathlib.Path(work)/"modules.zip"\n'
      f' p.write_bytes(base64.b64decode({encoded!r}))\n'
      ' sys.path.insert(0,str(p))\n'
      ' from model_library.node import main\n'
      f' sys.stdin=io.StringIO(base64.b64decode({argument!r}).decode())\n'
      ' raise SystemExit(main())')
