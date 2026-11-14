#!/usr/bin/env python3
"""The saved records menus read instead of probing: the service inventory, the
storage budget and the cluster changes noticed.

The inventory and budget are the last `inventory --json` and `model budget
--json` results, kept on the controller for menus that must not wait on a probe. Each is an
observation with its age, never current state: the inventory carries its own
`generated_at`, and the budget is stamped when the probe finished, right
before it is saved. Nothing here contacts a node.

    python3 -m scripts.saved_observations save-inventory < INVENTORY_JSON
    python3 -m scripts.saved_observations save-budget < BUDGET_NODES_JSON
    python3 -m scripts.saved_observations save-cluster-changes < CHANGES_BODY_JSON
    python3 -m scripts.saved_observations keep-cluster-changes
    python3 -m scripts.saved_observations save-cluster-check < CHECK_BODY_JSON

The "changes noticed" record (`cluster-changes`) is what the last discovery
against valid saved membership saw, compared with that membership's
`topology_id` (scripts/topology_actions.py changes_noticed); it is stamped
`observed_at` when saved. Recording SSH identities carries an `unchanged`
record to the new `topology_id` (carry-cluster-changes). A record whose
`topology_id` is not the saved membership's describes an earlier membership.

Two optional fields sit beside the changes, each stamped when written and
carried forward by every later save for the same `topology_id`:
`kept` (keep-cluster-changes) is the operator's answer that the members that
did not answer are only down; it is dropped as soon as a discovery sees
different changes, so the question is asked again only then. `check`
(save-cluster-check) is what the last Check the cluster now found on each
node: whether it answered, and its blocking issues and warnings.

The home menu's status and the Cluster area read these records through
scripts/cluster_status.py.

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
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_library.catalog import age_seconds  # noqa: E402
from model_library.catalog_menu import LIVE_FRESH_SECONDS  # noqa: E402
from model_library.integrity import StorageError, atomic_json, directory, read_json  # noqa: E402
from model_library.state import ensure_directory, now as utc_now  # noqa: E402
from scripts.service_status import StatusError, check_inventory  # noqa: E402
from scripts.terminal_format import TerminalWriter, error_line  # noqa: E402

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
# The optional `check` field: each node's answer and findings, and the
# cluster-wide links and SSH identity results.
CHECK_STATES = ("verified", "failed", "not-checked")
SCHEMA_VERSION = 1
BUDGET_SIZES = ("available", "total", "used", "reserve", "limit")

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
    if "kept" in record:
        kept = record["kept"]
        if (not isinstance(kept, dict) or not isinstance(kept.get("signature"), str)
                or not isinstance(kept.get("missing"), list)
                or not all(isinstance(item, str) and item for item in kept["missing"])):
            raise ValueError("the saved cluster changes have an invalid kept answer")
        _stamp(kept.get("kept_at"), "the kept answer's kept_at")
    if "check" in record:
        _check_check(record["check"])
    return record


def _check_check(check: Any) -> dict:
    """The optional `check` field: what the last cluster check found."""
    if not isinstance(check, dict) or not isinstance(check.get("nodes"), list):
        raise ValueError("the saved cluster check has no nodes list")
    _stamp(check.get("checked_at"), "the cluster check's checked_at")
    for key in ("links", "identities"):
        if check.get(key) not in CHECK_STATES:
            raise ValueError(f"the saved cluster check has no valid {key} result")
    for node in check["nodes"]:
        if (not isinstance(node, dict) or not isinstance(node.get("hostname"), str) or not node["hostname"]
                or node.get("answered") not in (True, False, None)):
            raise ValueError("a saved cluster check node has no hostname or answer")
        for key in ("failures", "warnings"):
            if not isinstance(node.get(key), list) or not all(isinstance(item, str) for item in node[key]):
                raise ValueError(f"a saved cluster check node has no {key} list")
    return check


def changes_signature(record: dict) -> str:
    """What a discovery saw, without its time: machines found, members that
    did not answer or were rejected, and members that changed. A kept answer
    stays only while this is the same."""
    seen = {
        "found": sorted(str(item.get("node_id") or item.get("hostname")) for item in record.get("found") or []),
        "missing": sorted(f"{item.get('node_id')}:{bool(item.get('answered'))}" for item in record.get("missing") or []),
        "changed": sorted(json.dumps(item, sort_keys=True) for item in record.get("changed") or []),
    }
    return hashlib.sha256(json.dumps(seen, sort_keys=True).encode()).hexdigest()


def _unreadable_as_none(path: Path) -> dict | None:
    """The saved changes, or None when there are none or they cannot be read."""
    try:
        return load_changes(path)
    except (ValueError, OSError):
        return None


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
        record = {key: value for key, value in body.items() if key not in ("kept", "check")}
        record.update(schema_version=SCHEMA_VERSION, kind=CHANGES_KIND, observed_at=clock())
        # The last check and a kept answer belong to this membership: they stay
        # with it, and a kept answer only while the changes are the same.
        previous = _unreadable_as_none(path)
        if previous is not None and previous["topology_id"] == record.get("topology_id"):
            if "check" in previous:
                record["check"] = previous["check"]
            if "kept" in previous and previous["kept"]["signature"] == changes_signature(record):
                record["kept"] = previous["kept"]
        check_changes_record(record)
        atomic_json(path, record)
    return True


def keep_changes(path: Path, *, clock: Callable[[], str] = utc_now) -> dict:
    """Record that the members that did not answer are only down: the menu
    keeps them and does not ask again until a discovery sees other changes."""
    path = Path(path)
    with record_lock(path):
        record = load_changes(path)
        unanswered = [item["node_id"] for item in (record or {}).get("missing") or [] if not item.get("answered")]
        if record is None or record["result"] != "changed" or not unanswered:
            raise ValueError("the saved cluster changes name no member that did not answer")
        record = {**record, "kept": {"kept_at": clock(), "signature": changes_signature(record),
                                     "missing": unanswered}}
        check_changes_record(record)
        atomic_json(path, record)
    return record


def save_check(body: Any, path: Path, *, clock: Callable[[], str] = utc_now) -> bool:
    """Keep what one cluster check found beside the changes its discovery saved.

    BODY names the membership it checked; a record for another membership,
    or none, cannot hold it."""
    if not isinstance(body, dict) or not isinstance(body.get("topology_id"), str):
        raise ValueError("the cluster check names no topology_id")
    path = Path(path)
    with record_lock(path):
        record = load_changes(path)
        if record is None or record["topology_id"] != body["topology_id"]:
            raise ValueError("the saved cluster changes describe another membership")
        check = {key: value for key, value in body.items() if key != "topology_id"}
        check["checked_at"] = clock()
        record = {**record, "check": _check_check(check)}
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
    sub.add_parser("keep-cluster-changes",
                   help="keep the members that did not answer: they are only down")
    sub.add_parser("save-cluster-check", help="save one cluster check's findings from stdin").add_argument(
        "--path", help="save here instead of the default path")
    carry = sub.add_parser("carry-cluster-changes",
                           help="after SSH enrollment: carry an unchanged record to the new topology_id")
    carry.add_argument("--from", dest="old", required=True)
    carry.add_argument("--to", dest="new", required=True)
    for command in sub.choices.values():
        command.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if args.command == "save-inventory":
        return _save(args, INVENTORY_NAME, "the service inventory", save_inventory)
    if args.command == "save-budget":
        return _save(args, BUDGET_NAME, "the storage budget", save_budget)
    if args.command == "save-cluster-changes":
        return _save(args, CHANGES_NAME, "the cluster changes noticed", save_changes)
    if args.command == "save-cluster-check":
        return _save(args, CHANGES_NAME, "the cluster check", save_check)
    if args.command == "keep-cluster-changes":
        # The menu asked; a record that cannot hold the answer is an error it shows.
        try:
            keep_changes(record_path(args.repo_root, CHANGES_NAME))
        except (ValueError, OSError) as exc:
            error_line(exc, tag="saved observations")
            return 1
        return 0
    if args.command == "carry-cluster-changes":
        # Like a save, this never fails its caller: a problem is one warning.
        path: object = os.environ.get(CHANGES_ENV, "")
        try:
            path = record_path(args.repo_root, CHANGES_NAME)
            carry_changes(path, args.old, args.new)
        except (ValueError, OSError) as exc:
            _warning("the cluster changes noticed", path, exc)
        return 0
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
