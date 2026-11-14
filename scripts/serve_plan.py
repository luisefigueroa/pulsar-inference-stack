#!/usr/bin/env python3
"""Serve a model…: the model list's readiness phrases and the plan it runs.

    python3 -m scripts.serve_plan entry [--discovered | --explain]
    python3 -m scripts.serve_plan running --inventory FILE < CATALOG_JSON
    python3 -m scripts.serve_plan list [--inventory FILE] [--leave-out SPEC]... < CATALOG_JSON
    python3 -m scripts.serve_plan nodes --spec-id S < CATALOG_JSON
    python3 -m scripts.serve_plan previews --spec-id S [--node ID] [--files FILE] [--source WHERE]
            [--files-node ID] [--not-found NAME] [--ready-only] [--image-source pull]
            [--stop SPEC | --restart] < CATALOG_JSON
    python3 -m scripts.serve_plan compose --spec-id S --out FILE [same options, --inventory FILE,
            --preview NAME=FILE..., --hf FILE] < CATALOG_JSON
    python3 -m scripts.serve_plan render|steps|choices|facts --plan FILE
    python3 -m scripts.serve_plan command --plan FILE (--step I [--preview] | --restart I [--shown]
            | --accept-memory-warning [--shown])
    python3 -m scripts.serve_plan check-step --plan FILE --step I --preview-file FILE [--preview-rc N]
    python3 -m scripts.serve_plan outcome --plan FILE --done N [--failed I --rc N] [--halted WHY]
            [--blockers FILE] [--ran-step I]... [--choices]

The plan is an ordered list of steps. Each has a sentence for the operator,
the `./pulsar` command it runs, what it changes, and whether it needs
authority of its own: stopping a service, pulling an image from a registry,
or anything that deletes. Those are always listed as their own steps; no step
passes `--replace` or `--pull-image`, and a pull is only ever a listed
`image stage --pull` step.

The first screen is a projection: saved records, a service inventory, and the
previews that can run now (`model restore|prepare --plan --json`, `image
stage --plan --json`, `start --dry-run --json`, and the `hf` prerequisites of
a node a plan downloads to). Prepare has no preview before a home exists, and
Start's dry run leaves later checks unrun after a blocking group, so those
steps are marked projected. When a step's turn comes the menu runs its own
preview again, and `check-step` says whether it still does what the approved
plan said: `same`, `skip` (nothing left to do), `differs` or `blocked`, each
with a reason. Nothing here contacts a node; the menu runs every command.

Step order is fixed: files (found, restored or downloaded, per required
snapshot), copies to the serving nodes, the serving image, then Stop of
whatever is in the way, then Start, so a failed download or copy never stops
what is serving. Output for the Bash menu is tab-separated lines.
"""
from __future__ import annotations

import argparse
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

from model_library.catalog import archive_facts  # noqa: E402
from model_library.catalog_menu import (ARCHIVE_LOCATION, SERVICE_STATES, CARD_NAME_COLUMN,  # noqa: E402
                                        CARD_SPEC_COLUMN, CARD_WRAP_COLUMN, _fit_name, _plan_actions,
                                        _plan_document, clean, distinguishing_facts, found_files, members,
                                        node_count, plan_blocked, plan_effect, plan_summary, recorded_node,
                                        services_in_the_way, short_model_names, start_unsupported)
from model_library.node_names import NodeNames  # noqa: E402
from model_library.render import short_size  # noqa: E402
from scripts.terminal_format import MAX_WIDTH, MIN_WIDTH, TerminalWriter, terminal_width  # noqa: E402

# The model list's readiness phrases, one per recipe. Each has a row under
# Catalog labels in docs/OPERATIONS.md (Serve: …); tests/test_help.py checks it.
READINESS = {
    "running": "running on {hosts}",
    "service": "{state} on {hosts}",
    "ready": "ready to start",
    "copy": "needs copying to {count} · {size}",
    "verify": "needs its files checked",
    "restore": "needs restoring from its archive",
    "download": "needs downloading · {size}",
    "too-big": "needs {needed}; the cluster has {have}",
    "unsupported": "start unsupported",
    "unknown": "size unknown",
}
# The plan screen's choices, in order; the cursor starts on Cancel (D1).
CHOICES = (("cancel", "Cancel"), ("go", "Go ahead"), ("ready", "Get it ready, but do not start"),
           ("change", "Change…"), ("details", "Details"))
# Start blockers a planned step resolves: they are expected in the dry run.
COVERED_BY = {"model_files_not_ready": ("register", "restore", "download", "prepare"),
              "image_missing": ("image",),
              "port_in_use": ("stop",), "memory_insufficient": ("stop",), "memory_warning": ("stop",),
              "service_exists": ("stop",)}
ACTIVE = ("running", "partial", "degraded")
FILE_STEPS = ("register", "restore", "download")
PLAN_KIND = "pulsar-serve-plan"


# A no-break space keeps a size or a choice together when a sentence wraps; it prints as a space.
KEEP = " "


def _kept(text: str) -> str:
    return text.replace(" ", KEEP)


def _list(names: list[str]) -> str:
    names = [str(name) for name in names]
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _count(number: int, one: str, many: str | None = None) -> str:
    return f"{number} {one if number == 1 else (many or one + 's')}"


# ---------------------------------------------------------------------------
# Saved facts about one recipe
# ---------------------------------------------------------------------------

def snapshot_sizes(repo: Path, spec_id: str) -> dict:
    """Bytes of each required snapshot, by the row's snapshot name (None for
    schema 2), read from the spec's manifests; {} when the spec cannot be read."""
    from release_spec.serving import required_snapshots
    try:
        spec = json.loads((Path(repo) / "releases" / f"{spec_id}.json").read_text(encoding="utf-8"))
        sizes = {name: int(model["snapshot_manifest"]["total_bytes"])
                 for name, model in required_snapshots(spec).items()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if spec.get("schema_version") != 3:
        return {None: sizes.get("target")}
    return sizes


def serving_nodes(row: dict, order: list[str], node: str | None) -> list[str]:
    """The serving node_ids: a one-node recipe's chosen node, or the first saved
    nodes in their saved order, as Start uses them. Empty when unknown."""
    count = row["geometry"]["nodes"]
    if count == 1:
        return [node] if node else []
    return list(order[:count]) if len(order) >= count else []


def without_home(row: dict) -> list:
    """Required snapshots with no recorded home, in the order they are recovered."""
    snapshots = members(row)
    return [name for name in sorted(snapshots, key=lambda name: (name != "target", name or ""))
            if not snapshots[name].get("home")]


def archive_available(row: dict, name, archive_location: str) -> bool:
    if archive_location != "configured":
        return False
    kind = archive_facts(row)[name][0]
    return kind in ("present", "verified")


def own_service(row: dict, inventory: dict | None, entries: list[dict]) -> dict | None:
    """This recipe's service in the inventory, projected (state, hosts, node_id), or None."""
    if not inventory:
        return None
    from scripts.inventory_menu import project
    catalog = [{"spec_id": entry["spec_id"], "model_id": entry["model_id"], "nodes": entry["geometry"]["nodes"]}
               for entry in entries]
    try:
        return project(inventory, catalog).get(row["spec_id"])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The model list
# ---------------------------------------------------------------------------

def readiness(row: dict, *, order: list[str], archive_location: str, sizes: dict,
              service: dict | None) -> str:
    """One phrase for where the recipe stands, from saved records and the inventory."""
    if service and service["state"] in ACTIVE:
        hosts = ", ".join(service["hosts"]) or "nodes not identified"
        if service["state"] == "running":
            return READINESS["running"].format(hosts=hosts)
        return READINESS["service"].format(state=SERVICE_STATES[service["state"]], hosts=hosts)
    nodes = row["geometry"]["nodes"]
    if start_unsupported(row):
        return READINESS["unsupported"]
    if order and nodes > len(order):
        return READINESS["too-big"].format(needed=node_count(nodes), have=node_count(len(order)))
    missing = without_home(row)
    if missing:
        if all(archive_available(row, name, archive_location) for name in missing):
            return READINESS["restore"]
        size = [sizes.get(name) for name in missing]
        text = short_size(sum(size)) if size and all(value is not None for value in size) else READINESS["unknown"]
        return READINESS["download"].format(size=text)
    if row.get("checked_at") and row.get("local_state") == "ready":
        return READINESS["ready"]
    count = copies_needed(row, serving_nodes(row, order, recorded_node(row)))
    if not count:
        return READINESS["verify"]
    total = [sizes.get(name) for name in members(row)]
    text = (short_size(sum(total) * count) if total and all(value is not None for value in total)
            else READINESS["unknown"])
    return READINESS["copy"].format(count=node_count(count), size=text)


def copies_needed(row: dict, serving: list[str]) -> int:
    """Serving nodes still to receive a copy, from saved records: those that hold
    neither the home of every required snapshot nor a prepared copy recorded
    for this recipe. A node with one snapshot's home still needs a copy of a
    snapshot whose home is elsewhere. A saved record is not proof the copy
    still verifies; the plan's own preview says."""
    homes = [(member.get("home") or {}).get("node_id") for member in members(row).values()]
    copied = {view.get("node_id") for view in row.get("prepared_copies") or []}
    return len([node for node in serving
                if not (homes and all(home == node for home in homes)) and node not in copied])


def _facts(rows: list[dict]) -> dict[str, list[str]]:
    """What tells each recipe apart from others of its model, without a digest or
    spec ID long enough to read as one (FORBIDDEN in the vocabulary test): an
    image is named by 8 characters. Recipes that differ in none of these facts
    are numbered in catalog order."""
    found: dict[str, list[str]] = {}
    for spec, facts in distinguishing_facts(rows).items():
        words = []
        for part in facts.sets[0]:
            if part.startswith("image ") and re.fullmatch(r"image [0-9a-f]{12,}", part):
                part = part[:len("image ") + 8]
            words.append(part)
        found[spec] = words if facts.unique else []
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row.get("model_id"), []).append(row["spec_id"])
    for specs in groups.values():
        if len(specs) > 1:
            for index, spec in enumerate(specs, 1):
                if not found.get(spec):
                    found[spec] = [f"recipe {index} of {len(specs)}"]
    return found


def list_lines(entries: list[dict], *, width: int, inventory: dict | None, archive_location: str,
               order: list[str], repo: Path, writer: TerminalWriter | None = None,
               leave_out: frozenset = frozenset()) -> list[str]:
    """The Serve list: a card per recipe grouped by model, each line the facts
    that tell it apart, its node count and one readiness phrase. Lines are
    KIND<TAB>TEXT, as the catalog list's cards: heading, spec, line, gap.
    LEAVE_OUT names recipes not to list, as Compare with another recipe
    leaves out the one it compares."""
    width = max(MIN_WIDTH, min(MAX_WIDTH, width))
    facts, names = _facts(entries), short_model_names(entries)
    shown = [row for row in entries if not row.get("historical") and row["spec_id"] not in leave_out]
    lines = []
    for index, row in enumerate(shown):
        if index == 0 or shown[index - 1].get("model_id") != row.get("model_id"):
            name = _fit_name(clean(row["model_id"]), clean(names[row["spec_id"]]) if row["spec_id"] in names
                             else None, width - CARD_NAME_COLUMN)
            if writer is not None:
                name = writer.paint(name, ["choice"] * len(name))
            lines.append("heading\t" + " " * CARD_NAME_COLUMN + name)
        parts = list(facts.get(row["spec_id"]) or [])
        phrase = readiness(row, order=order, archive_location=archive_location,
                           sizes=snapshot_sizes(repo, row["spec_id"]),
                           service=own_service(row, inventory, entries))
        # The node count, unless a fact or the phrase already names it.
        count = node_count(row["geometry"]["nodes"])
        if count not in parts and not phrase.startswith(f"needs {count}"):
            parts.append(count)
        if (row.get("review") or {}).get("status") == "withdrawn":
            parts.insert(0, "withdrawn")
        parts.append(phrase)
        text = _card(parts, width)
        lines.append(f"spec\t{row['spec_id']}\t{text[0]}")
        lines += [f"line\t{line}" for line in text[1:]]
        if index == len(shown) - 1 or shown[index + 1].get("model_id") != row.get("model_id"):
            lines.append("gap")
    return lines


def _card(parts: list[str], width: int) -> list[str]:
    import textwrap
    hang = " " * CARD_WRAP_COLUMN
    lines = [" " * CARD_SPEC_COLUMN + clean(parts[0])]
    for part in parts[1:]:
        part = clean(part)
        if len(lines[-1]) + 3 + len(part) <= width:
            lines[-1] += " · " + part
        else:
            lines.extend(textwrap.wrap(part, width, initial_indent=hang, subsequent_indent=hang,
                                       break_long_words=True, break_on_hyphens=False))
    return lines


def node_lines(row: dict, order: list[str], names: NodeNames, *, width: int) -> list[str]:
    """The machine question of a one-node recipe: a title, the node the cursor
    starts on (where its files are), then one choice per confirmed node."""
    model = clean(row["model_id"])
    lines = [f"title\tWhich machine should serve {model}?"]
    files = recorded_node(row)
    if files in order:
        lines.append(f"default\t{files}")
    homes = {(member.get("home") or {}).get("node_id") for member in members(row).values()}
    copies = {view.get("node_id") for view in row.get("prepared_copies") or []}
    for node in order:
        host = names(node)
        if node in homes:
            label = f"{host} — its model files are here"
        elif node in copies:
            label = f"{host} — a copy of its files is here"
        else:
            label = host
        lines.append(f"node\t{node}\t{label if len(label) <= width - 2 else host}")
    return lines


# ---------------------------------------------------------------------------
# Where missing files come from
# ---------------------------------------------------------------------------

def file_sources(row: dict, *, serving: list[str], order: list[str], archive_location: str,
                 files: dict[str, list[str]] | None = None, source: str | None = None,
                 files_node: str | None = None, not_found: frozenset = frozenset()) -> list[dict]:
    """Where each snapshot without a home comes from, in the design's order:
    files found on a node with no record naming them, then the archive, then
    a download. SOURCE (archive or download) is the operator's Change…, which
    skips found files. FILES_NODE is where restored or downloaded files go:
    the first serving node unless the operator changes it (D9); a multi-node
    recipe keeps them on one of its serving nodes. NOT_FOUND names snapshots
    whose found files Download's own plan did not take."""
    allowed = serving if row["geometry"]["nodes"] > 1 else order
    destination = files_node if files_node in allowed else (serving[0] if serving else None)
    result = []
    snapshots = members(row)
    for name in without_home(row):
        manifest = str(snapshots[name].get("snapshot_manifest_id"))
        holders = [node for node in (files or {}).get(manifest, []) if node in allowed]
        archive = archive_available(row, name, archive_location)
        if source is None and holders and name not in not_found:
            result.append({"snapshot": name, "kind": "register", "node": holders[0], "archive": archive})
        elif (source == "archive" or source is None) and archive:
            result.append({"snapshot": name, "kind": "restore", "node": destination, "archive": True})
        else:
            result.append({"snapshot": name, "kind": "download", "node": destination, "archive": archive})
    return result


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------

def load_json(path: str | None) -> Any:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def envelope_result(document: Any) -> tuple[dict | None, dict | None]:
    """(result, error) of a public JSON envelope; a bare document is its own result."""
    if not isinstance(document, dict):
        return None, None
    if "ok" in document:
        if document.get("ok") is True and isinstance(document.get("result"), dict):
            return document["result"], None
        error = document.get("error")
        return None, error if isinstance(error, dict) else {"message": "the preview failed"}
    return document, None


def image_lacking(plan: dict | None) -> list[str] | None:
    """Topology indexes of serving nodes without the pinned image, or None when unreadable."""
    if not isinstance(plan, dict) or plan.get("kind") != "pulsar-image-check":
        return None
    ranks = plan.get("ranks")
    if not isinstance(ranks, list):
        return None
    return [str(rank.get("topology_index")) for rank in ranks
            if isinstance(rank, dict) and rank.get("state") in ("missing", "other-name")]


def image_mode(plan: dict) -> str:
    """copy when this machine (topology index 0) is a serving node with the
    image; otherwise pull, which needs authority: whether this machine holds
    the image is not observed when it does not serve."""
    for rank in plan.get("ranks") or []:
        if isinstance(rank, dict) and rank.get("topology_index") == 0 and rank.get("state") == "ok":
            return "copy"
    return "pull"


def hf_finding(document: Any, host: str) -> tuple[str | None, str | None]:
    """(blocker, warning) for downloads to HOST from its prerequisites answer.

    A missing or unusable CLI blocks that destination; a rejected login warns;
    a Hub that could not be reached is not checked, never a failure."""
    from scripts.prerequisites import rows
    try:
        found = {identifier: (level, message) for level, identifier, message in rows(document)}
        hf = document["prerequisites"]["hf"]
    except (KeyError, TypeError, AttributeError):
        return None, f"hf on {host} was not checked: its prerequisites could not be read"
    blocker = warning = None
    if hf.get("state") != "ok":
        message = found.get("hf_cli", ("", "hf CLI not usable"))[1].replace("this node", host)
        blocker = f"Downloads to {host} need the hf CLI: {message}"
    login = hf.get("login")
    if not blocker and login in ("rejected", "not-reached"):
        warning = found["hf_login"][1].replace("this node", host)
    return blocker, warning


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def spec_ref(spec_id: str, entries: list[dict]) -> str:
    """How a command the menu runs names a spec: by its complete ID. `--json`
    requires it (the exact-ID contract), and `image` takes no shortened ID."""
    return spec_id


# Commands whose human mode accepts a catalog spec's 12-character prefix
# (scripts/spec_selector.py); the outcome shows them that way.
SHORTENED = ("start", "stop", "status", "model", "models", "logs")


def shown(command: list[str], entries: list[dict]) -> str:
    """COMMAND as the outcome names it under Same as: a catalog spec by its
    12-character prefix where the command's human mode accepts one."""
    catalog = {entry["spec_id"] for entry in entries}
    words = list(command)
    if len(words) > 1 and words[1] in SHORTENED and "--json" not in words:
        words = [word[:12] if word in catalog else word for word in words]
    return " ".join(words)


def compose(row: dict, entries: list[dict], *, names: NodeNames, node: str | None = None,
            archive_location: str = "not-configured", sizes: dict | None = None,
            inventory: dict | None = None, inventory_error: str | None = None,
            sources: list[dict] | None = None, previews: dict | None = None,
            hf: Any = None, unanswered: list[str] = (), ready_only: bool = False,
            api_key: str | None = None, image_source: str | None = None, stop: str | None = None,
            restart: bool = False) -> dict:
    """The plan for serving ROW, from saved records and the previews given.

    PREVIEWS maps "restore:NAME", "prepare", "image" and "start" to their
    parsed documents (an envelope or a bare plan); a missing key is a preview
    that did not run. SOURCES comes from file_sources. HF is the download
    destination's prerequisites answer. UNANSWERED lists node_ids that did not
    answer the last discovery: a plan that serves on one does not run.
    IMAGE_SOURCE "pull" is the operator's Change… to pull the serving image
    from its registry where a copy from this machine is possible too.

    STOP names a service the operator chose to stop, by its spec ID: Switch
    to another model… stops it directly before Start even when it runs on
    other nodes. RESTART plans Stop then Start of ROW's own service on the
    nodes it runs on, and nothing else: no files, copies or image."""
    previews = previews or {}
    sizes = sizes or {}
    spec = row["spec_id"]
    model = clean(row["model_id"])
    order = names.order
    nodes = row["geometry"]["nodes"]
    serving = serving_nodes(row, order, node)
    hosts = [names(item) for item in serving]
    ref = spec_ref(spec, entries)
    placement = ["--node", hosts[0]] if nodes == 1 and hosts else []
    told = [part for part in (_facts(entries).get(spec) or []) if part != node_count(nodes)]
    plan: dict = {"kind": PLAN_KIND, "spec_id": spec, "model_id": model, "ready_only": ready_only,
                  "restart": restart,
                  "title": (f"{'Get' if ready_only else 'Restart' if restart else 'Serve'} {model}"
                            + (f" ({' · '.join(told)})" if told else "")
                            + (" ready" if ready_only else "") + (f" on {_list(hosts)}" if hosts else "")),
                  "serving": serving, "hosts": hosts, "steps": [], "blocked": [], "warnings": [], "notes": [],
                  "projected": [], "stopped": [], "already": None, "api_key": api_key}
    blocked, steps = plan["blocked"], plan["steps"]
    if start_unsupported(row):
        blocked.append(f"This Stack cannot start this recipe: {start_unsupported(row)}.")
        return plan
    if not serving:
        blocked.append(f"It needs {node_count(nodes)}; the cluster has {node_count(len(order))}."
                       if nodes > 1 else "No machine was chosen to serve it.")
        return plan
    silent = [names(item) for item in serving if item in unanswered]
    if silent:
        blocked.append(f"{_list(silent)} did not answer the last cluster check, so Pulsar cannot tell whether "
                       f"{'it is' if len(silent) == 1 else 'they are'} down or gone. Review what changed… "
                       "under Cluster… keeps or leaves out a node that does not answer.")
    # What runs now, from one inventory: this recipe's own service, and others on its nodes.
    if inventory_error or not inventory:
        blocked.append(f"Running services could not be checked: {inventory_error or 'no inventory'}. "
                       "Unknown is not absent, so the plan does not run.")
        return plan
    service = own_service(row, inventory, entries)
    if service and service["state"] == "running" and not ready_only and not restart:
        plan["already"] = f"{model} is already running on {_list(service['hosts'])}."
        return plan
    from scripts.service_status import unobserved_nodes
    # A node the inventory did not observe matters when it serves, or when it cannot be identified.
    unseen = [clean(item["node"]) for item in unobserved_nodes(inventory["nodes"])
              if not item.get("node_id") or item["node_id"] in serving]
    if unseen:
        blocked.append(f"The service inventory did not observe {_list(unseen)}, so what runs there is unknown, "
                       "not absent. The plan does not run until it does.")
    catalog = [{"spec_id": entry["spec_id"], "model_id": entry["model_id"], "nodes": entry["geometry"]["nodes"]}
               for entry in entries]
    start = previews.get("start")
    start_result, start_error = envelope_result(start)
    records = [record for record in (start_error or {}).get("details") or [] if isinstance(record, dict)]
    ports = [int(match.group(1)) for record in records if record.get("blocker") == "port_in_use"
             for match in [re.fullmatch(r"port ([0-9]+)", clean(record.get("detail") or record.get("message") or ""))]
             if match]
    try:
        stops, notes = services_in_the_way(inventory, row, serving, catalog, ports)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        blocked.append(f"The service inventory could not be read: {exc}.")
        return plan
    if restart:
        return _restart(plan, row, entries, inventory, service, stops, hosts, placement, ref)
    if stop and stop != spec:
        _switch_from(plan, stop, stops, inventory, catalog)
    for note in notes:
        if "GPU process" in note or "more GPU processes" in note:
            plan["notes"].append(note)
        else:
            blocked.append(f"{note}. Pulsar cannot stop it from this plan.")

    # 1. Files: found, restored or downloaded, per required snapshot without a home.
    for found in sources or []:
        name, kind, where = found["snapshot"], found["kind"], found["node"]
        host = names(where) if where else "the first serving node"
        snapshot = ["--snapshot", str(name)] if name is not None else []
        what = "the model files" if name in (None, "target") else f"the files of {name}, a model it also loads"
        size = sizes.get(name)
        amount = _kept(short_size(size)) if size is not None else "size unknown"
        step = {"kind": kind, "snapshot": name, "node": where, "authority": False, "projected": False,
                "archive": bool(found.get("archive")), "facts": {"kind": kind, "node": where}}
        if kind == "register":
            step["sentence"] = (f"Check and record {what} found on {host}, which Pulsar has no record of; nothing "
                                "is downloaded when they match. They become its home, the copy Pulsar keeps.")
            step["changes"] = f"records the files on {host}"
            step["command"] = ["./pulsar", "model", "acquire", ref, *snapshot, "--node", host, "--yes"]
            step["preview"] = ["./pulsar", "model", "acquire", ref, *snapshot, "--node", host, "--plan", "--json"]
            step["projected"] = True
        elif kind == "restore":
            preview = previews.get(f"restore:{name}")
            document, error = envelope_result(preview)
            free = ""
            if document and document.get("kind") == "pulsar-restore-plan":
                space = (document.get("destination_space") or {}).get("available")
                if type(space) is int and space >= 0:
                    free = f"; {_kept(short_size(space))} free there"
                    if size is not None and space < size:
                        blocked.append(f"{host} has {short_size(space)} free; restoring {what} needs {amount}.")
            elif error is not None or preview is not None:
                blocked.append(f"Restoring {what} to {host} cannot run: "
                               f"{clean((error or {}).get('message') or 'its preview did not finish')}.")
            else:
                step["projected"] = True
            step["sentence"] = (f"Restore {what} from the archive to {host} ({amount}{free}). They become its "
                                "home, the copy Pulsar keeps.")
            step["changes"] = f"writes {amount} on {host}"
            step["command"] = ["./pulsar", "model", "restore", ref, *snapshot, "--node", host, "--yes"]
            step["preview"] = ["./pulsar", "model", "restore", ref, *snapshot, "--node", host, "--plan", "--json"]
        else:
            step["sentence"] = (f"Download {what} from Hugging Face to {host} ({amount}). They become its home, "
                                "the copy Pulsar keeps.")
            step["changes"] = f"writes {amount} on {host}"
            step["command"] = ["./pulsar", "model", "acquire", ref, *snapshot, "--node", host, "--yes"]
            step["preview"] = ["./pulsar", "model", "acquire", ref, *snapshot, "--node", host, "--plan", "--json"]
            step["projected"] = True
            blocker, warning = hf_finding(hf, host) if hf is not None else (
                None, f"hf on {host} was not checked")
            if blocker:
                blocked.append(blocker + " Change… keeps the files on another node.")
                plan["change_node"] = True
            if warning:
                plan["warnings"].append(warning)
        steps.append(step)

    # 2. Copies to every serving node.
    prepare = previews.get("prepare")
    homes = {(member.get("home") or {}).get("node_id") for member in members(row).values()}
    homes |= {found["node"] for found in sources or []}
    homes.discard(None)
    # Where each required snapshot's files are, or will be after its files step.
    placed = {name: (member.get("home") or {}).get("node_id") for name, member in members(row).items()}
    placed.update({found["snapshot"]: found["node"] for found in sources or []})

    def lacking(nodes: list[str]) -> list[str]:
        """The serving nodes that lack a required snapshot. Each node needs every
        snapshot, so a node that holds one snapshot's home still needs a copy of
        a snapshot whose home is on another node."""
        return [item for item in nodes if not placed or any(node != item for node in placed.values())]

    each = [sizes.get(name) for name in members(row)]
    amount = _kept(short_size(sum(each))) if each and all(value is not None for value in each) else None

    def copy_sentence(targets: list[str], tail: str) -> str:
        """Copy … to TARGETS: "them" after a files step, else from where the files are."""
        lead = "Copy them" if sources else (
            f"Copy the model files from {_list([names(item) for item in sorted(homes)])}" if homes
            else "Copy the model files")
        # A node that holds one snapshot's home needs the others only: the figure is a ceiling there.
        most = "up to " if any(item in homes for item in targets) else ""
        size = f" ({most}{amount}{' each' if len(targets) > 1 else ''})" if amount else ""
        return f"{lead} to {_list([names(item) for item in targets])}{size}{tail}"

    if prepare is not None or sources or row.get("local_state") != "ready" or not row.get("checked_at"):
        step = {"kind": "prepare", "authority": False, "projected": prepare is None, "node": None,
                "command": ["./pulsar", "model", "prepare", ref, *placement, "--yes"],
                "preview": ["./pulsar", "model", "prepare", ref, *placement, "--plan", "--json"]}
        document, error = envelope_result(prepare)
        kind = _plan_document(document).get("kind") if isinstance(document, dict) else None
        if kind in ("pulsar-preparation-plan", "pulsar-preparation-set-plan"):
            effect = plan_effect("prepare", document)
            copied = sorted({str(action["node_id"]) for action in _plan_actions(document)
                             if action.get("action") == "copy"}, key=lambda item: order.index(item)
                            if item in order else len(order))
            if plan_blocked(document):
                for line in plan_summary("prepare", row, document, names=names):
                    blocked.append(line.removeprefix("Blocked: "))
            step["facts"] = {"copy_to": copied}
            taken = effect["taken_over"]
            takeover = (f" It takes over {_count(taken, 'copy', 'copies')} made before the cluster's membership "
                        "changed, whose files still verify; they are not copied again." if taken else "")
            if copied:
                step["sentence"] = copy_sentence(copied, "." + takeover)
                step["changes"] = f"writes a prepared copy on {_list([names(item) for item in copied])}"
            else:
                step["sentence"] = (f"Check the files on {_list(hosts)} and record them for this recipe; nothing "
                                    "is copied." + takeover)
                step["changes"] = "records only"
                if effect["nothing"] and not taken and row.get("local_state") == "ready" and not sources:
                    step = None
        elif prepare is not None:
            error = error or {"message": "its preview did not return a plan"}
            blocked.append(f"Copying the files cannot be planned: {clean(error.get('message') or 'its preview failed')}.")
            step["facts"] = {"copy_to": lacking(serving)}
            step["sentence"] = "Copy the model files to the serving nodes that lack them."
            step["changes"] = "writes prepared copies"
        else:
            # No home yet, or no preview: Prepare's own plan runs when its turn comes.
            targets = lacking(serving)
            step["facts"] = {"copy_to": targets}
            if targets:
                step["sentence"] = copy_sentence(targets, "; a copy there that still verifies is taken over, not "
                                                          "copied again.")
                step["changes"] = f"writes a prepared copy on {_list([names(item) for item in targets])}"
            else:
                step["sentence"] = f"Check the files on {_list(hosts)} and record them for this recipe."
                step["changes"] = "records only"
        if step is not None:
            steps.append(step)

    # 3. The serving image.
    image = previews.get("image")
    image_doc, image_error = envelope_result(image)
    lacking = image_lacking(image_doc)
    if image is None:
        plan["notes"].append("The serving image was not checked yet; Start checks it.")
    elif lacking is None:
        blocked.append("The serving image could not be checked: "
                       f"{clean((image_error or {}).get('message') or 'its preview did not finish')}.")
    elif lacking:
        by_index = {str(index): item for index, item in enumerate(order)}
        targets = [by_index.get(index, index) for index in lacking]
        where = _list([names(item) for item in targets])
        mode = image_mode(image_doc)
        # Both sources are possible only where this machine's image was observed.
        copy_possible = mode == "copy"
        if image_source == "pull":
            mode = "pull"
        pull = ["--pull"] if mode == "pull" else []
        steps.append({"kind": "image", "authority": mode == "pull", "projected": False, "node": None,
                      "facts": {"mode": mode, "nodes": lacking, "copy_possible": copy_possible},
                      "sentence": (f"Pull the serving image on {where} (exact digest, from its registry)."
                                   if mode == "pull" else
                                   f"Copy the serving image from this machine to {where} (exact digest)."),
                      "changes": f"adds the image on {where}",
                      "command": ["./pulsar", "image", "stage", ref, *placement, *pull, "--yes"],
                      "preview": ["./pulsar", "image", "stage", ref, *placement, *pull, "--plan", "--json"]})

    # 4. Whatever is in the way, stopped directly before Start.
    if not ready_only:
        if service and service["state"] not in ("running",):
            where = _list(service["hosts"]) or "its nodes"
            words = SERVICE_STATES.get(service["state"], service["state"])
            stops = [{"conf": spec, "name": model, "hosts": service["hosts"], "port": None, "mib": None,
                      "own": True, "words": words}] + stops
        for stop in stops:
            where = _list(stop["hosts"]) or "its nodes"
            held = []
            if stop.get("port"):
                held.append(f"port {stop['port']}")
            if stop.get("mib") is not None:
                from model_library.catalog_menu import gib_text
                held.append(f"{_kept(gib_text(stop['mib']))} of memory"
                            + (" on each" if len(stop["hosts"]) > 1 else ""))
            if stop.get("own"):
                # Exited or stopped containers, or a service of this recipe only partly up.
                left = (f"the {stop['words']} of {stop['name']}" if stop["words"].endswith("containers")
                        else f"the containers of {stop['name']}, which is {stop['words']},")
                sentence = (f"Remove {left} on {where}; Start does not replace a service. Model files are "
                            "kept.")
            else:
                sentence = (f"Stop {stop['name']} on {where}"
                            + ("; you are switching from it." if stop.get("requested") else ".")
                            + (f" It holds {_list(held)}." if held else "") + " Its model files are kept.")
            own = next((entry for entry in entries if entry["spec_id"] == stop["conf"]), None)
            single = own is not None and own["geometry"]["nodes"] == 1
            steps.append({"kind": "stop", "authority": True, "projected": False, "node": None,
                          "facts": {"conf": stop["conf"], "requested": bool(stop.get("requested"))},
                          "sentence": sentence,
                          "changes": f"removes the service containers on {where}",
                          "command": ["./pulsar", "stop", spec_ref(stop["conf"], entries)],
                          "preview": None})
            if not stop.get("own"):
                # Starting it again after a failed Start is offered only for a
                # catalog recipe whose node is known when it serves on one.
                again = service_node(inventory, stop["conf"]) if single else None
                modified = any(item.get("conf") == stop["conf"] and item.get("matches_selected_spec") is False
                               for item in inventory.get("services") or [] if isinstance(item, dict))
                restart = (["./pulsar", "start", stop["conf"], *(["--node", names(again)] if again else [])]
                           if own is not None and not modified and (again or not single) else None)
                plan["stopped"].append({"conf": stop["conf"], "name": stop["name"], "hosts": stop["hosts"],
                                        "in_catalog": restart is not None, "node": again, "restart": restart,
                                        "restart_shown": shown(restart, entries) if restart else None})

    # 5. Start, and what its dry run said.
    if not ready_only:
        key = ("The API will require the key set in " + api_key + "." if api_key
               else "The API will accept requests without a key.")
        step = {"kind": "start", "authority": False, "projected": True, "node": None, "facts": {},
                "sentence": f"Start the service and wait until it answers. This can take several minutes. {key}",
                "changes": f"starts the service on {_list(hosts)}",
                "command": ["./pulsar", "start", ref, *placement], "preview": None}
        kinds = {item["kind"] for item in steps}
        checks = (start_result or start_error or {}).get("checks") or []
        if start is None:
            plan["notes"].append("Start's checks were not run yet; Start runs them when its turn comes.")
        elif start_result is not None:
            step["projected"] = bool(steps)
        elif start_error is not None:
            if not records:
                blocked.append(f"Start's checks could not run: {clean(start_error.get('message') or 'unknown')}.")
            for record in records:
                code = record.get("blocker")
                if any(kind in kinds for kind in COVERED_BY.get(code, ())):
                    continue
                message = clean(record.get("message") or code)
                fix = clean(record.get("fix") or "")
                blocked.append(f"{message}." + (f" Next: {fix}" if fix else "") if not message.endswith(".")
                               else message + (f" Next: {fix}" if fix else ""))
        # Checks the dry run left unrun make Start an estimate, which the footer says.
        if any(isinstance(check, dict) and check.get("outcome") == "not_run" for check in checks):
            step["projected"] = True
        # A dry run accepts a memory warning; Start itself refuses on one, and
        # this plan never passes --accept-memory-warn. Only the outcome of a
        # Start refused for nothing else offers it, as a choice of its own.
        if any(isinstance(check, dict) and check.get("id") == "memory" and check.get("outcome") == "info"
               for check in checks):
            plan["warnings"].append("Start's dry run found free memory below the recommended headroom. If it is "
                                    "still low when Start runs, Start refuses and this plan stops there; its "
                                    "outcome then offers to start it with less free memory than recommended.")
        steps.append(step)
    for step in steps:
        step["shown"] = shown(step["command"], entries)

    # Only step sentences keep their no-break spaces, for the screen's wrapping.
    for key in ("blocked", "warnings", "notes"):
        plan[key] = [text.replace(KEEP, " ") for text in plan[key]]
    for index, step in enumerate(steps):
        if step["projected"]:
            plan["projected"].append(index)
    return plan


def _switch_from(plan: dict, conf: str, stops: list[dict], inventory: dict, catalog: list[dict]) -> None:
    """Add the service the operator is switching from to STOPS, last, so it is
    stopped directly before Start wherever it runs; or say why it cannot be."""
    from scripts.inventory_menu import project, project_others
    found = next((stop for stop in stops if stop["conf"] == conf), None)
    if found is not None:
        stops.remove(found)
        stops.append({**found, "requested": True})
        return
    services = {**project(inventory, catalog), **project_others(inventory, catalog)}
    target = services.get(conf)
    if target is None:
        plan["notes"].append("The service you are switching from was not seen just now, so there is nothing to "
                             "stop for it.")
    elif target["stop_reason"]:
        plan["blocked"].append(f"Pulsar cannot stop {clean(target['model_id'])}, which you are switching from: "
                               f"{clean(target['stop_reason']).rstrip('.')}.")
    else:
        stops.append({"conf": conf, "name": clean(target["model_id"]), "hosts": list(target["hosts"]),
                      "port": target["port"], "mib": None, "requested": True})


def _restart(plan: dict, row: dict, entries: list[dict], inventory: dict, service: dict | None,
             beside: list[dict], hosts: list[str], placement: list[str], ref: str) -> dict:
    """Restart: Stop then Start of ROW's own service on the nodes it runs on.

    BESIDE are the other services on those nodes; they ran beside it, so
    the Stop step's own check does not count them as new."""
    from scripts.inventory_menu import project
    spec, model = row["spec_id"], plan["model_id"]
    blocked = plan["blocked"]
    catalog = [{"spec_id": entry["spec_id"], "model_id": entry["model_id"], "nodes": entry["geometry"]["nodes"]}
               for entry in entries]
    observed = project(inventory, catalog).get(spec)
    if service is None or observed is None:
        blocked.append(f"{model} was not seen running just now, so there is nothing to restart; Serve a model… "
                       "starts it.")
        return plan
    if observed["stop_reason"]:
        blocked.append(f"Pulsar cannot stop {model}: {clean(observed['stop_reason']).rstrip('.')}.")
    if observed["modified"]:
        blocked.append(f"{model} was started from a modified recipe, so Start would not start the same service.")
    if list(observed["hosts"]) != list(hosts):
        blocked.append(f"{model} runs on {_list(observed['hosts'])}, but Start serves it on {_list(hosts)}, so a "
                       "restart would not use the same nodes.")
    plan["coexisting"] = [item["conf"] for item in beside]
    where = _list(observed["hosts"]) or "its nodes"
    start = ["./pulsar", "start", ref, *placement]
    plan["steps"] += [
        {"kind": "stop", "authority": True, "projected": False, "node": None,
         "facts": {"conf": spec, "requested": True},
         "sentence": f"Stop {model} on {where}. Its model files are kept.",
         "changes": f"removes the service containers on {where}",
         "command": ["./pulsar", "stop", spec_ref(spec, entries)], "preview": None},
        {"kind": "start", "authority": False, "projected": True, "node": None, "facts": {},
         "sentence": ("Start it again on the same nodes and wait until it answers. This can take several "
                      "minutes. " + ("The API will require the key set in " + plan["api_key"] + "."
                                     if plan.get("api_key") else "The API will accept requests without a key.")),
         "changes": f"starts the service on {where}", "command": start, "preview": None}]
    plan["stopped"].append({"conf": spec, "name": model, "hosts": list(observed["hosts"]), "in_catalog": True,
                            "node": observed.get("node_id"), "restart": start,
                            "restart_shown": shown(start, entries)})
    for step in plan["steps"]:
        step["shown"] = shown(step["command"], entries)
    plan["projected"] = [1]
    return plan


def service_node(inventory: dict, conf: str) -> str | None:
    """The node_id a one-node service ran on, for starting it again after a failed Start."""
    for service in inventory.get("services") or []:
        if isinstance(service, dict) and service.get("conf") == conf:
            ranks = [rank for rank in service.get("ranks") or [] if isinstance(rank, dict)]
            if len(ranks) == 1:
                node = (inventory.get("nodes") or {}).get(ranks[0].get("node")) or {}
                return node.get("node_id")
    return None


# ---------------------------------------------------------------------------
# The plan screen
# ---------------------------------------------------------------------------

def render(plan: dict, *, width: int | None = None) -> str:
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    out.emit(plan["title"], role="heading")
    out.blank()
    if plan.get("already"):
        out.emit(plan["already"] + " Nothing to do.")
        return output.getvalue()
    steps = plan["steps"]
    if not steps:
        out.emit("Nothing to do: the files are ready on every serving node.")
    else:
        out.emit("Pulsar will:")
        for index, step in enumerate(steps, 1):
            mark = f"  {index}. "
            out.emit(step["sentence"], mark, " " * len(mark))
    out.blank()
    for line in footer(plan):
        out.emit(line)
    for note in plan["warnings"]:
        out.emit(note, "Warning: ", "  ", prefix_role="warn")
    for note in plan["notes"]:
        out.emit(note)
    if plan["blocked"]:
        out.blank()
        for reason in plan["blocked"]:
            out.emit(reason, "Blocked: ", "  ", prefix_role="fail")
    return output.getvalue().replace(KEEP, " ")


def footer(plan: dict) -> list[str]:
    """What the plan leaves untouched, and what a failure leaves."""
    steps = plan["steps"]
    lines = ["Nothing is deleted."]
    serving = {item["conf"] for item in plan.get("stopped") or []}
    stops = [index for index, step in enumerate(steps, 1)
             if step["kind"] == "stop" and (step.get("facts") or {}).get("conf") in serving]
    start = next((index for index, step in enumerate(steps, 1) if step["kind"] == "start"), None)
    others = [item["name"] for item in plan.get("stopped") or []]
    if stops and others:
        lines[0] += (f" {_list(others)} keeps serving until step {stops[0]}."
                     + (f" If step {start} fails, nothing is serving; Pulsar says so and offers to start "
                        f"{'it' if len(others) == 1 else 'them'} again." if start else ""))
    elif steps:
        lines[0] += " If a step fails, the steps before it stay done and the plan stops there."
    projected = [index + 1 for index in plan.get("projected") or []]
    if projected:
        lines.append(f"{'Step' if len(projected) == 1 else 'Steps'} {_list([str(n) for n in projected])} "
                     f"{'is an estimate' if len(projected) == 1 else 'are estimates'} until {'it runs' if len(projected) == 1 else 'they run'}: "
                     "each step checks first, and Pulsar stops and asks again if one is blocked or would do more "
                     "than it says.")
    return lines


def choices(plan: dict) -> list[tuple[str, str]]:
    """The plan screen's choices: Cancel first, then Go ahead only when nothing blocks."""
    found = [("cancel", "Cancel")]
    if plan.get("already"):
        return found + [("details", "Details")]
    if plan.get("restart"):
        return found + ([("go", "Go ahead")] if plan["steps"] and not plan["blocked"] else []) + [
            ("details", "Details")]
    runnable = plan["steps"] and not plan["blocked"]
    if runnable and not plan.get("ready_only"):
        found.append(("go", "Go ahead"))
    if runnable and any(step["kind"] not in ("stop", "start") for step in plan["steps"]):
        found.append(("ready", "Get it ready, but do not start" if not plan.get("ready_only") else "Go ahead"))
    # Change… only where something can change: a one-node recipe's machine,
    # where restored or downloaded files come from and go, or the serving
    # image's source where both a copy and a pull are possible.
    if len(plan.get("serving") or []) == 1 or any(
            step["kind"] in ("restore", "download")
            or (step["kind"] == "image" and (step.get("facts") or {}).get("copy_possible"))
            for step in plan["steps"]):
        found.append(("change", "Change…"))
    found.append(("details", "Details"))
    return found


# ---------------------------------------------------------------------------
# Running a step
# ---------------------------------------------------------------------------

def check_step(plan: dict, index: int, preview: Any, rc: int = 0, *, names: NodeNames | None = None,
               entries: list[dict] = ()) -> tuple[str, str]:
    """Whether a step's own preview, run when its turn came, still does what the
    approved plan said: ("same" | "skip" | "differs" | "blocked", reason)."""
    names = names or NodeNames()
    step = plan["steps"][index]
    kind, facts = step["kind"], step.get("facts") or {}
    document, error = envelope_result(preview)
    if kind == "stop":
        if document is None:
            return "blocked", "the service inventory could not be read, so what runs now is unknown"
        row = {"spec_id": plan["spec_id"], "geometry": {"nodes": len(plan["serving"])}}
        catalog = [{"spec_id": entry["spec_id"], "model_id": entry["model_id"], "nodes": entry["geometry"]["nodes"]}
                   for entry in entries]
        approved = {item["facts"]["conf"] for item in plan["steps"] if item["kind"] == "stop"}
        try:
            stops, _ = services_in_the_way(document, row, plan["serving"], catalog)
        except (ValueError, KeyError, TypeError, AttributeError):
            return "blocked", "the service inventory could not be read, so what runs now is unknown"
        found = {stop["conf"] for stop in stops}
        # A service the operator chose to stop is found wherever it runs.
        present = {item.get("conf") for item in document.get("services") or [] if isinstance(item, dict)}
        found |= {item["facts"]["conf"] for item in plan["steps"]
                  if item["kind"] == "stop" and (item.get("facts") or {}).get("requested")} & present
        if facts["conf"] == plan["spec_id"]:
            mine = any(service.get("conf") == plan["spec_id"] for service in document.get("services") or [])
            found.discard(plan["spec_id"])
            if mine:
                found.add(plan["spec_id"])
        extra = sorted(found - approved - set(plan.get("coexisting") or []))
        if extra:
            other = next((stop["name"] for stop in stops if stop["conf"] == extra[0]), extra[0][:12])
            return "differs", f"{other} now runs on a serving node too, and the plan did not say to stop it"
        if facts["conf"] not in found:
            return "skip", "it no longer runs"
        return "same", ""
    if rc != 0 or document is None:
        message = clean((error or {}).get("message") or (f"its preview exited {rc}" if rc else "its preview "
                                                                                                "was unreadable"))
        return "blocked", message
    if kind in ("register", "download"):
        if document.get("kind") != "pulsar-acquisition-plan":
            return "blocked", "Download's preview did not return its plan"
        reuse = document.get("action") == "reuse"
        node = (document.get("home") or {}).get("node_id") if reuse else document.get("selected_node")
        if kind == "register" and not reuse:
            return "differs", (f"the files found on {names(facts['node'])} do not match the recipe; Download would "
                               "fetch them from Hugging Face instead")
        if kind == "download" and reuse:
            return "differs", f"verified files are already on {names(node)}; nothing needs downloading"
        if node != facts["node"]:
            return "differs", f"the files would go to {names(node)}, not {names(facts['node'])}"
        return "same", ""
    if kind == "restore":
        if document.get("kind") != "pulsar-restore-plan":
            return "blocked", "Restore's preview did not return its plan"
        if document.get("selected_node") != facts["node"]:
            return "differs", f"the files would go to {names(document.get('selected_node'))}"
        return "same", ""
    if kind == "prepare":
        if plan_blocked(document):
            lines = plan_summary("prepare", {"model_id": plan["model_id"]}, document, names=names)
            return "blocked", " ".join(line.removeprefix("Blocked: ") for line in lines)
        copied = {str(action["node_id"]) for action in _plan_actions(document) if action.get("action") == "copy"}
        extra = sorted(copied - set(facts.get("copy_to") or []))
        if extra:
            return "differs", f"it would also copy the files to {_list([names(item) for item in extra])}"
        return "same", ""
    if kind == "image":
        lacking = image_lacking(document)
        if lacking is None:
            return "blocked", "the image preview did not return its plan"
        if not lacking:
            return "skip", "the image is already on every serving node"
        mode = "pull" if document.get("mode") == "pull-exact-digest" else "copy"
        if mode != facts["mode"] or set(lacking) - set(facts["nodes"]):
            return "differs", "the image would be staged on other nodes or another way than the plan said"
        return "same", ""
    return "same", ""


def memory_warning(records: list[dict]) -> str | None:
    """What a Start refused for nothing but a memory warning said, or None.

    The same test the catalog menu uses before it offers the retry
    (`memory_warning_only`); "" when the record names no detail."""
    from model_library.catalog_menu import memory_warning_only
    if not memory_warning_only(records):
        return None
    details = [clean(record.get("detail") or "") for record in records]
    return "; ".join(dict.fromkeys(detail for detail in details if detail))


def outcome(plan: dict, *, done: int, failed: int | None = None, rc: int = 0, ran: list[str] = (),
            stopped_ran: bool = False, halted: str | None = None, memory: str | None = None,
            width: int | None = None) -> tuple[str, list[tuple[str, str]]]:
    """What happened: the steps done, a failed step and what it leaves, then the
    commands that ran under Same as. HALTED is why step FAILED did not run:
    its own preview found it blocked, or doing what the plan did not say.
    MEMORY is what a Start refused for nothing but a memory warning said
    (memory_warning); only then is starting it anyway offered, as its own
    choice. Returns (text, choices)."""
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    steps = plan["steps"]
    choices_: list[tuple[str, str]] = []
    interrupted = rc in (3, 130) or 128 < rc < 160
    if halted is not None and failed is not None:
        stopped = plan.get("stopped") or []
        out.emit(f"Step {failed + 1} did not run: {halted}. Pulsar stopped before it, so it can ask again.",
                 "✗ ", "  ", prefix_role="fail")
        out.emit(f"Done: {_count(done, 'step')} before it." if done else "Nothing was changed.")
        if stopped and not stopped_ran:
            out.emit(f"Still serving: {_list([item['name'] for item in stopped])}.")
        elif stopped_ran:
            out.emit("Nothing is serving now: the plan stopped what was in the way before this step.")
        choices_.append(("again", "Plan again from the state as it is now"))
    elif failed is None:
        if plan.get("ready_only") or not any(step["kind"] == "start" for step in steps):
            out.emit(f"Ready: {plan['model_id']} has its files and image on {_list(plan['hosts'])}. "
                     "Nothing was started.", "✓ ", "  ")
        else:
            out.emit(f"Serving: {plan['model_id']} started on {_list(plan['hosts'])}. Running services… shows how "
                     "to connect.", "✓ ", "  ")
    else:
        step = steps[failed]
        what = step["sentence"].split(". ")[0].rstrip(".")
        if interrupted:
            out.emit(f"Step {failed + 1} was interrupted: {what}. Its outcome is not confirmed; inspect before "
                     "retrying.", "✗ ", "  ", prefix_role="fail")
        elif rc == 2:
            out.emit(f"Step {failed + 1} was refused before it changed anything: {what}.", "✗ ", "  ",
                     prefix_role="fail")
        else:
            out.emit(f"Step {failed + 1} did not finish: {what}. The reason is above.", "✗ ", "  ",
                     prefix_role="fail")
        out.emit(f"Done: {_count(done, 'step')} before it." if done else "No step before it ran.")
        stopped = plan.get("stopped") or []
        names = _list([item["name"] for item in stopped])
        were = "was" if len(stopped) == 1 else "were"
        if stopped_ran and step["kind"] == "start" and interrupted:
            # An interrupted Start may have launched its service: what serves is not known.
            out.emit(f"{names} {were} stopped before Start. Whether this model's service started is not "
                     "confirmed.")
        elif stopped_ran and step["kind"] == "start":
            out.emit(f"Nothing is serving now: {names} {were} stopped before Start.")
            for index, item in enumerate(stopped):
                if item.get("in_catalog"):
                    choices_.append((f"restart:{index}", f"Start {item['name']} again"))
        elif stopped_ran and step["kind"] == "stop":
            out.emit("A service was stopped before this step; whether the one this step stops still runs is "
                     "not confirmed.")
        elif stopped and not stopped_ran:
            out.emit(f"Still serving: {_list([item['name'] for item in stopped])}; the plan stopped before "
                     "stopping it.")
        if step["kind"] == "start" and rc == 1 and memory is not None:
            # Start refused for nothing but its memory warning: starting anyway
            # is the operator's own choice here, never part of the plan.
            out.emit("Start refused only because free memory is below the recommended headroom"
                     + (f": {memory}." if memory else ".")
                     + " Starting anyway can fail or slow other work on those nodes.")
            choices_.append(("memory", f"Start {plan['model_id']} with less free memory than recommended"))
        if interrupted and step["kind"] in FILE_STEPS + ("prepare",):
            choices_.append(("inspect", "Check its files now (read-only)"))
        elif interrupted and step["kind"] in ("start", "stop"):
            choices_.append(("status", "Check the service now (read-only)"))
        elif not interrupted:
            out.emit("Choose Serve a model… again to plan from the state as it is now; finished steps drop out.")
    if ran:
        out.blank()
        out.emit("Same as:")
        for command in ran:
            out.emit(command, "  ", "    ", role="command")
    return output.getvalue().replace(KEEP, " "), choices_ + [("back", "Back")]


# ---------------------------------------------------------------------------
# What the menu runs before it composes
# ---------------------------------------------------------------------------

# The pre-serve change check reuses a discovery of the saved membership this recent.
CHANGES_FRESH_SECONDS = 10 * 60


def entry_state(repo: Path, now=None, *, discovered: bool = False) -> str:
    """Where the pre-serve change check stands, from the saved records:

    discover     no discovery of the saved membership in the last ten minutes;
    unchanged    the last one saw the cluster as saved;
    kept         it saw only members the operator kept as only down;
    changed      it saw a change to review;
    not-checked  it did not finish (or, after DISCOVERED, saved nothing)."""
    from model_library.catalog import age_seconds
    from scripts.cluster_status import Records
    records = Records(repo, now)
    changes = records.changes
    age = age_seconds(changes["observed_at"], records.now) if changes else None
    stale = changes is None or age is None or age > CHANGES_FRESH_SECONDS
    if stale and not discovered:
        return "discover"
    # A discovery that saved nothing leaves an old record, which says nothing about now.
    if stale or changes["result"] == "not-checked":
        return "not-checked"
    if changes["result"] == "unchanged":
        return "unchanged"
    kept = set((changes.get("kept") or {}).get("missing") or [])
    missing = changes.get("missing") or []
    if (missing and not changes.get("found") and not changes.get("changed")
            and all(not item.get("answered") and item["node_id"] in kept for item in missing)):
        return "kept"
    return "changed"


def not_checked_text(repo: Path, now=None, *, width: int | None = None) -> str:
    """Why the change check could not say whether the cluster changed."""
    from scripts.cluster_status import Records
    records = Records(repo, now)
    issues = "; ".join((records.changes or {}).get("issues") or []) or "no reason was recorded"
    output = io.StringIO()
    TerminalWriter(width=width, stream=output).emit(
        f"The cluster check did not finish: {issues}. Pulsar can still serve on the cluster as saved; a plan "
        "that needs a node that does not answer stops before it changes anything.", "✗ ", "  ", prefix_role="fail")
    return output.getvalue()


def preview_requests(row: dict, entries: list[dict], *, names: NodeNames, node: str | None,
                     sources: list[dict], files_scanned: bool, source: str | None,
                     ready_only: bool = False, restart: bool = False) -> list[tuple[str, list[str]]]:
    """The previews the first screen can run now, as (NAME, COMMAND), in order.

    A files scan comes first and alone when a snapshot has no home and no
    scan ran yet (unless the operator picked the archive or a download): the
    sources depend on it. Then a restore plan per restored snapshot, `hf` with
    the node_id a download goes to (its prerequisites, one Hub request), the
    prepare plan once every snapshot has a home, the image plan, and Start's
    dry run. A download's own plan resolves its source on the Hub, so it runs
    only when its turn comes, after the hf check."""
    serving = serving_nodes(row, names.order, node)
    if not serving or restart:
        return []
    if without_home(row) and not files_scanned and source is None:
        return [("files", ["./pulsar", "model", "files", "--json"])]
    ref = spec_ref(row["spec_id"], entries)
    placement = ["--node", names(serving[0])] if row["geometry"]["nodes"] == 1 else []
    requests: list[tuple[str, list[str]]] = []
    for item in sources:
        name = "-" if item["snapshot"] is None else str(item["snapshot"])
        snapshot = ["--snapshot", name] if item["snapshot"] is not None else []
        if item["kind"] == "restore":
            requests.append((f"restore:{name}", ["./pulsar", "model", "restore", ref, *snapshot, "--node",
                                                 names(item["node"]), "--plan", "--json"]))
        elif item["kind"] == "download" and ("hf", [item["node"]]) not in requests:
            requests.append(("hf", [item["node"]]))
    if not sources:
        requests.append(("prepare", ["./pulsar", "model", "prepare", ref, *placement, "--plan", "--json"]))
    requests.append(("image", ["./pulsar", "image", "stage", ref, *placement, "--plan", "--json"]))
    if not ready_only:
        requests.append(("start", ["./pulsar", "start", ref, *placement, "--dry-run", "--json"]))
    return requests


def running_services(inventory: dict | None, entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Pulsar services with a running container, for stopping them before a save:
    (stoppable services as {conf, name, hosts}, the ones Stop cannot be offered for)."""
    from scripts.inventory_menu import project, project_others
    if not inventory:
        return [], []
    catalog = [{"spec_id": entry["spec_id"], "model_id": entry["model_id"], "nodes": entry["geometry"]["nodes"]}
               for entry in entries]
    try:
        services = {**project(inventory, catalog), **project_others(inventory, catalog)}
    except ValueError:
        return [], ["the service inventory could not be read"]
    stops, refused = [], []
    for conf, service in services.items():
        if service["state"] not in ACTIVE:
            continue
        if service.get("stop_reason"):
            refused.append(f"{service['model_id']}: {service['stop_reason']}")
        else:
            stops.append({"conf": conf, "name": clean(service["model_id"]), "hosts": service["hosts"]})
    return stops, refused


# ---------------------------------------------------------------------------

def _entries(stream) -> list[dict]:
    try:
        return json.load(stream).get("entries") or []
    except (ValueError, AttributeError):
        raise ValueError("the catalog document could not be read")


def _row(entries: list[dict], spec_id: str) -> dict:
    rows = [row for row in entries if row.get("spec_id") == spec_id]
    if len(rows) != 1:
        raise ValueError("the selected recipe is not in the catalog")
    return rows[0]


def _unanswered(repo: Path, topology_id: str | None) -> list[str]:
    """Members that did not answer the last discovery of this membership, kept or not."""
    from scripts import saved_observations as saved
    try:
        record = saved.load_changes(saved.record_path(repo, saved.CHANGES_NAME))
    except (ValueError, OSError):
        return []
    if not record or record.get("topology_id") != topology_id:
        return []
    return [item["node_id"] for item in record.get("missing") or [] if not item.get("answered")]


def _topology_id(repo: Path) -> str | None:
    from scripts.cluster_status import membership
    state, topology = membership(repo)
    return topology["topology_id"] if state == "saved" else None


def _api_key(repo: Path) -> str | None:
    from scripts.cluster_status import api_key_source
    name, source = api_key_source(repo)
    if name in (None, "unreadable"):
        return None
    return {"process": "the environment that started Pulsar", "dotenv": "this checkout's .env file"}[source]


def _archive_location(repo: Path) -> str:
    """The archive location's status as the catalog menu passes it; a setting
    that cannot be read is no archive Pulsar can restore from."""
    from scripts.setup_status import _archive_status
    status = _archive_status(Path(repo), None)["status"]
    return status if status in ARCHIVE_LOCATION else "not-configured"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    entry = sub.add_parser("entry", help="where the pre-serve change check stands")
    entry.add_argument("--discovered", action="store_true", help="a discovery just ran")
    entry.add_argument("--explain", action="store_true", help="why the check did not finish")
    entry.add_argument("--save-flags", action="store_true", help="the topology configure flags that save it")
    running = sub.add_parser("running", help="running Pulsar services, to stop before a save")
    running.add_argument("--inventory")
    listing = sub.add_parser("list")
    listing.add_argument("--inventory")
    listing.add_argument("--leave-out", action="append", default=[], help="a recipe not to list")
    nodes = sub.add_parser("nodes")
    nodes.add_argument("--spec-id", required=True)
    previews = sub.add_parser("previews")
    compose_ = sub.add_parser("compose")
    for command in (previews, compose_):
        command.add_argument("--spec-id", required=True)
        command.add_argument("--node")
        command.add_argument("--files")
        command.add_argument("--source", choices=("archive", "download"))
        command.add_argument("--files-node")
        command.add_argument("--not-found", action="append", default=[])
        command.add_argument("--ready-only", action="store_true")
        command.add_argument("--image-source", choices=("pull",), help="pull the serving image instead of a copy")
        command.add_argument("--stop", help="a service to stop directly before Start (Switch to another model…)")
        command.add_argument("--restart", action="store_true", help="Stop, then Start, of the recipe's own service")
    compose_.add_argument("--inventory")
    compose_.add_argument("--inventory-error")
    compose_.add_argument("--preview", action="append", default=[], help="NAME=FILE: restore:SNAPSHOT, prepare, "
                          "image or start")
    compose_.add_argument("--hf")
    compose_.add_argument("--out", required=True)
    for command in (listing, previews, compose_):
        command.add_argument("--archive-location", choices=sorted(ARCHIVE_LOCATION))
    for name in ("render", "steps", "choices", "facts"):
        sub.add_parser(name).add_argument("--plan", required=True)
    command_ = sub.add_parser("command")
    command_.add_argument("--plan", required=True)
    which = command_.add_mutually_exclusive_group(required=True)
    which.add_argument("--step", type=int)
    which.add_argument("--restart", type=int, help="start the INDEXth stopped service again")
    which.add_argument("--accept-memory-warning", action="store_true",
                       help="the plan's Start with Start's own memory-warning acknowledgement")
    command_.add_argument("--preview", action="store_true")
    command_.add_argument("--shown", action="store_true", help="with --restart: as the outcome names it")
    check = sub.add_parser("check-step")
    check.add_argument("--plan", required=True)
    check.add_argument("--step", type=int, required=True)
    check.add_argument("--preview-file")
    check.add_argument("--preview-rc", type=int, default=0)
    result = sub.add_parser("outcome")
    result.add_argument("--plan", required=True)
    result.add_argument("--done", type=int, required=True)
    result.add_argument("--failed", type=int)
    result.add_argument("--rc", type=int, default=0)
    result.add_argument("--ran-step", type=int, action="append", default=[], help="a step whose command ran")
    result.add_argument("--halted", help="why the failed step did not run")
    result.add_argument("--blockers", help="the blockers the failed Start recorded (PULSAR_START_BLOCKERS_FILE)")
    result.add_argument("--choices", action="store_true")
    for command in sub.choices.values():
        command.add_argument("--repo-root", type=Path, default=ROOT)
        command.add_argument("--width", type=int)
    args = parser.parse_args(argv)
    width = args.width or terminal_width()
    try:
        names = NodeNames.saved(args.repo_root)
        location = getattr(args, "archive_location", None) or _archive_location(args.repo_root)
        if args.command == "entry":
            if args.save_flags:
                # Saving the change leaves out the members that did not answer.
                if _unanswered(args.repo_root, _topology_id(args.repo_root)):
                    print("--remove-missing")
                return 0
            if args.explain:
                sys.stdout.write(not_checked_text(args.repo_root, width=width))
            else:
                print(entry_state(args.repo_root, discovered=args.discovered))
            return 0
        if args.command == "running":
            inventory = load_json(args.inventory)
            stops, refused = running_services(inventory if isinstance(inventory, dict) else None,
                                              _entries(sys.stdin))
            # CONF, NAME and hosts for each service Stop can remove; "refused" and why for the others.
            for stop in stops:
                print(f"{stop['conf']}\t{stop['name']}\t{', '.join(stop['hosts'])}")
            for reason in refused:
                print(f"refused\t{clean(reason)}")
            return 0
        if args.command == "list":
            entries = _entries(sys.stdin)
            inventory = load_json(args.inventory)
            for line in list_lines(entries, width=width - 2, inventory=inventory if isinstance(inventory, dict)
                                   else None, archive_location=location,
                                   order=names.order, repo=args.repo_root,
                                   writer=TerminalWriter(width=width), leave_out=frozenset(args.leave_out)):
                print(line)
            return 0
        if args.command == "nodes":
            # Nothing for a multi-node recipe: it serves on the saved order.
            row = _row(_entries(sys.stdin), args.spec_id)
            if row["geometry"]["nodes"] == 1:
                print("\n".join(node_lines(row, names.order, names, width=width)))
            return 0
        if args.command in ("previews", "compose"):
            entries = _entries(sys.stdin)
            row = _row(entries, args.spec_id)
            serving = serving_nodes(row, names.order, args.node)
            found = file_sources(row, serving=serving, order=names.order, archive_location=location,
                                 files=found_files(args.files), source=args.source, files_node=args.files_node,
                                 not_found=frozenset(None if item == "-" else item for item in args.not_found))
            if args.command == "previews":
                # NAME, a tab, then the command's arguments, each after a tab;
                # "hf" names the node_id to probe instead.
                for name, command in preview_requests(row, entries, names=names, node=args.node, sources=found,
                                                      files_scanned=bool(args.files), source=args.source,
                                                      ready_only=args.ready_only, restart=args.restart):
                    print("\t".join([name, *command]))
                return 0
            # A schema-2 recipe's one snapshot has no name; its restore preview is restore:-.
            loaded = {}
            for value in args.preview:
                key, separator, path = value.partition("=")
                if not separator:
                    raise ValueError("previews must use NAME=FILE")
                loaded["restore:None" if key == "restore:-" else key] = load_json(path)
            inventory = load_json(args.inventory)
            plan = compose(row, entries, names=names, node=args.node, archive_location=location,
                           sizes=snapshot_sizes(args.repo_root, row["spec_id"]),
                           inventory=inventory if isinstance(inventory, dict) else None,
                           inventory_error=args.inventory_error, sources=found, previews=loaded,
                           hf=load_json(args.hf) if args.hf else None,
                           unanswered=_unanswered(args.repo_root, _topology_id(args.repo_root)),
                           ready_only=args.ready_only, api_key=_api_key(args.repo_root),
                           image_source=args.image_source, stop=args.stop, restart=args.restart)
            Path(args.out).write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
            return 0
        plan = load_json(args.plan)
        if not isinstance(plan, dict) or plan.get("kind") != PLAN_KIND:
            raise ValueError("the plan could not be read")
        if args.command == "render":
            sys.stdout.write(render(plan, width=width))
        elif args.command == "steps":
            # INDEX, KIND, whether it needs authority, whether it has a preview,
            # its snapshot (- when none or unnamed) and its sentence.
            for index, step in enumerate(plan["steps"]):
                snapshot = step.get("snapshot")
                print(f"{index}\t{step['kind']}\t{int(step['authority'])}\t{int(bool(step.get('preview')))}\t"
                      f"{'-' if snapshot is None else snapshot}\t{clean(step['sentence'].replace(KEEP, ' '))}")
        elif args.command == "facts":
            # What Change… can offer, as KEY<TAB>VALUE lines.
            kinds = {step["kind"] for step in plan["steps"]}
            print(f"nodes\t{len(plan['serving']) if plan['serving'] else 0}")
            print(f"files\t{int(bool(kinds & {'restore', 'download'}))}")
            print(f"archive\t{int(any(step.get('kind') in FILE_STEPS and step.get('archive') for step in plan['steps']))}")
            print(f"source\t{'restore' if 'restore' in kinds else 'download' if 'download' in kinds else '-'}")
            print(f"stopped\t{len(plan.get('stopped') or [])}")
            # The serving image's source, and whether a copy from this machine is possible too.
            image = next((step for step in plan["steps"] if step["kind"] == "image"), None)
            if image is not None:
                facts = image.get("facts") or {}
                print(f"image\t{facts.get('mode')}\t{int(bool(facts.get('copy_possible')))}")
            # Where restored or downloaded files can go: a serving node of a
            # multi-node recipe, any node of a one-node recipe.
            if kinds & {"restore", "download"}:
                allowed = plan["serving"] if len(plan["serving"]) > 1 else names.order
                current = next(step["node"] for step in plan["steps"] if step["kind"] in ("restore", "download"))
                for node_id in allowed:
                    print(f"destination\t{node_id}\t{names(node_id)}{' (now)' if node_id == current else ''}")
        elif args.command == "choices":
            for action, label in choices(plan):
                print(f"{action}\t{label}")
        elif args.command == "command":
            if args.restart is not None:
                command = plan["stopped"][args.restart].get("restart")
                if not command:
                    raise ValueError("that service cannot be started again from this menu")
                if args.shown:
                    # How the outcome names it, on one line.
                    print(plan["stopped"][args.restart]["restart_shown"])
                    return 0
            elif args.accept_memory_warning:
                # Start's existing acknowledgement flag, added only here: the
                # operator chose it on the outcome of a Start refused for it.
                start = next((step for step in plan["steps"] if step["kind"] == "start"), None)
                if start is None:
                    raise ValueError("this plan has no Start")
                command = [*start["command"], "--accept-memory-warn"]
                if args.shown:
                    print(f"{start['shown']} --accept-memory-warn")
                    return 0
            else:
                step = plan["steps"][args.step]
                command = step["preview"] if args.preview else step["command"]
                if not command:
                    raise ValueError("that step has no preview command")
            print("\n".join(command))
        elif args.command == "check-step":
            entries = []
            from scripts.release_consumer import list_releases
            try:
                entries = [{"spec_id": item["spec_id"], "model_id": item["model_id"],
                            "geometry": {"nodes": item["nodes"]}} for item in list_releases(args.repo_root)]
            except (OSError, ValueError, KeyError, TypeError):
                entries = []
            verdict, reason = check_step(plan, args.step, load_json(args.preview_file), args.preview_rc,
                                         names=names, entries=entries)
            print(f"{verdict}\t{clean(reason)}")
        elif args.command == "outcome":
            ran = [plan["steps"][index].get("shown") or " ".join(plan["steps"][index]["command"])
                   for index in args.ran_step]
            # Removing this recipe's own leftover containers stops nothing that served.
            others = {item["conf"] for item in plan.get("stopped") or []}
            stopped = any(plan["steps"][index]["kind"] == "stop" and index != args.failed
                          and (plan["steps"][index].get("facts") or {}).get("conf") in others
                          for index in args.ran_step)
            memory = None
            if args.blockers and args.failed is not None and plan["steps"][args.failed]["kind"] == "start":
                from scripts.start_blockers import read
                try:
                    memory = memory_warning(read(args.blockers))
                except (OSError, ValueError):
                    memory = None
            text, found = outcome(plan, done=args.done, failed=args.failed, rc=args.rc, ran=ran,
                                  stopped_ran=stopped, halted=args.halted, memory=memory, width=width)
            if args.choices:
                for action, label in found:
                    print(f"{action}\t{label}")
            else:
                sys.stdout.write(text)
        return 0
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        from scripts.terminal_format import error_line
        error_line(exc, tag="serve plan")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
