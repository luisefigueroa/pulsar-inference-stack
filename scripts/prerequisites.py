#!/usr/bin/env python3
"""A node's operator prerequisites as check rows, worded once for every reader.

    python3 scripts/prerequisites.py < PROBE_JSON

reads `probe-node.py --prerequisites-only` JSON and prints one row per
prerequisite, as `pulsar doctor` records them: LEVEL, ID, the JSON message and
the text people read, split by a unit separator (0x1F). The JSON message is the
text without its marks; a value is never changed. Guided setup
(`topology setup`) shows the same rows for each machine. The rows
are state words: the probe never returns a token or an account name.

hf matters only on a node chosen to receive a download (its home); a node
without it can still serve and hold prepared copies. So every hf finding is a
warning that says what it affects, never a blocking issue.
"""
from __future__ import annotations

import json
import sys


def rows(document: dict, noun: str = "node", marks: bool = True) -> list[tuple[str, str, str]]:
    """(level, id, message) for each prerequisite in a --prerequisites-only answer.

    NOUN names what is checked: a "node" once it is a member, as doctor says,
    or a "machine" before, as guided setup says. MARKS puts backticks around
    commands and paths for people; without it the message is for JSON."""
    def code(text: str) -> str:
        return f"`{text}`" if marks else text

    docs = " (" + code("docs/OPERATIONS.md#installation-prerequisites") + ")"
    here = f"this {noun}"
    cannot = f", so models cannot be downloaded to {here}; it can still serve. To download here, "
    facts = document["prerequisites"]
    result = []
    python = facts["python"]
    if python.get("ok") is True:
        result.append(("ok", "python", "Python " + str(python.get("version"))))
    else:
        result.append(("fail", "python", "Python " + str(python.get("version") or "unknown")
                       + f" is older than 3.11; install Python 3.11 or newer on {here}" + docs))
    for tool, package in (("flock", "util-linux"), ("rsync", "rsync")):
        if facts.get(tool) is True:
            result.append(("ok", tool, f"{tool} present"))
        else:
            result.append(("fail", tool, f"{tool} not found; install {package} on {here}" + docs))
    hf = facts["hf"]
    state, version = hf.get("state"), str(hf.get("version") or "")
    if state == "ok":
        result.append(("ok", "hf_cli", "hf CLI " + (version or "present")))
    elif state == "missing" and hf.get("legacy_cli"):
        result.append(("warn", "hf_cli", "hf CLI not found, only the older huggingface-cli" + cannot
                       + f"install the current hf CLI on {here}" + docs))
    elif state == "missing":
        result.append(("warn", "hf_cli", "hf CLI not found" + cannot + f"install the Hugging Face hf CLI on {here}"
                       + docs))
    elif state == "unusable":
        result.append(("warn", "hf_cli", "hf CLI found, but its Python cannot load the Hugging Face library" + cannot
                       + f"install the hf CLI again on {here}" + docs))
    elif state == "too-old":
        result.append(("warn", "hf_cli", "hf CLI " + (version + " " if version else "") + "lacks what Download uses"
                       + cannot + f"install a newer hf CLI on {here}" + docs))
    else:
        result.append(("warn", "hf_cli", f"hf CLI not checked: the check did not finish on {here}"))
    login = hf.get("login")
    if login == "accepted":
        result.append(("ok", "hf_login", "hf logged in to the Hub"))
    elif login == "missing":
        result.append(("warn", "hf_login", f"hf is not logged in to the Hub on {here}; public models download "
                       f"without a login. To add one, run {code('hf auth login')} on {here}"))
    elif login == "rejected":
        result.append(("warn", "hf_login", f"the Hub rejected the hf login saved on {here}; downloads to {here} "
                       f"may fail until you run {code('hf auth login')} there again"))
    elif login == "not-reached":
        result.append(("warn", "hf_login", f"hf login not checked: the Hub could not be reached from {here}"))
    else:
        result.append(("warn", "hf_login", "hf login not checked because the hf CLI is not usable"))
    return result


def main() -> int:
    document = json.load(sys.stdin)
    for (level, identifier, message), (_, _, text) in zip(rows(document, marks=False), rows(document)):
        print("\x1f".join((level, identifier, message, text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
