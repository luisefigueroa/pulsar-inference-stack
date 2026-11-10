#!/usr/bin/env python3
"""A node's operator prerequisites as check rows, worded once for every reader.

    python3 scripts/prerequisites.py < PROBE_JSON

reads `probe-node.py --prerequisites-only` JSON and prints one
LEVEL|ID|MESSAGE row per prerequisite, as `pulsar doctor` records them;
guided setup (`topology setup`) shows the same rows for each machine. The rows
are state words: the probe never returns a token or an account name.

hf matters only on a node chosen to receive a download (its home); a node
without it can still serve and hold prepared copies. So every hf finding is a
warning that says what it affects, never a blocking issue.
"""
from __future__ import annotations

import json
import sys

DOCS = " (`docs/OPERATIONS.md#installation-prerequisites`)"
CANNOT = ", so models cannot be downloaded to this node; it can still serve. To download here, "


def rows(document: dict) -> list[tuple[str, str, str]]:
    """(level, id, message) for each prerequisite in a --prerequisites-only answer."""
    facts = document["prerequisites"]
    result = []
    python = facts["python"]
    if python.get("ok") is True:
        result.append(("ok", "python", "Python " + str(python.get("version"))))
    else:
        result.append(("fail", "python", "Python " + str(python.get("version") or "unknown")
                       + " is older than 3.11; install Python 3.11 or newer on this node" + DOCS))
    for tool, package in (("flock", "util-linux"), ("rsync", "rsync")):
        if facts.get(tool) is True:
            result.append(("ok", tool, f"{tool} present"))
        else:
            result.append(("fail", tool, f"{tool} not found; install {package} on this node" + DOCS))
    hf = facts["hf"]
    state, version = hf.get("state"), str(hf.get("version") or "")
    if state == "ok":
        result.append(("ok", "hf_cli", "hf CLI " + (version or "present")))
    elif state == "missing" and hf.get("legacy_cli"):
        result.append(("warn", "hf_cli", "hf CLI not found, only the older huggingface-cli" + CANNOT
                       + "install the current hf CLI on this node" + DOCS))
    elif state == "missing":
        result.append(("warn", "hf_cli", "hf CLI not found" + CANNOT + "install the Hugging Face hf CLI on this node"
                       + DOCS))
    elif state == "unusable":
        result.append(("warn", "hf_cli", "hf CLI found, but its Python cannot load the Hugging Face library" + CANNOT
                       + "install the hf CLI again on this node" + DOCS))
    elif state == "too-old":
        result.append(("warn", "hf_cli", "hf CLI " + (version + " " if version else "") + "lacks what Download uses"
                       + CANNOT + "install a newer hf CLI on this node" + DOCS))
    else:
        result.append(("warn", "hf_cli", "hf CLI not checked: the check did not finish on this node"))
    login = hf.get("login")
    if login == "accepted":
        result.append(("ok", "hf_login", "hf logged in to the Hub"))
    elif login == "missing":
        result.append(("warn", "hf_login", "hf is not logged in to the Hub on this node; public models download "
                       "without a login. To add one, run `hf auth login` on this node"))
    elif login == "rejected":
        result.append(("warn", "hf_login", "the Hub rejected the hf login saved on this node; downloads to this node "
                       "may fail until you run `hf auth login` there again"))
    elif login == "not-reached":
        result.append(("warn", "hf_login", "hf login not checked: the Hub could not be reached from this node"))
    else:
        result.append(("warn", "hf_login", "hf login not checked because the hf CLI is not usable"))
    return result


def main() -> int:
    for level, identifier, message in rows(json.load(sys.stdin)):
        print(f"{level}|{identifier}|{message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
