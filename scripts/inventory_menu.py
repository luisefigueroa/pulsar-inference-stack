"""Read-only menu projection of the existing inventory and published catalog."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
from model_library.catalog import age_seconds, age_text
from model_library.catalog_menu import OUTSIDE_CATALOG, SERVICE_STATES, STOP_KEEPS, stop_question
from model_library.render import short_size


class ServiceNotObserved(LookupError):
    pass


def clean(value):
    return " ".join(str(value).split())


def rank_order(rank):
    """Rank 0 first; a one-node service's "single" rank is rank 0."""
    value = rank.get("rank")
    if value == "single":
        return (0, 0, "")
    try:
        return (0, int(value), "")
    except (TypeError, ValueError):
        return (1, 0, str(value))


def observed_text(generated_at, now=None):
    """How long ago the inventory was taken, and its UTC time of day."""
    try:
        age = age_seconds(generated_at, now)
        stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return clean(generated_at or "time unknown")
    return f"{age_text(age)} ({stamp:%H:%M} UTC)"


def port_number(value):
    """A port as a number; a service outside the catalog reports it as text from its command."""
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def api_port(service, ranks):
    """The port rank 0 runs with, or today's configured port."""
    for rank in ranks[:1]:
        if isinstance(rank.get("observed_api_port"), int):
            return rank["observed_api_port"]
    return port_number(service.get("api_port"))


# How a container Pulsar cannot stop is owned, for its read-only line.
OWNERS = {"managed": "Pulsar-managed, not in this catalog", "legacy": "older Pulsar labels",
          "mismatch": "Pulsar labels do not match", "mixed": "mixed ownership", "unknown": "not started by Pulsar"}
MANAGED_LABEL = "io.pulsar.gb10.managed"
SPEC_ID = re.compile(r"[0-9a-f]{64}")


def unobserved_hosts(inventory):
    return [clean(node.get("hostname") or name) for name, node in inventory["nodes"].items()
            if isinstance(node, dict) and node.get("confirmed") and node.get("probe_status") != "ok"]


def stop_all_question(inventory, published):
    """The question before stop --all, naming each service it would remove.

    stop --all removes every container with the Pulsar-managed label, catalog
    spec or not, so the list comes from those labels. None when there is none.
    """
    names = []
    for service in inventory["services"]:
        # Only labelled containers are removed, so only their nodes are named.
        ranks = sorted((rank for rank in service.get("ranks") or []
                        if isinstance(rank, dict) and (rank.get("labels") or {}).get(MANAGED_LABEL) == "true"),
                       key=rank_order)
        if not ranks:
            continue
        spec_id = service.get("conf") or service.get("spec_id")
        model = (published.get(spec_id) or {}).get("model_id") or service.get("served_name") or "service"
        hosts = list(dict.fromkeys(clean((inventory["nodes"].get(rank.get("node")) or {}).get("hostname")
                                         or rank.get("node") or "unknown node") for rank in ranks))
        names.append(f"{clean(model)}{f' [{spec_id[:12]}]' if spec_id else ''} on {', '.join(hosts)}")
    if not names:
        return None
    unobserved = unobserved_hosts(inventory)
    blind = (f" {', '.join(unobserved)} could not be observed, and Stop refuses until every confirmed node "
             "answers.") if unobserved else ""
    return (f"Stop every Pulsar service on all confirmed nodes? It removes the containers of "
            f"{'; '.join(names)}.{blind} Containers Pulsar does not manage are untouched. {STOP_KEEPS}")


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
        ranks = sorted(ranks, key=rank_order)
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
            "where": ", ".join(where) or "unknown", "hosts": where, "node_id": node_id,
            "port": api_port(service, ranks),
            "stop_reason": reason, "modified": service.get("matches_selected_spec") is False,
        }
    return result


def project_others(inventory, catalog):
    """Pulsar-managed services whose full spec ID is not in this catalog, keyed by that ID.

    Such a service was started from a spec this checkout does not publish, for
    example by an experiment. Stop selects containers by their Pulsar labels and
    needs no catalog entry, so it is offered when the inventory established safe
    ownership and every confirmed node answered. Call project() first: it
    validates the inventory document.
    """
    published = {row["spec_id"] for row in catalog}
    unobserved = unobserved_hosts(inventory)
    result = {}
    for service in inventory["services"]:
        conf = service.get("conf")
        if (service.get("ownership") != "managed" or not isinstance(conf, str) or conf in published
                or not SPEC_ID.fullmatch(conf) or conf in result):
            continue
        ranks = service.get("ranks")
        if not isinstance(ranks, list) or any(not isinstance(rank, dict) for rank in ranks):
            raise ValueError("inventory service has invalid rank information")
        ranks = sorted(ranks, key=rank_order)
        nodes = [inventory["nodes"].get(rank.get("node"), {}) for rank in ranks]
        if any(not isinstance(node, dict) for node in nodes):
            raise ValueError("inventory service has invalid node information")
        where = list(dict.fromkeys(clean(node.get("hostname") or node.get("node_id") or "unknown node")
                                   for node in nodes))
        expected = service.get("expected_nodes")
        reason = None
        if service.get("safe_to_stop") is not True:
            reason = "Inventory has not established safe ownership and observability for Stop."
        elif not ranks:
            reason = "No service containers were observed; refresh the inventory."
        elif any(not node.get("node_id") for node in nodes):
            reason = "The observed service placement is incomplete; refresh inventory before stopping."
        elif unobserved:
            reason = f"{', '.join(unobserved)} could not be observed; Stop needs every confirmed node to answer."
        name = service.get("served_name") or service.get("container_name")
        result[conf] = {
            "spec_id": conf, "model_id": clean(name) if name else "served name not recorded",
            "nodes": expected if type(expected) is int and expected > 0 else len(where),
            "state": clean(service.get("state") or "unknown"), "ownership": "managed",
            "where": ", ".join(where) or "unknown", "hosts": where,
            "port": api_port(service, ranks), "stop_reason": reason,
        }
    return result


def service_label(state, key, model, details, budget, rooms=None):
    """STATE [spec12] MODEL then the longest of DETAILS that leaves the model its room.

    ROOMS gives, for each detail, the characters the model keeps before that
    detail is dropped; 12 for each by default.
    """
    prefix = f"{state.upper()} [{key[:12]}] "
    rooms = list(rooms or [12] * len(details))
    # Narrow terminals drop details, last first, before the model's room.
    for suffix, room in zip((*details, ""), (*rooms, 12)):
        remaining = budget - len(prefix) - len(suffix)
        if remaining >= min(len(model), room):
            break
    if len(model) > remaining:
        model = model[:remaining - 3] + "..." if remaining >= 4 else ""
    return clean((prefix + model + suffix).rstrip())


def node_count(service):
    seen = len(service["hosts"])
    if seen != service["nodes"]:
        return f"{seen} of {service['nodes']} nodes"
    return f"{seen} node{'' if seen == 1 else 's'}"


def read_only_line(service):
    """One container Pulsar cannot stop, by what it serves and who owns it."""
    ranks = [rank for rank in service.get("ranks") or [] if isinstance(rank, dict)]
    name = service.get("served_name") or service.get("container_name") or next(
        (rank.get("container_name") for rank in ranks if rank.get("container_name")), None)
    state = clean(service.get("state") or "unknown")
    owner = OWNERS.get(service.get("ownership"), OWNERS["unknown"])
    return f"{clean(name or service.get('service_id') or 'unnamed')} · {SERVICE_STATES.get(state, state)} · {owner} · read-only"


def gpu_process_text(inventory):
    """How many GPU processes Pulsar did not start, and their memory on each node."""
    processes = [process for process in inventory.get("unmanaged_gpu_processes") or [] if isinstance(process, dict)]
    by_node = {}
    for process in processes:
        mib = process.get("used_memory_mib")
        if type(mib) is int and mib >= 0:
            by_node[process.get("node")] = by_node.get(process.get("node"), 0) + mib
    parts = [str(len(processes))]
    for key, mib in by_node.items():
        if not mib:
            continue
        host = clean((inventory["nodes"].get(key) or {}).get("hostname") or key or "unknown node")
        parts.append(f"{short_size(mib * 1024 ** 2)} on {host}")
    return " · ".join(parts) + (" · read-only" if processes else "")


def view(inventory, catalog, spec_id=None, *, width=None, now=None):
    services = project(inventory, catalog)
    others = project_others(inventory, catalog)
    output = io.StringIO()
    writer = TerminalWriter(width=width, stream=output)
    writer.emit("Live service inventory")
    writer.field("Observed", observed_text(inventory.get("generated_at"), now))
    commands = []
    if spec_id is None:
        # Catalog services and Pulsar services outside it are the rows; the
        # header names only what this menu cannot act on.
        read_only = [service for service in inventory["services"]
                     if service.get("conf") not in services and service.get("conf") not in others]
        for service in read_only[:3]:
            writer.emit(read_only_line(service), "", "  ")
        if len(read_only) > 3:
            writer.emit(f"and {len(read_only) - 3} more in Show full inventory", "", "  ")
        writer.emit(f"Unmanaged GPU processes {gpu_process_text(inventory)}", "", "  ")
        unavailable = unobserved_hosts(inventory)
        if unavailable:
            writer.field("Not observed", ", ".join(unavailable))
        if not services:
            writer.emit("No catalog services were observed.")
            if unavailable:
                writer.emit("Unobserved nodes may still be running services.")
        # Gum draws only its two-column cursor before each label.
        budget = writer.width - 2
        for key, service in services.items():
            port = f" · port {service['port']}" if service["port"] else ""
            count = node_count(service)
            label = service_label(service["state"], key, service["model_id"],
                                  (f" · {count}{port}", f" · {count}"), budget)
            commands.append(f"service\t{key}\t{label}")
        for key, service in others.items():
            port = f" · port {service['port']}" if service["port"] else ""
            count = node_count(service)
            outside = " · outside the catalog"
            # The served name is this row's identity, so the count and port
            # give way before it is cut below 24 characters.
            label = service_label(service["state"], key, service["model_id"],
                                  (f" · {count}{port}{outside}", f" · {count}{outside}", outside), budget,
                                  rooms=(24, 24, 12))
            commands.append(f"other\t{key}\t{label}")
        question = stop_all_question(inventory, {row["spec_id"]: row for row in catalog})
        if question:
            commands.append(f"stopall\t{clean(question)}")
    elif spec_id in others:
        service = others[spec_id]
        writer.field("Model", service["model_id"])
        writer.field("Spec", spec_id)
        writer.field("State", {"stale": "exited containers remain"}.get(service["state"], service["state"]))
        writer.field("Ownership", service["ownership"])
        writer.field("Nodes observed", service["where"])
        if service["port"]:
            writer.field("API port", service["port"])
        writer.emit("Started outside this catalog, for example by an experiment; Pulsar manages its containers.")
        commands.append(f"model\t{clean(service['model_id'])}")
        # No Detailed status: status needs a catalog spec. Logs needs only the ID.
        commands.append(f"logs\t{spec_id}")
        if service["stop_reason"]:
            writer.emit(service["stop_reason"])
        else:
            # Stop without --node removes the service's containers on every node.
            question = f"{stop_question(service['model_id'], spec_id, service['where'])} {OUTSIDE_CATALOG}"
            commands.extend((f"stop\t{spec_id}\t-", f"confirm\t{clean(question)}"))
    else:
        if spec_id not in services:
            raise ServiceNotObserved("The selected service is no longer observed; returning to the service list.")
        service = services[spec_id]
        writer.field("Model", service["model_id"])
        writer.field("Spec", spec_id)
        writer.field("State", {"stale": "exited containers remain"}.get(service["state"], service["state"]))
        writer.field("Ownership", service["ownership"])
        writer.field("Nodes observed", service["where"])
        if service["port"]:
            writer.field("API port", service["port"])
        if service["modified"]:
            writer.emit("The observed recipe differs from the selected catalog spec.")
        writer.emit("Detailed status uses the existing service and file checks.")
        node = service["node_id"] or "-"
        # Outcomes name the model as well as the spec.
        commands.append(f"model\t{clean(service['model_id'])}")
        commands.append(f"status\t{spec_id}\t{node}")
        # Logs reads every rank's container, so it needs only the spec.
        commands.append(f"logs\t{spec_id}")
        if service["stop_reason"]:
            writer.emit(service["stop_reason"])
        else:
            # The same question as the catalog menu's Stop, naming the observed nodes.
            question = stop_question(service["model_id"], spec_id, service["where"])
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
        from scripts.terminal_format import error_line
        error_line(exc, tag='inventory menu')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
