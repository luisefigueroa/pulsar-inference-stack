"""Human names for topology node IDs.

Human output names machines by hostname; JSON and saved records keep the stable
node_id. Only the saved topology file is read: no node, SSH or Docker probes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def topology_path(repo: str | Path = ROOT) -> Path:
    override = os.environ.get("CLUSTER_TOPOLOGY_FILE")
    return Path(override) if override else Path(repo) / ".cluster-topology.json"


def saved_hostnames(path: str | Path) -> dict[str, str]:
    """Map node_id to hostname from a saved topology; an unreadable file maps nothing."""
    from scripts.topology_manifest import extract_topology
    try:
        topology = extract_topology(json.loads(Path(path).read_text(encoding="utf-8")))
        nodes = topology.get("nodes") or []
        return {str(node["node_id"]): str(node["hostname"]) for node in nodes
                if isinstance(node, dict) and node.get("node_id") and node.get("hostname")}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def saved_order(path: str | Path) -> list[str]:
    """Node IDs in saved topology order, which is ordinary start's rank order; unreadable maps nothing."""
    from scripts.topology_manifest import extract_topology
    try:
        topology = extract_topology(json.loads(Path(path).read_text(encoding="utf-8")))
        return [str(node["node_id"]) for node in topology.get("nodes") or [] if isinstance(node, dict)
                and node.get("node_id")]
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        return []


class NodeNames:
    """Name nodes for people. Without a mapping, node IDs are shown unchanged.

    A node_id missing from a loaded topology stays visible with that fact, so
    an unknown machine is never presented as a known one.
    """

    def __init__(self, hostnames: dict[str, str] | None = None, order: list[str] | None = None):
        self.hostnames = hostnames
        # Node IDs in saved topology order, when known.
        self.order = order or []

    @classmethod
    def saved(cls, repo: str | Path = ROOT) -> "NodeNames":
        path = topology_path(repo)
        return cls(saved_hostnames(path), saved_order(path))

    def serving_hostnames(self, count: int) -> list[str] | None:
        """Ordinary start's ranks 0..count-1: the first ``count`` saved nodes, named.

        None when the saved topology is unknown or has fewer nodes; start then
        refuses, and callers say "count nodes" instead of guessing machines.
        """
        if count < 1 or len(self.order) < count:
            return None
        return [self(node_id) for node_id in self.order[:count]]

    def __call__(self, node_id: object) -> str:
        text = str(node_id)
        if self.hostnames is None:
            return text
        return self.hostnames.get(text) or f"{text} (not in saved topology)"

    def prefixed(self, message: str) -> str:
        """Replace a leading "node_id: " in a saved blocker message with the hostname."""
        node_id, separator, rest = message.partition(": ")
        if separator and self.hostnames and node_id in self.hostnames:
            return f"{self.hostnames[node_id]}: {rest}"
        return message
