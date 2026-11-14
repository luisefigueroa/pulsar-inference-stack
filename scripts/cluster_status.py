#!/usr/bin/env python3
"""The home status and the Cluster area's screens, from saved records only.

    python3 -m scripts.cluster_status home [--now TIME] [--width N] < CATALOG_JSON
    python3 -m scripts.cluster_status frame [--now TIME] [--width N]
    python3 -m scripts.cluster_status review [--now TIME] [--width N] [--choices] [--serve]
    python3 -m scripts.cluster_status api-key [--width N]
    python3 -m scripts.cluster_status saved-id|node-count

Every line comes from a saved record and says how old it is: the saved
membership, the changes noticed with the last cluster check and a kept answer
beside them (scripts/saved_observations.py), the saved service inventory and
storage budget, and the archive setting. Nothing here contacts a node, and a
record that is missing or unreadable is said to be so, never read as fine.

`home` prints `key<TAB>KEY` on its first line, then the status block: at most
four labelled lines (Cluster, Serving, Disk, Attention) with their wrapped
continuations, so the block and the six-choice home menu fit 80x24 together.
KEY changes only with a new record, not with the ages. `review --choices`
prints the Review screen's choices as `ACTION<TAB>LABEL` lines.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_library.catalog import age_seconds, age_text  # noqa: E402
from model_library.catalog_menu import LIVE_FRESH_SECONDS, SERVICE_STATES, live_inventory  # noqa: E402
from model_library.render import can_still_copy, short_size  # noqa: E402
from scripts import saved_observations as saved  # noqa: E402
from scripts.inventory_menu import project_others  # noqa: E402
from scripts.setup_status import _archive_status, topology_file  # noqa: E402
from scripts.terminal_format import TerminalWriter, cut_middle  # noqa: E402
from scripts.topology_manifest import extract_topology, topology_has_ssh_trust  # noqa: E402

LABEL_WIDTH = 11
# Attention shows this many items, then a count of the rest.
ATTENTION_ROWS = 2
# Serving shows this many services, then a count of the rest.
SERVING_ROWS = 2
# A no-break space keeps words together; it prints as a space.
KEEP = " "

# The choices these screens name, as the menus label them.
CLUSTER = "**Cluster…**"
CHECK = "**Check the cluster now**"
REVIEW = "**Review what changed…**"
SETTINGS = "**Settings…**"
RUNNING = "**Running services…**"
MODELS = "**Models and disk space…**"
FIND = "**Look for model files Pulsar has no record of…**"

# The status words these screens print. Each has a row under Catalog labels
# in docs/OPERATIONS.md; tests/test_help.py checks it.
STATUS_WORDS = {
    "not-checked-yet": "not checked yet",
    "since-saved": "not checked since membership was saved",
    "all-answered": "all answered",
    "answered": "answered",
    "did-not-answer": "did not answer",
    "not-observed": "not observed",
    "only-down": "kept as only down",
    "not-a-member": "not a member",
    "cannot-be-member": "answered but cannot be a member",
    "changed": "changed",
    "did-not-finish": "the last discovery did not finish",
    "not-trusted": "SSH on this machine does not trust it yet",
    "none-noticed": "none noticed",
    "links-verified": "links verified",
    "links-failed": "links failed",
    "identities-verified": "SSH identities verified",
    "identities-failed": "SSH identities did not match",
    "identities-recorded": "SSH identities recorded",
    "identities-not-recorded": "SSH identities not recorded",
    "room-for": "room for",
    "past": "past its reserve or copy budget by",
    "headroom": "headroom not observed",
    "not-observed-yet": "not observed yet",
    "none-running": "none running",
    "unknown": "unknown",
    "remain": "remain",
    "outside": "outside the catalog",
    "unreadable": "unreadable",
    "archive-unreadable": "the archive location setting could not be read",
    "more": "more",
    "no-files-recorded": "no model has files recorded yet",
}


def _kept(*words: object) -> str:
    return KEEP.join(str(word) for word in words)


def _plain(text: str) -> str:
    return text.replace(KEEP, " ")


def _list(names: list[str]) -> str:
    names = [str(name) for name in names]
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _ago(stamp: Any, now: datetime) -> str:
    return age_text(age_seconds(stamp, now))


def _stamp(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Saved records
# ---------------------------------------------------------------------------

def membership(repo: Path) -> tuple[str, dict | None]:
    """("missing" | "invalid" | "saved", the saved topology)."""
    path = topology_file(repo)
    if not path.exists():
        return "missing", None
    try:
        topology = extract_topology(json.loads(path.read_text(encoding="utf-8")))
        nodes = topology["nodes"]
        if not isinstance(topology.get("topology_id"), str) or not isinstance(nodes, list) or not nodes:
            raise ValueError("incomplete membership")
        for node in nodes:
            if not isinstance(node.get("hostname"), str) or not isinstance(node.get("rank"), int):
                raise ValueError("a node has no hostname or position")
        return "saved", topology
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        return "invalid", None


class Records:
    """Every saved record the screens read, read once."""

    def __init__(self, repo: Path, now: datetime | None = None, environ: dict | None = None):
        self.repo = Path(repo)
        self.now = now or datetime.now(timezone.utc)
        self.state, self.topology = membership(self.repo)
        self.nodes = sorted((self.topology or {}).get("nodes") or [], key=lambda node: node["rank"])
        self.inventory, self.inventory_unreadable = saved._read(saved.load_inventory, self.repo, saved.INVENTORY_NAME)
        self.budget, self.budget_unreadable = saved._read(saved.load_budget, self.repo, saved.BUDGET_NAME)
        record, self.changes_unreadable = saved._read(saved.load_changes, self.repo, saved.CHANGES_NAME)
        self.all_changes = record
        # A record of another membership describes an earlier one: it is not current.
        self.stale = bool(record and self.topology and record["topology_id"] != self.topology["topology_id"])
        self.changes = record if record and not self.stale and self.topology else None
        self.archives = _archive_status(self.repo, environ)

    @property
    def hostnames(self) -> dict[str, str]:
        return {node["node_id"]: node["hostname"] for node in self.nodes if node.get("node_id")}

    def key(self) -> str:
        """Changes with a new record or the inventory turning "last seen", never with an age."""
        inventory = "none" if self.inventory is None else (
            f"{self.inventory['generated_at']}:"
            f"{'fresh' if saved._fresh(self.inventory, self.now) else 'last-seen'}")
        parts = [self.state, (self.topology or {}).get("topology_id", ""), inventory,
                 "none" if self.budget is None else self.budget["saved_at"],
                 json.dumps(self.all_changes, sort_keys=True), self.archives["status"],
                 str(self.inventory_unreadable), str(self.budget_unreadable), str(self.changes_unreadable)]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# What the last check and discovery said about the nodes
# ---------------------------------------------------------------------------

def _check(records: Records) -> dict | None:
    """The last cluster check of this membership, unless a newer discovery replaced what it says."""
    changes = records.changes
    if not changes or "check" not in changes:
        return None
    return changes["check"]


def answered_text(records: Records) -> str:
    """How many nodes answered the last check or discovery, and when."""
    if records.changes_unreadable:
        return f"saved check {STATUS_WORDS['unreadable']}; {CHECK} replaces it"
    if records.stale:
        return f"{STATUS_WORDS['since-saved']}; {CHECK} looks"
    changes = records.changes
    if not changes:
        return f"{STATUS_WORDS['not-checked-yet']}; {CHECK} looks"
    total = len(records.nodes)
    check = _check(records)
    checked = _stamp((check or {}).get("checked_at"))
    observed = _stamp(changes["observed_at"])
    if check and checked and (observed is None or checked >= observed):
        answered = sum(1 for node in check["nodes"] if node["answered"] is True)
        when = _ago(check["checked_at"], records.now)
        # Unknown is not absent: a node the check could not observe did not
        # fail to answer; it is counted apart.
        unknown = sum(1 for node in check["nodes"] if node["answered"] is None)
        if unknown:
            if unknown >= len(check["nodes"]):
                return f"{STATUS_WORDS['not-observed']} at the last check {when}; {CHECK} looks again"
            return (f"{answered} of {total} {STATUS_WORDS['answered']}, {unknown} {STATUS_WORDS['not-observed']} "
                    f"{when}")
    else:
        if changes["result"] == "not-checked":
            return f"{STATUS_WORDS['did-not-finish']} ({_ago(changes['observed_at'], records.now)})"
        unanswered = [item for item in changes["missing"] if not item.get("answered")]
        answered = total - len(unanswered)
        when = _ago(changes["observed_at"], records.now)
    if answered >= total:
        return f"{STATUS_WORDS['all-answered']} {when}"
    return f"{answered} of {total} {STATUS_WORDS['answered']} {when}"


def change_items(records: Records, *, review: bool = False) -> list[str]:
    """The changes noticed against this membership, one sentence each, with their age."""
    changes = records.changes
    if not changes or changes["result"] != "changed":
        if changes and changes["result"] == "not-checked":
            issues = "; ".join(changes["issues"]) or "no reason recorded"
            return [f"{STATUS_WORDS['did-not-finish']} ({_ago(changes['observed_at'], records.now)}): {issues}"
                    if review else
                    f"{STATUS_WORDS['did-not-finish']} ({_ago(changes['observed_at'], records.now)})"]
        return []
    when = f"({_ago(changes['observed_at'], records.now)})"
    kept = set((changes.get("kept") or {}).get("missing") or [])
    items = []
    for item in changes["missing"]:
        if item.get("answered"):
            reasons = "; ".join(item.get("reasons") or []) or "not qualified"
            items.append(f"{item['hostname']} {STATUS_WORDS['cannot-be-member']}: {reasons} {when}")
        elif item["node_id"] in kept:
            items.append(f"{item['hostname']} {STATUS_WORDS['did-not-answer']} {when}; {STATUS_WORDS['only-down']}")
        else:
            items.append(f"{item['hostname']} {STATUS_WORDS['did-not-answer']} {when}")
    for item in changes["found"]:
        text = f"found {item['hostname']}, {STATUS_WORDS['not-a-member']} {when}"
        if item.get("possible_reinstall"):
            text += f"; it may be {item['hostname']} reinstalled"
        items.append(text)
    for item in changes["changed"]:
        what = {"control": "address", "links": "links", "position": "position"}
        fields = [what[key] for key in ("control", "links", "position") if key in item.get("changes", {})]
        items.append(f"{item['hostname']} {_list(fields) or 'details'} {STATUS_WORDS['changed']} {when}")
    return items


def check_items(records: Records) -> list[str]:
    """Blocking issues the last check found on each node, and nodes it could not reach."""
    check = _check(records)
    if not check:
        return []
    when = f"({_ago(check['checked_at'], records.now)})"
    missing = {item["hostname"] for item in (records.changes or {}).get("missing") or []}
    items = []
    for node in check["nodes"]:
        if node["answered"] is False and node["hostname"] not in missing:
            items.append(f"{node['hostname']} {STATUS_WORDS['did-not-answer']} the last check {when}")
        elif node["failures"]:
            more = len(node["failures"]) - 1
            text = f"{node['hostname']}: {node['failures'][0]}"
            if more:
                text += f" and {more} {STATUS_WORDS['more']}"
            items.append(f"{text} {when}")
    if check.get("links") == "failed":
        items.append(f"{STATUS_WORDS['links-failed']} {when}")
    if check.get("identities") == "failed":
        items.append(f"{STATUS_WORDS['identities-failed']} {when}")
    return items


# ---------------------------------------------------------------------------
# Services and disk, from the saved inventory and budget
# ---------------------------------------------------------------------------

# Service states that hold a running container; the others only leave containers behind.
ACTIVE_STATES = ("running", "partial", "degraded")


def service_name(service: dict, room: int, *, outside: bool = False) -> str:
    """One Pulsar service's name, the same on every screen: "MODEL running on
    NODES · port P" while a container runs, else "exited containers of MODEL
    remain on NODES", and " · outside the catalog" for a service whose spec
    this checkout does not publish. MODEL is cut to ROOM, keeping its end.
    Words are joined by no-break spaces where they must stay together."""
    state = SERVICE_STATES.get(service["state"], service["state"])
    where = ", ".join(service["hosts"]) or "nodes not identified"
    model = cut_middle(service["model_id"], room)
    if service["state"] in ACTIVE_STATES:
        text = f"{model} {state} on {where}"
        if service.get("port"):
            text += f" ·{KEEP}{_kept('port', service['port'])}"
    else:
        text = f"{state} of {model} {STATUS_WORDS['remain']} on {where}"
    if outside:
        text += f" ·{KEEP}{_kept(*STATUS_WORDS['outside'].split())}"
    return text


def service_texts(records: Records, entries: list[dict], room: int,
                  states: tuple[str, ...] | None = None) -> tuple[list[str], str | None]:
    """One text per Pulsar service (only those in STATES, when given), and what is unknown about the others.

    Services with a running container come first, as "MODEL running on
    NODES"; a service with only exited or stopped containers left reads
    "exited containers of MODEL remain on NODES". Every caller ends its line
    with the inventory's age (`seen …`), so no text says "last seen" again."""
    inventory = records.inventory
    live = live_inventory(inventory, entries, now=records.now)
    catalog = [{"spec_id": row["spec_id"], "model_id": row["model_id"], "nodes": row["geometry"]["nodes"]}
               for row in entries]
    active, remains = [], []
    for outside, services in ((False, live["services"]), (True, project_others(inventory, catalog))):
        for service in services.values():
            if states is not None and service["state"] not in states:
                continue
            text = service_name(service, room, outside=outside and bool(entries))
            (active if service["state"] in ACTIVE_STATES else remains).append(text)
    texts = active + remains
    # Only a complete inventory can say that nothing runs: a node it did not
    # observe may run a service.
    if remains and not active and not live["incomplete"]:
        texts[0] = f"{STATUS_WORDS['none-running']} · {texts[0]}"
    unknown = None
    if live["incomplete"]:
        unobserved = _list(live["unobserved"]) or "a node"
        unknown = f"{unobserved} not observed"
    return texts, unknown


def serving_rows(records: Records, entries: list[dict], room: int) -> list[str]:
    if records.inventory_unreadable:
        return [f"saved inventory {STATUS_WORDS['unreadable']}; {RUNNING} replaces it"]
    if records.inventory is None:
        return [f"{STATUS_WORDS['not-observed-yet']}; {RUNNING} looks"]
    try:
        texts, unknown = service_texts(records, entries, room)
    except ValueError:
        return [f"saved inventory {STATUS_WORDS['unreadable']}; {RUNNING} replaces it"]
    seen = f"seen {_ago(records.inventory['generated_at'], records.now)}"
    if not texts:
        if unknown:
            return [f"{STATUS_WORDS['unknown']}; {unknown} · {seen}"]
        return [f"{STATUS_WORDS['none-running']} · {seen}"]
    rows = texts[:SERVING_ROWS]
    if len(texts) > SERVING_ROWS:
        rows.append(f"and {len(texts) - SERVING_ROWS} {STATUS_WORDS['more']}")
    tail = f" ·{KEEP}{seen}"
    if unknown:
        tail += f"; others {STATUS_WORDS['unknown']}, {unknown}"
    # The saved inventory holds no API URL; the services page shows how to connect.
    rows[-1] += f"{tail}; {RUNNING} has the details"
    return rows


def disk_text(records: Records) -> str:
    if records.budget_unreadable:
        return f"saved budget {STATUS_WORDS['unreadable']}; {MODELS} replaces it"
    if records.budget is None:
        return f"{STATUS_WORDS['not-observed-yet']}; {MODELS} reads it"
    names = records.hostnames
    order = {node["node_id"]: node["rank"] for node in records.nodes if node.get("node_id")}
    nodes = sorted(records.budget["nodes"], key=lambda node: (order.get(node["node_id"], 1_000_000), node["node_id"]))
    groups: dict[str, list[str]] = {}
    problems = []
    for node in nodes:
        host = names.get(node["node_id"]) or node["node_id"][:12]
        left = can_still_copy(node)
        if left is None:
            problems.append(f"{host} {STATUS_WORDS['headroom']}")
        elif left < 0:
            problems.append(f"{host} {STATUS_WORDS['past']} {_kept(*short_size(-left).split())}")
        else:
            groups.setdefault(_kept(*short_size(left).split()), []).append(host)
    parts = []
    for size, hosts in groups.items():
        parts.append(f"{size} on {', '.join(hosts)}")
    text = f"{STATUS_WORDS['room-for']} {' · '.join(parts)}" if parts else ""
    text = " · ".join(part for part in (text, *problems) if part)
    return f"{text} · checked {_ago(records.budget['saved_at'], records.now)}"


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

def files_suggestion(entries: list[dict]) -> str | None:
    """Look for model files…, suggested when no catalog recipe has its files recorded on the cluster
    (no home in the catalog's saved records). Membership is saved wherever home shows this. An
    empty or unreadable catalog says nothing: unknown is not absent."""
    rows = [row for row in entries or [] if isinstance(row, dict) and not row.get("historical")]
    if not rows:
        return None

    def recorded(row: dict) -> bool:
        members = row["snapshots"] if isinstance(row.get("snapshots"), dict) else {None: row}
        return any(isinstance(member, dict) and member.get("home") for member in members.values())
    if any(recorded(row) for row in rows):
        return None
    return f"{STATUS_WORDS['no-files-recorded']}; {MODELS} → {FIND}"


def attention_items(records: Records, entries: list[dict] = ()) -> list[str]:
    """What needs a look, in order: the archive setting, changes noticed, the last check's issues, then
    the suggestion to look for model files when no model has its files recorded.

    The last cluster item shown names where to look; the archive item names its own fix. The
    suggestion keeps its own row, so the cluster items give it one when they overflow."""
    archive = []
    if records.archives["status"] == "unreadable":
        archive.append(f"{STATUS_WORDS['archive-unreadable']}; {CLUSTER} → {SETTINGS} says how to fix it")
    changes, issues = change_items(records), check_items(records)
    items = archive + changes + issues
    suggestion = files_suggestion(list(entries))
    if len(items) > ATTENTION_ROWS:
        # At most three rows: two items, or one and the count when the suggestion takes the third.
        rows = ATTENTION_ROWS - (1 if suggestion else 0)
        items = items[:rows] + [f"and {len(items) - rows} {STATUS_WORDS['more']}; {CLUSTER} shows them"]
    else:
        if changes:
            items[len(archive) + len(changes) - 1] += f"; {CLUSTER} → {REVIEW}"
        if issues:
            items[-1] += f"; {CLUSTER} has the details"
    return items + ([suggestion] if suggestion else [])


def home_block(records: Records, entries: list[dict], *, width: int | None = None) -> str:
    """The home status: Cluster, Serving, Disk and Attention, from saved records."""
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)

    def field(label: str, value: str, role: str | None = None) -> None:
        out.field(label, value, label_width=LABEL_WIDTH, label_role=role, markup=True)

    count = len(records.nodes)
    noun = "node" if count == 1 else "nodes"
    field("Cluster", f"{count} {noun} · {answered_text(records).replace(CHECK, CLUSTER)}")
    for index, row in enumerate(serving_rows(records, entries, max(16, out.width - LABEL_WIDTH - 24))):
        field("Serving" if index == 0 else "", row)
    field("Disk", disk_text(records))
    for index, item in enumerate(attention_items(records, entries)):
        field("Attention" if index == 0 else "", item, "warn" if index == 0 else None)
    return _plain(output.getvalue())


# ---------------------------------------------------------------------------
# The Cluster area
# ---------------------------------------------------------------------------

def identities_text(records: Records) -> str:
    if not topology_has_ssh_trust(records.topology or {}):
        return STATUS_WORDS["identities-not-recorded"]
    check = _check(records)
    if check and check.get("identities") == "verified":
        return STATUS_WORDS["identities-verified"]
    if check and check.get("identities") == "failed":
        return STATUS_WORDS["identities-failed"]
    return STATUS_WORDS["identities-recorded"]


def frame(records: Records, *, width: int | None = None) -> str:
    """The Cluster menu's frame: the nodes, when they last answered, and the changes noticed."""
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)

    def field(label: str, value: str, role: str | None = None) -> None:
        out.field(label, value, label_width=LABEL_WIDTH, label_role=role, markup=True)

    count = len(records.nodes)
    field("Nodes", f"{count} · {', '.join(node['hostname'] for node in records.nodes)}")
    checked = answered_text(records)
    check = _check(records)
    extras = []
    if check and check.get("links") in ("verified", "failed"):
        extras.append(STATUS_WORDS[f"links-{check['links']}"])
    extras.append(identities_text(records))
    field("Checked", " · ".join([checked, *extras]))
    changes = change_items(records)
    if records.changes and not changes:
        field("Changes", f"{STATUS_WORDS['none-noticed']} ({_ago(records.changes['observed_at'], records.now)})")
    for index, item in enumerate(changes):
        field("Changes" if index == 0 else "", item, "warn" if index == 0 else None)
    for index, item in enumerate(check_items(records)):
        field("Issues" if index == 0 else "", item, "fail" if index == 0 else None)
    return _plain(output.getvalue())


def has_review(records: Records) -> bool:
    """Whether Review what changed… has something to show: a change, or a discovery that did not finish."""
    return bool(records.changes and records.changes["result"] in ("changed", "not-checked"))


def running_services(records: Records) -> list[str]:
    """Pulsar services with running containers in the saved inventory, as "model running on nodes".

    They block a save, as the rewrite's idle rule does (`docker ps`, running
    containers only); exited or stopped containers do not."""
    if records.inventory is None:
        return []
    try:
        texts, _ = service_texts(records, [], 40, states=("running", "partial", "degraded"))
    except ValueError:
        return []
    return texts


def projected_positions(records: Records) -> list[tuple[str, int | None, int | None]]:
    """Each node's position before and after a save: members that answered
    keep their order, and machines found join after them by hostname, as
    discovery orders them (scripts/topology_manifest.py assemble)."""
    changes = records.changes or {}
    gone = {item["node_id"] for item in changes.get("missing") or [] if not item.get("answered")}
    kept = [node for node in records.nodes if node.get("node_id") not in gone]
    found = sorted(changes.get("found") or [], key=lambda item: (item["hostname"].lower(), item["node_id"]))
    rows: list[tuple[str, int | None, int | None]] = [
        (node["hostname"], node["rank"], position) for position, node in enumerate(kept)]
    rows += [(item["hostname"], None, len(kept) + index) for index, item in enumerate(found)]
    rows += [(node["hostname"], node["rank"], None) for node in records.nodes if node.get("node_id") in gone]
    return rows


def running_models(records: Records) -> list[str]:
    """The model names of the Pulsar services with a running container in the saved inventory."""
    if records.inventory is None:
        return []
    from scripts.inventory_menu import project
    try:
        services = {**project(records.inventory, []), **project_others(records.inventory, [])}
    except ValueError:
        return []
    return [service["model_id"] for service in services.values() if service["state"] in ACTIVE_STATES]


def review(records: Records, *, width: int | None = None, serve: bool = False,
           ready: bool = False) -> tuple[str, list[tuple[str, str]]]:
    """The Review screen: saved against observed, then one question's choices.

    SERVE is the screen Serve a model… shows before its list when the change
    check found a change: it adds Serve on the cluster as saved and, when a
    running service refuses the save, Stop it, save the change, then continue.
    READY is the same screen for Get a model ready ahead of time…, which starts
    nothing: it offers Get it ready on the cluster as saved, and never the Stop,
    which would leave the cluster idle with nothing to start after it."""
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)

    def field(label: str, value: str, role: str | None = None) -> None:
        out.field(label, value, label_width=14, label_role=role, markup=True)

    changes = records.changes or {}
    out.emit("Review what changed", role="heading")
    field("Saved", ", ".join(f"{node['hostname']}" for node in records.nodes))
    if not changes:
        field("Observed", f"{STATUS_WORDS['since-saved'] if records.stale else STATUS_WORDS['not-checked-yet']}; "
                          f"{CHECK} looks")
        return _plain(output.getvalue()), [("check", "Check the cluster now"), ("back", "Back")]
    field("Observed", f"by discovery {_ago(changes['observed_at'], records.now)}")
    if changes["result"] == "not-checked":
        field("Not checked", f"{STATUS_WORDS['did-not-finish']}: {'; '.join(changes['issues']) or 'no reason'}. "
                             f"Nothing can be saved from it; {CHECK} tries again.", "warn")
        return _plain(output.getvalue()), [("check", "Check the cluster now"), ("back", "Back")]
    out.blank()
    unanswered = [item for item in changes["missing"] if not item.get("answered")]
    rejected = [item for item in changes["missing"] if item.get("answered")]
    kept = set((changes.get("kept") or {}).get("missing") or [])
    for item in unanswered:
        text = (f"{item['hostname']} (position {item['position']}) {STATUS_WORDS['did-not-answer']}. "
                "It may be down or gone; Pulsar cannot tell which.")
        if item["node_id"] in kept:
            text += (f" You kept it as only down {_ago(changes['kept']['kept_at'], records.now)}; it stays under "
                     "Attention until it answers.")
        field("Not answering", text, "warn")
    for item in rejected:
        reasons = "; ".join(item.get("reasons") or []) or "not qualified"
        field("Not usable", f"{item['hostname']} (position {item['position']}) answered but cannot be a member: "
                            f"{reasons}. Fix it on that machine first; Pulsar never leaves out a node that "
                            "answered.", "fail")
    for item in changes["found"]:
        text = f"{item['hostname']} ({item['ssh_host']}) answered and qualifies; it is not a member."
        if item.get("possible_reinstall"):
            text += (f" It has the hostname of a member that did not answer, so it may be {item['hostname']} "
                     "reinstalled. Pulsar knows a node by its OS machine ID, so a reinstalled machine is a new "
                     "node and the old one is left out.")
        field("New machine", text)
    for item in changes["changed"]:
        parts = []
        details = item.get("changes") or {}
        if "control" in details:
            old, new = details["control"].get("old") or {}, details["control"].get("new") or {}
            parts.append(f"address {old.get('ip', '?')} → {new.get('ip', '?')}")
        if "links" in details:
            def links(values):
                return ", ".join(f"{link['netdev']} {' '.join(link.get('cidrs') or [])}".strip()
                                 for link in values or []) or "none"
            parts.append(f"links {links(details['links'].get('old'))} → {links(details['links'].get('new'))}")
        if "position" in details:
            parts.append(f"position {details['position'].get('old')} → {details['position'].get('new')}")
        field("Changed", f"{item['hostname']}: {'; '.join(parts) or 'details changed'}")
    for item in changes["untrusted"]:
        field("Not trusted", f"{item['address']} answers, but {STATUS_WORDS['not-trusted']}. Check its host key "
                             f"with `ssh -o StrictHostKeyChecking=ask {item['address']}`, then {CHECK}. "
                             "It is not added until SSH trusts it.")
    choices: list[tuple[str, str]] = []
    running = running_services(records)
    can_save = (changes["found"] or changes["changed"] or unanswered) and not rejected
    positions = projected_positions(records)
    if can_save and any(old != new for _, old, new in positions):
        out.blank()
        moves = " · ".join(f"{name} {'new' if old is None else old} → {'left out' if new is None else new}"
                           for name, old, new in positions)
        field("Positions", f"{moves}. Multi-node recipes use the first nodes in this order.")
    if can_save:
        out.blank()
        field("Saving", "Pulsar asks once more before it saves, and saves only while no Pulsar service runs. "
                        "Afterwards it records the nodes' SSH identities, with its own question and the "
                        "fingerprints. Models prepared before keep their files: Prepare takes over each copy "
                        "that still verifies, with nothing copied again.")
        if unanswered:
            field("", "Saving without a node keeps the files recorded on it; the save lists them first.")
    names = _list([item["hostname"] for item in unanswered])
    if unanswered and not rejected and not all(item["node_id"] in kept for item in unanswered):
        # Keeping saves nothing, so a running service does not stop it.
        choices.append(("keep", f"Keep {names}; {'it is' if len(unanswered) == 1 else 'they are'} only down"))
    if running and can_save:
        out.blank()
        field("Refused", f"Saving is refused while a Pulsar service runs: {_list(running)} "
                         f"(seen {_ago(records.inventory['generated_at'], records.now)}). Stop it under {RUNNING} "
                         f"(same as: `./pulsar stop SPEC`), then {CHECK}.", "fail")
        choices.append(("check", "Check the cluster now"))
    elif can_save and unanswered:
        choices.append(("drop", f"Save the cluster without {names}"))
    elif can_save:
        choices.append(("save", "Save the cluster as it is now"))
    else:
        choices.append(("check", "Check the cluster now"))
    commands = {"save": "`./pulsar topology configure`, then `./pulsar ssh-trust enroll`",
                "drop": "`./pulsar topology configure --remove-missing`, then `./pulsar ssh-trust enroll`"}
    same = [commands[action] for action, _ in choices if action in commands]
    if same:
        out.blank()
        field("Same as", "; ".join(same))
    if serve:
        # Serving on the cluster as saved needs no save; a plan whose nodes did
        # not answer stops before it changes anything.
        choices.append(("serve", "Get it ready on the cluster as saved" if ready else "Serve on the cluster as saved"))
        # The Refused line above names what runs; the saved inventory has no catalog to name it here.
        models = running_models(records)
        if running and can_save and models and not ready:
            what = "the running service" if len(models) == 1 else "the running services"
            choices.append(("stop-save", f"Stop {what}, save the change, then continue"))
    choices.append(("back", "Back"))
    return _plain(output.getvalue()), choices


# ---------------------------------------------------------------------------
# Settings: the API key, without the secret
# ---------------------------------------------------------------------------

_KEY_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(VLLM_API_KEY|API_KEY)\s*=(.*)$")
# An assignment of either name elsewhere in a line, such as after `&&`: start may or may not run it.
_KEY_ELSEWHERE = re.compile(r"(?<![\w$])(VLLM_API_KEY|API_KEY)=")


def _before_comment(line: str) -> str:
    """LINE up to its shell comment: a `#` that starts a word outside quotes."""
    quote, escaped = None, False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char in "'\"" and quote in (None, char):
            quote = None if quote else char
        elif char == "#" and quote is None and (index == 0 or line[index - 1] in " \t;&|()"):
            return line[:index]
    return line


def api_key_source(repo: Path, environ: dict | None = None) -> tuple[str | None, str | None]:
    """Where the API key start uses is configured: (variable, "process" | "dotenv" | "expression"),
    or (None, None).

    As `pulsar_api_key` in scripts/lib.sh reads it: `VLLM_API_KEY`, else
    `API_KEY`, after this checkout's .env is sourced over the environment, so
    an assignment in .env wins, even an empty one. The menu reads .env as data
    and never runs it: an assignment that is not a literal value, such as
    `"${OTHER:-}"` or one after `&&`, gives (variable, "expression"), because
    only start, which sources the file, knows its value. The value is never
    returned or printed. A .env that cannot be read gives ("unreadable", "dotenv")."""
    env = os.environ if environ is None else environ
    from model_library.configuration import _decode, _read
    from model_library.integrity import StorageError
    try:
        data, _ = _read(Path(repo))
        text = data.decode("utf-8")
    except (StorageError, OSError, UnicodeError):
        return "unreadable", "dotenv"
    assigned: dict[str, bool | None] = {}  # None: not a literal value
    for line in text.splitlines():
        match = _KEY_ASSIGNMENT.match(line)
        if match:
            try:
                assigned[match.group(1)] = bool(_decode(match.group(2)))
            except StorageError:
                assigned[match.group(1)] = None
        else:
            for name in _KEY_ELSEWHERE.findall(_before_comment(line)):
                assigned[name] = None
    for name in ("VLLM_API_KEY", "API_KEY"):
        if name in assigned:
            if assigned[name] is None:
                return name, "expression"
            if assigned[name]:
                return name, "dotenv"
        elif env.get(name):
            return name, "process"
    return None, None


def api_key_text(repo: Path, *, width: int | None = None, environ: dict | None = None) -> str:
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    name, source = api_key_source(repo, environ)
    where = {"process": "the environment that started Pulsar", "dotenv": "this checkout's .env file"}
    if name == "unreadable":
        value = ("not known: this checkout's .env file could not be read. Fix the file; the menu does not "
                 "read or take the key.")
    elif source == "expression":
        value = (f"not known: this checkout's .env file sets `{name}` with a shell expression. Start reads its "
                 "value when it sources the file; the menu does not run the file. The menu takes no secret.")
    elif name:
        value = (f"configured: `{name}` is set in {where[source]}. Clients send it as a bearer token. "
                 "The menu takes no secret: change it there, then start the service again.")
    else:
        value = ("not configured: the API accepts requests without a key. To require one, set `VLLM_API_KEY` "
                 "in this checkout's .env file or the environment that starts Pulsar, then start the service "
                 "again. The menu takes no secret.")
    out.field("API key", value, label_width=LABEL_WIDTH, markup=True)
    return output.getvalue()


# ---------------------------------------------------------------------------

def _instant(value: str) -> datetime:
    return saved._instant(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("home", "frame", "review", "api-key", "saved-id", "node-count"):
        command = sub.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=ROOT)
        command.add_argument("--width", type=int)
        command.add_argument("--now", type=_instant)
        if name == "review":
            command.add_argument("--choices", action="store_true")
            command.add_argument("--serve", action="store_true", help="the screen Serve a model… shows")
            command.add_argument("--ready", action="store_true",
                                 help="the screen Get a model ready ahead of time… shows: no Stop")
    args = parser.parse_args(argv)
    if args.command == "api-key":
        sys.stdout.write(api_key_text(args.repo_root, width=args.width))
        return 0
    if args.command in ("saved-id", "node-count"):
        # For the menus: whether a save changed membership, and how many nodes it has.
        state, topology = membership(args.repo_root)
        if state != "saved":
            return 1
        print(topology["topology_id"] if args.command == "saved-id" else len(topology["nodes"]))
        return 0
    records = Records(args.repo_root, args.now)
    if records.state != "saved":
        print("error: no saved cluster membership to describe", file=sys.stderr)
        return 1
    if args.command == "home":
        try:
            entries = json.load(sys.stdin).get("entries") or []
        except (ValueError, AttributeError):
            entries = []
        # The suggestion to look for model files comes from the catalog, not a saved record: it is part of the key.
        key = records.key() + ("-find" if files_suggestion(entries) else "")
        sys.stdout.write(f"key\t{key}\n{home_block(records, entries, width=args.width)}")
    elif args.command == "frame":
        # The first line tells the menu when to draw the frame again and
        # whether Review what changed… has something to show.
        sys.stdout.write(f"key\t{records.key()}\t{'review' if has_review(records) else '-'}\n"
                         f"{frame(records, width=args.width)}")
    else:
        text, choices = review(records, width=args.width, serve=args.serve or args.ready, ready=args.ready)
        if args.choices:
            sys.stdout.write("".join(f"{action}\t{label}\n" for action, label in choices))
        else:
            sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
