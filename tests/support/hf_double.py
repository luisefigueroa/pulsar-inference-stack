"""A synthetic `hf` CLI, its Python and a fake Hugging Face library.

`install(root)` builds, under the temporary ROOT:

- `bin/`: `hf` (whose first line names the fake Python), `flock` and `rsync`;
- `python/python3`: runs this Python with the fake `huggingface_hub` first;
- `hub/huggingface_hub/`: answers `whoami` by PULSAR_TEST_HF_MODE;
- `node-home/`: an empty home directory.

It returns the environment a prerequisite probe runs in: PATH holds only
`bin/`, so a real `hf`, Python or login on this machine is never found, and
nothing reaches the Hub. Every library call is logged to `hf-calls.jsonl`.

Modes: accepted, missing (no login), rejected (401), unreachable (connection
error), server-error (503), timeout (the request never returns), too-old
(no `expand` in model_info), broken (the library cannot be imported).
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

# A recognisable fake login; tests assert neither appears in any output.
TOKEN = 'hf_fixture_secret_token'
ACCOUNT = 'fixture-account-name'

_LIBRARY = '''
import json, os, time
from pathlib import Path
__version__ = "9.9.0-fixture"
MODE = os.environ.get("PULSAR_TEST_HF_MODE", "accepted")
if MODE == "broken":
    raise ImportError("fixture library cannot be imported")

def _log(call):
    with (Path(os.environ["PULSAR_TEST_HF_ROOT"]) / "hf-calls.jsonl").open("a") as stream:
        stream.write(json.dumps(call) + "\\n")

def get_token():
    _log("get_token")
    return None if MODE == "missing" else TOKEN

class _Response:
    def __init__(self, status_code):
        self.status_code = status_code

class HfApi:
    if MODE == "too-old":
        def model_info(self, repo_id, revision=None):
            raise AssertionError("never called")
    else:
        def model_info(self, repo_id, revision=None, expand=None):
            raise AssertionError("never called")

    def list_repo_tree(self, repo_id, repo_type=None, revision=None, recursive=False, expand=False):
        raise AssertionError("never called")

    def whoami(self, token=None):
        _log("whoami")
        assert token == TOKEN
        if MODE == "rejected":
            from huggingface_hub.errors import HfHubHTTPError
            raise HfHubHTTPError("Invalid user token.", response=_Response(401))
        if MODE == "server-error":
            from huggingface_hub.errors import HfHubHTTPError
            raise HfHubHTTPError("Service unavailable", response=_Response(503))
        if MODE == "unreachable":
            raise ConnectionError("fixture network is unreachable")
        if MODE == "timeout":
            time.sleep(60)
        return {"name": ACCOUNT, "auth": {"accessToken": {"displayName": TOKEN}}}
'''

_ERRORS = '''
class HfHubHTTPError(OSError):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response
'''

_HF_API = '''
class RepoFile:
    pass
'''


def install(root: Path, mode: str = 'accepted', *, tools=('hf', 'flock', 'rsync')) -> dict[str, str]:
    """Build the double under ROOT; the environment for one prerequisite probe."""
    root = Path(root)
    hub = root / 'hub' / 'huggingface_hub'
    hub.mkdir(parents=True, exist_ok=True)
    (hub / '__init__.py').write_text(
        f'TOKEN = {TOKEN!r}\nACCOUNT = {ACCOUNT!r}\n' + _LIBRARY)
    (hub / 'errors.py').write_text(_ERRORS)
    (hub / 'hf_api.py').write_text(_HF_API)
    python = root / 'python' / 'python3'
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(f'#!/bin/sh\nPYTHONPATH={str(root / "hub")!r} exec {sys.executable!r} "$@"\n')
    python.chmod(0o755)
    binary = root / 'bin'
    binary.mkdir(parents=True, exist_ok=True)
    for name in ('hf', 'flock', 'rsync', 'huggingface-cli'):
        path = binary / name
        if name in tools:
            # The fake hf is only read for its first line; running it is logged.
            first = f'#!{python}' if name == 'hf' else '#!/bin/sh'
            path.write_text(f'{first}\necho executed {name} >> {str(root / "hf-calls.jsonl")!r}\n')
            path.chmod(0o755)
        else:
            path.unlink(missing_ok=True)
    (root / 'node-home').mkdir(exist_ok=True)
    return {'PATH': str(binary), 'HOME': str(root / 'node-home'), 'PULSAR_TEST_HF_MODE': mode,
            'PULSAR_TEST_HF_ROOT': str(root), 'LANG': 'C.UTF-8'}


def calls(root: Path) -> list[str]:
    path = Path(root) / 'hf-calls.jsonl'
    return path.read_text().split() if path.exists() else []


def probe_environment(root: Path, mode: str = 'accepted', **kwargs) -> dict[str, str]:
    """A whole environment for probe-node.py: only the double's variables."""
    env = install(root, mode, **kwargs)
    return dict(env, PYTHONDONTWRITEBYTECODE='1')


if __name__ == '__main__':  # pragma: no cover - a manual aid
    print(install(Path(os.environ['PULSAR_TEST_HF_ROOT'])))
