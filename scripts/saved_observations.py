#!/usr/bin/env python3
"""The saved service inventory and saved storage budget, and the home menu's Running block.

They are the last `inventory --json` and `model budget --json` results, kept
on the controller for menus that must not wait on a probe. Each is an
observation with its age, never current state: the inventory carries its own
`generated_at`, and the budget is stamped when the probe finished, right
before it is saved. Nothing here contacts a node.

    python3 -m scripts.saved_observations save-inventory < INVENTORY_JSON
    python3 -m scripts.saved_observations save-budget < BUDGET_NODES_JSON
    python3 -m scripts.saved_observations save-cluster-changes < CHANGES_BODY_JSON
    python3 -m scripts.saved_observations home [--now TIME] [--width N] < CATALOG_JSON

The "changes noticed" record (`cluster-changes`) is what the last discovery
against valid saved membership saw, compared with that membership's
`topology_id` (scripts/topology_actions.py changes_noticed); it is stamped
`observed_at` when saved. Recording SSH identities carries an `unchanged`
record to the new `topology_id` (carry-cluster-changes). A record whose
`topology_id` is not the saved membership's describes an earlier membership.

`home` prints `key<TAB>KEY` on its first line, then the Running block. Both
come from one read of each record at one instant, so the home menu compares
exactly the records it printed; KEY changes only with a new observation or the
inventory turning "last seen".

The records live under the checkout's `.pulsar/` directory, which is never
tracked; PULSAR_SERVICE_INVENTORY_FILE, PULSAR_STORAGE_BUDGET_FILE and
PULSAR_CLUSTER_CHANGES_FILE name other absolute paths. Saving never fails its caller: a record that cannot be
saved prints one warning on stderr and exits 0.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_library.catalog import age_seconds, age_text  # noqa: E402
from model_library.catalog_menu import LIVE_FRESH_SECONDS, SERVICE_STATES, live_inventory  # noqa: E402
from model_library.integrity import StorageError, atomic_json, directory, read_json  # noqa: E402
from model_library.node_names import NodeNames  # noqa: E402
from model_library.render import can_still_copy, short_size  # noqa: E402
from model_library.state import ensure_directory, now as utc_now  # noqa: E402
from scripts.inventory_menu import gpu_process_parts, observed_text, project_others  # noqa: E402
from scripts.service_status import StatusError, check_inventory  # noqa: E402
from scripts.terminal_format import TerminalWriter, cut_middle, error_line  # noqa: E402

INVENTORY_ENV = "PULSAR_SERVICE_INVENTORY_FILE"
BUDGET_ENV = "PULSAR_STORAGE_BUDGET_FILE"
CHANGES_ENV = "PULSAR_CLUSTER_CHANGES_FILE"
INVENTORY_NAME = "service-inventory"
BUDGET_NAME = "storage-budget"
CHANGES_NAME = "cluster-changes"
ENVIRONMENT = {INVENTORY_NAME: INVENTORY_ENV, BUDGET_NAME: BUDGET_ENV, CHANGES_NAME: CHANGES_ENV}
BUDGET_KIND = "pulsar-saved-storage-budget"
CHANGES_KIND = "pulsar-cluster-changes"
CHANGE_RESULTS = ("unchanged", "changed", "not-checked")
CHANGE_LISTS = ("issues", "found", "missing", "changed", "untrusted")
SCHEMA_VERSION = 1
BUDGET_SIZES = ("available", "total", "used", "reserve", "limit")

# The choices that refresh the records, as the home menu names them.
OVERVIEW = "**Cluster overview (checks nodes now)**"
MODEL_FILES = "**Model files and disk space (checks nodes now)**"

# The status words the Running block prints. Each has a row under Catalog
# labels in docs/OPERATIONS.md; tests/test_help.py checks it.
HOME_WORDS = {
    "not-observed-yet": "not observed yet",
    "none-running": "none running",
    "unknown": "unknown",
    "others-unknown": "others unknown",
    "last-seen": "last seen",
    "not-current": "not current",
    "outside": "outside the catalog",
    "gpu-memory": "GPU memory",
    "unreadable": "unreadable",
    "available": "available of",
    "node-not-observed": "not observed",
    "memory-not-measured": "memory not measured",
    "gpu-work": "Pulsar did not start",
    "can-copy": "can still copy",
    "free": "free",
    "past": "past its reserve or copy budget",
    "headroom": "headroom not observed",
    "modified": "modified recipe",
}

# A no-break space keeps a number with its unit, and a " · " separator with
# the part after it, so a narrow terminal wraps between parts, never inside
# one. TerminalWriter never breaks there; home_block prints it as a space.
KEEP = "\u00a0"


def record_path(repo_root: str | Path, name: str) -> Path:
    """Where the record NAME is kept: its environment override, else `.pulsar/NAME.json`."""
    variable = ENVIRONMENT[name]
    override = os.environ.get(variable)
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise StorageError(f"{variable} must be an absolute path")
        # Saved files are opened without following links; the chosen
        # directory may itself be an alias, as the checkout may.
        return Path(os.path.realpath(path.parent)) / path.name
    return Path(os.path.realpath(repo_root)) / ".pulsar" / f"{name}.json"


def _stamp(value: Any, what: str) -> datetime:
    """A saved UTC time, parsed; a time without a timezone is refused."""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be UTC text")
    age_seconds(value)  # raises for an invalid time or one without a timezone
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def check_inventory_document(document: Any) -> dict:
    """An `inventory --json` document with the structure the menus read, or ValueError."""
    if (not isinstance(document, dict) or type(document.get("schema_version")) is not int
            or document["schema_version"] != SCHEMA_VERSION):
        raise ValueError("the service inventory is not a schema-1 inventory document")
    try:
        check_inventory(document)
    except StatusError as exc:
        raise ValueError(str(exc)) from exc
    _stamp(document.get("generated_at"), "the inventory's generated_at")
    return document


def check_budget_nodes(nodes: Any) -> list:
    """The `model budget` nodes list: a node_id and whole-byte sizes for each node."""
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("the storage budget names no node")
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("node_id"), str) or not node["node_id"]:
            raise ValueError("a storage budget node has no node_id")
        for key in BUDGET_SIZES:
            if type(node.get(key)) is not int:
                raise ValueError(f"storage budget node {node['node_id'][:12]} has no whole-byte {key}")
    return nodes


def check_budget_record(record: Any) -> dict:
    if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
            or record["schema_version"] != SCHEMA_VERSION or record.get("kind") != BUDGET_KIND):
        raise ValueError(f"the saved storage budget is not a schema-1 {BUDGET_KIND} record")
    _stamp(record.get("saved_at"), "the budget's saved_at")
    check_budget_nodes(record.get("nodes"))
    return record


def check_changes_record(record: Any) -> dict:
    """A saved "changes noticed" record: what one discovery saw against the
    saved membership `topology_id`, stamped `observed_at` when it was saved."""
    if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
            or record["schema_version"] != SCHEMA_VERSION or record.get("kind") != CHANGES_KIND):
        raise ValueError(f"the saved cluster changes are not a schema-1 {CHANGES_KIND} record")
    _stamp(record.get("observed_at"), "the changes' observed_at")
    if not isinstance(record.get("topology_id"), str) or not record["topology_id"]:
        raise ValueError("the saved cluster changes name no topology_id")
    if record.get("result") not in CHANGE_RESULTS:
        raise ValueError("the saved cluster changes have no valid result")
    for key in CHANGE_LISTS:
        if not isinstance(record.get(key), list):
            raise ValueError(f"the saved cluster changes have no {key} list")
    return record


def load_changes(path: Path) -> dict | None:
    """The saved changes record; None when none is saved. A present but unreadable file raises."""
    if not os.path.lexists(path):
        return None
    return check_changes_record(read_json(path))


def save_changes(body: Any, path: Path, *, clock: Callable[[], str] = utc_now) -> bool:
    """Save one discovery's BODY (scripts/topology_actions.py changes_noticed),
    stamped with the time it is saved, under the record's lock."""
    if not isinstance(body, dict):
        raise ValueError("the cluster changes must be an object")
    path = Path(path)
    ensure_directory(path.parent)
    with record_lock(path):
        record = {**body, "schema_version": SCHEMA_VERSION, "kind": CHANGES_KIND, "observed_at": clock()}
        check_changes_record(record)
        atomic_json(path, record)
    return True


def carry_changes(path: Path, old_topology_id: str, new_topology_id: str) -> bool:
    """Recording SSH identities changes the membership's topology_id without
    changing its nodes. An `unchanged` record that names OLD_TOPOLOGY_ID is
    carried forward to NEW_TOPOLOGY_ID, keeping its observed_at; any other
    record is left exactly as it is. True when the record was rewritten."""
    path = Path(path)
    if not os.path.lexists(path):
        return False
    with record_lock(path):
        record = load_changes(path)
        if record is None or record["topology_id"] != old_topology_id or record["result"] != "unchanged":
            return False
        record = check_changes_record({**record, "topology_id": new_topology_id})
        atomic_json(path, record)
    return True


def load_inventory(path: Path) -> dict | None:
    """The saved inventory; None when none is saved. A present but unreadable file raises."""
    if not os.path.lexists(path):
        return None
    return check_inventory_document(read_json(path))


def load_budget(path: Path) -> dict | None:
    """The saved budget record; None when none is saved. A present but unreadable file raises."""
    if not os.path.lexists(path):
        return None
    return check_budget_record(read_json(path))


@contextmanager
def record_lock(path: Path) -> Iterator[None]:
    """Hold the record's own lock, `<record>.lock` beside it, exclusively.

    Saves of one record take turns, so the compare and the write of one save
    are never interleaved with another's. The directory must exist.
    """
    path = Path(path)
    with directory(path.parent) as parent:
        descriptor = os.open(f"{path.name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise StorageError(f"{path.name}.lock is not a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)


def save_inventory(document: Any, path: Path) -> bool:
    """Save DOCUMENT unless the saved one is valid and strictly newer; True when written.

    Two inventories can overlap, so the newer observation is kept; the
    record's lock covers the compare and the write. A saved file that is
    unreadable or invalid is replaced, never compared, so it can never block
    saving.
    """
    check_inventory_document(document)
    path = Path(path)
    ensure_directory(path.parent)
    with record_lock(path):
        try:
            saved = load_inventory(path)
        except (ValueError, OSError):
            saved = None
        if saved is not None and _stamp(saved["generated_at"], "saved") > _stamp(document["generated_at"], "new"):
            return False
        atomic_json(path, document)
    return True


def save_budget(nodes: Any, path: Path, *, clock: Callable[[], str] = utc_now) -> bool:
    """Save the budget NODES with the time the probe finished, taken now.

    The stamp is taken under the record's lock, right before the write, so
    the last budget saved is also the newest.
    """
    check_budget_nodes(nodes)
    path = Path(path)
    ensure_directory(path.parent)
    with record_lock(path):
        record = {"schema_version": SCHEMA_VERSION, "kind": BUDGET_KIND, "saved_at": clock(), "nodes": nodes}
        check_budget_record(record)
        atomic_json(path, record)
    return True


def _fresh(inventory: dict, now=None) -> bool:
    age = age_seconds(inventory["generated_at"], now)
    return age is not None and age <= LIVE_FRESH_SECONDS


def change_key(inventory: dict | None, budget: dict | None, now=None) -> str:
    """Changes only with a new observation or the inventory turning "last seen".

    Pass the NOW the block was rendered at, so the two agree on "last seen".
    """
    now = now or datetime.now(timezone.utc)
    inventory_key = "none" if inventory is None else (
        f"{inventory['generated_at']} {'fresh' if _fresh(inventory, now) else 'last-seen'}")
    budget_key = "none" if budget is None else budget["saved_at"]
    return f"{inventory_key} {budget_key}"


def _kept(*words: object) -> str:
    return KEEP.join(str(word) for word in words)


def _part(text: str) -> str:
    """ · TEXT, kept with the separator so a wrapped line starts with it."""
    return f" ·{KEEP}{text}"


def _size(value: int | None) -> str:
    return short_size(value).replace(" ", KEEP)


def _host(node: dict, key: str) -> str:
    return " ".join(str(node.get("hostname") or node.get("node_id") or key).split())


def _node_order(item: tuple[str, Any]) -> tuple[int, str]:
    """inventory.sh's node order: head, worker, then rank-N."""
    name = item[0]
    if name == "head":
        return (0, name)
    if name == "worker":
        return (1, name)
    if name.startswith("rank-") and name[5:].isdigit():
        return (int(name[5:]), name)
    return (1_000_000, name)


def _confirmed_nodes(inventory: dict) -> list[tuple[str, dict]]:
    """Membership nodes in order; a one-node cluster's placeholder worker is left out."""
    return [(key, node) for key, node in sorted(inventory["nodes"].items(), key=_node_order)
            if isinstance(node, dict) and (key == "head" or node.get("confirmed"))]


def _gpu_mib(service: dict) -> int | None:
    """The service's measured GPU memory, when every rank measured it."""
    ranks = [rank for rank in service.get("ranks") or [] if isinstance(rank, dict)]
    values = [(rank.get("gpu_memory") or {}).get("measured_mib") for rank in ranks]
    if not values or any(type(value) is not int or value < 0 for value in values):
        return None
    return sum(values)


def _service_rows(inventory: dict, entries: list[dict], room: int, now=None) -> list[str]:
    """One row per Pulsar service: catalog specs first, then those outside it."""
    live = live_inventory(inventory, entries, now=now)
    catalog = [{"spec_id": row["spec_id"], "model_id": row["model_id"], "nodes": row["geometry"]["nodes"]}
               for row in entries]
    others = project_others(inventory, catalog)
    fresh = live["age"] is not None and live["age"] <= LIVE_FRESH_SECONDS
    # The first group for each spec, as project_others takes it.
    by_conf: dict = {}
    for service in inventory["services"]:
        by_conf.setdefault(service.get("conf"), service)
    rows = []
    for outside, services in ((False, live["services"]), (True, others)):
        for spec_id, service in services.items():
            state = SERVICE_STATES.get(service["state"], service["state"])
            where = ", ".join(service["hosts"]) or "nodes not identified"
            text = cut_middle(service["model_id"], room)
            text += _part(f"{'' if fresh else 'last seen '}{state} on {where}")
            # A catalog service started with execution overrides runs a
            # modified recipe; one outside the catalog has no spec to differ from.
            if not outside and service.get("modified"):
                text += _part(_kept("modified", "recipe"))
            if service.get("port"):
                text += _part(_kept("port", service["port"]))
            mib = _gpu_mib(by_conf.get(spec_id) or {})
            if mib:
                text += _part(_kept(_size(mib * 1024 ** 2), "GPU", "memory"))
            if outside:
                text += _part(_kept("outside", "the", "catalog"))
            rows.append(text)
    unobserved = ", ".join(live["unobserved"])
    if not rows:
        if not live["incomplete"]:
            rows.append("none running")
        else:
            rows.append(f"unknown; {unobserved} not observed" if unobserved
                        else "unknown; a node could not be observed")
    elif live["incomplete"]:
        # Unknown is not absent: a node that did not answer may run more.
        rows.append(f"others unknown; {unobserved} not observed" if unobserved
                    else "others unknown; a node could not be observed")
    return rows


def _memory_rows(inventory: dict) -> list[str]:
    rows = []
    for key, node in _confirmed_nodes(inventory):
        host = _host(node, key)
        available, total = node.get("mem_available_gib"), node.get("mem_total_gib")
        if node.get("probe_status") != "ok":
            reason = " ".join(str(node.get("probe_reason") or node.get("probe_status") or "no answer").split())
            rows.append(f"{host} not observed: {reason}")
        elif isinstance(available, (int, float)) and not isinstance(available, bool):
            text = f"{host} {_kept(f'{available:.0f}', 'GiB')} available"
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                text += f" {_kept('of', f'{total:.0f}')}"
            rows.append(text)
        else:
            rows.append(f"{host} memory not measured")
    return rows


def _gpu_work(inventory: dict) -> str | None:
    count, hosts = gpu_process_parts(inventory)
    if not count:
        return None
    text = f"{count} process{'' if count == 1 else 'es'} Pulsar did not start"
    return text + "".join(_part(f"{_size(size)} on {host}") for host, size in hosts)


def _disk_rows(budget: dict, inventory: dict | None, hostnames: dict[str, str]) -> list[str]:
    """One row per budget node, in the inventory's node order where it knows the node."""
    named: dict[str, str] = {}
    order: list[str] = []
    if inventory is not None:
        for key, node in _confirmed_nodes(inventory):
            if isinstance(node.get("node_id"), str) and node["node_id"]:
                named[node["node_id"]] = _host(node, key)
                order.append(node["node_id"])
    nodes = {node["node_id"]: node for node in budget["nodes"]}
    ordered = [node_id for node_id in order if node_id in nodes]
    ordered += [node_id for node_id in nodes if node_id not in ordered]
    rows = []
    for node_id in ordered:
        node = nodes[node_id]
        host = named.get(node_id) or hostnames.get(node_id) or node_id[:12]
        free = _part(_kept(_size(node.get("available")), "free"))
        left = can_still_copy(node)
        if left is None:
            rows.append(f"{host} headroom not observed{free}")
        elif left < 0:
            rows.append(f"{host} {_size(-left)} past its reserve or copy budget{free}")
        else:
            rows.append(f"{host} can still copy {_size(left)}{free}")
    return rows


def home_block(inventory: dict | None, budget: dict | None, entries: list[dict], *, width: int | None = None,
               now=None, unreadable: frozenset[str] | set[str] = frozenset(),
               hostnames: dict[str, str] | None = None) -> str:
    """The home menu's Running block from the saved records, with their ages.

    UNREADABLE names a record ("inventory", "budget") whose file could not
    be read; its row says so. HOSTNAMES names budget nodes the inventory does
    not, from the saved topology. Every age is taken at one NOW, the current
    time by default. Nothing here probes a node.
    """
    now = now or datetime.now(timezone.utc)
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    hostnames = hostnames or {}
    if inventory is not None:
        try:
            services = _service_rows(inventory, entries, out.width - 12, now)
        except ValueError:
            inventory, services = None, []
            unreadable = {*unreadable, "inventory"}
    if inventory is None and budget is None and not unreadable:
        out.field("Running", f"{HOME_WORDS['not-observed-yet']}; {OVERVIEW} probes the nodes and saves what it "
                  "sees", label_role="heading", markup=True)
        return output.getvalue().replace(KEEP, " ")

    def rows(label: str, values: list[str], markup: bool = False) -> None:
        for index, value in enumerate(values):
            out.field(label if index == 0 else "", value, indent=2, markup=markup)

    out.emit("Running", role="heading")
    if inventory is not None:
        rows("Services", services)
        rows("Memory", _memory_rows(inventory))
        work = _gpu_work(inventory)
        if work:
            rows("GPU work", [work])
    elif "inventory" in unreadable:
        rows("Services", [f"saved inventory unreadable; {OVERVIEW} replaces it"], markup=True)
    else:
        rows("Services", ["not observed yet"])
        rows("Memory", ["not observed yet"])
    if budget is not None:
        rows("Disk", _disk_rows(budget, inventory, hostnames))
    elif "budget" in unreadable:
        rows("Disk", [f"saved budget unreadable; {OVERVIEW} or {MODEL_FILES} replaces it"], markup=True)
    else:
        rows("Disk", [f"not observed yet; {OVERVIEW} or {MODEL_FILES} reads it"], markup=True)
    ages = []
    if inventory is not None:
        ages.append("services " + observed_text(inventory["generated_at"], now).replace(" UTC)", KEEP + "UTC)"))
    if budget is not None:
        ages.append("disk " + age_text(age_seconds(budget["saved_at"], now)))
    if ages:
        stale = "; last seen, not current" if inventory is not None and not _fresh(inventory, now) else ""
        rows("Observed", [f"{' · '.join(ages)}{stale}; {OVERVIEW} rechecks"], markup=True)
    return output.getvalue().replace(KEEP, " ")


def _warning(what: str, path: object, reason: object, stream=None) -> None:
    """One `warning: …` line; its label is styled only on a terminal, as error_line's is."""
    stream = stream or sys.stderr
    label = "warning:"
    try:
        terminal = stream.isatty()
    except (AttributeError, ValueError):
        terminal = False
    if terminal:
        label = TerminalWriter(stream=stream).paint(label, ["warn"] * len(label))
    print(f"{label} {what} was not saved to {path}: {' '.join(str(reason).split())}", file=stream)


def _save(args: argparse.Namespace, name: str, what: str, save: Callable[[Any, Path], bool]) -> int:
    """Save stdin's document; any failure is one warning, and the caller's status is unchanged."""
    # Only a relative override is refused before the path is known; the warning names it.
    path: object = os.environ.get(ENVIRONMENT[name], "")
    try:
        path = Path(args.path) if args.path else record_path(args.repo_root, name)
        save(json.load(sys.stdin), path)
    except (ValueError, OSError) as exc:
        _warning(what, path, exc)
    return 0


def _read(loader: Callable[[Path], dict | None], repo_root: Path, name: str) -> tuple[dict | None, bool]:
    """A saved record and whether it was unreadable."""
    try:
        return loader(record_path(repo_root, name)), False
    except (ValueError, OSError):
        return None, True


def _instant(value: str) -> datetime:
    """A --now value: an ISO 8601 time with its timezone, such as 2026-11-05T22:00:00Z."""
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid time {value!r}; use UTC, such as 2026-11-05T22:00:00Z") from exc
    if stamp.tzinfo is None:
        raise argparse.ArgumentTypeError(f"time {value!r} has no timezone; use UTC, such as 2026-11-05T22:00:00Z")
    return stamp.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for command, text in (("save-inventory", "save an inventory --json document from stdin"),
                          ("save-budget", "save the model budget nodes list from stdin"),
                          ("save-cluster-changes", "save one discovery's changes noticed from stdin")):
        save = sub.add_parser(command, help=text)
        save.add_argument("--path", help="save here instead of the default path")
    carry = sub.add_parser("carry-cluster-changes",
                           help="after SSH enrollment: carry an unchanged record to the new topology_id")
    carry.add_argument("--from", dest="old", required=True)
    carry.add_argument("--to", dest="new", required=True)
    home = sub.add_parser("home", help="a `key\\tKEY` line, then the Running block; catalog JSON on stdin")
    home.add_argument("--width", type=int)
    home.add_argument("--now", type=_instant, help="the time ages are taken at (UTC); the current time by default")
    for command in sub.choices.values():
        command.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if args.command == "save-inventory":
        return _save(args, INVENTORY_NAME, "the service inventory", save_inventory)
    if args.command == "save-budget":
        return _save(args, BUDGET_NAME, "the storage budget", save_budget)
    if args.command == "save-cluster-changes":
        return _save(args, CHANGES_NAME, "the cluster changes noticed", save_changes)
    if args.command == "carry-cluster-changes":
        # Like a save, this never fails its caller: a problem is one warning.
        path: object = os.environ.get(CHANGES_ENV, "")
        try:
            path = record_path(args.repo_root, CHANGES_NAME)
            carry_changes(path, args.old, args.new)
        except (ValueError, OSError) as exc:
            _warning("the cluster changes noticed", path, exc)
        return 0
    # One read of each record and one instant give both the block and its
    # change key, so a save while the menu draws can never split them.
    now = args.now or datetime.now(timezone.utc)
    inventory, inventory_unreadable = _read(load_inventory, args.repo_root, INVENTORY_NAME)
    budget, budget_unreadable = _read(load_budget, args.repo_root, BUDGET_NAME)
    try:
        entries = json.load(sys.stdin).get("entries") or []
        unreadable = {name for name, flag in (("inventory", inventory_unreadable), ("budget", budget_unreadable))
                      if flag}
        block = home_block(inventory, budget, entries, width=args.width, now=now, unreadable=unreadable,
                           hostnames=NodeNames.saved(args.repo_root).hostnames)
        sys.stdout.write(f"key\t{change_key(inventory, budget, now)}\n{block}")
        return 0
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        error_line(exc, tag="saved observations")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
