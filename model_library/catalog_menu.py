"""Interactive catalog menu decisions from saved records only.

Decides which operations the catalog menu offers for one recipe, which it
leaves out and why, one suggested next step (also as the command that runs
it), the recipe list labels, the required-snapshot choices, the summary that
leads each storage preview, the wording of confirmation questions, and the
home screen's Next line. Input
is the catalog projection (``pulsar models show --json``) and the
archive-location status, plus what this menu session observed about services:
a service inventory the menu captured on request and its own Live status, Start
and Stop outcomes. Nothing here probes nodes. Leaving an operation out
is a menu convenience: every operation still enforces its own preconditions
and remains available from the CLI.

Output for the Bash menu is tab-separated lines; the menu never parses JSON
itself.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import sys
from typing import Any, NamedTuple

from .catalog import (NOT_CHECKED, ROOT, age_seconds, age_text, archive_facts, archive_text, blocker_text, files_text,
                      recipe_text)
from .published_results import collect as published_results, frame_line as published_line
from .node_names import NodeNames
from .render import (HOME_REMOVAL_RECOVERY, blocker_detail_text, blocker_message, preparation_estimate,
                     projected_headroom, short_size, size_text)
from scripts.terminal_format import TerminalWriter, cut_middle, terminal_width

MAIN = ("check", "acquire", "restore", "prepare", "start", "status", "observe", "logs", "stop")
LAUNCH = ("readiness", "image-check", "image-stage")
STORAGE = ("audit", "move", "archive", "verify", "pin", "unpin", "purge", "remove")
LABELS = {
    "check": "Check now", "acquire": "Download", "restore": "Restore", "prepare": "Prepare",
    "start": "Start", "stop": "Stop", "status": "Live status", "observe": "Verify running service",
    "logs": "Service logs",
    "readiness": "Check launch prerequisites", "image-check": "Check serving image",
    "image-stage": "Stage serving image",
    "audit": "Full file audit (slow; hashes every file)",
    "move": "Move home", "archive": "Create archive", "verify": "Verify archive",
    "pin": "Pin prepared copies", "unpin": "Unpin prepared copies",
    "purge": "Purge prepared copies", "remove": "Remove home",
}
# What each operation does, shown after its label where the terminal is wide enough.
EFFECTS = {
    "check": "refresh saved file records", "acquire": "fetch model files to one node",
    "restore": "copy files back from the archive", "prepare": "copy files to every serving node",
    "start": "launch the service; asks first", "status": "check every node for it now",
    "observe": "recheck its recipe, containers and files", "logs": "recent output of its containers",
    "stop": "remove it if running; files are kept",
    "readiness": "dry run of Start's checks", "image-check": "is the exact digest on every node?",
    "image-stage": "pull or copy the exact digest",
    "audit": "read-only; can take many minutes",
    "move": "move the home to another node",
    "archive": "save a verified recovery copy", "verify": "recheck the archive's contents",
    "pin": "protect from purge", "unpin": "allow purge again",
    "purge": "delete prepared copies; the home is kept", "remove": "delete the home; no archive required",
}
# Operations that change model files or records; a success makes the saved
# check out of date until the next Check now. Download, Restore and Prepare
# verify what they record, so after them the next lifecycle step is suggested
# instead (see suggestion).
MUTATIONS = frozenset({"acquire", "restore", "prepare", "move", "archive", "pin", "unpin", "purge", "remove"})
INTERRUPTIONS = frozenset({"storage-interrupted", "service-interrupted", "image-interrupted", "check-interrupted"})
STALE_SECONDS = 24 * 3600
ARCHIVE_LOCATION = {"configured", "disabled", "not-configured"}
# Why Start is left out when the catalog says this Stack cannot start the spec,
# by its start_unsupported_reason (a start blocker code).
START_UNSUPPORTED = {"guard_unsupported": "ordinary start cannot enforce the spec's serving guard",
                     "historical_spec": "historical schema-1 specs cannot be started"}


# A session observation of a service older than this is shown as "last seen",
# never as current.
LIVE_FRESH_SECONDS = 10 * 60
SERVICE_STATES = {"running": "running", "partial": "partly running", "degraded": "degraded",
                  "stale": "exited containers", "stopped": "stopped containers"}
SOURCES = {"inventory": "service inventory", "status": "Live status", "stop": "Stop"}


def live_inventory(inventory: dict, rows: list[dict], *, now=None) -> dict:
    """A captured service inventory, joined to catalog specs by exact spec ID.

    Only a complete probe can say a spec is not running, so confirmed nodes
    the inventory could not observe are kept with it.
    """
    from scripts.inventory_menu import project
    from scripts.service_status import unobserved_nodes
    catalog = [{"spec_id": row["spec_id"], "model_id": row["model_id"], "nodes": row["geometry"]["nodes"]}
               for row in rows]
    services = project(inventory, catalog)
    unobserved = [clean(node["node"]) for node in unobserved_nodes(inventory["nodes"])]
    worker = inventory.get("worker") or {}
    # As status reads it: a failed node probe summary leaves the state unknown
    # even when no node record names the node.
    blind = not unobserved and worker.get("status") not in ("ok", "unset")
    return {"age": age_seconds(inventory.get("generated_at"), now), "services": services,
            "unobserved": unobserved, "incomplete": bool(unobserved) or blind}


def service_outcomes(values: list[str], *, now=None, names: NodeNames | None = None) -> dict[str, tuple]:
    """SPEC:ACTION:STATUS:EPOCH[:NODE] values from this menu session, by spec.

    STATUS is the exit status or ``interrupted``; the age is in seconds. NODE
    is the node_id the command was scoped to, shown by its saved hostname.
    """
    names = names or NodeNames()
    current = int((now or datetime.now(timezone.utc)).timestamp())
    outcomes = {}
    for value in values:
        # A node_id may itself contain colons, so it is the remainder.
        spec, action, status, at, node = (value.split(":", 4) + ["", "", "", ""])[:5]
        if action not in ("start", "stop", "status") or not at.isdigit() or not status:
            raise ValueError("service outcomes must use SPEC:ACTION:STATUS:EPOCH[:NODE]")
        host = ((names.hostnames or {}).get(node) or node) if node else None
        outcomes[spec] = (action, status, max(0, current - int(at)), host)
    return outcomes


def _outcome_fact(action: str, status: str, age: int, node: str | None = None) -> dict | None:
    """What one Live status, Start or Stop outcome established about the service."""
    if status == "2":
        return None  # Rejected before any action: earlier observations still hold.
    if status == "interrupted":
        return {"kind": "changed", "action": action, "age": age} if action != "status" else None
    if action == "status":
        # Exit 0 means a service exists in some state, not that it is running.
        # Absence is global: status falls back to the whole inventory.
        kind = {"0": "exists", "1": "absent", "3": "unknown"}.get(status)
        return {"kind": kind, "age": age, "source": "status", "unobserved": []} if kind else None
    if action == "stop" and status == "0":
        if node:
            # A one-node Stop removes only that node's containers.
            return {"kind": "changed", "action": action, "age": age, "stopped_on": node}
        return {"kind": "absent", "age": age, "source": "stop"}
    return {"kind": "changed", "action": action, "age": age}


def live_fact(row: dict, live: dict | None, outcome: tuple | None) -> dict | None:
    """The newest session observation of this spec's service, or None."""
    fact = None
    if live is not None:
        service = live["services"].get(row["spec_id"])
        if service:
            fact = {"kind": "service", "state": service["state"], "hosts": service["hosts"],
                    "node_id": service.get("node_id"), "modified": service["modified"],
                    "age": live["age"], "source": "inventory"}
        elif live["incomplete"]:
            fact = {"kind": "unknown", "unobserved": live["unobserved"], "age": live["age"], "source": "inventory"}
        else:
            fact = {"kind": "absent", "age": live["age"], "source": "inventory"}
    if outcome is not None and (fact is None or fact["age"] is None or outcome[2] <= fact["age"]):
        fact = _outcome_fact(*outcome) or fact
    return fact


def live_current(fact: dict) -> bool:
    return fact["age"] is not None and fact["age"] <= LIVE_FRESH_SECONDS


def _service_words(fact: dict, *, hosts: bool) -> str:
    state = SERVICE_STATES.get(fact["state"], fact["state"])
    if hosts:
        where = ", ".join(fact["hosts"]) or "nodes not identified"
    else:
        count = len(fact["hosts"])
        where = f"{count} node{'' if count == 1 else 's'}" if count else "nodes not identified"
    return f"{state} on {where}" + (" (modified recipe)" if fact["modified"] else "")


def live_label(fact: dict | None) -> str | None:
    """A few words for a recipe label when a service was seen; None otherwise."""
    if fact is None or fact["kind"] not in ("service", "exists"):
        return None
    if fact["kind"] == "exists":
        return "service exists" if live_current(fact) else "service seen " + age_text(fact["age"])
    if live_current(fact):
        return _service_words(fact, hosts=False)
    return f"last seen {SERVICE_STATES.get(fact['state'], fact['state'])} {age_text(fact['age'])}"


def live_line(fact: dict | None, *, read_only: bool = False) -> str:
    """The recipe frame's Live service line: what was seen, by what, and when."""
    if fact is None:
        if read_only:
            return "Live service: not observed here."
        return "Live service: not observed in this menu session; Live status checks it."
    age, current = age_text(fact["age"]), live_current(fact)
    if fact["kind"] == "changed" and fact.get("stopped_on"):
        return f"Live service: stopped on {fact['stopped_on']}; other nodes not observed since (Stop, {age})."
    if fact["kind"] == "changed":
        return f"Live service: not observed since {LABELS[fact['action']]} ({age})."
    when = f"({SOURCES[fact['source']]}, {age})"
    if fact["kind"] == "service":
        words = _service_words(fact, hosts=True)
        return f"Live service: {words} {when}." if current else f"Live service: last seen {words} {when}."
    if fact["kind"] == "exists":
        return f"Live service: {'a service exists' if current else 'a service was found'} {when}."
    if fact["kind"] == "absent":
        return f"Live service: {'not running' if current else 'was not running'} on any node {when}."
    unobserved = ", ".join(fact["unobserved"])
    return (f"Live service: unknown; {unobserved} {'was' if len(fact['unobserved']) == 1 else 'were'} "
            f"not observed {when}." if unobserved else f"Live service: unknown; a node could not be observed {when}.")


def live_value(fact: dict | None, *, read_only: bool = False) -> str:
    """The recipe frame's Running? field: live_line's words without its label."""
    if fact is None:
        return "not checked here" if read_only else "not checked here; choose Live status"
    return live_line(fact).removeprefix("Live service: ").rstrip(".")


def start_unsupported(row: dict) -> str | None:
    """The reason Start is left out, or None when this Stack can start the spec."""
    if row.get("start_supported") is not False:
        return None
    return START_UNSUPPORTED.get(row.get("start_unsupported_reason"), "this Stack cannot start this spec")


def members(row: dict) -> dict[str | None, dict]:
    """Required snapshots by name; a schema-2 spec has one unnamed snapshot."""
    if isinstance(row.get("snapshots"), dict):
        return row["snapshots"]
    return {None: {"home": row.get("home"), "archive": row.get("archive"),
                   "model_id": row["model_id"], "model_commit": row["snapshot_revision"],
                   "snapshot_manifest_id": row.get("snapshot_manifest_id")}}


def found_files(path: str | None) -> dict[str, list[str]]:
    """Homes the Model files scan saw on a node with no record naming them.

    PATH is the scan this menu session saved (``model files --json``). The
    result maps each snapshot_manifest_id to the node_ids holding such a
    home folder, in scan order. A folder a home record or any spec's
    prepared-copy binding names is in Pulsar's records, as on the Model files
    page. A missing, failed or unreadable scan maps nothing: it is a hint for
    the menu, never a gate.
    """
    if not path:
        return {}
    from .file_inventory import read_inventory
    from .integrity import StorageError
    found: dict[str, list[str]] = {}
    try:
        for snapshot in read_inventory(Path(path)).get("snapshots") or []:
            for location in snapshot.get("locations") or []:
                if (location.get("kind") == "home" and location.get("state") == "present"
                        and not location.get("registered_home") and not location.get("bindings")
                        and location.get("node_id")):
                    nodes = found.setdefault(str(snapshot["snapshot_manifest_id"]), [])
                    if str(location["node_id"]) not in nodes:
                        nodes.append(str(location["node_id"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StorageError):
        return {}
    return found


def unrecorded_files(row: dict, files: dict[str, list[str]] | None,
                     snapshot: str | None = None) -> list[str]:
    """Node_ids where the scan found files of SNAPSHOT while no home is
    recorded for it. Without SNAPSHOT, the first required snapshot without a
    home: the one the suggestion names, as Download acts on one snapshot.
    A node where another spec's prepared copy of SNAPSHOT is recorded is left
    out: a record names its files, and the frame names that spec there (see
    same_files_prepared)."""
    snapshots = members(row)
    if not files:
        return []
    if snapshot not in snapshots:
        missing = [name for name in recovery_snapshots(row) if not snapshots[name].get("home")]
        if not missing:
            return []
        snapshot = missing[0]
    member = snapshots[snapshot]
    if member.get("home"):
        return []
    manifest = str(member.get("snapshot_manifest_id"))
    recorded = {str(copy.get("node_id")) for copy in row.get("other_specs_prepared_copies") or []
                if copy.get("snapshot_manifest_id") == manifest}
    return [node_id for node_id in files.get(manifest, []) if node_id not in recorded]


def recovery_snapshots(row: dict) -> list[str | None]:
    """Unregistered snapshots first, in the same order before/after JSON encoding."""
    snapshots = members(row)
    return sorted(snapshots, key=lambda name: (bool(snapshots[name].get("home")),
                                             name != "target", name or ""))


def _location_reason(archive_location: str) -> str:
    return "archives are disabled" if archive_location == "disabled" else "no archive location is configured"


def hidden_fix(reason: str, hidden: dict[str, str]) -> str | None:
    """What makes operations left out for REASON available again, or None when
    nothing does (a historical spec). A serving guard points to the guarded
    trial procedure, outside ordinary start. Check now records file
    observations only, so it is never the fix: homes are recorded by Download
    and Restore, and the archive location is a setting."""
    if reason == _location_reason("not-configured"):
        return "Set one in Archive storage configuration on the home menu."
    if reason == _location_reason("disabled"):
        return "Set a location in Archive storage configuration on the home menu."
    if reason.startswith("no home is recorded"):
        return "Download or Restore records a home." if "restore" not in hidden else "Download records a home."
    if reason == START_UNSUPPORTED["guard_unsupported"]:
        return "Use a guarded trial: ./pulsar guarded --help; see docs/GUARDED_SERVING.md."
    return None


def operations(row: dict, archive_location: str) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    """Return offered actions, left-out actions with reasons, and eligible snapshots.

    A home record locates files; it cannot rule out recovery after file loss.
    Download and configured-archive Restore stay available, and their live
    plans decide eligibility. Service state is live, so start, stop and status
    are offered, except that start is left
    out for a spec this Stack cannot start (a serving guard); stop and status
    stay because such a service may have been started elsewhere. Pin, unpin
    and purge also act on node records and incomplete staging that saved
    records do not show, so they are always offered and their plan shows what
    they would do.
    """
    if archive_location not in ARCHIVE_LOCATION:
        raise ValueError("unknown archive location status")
    snapshots = members(row)
    with_home = [name for name, member in snapshots.items() if member.get("home")]
    without_home = [name for name, member in snapshots.items() if not member.get("home")]
    archives = archive_location == "configured"
    schema3 = isinstance(row.get("snapshots"), dict)
    missing_reason = ("no home is recorded for snapshot " + ", ".join(str(n) for n in without_home)
                      if schema3 else "no home is recorded")
    hidden: dict[str, str] = {}
    if not archives:
        hidden["restore"] = _location_reason(archive_location)
    if without_home:
        hidden["prepare"] = missing_reason
    if not with_home:
        for action in ("move", "remove", "archive"):
            hidden[action] = "no home is recorded"
    if not archives:
        hidden.setdefault("archive", _location_reason(archive_location))
        hidden["verify"] = _location_reason(archive_location)
    unsupported = start_unsupported(row)
    if unsupported:
        hidden["start"] = unsupported
    if row.get("start_unsupported_reason") == "historical_spec":
        for action in LAUNCH:
            hidden[action] = "historical schema-1 specs do not support these operations"
    offered = [action for action in MAIN + LAUNCH + STORAGE if action not in hidden]
    eligible = {}
    if schema3:
        for action in ("acquire", "restore"):
            # Put unregistered snapshots first, while keeping recorded homes
            # selectable when their files have gone missing.
            eligible[action] = [str(n) for n in recovery_snapshots(row)]
        for action in ("move", "remove", "archive"):
            eligible[action] = [str(n) for n in recovery_snapshots(row) if snapshots[n].get("home")]
    return offered, hidden, eligible


def suggestion(row: dict, offered: list[str], archive_location: str, after: str | None = None,
               names: NodeNames | None = None, *, now=None, live: dict | None = None,
               files: dict[str, list[str]] | None = None) -> tuple[str, str] | None:
    """One suggested next step from saved state; the first matching rule wins.

    ``after`` is the last relevant outcome in this menu session. A recorded
    check clears an earlier mutation even when it found blockers; a check
    without a recorded result must not reuse old readiness. A mutation makes
    the saved check out of date, and a started service is live. ``live`` is
    this session's newest observation of the service (see live_fact): a
    service seen in it, even long ago, is inspected before anything else, and
    Start waits for Live status while the service state is unknown. A newer
    observation also replaces a remembered Start or service interruption. For a
    spec this Stack cannot start, nothing after the Check, Download and
    Restore rules is suggested: Prepare and Start would only lead to a start.
    ``files`` is this session's Model files scan (see found_files); files it
    found for a snapshot without a home lead to Download (see _next_home).
    """
    names = names or NodeNames()
    age = row.get("observation_age_seconds")
    # A Start or service interruption stands until a newer observation of the
    # service replaces it (live_fact keeps the newest).
    superseded = live is not None and live["kind"] != "changed"
    if after == "check-interrupted":
        return "check", "check interruption requested; rerun before relying on saved observations"
    if after == "storage-interrupted":
        return "check", "storage outcome unconfirmed; inspect current files before retrying"
    if after == "storage-failed":
        return "check", "the last storage change did not succeed; inspect current files before retrying"
    if after == "service-interrupted" and not superseded:
        return "status", "service outcome unconfirmed; current service state is unknown"
    if after == "image-interrupted" and "image-check" in offered:
        return "image-check", "staging outcome unconfirmed; inspect the serving image before retrying"
    if after == "check-failed":
        return "check", "the last check did not record an observation"
    if after == "start" and not superseded:
        return "status", "started from this menu; live status observes the service"
    if live and live["kind"] in ("service", "exists") and "status" in offered:
        seen = live_value(live)
        return "status", seen + ("; Live status shows its state" if live.get("source") == "status"
                                 else "; Live status checks it now")
    if after in ("acquire", "restore"):
        # Both verify the files before recording the home (a reused home is
        # checked as Check now would), so the next step is the next snapshot
        # without a home, then Prepare.
        found = _next_home(row, offered, archive_location, now, files=files, names=names)
        if found and found[1]:
            name, action, detail = found[:3]
            if found[3:]:
                return action, detail
            return action, (f"{name} has no home yet" if name is not None else "no home recorded yet") + f"; {detail}"
        if found is None and "prepare" in offered and not start_unsupported(row):
            return "prepare", f"{LABELS[after].lower()} finished; every required snapshot has a verified home"
    if after == "prepare" and "start" in offered:
        # Prepare ends by verifying the prepared copy on every serving rank.
        if live and live["kind"] in ("unknown", "changed") and "status" in offered:
            return "status", live_value(live) + "; check before starting"
        return "start", "prepared on every serving rank just now; start rechecks prerequisites"
    if after in MUTATIONS:
        return "check", f"{LABELS[after].lower()} ran after the last check"
    # Check now reads homes from records only, so it cannot see a home the
    # session's Model files scan found with no record naming it; no saved or
    # current check is needed before Download looks for it.
    found = _next_home(row, offered, archive_location, now, files=files, names=names)
    if found and found[3:]:
        return found[1], found[2]
    if row.get("checked_at") is None:
        return "check", "no saved check"
    local = row.get("local_state", "unknown")
    blocker = "saved blocker: " + str(blocker_text(row["blockers"][0], row, names)) if row.get("blockers") else None
    # Start rechecks prepared files before launching, so a saved check that
    # found them prepared stays usable at any age; every other state is
    # checked again once its observation is out of date.
    if age is None or (age > STALE_SECONDS and (local != "ready" or blocker)):
        return "check", "last check " + age_text(age)
    if local == "unknown":
        return "check", blocker or "file state was not established by the last check"
    found = _next_home(row, offered, archive_location, now, files=files, names=names)
    if found:
        name, action, detail = found[:3]
        if action is None:
            return None
        if found[3:]:
            return action, detail
        return action, "no home recorded" + (f" for snapshot {name}" if name is not None else "") + f"; {detail}"
    if start_unsupported(row):
        return None
    if local in ("missing", "changed") and "prepare" in offered:
        reason = "files changed since they were verified" if local == "changed" else "files not prepared on every serving node"
        same = same_files_prepared(row)
        bind = f"; {SAME_FILES_REASON.format(spec=_other_specs(same, row))}" if same else ""
        return "prepare", f"{reason} (checked {age_text(age)}){bind}"
    if blocker:
        return "check", blocker
    if local == "ready":
        if live and live["kind"] in ("unknown", "changed") and "status" in offered:
            return "status", live_value(live) + "; check before starting"
        return "start", f"files prepared as of the last check ({age_text(age)}); Start rechecks them"
    return None


def _next_home(row: dict, offered: list[str], archive_location: str, now=None, *,
               files: dict[str, list[str]] | None = None, names: NodeNames | None = None) -> tuple | None:
    """The first required snapshot without a home, the operation that records one, and why,
    as (NAME, ACTION, DETAIL), or (NAME, ACTION, REASON, True) when REASON is the whole reason.

    Home operations act on one snapshot. Use that snapshot's reconciled
    archive fact, not the aggregate state or the existence of a record.
    Files this session's Model files scan found for it on a node come first:
    Download's own plan reuses them when it finds them, and copies nothing
    (Restore always copies). The scan sees more folders than Download's
    discovery, so the reason says Download checks them, not that it reuses.
    """
    snapshots = members(row)
    missing = [name for name in recovery_snapshots(row) if not snapshots[name].get("home")]
    if not missing:
        return None
    name = missing[0]
    holders = unrecorded_files(row, files, name)
    if holders and "acquire" in offered:
        found = FILES_FOUND.format(hosts=_node_list(holders, names or NodeNames()))
        if name is not None:
            found = found.replace("files", f"files of snapshot {name}", 1)
        # The whole reason: no "no home recorded" before it.
        return name, "acquire", found + FILES_REASON, True
    if archive_location == "configured":
        kind, _, wording = archive_facts(row, now)[name]
        detail = f"archive: {wording}"
        if kind in ("present", "verified") and "restore" in offered:
            return name, "restore", detail + "; Restore rechecks contents"
    else:
        detail = _location_reason(archive_location)
    return name, ("acquire" if "acquire" in offered else None), detail


def recorded_node(row: dict) -> str | None:
    """The node_id saved records name for the recipe: a home, else a prepared copy."""
    for member in members(row).values():
        if (member.get("home") or {}).get("node_id"):
            return str(member["home"]["node_id"])
    for view in sorted(row.get("prepared_copies") or [], key=lambda view: view.get("rank", 0)):
        if view.get("node_id"):
            return str(view["node_id"])
    return None


def suggested_command(row: dict, archive_location: str, names: NodeNames | None = None, *, now=None) -> list[str] | None:
    """The suggested next step (see suggestion) as the command that runs it.

    One-node recipes name the node their saved records place them on, by
    hostname when the saved topology knows it. A download or restore that no
    saved record places leaves --node out, so it goes to the command's default
    destination: the deployment overlay's placement, or this node. Returns
    None when nothing is suggested, as for a guarded spec.
    """
    names = names or NodeNames()
    offered, _, _ = operations(row, archive_location)
    suggested = suggestion(row, offered, archive_location, None, names, now=now)
    if not suggested:
        return None
    action, spec = suggested[0], row["spec_id"][:12]
    node = recorded_node(row) if row["geometry"]["nodes"] == 1 else None
    placement = ["--node", (names.hostnames or {}).get(node) or node] if node else []
    if action in ("acquire", "restore"):
        snapshot = []
        if isinstance(row.get("snapshots"), dict):
            without_home = [str(name) for name in recovery_snapshots(row) if not members(row)[name].get("home")]
            snapshot = ["--snapshot", without_home[0]] if without_home else []
        return ["./pulsar", "model", action, spec, *snapshot, *placement, "--yes"]
    if action == "check":
        return ["./pulsar", "models", "check", spec, *placement]
    if action == "prepare":
        return ["./pulsar", "model", "prepare", spec, *placement, "--yes"]
    if action == "start":
        return ["./pulsar", "start", spec, *placement]
    # Status and stop find the recorded service without a node.
    return ["./pulsar", action, spec]


# Files state in a recipe label: the same saved facts, in a few words. Each
# phrase has a row under Catalog labels in docs/OPERATIONS.md.
LABEL_FILES = {"ready": "files prepared", "missing": "files not prepared", "changed": "files changed",
               "unknown": "files unknown"}
# The frame's hint after a Files value that no check has established yet.
CHECK_HINT = "choose Check now (read-only)"
# Home folders this session's Model files scan found where no record names
# them: Download's plan registers them when its discovery finds them.
FILES_FOUND = "files found on {hosts}"
FILES_REASON = " but not in Pulsar's records; Download checks them before fetching anything"
FILES_HINT = FILES_FOUND + ", not in Pulsar's records; Download checks them"
FILES_FOUND_HERE = "files found here, not in Pulsar's records"
# Another spec's prepared copies of this spec's snapshots (see
# same_files_prepared): Prepare binds this spec to them without copying.
# {spec} is that spec's 12-character prefix, or the first one's and "and N more"
# (see _other_specs). A spec this checkout does not publish is "outside the
# catalog", the words the Running labels use for a service of such a spec.
OUTSIDE_THE_CATALOG = "outside the catalog"
SAME_FILES = "same files prepared for {spec}"
SAME_FILES_ON = "the same files are prepared for {spec} on {hosts}"
SAME_FILES_BIND = "Prepare binds them there without copying"
SAME_FILES_REASON = "Prepare binds the same files prepared for {spec} without copying"
SAME_FILES_HERE = "same files prepared here for {spec}"


def same_files_prepared(row: dict) -> list[dict]:
    """Other specs' prepared-copy records that this spec's Prepare can bind to, or [].

    They count only when every required snapshot has at least one, since
    Prepare needs the complete set, and only while this spec's own saved
    state is missing, unknown or not checked yet: a ready spec has its own
    bindings, and a changed folder is changed for the other spec too.
    """
    others = [copy for copy in row.get("other_specs_prepared_copies") or [] if copy.get("spec_id") != row["spec_id"]]
    if not others or (row.get("checked_at") and row.get("local_state") not in ("missing", "unknown")):
        return []
    required = [member.get("snapshot_manifest_id") for member in members(row).values()]
    if not required or not all(required):
        return []
    held = {copy.get("snapshot_manifest_id") for copy in others}
    return others if all(manifest in held for manifest in required) else []


def _other_specs(records: list[dict], row: dict) -> str:
    """The specs RECORDS belong to: the first catalog member by its 12-character
    prefix and how many more, with how many of them are outside the catalog.

    Specs started from spec files outside releases/ hold bindings too, and
    people cannot find them in the catalog, so a catalog member is named
    first. Where the row does not know membership
    (other_prepared_specs_in_catalog is None), every spec counts as one.
    """
    specs = sorted({str(record["spec_id"]) for record in records})
    known = row.get("other_prepared_specs_in_catalog")
    listed = [spec for spec in specs if known is None or spec in known]
    outside = len(specs) - len(listed)
    if not listed:
        return f"a spec {OUTSIDE_THE_CATALOG}" if outside == 1 else f"{outside} specs {OUTSIDE_THE_CATALOG}"
    more = len(specs) - 1
    if not more:
        return listed[0][:12]
    return f"{listed[0][:12]} and {more} more" + (f" ({outside} {OUTSIDE_THE_CATALOG})" if outside else "")


def state_parts(row: dict) -> list[str]:
    """Saved state for a recipe label, files first; review status only when the
    spec sets one. Other specs' prepared copies of the same files come last, so
    they are the first part to yield when the label runs short."""
    parts = [LABEL_FILES[row["local_state"]] if row.get("checked_at") else NOT_CHECKED]
    if start_unsupported(row):
        parts.append("start unsupported")
    status = (row.get("review") or {}).get("status")
    if status:
        parts.append(str(status))
    same = same_files_prepared(row)
    if same:
        parts.append(SAME_FILES.format(spec=_other_specs(same, row)))
    return parts


def short_state(row: dict) -> str:
    return " · ".join(state_parts(row))


def withdrawal_notice(row: dict) -> list[str]:
    """Display the maintainer's warning without adding an operation gate."""
    review = row.get("review") or {}
    if review.get("status") != "withdrawn":
        return []
    lines = ["Maintainer warning: withdrawn", f"Reason: {clean(review.get('reason') or 'No reason recorded.')}"]
    if review.get("reviewed_at"):
        lines.append(f"Reviewed: {clean(review['reviewed_at'])}")
    lines.append("Advisory metadata; launch checks still apply.")
    return lines


# Reclaiming space, in the order that unblocks the next step: a running
# container holds its copies, a pin keeps them from purge, and a home goes last.
RECLAIM = ("stop", "unpin", "purge", "remove")


def menu_label(row: dict, width: int, live: dict | None = None, recipe_facts: RecipeFacts | None = None,
               short_name: str | None = None) -> str:
    """One recipe list entry that fits the menu: model [spec] state.

    Critical warnings take space before optional model/file context. Ordinary
    entries retain their model-first layout. A service seen in this session
    leads the state; the saved files state yields to it when space runs out,
    since the recipe frame repeats it. Gum's two-column cursor is the only
    text drawn before the label.

    When the catalog has other recipes of the same model, RECIPE_FACTS (see
    distinguishing_facts) follow the spec, and no fact is ever cut. As room
    runs short the model name is cut, then the saved state yields, then facts
    this recipe does not need to be told apart, then the spec prefix shortens
    to 8, then the model name goes and the facts that do not fit whole. A
    label always keeps at least the 8-character spec prefix, after any
    critical warning. SHORT_NAME is the model name without
    its organization, given where no other model would read the same; a cut
    name uses it. Names are cut in the middle and keep their end, where
    recipes and models of one organization differ.

    Another spec's prepared copies of the same files (see same_files_prepared)
    are the last state part: shown whole or not at all, ahead of the node count
    and spec state only. Where they fit nowhere, the label reads as it would
    without them.
    """
    seen = live_label(live)
    budget = max(32, min(100, width)) - 2
    model = clean(row["model_id"])
    short = clean(short_name) if short_name else None
    review = (row.get("review") or {}).get("status")
    critical = (["withdrawn"] if review == "withdrawn" else [])
    if start_unsupported(row):
        critical.append("start unsupported")
    if critical:
        files = seen or (LABEL_FILES[row["local_state"]] if row.get("checked_at") else NOT_CHECKED)
        rest = [files] + ([review] if review and review != "withdrawn" else [])
        if recipe_facts:
            return _critical_label_with_facts(critical, model, short, row["spec_id"], recipe_facts, rest, budget)
        warning = " · ".join(critical)
        identity = f" [{row['spec_id'][:12]}]"
        if len(warning + identity) > budget:
            warning = warning.replace("start unsupported", "start blocked")
        if len(warning + identity) > budget:
            # At the narrowest widths the warning outranks the copyable prefix;
            # the selected view shows the complete spec ID.
            identity = f" [{row['spec_id'][:8]}]"
        if len(warning + identity) > budget:
            # At the minimum width the withdrawal warning has priority; the
            # selected view still explains every launch restriction in full.
            warning = critical[0]
        heading = warning + identity
        room = budget - len(heading) - 1
        if room < 4:
            return heading
        tail = " · " + " · ".join(rest)
        context = _fit_name(model, short, max(MIN_NAME, room - len(tail))) + tail
        if len(context) > room:
            context = context[:room - 3] + "..."
        return heading + " " + context
    parts = state_parts(row)
    # Other specs' copies of the same files are the last state part and the
    # first to yield, always whole: a cut spec prefix could name another spec.
    same = bool(same_files_prepared(row))
    if seen:
        parts.insert(0, seen)
    # Where the whole label fits (60 columns or wider), it also names the node
    # count (unless a seen service or a fact already does) and the spec's
    # state, as models list does. State is metadata only.
    nodes = node_count(row["geometry"]["nodes"])
    lead = recipe_facts.sets[0] if recipe_facts else []
    identity = f" [{row['spec_id'][:12]}] "

    def wide(kept: list[str]) -> str:
        wider = lead + ([] if seen or nodes in lead else [nodes]) + kept
        if row.get("state") and row["state"] not in kept:
            wider.append(str(row["state"]))
        return model + identity + " · ".join(wider)

    def whole(kept: list[str]) -> bool:
        return len(model) + len(identity + " · ".join(lead + kept)) <= budget

    def narrow(kept: list[str]) -> str:
        kept = list(kept)
        # Without facts the name is cut and then the label truncated, so the
        # part goes before the name is cut; beside a seen service, before the
        # files state.
        if same and (seen or not recipe_facts) and not whole(kept):
            kept.pop()
        if seen and not whole(kept):
            kept.pop(1)
        if recipe_facts:
            return _label_with_facts(model, short, row["spec_id"], recipe_facts, kept, budget)
        suffix = identity + " · ".join(kept)
        text = _fit_name(model, short, max(MIN_NAME, budget - len(suffix))) + suffix
        return text if len(text) <= budget else text[:budget - 3] + "..."

    if width >= 60 and len(wide(parts)) <= budget:
        return wide(parts)
    label = narrow(parts)
    if same and width >= 60 and parts[-1] not in label and len(wide(parts[:-1])) <= budget:
        # The part did not fit anywhere: the label reads as it would without it.
        return wide(parts[:-1])
    return label


# A model name cut to fit a list label keeps at least this many characters
# while other parts of the label can still yield.
MIN_NAME = 11


def _fit_name(model: str, short: str | None, room: int) -> str:
    """The model name in ROOM: whole, then without its organization, then cut in the middle."""
    if len(model) <= room:
        return model
    return cut_middle(short or model, room)


def _fact_layouts(facts: RecipeFacts, parts: list[str]):
    """(facts, state parts) for a label, most first: the state yields from its
    end, then the facts shorten as far as they still tell the recipe apart."""
    for count in range(len(parts), -1, -1):
        yield facts.sets[0], parts[:count]
    for shorter in facts.sets[1:]:
        yield shorter, []


def _lead_and_facts(lead: str, facts: RecipeFacts, budget: int) -> str:
    """LEAD (a spec prefix, after any warning) and the fullest fact set that
    fits whole; else the whole facts of the shortest set that fit, the later
    ones first since they separate the closest recipes; else LEAD alone. A
    label never loses its identity, and a fact is never cut."""
    for told in facts.sets:
        text = f"{lead} " + " · ".join(told)
        if len(text) <= budget:
            return text
    room, kept = budget - len(lead) - 1, []
    for fact in reversed(facts.sets[-1]):
        if len(fact) + 3 * bool(kept) <= room:
            kept.insert(0, fact)
            room -= len(fact) + 3 * (len(kept) > 1)
    return f"{lead} " + " · ".join(kept) if kept else lead


def _label_with_facts(model: str, short: str | None, spec_id: str, facts: RecipeFacts, parts: list[str],
                      budget: int) -> str:
    """model [spec] facts · state, giving way in the order menu_label describes."""
    for prefix, layouts in ((12, list(_fact_layouts(facts, parts))), (8, [(facts.sets[-1], [])])):
        for told, shown in layouts:
            suffix = f" [{spec_id[:prefix]}] " + " · ".join(told + shown)
            room = budget - len(suffix)
            if room >= min(MIN_NAME, len(model)):
                return _fit_name(model, short, room) + suffix
    # The model name gives way; the 8-character spec prefix always stays.
    suffix = f" [{spec_id[:8]}] " + " · ".join(facts.sets[-1])
    if budget - len(suffix) >= min(5, len(model)):
        return _fit_name(model, short, budget - len(suffix)) + suffix
    return _lead_and_facts(f"[{spec_id[:8]}]", facts, budget)


def _critical_label_with_facts(critical: list[str], model: str, short: str | None, spec_id: str,
                               facts: RecipeFacts, rest: list[str], budget: int) -> str:
    """warning [spec] model · facts · state. The warning and at least the
    8-character spec prefix are placed first; facts take what is left."""
    joined = " · ".join(critical)
    blocked = joined.replace("start unsupported", "start blocked")
    for warning, prefix in ((joined, 12), (blocked, 12), (blocked, 8), (critical[0], 8)):
        heading = f"{warning} [{spec_id[:prefix]}] "
        room = budget - len(heading)
        if room < 0:
            continue
        for told, shown in _fact_layouts(facts, rest):
            tail = " · " + " · ".join(told + shown)
            if room - len(tail) >= min(MIN_NAME, len(model)):
                return heading + _fit_name(model, short, room - len(tail)) + tail
        # The model name gives way.
        tail = " · " + " · ".join(facts.sets[-1])
        if room - len(tail) >= min(5, len(model)):
            return heading + _fit_name(model, short, room - len(tail)) + tail
        if len(tail) - 3 <= room:
            return heading + tail[3:]
    # Lead with the blocker: the shortest warning that keeps every critical
    # word, else the first, and the spec prefix; facts take what is left.
    leads = [f"{warning} [{spec_id[:8]}]" for warning in (blocked, critical[0])]
    lead = next((text for text in leads if len(text) <= budget), leads[-1])
    return _lead_and_facts(lead, facts, budget)


def node_count(nodes: object) -> str:
    return f"{nodes} node{'' if nodes == 1 else 's'}"


def short_count(value: object) -> str:
    """A recorded count in few characters: 1048576 is 1M and 500000 is 500K.

    Multiples of 1024 and of 1000 take K or M; any other value keeps its digits.
    """
    text = str(value)
    if not re.fullmatch(r"[0-9]+", text):
        return text
    number = int(text)
    for unit, suffix in ((1024 ** 2, "M"), (1000 ** 2, "M"), (1024, "K"), (1000, "K")):
        if number >= unit and number % unit == 0:
            return f"{number // unit}{suffix}"
    return text


def _recipe_facts(row: dict) -> list[tuple[object, str, str]]:
    """(recorded value, short words, exact words) for each fact that can tell
    recipes of one model apart, in the order a list label names them."""
    nodes = (row.get("geometry") or {}).get("nodes")
    facts: list[tuple[object, str, str]] = [(nodes, node_count(nodes), node_count(nodes))]
    arguments = row.get("engine_args")
    for flag, values in setting_values(arguments or []).items():
        label = SETTING_FLAGS[flag]
        if arguments is None:
            # Unknown is not absent: a row without its arguments says so.
            facts.append((None, f"{label} not recorded", f"{label} not recorded"))
        elif not values:
            facts.append(((), f"{label} not set", f"{label} not set"))
        else:
            facts.append((tuple(values), f"{label} {'/'.join(short_count(value) for value in values)}",
                          f"{label} {'/'.join(values)}"))
    digest = str((row.get("image") or {}).get("digest") or "")
    image = f"image {(digest.partition(':')[2] or digest)[:12]}" if digest else "image not recorded"
    facts.append((digest, image, f"image {digest}" if digest else image))
    return facts


class RecipeFacts(NamedTuple):
    """What tells one recipe apart from others of its model.

    sets runs from fullest to shortest: the first names every fact that told
    the model's recipes apart, and each next one leaves out one more fact
    while the facts it keeps still tell this recipe apart from every other.
    unique says whether those facts tell it apart at all; recipes that differ
    in none of them keep their fullest facts and are told apart by spec ID.
    """
    sets: list[list[str]]
    unique: bool

    def words(self) -> str:
        return " · ".join(self.sets[0])


def distinguishing_facts(rows: list[dict]) -> dict[str, RecipeFacts]:
    """For recipes that share a model, the facts that tell each one apart, by spec ID.

    Facts are tried in order: node count, context (--max-model-len), max
    sequences (--max-num-seqs), quantization, then a short image digest. A
    fact is named when it differs between recipes not yet told apart. A
    shorter set leaves out facts earliest first, and only where the facts it
    keeps still differ from every other recipe of the model. A model with one
    recipe gets no entry, nor do recipes that differ in none of these facts.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("model_id"):
            groups.setdefault(row["model_id"], []).append(row)
    found: dict[str, RecipeFacts] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        facts = [_recipe_facts(row) for row in group]
        ties = [list(range(len(group)))]
        named = []
        for position in range(len(facts[0])):
            if all(len(tie) == 1 for tie in ties):
                break
            split = []
            for tie in ties:
                values: dict[object, list[int]] = {}
                for index in tie:
                    values.setdefault(facts[index][position][0], []).append(index)
                split.extend(values.values())
            if len(split) > len(ties):
                named.append(position)
                ties = split
        if not named:
            continue
        # Short words that would read the same for different values (1M for
        # 1000000 and 1048576) give way to the exact ones.
        exact = {position: len({fact[position][1] for fact in facts}) < len({fact[position][0] for fact in facts})
                 for position in named}

        def apart(index: int, kept: list[int]) -> bool:
            return all(any(facts[index][p][0] != facts[other][p][0] for p in kept)
                       for other in range(len(group)) if other != index)

        for index, row in enumerate(group):
            words = {position: facts[index][position][2 if exact[position] else 1] for position in named}
            unique = apart(index, named)
            kept, sets = list(named), [[words[position] for position in named]]
            for position in named if unique else []:
                trial = [other for other in kept if other != position]
                if trial and apart(index, trial):
                    kept = trial
                    sets.append([words[other] for other in kept])
            found[row["spec_id"]] = RecipeFacts(sets, unique)
    return found


def short_model_names(rows: list[dict]) -> dict[str, str]:
    """Model names without the organization, by spec ID, where no other model
    in ROWS has the same name; a list label cut to fit uses them."""
    models: dict[str, set[str]] = {}
    for model in {row.get("model_id") for row in rows} - {None}:
        models.setdefault(model.rpartition("/")[2], set()).add(model)
    found: dict[str, str] = {}
    for row in rows:
        organization, _, name = (row.get("model_id") or "").rpartition("/")
        if organization and len(models[name]) == 1:
            found[row["spec_id"]] = name
    return found


# The engine settings a recipe frame summarizes; they can also tell recipes of
# one model apart in the list.
SETTING_FLAGS = {"--max-model-len": "context", "--max-num-seqs": "max sequences", "--quantization": "quantization"}


def setting_values(arguments: list[str]) -> dict[str, list[str]]:
    """Each summarized flag's recorded values in order; a flag given without a value records "not supplied"."""
    values: dict[str, list[str]] = {flag: [] for flag in SETTING_FLAGS}
    for index, token in enumerate(arguments):
        flag, separator, value = token.partition("=")
        if flag in SETTING_FLAGS:
            if not separator:
                value = arguments[index + 1] if index + 1 < len(arguments) and not arguments[index + 1].startswith("--") else "not supplied"
            values[flag].append(value)
    return values


def explicit_settings(arguments: list[str]) -> str:
    """Summarize recorded values only; do not guess engine defaults or precedence."""
    parts = [f"{SETTING_FLAGS[flag]} {items[0] if len(items) == 1 else 'has repeated values; see details'}"
             for flag, items in setting_values(arguments).items() if items]
    return "; ".join(parts) if parts else "see Show details"


# Frame fields share one label column; continuation lines hang under the value.
FRAME_LABEL_WIDTH = 10


def header(row: dict, hidden: dict[str, str], suggested: tuple[str, str] | None, width: int, *, now=None,
           published=None, live: dict | None = None, read_only: bool = False,
           after: str | None = None, after_age: int | None = None,
           found_on: str | None = None, same_files: str | None = None) -> tuple[list[str], list[str]]:
    """The recipe frame as (body lines, Next lines).

    The body starts with the model and spec; saved facts follow as aligned
    fields. Next is kept apart so it stays the frame's last line, and so a
    short terminal can show only the first line and Next. FOUND_ON names the
    machines where this session's Model files scan found files that no
    record names (see unrecorded_files). SAME_FILES says where another spec's
    prepared copies of the same files are (see same_files_prepared).
    """
    buffer = io.StringIO()
    out = TerminalWriter(width=width, stream=buffer)

    def field(label: str, value: object) -> None:
        out.field(label, value, label_width=FRAME_LABEL_WIDTH)

    out.emit(f"{row['model_id']} [{row['spec_id'][:12]}]")
    for line in withdrawal_notice(row):
        out.emit(line)
    field("Recipe", recipe_text(row["geometry"]))
    field("Settings", explicit_settings(row.get("engine_args") or []))
    digest = str((row.get("image") or {}).get("digest") or "not recorded")
    image = digest if len(digest) <= 24 else digest[:24] + "…"
    field("Image", f"{image} (exact digest)" if digest != "not recorded" else digest)
    review = row.get("review") or {}
    if review.get("status") and review["status"] != "withdrawn":
        field("Review", f"{review['status']} · {review.get('reviewed_at') or 'date not recorded'} (advisory)")
    files = files_text(row)
    if found_on and not read_only:
        # Check now reads homes from records only, so it cannot find these;
        # Download's plan looks for them.
        files = f"{files}; {FILES_HINT.format(hosts=found_on)}"
    elif not row.get("checked_at") and not read_only:
        # Say what establishes the files, as Running? does; read-only views offer no check.
        files = f"{files}; {CHECK_HINT}"
    if same_files:
        files = f"{files}; {same_files}"
    if after == "prepare" and after_age is not None:
        # Prepare saves no observation; keep the old check visible as such.
        files = f"prepared in this session ({age_text(after_age)}); saved check: {files}"
    field("Files", files)
    field("Archive", archive_text(row, now))
    field("Running?", live_value(live, read_only=read_only))
    field("Lifecycle", lifecycle(row, live, mark=not read_only))
    field("Results", published_line(published, now=now))
    by_reason: dict[str, list[str]] = {}
    for action in MAIN + LAUNCH + STORAGE:
        if action in hidden:
            by_reason.setdefault(hidden[action], []).append(LABELS[action])
    for reason, labels in by_reason.items():
        fix = hidden_fix(reason, hidden)
        field("Not shown", f"{', '.join(labels)} ({reason})." + (f" {fix}" if fix else ""))
    body = buffer.getvalue().splitlines()
    if not suggested:
        return body, []
    buffer.seek(0)
    buffer.truncate()
    field("Next", f"{LABELS[suggested[0]]} — {suggested[1]}")
    return body, buffer.getvalue().splitlines()


def clean(text: object) -> str:
    return " ".join(str(text).split())


def option_label(action: str, suggested: tuple[str, str] | None, width: int) -> str:
    """LABEL[ (suggested)][ — effect]: the effect only when the whole label fits.

    The menu passes the label before " — " to the operation, without "(suggested)".
    """
    label = LABELS[action] + (" (suggested)" if suggested and suggested[0] == action else "")
    with_effect = f"{label} — {EFFECTS[action]}"
    return with_effect if len(with_effect) <= width else label


def snapshot_label(row: dict, name: str, width: int, names: NodeNames) -> str:
    """NAME — ROLE (MODEL @ COMMIT) · HOME: one required-snapshot choice.

    A part shows only where the whole label fits: the identity goes first,
    since the confirmation question repeats it, then the role. A long name is
    then shortened, keeping at least 8 characters, before the home is cut. The
    menu passes the name, not this label, to the operation.
    """
    member = members(row)[name]
    home = member.get("home") or {}
    where = (f"home on {names(home['node_id'])}" if home.get("node_id")
             else "home recorded" if home else "no home recorded")
    # A recipe binds a snapshot other than target only as the speculative
    # configuration's model (release_spec.serving.snapshot_engine_args).
    lead = f"{name} — " + ("served model" if name == "target" else "speculative-decoding checkpoint")
    for text in (f"{lead} ({_snapshot_identity(row, name)}) · {where}", f"{lead} · {where}", f"{name} · {where}"):
        if len(text) <= width:
            return text
    room = width - len(f"... · {where}")
    if room >= 8:
        return f"{name[:room]}... · {where}"
    return text[:width - 3] + "..."


# The question above the node choices, by operation. One-node recipes also ask
# where to serve; NODE_QUESTION covers the operations not listed.
NODE_QUESTIONS = {
    "acquire": "Which machine should hold the model files?",
    "restore": "Which machine should hold the model files?",
    "move": "Which machine should the home move to?",
    "check": "Which machine's files should be checked?",
    "status": "Which machine runs the service?", "stop": "Which machine runs the service?",
    "observe": "Which machine runs the service?", "audit": "Which machine's prepared files should be hashed?",
}
NODE_QUESTION = "Which machine should serve the model?"
# These operations choose where files go, so no machine is preselected.
NODE_UNSELECTED = {"acquire", "restore", "move"}


def seen_node(row: dict, live: dict | None, node_ids: list[str]) -> str | None:
    """The node_id where a complete service inventory saw this one-node spec's
    service, when it is one of NODE_IDS; None otherwise.

    Stop without a node removes containers on every node, so only a service
    seen on exactly one identified node can skip the machine question.
    """
    if live is None or live["incomplete"]:
        return None
    fact = live_fact(row, live, None)
    if fact is None or fact["kind"] != "service" or fact.get("node_id") not in node_ids:
        return None
    return fact["node_id"]


def node_label(row: dict, node_id: str, hostname: str, width: int, snapshot: str | None = None,
               seen: str | None = None, found: list[str] | None = None) -> str:
    """HOSTNAME — what saved records place there: the home, a prepared copy.

    The home is the selected snapshot's, or any required snapshot's. SEEN is
    the node_id where a service inventory taken just now saw the service; it
    is named first. FOUND lists the node_ids where this session's Model files
    scan found the snapshot's files with no record naming them; that comes
    next. The records are shown only where the whole label fits, the home
    first; another spec's prepared copy of the same files comes last (see
    same_files_prepared).
    """
    snapshots = members(row)
    holders = [snapshots[snapshot]] if snapshot in snapshots else list(snapshots.values())
    parts = []
    if seen is not None and seen == node_id:
        parts.append("service seen here just now")
    if found and node_id in found:
        parts.append(FILES_FOUND_HERE)
    if any((member.get("home") or {}).get("node_id") == node_id for member in holders):
        parts.append("home recorded here")
    if any(copy.get("node_id") == node_id for copy in row.get("prepared_copies") or []):
        parts.append("prepared copy recorded here")
    manifests = {member.get("snapshot_manifest_id") for member in holders}
    here = [copy for copy in same_files_prepared(row)
            if copy.get("node_id") == node_id and copy.get("snapshot_manifest_id") in manifests]
    if here:
        parts.append(SAME_FILES_HERE.format(spec=_other_specs(here, row)))
    for text in ([f"{hostname} — {', '.join(parts)}", f"{hostname} — {parts[0]}"] if parts else []):
        if len(text) <= width:
            return text
    return hostname


def lifecycle(row: dict, live: dict | None, *, mark: bool = True) -> str:
    """Where the spec stands in Download → Prepare → Start, from saved records
    and this session's live facts only. Prepared files never mark Start done:
    only a current running observation does. The current step is bracketed
    when MARK is set."""
    snapshots = members(row)
    homes = bool(snapshots) and all(member.get("home") for member in snapshots.values())
    local = row.get("local_state", "unknown")
    running = bool(live) and live.get("kind") == "service" and live_current(live) \
        and live.get("state") == "running"
    done, current, note = 0, "Download", ""
    if running:
        done, current, note = 3, None, " (running)"
    elif homes and local == "ready":
        done, current = 2, "Start"
    elif homes and local in ("missing", "changed"):
        done, current = 1, "Prepare"
    elif homes:
        done, current, note = 1, None, " (files not checked)"
    if current == "Start" and start_unsupported(row):
        # The menu leaves Start out for this spec; don't point at it.
        current, note = None, " (not offered here)"
    steps = []
    for index, step in enumerate(("Download", "Prepare", "Start")):
        if index < done:
            steps.append(f"{step} ✓")
        elif mark and step == current:
            steps.append(f"[{step}]")
        else:
            steps.append(step)
    return " → ".join(steps) + note + " · Stop → Purge"


def frame_line(text: object) -> str:
    """One frame line for the shell: no tabs or newlines, leading spaces kept for alignment."""
    return str(text).replace("\t", " ").replace("\n", " ").replace("\r", " ").rstrip()


def view_lines(row: dict, archive_location: str, *, after: str | None = None,
               width: int | None = None, names: NodeNames | None = None, now=None,
               published=None, read_only=False, live: dict | None = None,
               after_age: int | None = None, files: dict[str, list[str]] | None = None) -> list[str]:
    offered, hidden, eligible = ([], {}, {}) if read_only else operations(row, archive_location)
    suggested = None if read_only else suggestion(row, offered, archive_location, after, names, now=now, live=live,
                                                  files=files)
    holders = unrecorded_files(row, files) if "acquire" in offered else []
    found_on = _node_list(holders, names or NodeNames()) if holders else None
    # After a Prepare in this session the spec has bindings of its own; the
    # saved check that left it unprepared is older than they are.
    same = same_files_prepared(row) if after != "prepare" else []
    same_files = None
    if same:
        same_files = SAME_FILES_ON.format(spec=_other_specs(same, row),
                                          hosts=_node_list([str(copy["node_id"]) for copy in same],
                                                           names or NodeNames()))
        if "prepare" in offered:
            same_files += f"; {SAME_FILES_BIND}"
    frame_width = max(32, (width or terminal_width()) - 4)
    lines = [f"recipe\t{row['geometry']['nodes']}\t{clean(row['model_id'])}"]
    body, next_lines = header(row, hidden, suggested, frame_width, now=now, published=published, live=live,
                              read_only=read_only, after=after, after_age=after_age, found_on=found_on,
                              same_files=same_files)
    lines += ["header\t" + frame_line(line) for line in body]
    lines += ["next\t" + frame_line(line) for line in next_lines]
    list_width = (width or terminal_width()) - 2
    for action in offered:
        group = "main" if action in MAIN else ("launch" if action in LAUNCH else "storage")
        lines.append(f"option\t{group}\t{action}\t{option_label(action, suggested, list_width)}")
    if suggested:
        lines.append(f"suggest\t{suggested[0]}")
    for action, snapshots in eligible.items():
        for name in snapshots:
            label = snapshot_label(row, name, list_width, names or NodeNames())
            lines.append(f"snapshot\t{action}\t{clean(name)}\t{clean(label)}")
    return lines


def memory_warning_only(records: list[dict]) -> bool:
    """Offer acknowledgement only for the existing start check's warning code."""
    return bool(records) and all(isinstance(record, dict)
                                and record.get("stage") == "check"
                                and record.get("blocker") == "memory_warning" for record in records)


# Menu choices after a refused or failed Start, by blocker code, in the order
# offered. "files" resolves to Download, Restore or Prepare from saved records.
# Each choice runs an existing menu operation with its own selection, preview
# and confirmation; none adds --replace or --pull-image.
START_CHOICES = {
    "topology_incomplete": ("topology-menu",), "fabric_incomplete": ("topology-check",),
    "node_unreachable": ("topology-check",),
    "docker_unavailable": ("doctor",), "container_start_failed": ("doctor",),
    "image_missing": ("image-stage",),
    "model_files_not_ready": ("files",),
    "memory_insufficient": ("inventory",), "port_in_use": ("inventory",),
    "service_exists": ("status", "stop"),
    "smoke_test_failed": ("status", "logs", "stop"), "health_timeout": ("status", "logs", "stop"),
    "container_exited": ("status", "logs", "stop"),
    "image_check_failed": ("verbose",), "model_files_check_failed": ("verbose",),
    "memory_check_failed": ("verbose",), "preflight_failed": ("verbose",),
}
# Codes that get no choice here: the memory-warning retry is offered on its
# own, an interrupted Start's outcome names the next inspection, and the rest
# have no menu operation that resolves them.
START_NO_CHOICE = frozenset({"memory_warning", "start_interrupted", "guard_unsupported", "historical_spec",
                             "service_stopped"})
START_CHOICE_LABELS = {
    "topology-menu": "Open Cluster topology (membership)",
    "topology-check": "Check saved nodes and fabric (checks nodes now)",
    "doctor": "Host diagnostics",
    "inventory": "Open Live service inventory",
    "verbose": "Rerun the checks with full diagnostics",
}


def start_choices(records: list[dict], row: dict, archive_location: str,
                  stops: list[tuple[str, str, str]] | tuple = ()) -> list[tuple[str, ...]]:
    """(action, label) choices for recorded start blockers, without duplicates.

    Actions name existing menu operations, or one of START_CHOICE_LABELS. A
    code without a choice, or an unknown one, adds nothing; its own next step
    is already printed with the blocker. STOPS are (spec ID, model, label) for
    services in the way (see services_in_the_way); each becomes a
    ("stop-other", spec ID, model, label) choice right before the service
    inventory, which stays offered.
    """
    offered, _, _ = operations(row, archive_location)
    choices: list[tuple[str, ...]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        kinds = START_CHOICES.get(record.get("blocker"), ())
        # A failed launch's record says whether its containers remain: none
        # to inspect, read or stop once every rank's was seen removed, and
        # Stop clears what an unconfirmed removal may have left.
        if record.get("stage") == "launch" and record.get("cleanup") == "removed":
            kinds = tuple(kind for kind in kinds if kind not in ("status", "logs", "stop"))
        elif record.get("stage") == "launch" and record.get("cleanup") == "unconfirmed":
            kinds = (*kinds, "status", "stop")
        for kind in kinds:
            if kind == "files":
                missing = _next_home(row, offered, archive_location)
                kind = missing[1] if missing else "prepare"
            if not kind or kind in (choice[0] for choice in choices):
                continue
            if kind == "inventory":
                choices.extend(("stop-other", *stop) for stop in stops)
            if kind in START_CHOICE_LABELS:
                choices.append((kind, START_CHOICE_LABELS[kind]))
            elif kind in offered:
                choices.append((kind, LABELS[kind]))
    return choices


def start_choices_title(records: list[dict], *, dry_run: bool = False) -> str:
    """The question above the choices: what happened, and how many blockers."""
    codes = [record.get("blocker") for record in records if isinstance(record, dict)]
    count = _count(len(codes), "blocker")
    if dry_run:
        return f"Not ready to start: {count}. Choose a next step"
    if any(isinstance(record, dict) and record.get("stage") == "launch" for record in records):
        return f"Start failed after launch: {count}. Choose a next step"
    return f"Start was refused: {count}. Choose a next step"


# Start cannot say what holds a busy port or the memory; a service inventory
# taken after the refusal can name the services on the refused spec's nodes.
SERVICE_BLOCKERS = frozenset({"port_in_use", "memory_insufficient"})


def service_blockers(records: list[dict]) -> bool:
    """Whether a running service may be what refused the start: a busy port or too little free memory."""
    return any(isinstance(record, dict) and record.get("blocker") in SERVICE_BLOCKERS for record in records)


def blocked_ports(records: list[dict]) -> list[int]:
    """The ports start found in use ("port 8000" in a record's detail), in order; they only order the choices."""
    ports: list[int] = []
    for record in records:
        if isinstance(record, dict) and record.get("blocker") == "port_in_use":
            found = re.fullmatch(r"port ([0-9]+)", clean(record.get("detail") or ""))
            if found and int(found.group(1)) not in ports:
                ports.append(int(found.group(1)))
    return ports


def serving_node_ids(row: dict, names: NodeNames, node: str | None) -> list[str]:
    """The node_ids the refused spec serves on: the selected node of a one-node
    spec, or ordinary start's first saved nodes. Empty when they are unknown."""
    nodes = row["geometry"]["nodes"]
    if nodes == 1:
        return [node] if node else []
    return list(names.order[:nodes]) if nodes > 1 and len(names.order) >= nodes else []


def gib_text(mib: int | float) -> str:
    gib = mib / 1024
    return f"{gib:.0f} GiB" if gib >= 10 else f"{gib:.1f} GiB"


def services_in_the_way(inventory: dict, row: dict, node_ids: list[str], catalog: list[dict],
                        ports: list[int] = ()) -> tuple[list[dict], list[str]]:
    """Services on the refused spec's serving nodes, from a service inventory taken just now.

    Returns (stops, notes). A stop is a Pulsar-managed service the inventory
    established as safe to stop, other than the refused spec's own (Start
    reports that one as service_exists): its spec ID (conf), name (catalog
    model, else served name), hosts in rank order (Stop removes every rank),
    API port and the most GPU memory one of its ranks uses, in MiB, when
    measured. Services on a busy port come first, then the most memory.
    Notes name, with no choice, what this menu cannot stop there: managed
    services without safe ownership, containers Pulsar does not manage and
    GPU processes Pulsar did not start. CATALOG rows hold spec_id, model_id
    and nodes; nothing here probes.
    """
    from scripts.inventory_menu import SPEC_ID, project, project_others, read_only_line
    services = project(inventory, catalog)
    others = project_others(inventory, catalog)
    nodes = inventory["nodes"]
    serving = {key for key, node in nodes.items() if isinstance(node, dict) and node.get("node_id") in node_ids}

    def host(key: object) -> str:
        node = nodes.get(key) if isinstance(key, str) else None
        return clean((node or {}).get("hostname") or (node or {}).get("node_id") or key or "unknown node")

    stops: list[dict] = []
    notes: list[str] = []
    for service in inventory["services"]:
        ranks = [rank for rank in service.get("ranks") or [] if isinstance(rank, dict)]
        here = [rank for rank in ranks if rank.get("node") in serving]
        conf = service.get("conf")
        if not here or conf == row["spec_id"]:
            continue
        where = ", ".join(dict.fromkeys(host(rank.get("node")) for rank in here))
        if service.get("ownership") != "managed" or not (isinstance(conf, str) and SPEC_ID.fullmatch(conf)):
            notes.append(f"On {where}: {read_only_line(service)}")
            continue
        projected = services.get(conf) or others.get(conf)
        if projected is None:
            continue
        if projected["stop_reason"]:
            # Inventory's own reason when it found ownership unsafe; else the menu's.
            reasons = service.get("reasons") if service.get("safe_to_stop") is not True else None
            reason = clean(reasons[0]) if isinstance(reasons, list) and reasons and reasons[0] else projected["stop_reason"]
            notes.append(f"On {where}: {clean(projected['model_id'])} [{conf[:12]}] · no Stop offered: "
                         f"{reason.rstrip('.')}")
            continue
        measured = [(rank.get("gpu_memory") if isinstance(rank.get("gpu_memory"), dict) else {}).get("measured_mib")
                    for rank in ranks]
        measured = [mib for mib in measured if type(mib) in (int, float) and mib >= 0]
        stops.append({"conf": conf, "name": clean(projected["model_id"]), "hosts": list(projected["hosts"]),
                      "port": projected["port"], "mib": max(measured) if measured else None})
    stops.sort(key=lambda stop: (stop["port"] not in ports, stop["mib"] is None, -(stop["mib"] or 0)))
    processes = [process for process in inventory.get("unmanaged_gpu_processes") or []
                 if isinstance(process, dict) and process.get("node") in serving]
    for process in processes[:3]:
        name = str(process.get("process_name") or "?").rstrip("/").rpartition("/")[2] or "?"
        mib = process.get("used_memory_mib")
        size = f" · {gib_text(mib)}" if type(mib) is int and mib >= 0 else ""
        notes.append(f"On {host(process.get('node'))}: GPU process {clean(name)} (PID {process.get('pid') or '?'})"
                     f"{size} · not started by Pulsar · read-only")
    if len(processes) > 3:
        notes.append(f"and {len(processes) - 3} more GPU processes not started by Pulsar")
    return stops, notes


def stop_other_label(service: dict, width: int) -> str:
    """Stop MODEL on HOSTS (port P · N GiB per node), fitted to WIDTH.

    The facts are shown only when known, and say why the service is in the
    way, so they stay longest: where the label does not fit, a multi-node
    service's hosts become a count (Stop's question names them), then the
    model name is cut from its start, keeping its end and at least
    STOP_NAME_ROOM characters; then the facts give way, then the hosts give
    way to the spec prefix, before which the name is cut as far as needed.
    """
    count = len(service["hosts"])
    name, hosts = clean(service["name"]), ", ".join(service["hosts"]) or "its nodes"
    facts = []
    if service.get("port") is not None:
        facts.append(f"port {service['port']}")
    if service.get("mib") is not None:
        facts.append(gib_text(service["mib"]) + (" per node" if count > 1 else ""))
    places = [f" on {hosts}"] + ([f" on {count} nodes"] if count > 1 else [])
    with_facts = [place + f" ({' · '.join(facts)})" for place in places] if facts else []
    for tail, least in ([(tail, len(name)) for tail in with_facts] + [(tail, STOP_NAME_ROOM) for tail in with_facts]
                        + [(places[0], len(name)), (places[0], STOP_NAME_ROOM)]):
        room = width - len("Stop ") - len(tail)
        if room >= min(least, len(name)):
            return f"Stop {_keep_end(name, room)}{tail}"
    identity = f" [{service['conf'][:12]}]"
    room = width - len("Stop ") - len(identity)
    return f"Stop {_keep_end(name, room)}{identity}" if room >= 4 else f"Stop{identity}"


# A model name cut to fit a Stop choice keeps at least this many characters
# while other parts of the label can still give way.
STOP_NAME_ROOM = 20


def _keep_end(name: str, room: int) -> str:
    """NAME in ROOM characters: whole, or cut from its start, keeping its end."""
    return name if len(name) <= room else "…" + name[len(name) - room + 1:]


def start_choice_lines(records: list[dict], row: dict, archive_location: str, *, dry_run: bool = False,
                       stops: list[dict] = (), notes: list[str] = (), start_again: bool = False,
                       width: int | None = None) -> list[str]:
    """The menu lines after a refused Start or dry run: the title, Start again
    (Start now after a dry run) once a choice fixed something, the notes on
    what is in the way, then the choices. Empty when nothing applies.

    Each line is KIND, a tab, then its text; a stop-other line is
    stop-other, the spec ID, the model and the label.
    """
    width = width or terminal_width() - 2
    found = start_choices(records, row, archive_location,
                          [(stop["conf"], stop["name"], stop_other_label(stop, width)) for stop in stops])
    if not found and not start_again:
        return []
    lines = [f"title\t{clean(start_choices_title(records, dry_run=dry_run))}"]
    if start_again:
        lines.append("start-again\t" + ("Start now" if dry_run else "Start again"))
    lines += [f"note\t{clean(note)}" for note in notes]
    lines += ["\t".join(clean(part) for part in choice) for choice in found]
    return lines


def other_stop_question(conf: str, name: str, inventory: dict | None = None) -> str:
    """Stop's question for a Pulsar-managed service whose spec is not in this
    catalog, naming the nodes a service inventory taken just now saw it on."""
    from scripts.inventory_menu import project, project_others, unobserved_hosts
    model = clean(name)
    if inventory is None:
        return f"{stop_question(model, conf, None)} {OUTSIDE_CATALOG}"
    project(inventory, [])
    service = project_others(inventory, []).get(conf)
    if service is not None:
        return f"{stop_question(model, conf, service['where'])} {OUTSIDE_CATALOG}"
    if unobserved_hosts(inventory):
        return f"{stop_question(model, conf, None)} {OUTSIDE_CATALOG}"
    return (f"Stop {model} [{conf[:12]}]? No container of it was seen running just now; "
            f"Stop removes any that remain. {STOP_KEEPS} {OUTSIDE_CATALOG}")


def render_image_plan(row: dict, response: dict, node_names: dict[int, str], *, mode: str, width=None) -> bool:
    """Display the public image stage preview; never inspect or stage an image."""
    plan = response.get("result")
    if (type(response.get("schema_version")) is not int or response["schema_version"] != 1 or response.get("ok") is not True
            or not isinstance(plan, dict) or plan.get("kind") != "pulsar-image-check"
            or plan.get("operation") != "stage-image" or plan.get("model") != row["spec_id"]
            or plan.get("image") != row["image"]["digest"]):
        raise ValueError("image preview does not name the selected catalog spec and serving image")
    modes = {"pull-exact-digest": "Pull the exact digest from its registry",
             "stream-from-controller": "Copy the serving image from this node; no registry fallback"}
    ranks = plan.get("ranks")
    if (mode not in modes or plan.get("mode") != mode or not isinstance(ranks, list)
            or type(plan.get("nodes")) is not int or plan["nodes"] != row["geometry"]["nodes"]
            or len(ranks) != row["geometry"]["nodes"]
            or any(not isinstance(rank, dict) or type(rank.get("rank")) is not int or rank["rank"] != index
                   or type(rank.get("topology_index")) is not int or rank["topology_index"] < 0
                   or rank["topology_index"] not in node_names
                   or rank.get("state") not in ("ok", "missing", "other-name") for index, rank in enumerate(ranks))):
        raise ValueError("image preview does not establish every selected rank")
    if len({rank["topology_index"] for rank in ranks}) != len(ranks):
        raise ValueError("image preview repeats a selected node")
    out = TerminalWriter(width=width)
    out.emit("Serving image staging preview", role="heading")
    out.field("Model", row["model_id"])
    out.field("Spec", row["spec_id"])
    out.field("Image", plan["image"])
    out.field("Source", modes[plan["mode"]])
    # A pull records the pinned name over content already there; a copy stages it again.
    renamed = ("will add the pinned name; the content is already here" if plan["mode"] == "pull-exact-digest"
               else "will stage; it holds the same content under another name")
    actions = {"missing": "will stage", "ok": "already present", "other-name": renamed}
    for rank in ranks:
        node = node_names[rank["topology_index"]]
        out.field(f"Rank {rank['rank']}", f"{node}: {actions[rank['state']]}")
    missing = any(rank["state"] != "ok" for rank in ranks)
    out.emit("No service is started or replaced." if missing else "The serving image is already present; nothing to stage.")
    return missing


def _short(commit: object) -> str:
    return str(commit)[:8]


def _snapshot_identity(row: dict, snapshot: str | None) -> str:
    member = members(row).get(snapshot) if snapshot else None
    if member is None:
        member = next(iter(members(row).values()))
    return f"{member['model_id']} @ {_short(member['model_commit'])}"


def _node_list(node_ids: list[str], names: NodeNames) -> str:
    seen: list[str] = []
    for node_id in node_ids:
        if node_id not in seen:
            seen.append(node_id)
    return ", ".join(names(node_id) for node_id in seen)


def _plan_actions(plan: dict) -> list[dict]:
    if isinstance(plan.get("plan"), dict):
        plan = plan["plan"]
    if plan.get("kind") == "pulsar-preparation-set-plan":
        return [action for member in plan.get("snapshots") or [] for action in member.get("actions") or []]
    return list(plan.get("actions") or [])


def _plan_document(plan: dict) -> dict:
    return plan["plan"] if isinstance(plan.get("plan"), dict) else plan


def plan_blocked(plan: dict) -> bool:
    return _plan_document(plan).get("eligible") is False


SPEC_ID = re.compile(r"\b[0-9a-f]{64}\b")


def _count(number: int, one: str, many: str | None = None) -> str:
    return f"{number} {one if number == 1 else (many or one + 's')}"


def _view_size(view: dict) -> int | None:
    """Bytes of a prepared copy's files from its verification stamp (path -> [dev, ino, mode, size, ...])."""
    verification = view.get("verification")
    files = verification.get("files") if isinstance(verification, dict) else None
    if not isinstance(files, dict) or not files:
        return None
    sizes = [meta[3] for meta in files.values() if isinstance(meta, list) and len(meta) > 3]
    return sum(sizes) if len(sizes) == len(files) and all(type(value) is int for value in sizes) else None


def purge_parts(effect: dict) -> tuple[list[str], bool]:
    """What a purge does, in glossary words, omitting what it does not do; and whether
    it releases shared copies, whose files stay for other specs."""
    parts = []
    if effect["removed"]:
        size = f" ({short_size(effect['removed_bytes'])})" if effect.get("removed_bytes") else ""
        parts.append("delete " + _count(effect["removed"], "prepared copy", "prepared copies") + size)
    if effect.get("released_shared") is None:
        # A plan without its views cannot say which released copy is the home.
        if effect["released"]:
            parts.append("release " + _count(effect["released"], "prepared copy", "prepared copies")
                         + " whose files stay")
    else:
        if effect["released_shared"]:
            parts.append("release " + _count(effect["released_shared"], "shared copy", "shared copies"))
        if effect["released_home"]:
            parts.append("stop using the home as a prepared copy")
    if effect["pending"]:
        parts.append("clear " + _count(effect["pending"], "unfinished copy", "unfinished copies"))
    return parts, bool(effect.get("released_shared"))


def _joined(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def plan_effect(action: str, plan: dict) -> dict:
    """What a storage plan would change; the preview summary and the question share it.

    ``nothing`` is true when the plan copies, deletes and changes no files:
    Prepare where every rank already has its files, a purge with nothing
    recorded, a pin with every copy already in that state, or a move to the
    node that already holds the home.
    """
    document = _plan_document(plan)
    steps = _plan_actions(plan)
    if action == "prepare":
        # A manifest shared by several snapshots is copied once per node; later
        # snapshots reuse that copy, as preparation_estimate counts it.
        members = (document.get("snapshots") or []) if document.get("kind") == "pulsar-preparation-set-plan" \
            else [document]
        copies = {(step.get("node_id"), member.get("snapshot_manifest_id") or index)
                  for index, member in enumerate(members) for step in member.get("actions") or []
                  if step.get("action") == "copy"}
        return {"copied": len(copies), "reused": len(steps) - len(copies), "nothing": bool(steps) and not copies}
    if action == "purge":
        removed = sum(1 for step in steps if step.get("action") == "remove-copy")
        pending = len(plan.get("incomplete_preparations") or [])
        # The plan lists its views in the order of its actions; with them, a release
        # is told apart as the home or a shared copy, and deleted files have a size.
        views = document.get("views")
        paired = isinstance(views, list) and len(views) == len(steps) and all(isinstance(v, dict) for v in views)
        home = shared = size = None
        if paired:
            releases = [view for view, step in zip(views, steps) if step.get("action") != "remove-copy"]
            home = sum(1 for view in releases if view.get("is_home_view") is True)
            shared = len(releases) - home
            sizes = [_view_size(view) for view, step in zip(views, steps) if step.get("action") == "remove-copy"]
            size = sum(sizes) if sizes and all(value is not None for value in sizes) else None
        return {"removed": removed, "released": len(steps) - removed, "pending": pending,
                "released_home": home, "released_shared": shared, "removed_bytes": size,
                "nothing": not steps and not pending}
    if action in ("pin", "unpin"):
        views = document.get("views") or []
        changing = [view for view in views if view.get("pinned") is not (action == "pin")]
        return {"views": len(views), "changing": len(changing), "nothing": not changing,
                "nodes": [str(view["node_id"]) for view in changing if view.get("node_id")]}
    if action == "move":
        return {"nothing": document.get("transfer_route") == "already-home"}
    if action == "acquire":
        return {"nothing": document.get("action") == "reuse"}
    return {"nothing": False}


def _short_ids(text: str, keep: str | None = None) -> str:
    """Shorten each full 64-character spec ID to its 12-character prefix.

    KEEP is a path to show whole, such as a folder the operator must inspect,
    whose name can be a full spec ID.
    """
    if keep:
        return keep.join(_short_ids(part) for part in text.split(keep))
    return SPEC_ID.sub(lambda match: match.group(0)[:12], text)


def _source_size(source: dict) -> tuple[int | None, int | None]:
    files = source.get("files")
    if isinstance(files, list):
        sizes = [item.get("size") for item in files if isinstance(item, dict)]
        total = sum(sizes) if len(sizes) == len(files) and all(type(size) is int and size >= 0 for size in sizes) else None
        return len(files), total
    count = source.get("file_count")
    return (count if type(count) is int else None), source.get("total_bytes")


def blocked_choices(plan: dict, entries: list[dict]) -> list[tuple[str, str, str, str]]:
    """Menu choices for a blocked plan's Pulsar container blockers, as (kind, spec, model, label).

    Stop for each catalog spec whose Pulsar container mounts the files, then
    Live service inventory. A container that Pulsar did not start gets no
    choice: its fix is to remove it or its mount, which the menu never does.
    Neither does a folder at a prepared copy's path that no record names: the
    operator inspects it and removes it outside Pulsar.
    """
    specs: list[str] = []
    for item in _plan_document(plan).get("blocker_details") or []:
        container = item.get("container") if isinstance(item, dict) else None
        if isinstance(container, dict) and container.get("spec_id") and container["spec_id"] not in specs:
            specs.append(str(container["spec_id"]))
    models = {entry["spec_id"]: entry["model_id"] for entry in entries}
    choices = [("stop", spec, models[spec], f"Stop {models[spec]} ({spec[:12]})") for spec in specs if spec in models]
    if specs:
        choices.append(("inventory", "", "", "Open Live service inventory"))
    return choices


def _amount(size: object, count: object, unknown: str) -> str:
    """"1.20 TiB (40 files)", or UNKNOWN with "size not observed" when the plan has no size."""
    files = f"{count:,} files" if type(count) is int else ""
    if type(size) is not int or size < 0:
        return f"{unknown} ({files + ', ' if files else ''}size not observed)"
    return size_text(size) + (f" ({files})" if files else "")


def plan_summary(action: str, row: dict, plan: dict, *, names: NodeNames | None = None) -> list[str]:
    """One to three lines that lead a storage preview, built from its plan.

    What changes, where, how much, and the projected headroom; or why the plan
    is blocked. Sizes use binary units; exact byte counts stay in the full
    preview (model_library.render).
    """
    names = names or NodeNames()
    document = _plan_document(plan)
    effect = plan_effect(action, plan)
    if plan_blocked(plan):
        details = [item for item in document.get("blocker_details") or [] if isinstance(item, dict)]
        if details:
            causes = [_short_ids(blocker_detail_text(item, names) + (f"; {item['fix']}" if item.get("fix") else ""),
                                 keep=item.get("path") if item.get("code") == "unrecorded-destination" else None)
                      for item in details]
        else:
            causes = [_short_ids(blocker_message(str(item), names)) for item in document.get("blockers") or []
                      if isinstance(item, str)]
        # One mount over several copies repeats the same cause; show it once.
        causes = list(dict.fromkeys(causes))
        shown = causes if len(causes) <= 3 else causes[:2]
        lines = [f"Blocked: {text.rstrip('.')}." for text in shown] \
            or ["Blocked; the full preview has details."]
        if len(causes) > len(shown):
            lines.append(f"{_count(len(causes) - len(shown), 'more blocker')} in the full preview.")
        return lines
    if action == "prepare":
        plans = ([member for member in document.get("snapshots") or []]
                 if document.get("kind") == "pulsar-preparation-set-plan" else [document])
        _, nodes = preparation_estimate(plans)
        copying = [node for node, estimate in nodes.items() if estimate["copies"]]
        if effect["nothing"]:
            what = "the snapshot is" if len(plans) == 1 else ("both snapshots are" if len(plans) == 2
                                                           else f"all {len(plans)} snapshots are")
            return [f"Nothing to copy: {what} already on {_node_list(list(nodes), names)}. "
                    "Prepare verifies the files and records them."]
        sizes = [nodes[node]["new_copy"] for node in copying]
        size = sum(sizes) if all(value is not None for value in sizes) else None
        lines = [f"Copy {size_text(size)} to {_node_list(copying, names)} "
                 f"({_count(effect['copied'], 'new copy', 'new copies')}, {effect['reused']} reused)."]
        after = [(projected_headroom(nodes[node]["budgets"], nodes[node]["new_copy"]), node) for node in copying]
        if after and all(value is not None for value, _ in after):
            low, node = min(after, key=lambda item: item[0])
            lines.append(f"Short by about {size_text(-low)} on {names(node)}." if low < 0
                         else f"Estimated headroom after copying: {size_text(low)} on {names(node)} "
                              "(file sizes only; space is not reserved).")
        else:
            lines.append("Estimated headroom after copying: not observed.")
        return lines
    if action == "purge":
        if effect["nothing"]:
            return ["Nothing to purge: no prepared copies or unfinished copies are recorded for this spec."]
        parts, shared = purge_parts(effect)
        where = _node_list([str(step["node_id"]) for step in _plan_actions(plan) if step.get("node_id")], names)
        text = _joined(parts) + (f" on {where}." if where else ".")
        return [text[0].upper() + text[1:] + (" Shared files stay for other specs." if shared else "")]
    if action in ("pin", "unpin"):
        state = "pinned" if action == "pin" else "unpinned"
        if not effect["views"]:
            return [f"No prepared copies are recorded for this spec; nothing to {action}."]
        if effect["nothing"]:
            every = "The prepared copy is" if effect["views"] == 1 else f"All {effect['views']} prepared copies are"
            return [f"{every} already {state}; nothing changes."]
        unchanged = effect["views"] - effect["changing"]
        return [f"{action.capitalize()} {_count(effect['changing'], 'prepared copy', 'prepared copies')} on "
                f"{_node_list(effect['nodes'], names)}" + (f"; {unchanged} already {state}." if unchanged else ".")]
    if action == "move":
        target = names(document["destination_node"]) if document.get("destination_node") else "the selected node"
        if effect["nothing"]:
            return [f"Nothing to move: the home is already on {target}."]
        source = names(document["source_node"]) if document.get("source_node") else "its current node"
        routes = {"direct-ssh-roce": "a direct copy over the confirmed RoCE rail",
                  "controller-stream-relay": "streamed through this node over RoCE; this node keeps no copy"}
        route = routes.get(document.get("transfer_route"), document.get("transfer_route") or "route not stated")
        return [f"Move the home from {source} to {target}: {route}."]
    if action == "archive":
        home = document.get("home") or {}
        where = names(home["node_id"]) if home.get("node_id") else "its node"
        root = document.get("archive_root")
        return [f"Copy the home on {where} into the archive{' at ' + root if root else ''}; "
                "an archive already there is verified, never replaced."]
    if action == "restore":
        target = names(document["selected_node"]) if document.get("selected_node") else "the selected node"
        size, count = document.get("total_bytes"), document.get("file_count")
        available = (document.get("destination_space") or {}).get("available")
        lines = [f"Copy {_amount(size, count, 'the snapshot')} from the archive to {target}."]
        if type(available) is int and type(size) is int and available >= 0 and size >= 0:
            after = available - size
            lines.append(f"Short by about {size_text(-after)} on {target}." if after < 0
                         else f"Estimated disk free after the copy: {size_text(after)} on {target} "
                              "(file sizes only; space is not reserved).")
        else:
            lines.append(f"Disk free on {target}: not observed.")
        return lines
    if action == "acquire":
        # A reuse plan names its node only in the home it registers.
        node = document.get("selected_node") or (document.get("home") or {}).get("node_id")
        target = names(node) if node else "the selected node"
        if effect["nothing"]:
            return [f"Nothing to download: verified files are already recorded on {target}; Download registers them."]
        count, size = _source_size(document.get("source") or {})
        commit = document.get("snapshot_revision")
        model = document.get("model_id") or row["model_id"]
        return [f"Download {_amount(size, count, 'the files')} of {model}{' @ ' + _short(commit) if commit else ''} "
                f"to {target}."]
    if action == "remove":
        home = document.get("home") or {}
        where = names(home["node_id"]) if home.get("node_id") else "its node"
        return [f"Delete the home model files on {where}. No archive is required or checked.", HOME_REMOVAL_RECOVERY]
    raise ValueError(f"no preview summary for {action}")


def _start_place(nodes: int, place: str | None, names: NodeNames) -> str:
    """Where Start runs: the selected node, or each rank's node from the saved
    topology, which ordinary start uses in order. Without enough saved nodes,
    only the count, since start then refuses before anything runs."""
    if place:
        return place
    hosts = names.serving_hostnames(nodes) if nodes > 1 else None
    if not hosts:
        return _count(nodes, "node")
    return ", ".join(f"{host} (rank {rank})" for rank, host in enumerate(hosts))


def question(action: str, row: dict, *, plan: dict | None = None, snapshot: str | None = None,
             node: str | None = None, names: NodeNames | None = None, live: dict | None = None,
             distinguishing_fact: str | None = None) -> str:
    """Confirmation question naming the action, model, placement and consequence.

    When the catalog has other recipes of the same model, Start names
    DISTINGUISHING_FACT after the model, as the recipe list does, except a
    node count: the question already says where Start runs.
    """
    names = names or NodeNames()
    plan = plan or {}
    model = row["model_id"]
    identity = _snapshot_identity(row, snapshot)
    place = names(node) if node else None
    nodes = row["geometry"]["nodes"]
    warning = "Withdrawn spec. " if (row.get("review") or {}).get("status") == "withdrawn" else ""
    if action == "acquire":
        where = plan.get("selected_node") or (plan.get("home") or {}).get("node_id") or node
        target = names(where) if where else "the selected node"
        if plan.get("action") == "reuse":
            return f"Register the existing verified files of {identity} on {target}? Nothing is downloaded."
        return f"Download {identity} to {target}?"
    if action == "restore":
        target = names(plan.get("selected_node") or node) if (plan.get("selected_node") or node) else "the selected node"
        return f"Restore {identity} from the archive to {target}?"
    if action == "move":
        source = names(plan["source_node"]) if plan.get("source_node") else "its current node"
        target = names(plan.get("destination_node") or node) if (plan.get("destination_node") or node) else "the selected node"
        if plan.get("transfer_route") == "already-home":
            return f"Move the home of {identity} to {target}? It is already there; nothing is copied."
        return f"Move the home of {identity} from {source} to {target}?"
    if action == "prepare":
        steps = _plan_actions(plan)
        effect = plan_effect(action, plan)
        where = _node_list([str(step["node_id"]) for step in steps if step.get("node_id")], names) or place or "its nodes"
        if effect["nothing"]:
            return f"Prepare {model} on {where}? Nothing is copied; Prepare verifies and records the files already there."
        return (f"Prepare {model} on {where}? {_count(effect['copied'], 'new copy', 'new copies')}, "
                f"{effect['reused']} reused.")
    if action in ("pin", "unpin"):
        effect = plan_effect(action, plan)
        verb = LABELS[action].split()[0]
        if not effect["views"]:
            return f"{verb} prepared copies of {model}? None are recorded; nothing changes."
        if effect["nothing"]:
            state = "pinned" if action == "pin" else "unpinned"
            every = "It is" if effect["views"] == 1 else f"All {effect['views']} are"
            return f"{verb} prepared copies of {model}? {every} already {state}; nothing changes."
        consequence = "Purge refuses pinned copies." if action == "pin" else "Unpinned copies can be purged."
        return f"{verb} {_count(effect['changing'], 'prepared copy', 'prepared copies')} of {model}? {consequence}"
    if action == "purge":
        effect = plan_effect(action, plan)
        if effect["nothing"]:
            return f"Purge {model}? No prepared copies or unfinished copies are recorded; nothing is deleted."
        parts, shared = purge_parts(effect)
        where = _node_list([str(step["node_id"]) for step in _plan_actions(plan) if step.get("node_id")], names)
        return (f"Purge {model}: {_joined(parts)}{f' on {where}' if where else ''}? "
                + ("Shared files stay for other specs; the home and the archive are kept." if shared
                   else "The home and the archive are kept."))
    if action == "remove":
        home = (plan.get("plan") or plan).get("home") or {}
        where = names(home["node_id"]) if home.get("node_id") else "its node"
        return f"Remove the home model files of {identity} on {where}? {HOME_REMOVAL_RECOVERY}"
    if action == "archive":
        root = plan.get("archive_root")
        return (f"Create an archive of {identity}{' in ' + root if root else ''}? "
                "An existing archive is never replaced.")
    told = [part for part in (distinguishing_fact or "").split(" · ") if part and part != node_count(nodes)]
    recipe = f"{model} ({' · '.join(told)})" if told else model
    if action == "start":
        return (f"{warning}Start {recipe} on {_start_place(nodes, place, names)}? Start rechecks prerequisites "
                "and never replaces a running service. Loading can take several minutes.")
    if action == "start-memory":
        return (f"{warning}Accept the reduced free-memory headroom and retry Start for {recipe} on "
                f"{_start_place(nodes, place, names)}? Insufficient memory and all other blockers still prevent "
                "start. Loading can take several minutes.")
    if action == "stop":
        current = bool(live) and live_current(live)
        if current and live["kind"] == "absent":
            scope = f" on {place}" if place else ""
            return (f"Stop {model} [{row['spec_id'][:12]}]{scope}? No container of it was seen running just now; "
                    f"Stop removes any that remain. {STOP_KEEPS}")
        hosts = live["hosts"] if current and live["kind"] == "service" else []
        if not place:
            return stop_question(model, row["spec_id"], ", ".join(hosts) or None)
        # Stop with a selected node removes containers there only. A one-node
        # service's inventory names its node ID; otherwise compare hostnames.
        here = live.get("node_id") == node if hosts and live.get("node_id") else place in hosts
        elsewhere = "" if not hosts or here else (
            f" The service inventory saw it running on {', '.join(hosts)} just now, not on {place}.")
        return stop_question(model, row["spec_id"], place) + elsewhere
    raise ValueError(f"no confirmation for {action}")


STOP_KEEPS = "Model files, pins and archives are kept."
# Added to the Stop question of a Pulsar-managed service whose spec is not in this catalog.
OUTSIDE_CATALOG = "This service was started outside this catalog."


def stop_question(model: str, spec_id: str, where: str | None) -> str:
    """The Stop question, the same in the catalog and the inventory menus."""
    scope = f"on {where}" if where else "on its nodes"
    return f"Stop and remove the service containers of {model} [{spec_id[:12]}] {scope}? {STOP_KEEPS}"


def load_live(path: str | None, rows: list[dict]) -> dict | None:
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        return live_inventory(json.load(handle), rows)


def inventory_line(live: dict) -> str:
    """The catalog list's note on the captured inventory: its age and blind spots."""
    line = f"Running services: service inventory {age_text(live['age'])}"
    if live["unobserved"]:
        line += "; not observed: " + ", ".join(live["unobserved"])
    elif live["incomplete"]:
        line += "; a node could not be observed"
    return line + "."


# The home screen's Next line groups each spec's suggested step. Each entry is
# (singular, plural) wording after the spec count, in display order.
HOME_NEXT = {"not-checked": (NOT_CHECKED, NOT_CHECKED),
             "check": ("needs a check", "need a check"),
             "acquire": ("needs Download", "need Download"),
             "restore": ("needs Restore", "need Restore"),
             "prepare": ("needs Prepare", "need Prepare"),
             "start": ("ready to Start", "ready to Start")}


def home_next(rows: list[dict], archive_location: str, *, now=None, marked: bool = False) -> str | None:
    """One line for the home screen: what the saved records suggest, and their age.

    Uses the same suggestion as each recipe menu, without session outcomes or
    service observations; nothing here probes nodes. When some spec has a
    recorded home, the check counts cover only specs with one, so recipes
    this cluster never downloaded are not counted. None when no spec has a
    suggested step.
    """
    def has_home(row: dict) -> bool:
        return any(member.get("home") for member in members(row).values())

    any_home = any(has_home(row) for row in rows)
    counts: dict[str, int] = {}
    for row in rows:
        offered, _, _ = operations(row, archive_location)
        found = suggestion(row, offered, archive_location, now=now)
        if not found or found[0] not in HOME_NEXT:
            continue
        key = "not-checked" if found[0] == "check" and row.get("checked_at") is None else found[0]
        if key in ("not-checked", "check") and any_home and not has_home(row):
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    parts = []
    for key, (one, many) in HOME_NEXT.items():
        if key in counts:
            count = counts[key]
            parts.append(f"{count} spec{'' if count == 1 else 's'} {one if count == 1 else many}")
    choice = "**Catalog and storage**" if marked else "Catalog and storage"
    line = "Next: " + " · ".join(parts) + f" — open {choice}."
    ages = [row["observation_age_seconds"] for row in rows
            if row.get("checked_at") and type(row.get("observation_age_seconds")) is int]
    if ages:
        line += f" Newest saved check: {age_text(min(ages))}."
    return line


def read_row(stream, spec_id: str) -> tuple[dict, list[dict]]:
    """The selected recipe and every entry of the catalog document on STREAM."""
    entries = json.load(stream).get("entries") or []
    rows = [row for row in entries if row.get("spec_id") == spec_id]
    if len(rows) != 1:
        raise ValueError("the selected recipe is not in the catalog")
    return rows[0], entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    labels = sub.add_parser("labels", help="spec ID and menu label per line; catalog JSON on stdin")
    labels.add_argument("--compare-with", help="only other catalog specs supported by spec compare")
    live_help = "service inventory JSON this menu session captured"
    outcome_help = "SPEC:ACTION:STATUS:EPOCH for a Live status, Start or Stop in this session"
    files_help = "model files --json this menu session saved; an unreadable or failed scan is ignored"
    labels.add_argument("--live", help=live_help)
    labels.add_argument("--service-outcome", action="append", default=[], help=outcome_help)
    # Accepted with the session's other observations; a list label adds no word for found files.
    labels.add_argument("--files", help=files_help)
    warning = sub.add_parser("memory-warning", help="whether start reported only an acknowledgeable memory warning")
    warning.add_argument("--blockers-file", required=True)
    choices = sub.add_parser("start-choices", help="a title, then one menu choice per line for recorded start "
                             "blockers; catalog JSON on stdin")
    choices.add_argument("--spec-id", required=True)
    choices.add_argument("--archive-location", required=True, choices=sorted(ARCHIVE_LOCATION))
    choices.add_argument("--blockers-file", required=True)
    choices.add_argument("--dry-run", action="store_true", help="the blockers come from Check launch prerequisites")
    choices.add_argument("--live", help="a service inventory captured after the refusal; names the services "
                         "on the serving nodes when start found a busy port or too little memory")
    choices.add_argument("--node", help="the node_id a one-node spec was started on")
    choices.add_argument("--repo-root", default=ROOT, help="the catalog that names other services' models")
    choices.add_argument("--start-again", action="store_true", help="a choice fixed something: offer Start again")
    service_check = sub.add_parser("service-blockers", help="whether start found a busy port or too little memory")
    service_check.add_argument("--blockers-file", required=True)
    other_stop = sub.add_parser("other-stop-question", help="Stop's question for a Pulsar-managed service "
                                "whose spec is not in this catalog")
    other_stop.add_argument("--conf", required=True, help="the service's full spec ID")
    other_stop.add_argument("--served-name", required=True)
    other_stop.add_argument("--live", help="a service inventory captured just now")
    nodes = sub.add_parser("node-choices", help="a title, then one choice per confirmed node from saved "
                           "records; catalog JSON on stdin")
    nodes.add_argument("--spec-id", required=True)
    nodes.add_argument("--action", required=True)
    nodes.add_argument("--snapshot")
    nodes.add_argument("--node-name", action="append", default=[], help="NODE_ID=HOSTNAME, in topology order")
    nodes.add_argument("--live", help="with --action stop: a service inventory captured just now")
    nodes.add_argument("--files", help=files_help)
    image = sub.add_parser("image-plan", help="render an existing public image stage preview")
    image.add_argument("--spec-id", required=True)
    image.add_argument("--plan-file", required=True)
    image.add_argument("--mode", required=True, choices=("pull-exact-digest", "stream-from-controller"))
    image.add_argument("--node-name", action="append", default=[])
    view = sub.add_parser("view", help="menu lines for one recipe; catalog JSON on stdin")
    view.add_argument("--spec-id", required=True)
    view.add_argument("--archive-location", required=True, choices=sorted(ARCHIVE_LOCATION))
    view.add_argument("--repo-root", default=ROOT)
    view.add_argument("--read-only", action="store_true")
    view.add_argument("--live", help=live_help)
    view.add_argument("--service-outcome", action="append", default=[], help=outcome_help)
    view.add_argument("--files", help=files_help)
    view.add_argument("--after", choices=sorted(MUTATIONS | INTERRUPTIONS
                                                | {"start", "stop", "check", "check-failed", "storage-failed"}))
    view.add_argument("--after-at", type=int, help="when --after happened, as a Unix time")
    preview = sub.add_parser("preview", help="a storage preview's summary lines; catalog JSON on stdin")
    preview.add_argument("--spec-id", required=True)
    preview.add_argument("--action", required=True, choices=sorted(MUTATIONS))
    preview.add_argument("--plan-file", required=True)
    blocked = sub.add_parser("blocked-choices", help="choices for a blocked plan; catalog JSON on stdin")
    blocked.add_argument("--plan-file", required=True)
    home = sub.add_parser("next", help="the home screen's Next line; catalog JSON on stdin")
    home.add_argument("--archive-location", required=True, choices=sorted(ARCHIVE_LOCATION))
    confirm = sub.add_parser("confirm", help="confirmation question; catalog JSON on stdin")
    confirm.add_argument("--spec-id", required=True)
    confirm.add_argument("--action", required=True, choices=sorted(MUTATIONS | {"start", "stop", "start-memory"}))
    confirm.add_argument("--plan-file")
    confirm.add_argument("--snapshot")
    confirm.add_argument("--node")
    confirm.add_argument("--live", help=live_help)
    confirm.add_argument("--service-outcome", action="append", default=[], help=outcome_help)
    # Accepted with the session's other observations; no question names found files.
    confirm.add_argument("--files", help=files_help)
    args = parser.parse_args(argv)
    try:
        if args.command == "memory-warning":
            from scripts.start_blockers import read
            return 0 if memory_warning_only(read(args.blockers_file)) else 1
        if args.command == "service-blockers":
            from scripts.start_blockers import read
            return 0 if service_blockers(read(args.blockers_file)) else 1
        if args.command == "other-stop-question":
            # No catalog row exists for this service, so nothing is read from stdin.
            if not SPEC_ID.fullmatch(args.conf):
                raise ValueError("the service's spec ID must be 64 lowercase hex characters")
            inventory = None
            if args.live:
                with open(args.live, encoding="utf-8") as handle:
                    inventory = json.load(handle)
            print(clean(other_stop_question(args.conf, args.served_name, inventory)))
            return 0
        if args.command == "labels":
            width = terminal_width()
            entries = json.load(sys.stdin).get("entries") or []
            if args.compare_with and any(entry["spec_id"] == args.compare_with and entry.get("historical") for entry in entries):
                raise ValueError("spec comparison supports schemas 2 and 3; use Show details for this historical spec")
            live = load_live(args.live, entries)
            outcomes = service_outcomes(args.service_outcome, names=NodeNames.saved())
            if live:
                print(f"observed\t{clean(inventory_line(live))}")
            # Facts and short names come from every entry, so a label reads the
            # same whichever entries this list leaves out.
            facts, short_names = distinguishing_facts(entries), short_model_names(entries)
            for entry in entries:
                if args.compare_with and (entry["spec_id"] == args.compare_with or entry.get("historical")):
                    continue
                service = live_fact(entry, live, outcomes.get(entry["spec_id"]))
                spec = entry["spec_id"]
                label = menu_label(entry, width, service, facts.get(spec), short_names.get(spec))
                print(f"{spec}\t{label}")
            return 0
        if args.command == "blocked-choices":
            with open(args.plan_file, encoding="utf-8") as handle:
                plan = json.load(handle)
            if not isinstance(plan, dict):
                raise ValueError("plan must be a JSON object")
            for choice in blocked_choices(plan, json.load(sys.stdin).get("entries") or []):
                print("\t".join(clean(part) for part in choice))
            return 0
        if args.command == "next":
            # The home screen renders the menu choice's mark (terminal_format.py --text).
            line = home_next(json.load(sys.stdin).get("entries") or [], args.archive_location, marked=True)
            if line:
                print(clean(line))
            return 0
        row, entries = read_row(sys.stdin, args.spec_id)
        if args.command == "start-choices":
            # Each line is TITLE or ACTION, a tab, then a nonempty label. Nothing
            # is printed, and the status is 1, when no choice applies.
            from scripts.start_blockers import read
            records = read(args.blockers_file)
            stops, notes = [], []
            if args.live and service_blockers(records):
                # An inventory that cannot be read leaves today's choices.
                try:
                    from scripts.release_consumer import list_releases
                    with open(args.live, encoding="utf-8") as handle:
                        inventory = json.load(handle)
                    node_ids = serving_node_ids(row, NodeNames.saved(), args.node)
                    stops, notes = services_in_the_way(inventory, row, node_ids, list_releases(args.repo_root),
                                                       blocked_ports(records))
                except (ValueError, OSError, KeyError, TypeError, AttributeError):
                    stops, notes = [], []
            lines = start_choice_lines(records, row, args.archive_location, dry_run=args.dry_run, stops=stops,
                                       notes=notes, start_again=args.start_again)
            if not lines:
                return 1
            print("\n".join(lines))
            return 0
        if args.command == "node-choices":
            # Each line is "title" and the question; "seen" and the node_id
            # that answers it, so nothing is asked; "default" and the node_id
            # the cursor starts on; or "node", a node_id and its label.
            nodes = []
            for value in args.node_name:
                node_id, separator, hostname = value.partition("=")
                if not separator or not node_id or not hostname:
                    raise ValueError("node names must use NODE_ID=HOSTNAME")
                nodes.append((node_id, hostname))
            ids = [node_id for node_id, _ in nodes]
            seen = seen_node(row, load_live(args.live, [row]), ids) if args.action == "stop" else None
            # Files the session's scan found with no record naming them: one
            # confirmed node holding them is where Download would reuse them.
            found = [node_id for node_id in unrecorded_files(row, found_files(args.files), args.snapshot)
                     if node_id in ids]
            print(f"title\t{clean(NODE_QUESTIONS.get(args.action, NODE_QUESTION))}")
            if seen is not None:
                print(f"seen\t{seen}")
            elif len(found) == 1:
                print(f"default\t{found[0]}")
            elif args.action not in NODE_UNSELECTED and recorded_node(row) in ids:
                print(f"default\t{recorded_node(row)}")
            width = terminal_width() - 2
            for node_id, hostname in nodes:
                label = node_label(row, node_id, hostname, width, args.snapshot, seen, found)
                print(f"node\t{node_id}\t{clean(label)}")
            return 0
        names = NodeNames.saved()
        if args.command == "image-plan":
            with open(args.plan_file, encoding="utf-8") as handle:
                response = json.load(handle)
            node_names = {}
            for value in args.node_name:
                index, separator, name = value.partition("=")
                if not separator or not index.isdigit() or not name:
                    raise ValueError("node names must use INDEX=NAME")
                node_names[int(index)] = name
            if not isinstance(response, dict):
                raise ValueError("image preview must be a public result envelope")
            return 0 if render_image_plan(row, response, node_names, mode=args.mode) else 1
        if args.command == "view":
            published = published_results(args.repo_root, row["spec_id"])
            fact = live_fact(row, load_live(args.live, [row]),
                             service_outcomes(args.service_outcome, names=names).get(row["spec_id"]))
            after_age = (max(0, int(datetime.now(timezone.utc).timestamp()) - args.after_at)
                         if args.after_at is not None else None)
            print("\n".join(view_lines(row, args.archive_location, after=args.after, names=names,
                                      published=published, read_only=args.read_only, live=fact,
                                      after_age=after_age, files=found_files(args.files))))
            return 0
        if args.command == "preview":
            with open(args.plan_file, encoding="utf-8") as handle:
                plan = json.load(handle)
            if not isinstance(plan, dict):
                raise ValueError("plan must be a JSON object")
            out = TerminalWriter()
            for line in plan_summary(args.action, row, plan, names=names):
                out.emit(clean(line))
            return 3 if plan_blocked(plan) else 0
        plan = None
        if args.plan_file:
            with open(args.plan_file, encoding="utf-8") as handle:
                plan = json.load(handle)
            if not isinstance(plan, dict):
                raise ValueError("plan must be a JSON object")
            if plan_blocked(plan):
                return 3
        if args.action in ("start", "start-memory"):
            notice = withdrawal_notice(row)
            if notice:
                out = TerminalWriter(stream=sys.stderr)
                out.blank()
                for line in notice:
                    out.emit(line)
        service = live_fact(row, load_live(args.live, [row]),
                            service_outcomes(args.service_outcome, names=names).get(row["spec_id"]))
        # Start names what tells this recipe apart when the document on stdin
        # is the whole catalog; one recipe's document has nothing to compare.
        told = distinguishing_facts(entries).get(row["spec_id"]) if args.action in ("start", "start-memory") else None
        fact = told.words() if told else None
        print(clean(question(args.action, row, plan=plan, snapshot=args.snapshot, node=args.node, names=names,
                             live=service, distinguishing_fact=fact)))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        from scripts.terminal_format import error_line
        error_line(exc, tag='catalog menu')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
