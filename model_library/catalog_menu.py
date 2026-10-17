"""Interactive catalog menu decisions from saved records only.

Decides which operations the catalog menu offers for one recipe, which it
leaves out and why, one suggested next step (also as the command that runs
it), the recipe list labels, and the wording of confirmation questions. Input
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
import sys
from typing import Any

from .catalog import ROOT, age_seconds, age_text, archive_facts, archive_text, blocker_text, files_text, recipe_text
from .published_results import collect as published_results, compact_lines as published_lines
from .node_names import NodeNames
from scripts.terminal_format import TerminalWriter, terminal_width

MAIN = ("check", "acquire", "restore", "prepare", "start", "stop", "status")
LAUNCH = ("readiness", "image-check", "image-stage")
STORAGE = ("budget", "move", "archive", "verify", "pin", "unpin", "purge", "remove")
LABELS = {
    "check": "Check now", "acquire": "Download", "restore": "Restore", "prepare": "Prepare",
    "start": "Start", "stop": "Stop", "status": "Live status",
    "readiness": "Check launch prerequisites", "image-check": "Check pinned image",
    "image-stage": "Stage pinned image",
    "budget": "Storage budget (all nodes)",
    "move": "Move home", "archive": "Create archive", "verify": "Verify archive",
    "pin": "Pin prepared copies", "unpin": "Unpin prepared copies",
    "purge": "Purge prepared copies", "remove": "Remove home",
}
# Operations that change model files or records; a success makes the saved
# check out of date until the next Check now.
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
                    "modified": service["modified"], "age": live["age"], "source": "inventory"}
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
                   "model_id": row["model_id"], "model_commit": row["snapshot_revision"]}}


def recovery_snapshots(row: dict) -> list[str | None]:
    """Unregistered snapshots first, in the same order before/after JSON encoding."""
    snapshots = members(row)
    return sorted(snapshots, key=lambda name: (bool(snapshots[name].get("home")),
                                             name != "target", name or ""))


def _location_reason(archive_location: str) -> str:
    return "archives are disabled" if archive_location == "disabled" else "no archive location is configured"


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
            eligible[action] = [str(n) for n in with_home]
    return offered, hidden, eligible


def suggestion(row: dict, offered: list[str], archive_location: str, after: str | None = None,
               names: NodeNames | None = None, *, now=None, live: dict | None = None) -> tuple[str, str] | None:
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
    if after == "service-interrupted" and not superseded:
        return "status", "service outcome unconfirmed; current service state is unknown"
    if after == "image-interrupted" and "image-check" in offered:
        return "image-check", "staging outcome unconfirmed; inspect the pinned image before retrying"
    if after == "check-failed":
        return "check", "the last check did not record an observation"
    if after == "start" and not superseded:
        return "status", "started from this menu; live status observes the service"
    if live and live["kind"] in ("service", "exists") and "status" in offered:
        seen = live_line(live).removeprefix("Live service: ").rstrip(".")
        return "status", seen + ("; Live status shows its state" if live.get("source") == "status"
                                 else "; Live status checks it now")
    if after in MUTATIONS:
        return "check", f"{LABELS[after].lower()} ran after the last check"
    if row.get("checked_at") is None:
        return "check", "no saved check"
    if age is None or age > STALE_SECONDS:
        return "check", "last check " + age_text(age)
    local = row.get("local_state", "unknown")
    blocker = "saved blocker: " + str(blocker_text(row["blockers"][0], row, names)) if row.get("blockers") else None
    if local == "unknown":
        return "check", blocker or "file state was not established by the last check"
    snapshots = members(row)
    missing = [name for name in recovery_snapshots(row) if not snapshots[name].get("home")]
    if missing:
        # Home operations act on one snapshot. Use that snapshot's reconciled
        # archive fact, not the aggregate state or the existence of a record.
        name = missing[0]
        reason = "no home recorded" + (f" for snapshot {name}" if name is not None else "")
        if archive_location == "configured":
            kind, _, wording = archive_facts(row, now)[name]
            reason += f"; archive: {wording}"
            if kind in ("present", "verified") and "restore" in offered:
                return "restore", reason + "; Restore rechecks contents"
        else:
            reason += "; " + _location_reason(archive_location)
        if "acquire" in offered:
            return "acquire", reason
        return None
    if start_unsupported(row):
        return None
    if local in ("missing", "changed") and "prepare" in offered:
        reason = "files changed since they were verified" if local == "changed" else "files not prepared on every rank"
        return "prepare", f"{reason} (checked {age_text(age)})"
    if blocker:
        return "check", blocker
    if local == "ready":
        if live and live["kind"] in ("unknown", "changed") and "status" in offered:
            return "status", live_line(live).removeprefix("Live service: ").rstrip(".") + "; check before starting"
        return "start", "files prepared as of the last check; start rechecks prerequisites"
    return None


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


# Files state in a recipe label: the same saved facts, in a few words.
LABEL_FILES = {"ready": "files prepared", "missing": "not prepared", "changed": "files changed",
               "unknown": "files unknown"}


def state_parts(row: dict) -> list[str]:
    """Saved state for a recipe label, files first; review status only when the spec sets one."""
    parts = [LABEL_FILES[row["local_state"]] if row.get("checked_at") else "never checked"]
    if start_unsupported(row):
        parts.append("start unsupported")
    status = (row.get("review") or {}).get("status")
    if status:
        parts.append(str(status))
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


def menu_label(row: dict, width: int, live: dict | None = None) -> str:
    """One recipe list entry that fits the menu: model [spec] state.

    Critical warnings take space before optional model/file context. Ordinary
    entries retain their model-first layout. A service seen in this session
    leads the state; the saved files state yields to it when space runs out,
    since the recipe frame repeats it. Gum's two-column cursor is the only
    text drawn before the label.
    """
    seen = live_label(live)
    budget = max(32, min(100, width)) - 2
    model = clean(row["model_id"])
    review = (row.get("review") or {}).get("status")
    critical = (["withdrawn"] if review == "withdrawn" else [])
    if start_unsupported(row):
        critical.append("start unsupported")
    if critical:
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
        files = seen or (LABEL_FILES[row["local_state"]] if row.get("checked_at") else "never checked")
        context = f"{model} · {files}" + (f" · {review}" if review and review != "withdrawn" else "")
        if len(context) > room:
            context = context[:room - 3] + "..."
        return heading + " " + context
    parts = state_parts(row)
    if seen:
        parts.insert(0, seen)
        if len(model) + len(f" [{row['spec_id'][:12]}] " + " · ".join(parts)) > budget:
            parts.pop(1)
    suffix = f" [{row['spec_id'][:12]}] " + " · ".join(parts)
    if len(model) + len(suffix) > budget and len(model) > 11:
        model = model[:max(8, budget - len(suffix) - 3)] + "..."
    text = model + suffix
    return text if len(text) <= budget else text[:budget - 3] + "..."


def explicit_settings(arguments: list[str]) -> str:
    """Summarize recorded values only; do not guess engine defaults or precedence."""
    labels = {"--max-model-len": "context", "--max-num-seqs": "max sequences", "--quantization": "quantization"}
    values: dict[str, list[str]] = {flag: [] for flag in labels}
    for index, token in enumerate(arguments):
        flag, separator, value = token.partition("=")
        if flag in labels:
            if not separator:
                value = arguments[index + 1] if index + 1 < len(arguments) and not arguments[index + 1].startswith("--") else "not supplied"
            values[flag].append(value)
    parts = [f"{labels[flag]} {items[0] if len(items) == 1 else 'has repeated values; see details'}"
             for flag, items in values.items() if items]
    return "; ".join(parts) if parts else "see Show details"


def header(row: dict, hidden: dict[str, str], suggested: tuple[str, str] | None, width: int, *, now=None,
           published=None, live: dict | None = None, read_only: bool = False) -> list[str]:
    buffer = io.StringIO()
    out = TerminalWriter(width=width, stream=buffer)
    out.emit(f"{row['model_id']} [{row['spec_id'][:12]}]")
    for line in withdrawal_notice(row):
        out.emit(line)
    out.emit("Selected recipe: " + recipe_text(row["geometry"]))
    digest = str((row.get("image") or {}).get("digest") or "not recorded")
    image = digest if len(digest) <= 24 else digest[:24] + "…"
    out.emit(f"Image: {image}")
    out.emit("Settings: " + explicit_settings(row.get("engine_args") or []))
    review = row.get("review") or {}
    if review.get("status") and review["status"] != "withdrawn":
        out.emit(f"Maintainer review (advisory): {review['status']} · "
                 f"{review.get('reviewed_at') or 'date not recorded'}")
    out.emit("Current observations (saved)")
    out.emit(f"Files: {files_text(row)} · Archive: {archive_text(row, now)}")
    out.emit(live_line(live, read_only=read_only))
    for line in published_lines(published, now=now):
        out.emit(line)
    if suggested:
        out.emit(f"Suggested: {LABELS[suggested[0]]} — {suggested[1]}")
    by_reason: dict[str, list[str]] = {}
    for action in MAIN + LAUNCH + STORAGE:
        if action in hidden:
            by_reason.setdefault(hidden[action], []).append(LABELS[action])
    for reason, labels in by_reason.items():
        out.emit(f"Not shown: {', '.join(labels)} ({reason})")
    if by_reason:
        out.emit("These are saved records; Check now refreshes them.")
    return buffer.getvalue().splitlines()


def clean(text: object) -> str:
    return " ".join(str(text).split())


def view_lines(row: dict, archive_location: str, *, after: str | None = None,
               width: int | None = None, names: NodeNames | None = None, now=None,
               published=None, read_only=False, live: dict | None = None) -> list[str]:
    offered, hidden, eligible = ([], {}, {}) if read_only else operations(row, archive_location)
    suggested = None if read_only else suggestion(row, offered, archive_location, after, names, now=now, live=live)
    frame_width = max(32, (width or terminal_width()) - 4)
    lines = [f"recipe\t{row['geometry']['nodes']}\t{clean(row['model_id'])}"]
    lines += ["header\t" + clean(line) for line in header(row, hidden, suggested, frame_width, now=now,
                                                          published=published, live=live, read_only=read_only)]
    for action in offered:
        group = "main" if action in MAIN else ("launch" if action in LAUNCH else "storage")
        label = LABELS[action] + (" (suggested)" if suggested and suggested[0] == action else "")
        lines.append(f"option\t{group}\t{action}\t{label}")
    if suggested:
        lines.append(f"suggest\t{suggested[0]}")
    for action, snapshots in eligible.items():
        for name in snapshots:
            lines.append(f"snapshot\t{action}\t{clean(name)}")
    return lines


def memory_warning_only(records: list[dict]) -> bool:
    """Offer acknowledgement only for the existing start check's warning code."""
    return bool(records) and all(isinstance(record, dict)
                                and record.get("stage") == "check"
                                and record.get("blocker") == "memory_warning" for record in records)


def render_image_plan(row: dict, response: dict, node_names: dict[int, str], *, mode: str, width=None) -> bool:
    """Display the public image stage preview; never inspect or stage an image."""
    plan = response.get("result")
    if (type(response.get("schema_version")) is not int or response["schema_version"] != 1 or response.get("ok") is not True
            or not isinstance(plan, dict) or plan.get("kind") != "pulsar-image-check"
            or plan.get("operation") != "stage-image" or plan.get("model") != row["spec_id"]
            or plan.get("image") != row["image"]["digest"]):
        raise ValueError("image preview does not name the selected catalog spec and pinned image")
    modes = {"pull-exact-digest": "Pull the exact digest from its registry",
             "stream-from-controller": "Copy the pinned image from this node; no registry fallback"}
    ranks = plan.get("ranks")
    if (mode not in modes or plan.get("mode") != mode or not isinstance(ranks, list)
            or type(plan.get("nodes")) is not int or plan["nodes"] != row["geometry"]["nodes"]
            or len(ranks) != row["geometry"]["nodes"]
            or any(not isinstance(rank, dict) or type(rank.get("rank")) is not int or rank["rank"] != index
                   or type(rank.get("topology_index")) is not int or rank["topology_index"] < 0
                   or rank["topology_index"] not in node_names
                   or rank.get("state") not in ("ok", "missing") for index, rank in enumerate(ranks))):
        raise ValueError("image preview does not establish every selected rank")
    if len({rank["topology_index"] for rank in ranks}) != len(ranks):
        raise ValueError("image preview repeats a selected node")
    out = TerminalWriter(width=width)
    out.emit("Pinned image staging preview")
    out.field("Model", row["model_id"])
    out.field("Spec", row["spec_id"])
    out.field("Image", plan["image"])
    out.field("Source", modes[plan["mode"]])
    for rank in ranks:
        node = node_names[rank["topology_index"]]
        action = "will stage" if rank["state"] == "missing" else "already present"
        out.field(f"Rank {rank['rank']}", f"{node}: {action}")
    missing = any(rank["state"] == "missing" for rank in ranks)
    out.emit("No service is started or replaced." if missing else "The pinned image is already present; nothing to stage.")
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


def plan_blocked(plan: dict) -> bool:
    document = plan["plan"] if isinstance(plan.get("plan"), dict) else plan
    return document.get("eligible") is False


def question(action: str, row: dict, *, plan: dict | None = None, snapshot: str | None = None,
             node: str | None = None, names: NodeNames | None = None) -> str:
    """Confirmation question naming the action, model, placement and consequence."""
    names = names or NodeNames()
    plan = plan or {}
    model = row["model_id"]
    identity = _snapshot_identity(row, snapshot)
    place = names(node) if node else None
    nodes = row["geometry"]["nodes"]
    warning = "Withdrawn spec. " if (row.get("review") or {}).get("status") == "withdrawn" else ""
    if action == "acquire":
        target = names(plan.get("selected_node") or node) if (plan.get("selected_node") or node) else "the selected node"
        if plan.get("action") == "reuse":
            return f"Register the existing verified files of {identity} on {target}?"
        return f"Download {identity} to {target}?"
    if action == "restore":
        target = names(plan.get("selected_node") or node) if (plan.get("selected_node") or node) else "the selected node"
        return f"Restore {identity} from the archive to {target}?"
    if action == "move":
        source = names(plan["source_node"]) if plan.get("source_node") else "its current node"
        target = names(plan.get("destination_node") or node) if (plan.get("destination_node") or node) else "the selected node"
        return f"Move the home of {identity} from {source} to {target}?"
    if action == "prepare":
        steps = _plan_actions(plan)
        copied = sum(1 for step in steps if step.get("action") == "copy")
        reused = len(steps) - copied
        where = _node_list([str(step["node_id"]) for step in steps if step.get("node_id")], names) or place or "its nodes"
        return f"Prepare {model} on {where}? {copied} new copies, {reused} reused."
    if action in ("pin", "unpin"):
        count = len(plan.get("views") or [])
        effect = "Purge refuses pinned copies." if action == "pin" else "Unpinned copies can be purged."
        return f"{LABELS[action].split()[0]} {count} prepared copies of {model}? {effect}"
    if action == "purge":
        steps = _plan_actions(plan)
        removed = sum(1 for step in steps if step.get("action") == "remove-copy")
        released = len(steps) - removed
        pending = len(plan.get("incomplete_preparations") or [])
        staging = f" and {pending} incomplete preparations" if pending else ""
        return (f"Purge {model}: delete {removed} working copies{staging}, release {released} bindings? "
                "The home and the archive are kept.")
    if action == "remove":
        home = (plan.get("plan") or plan).get("home") or {}
        where = names(home["node_id"]) if home.get("node_id") else "its node"
        return f"Remove the home of {identity} on {where}? Files come back only through Restore or Download."
    if action == "archive":
        root = plan.get("archive_root")
        return (f"Create an archive of {identity}{' in ' + root if root else ''}? "
                "An existing archive is never replaced.")
    if action == "start":
        where = place or f"{nodes} nodes"
        return f"{warning}Start {model} on {where}? Start rechecks prerequisites and never replaces a running service."
    if action == "start-memory":
        where = place or f"{nodes} nodes"
        return (f"{warning}Accept the reduced free-memory headroom and retry Start for {model} on {where}? "
                "Insufficient memory and all other blockers still prevent start.")
    if action == "stop":
        where = f" on {place}" if place else ""
        return f"Stop {model}{where}? Model files and pins are kept."
    raise ValueError(f"no confirmation for {action}")


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


def read_row(stream, spec_id: str) -> dict:
    document = json.load(stream)
    rows = [row for row in document.get("entries") or [] if row.get("spec_id") == spec_id]
    if len(rows) != 1:
        raise ValueError("the selected recipe is not in the catalog")
    return rows[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    labels = sub.add_parser("labels", help="spec ID and menu label per line; catalog JSON on stdin")
    labels.add_argument("--compare-with", help="only other catalog specs supported by spec compare")
    live_help = "service inventory JSON this menu session captured"
    outcome_help = "SPEC:ACTION:STATUS:EPOCH for a Live status, Start or Stop in this session"
    labels.add_argument("--live", help=live_help)
    labels.add_argument("--service-outcome", action="append", default=[], help=outcome_help)
    warning = sub.add_parser("memory-warning", help="whether start reported only an acknowledgeable memory warning")
    warning.add_argument("--blockers-file", required=True)
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
    view.add_argument("--after", choices=sorted(MUTATIONS | INTERRUPTIONS | {"start", "stop", "check", "check-failed"}))
    confirm = sub.add_parser("confirm", help="confirmation question; catalog JSON on stdin")
    confirm.add_argument("--spec-id", required=True)
    confirm.add_argument("--action", required=True, choices=sorted(MUTATIONS | {"start", "stop", "start-memory"}))
    confirm.add_argument("--plan-file")
    confirm.add_argument("--snapshot")
    confirm.add_argument("--node")
    args = parser.parse_args(argv)
    try:
        if args.command == "memory-warning":
            from scripts.start_blockers import read
            return 0 if memory_warning_only(read(args.blockers_file)) else 1
        if args.command == "labels":
            width = terminal_width()
            entries = json.load(sys.stdin).get("entries") or []
            if args.compare_with and any(entry["spec_id"] == args.compare_with and entry.get("historical") for entry in entries):
                raise ValueError("spec comparison supports schemas 2 and 3; use Show details for this historical spec")
            live = load_live(args.live, entries)
            outcomes = service_outcomes(args.service_outcome, names=NodeNames.saved())
            if live:
                print(f"observed\t{clean(inventory_line(live))}")
            for entry in entries:
                if args.compare_with and (entry["spec_id"] == args.compare_with or entry.get("historical")):
                    continue
                fact = live_fact(entry, live, outcomes.get(entry["spec_id"]))
                print(f"{entry['spec_id']}\t{menu_label(entry, width, fact)}")
            return 0
        row = read_row(sys.stdin, args.spec_id)
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
            print("\n".join(view_lines(row, args.archive_location, after=args.after, names=names,
                                      published=published, read_only=args.read_only, live=fact)))
            return 0
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
        print(clean(question(args.action, row, plan=plan, snapshot=args.snapshot, node=args.node, names=names)))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        from scripts.terminal_format import error_line
        error_line(exc, tag='catalog menu')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
