"""A deterministic CPU guard program, retained in each launch plan."""

import base64
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def program():
    resources = base64.b64encode((ROOT / "scripts/resource_sample.py").read_bytes()).decode()
    runtime = base64.b64encode((ROOT / "serving_guard/runtime.py").read_bytes()).decode()
    return (
        "import base64,sys,types\n"
        "pkg=types.ModuleType('scripts');pkg.__path__=[];sys.modules['scripts']=pkg\n"
        "mod=types.ModuleType('scripts.resource_sample');sys.modules[mod.__name__]=mod\n"
        f"exec(compile(base64.b64decode({resources!r}),'<guard-resources>','exec'),mod.__dict__)\n"
        f"exec(compile(base64.b64decode({runtime!r}),'<serving-guard>','exec'),"
        "{'__name__':'__main__'})\n"
    )


def digest(source):
    return hashlib.sha256(source.encode()).hexdigest()


def template(entrypoint, *, minimum=24 * 1024**3, startup=7200, timeout=10800,
             max_host_swap_growth_bytes=None):
    value = {"schema_version": 1, "program_sha256": digest(program()),
            "entrypoint": entrypoint, "min_host_available_bytes": minimum,
            "startup_timeout_seconds": startup, "timeout_seconds": timeout}
    if max_host_swap_growth_bytes is not None:
        value.update(schema_version=2, max_host_swap_growth_bytes=max_host_swap_growth_bytes)
    return value
