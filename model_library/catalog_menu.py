"""Interactive catalog menu decisions from saved records only.

Decides which operations the catalog menu offers for one recipe, which it
leaves out and why, one suggested next step (also as the command that runs
it), the recipe list labels, and the wording of confirmation questions. Input
is the catalog projection (``pulsar models show --json``) and the
archive-location status; nothing here probes nodes. Leaving an operation out
is a menu convenience: every operation still enforces its own preconditions
and remains available from the CLI.

Output for the Bash menu is tab-separated lines; the menu never parses JSON
itself.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from typing import Any

from .catalog import age_text, archive_facts, archive_text, files_text
from .node_names import NodeNames
from scripts.terminal_format import TerminalWriter, terminal_width

MAIN = ("check", "acquire", "restore", "prepare", "start", "stop", "status")
STORAGE = ("move", "archive", "verify", "pin", "unpin", "purge", "remove")
LABELS = {
    "check": "Check now", "acquire": "Download", "restore": "Restore", "prepare": "Prepare",
    "start": "Start", "stop": "Stop", "status": "Live status",
    "move": "Move home", "archive": "Create archive", "verify": "Verify archive",
    "pin": "Pin prepared copies", "unpin": "Unpin prepared copies",
    "purge": "Purge prepared copies", "remove": "Remove home",
}
# Operations that change model files or records; a success makes the saved
# check out of date until the next Check now.
MUTATIONS = frozenset({"acquire", "restore", "prepare", "move", "archive", "pin", "unpin", "purge", "remove"})
STALE_SECONDS = 24 * 3600
ARCHIVE_LOCATION = {"configured", "disabled", "not-configured"}
# Why Start is left out when the catalog says this Stack cannot start the spec,
# by its start_unsupported_reason (a start blocker code).
START_UNSUPPORTED = {"guard_unsupported": "ordinary start cannot enforce the spec's serving guard",
                     "historical_spec": "historical schema-1 specs cannot be started"}


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
    offered = [action for action in MAIN + STORAGE if action not in hidden]
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
               names: NodeNames | None = None, *, now=None) -> tuple[str, str] | None:
    """One suggested next step from saved state; the first matching rule wins.

    ``after`` is the last relevant outcome in this menu session. A recorded
    check clears an earlier mutation even when it found blockers; a check
    without a recorded result must not reuse old readiness. A mutation makes
    the saved check out of date, and a started service is live. For a
    spec this Stack cannot start, nothing after the Check, Download and
    Restore rules is suggested: Prepare and Start would only lead to a start.
    """
    names = names or NodeNames()
    age = row.get("observation_age_seconds")
    if after == "check-failed":
        return "check", "the last check did not record an observation"
    if after == "start":
        return "status", "started from this menu; live status observes the service"
    if after in MUTATIONS:
        return "check", f"{LABELS[after].lower()} ran after the last check"
    if row.get("checked_at") is None:
        return "check", "no saved check"
    if age is None or age > STALE_SECONDS:
        return "check", "last check " + age_text(age)
    local = row.get("local_state", "unknown")
    blocker = "saved blocker: " + names.prefixed(str(row["blockers"][0])) if row.get("blockers") else None
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


def short_state(row: dict) -> str:
    """Saved state for a recipe label; review status only when the spec sets one."""
    parts = [LABEL_FILES[row["local_state"]] if row.get("checked_at") else "never checked"]
    if start_unsupported(row):
        parts.append("start unsupported")
    status = (row.get("review") or {}).get("status")
    if status:
        parts.append(str(status))
    return " · ".join(parts)


def menu_label(row: dict, width: int) -> str:
    """One recipe list entry that fits the menu: model [spec] state.

    A long model ID is shortened first, to no fewer than 8 characters; only
    then is the end of the state cut.
    """
    budget = max(32, min(100, width)) - 6
    suffix = f" [{row['spec_id'][:8]}] {short_state(row)}"
    model = clean(row["model_id"])
    if len(model) + len(suffix) > budget and len(model) > 11:
        model = model[:max(8, budget - len(suffix) - 3)] + "..."
    text = model + suffix
    return text if len(text) <= budget else text[:budget - 3] + "..."


def header(row: dict, hidden: dict[str, str], suggested: tuple[str, str] | None, width: int, *, now=None) -> list[str]:
    buffer = io.StringIO()
    out = TerminalWriter(width=width, stream=buffer)
    out.emit(f"{row['model_id']} [{row['spec_id'][:8]}]")
    out.emit(f"Files: {files_text(row)} · Archive: {archive_text(row, now)}")
    if suggested:
        out.emit(f"Suggested: {LABELS[suggested[0]]} — {suggested[1]}")
    by_reason: dict[str, list[str]] = {}
    for action in MAIN + STORAGE:
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
               width: int | None = None, names: NodeNames | None = None, now=None) -> list[str]:
    offered, hidden, eligible = operations(row, archive_location)
    suggested = suggestion(row, offered, archive_location, after, names, now=now)
    frame_width = max(32, (width or terminal_width()) - 4)
    lines = [f"recipe\t{row['geometry']['nodes']}\t{clean(row['model_id'])}"]
    lines += ["header\t" + clean(line) for line in header(row, hidden, suggested, frame_width, now=now)]
    for action in offered:
        group = "main" if action in MAIN else "storage"
        label = LABELS[action] + (" (suggested)" if suggested and suggested[0] == action else "")
        lines.append(f"option\t{group}\t{action}\t{label}")
    if suggested:
        lines.append(f"suggest\t{suggested[0]}")
    for action, snapshots in eligible.items():
        for name in snapshots:
            lines.append(f"snapshot\t{action}\t{clean(name)}")
    return lines


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
        return f"Start {model} on {where}? Start rechecks prerequisites and never replaces a running service."
    if action == "stop":
        where = f" on {place}" if place else ""
        return f"Stop {model}{where}? Model files and pins are kept."
    raise ValueError(f"no confirmation for {action}")


def read_row(stream, spec_id: str) -> dict:
    document = json.load(stream)
    rows = [row for row in document.get("entries") or [] if row.get("spec_id") == spec_id]
    if len(rows) != 1:
        raise ValueError("the selected recipe is not in the catalog")
    return rows[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("labels", help="spec ID and menu label per line; catalog JSON on stdin")
    view = sub.add_parser("view", help="menu lines for one recipe; catalog JSON on stdin")
    view.add_argument("--spec-id", required=True)
    view.add_argument("--archive-location", required=True, choices=sorted(ARCHIVE_LOCATION))
    view.add_argument("--after", choices=sorted(MUTATIONS | {"start", "stop", "check", "check-failed"}))
    confirm = sub.add_parser("confirm", help="confirmation question; catalog JSON on stdin")
    confirm.add_argument("--spec-id", required=True)
    confirm.add_argument("--action", required=True, choices=sorted(MUTATIONS | {"start", "stop"}))
    confirm.add_argument("--plan-file")
    confirm.add_argument("--snapshot")
    confirm.add_argument("--node")
    args = parser.parse_args(argv)
    try:
        if args.command == "labels":
            width = terminal_width()
            for entry in json.load(sys.stdin).get("entries") or []:
                print(f"{entry['spec_id']}\t{menu_label(entry, width)}")
            return 0
        row = read_row(sys.stdin, args.spec_id)
        names = NodeNames.saved()
        if args.command == "view":
            print("\n".join(view_lines(row, args.archive_location, after=args.after, names=names)))
            return 0
        plan = None
        if args.plan_file:
            with open(args.plan_file, encoding="utf-8") as handle:
                plan = json.load(handle)
            if not isinstance(plan, dict):
                raise ValueError("plan must be a JSON object")
            if plan_blocked(plan):
                return 3
        print(clean(question(args.action, row, plan=plan, snapshot=args.snapshot, node=args.node, names=names)))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"error: catalog menu: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
