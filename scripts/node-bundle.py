#!/usr/bin/env python3
"""Emit a self-contained node program from checked-in code and one JSON request.

Standalone output runs with `python3 -` and source on stdin. Supervised
output uses the framed stdin transport and the fixed --bootstrap program.
Only that small bootstrap goes in argv; complete code/request bundles can
exceed Linux MAX_ARG_STRLEN and must remain on stdin.
"""
import base64
import argparse
import io
import json
from pathlib import Path
import sys
import zipfile

root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root))
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--supervised',action='store_true')
parser.add_argument('--bootstrap',action='store_true')
options=parser.parse_args()
if options.bootstrap:
    from model_library.verification_process import BOOTSTRAP
    print(BOOTSTRAP)
    raise SystemExit(0)
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
      + (' from model_library.verification_process import supervise_node\n'
         ' raise SystemExit(supervise_node(main,_pulsar_control.fileno(),_pulsar_token))'
         if options.supervised else ' raise SystemExit(main())'))
