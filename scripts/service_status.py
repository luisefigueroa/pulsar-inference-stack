#!/usr/bin/env python3
"""Status result for one spec: what was established, and how people read it.

status.sh first runs the complete all-rank observation. When it succeeds, the
result is that observation, verified. Otherwise the service inventory decides:

- services for the spec exist: a result with the inventory's state, not verified;
- none exist and every node was observed: the service_absent error;
- none exist and a node could not be observed: the service_state_unknown error.

A running service's API gets one GET /health with a 3-second timeout. Nothing
here changes a node. Errors are printed for people and, when
PULSAR_STATUS_ERROR_FILE is set, written there for the --json envelope.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_library.node_names import NodeNames
from scripts.terminal_format import TerminalWriter

HEALTH_TIMEOUT_SECONDS = 3
# Inventory service states in human terms; JSON keeps the inventory's values.
STATE_WORDS = {
    "stale": "exited (its containers exist, but none is running)",
    "stopped": "exited (its containers exist, but none is running)",
    "partial": "partial (some ranks are missing)",
    "degraded": "degraded (its ranks are inconsistent)",
}


class StatusError(Exception):
    def __init__(self, code: str, message: str, details: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or [{"field": "spec", "message": message}]


def health(api_url: str | None) -> bool | None:
    """True when GET /health answers 2xx, False when it does not; None without a URL."""
    if not api_url:
        return None
    request = urllib.request.Request(api_url.rstrip("/") + "/health")
    key = os.environ.get("VLLM_API_KEY") or os.environ.get("API_KEY")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=HEALTH_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def verified(observation: dict) -> dict:
    """The complete observation, with what status established about it."""
    return {**observation, "state": "running", "verified": True,
            "healthy": health(observation.get("api_url")), "reason": None}


def inventory_api_url(service: dict, nodes: dict) -> str | None:
    """The API of rank 0 from inventory facts, or None when they do not say."""
    port = service.get("api_port")
    ranks = sorted(service.get("ranks") or [], key=lambda rank: str(rank.get("rank") or "0"))
    node = nodes.get((ranks[0].get("node") if ranks else None) or "") or {}
    host = "127.0.0.1" if node.get("local") else node.get("control_ip")
    return f"http://{host}:{port}" if port and host else None


def unobserved_nodes(nodes: dict) -> list[dict]:
    return [{"field": "node", "node": info.get("hostname") or name, "node_id": info.get("node_id"),
             "message": info.get("probe_reason") or f"probe {info.get('probe_status')}"}
            for name, info in sorted(nodes.items())
            if info.get("confirmed") and info.get("probe_status") not in ("ok", "unset")]


def from_inventory(spec_id: str, inventory: dict | None, observe_error: str) -> dict:
    """The result when the complete observation was unavailable."""
    shown = spec_id[:12]
    if inventory is None:
        raise StatusError("service_state_unknown",
                          f"Service state for spec {shown} is unknown: the service inventory could not run. "
                          "Run ./pulsar inventory for details")
    nodes = inventory.get("nodes") or {}
    services = [row for row in inventory.get("services") or [] if row.get("conf") == spec_id]
    if not services:
        missing = unobserved_nodes(nodes)
        worker = inventory.get("worker") or {}
        if missing or worker.get("status") not in ("ok", "unset"):
            reason = "; ".join(f"{item['node']}: {item['message']}" for item in missing) \
                or worker.get("reason") or "a node could not be observed"
            raise StatusError("service_state_unknown",
                              f"Service state for spec {shown} is unknown: {reason}. Run ./pulsar topology check",
                              missing or None)
        raise StatusError("service_absent",
                          f"No service for spec {shown} exists on any node. Start it with ./pulsar start {shown}")
    service = services[0]
    state = service.get("state")
    api_url = inventory_api_url(service, nodes) if state == "running" else None
    reason = observe_error or "the complete observation was unavailable"
    if len(services) > 1:
        reason += f"; {len(services)} services match this spec"
    return {"schema_version": 1, "kind": "pulsar-service-status", "selected_spec_id": spec_id,
            "configuration_verified": False, "state": state, "verified": False,
            "healthy": health(api_url), "reason": reason, "api_url": api_url, "services": services,
            "message": "Service inventory only; complete current-spec observation is unavailable."}


def joined(names) -> str:
    unique = list(dict.fromkeys(name for name in names if name))
    return ", ".join(unique) or "its nodes"


def where(result: dict, inventory: dict | None) -> str:
    """The nodes a result covers, by hostname."""
    if result.get("verified"):
        names = NodeNames.saved()
        return joined(names(rank.get("node_id")) for rank in result.get("ranks") or [])
    nodes = (inventory or {}).get("nodes") or {}
    return joined((nodes.get(rank.get("node") or "") or {}).get("hostname") or rank.get("node")
                  for service in result.get("services") or [] for rank in service.get("ranks") or [])


def human(result: dict, nodes: str, writer: TerminalWriter | None = None) -> None:
    out = writer or TerminalWriter()
    spec = str(result.get("selected_spec_id") or result.get("spec_id") or "")[:12]
    state = result.get("state")
    if state == "running":
        state_words = {True: "running and healthy", False: "running, but its API did not answer /health",
                       None: "running"}[result.get("healthy")]
    else:
        state_words = STATE_WORDS.get(state, str(state))
    verified = "recipe and files verified" if result.get("verified") else f"not verified ({result.get('reason')})"
    out.emit(f"spec {spec}: {state_words} on {nodes}; {verified}")
    if result.get("verified") and not result.get("matches_selected_spec", True):
        out.emit(f"Modified recipe. Selected catalog spec: {result.get('selected_spec_id')}")
        out.emit("Selected-recipe measurements are reference only.")
    if result.get("api_url"):
        out.emit(f"API: {result['api_url'].rstrip('/')}/v1")
    if state in ("stale", "stopped"):
        out.emit(f"Remove it with ./pulsar stop {spec}")


def load(path: str | None):
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def last_error(path: str | None) -> str:
    """The last error line an observation printed, without its prefix."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines() if path else []
    except OSError:
        return ""
    errors = [line.split("error: ", 1)[1].strip() for line in lines if "error: " in line]
    return errors[-1] if errors else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--observation", help="complete observation JSON (status verified it)")
    parser.add_argument("--inventory", help="service inventory JSON; absent when inventory could not run")
    parser.add_argument("--observe-error", help="stderr of the failed complete observation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    observation, inventory = load(args.observation), None
    try:
        if observation is not None:
            result = verified(observation)
        else:
            inventory = load(args.inventory)
            result = from_inventory(args.spec, inventory, last_error(args.observe_error))
    except StatusError as exc:
        target = os.environ.get("PULSAR_STATUS_ERROR_FILE")
        if target:
            Path(target).write_text(json.dumps({"code": exc.code, "message": str(exc), "details": exc.details}),
                                    encoding="utf-8")
        TerminalWriter(stream=sys.stderr).emit(f"error: {exc}")
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        human(result, where(result, inventory))
    return 0


if __name__ == "__main__":
    sys.exit(main())
