"""Read-only menu projection of the existing inventory and published catalog."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_consumer import list_releases
from scripts.service_status import StatusError, check_inventory
from scripts.terminal_format import TerminalWriter


class ServiceNotObserved(LookupError):
    pass


def clean(value):
    return " ".join(str(value).split())


def project(inventory, catalog):
    """Join by exact catalog spec ID; inventory remains the ownership authority."""
    if (not isinstance(inventory, dict) or type(inventory.get("schema_version")) is not int
            or inventory["schema_version"] != 1):
        raise ValueError("inventory did not return its supported JSON document")
    try:
        check_inventory(inventory)
    except StatusError as exc:
        raise ValueError(str(exc)) from exc
    published = {row["spec_id"]: row for row in catalog}
    result = {}
    for service in inventory["services"]:
        if not isinstance(service, dict):
            raise ValueError("inventory contains an invalid service")
        spec_id = service.get("conf")
        if spec_id not in published:
            continue
        if spec_id in result:
            raise ValueError("inventory contains duplicate groups for a catalog spec")
        spec = published[spec_id]
        ranks = service.get("ranks")
        if not isinstance(ranks, list) or any(not isinstance(rank, dict) for rank in ranks):
            raise ValueError("inventory service has invalid rank information")
        nodes = [inventory["nodes"].get(rank.get("node"), {}) for rank in ranks]
        if any(not isinstance(node, dict) for node in nodes):
            raise ValueError("inventory service has invalid node information")
        where = list(dict.fromkeys(clean(node.get("hostname") or node.get("node_id") or "unknown node")
                                   for node in nodes))
        node_id = None
        if spec["nodes"] == 1 and len(nodes) == 1:
            value = nodes[0].get("node_id")
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
                node_id = value
        reason = None
        if service.get("ownership") != "managed" or service.get("safe_to_stop") is not True:
            reason = "Inventory has not established safe ownership and observability for Stop."
        elif not ranks:
            reason = "No service containers were observed; refresh the inventory."
        elif any(not node.get("node_id") for node in nodes):
            reason = "The observed service placement is incomplete; refresh inventory before stopping."
        elif spec["nodes"] == 1 and node_id is None:
            reason = "A single service node cannot be identified; inspect the inventory before stopping."
        result[spec_id] = {
            "spec_id": spec_id, "model_id": spec["model_id"], "nodes": spec["nodes"],
            "state": clean(service.get("state") or "unknown"),
            "ownership": clean(service.get("ownership") or "unknown"),
            "where": ", ".join(where) or "unknown", "node_id": node_id,
            "stop_reason": reason, "modified": service.get("matches_selected_spec") is False,
        }
    return result


def view(inventory, catalog, spec_id=None, *, width=None):
    services = project(inventory, catalog)
    output = io.StringIO()
    writer = TerminalWriter(width=width, stream=output)
    writer.emit("Live service inventory")
    writer.field("Observed", clean(inventory.get("generated_at") or "time unknown"))
    commands = []
    if spec_id is None:
        writer.field("Catalog services", len(services))
        writer.field("Other services", len(inventory["services"]) - len(services))
        writer.field("Unmanaged GPU processes", len(inventory.get("unmanaged_gpu_processes") or []))
        unavailable = [clean(node.get("hostname") or name) for name, node in inventory["nodes"].items()
                       if isinstance(node, dict) and node.get("confirmed") and node.get("probe_status") != "ok"]
        if unavailable:
            writer.field("Not observed", ", ".join(unavailable))
        if not services:
            writer.emit("No catalog services were observed.")
            if unavailable:
                writer.emit("Unobserved nodes may still be running services.")
        writer.emit("Other workloads remain read-only context in Show full inventory.")
        budget = writer.width - 6
        for key, service in services.items():
            prefix = f"{service['state'].upper()} [{key[:12]}] "
            remaining = budget - len(prefix)
            model = service["model_id"]
            if len(model) > remaining:
                model = model[:remaining - 3] + "..." if remaining >= 4 else ""
            label = (prefix + model).rstrip()
            commands.append(f"service\t{key}\t{clean(label)}")
    else:
        if spec_id not in services:
            raise ServiceNotObserved("The selected catalog service is no longer observed; returning to the service list.")
        service = services[spec_id]
        writer.field("Model", service["model_id"])
        writer.field("Spec", spec_id)
        writer.field("State", {"stale": "exited containers remain"}.get(service["state"], service["state"]))
        writer.field("Ownership", service["ownership"])
        writer.field("Nodes observed", service["where"])
        if service["modified"]:
            writer.emit("The observed recipe differs from the selected catalog spec.")
        writer.emit("Detailed status uses the existing service and file checks.")
        node = service["node_id"] or "-"
        commands.append(f"status\t{spec_id}\t{node}")
        if service["stop_reason"]:
            writer.emit(service["stop_reason"])
        else:
            scope = (f"on {service['where']}" if service["nodes"] == 1 else "across its participating nodes")
            question = (f"Stop and remove the owned service containers for {service['model_id']} "
                        f"[{spec_id[:12]}] {scope}? Model files, pins and archives are kept.")
            commands.extend((f"stop\t{spec_id}\t{node}", f"confirm\t{clean(question)}"))
    return ["header\t" + line for line in output.getvalue().splitlines()] + commands


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--spec-id")
    args = parser.parse_args(argv)
    try:
        with open(args.inventory, encoding="utf-8") as handle:
            inventory = json.load(handle)
        print("\n".join(view(inventory, list_releases(args.repo_root), args.spec_id)))
        return 0
    except ServiceNotObserved as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"error: inventory menu: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
