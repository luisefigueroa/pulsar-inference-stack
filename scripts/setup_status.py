#!/usr/bin/env python3
"""Local-only cluster setup status for the interactive pulsar menu.

Reads topology, archive configuration and catalog filenames on disk. Does not
contact nodes, Docker, GPUs or Hugging Face.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import sys
from typing import Any, Mapping

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from model_library.configuration import effective
from model_library.integrity import StorageError
from scripts.terminal_format import TerminalWriter
from scripts.topology_manifest import extract_topology, topology_has_ssh_trust

KIND = "pulsar-setup-status"
SCHEMA_VERSION = 1
SPEC_FILENAME = re.compile(r"^[0-9a-f]{64}\.json$")
# The next setup step. Without saved membership, guided topology setup
# confirms membership and enrolls SSH trust in one step; trust alone is
# enrolled when membership is already saved.
ACTIONS = {
    "set-up-topology": "Set up cluster membership and SSH trust",
    "enroll-ssh-trust": "Enroll SSH trust",
}


def topology_file(repo: Path) -> Path:
    override = os.environ.get("CLUSTER_TOPOLOGY_FILE")
    if override:
        return Path(override)
    return Path(repo) / ".cluster-topology.json"


def catalog_spec_count(repo: Path) -> int:
    releases = Path(repo) / "releases"
    if not releases.is_dir():
        return 0
    return sum(1 for path in releases.iterdir() if path.is_file() and SPEC_FILENAME.fullmatch(path.name))


def _topology_view(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "nodes": None, "trust": "not-enrolled"}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        topology = extract_topology(document)
        identity = topology.get("topology_id")
        nodes = topology.get("nodes")
        if not isinstance(identity, str) or not identity:
            raise ValueError("topology_id missing")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("nodes missing")
        count = len(nodes)
        if count > 1 and topology_has_ssh_trust(topology):
            trust = "enrolled"
        elif count > 1:
            trust = "not-enrolled"
        else:
            trust = "not-required"
        return {"status": "confirmed", "nodes": count, "trust": trust}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        return {"status": "invalid", "nodes": None, "trust": "not-enrolled"}


def _archive_status(repo: Path, environ: Mapping[str, str] | None) -> str:
    try:
        state = effective(repo, environ)
    except (StorageError, OSError):
        return "not-configured"
    status = state.get("status")
    if status in {"not-configured", "disabled", "configured"}:
        return status
    return "not-configured"


def build(repo: str | Path, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    repo = Path(repo)
    env = os.environ if environ is None else environ
    topology = _topology_view(topology_file(repo))
    archives = _archive_status(repo, env)
    specs = catalog_spec_count(repo)
    if topology["status"] != "confirmed":
        action = "set-up-topology"
    elif topology["trust"] == "not-enrolled":
        action = "enroll-ssh-trust"
    else:
        action = None
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        # Optional archive configuration does not block cluster operations.
        "complete": action is None,
        "topology": {"status": topology["status"], "nodes": topology["nodes"]},
        "ssh_trust": {"status": topology["trust"]},
        "archives": {"status": archives},
        "catalog": {"spec_count": specs},
    }
    if action is not None:
        document["next_action"] = action
        document["next_label"] = ACTIONS[action]
    return document


def render_text(
    document: dict[str, Any],
    *,
    writer: TerminalWriter | None = None,
    width: int | None = None,
) -> None:
    out = writer or TerminalWriter(width=width)
    topology = document["topology"]["status"]
    if topology == "confirmed":
        nodes = document["topology"]["nodes"]
        topology_text = f"{nodes} node" if nodes == 1 else f"{nodes} nodes"
    elif topology == "invalid":
        topology_text = "invalid"
    else:
        topology_text = "not confirmed"
    trust = {
        "enrolled": "enrolled",
        "not-required": "not required",
        "not-enrolled": "not enrolled",
    }[document["ssh_trust"]["status"]]
    archives = {
        "configured": "configured",
        "disabled": "disabled",
        "not-configured": "not configured (optional)",
    }[document["archives"]["status"]]
    specs = document["catalog"]["spec_count"]
    catalog = (
        "empty (no specs published yet)"
        if specs == 0
        else ("1 spec" if specs == 1 else f"{specs} specs")
    )
    if document["complete"]:
        out.emit("Cluster")
        out.field("Topology", topology_text, indent=2)
        if document["ssh_trust"]["status"] != "not-required":
            out.field("SSH trust", trust, indent=2)
        out.field("Archives", archives, indent=2)
        if document["archives"]["status"] == "not-configured":
            out.emit("Archive actions need a location; choose Archive storage configuration when needed.")
        out.blank()
        out.emit("Catalog")
        out.emit(catalog, initial_indent="  ", subsequent_indent="  ")
        return
    out.emit("Setup")
    out.field("Topology", topology_text, indent=2)
    out.field("SSH trust", trust, indent=2)
    out.field("Archives", archives, indent=2)
    out.field("Catalog", catalog, indent=2)
    out.blank()
    if topology == "confirmed":
        out.emit("Cluster membership is confirmed; SSH trust is not enrolled.")
    elif topology == "invalid":
        out.emit("Saved cluster membership is invalid.")
    else:
        out.emit("Cluster membership is not configured.")
    out.emit(f"Next: {document['next_label']}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_ROOT)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--width", type=int)
    args = parser.parse_args(argv)
    document = build(args.repo_root)
    if args.format == "json":
        json.dump(document, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    render_text(document, width=args.width)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
