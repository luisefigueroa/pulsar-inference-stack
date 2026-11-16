#!/usr/bin/env python3
"""Models and disk space…: room on each node, the models on the cluster, and
the plans that free space, keep a model, back it up and check its archives.

    python3 -m scripts.models_page page|choices --files FILE [--budget FILE] < CATALOG_JSON
    python3 -m scripts.models_page screen|options free|keep|backup|check --files FILE
            [--budget FILE] < CATALOG_JSON
    python3 -m scripts.models_page previews|compose --target TARGET --files FILE [--budget FILE]
            [--archive-first] [--preview NAME=FILE]... [--out PLAN] < CATALOG_JSON
    python3 -m scripts.models_page render|steps|choices-of --plan PLAN
    python3 -m scripts.models_page command --plan PLAN --step I [--preview]
    python3 -m scripts.models_page check-step --plan PLAN --step I --preview-file FILE [--preview-rc N]
    python3 -m scripts.models_page outcome --plan PLAN --done N [--failed I --rc N] [--halted WHY]
            [--ran-step I]...

Every fact comes from the catalog's saved records, one model files scan
(`./pulsar model files --json`) and one storage budget (`./pulsar model budget
--json`), taken on entering the page and reused until a plan runs, and from the
manifest files saved under `.pulsar/manifests` of the checkout
(saved_manifests). Nothing here contacts a node; the menu
(scripts/models-menu.sh) runs every command.

The page's choices follow a model's life on the cluster in four groups
(page_groups). A choice whose label cannot say what it does carries a second
line (HINTS), and `choices` prints each as ACTION, its label and its card.

A home no recipe in the catalog uses (loose_homes) has no spec to select it:
its plans pass `--manifest FILE` with the saved file, are offered only while
that file is saved and verifies, and each step's own preview must name the
same manifest when its turn comes.

Sizes are what the commands would free: Purge's from the estimate the Model
files page uses (model_library.file_inventory purge_estimate), a home's from
its listed size, and each step's own `--plan --json` when it runs. A size that
is not known is said to be unknown, never zero. A node that did not answer
makes its files and room unknown, not absent, and nothing destructive is
offered while one does not answer: Purge, Remove home, Move home and Pin all
need every node.

A plan is an ordered list of steps, each with a sentence, the `./pulsar`
command it runs and its preview, approved once with the cursor on Cancel. When
a step's turn comes the menu runs its own preview again, and `check-step` says
whether it still does what the plan said (`same`, `skip`, `differs` or
`blocked`). No plan unpins on its own, deletes an archive, deletes files no
record names, or moves a home unless the operator chose that move.

Record model files found on the nodes… (`compose --target find`) is one
plan from the same scan: a step per home folder no record names, through
Download's own reuse (it holds only if `model acquire … --plan --json` answers
`reuse`), then a step per recipe whose prepared copies no record names, through
`model prepare … --adopt-unrecorded`, the only menu step that passes that flag
(D7). What neither can record is listed with why and what gets the model back.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_library.catalog import age_seconds, age_text  # noqa: E402
from model_library.catalog_menu import (ARCHIVE_LOCATION, _plan_actions, _plan_document, clean,  # noqa: E402
                                        members, plan_blocked, plan_effect, plan_summary)
from model_library.node_names import NodeNames  # noqa: E402
from model_library.render import can_still_copy, short_size  # noqa: E402
from scripts.serve_plan import _facts, _list, envelope_result, load_json, shown  # noqa: E402
from scripts.terminal_format import MAX_WIDTH, MIN_WIDTH, TerminalWriter, terminal_width  # noqa: E402

PLAN_KIND = "pulsar-storage-plan"
DOWNLOAD_LABEL = "Download a model that has no recipe yet…"
READY_LABEL = "Get a model with a recipe ready to serve…"
KEEP_LABEL = "Keep a model on the cluster…"
BACKUP_LABEL = "Back up a model to the archive…"
CHECK_LABEL = "Check the archive copies…"
FIND_LABEL = "Record model files found on the nodes…"
MOVE_LABEL = "Move a model to another node…"
# What a choice does, where its label cannot say it: a second line under the label, in lower case
# with no full stop, at most 40 characters so that it stays one line at 44 columns. It names what
# the operator sees and does, never a low-level noun (tests/test_cluster_menu.FORBIDDEN).
HINTS = {"download": "files and hashes only, to one node",
         "ready": "files, copies and image; nothing starts",
         "move": "copies, checks, then frees the old node",
         "keep": "Free up space never removes it",
         "check": "compares hashes; changes nothing",
         "free": "remove a model's files or move them",
         "find": "files Pulsar has no record of"}
LABEL_WIDTH = 10
# A no-break space keeps a size together when a sentence wraps; it prints as a space.
KEEP = "\u00a0"

# The words this page prints about a node or a model. Each has a row under
# Catalog labels in docs/OPERATIONS.md (Models: …); tests/test_help.py checks it.
STATUS_WORDS = {
    "free": "free",
    "room": "room for",
    "past-reserve": "past its reserve",
    "room-unknown": "not checked, so its files and room are unknown",
    "home-on": "home on",
    "home-copy": "(also its copy there)",
    "copies-on": "copies on",
    "no-copies": "no copies on serving nodes",
    "files-missing": "files not found on",
    "files-unknown": "files could not be checked on",
    "kept": "kept",
    "not-kept": "not kept",
    "archived": "archived,",
    "no-archive": "no archive recorded",
    "archive-unverified": "archive seen, not verified",
    "partly-archived": "partly archived",
    "in-use": "in use by a container",
    "unused": "no recipe in this catalog uses these files",
    "size-unknown": "size unknown",
}
BACKUPS_NEED = "Backups need the archive location: Cluster… → Settings…"
# What the page's words mean, said once under the list (rule 3: in place).
EXPLAIN = "Home: the copy Pulsar keeps; copies: what serving nodes load; kept: never offered by Free up space…"


def _kept(text: str) -> str:
    return text.replace(" ", KEEP)


def _size(value: Any) -> str:
    return _kept(short_size(value)) if type(value) is int and value >= 0 else STATUS_WORDS["size-unknown"]


def _sum(values: list) -> int | None:
    return sum(values) if values and all(type(value) is int for value in values) else None


def _count(number: int, one: str, many: str | None = None) -> str:
    return f"{number} {one if number == 1 else (many or one + 's')}"


def recipe_names(cluster: "Cluster", specs) -> str:
    """SPECS for people: each catalog recipe by its name, then the recipes
    outside this catalog counted once ("a recipe outside this catalog", "5
    recipes outside this catalog"), joined as a list."""
    specs = list(dict.fromkeys(specs))
    named = [cluster.name(spec) for spec in specs if spec in cluster.catalog]
    outside = len(specs) - len(named)
    if outside:
        named.append("a recipe outside this catalog" if outside == 1 else
                     f"{outside} recipes outside this catalog")
    return _list(named)


# ---------------------------------------------------------------------------
# What one files scan, one storage budget and the catalog say
# ---------------------------------------------------------------------------

def files_document(path: str | None) -> dict | None:
    """The model files scan (an envelope or a bare document), or None when unreadable."""
    document, _ = envelope_result(load_json(path))
    if not isinstance(document, dict) or document.get("kind") != "pulsar-model-file-inventory":
        return None
    return document


def budget_nodes(path: str | None) -> list[dict] | None:
    """The storage budget's nodes, or None when it was not observed."""
    document, _ = envelope_result(load_json(path))
    if not isinstance(document, dict) or document.get("kind") != "pulsar-storage-budget":
        return None
    nodes = document.get("nodes")
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else None


def saved_manifests(repo_root: Path) -> dict[str, str]:
    """The manifest files saved under `.pulsar/manifests` of the checkout, by manifest ID.

    Download a model that has no recipe yet… saves them there. Only a file the shared verifier
    accepts counts: the commands that take the file with --manifest refuse any other. Of two files
    with the same manifest, the first by path is used."""
    from model_library.integrity import verify_manifest
    folder = Path(repo_root) / ".pulsar" / "manifests"
    found: dict[str, str] = {}
    try:
        paths = sorted(folder.rglob("*.json")) if folder.is_dir() else []
    except OSError:
        return found
    for path in paths:
        try:
            document = verify_manifest(load_json(str(path)))
        except (ValueError, OSError, TypeError, KeyError):
            continue
        found.setdefault(document["manifest_id"], str(path))
    return found


class Cluster:
    """The catalog joined to one files scan and one storage budget. SAVED is saved_manifests: with
    its file, the commands can select a home that no recipe in the catalog uses."""

    def __init__(self, entries: list[dict], files: dict, budget: list[dict] | None, *,
                 names: NodeNames | None = None, archive_location: str = "not-configured", now=None,
                 pairs: list[tuple[str, str]] = (), topology_id: str | None = None,
                 saved: dict[str, str] | None = None):
        self.saved = dict(saved or {})
        from model_library.file_inventory import _unread
        # A saved ring's neighbouring pairs (serve_plan.ring_pairs) and the saved
        # cluster's topology_id: with them, readiness names a ring's placements.
        self.pairs, self.topology_id = list(pairs), topology_id
        self.entries = [row for row in entries if not row.get("historical")]
        self.catalog = {row["spec_id"]: row for row in self.entries}
        self.files = files
        self.nodes = [node for node in files.get("nodes") or [] if isinstance(node, dict)]
        self.snapshots = [entry for entry in files.get("snapshots") or [] if isinstance(entry, dict)]
        self.by_manifest = {entry["snapshot_manifest_id"]: entry for entry in self.snapshots}
        self.budget = {item.get("node_id"): item for item in budget or []} if budget is not None else None
        self.names = names or NodeNames()
        self.archive_location = archive_location
        self.now = now
        self.unread = [node["node_id"] for node in self.nodes if _unread(node)]
        self.facts = _facts(self.entries)

    # -- names ----------------------------------------------------------------

    def host(self, node_id: str | None) -> str:
        for node in self.nodes:
            if node.get("node_id") == node_id and node.get("hostname"):
                return clean(node["hostname"])
        return self.names(node_id) if node_id else "an unknown node"

    def hosts(self, node_ids) -> str:
        return _list([self.host(node) for node in self.ordered(node_ids)])

    def ordered(self, node_ids) -> list[str]:
        order = [node["node_id"] for node in self.nodes]
        return sorted(dict.fromkeys(node_ids), key=lambda node: order.index(node) if node in order else len(order))

    def name(self, spec: str) -> str:
        """A recipe by its model name and, where recipes of one model differ, by what differs."""
        row = self.catalog[spec]
        facts = self.facts.get(spec) or []
        return clean(row["model_id"]) + (f" ({' · '.join(facts)})" if facts else "")

    def model_name(self, specs) -> str:
        """Several recipes' files by their model's name, or a recipe's name when it is the only
        one of its model."""
        specs = list(specs)
        models = {clean(self.catalog[spec]["model_id"]) for spec in specs}
        if len(specs) == 1:
            return self.name(specs[0])
        return next(iter(models)) if len(models) == 1 else _list(sorted(models))

    # -- one recipe's files ----------------------------------------------------

    def manifests(self, spec: str) -> dict:
        """Each required snapshot's manifest id, by its name (None for schema 2)."""
        return {name: member.get("snapshot_manifest_id") for name, member in members(self.catalog[spec]).items()}

    def entry(self, manifest: str | None) -> dict | None:
        return self.by_manifest.get(manifest) if manifest else None

    def homes(self, spec: str) -> dict:
        """Each required snapshot's home location in the scan, by its name; None when it has none."""
        found = {}
        for name, manifest in self.manifests(spec).items():
            entry = self.entry(manifest)
            found[name] = next((location for location in (entry or {}).get("locations") or []
                                if location.get("registered_home")), None)
        return found

    def bound(self, spec: str) -> list[tuple[dict, dict]]:
        """(entry, location) for every location in the scan that SPEC's records bind."""
        return [(entry, location) for entry in self.snapshots for location in entry.get("locations") or []
                if any(binding.get("spec_id") == spec for binding in location.get("bindings") or [])]

    def copies(self, spec: str) -> list[str]:
        """Nodes holding a copy SPEC serves from, the home itself included when it doubles as one."""
        return self.ordered(location["node_id"] for _, location in self.bound(spec))

    def unfinished(self, spec: str) -> list[dict]:
        from model_library.file_inventory import _unfinished
        return _unfinished(self.nodes, spec)

    def snapshot_size(self, spec: str, name) -> int | None:
        """One required snapshot's listed size, from the scan."""
        return (self.entry(self.manifests(spec).get(name)) or {}).get("payload_bytes")

    def size(self, spec: str) -> int | None:
        """The listed size of every required snapshot, once."""
        return _sum([(self.entry(manifest) or {}).get("payload_bytes") for manifest in self.manifests(spec).values()])

    def on_cluster(self, spec: str) -> bool:
        return bool(any(self.homes(spec).values()) or self.bound(spec) or self.unfinished(spec))

    def kept(self, spec: str) -> tuple[list[str], list[str]]:
        """(nodes where SPEC's copies are pinned, nodes where they are not), from observed records only."""
        pinned, loose = [], []
        for _, location in self.bound(spec):
            own = [binding for binding in location.get("bindings") or [] if binding.get("spec_id") == spec]
            if not location.get("bindings_observed"):
                continue
            (pinned if any(binding.get("pinned") for binding in own) else loose).append(location["node_id"])
        return self.ordered(pinned), self.ordered(loose)

    def archives(self, spec: str) -> dict:
        """Each required snapshot's archive fact (kind, age), from the catalog's saved records."""
        from model_library.catalog import archive_facts
        return {name: (kind, age) for name, (kind, age, _) in archive_facts(self.catalog[spec], self.now).items()}

    def archive_words(self, spec: str) -> str:
        facts = self.archives(spec)
        kinds = [kind for kind, _ in facts.values()]
        if all(kind == "verified" for kind in kinds):
            ages = [age for _, age in facts.values()]
            age = None if None in ages else max(ages)
            return f"{STATUS_WORDS['archived']} verified {age_text(age) if age is not None else '(age unknown)'}"
        if any(kind == "verified" for kind in kinds):
            return STATUS_WORDS["partly-archived"]
        if any(kind == "present" for kind in kinds):
            return STATUS_WORDS["archive-unverified"]
        return STATUS_WORDS["no-archive"]

    def readiness(self, spec: str) -> str:
        """What the page says of SPEC's readiness: Serve a model…'s phrase (saved_readiness), except
        that "ready to start" is the result of the last saved check, which the scan does not repeat.
        Where the scan, which is newer, did not find a home or a copy the records name for SPEC, or
        could not check one, the page says that instead. A "ready to start" that stays carries the
        saved check's own age, as in Serve a model…, since the page's first line gives only the
        scan's. A record on a node outside confirmed membership does not count: no scan reads that
        node, and Start does not use it."""
        from model_library.file_inventory import OUTSIDE_MEMBERSHIP
        from scripts.serve_plan import READINESS
        phrase = self.saved_readiness(spec)
        # Only a phrase that says ready to start rests on the saved check; the others claim no readiness.
        if not phrase.startswith(READINESS["ready"]):
            return phrase
        outside = {node["node_id"] for node in self.nodes if node.get("error") == OUTSIDE_MEMBERSHIP}
        locations = [location for location in self.homes(spec).values() if location]
        locations += [location for _, location in self.bound(spec)]
        locations = [location for location in locations if location["node_id"] not in outside]
        missing = [location["node_id"] for location in locations if location.get("state") == "missing"]
        unknown = [location["node_id"] for location in locations if location.get("state") not in ("present", "missing")]
        if missing:
            return f"{STATUS_WORDS['files-missing']} {self.hosts(missing)}"
        if unknown:
            return f"{STATUS_WORDS['files-unknown']} {self.hosts(unknown)}"
        return phrase

    def saved_readiness(self, spec: str) -> str:
        """Serve a model…'s readiness phrase, from saved records, with the scan's sizes."""
        from scripts.serve_plan import readiness
        sizes = {name: (self.entry(manifest) or {}).get("payload_bytes")
                 for name, manifest in self.manifests(spec).items()}
        ring = {"host": self.host, "pairs": self.pairs, "topology_id": self.topology_id} if self.names.ring else {}
        return readiness(self.catalog[spec], order=self.names.order, archive_location=self.archive_location,
                         sizes=sizes, service=None, ring=self.names.ring, **ring)

    # -- containers that use files --------------------------------------------

    def users(self, locations: list[dict]) -> list[dict]:
        """Each container that mounts LOCATIONS: who started it, its name and node."""
        from model_library.planning import CONF_LABEL, MANAGED_LABEL, SELECTED_SPEC_LABEL, SPEC_LABEL
        found = []
        for location in locations:
            for container in location.get("container_references") or []:
                labels = container.get("labels") if isinstance(container.get("labels"), dict) else {}
                managed = labels.get(MANAGED_LABEL) == "true"
                owner = (labels.get(SELECTED_SPEC_LABEL) or labels.get(CONF_LABEL) or labels.get(SPEC_LABEL)
                         if managed else None)
                item = {"node": location["node_id"], "owner": owner, "managed": managed,
                        "name": clean(container.get("name") or str(container.get("id") or "")[:12] or "unnamed")}
                if item not in found:
                    found.append(item)
        return found


# ---------------------------------------------------------------------------
# Homes that no recipe in the catalog uses
# ---------------------------------------------------------------------------

NO_MANIFEST = "their manifest, the list of the files and their hashes, is not saved in this checkout"
# What saves it: Download's own reuse writes the manifest of files it finds on the node it is given.
SAVE_MANIFEST = (f"{DOWNLOAD_LABEL} saves it: choose it for the same model, commit and node; it keeps the files "
                 "there and downloads nothing.")



def _model(entry: dict) -> str:
    return f"{clean(entry.get('model_id') or 'Unknown model')} @ {str(entry.get('model_commit') or '?')[:8]}"


def loose_homes(cluster: Cluster) -> list[dict]:
    """Each home in the scan that no recipe in this catalog uses, as a download before its recipe
    leaves it: its name, node, size, saved manifest file (None when not saved), archive record and
    why its home cannot be removed or moved now (None when it can).

    Without a recipe a command selects these files with `--manifest FILE`, so every choice for them
    needs the saved file, and its own preview must name the same manifest when its turn comes."""
    found = []
    for entry in cluster.snapshots:
        locations = entry.get("locations") or []
        home = next((location for location in locations if location.get("registered_home")), None)
        if entry.get("catalog_specs") or home is None:
            continue
        record = entry.get("archive") if isinstance(entry.get("archive"), dict) else None
        host = cluster.host(home["node_id"])
        users = cluster.users([home])
        if cluster.unread:
            stays = None  # the page says once that nothing is offered until every node answers
        elif home.get("state") != "present":
            stays = f"its files on {host} " + ("were not found" if home.get("state") == "missing" else
                                               "could not be checked")
        elif any(location.get("bindings") for location in locations):
            stays = "copies made for a recipe outside this catalog use these files"
        elif users:
            stays = f"{_list([item['name'] for item in users])} on {host} mounts these files"
        elif home.get("container_references") != []:
            stays = f"the containers on {host} could not be checked"
        elif entry["snapshot_manifest_id"] not in cluster.saved:
            stays = NO_MANIFEST
        else:
            stays = None
        found.append({"id": entry["snapshot_manifest_id"], "entry": entry, "name": _model(entry), "home": home,
                      "node": home["node_id"], "size": entry.get("payload_bytes"),
                      "file": cluster.saved.get(entry["snapshot_manifest_id"]),
                      "archived": record is not None,
                      "age": age_seconds(record.get("verified_at"), cluster.now) if record else None,
                      "stays": stays})
    return found


def loose_home(cluster: Cluster, manifest: str) -> dict:
    item = next((item for item in loose_homes(cluster) if item["id"] == manifest), None)
    if item is None:
        raise ValueError("those files are not in the scan")
    if not item["file"]:
        raise ValueError("the manifest file of those files is no longer saved in this checkout")
    return item


def _archive_words(item: dict) -> str:
    if not item["archived"]:
        return STATUS_WORDS["no-archive"]
    age = item["age"]
    return f"{STATUS_WORDS['archived']} verified {age_text(age) if age is not None else '(age unknown)'}"


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def room_lines(cluster: Cluster) -> list[tuple[str, str, str | None]]:
    """(host, words, role) for each node: disk free and room left for copies, or unknown."""
    lines = []
    for node in cluster.nodes:
        if node["node_id"] in cluster.unread:
            reason = clean(node.get("error") or node.get("container_error") or "")
            lines.append((cluster.host(node["node_id"]),
                          STATUS_WORDS["room-unknown"] + (f" ({reason})" if reason else ""), "warn"))
            continue
        free = sorted({disk.get("available") for disk in node.get("disks") or []
                       if isinstance(disk, dict) and not disk.get("error") and type(disk.get("available")) is int})
        parts = [f"{_size(free[0])}{'–' + _size(free[-1]) if len(free) > 1 else ''} {STATUS_WORDS['free']}"
                 if free else f"{STATUS_WORDS['free']} space not observed"]
        budget = (cluster.budget or {}).get(node["node_id"])
        left = can_still_copy(budget) if budget else None
        role = None
        if left is not None and left < 0:
            parts.append(f"{_size(-left)} {STATUS_WORDS['past-reserve']}")
            role = "warn"
        elif left is not None:
            parts.append(f"{STATUS_WORDS['room']} {_size(left)}")
        lines.append((cluster.host(node["node_id"]), " · ".join(parts), role))
    return lines


def model_lines(cluster: Cluster, spec: str, room: int | None = None) -> list[str]:
    """One recipe's facts: size and where its files are, then ready, kept and archived.

    On a saved ring, where the readiness phrase names placements, each part of
    the second line that fits in ROOM columns with its " ·" is kept whole, so
    the line breaks only between parts; a longer part breaks between words,
    never inside a pair "first + second"."""
    homes = {name: location for name, location in cluster.homes(spec).items() if location}
    where = [_size(cluster.size(spec))]
    bound = cluster.bound(spec)
    if homes:
        # The home doubles as a serving node's copy where this recipe is bound to it.
        serves = any(location.get("registered_home") for _, location in bound)
        where.append(f"{STATUS_WORDS['home-on']} {cluster.hosts(location['node_id'] for location in homes.values())}"
                     + (f" {STATUS_WORDS['home-copy']}" if serves else ""))
    copies = cluster.ordered(location["node_id"] for _, location in bound if not location.get("registered_home"))
    if copies or not bound:
        where.append(f"{STATUS_WORDS['copies-on']} {cluster.hosts(copies)}" if copies else STATUS_WORDS["no-copies"])
    state = [cluster.readiness(spec)]
    ring = bool(cluster.names.ring and room)
    if ring:
        state = state[0].split(" · ")
    pinned, loose = cluster.kept(spec)
    if pinned or loose:
        state.append(STATUS_WORDS["kept"] if pinned and not loose else
                     f"{STATUS_WORDS['kept']} on {cluster.hosts(pinned)}" if pinned else STATUS_WORDS["not-kept"])
    state.append(cluster.archive_words(spec))
    used = cluster.users([location for _, location in cluster.bound(spec)] + list(homes.values()))
    if used:
        state.append(f"{STATUS_WORDS['in-use']} on {cluster.hosts(item['node'] for item in used)}")
    if ring:
        # Each part that fits keeps its " ·", so a line never starts with one.
        from scripts.serve_plan import keep_pairs
        return [" · ".join(where), f"{KEEP}· ".join(_kept(part) if len(part) + 2 <= room
                                                     else keep_pairs(part, room) for part in state)]
    return [" · ".join(where), " · ".join(state)]


def page(cluster: Cluster, *, width: int | None = None) -> str:
    """Room on each node, then each model on the cluster: compact, so three nodes, one model and
    the page's labels fit 80x25; a longer page opens in the pager."""
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    age = age_text(age_seconds(cluster.files.get("observed_at"), cluster.now)) \
        if cluster.files.get("observed_at") else "not observed"
    out.emit(f"Models and disk space · checked {age}", role="heading")
    rows = room_lines(cluster)
    column = max([len(host) for host, _, _ in rows] + [7]) + 2
    hang = " " * (LABEL_WIDTH + column)
    for index, (host, words, role) in enumerate(rows):
        lead = f"{'Room' if index == 0 else '':<{LABEL_WIDTH}}{host:<{column}}"
        out.emit(words, lead, hang, prefix_role=role)
    if cluster.budget is None:
        out.emit("The storage budget was not observed, so the room for copies is unknown.", " " * LABEL_WIDTH)
    shown_specs = [row["spec_id"] for row in cluster.entries if cluster.on_cluster(row["spec_id"])]
    indent, more = " " * LABEL_WIDTH, " " * (LABEL_WIDTH + 2)
    first = True

    def label() -> str:
        nonlocal first
        text, first = (f"{'Models':<{LABEL_WIDTH}}" if first else indent), False
        return text
    for spec in shown_specs:
        lines = model_lines(cluster, spec, out.width - len(more))
        out.emit(f"{cluster.name(spec)} · {lines[0]}", label(), more)
        out.emit(lines[1], more, more)
    loose = {item["id"]: item for item in loose_homes(cluster)}
    for entry in cluster.snapshots:
        if entry.get("catalog_specs") or not any(location.get("state") != "missing"
                                                 for location in entry.get("locations") or []):
            continue
        where = cluster.hosts(location["node_id"] for location in entry.get("locations") or [])
        item = loose.get(entry["snapshot_manifest_id"])
        out.emit(f"{_model(entry)} · {_size(entry.get('payload_bytes'))} on {where} · {STATUS_WORDS['unused']}"
                 + (f" · {_archive_words(item)}" if item else ""), label(), more)
    found = find_items(cluster)
    holders = [item["node"] for item in found["register"]] + [node for item in found["adopt"] for node in item["nodes"]]
    if holders:
        # Catalog files the scan found that no record names: on no line above, so said here.
        out.emit(f"Files Pulsar has no record of are on {cluster.hosts(holders)}; {FIND_LABEL} records them.",
                 label(), more)
    if first:
        out.emit(f"No model in the catalog has files on the cluster. Serve a model… or {READY_LABEL} brings one.",
                 label(), indent)
    others = [row for row in cluster.entries if row["spec_id"] not in shown_specs]
    if others and shown_specs:
        out.emit(f"{_count(len(others), 'other recipe')} in the catalog {'has' if len(others) == 1 else 'have'} no "
                 "files on the cluster.", indent, indent)
    if shown_specs:
        out.emit(EXPLAIN, indent, indent)
    if cluster.archive_location != "configured":
        out.emit(BACKUPS_NEED)
    return output.getvalue().replace(KEEP, " ")


def page_groups(cluster: Cluster) -> list[list[tuple[str, str]]]:
    """The page's choices in the order of a model's life on the cluster, a group for each stage:
    bring its files, protect them, make room, then look at what is there."""
    protect = [("keep", KEEP_LABEL)]
    if cluster.archive_location == "configured":
        protect += [("backup", BACKUP_LABEL), ("check", CHECK_LABEL)]
    return [[("download", DOWNLOAD_LABEL), ("ready", READY_LABEL), ("move", MOVE_LABEL)], protect,
            [("free", "Free up space…")],
            [("find", FIND_LABEL), ("again", "Look again (checks nodes now)"), ("back", "Back")]]


def page_choices(cluster: Cluster) -> list[tuple[str, str]]:
    return [choice for group in page_groups(cluster) for choice in group]


def page_cards(cluster: Cluster, width: int, paint=None) -> list[tuple[str, str, str]]:
    """(action, label, card) for each choice. The card is the label, in the choice role where PAINT
    is given, then its second line (HINTS) two columns further in; lines are joined by the unit
    separator, as choice_label joins them, and a card that ends a group ends with one, which the
    menu draws as a blank line. A line too long for WIDTH wraps between words."""
    import textwrap
    found = []
    groups = page_groups(cluster)
    for index, group in enumerate(groups):
        for action, label in group:
            # Gum draws its cursor gutter before a card's first line only: later lines come at their
            # screen columns, a label's own two columns in and a second line two more.
            lines = textwrap.wrap(label, max(width - 2, 8), subsequent_indent="  ", break_long_words=False,
                                  break_on_hyphens=False)
            lines = [lines[0]] + ["  " + line for line in lines[1:]]
            lines = [paint(line) if paint else line for line in lines]
            if action in HINTS:
                lines += textwrap.wrap(HINTS[action], max(width, 10), initial_indent="    ",
                                       subsequent_indent="    ", break_long_words=False, break_on_hyphens=False)
            ends = action == group[-1][0] and index < len(groups) - 1
            found.append((action, label, "\x1f".join(lines) + ("\x1f" if ends else "")))
    return found


# ---------------------------------------------------------------------------
# Free up space…: what can go, and what cannot
# ---------------------------------------------------------------------------

def purge_facts(cluster: Cluster, spec: str) -> dict | None:
    """What Purge through SPEC would delete and what blocks it, from the Model files page's estimate,
    or None when SPEC has no copies and no unfinished copies recorded."""
    from model_library.file_inventory import _unfinished_blockers, purge_estimate
    bound = cluster.bound(spec)
    unfinished = cluster.unfinished(spec)
    if not bound and not unfinished:
        return None
    entries = list({id(entry): entry for entry, _ in bound}.values())
    freed: dict[str, int | None] = {}
    blockers = {"pinned_on": [], "in_use_on": [], "unknown_on": []}
    for entry in entries:
        estimate = purge_estimate(entry, spec, cluster.nodes, cluster.snapshots)
        for key in blockers:
            blockers[key] += estimate[key]
        for node, value in estimate["bytes_by_node"].items():
            previous = freed.get(node, 0)
            freed[node] = previous + value if type(value) is int and type(previous) is int else None
    if not entries:
        for key, values in _unfinished_blockers(cluster.nodes, spec).items():
            blockers[key] += values
        blockers["unknown_on"] += cluster.unread
    shared = {}
    for _, location in bound:
        others = {binding.get("spec_id") for binding in location.get("bindings") or []} - {spec}
        if others and location.get("kind") == "prepared":
            shared.setdefault(location["node_id"], set()).update(others)
    homes = [location["node_id"] for location in cluster.homes(spec).values() if location]
    return {"spec": spec, "freed": {node: freed[node] for node in cluster.ordered(freed)},
            "total": _sum(list(freed.values())) if freed else 0,
            "pinned_on": cluster.ordered(blockers["pinned_on"]), "in_use_on": cluster.ordered(blockers["in_use_on"]),
            "unknown_on": cluster.ordered(blockers["unknown_on"]), "unfinished": unfinished,
            "shared": shared, "homes": cluster.ordered(homes),
            "users": cluster.users([location for _, location in bound] + unfinished)}


def _users_text(cluster: Cluster, spec: str, users: list[dict]) -> list[tuple[str, tuple[str, str] | None]]:
    """Why containers keep SPEC's files, each with what lifts it: Stop through Running services…, or
    nothing Pulsar does for a container it did not start."""
    lines = []
    name = cluster.name(spec) if spec in cluster.catalog else spec[:12]
    own = [item["node"] for item in users if item["owner"] == spec]
    if own:
        lines.append((f"{name} is serving on {cluster.hosts(own)}, or its stopped containers remain there. "
                      "Stop it through Running services… first.", None))
    others = {}
    for item in users:
        if item["managed"] and item["owner"] != spec:
            # Services started outside this catalog are counted together, not repeated.
            key = item["owner"] if item["owner"] in cluster.catalog else None
            others.setdefault(key, {"owners": set(), "nodes": []})
            others[key]["owners"].add(item["owner"])
            others[key]["nodes"].append(item["node"])
    for owner, found in sorted(others.items(), key=lambda pair: pair[0] is None):
        count = len(found["owners"])
        if owner is not None:
            who, verb, it = cluster.name(owner), "uses", "it"
        else:
            who = ("a Pulsar service started outside this catalog" if count == 1 else
                   f"{count} Pulsar services started outside this catalog")
            verb, it = ("uses", "it") if count == 1 else ("use", "them")
        lines.append((f"{who} {verb} the files of {name} on {cluster.hosts(found['nodes'])}. "
                      f"Stop {it} through Running services… first.", None))
    for item in users:
        if not item["managed"]:
            lines.append((f"{item['name']}, a container Pulsar did not start, mounts the files of {name} on "
                          f"{cluster.host(item['node'])}. Pulsar never removes it (read-only).", None))
    return lines


def free_options(cluster: Cluster) -> tuple[list[dict], list[tuple[str, tuple[str, str] | None]]]:
    """(what can go, largest gain first; what cannot, each with the choice that lifts it or None).

    Each option is {target, label, size, text}. TARGET is what compose takes."""
    removable: list[dict] = []
    blocked: list[tuple[str, tuple[str, str] | None]] = []
    if cluster.unread:
        hosts = cluster.hosts(cluster.unread)
        verb = "does" if len(cluster.unread) == 1 else "do"
        blocked.append((f"{hosts} could not be checked, so the files and room there are unknown, not absent. "
                        f"Removing, moving or keeping files needs every node to answer, so nothing is offered "
                        f"until {hosts} {verb}; choose Look again.", None))
        return removable, blocked
    facts = {spec: purge_facts(cluster, spec) for spec in cluster.catalog}
    for spec, fact in facts.items():
        if fact is None:
            continue
        name = cluster.name(spec)
        if fact["in_use_on"]:
            blocked += _users_text(cluster, spec, fact["users"])
        if fact["pinned_on"]:
            blocked.append((f"{name} is kept on {cluster.hosts(fact['pinned_on'])}, so Free up space… does not "
                            "offer it.", ("unkeep:" + spec, f"Stop keeping {name}")))
        if fact["in_use_on"] or fact["pinned_on"]:
            continue
        if fact["unknown_on"]:
            # Purge needs every copy observed; a size without them would read as the whole.
            blocked.append((f"What removing {name} would free on {cluster.hosts(fact['unknown_on'])} is unknown: its "
                            "files there could not be fully checked, so nothing is offered for it. Choose Look "
                            "again.", None))
            continue
        unfinished = fact["unfinished"]
        if fact["freed"]:
            where = cluster.hosts(fact["freed"])
            label = f"Remove {name} from {where}: frees {_size(fact['total'])}"
            home = (f"Its home on {cluster.hosts(fact['homes'])} stays, so serving it again copies from there, with "
                    "no download." if fact["homes"] else
                    "It has no home on the cluster, so getting it back needs its archive or a download.")
            tail = (f" It also clears {_count(len(unfinished), 'unfinished copy', 'unfinished copies')} (size "
                    "unknown)." if unfinished else "")
            removable.append({"target": "purge:" + spec, "label": label, "size": fact["total"],
                              "text": f"{label}. {home}{tail}"})
        elif unfinished and not cluster.bound(spec):
            where = cluster.hosts(item["node_id"] for item in unfinished)
            label = (f"Clear {_count(len(unfinished), 'unfinished copy', 'unfinished copies')} of {name} on {where}: "
                     f"frees an unknown amount")
            them = ("It is a copy that never finished; Pulsar does not serve from it." if len(unfinished) == 1 else
                    "They are copies that never finished; Pulsar does not serve from them.")
            removable.append({"target": "clear:" + spec, "label": label, "size": None, "text": f"{label}. {them}"})
        if fact["shared"] and not fact["freed"]:
            nodes = list(fact["shared"])
            others = sorted({other for value in fact["shared"].values() for other in value})
            who = recipe_names(cluster, others)
            blocked.append((f"The copies of {name} on {cluster.hosts(nodes)} are shared with {who}; their files stay, "
                            "so removing them frees nothing.", None))
    removable += cluster_options(cluster, facts, blocked)
    removable += move_options(cluster)
    for item in [node for node in cluster.nodes]:
        loose = [entry for entry in item.get("unidentified") or []
                 if entry.get("kind") == "incomplete" and not entry.get("preparation_spec_id")]
        if loose:
            blocked.append((f"{_count(len(loose), 'unfinished download or copy', 'unfinished downloads or copies')} on "
                            f"{cluster.host(item['node_id'])} {'has' if len(loose) == 1 else 'have'} no record "
                            "naming a recipe. Pulsar never deletes files it has no record of.", None))
    loose = {item["id"]: item for item in loose_homes(cluster)}
    for entry in cluster.snapshots:
        if entry.get("catalog_specs") or not entry.get("locations"):
            continue
        where = cluster.hosts(location["node_id"] for location in entry["locations"])
        item = loose.get(entry["snapshot_manifest_id"])
        what = f"{_model(entry)} on {where} ({_size(entry.get('payload_bytes'))}): {STATUS_WORDS['unused']}"
        if item is None:
            # Copies with no home recorded: no command of this page selects them.
            blocked.append((f"{what}, and they have no home recorded, so this page does not remove them.", None))
        elif item["stays"] == NO_MANIFEST:
            blocked.append((f"{what}, and {NO_MANIFEST}, so this page does not remove them. {SAVE_MANIFEST}",
                            None))
        elif item["stays"]:
            blocked.append((f"{what}. They stay: {item['stays']}.", None))
        else:
            label = f"Remove {item['name']} from the cluster: frees {_size(item['size'])}"
            removable.append({"target": "files:" + item["id"], "label": label, "size": item["size"],
                              "text": f"{label}. {STATUS_WORDS['unused'][0].upper() + STATUS_WORDS['unused'][1:]}. "
                                      f"It deletes its home on {cluster.host(item['node'])}, the copy Pulsar keeps. "
                                      f"{files_archive_after(cluster, item, plan=False)}"})
    removable.sort(key=lambda option: (option["size"] is None, -(option["size"] or 0)))
    return removable, blocked


def cluster_set(cluster: Cluster, spec: str, facts: dict) -> tuple[list[str] | None, str | None]:
    """The recipes whose copies go before SPEC's homes can, or (None, why not)."""
    manifests = set(cluster.manifests(spec).values())
    dependents = set()
    for manifest in manifests:
        for location in (cluster.entry(manifest) or {}).get("locations") or []:
            dependents |= {binding.get("spec_id") for binding in location.get("bindings") or []}
    dependents.discard(None)
    for other in sorted(dependents):
        if other not in cluster.catalog:
            return None, "copies made for a recipe outside this catalog also use these files"
        if not set(cluster.manifests(other).values()) <= manifests:
            return None, f"{cluster.name(other)} also uses these files, with others"
        fact = facts.get(other)
        if fact and (fact["pinned_on"] or fact["in_use_on"] or fact["unknown_on"]):
            return None, None  # its own line says why
    return sorted(dependents), None


def cluster_options(cluster: Cluster, facts: dict, blocked: list) -> list[dict]:
    """Remove a model from the cluster: Purge every recipe whose copies use its files, then Remove home of
    each of its required snapshots, with its archive state said."""
    found, seen = [], set()
    for spec in cluster.catalog:
        homes = cluster.homes(spec)
        manifests = frozenset(cluster.manifests(spec).values())
        if not homes or not all(homes.values()) or manifests in seen:
            continue
        if any(location.get("state") != "present" for location in homes.values()):
            continue
        seen.add(manifests)
        users = cluster.users(list(homes.values()))
        if users or any(location.get("container_references") is None for location in homes.values()):
            if users:
                blocked += [line for line in _users_text(cluster, spec, users) if line not in blocked]
            continue
        purged, why = cluster_set(cluster, spec, facts)
        if purged is None:
            if why:
                blocked.append((f"The home of {cluster.name(spec)} stays: {why}.", None))
            continue
        recipes = sorted(set(purged) | {other for other in cluster.catalog
                                        if frozenset(cluster.manifests(other).values()) == manifests})
        name = cluster.model_name(recipes)
        sizes = [facts[other]["total"] if facts.get(other) else 0 for other in purged]
        # A home frees its listed size, as the Model files page sizes it.
        sizes += [cluster.snapshot_size(spec, snapshot) for snapshot in homes]
        total = _sum(sizes)
        # A copy these recipes share frees nothing through either one's estimate, though both Purges
        # together delete it: the figure is then a minimum, as the Model files page says of its own.
        least = any(facts[other]["shared"] for other in purged if facts.get(other))
        hosts = cluster.hosts(location["node_id"] for location in homes.values())
        label = f"Remove {name} from the cluster: frees {'at least ' if least and total else ''}{_size(total)}"
        deletes = least or any(facts[other]["freed"] for other in purged if facts.get(other))
        copies = " its copies, then" if deletes else ""
        found.append({"target": "cluster:" + spec, "label": label, "size": total,
                      "text": f"{label}. It deletes{copies} its home on {hosts}, the copy Pulsar keeps. "
                              f"{archive_after(cluster, spec, plan=False)}"})
    return found


def archive_after(cluster: Cluster, spec: str, *, plan: bool, archive_first: bool = False) -> str:
    """What getting SPEC back after it leaves the cluster takes: its archive, when every required
    snapshot has a verified one and the archive location is set, or a download."""
    archives = cluster.archives(spec)
    ages = [age for _, age in archives.values()]
    return _archive_after(cluster, all(kind == "verified" for kind, _ in archives.values()),
                          None if None in ages else max(ages), cluster.size(spec), plan=plan,
                          archive_first=archive_first)


def files_archive_after(cluster: Cluster, item: dict, *, plan: bool, archive_first: bool = False) -> str:
    """archive_after for a home no recipe uses (loose_homes): its one archive record decides."""
    return _archive_after(cluster, item["archived"], item["age"], item["size"], plan=plan,
                          archive_first=archive_first)


def _archive_after(cluster: Cluster, archived: bool, age, size, *, plan: bool, archive_first: bool) -> str:
    configured = cluster.archive_location == "configured"
    if archive_first:
        return "Its archive is made first and stays, so it can be restored without the internet."
    if archived:
        verified = f"verified {age_text(age)}" if age is not None else "verified (age unknown)"
        if configured:
            return (f"Its archive stays, {verified}, so it can be restored without the internet." if plan else
                    f"Its archive was {verified}, so it can be restored without the internet.")
        return (f"Its archive was {verified}, but the archive location is not set, so restoring it first needs "
                "Cluster… → Settings….")
    text = f"It has no verified archive, so getting it back means downloading it again ({_size(size)})."
    if plan:
        return text
    return text + (" Its plan offers to back it up first." if configured else
                   " Backing it up first needs the archive location; Cluster… → Settings… sets it.")


def short_nodes(cluster: Cluster) -> list[str]:
    """Nodes past the reserve of their storage budget: the measure the budget table names as short."""
    if not cluster.budget:
        return []
    return [node for node in cluster.ordered(cluster.budget)
            if (left := can_still_copy(cluster.budget[node])) is not None and left < 0]


def home_room(cluster: Cluster, node_id: str) -> int | None:
    """Free space where NODE_ID keeps homes, after its budget's reserve; None when not observed."""
    node = next((item for item in cluster.nodes if item["node_id"] == node_id), None)
    if node is None or node_id in cluster.unread:
        return None
    paths = {scope.get("path") for scope in node.get("scopes") or [] if scope.get("kind") == "home"}
    free = [disk.get("available") for disk in node.get("disks") or []
            if disk.get("path") in paths and type(disk.get("available")) is int]
    reserve = ((cluster.budget or {}).get(node_id) or {}).get("reserve")
    if not free or type(reserve) is not int:
        return None
    return min(free) - reserve


def move_options(cluster: Cluster) -> list[dict]:
    """Move a home off a node past its reserve to one with room for it: only a home no copy and no
    unfinished copy depends on, as Move home itself requires, and only to a node a recipe of it can
    keep its home on. A home no recipe in the catalog uses can go to any node with room, when its
    manifest file is saved (loose_homes)."""
    found = []
    for short in short_nodes(cluster):
        for entry in cluster.snapshots:
            home = next((location for location in entry.get("locations") or []
                         if location.get("registered_home") and location["node_id"] == short), None)
            if (home is None or home.get("state") != "present" or home.get("container_references") != []
                    or any(location.get("bindings") for location in entry.get("locations") or [])):
                continue
            size = entry.get("payload_bytes")
            if type(size) is not int:
                continue
            for node in cluster.ordered(item["node_id"] for item in cluster.nodes):
                room = home_room(cluster, node)
                if node == short or room is None or room < size:
                    continue
                if entry.get("catalog_specs"):
                    choice = move_spec(cluster, entry, node)
                    if choice is None:
                        continue
                    name, kind = cluster.name(choice[0]), "move"
                elif entry["snapshot_manifest_id"] in cluster.saved:
                    # No recipe limits where these files may be kept; the command takes their manifest.
                    name, kind = _model(entry), "move-files"
                else:
                    break
                label = (f"Move the files of {name} from {cluster.host(short)} to {cluster.host(node)}: frees "
                         f"{_size(size)} on {cluster.host(short)}")
                left = can_still_copy(cluster.budget[short])
                found.append({"target": f"{kind}:{entry['snapshot_manifest_id']}:{node}", "label": label, "size": size,
                              "text": f"{label}. {cluster.host(short)} is {_size(-left)} past its reserve and "
                                      f"{cluster.host(node)} has room. Its home, the copy Pulsar keeps, moves; "
                                      "nothing is lost."})
                break
    return found


def move_spec(cluster: Cluster, entry: dict, node: str) -> tuple[str, Any] | None:
    """A catalog recipe of ENTRY's files whose Move home may put them on NODE: any node for a one-node
    recipe, a serving node for a multi-node one (Move home refuses others)."""
    order = cluster.names.order
    for item in entry.get("catalog_specs") or []:
        spec = item.get("spec_id")
        row = cluster.catalog.get(spec)
        if row is None:
            continue
        nodes = row["geometry"]["nodes"]
        if nodes == 1 or (node in order and order.index(node) < nodes):
            name = item.get("snapshot")
            return spec, (None if not isinstance(row.get("snapshots"), dict) else name)
    return None


def move_destinations(cluster: Cluster, manifest: str) -> list[tuple[str, str]]:
    """(target, label) for each node that can take the home of MANIFEST's files: every node that
    answered but its own, where a recipe of the files may keep its home (move_spec; any node for
    files no recipe uses) and whose homes folder is not known to lack the room. TARGET is what
    compose takes; the command's own preview still decides."""
    entry = cluster.entry(manifest)
    home = next((location for location in (entry or {}).get("locations") or []
                 if location.get("registered_home")), None)
    if home is None:
        return []
    size, found = entry.get("payload_bytes"), []
    kind = "move" if entry.get("catalog_specs") else "move-files"
    for node in cluster.ordered(item["node_id"] for item in cluster.nodes):
        if node == home["node_id"] or node in cluster.unread:
            continue
        if kind == "move" and move_spec(cluster, entry, node) is None:
            continue
        room = home_room(cluster, node)
        if room is not None and type(size) is int and room < size:
            continue
        note = f"{_size(room)} free after its reserve" if room is not None else "room not observed"
        found.append((f"{kind}:{manifest}:{node}", f"{cluster.host(node)} ({note})".replace(KEEP, " ")))
    return found


def move_page_options(cluster: Cluster) -> tuple[list[tuple[str, str]], list[str]]:
    """(choices, lines) of Move a model to another node…: each home on the cluster, offered when
    Move home can run for it (nothing depends on it, as the command requires) and a node can take
    it, else with why it stays. A choice's target is `pick:MANIFEST`: the menu asks the node next."""
    choices, lines = [], []
    if cluster.unread:
        hosts = cluster.hosts(cluster.unread)
        lines.append(f"{hosts} could not be checked, and a move needs every node to answer, so nothing is offered. "
                     "Choose Look again on the page before this one.")
        return [("back", "Back")], lines
    for entry in cluster.snapshots:
        locations = entry.get("locations") or []
        home = next((location for location in locations if location.get("registered_home")), None)
        if home is None:
            continue
        specs = [item.get("spec_id") for item in entry.get("catalog_specs") or [] if item.get("spec_id") in cluster.catalog]
        name = cluster.model_name(specs) if specs else _model(entry)
        host, size = cluster.host(home["node_id"]), _size(entry.get("payload_bytes"))
        bound = cluster.ordered(location["node_id"] for location in locations if location.get("bindings"))
        users = cluster.users([home])
        if home.get("state") != "present":
            stays = "its files there " + ("were not found" if home.get("state") == "missing" else "could not be checked")
        elif bound:
            stays = (f"its copies on {cluster.hosts(bound)} depend on it, and a move never removes them. Free up "
                     "space… removes them first")
        elif users:
            stays = f"{_list([item['name'] for item in users])} mounts these files"
        elif home.get("container_references") != []:
            stays = "the containers there could not be checked"
        elif not entry.get("catalog_specs") and entry["snapshot_manifest_id"] not in cluster.saved:
            stays = f"{STATUS_WORDS['unused']}, and {NO_MANIFEST}. {SAVE_MANIFEST.rstrip('.')}"
        elif not move_destinations(cluster, entry["snapshot_manifest_id"]):
            stays = "no other node can take it: none has room for it, or none serves its recipe"
        else:
            stays = None
        if stays:
            lines.append(f"{name} ({size}) stays on {host}: {stays}.")
        else:
            lines.append(f"{name}: {size} on {host}.")
            choices.append(("pick:" + entry["snapshot_manifest_id"], f"Move {name} from {host}"))
    return choices + [("back", "Back")], lines


def free_screen(cluster: Cluster, *, width: int | None = None) -> str:
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    removable, blocked = free_options(cluster)
    out.emit("Free up space", role="heading")
    out.blank()
    if removable:
        out.emit("What can go, largest gain first:")
        for index, option in enumerate(removable, 1):
            mark = f"  {index}. "
            out.emit(option["text"], mark, " " * len(mark))
    else:
        out.emit("Nothing can go now.")
    if blocked:
        out.blank()
        out.emit("What cannot go now, and why:")
        for text, _ in blocked:
            out.emit(text, "  • ", "    ")
    out.blank()
    out.emit("Nothing that is kept stops being kept, no archive is deleted, and no file Pulsar has no record of is "
             "deleted.")
    return output.getvalue().replace(KEEP, " ")


def choice_label(label: str, width: int) -> str:
    """LABEL as a menu choice: one line when it fits WIDTH (the row after Gum's cursor), else a
    card whose lines are given at screen columns, joined by the unit separator for the menu to
    turn into newlines, as Running services… lists long names."""
    import textwrap
    if len(label) <= width - 2:
        return label
    lines = textwrap.wrap(label, width - 2, subsequent_indent="  ", break_long_words=False, break_on_hyphens=False)
    return "\x1f".join("  " + line for line in lines)


def free_choices(cluster: Cluster) -> list[tuple[str, str]]:
    removable, blocked = free_options(cluster)
    found = [(option["target"], option["label"].replace(KEEP, " ")) for option in removable]
    found += [choice for _, choice in blocked if choice and choice not in found]
    return found + [("back", "Back")]


# ---------------------------------------------------------------------------
# Keep a model on the cluster…, Back up a model to the archive…, Check the archive copies…
# ---------------------------------------------------------------------------

def keep_options(cluster: Cluster) -> tuple[list[tuple[str, str]], list[str]]:
    """(choices, lines): Keep … on the cluster for a recipe with copies not kept, Stop keeping … for one
    with kept copies; the lines say where each stands."""
    choices, lines = [], []
    for spec in cluster.catalog:
        if not cluster.on_cluster(spec):
            continue
        name = cluster.name(spec)
        pinned, loose = cluster.kept(spec)
        if not pinned and not loose:
            if cluster.bound(spec):
                lines.append(f"{name}: whether its copies are kept could not be read.")
            else:
                lines.append(f"{name} has no copies on its serving nodes, so there is nothing to keep yet; "
                             f"{READY_LABEL} makes them.")
            continue
        if loose:
            lines.append(f"{name}: {STATUS_WORDS['not-kept']} on {cluster.hosts(loose)}"
                         + (f"; {STATUS_WORDS['kept']} on {cluster.hosts(pinned)}." if pinned else "."))
            choices.append(("keep:" + spec, f"Keep {name} on the cluster"))
        else:
            lines.append(f"{name}: {STATUS_WORDS['kept']} on {cluster.hosts(pinned)}.")
        if pinned:
            choices.append(("unkeep:" + spec, f"Stop keeping {name}"))
    return choices + [("back", "Back")], lines


def backup_options(cluster: Cluster) -> tuple[list[tuple[str, str]], list[str]]:
    choices, lines = [], []
    for spec in cluster.catalog:
        homes = cluster.homes(spec)
        if not any(homes.values()):
            continue
        name = cluster.name(spec)
        archives = cluster.archives(spec)
        missing = [snapshot for snapshot, (kind, _) in archives.items() if kind != "verified"]
        lines.append(f"{name}: {cluster.archive_words(spec)}.")
        if missing:
            choices.append(("backup:" + spec, f"Back up {name}"))
    for item in loose_homes(cluster):
        line = f"{item['name']} ({STATUS_WORDS['unused']}): {_archive_words(item)}."
        if not item["archived"] and item["file"] and item["home"].get("state") == "present":
            choices.append(("backup-files:" + item["id"], f"Back up {item['name']}"))
        elif not item["archived"] and not item["file"]:
            line += f" This page cannot back it up: {NO_MANIFEST}. {SAVE_MANIFEST}"
        lines.append(line)
    return choices + [("back", "Back")], lines


def check_options(cluster: Cluster) -> tuple[list[tuple[str, str]], list[str]]:
    choices, lines = [], []
    for spec in cluster.catalog:
        archives = cluster.archives(spec)
        if not any(kind in ("verified", "present") for kind, _ in archives.values()):
            continue
        name = cluster.name(spec)
        lines.append(f"{name}: {cluster.archive_words(spec)}.")
        choices.append(("check:" + spec, f"Check the archives of {name}"))
    for item in loose_homes(cluster):
        if not item["archived"]:
            continue
        line = f"{item['name']} ({STATUS_WORDS['unused']}): {_archive_words(item)}."
        if item["file"]:
            choices.append(("check-files:" + item["id"], f"Check the archive of {item['name']}"))
        else:
            line += f" This page cannot check it: {NO_MANIFEST}. {SAVE_MANIFEST}"
        lines.append(line)
    return choices + [("back", "Back")], lines


INTENTS = {"keep": (KEEP_LABEL.rstrip("…"),
                    "Pulsar marks a model's copies as kept (pinned), so Free up space… never offers them and "
                    "Purge refuses them until you stop keeping it."),
           "backup": (BACKUP_LABEL.rstrip("…"),
                      "Pulsar copies every required set of a model's files from its home into the archive. An "
                      "archive already there is verified, never replaced."),
           "move": (MOVE_LABEL.rstrip("…"),
                    "Pulsar copies a model's home, the copy it keeps, to the node you choose, checks every file "
                    "there, then deletes it on the old node. Nothing is lost if the move stops."),
           "check": (CHECK_LABEL.rstrip("…"),
                     "Pulsar reads each archive of a model and compares it with its expected file hashes. "
                     "Nothing is changed.")}


INTENT_OPTIONS = {"keep": keep_options, "backup": backup_options, "check": check_options,
                  "move": move_page_options}


def intent_screen(cluster: Cluster, intent: str, *, width: int | None = None) -> str:
    if intent == "free":
        return free_screen(cluster, width=width)
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    title, explain = INTENTS[intent]
    _, lines = INTENT_OPTIONS[intent](cluster)
    out.emit(title, role="heading")
    out.blank()
    out.emit(explain)
    if cluster.unread and intent in ("keep", "backup"):
        them = "it" if len(cluster.unread) == 1 else "them"
        out.emit(f"{cluster.hosts(cluster.unread)} could not be checked, so a plan that needs {them} "
                 "stops at its own check.")
    out.blank()
    if not lines:
        out.emit({"keep": "No model has copies on the cluster to keep.",
                  "move": "No model has a home on the cluster to move.",
                  "backup": "No model has files on the cluster to back up.",
                  "check": f"No model has an archive recorded. {BACKUP_LABEL} makes one."}[intent])
    for line in lines:
        out.emit(line, "  ", "    ")
    return output.getvalue().replace(KEEP, " ")


# ---------------------------------------------------------------------------
# Record model files found on the nodes…
# ---------------------------------------------------------------------------

GET_BACK = f"{READY_LABEL} restores it from the archive or downloads it instead"


def _folder_recipe(cluster: Cluster, entry: dict, location: dict) -> tuple[str, Any] | None:
    """(spec, snapshot) of the catalog recipe whose prepared copy path is LOCATION's folder, or None.

    Prepare places a recipe's copy at a path made from the spec (a schema-2 spec's ID; for
    schema 3, a key of the spec, the node and the snapshot), so the folder's name says whose it is."""
    from model_library.state import view_key
    name = Path(str(location.get("hub_path") or "")).name
    for item in entry.get("catalog_specs") or []:
        spec = item.get("spec_id")
        row = cluster.catalog.get(spec)
        if row is None:
            continue
        expected = (view_key(spec, location["node_id"], entry["snapshot_manifest_id"])
                    if isinstance(row.get("snapshots"), dict) else spec)
        if name == expected:
            return spec, item.get("snapshot")
    return None



def find_items(cluster: Cluster) -> dict:
    """What the page's scan found that no record names, on the nodes that answered:

    register  one item per found home folder Download can record (its own reuse)
    adopt     one item per recipe whose prepared copies no record names, with their nodes
    cannot    (text) for each found folder neither can record, with why and what gets it back
    unknown   nodes that did not answer, whose files are unknown, not absent
    """
    register, adopt, cannot = [], {}, []
    order = cluster.names.order
    for entry in cluster.snapshots:
        manifest = entry.get("snapshot_manifest_id")
        found = [location for location in entry.get("locations") or []
                 if location.get("kind") == "home" and not location.get("registered_home")
                 and not location.get("bindings") and location.get("node_id") not in cluster.unread
                 and location.get("state") != "missing"]
        if not found:
            continue
        where = cluster.hosts(location["node_id"] for location in found)
        if not entry.get("catalog_specs"):
            cannot.append(f"{_model(entry)} on {where} ({_size(entry.get('payload_bytes'))}): "
                          f"{STATUS_WORDS['unused']}, so there is no recipe to record them for (read-only).")
            continue
        name = clean(entry.get("model_id") or "the model")
        recorded = [location for location in entry.get("locations") or [] if location.get("registered_home")]
        if recorded:
            cannot.append(f"Files of {name} on {where}: its home, the copy Pulsar keeps, is already on "
                          f"{cluster.hosts(location['node_id'] for location in recorded)}, and Pulsar records one "
                          "home, so it does not record these (read-only).")
            continue
        broken = [location for location in found if location.get("state") != "present"]
        if broken:
            reason = clean(broken[0].get("error") or "not every file is there at its expected size")
            cannot.append(f"Files of {name} on {cluster.hosts(item['node_id'] for item in broken)} could not be "
                          f"read as a complete copy ({reason}). {GET_BACK}.")
            continue
        if len(found) > 1:
            cannot.append(f"Files of {name} were found on {where}. Download records one home and stops when it "
                          "finds two, so neither is recorded; remove the one you do not need outside Pulsar, then "
                          "Look again.")
            continue
        location = found[0]
        choice = move_spec(cluster, entry, location["node_id"])
        if choice is None:
            cannot.append(f"Files of {name} on {cluster.host(location['node_id'])}: its recipes keep their files on "
                          f"their serving nodes, and this node serves none of them. {GET_BACK}.")
            continue
        spec, snapshot = choice
        register.append({"spec": spec, "snapshot": snapshot, "node": location["node_id"], "manifest": manifest,
                         "size": entry.get("payload_bytes"), "name": name})
    registered = {item["manifest"] for item in register}
    for entry in cluster.snapshots:
        for location in entry.get("locations") or []:
            if (location.get("kind") != "prepared" or location.get("bindings")
                    or location.get("node_id") in cluster.unread or location.get("state") == "missing"):
                continue
            node = location["node_id"]
            name = clean(entry.get("model_id") or "the model")
            if not location.get("bindings_observed", True):
                cannot.append(f"A copy of {name} on {cluster.host(node)}: the records there could not be read, so "
                              "whether Pulsar has a record of it is unknown. Choose Look again.")
                continue
            match = _folder_recipe(cluster, entry, location)
            if match is None:
                cannot.append(f"A copy of {_model(entry)} on {cluster.host(node)} that no record names was made for "
                              "a recipe outside this catalog, so nothing here can record it (read-only).")
                continue
            spec, _ = match
            recipe = cluster.name(spec)
            if location.get("state") != "present":
                reason = clean(location.get("error") or "not every file is there at its expected size")
                cannot.append(f"A copy of {recipe} on {cluster.host(node)} could not be read as a complete copy "
                              f"({reason}), so it cannot be taken over. Pulsar never deletes it.")
                continue
            row = cluster.catalog[spec]
            nodes = row["geometry"]["nodes"]
            if nodes > 1 and (node not in order or order.index(node) >= nodes):
                cannot.append(f"A copy of {recipe} on {cluster.host(node)}: this node does not serve that recipe, "
                              "so Prepare never looks there (read-only).")
                continue
            homes = cluster.homes(spec)
            missing = [snapshot for snapshot, home in homes.items() if not home
                       and cluster.manifests(spec).get(snapshot) not in registered]
            if missing:
                cannot.append(f"A copy of {recipe} on {cluster.host(node)}: the model has no home on the cluster, "
                              f"the copy Pulsar keeps, to record it against. {READY_LABEL} "
                              "restores or downloads it; then this choice can take the copy over.")
                continue
            key = (spec, None if nodes > 1 else node)
            item = adopt.setdefault(key, {"spec": spec, "nodes": [], "size": entry.get("payload_bytes"),
                                          "one_node": nodes == 1,
                                          "projected": any(not home for home in homes.values())})
            if node not in item["nodes"]:
                item["nodes"].append(node)
    for node in cluster.nodes:
        loose = [item for item in node.get("unidentified") or []
                 if item.get("kind") == "incomplete" and not item.get("preparation_spec_id")]
        if loose and node["node_id"] not in cluster.unread:
            # "Never finished" says it once; "unfinished" before it would say it twice.
            cannot.append(f"{_count(len(loose), 'download or copy', 'downloads or copies')} on "
                          f"{cluster.host(node['node_id'])} never finished, so there is nothing complete to record. "
                          "Pulsar never deletes files it has no record of.")
    return {"register": register, "adopt": list(adopt.values()), "cannot": cannot, "unknown": list(cluster.unread)}


def intent_choices(cluster: Cluster, intent: str) -> list[tuple[str, str]]:
    if intent == "free":
        return free_choices(cluster)
    return INTENT_OPTIONS[intent](cluster)[0]


# ---------------------------------------------------------------------------
# The plans
# ---------------------------------------------------------------------------

def _snapshot_args(row: dict, name) -> list[str]:
    return ["--snapshot", str(name)] if isinstance(row.get("snapshots"), dict) and name is not None else []


def _what(row: dict, name) -> str:
    """A required snapshot in the operator's words: the model files, or a model it also loads."""
    if name in (None, "target") or not isinstance(row.get("snapshots"), dict):
        return "the model files"
    model = clean((row["snapshots"].get(name) or {}).get("model_id") or name)
    return f"the files of {model} (a model it also loads)"


def _step(kind: str, sentence: str, command: list[str], preview: list[str] | None, facts: dict, *,
          authority: bool, now: bool = True, entries: list[dict] = (), summary: str | None = None,
          at_turn: bool = False) -> dict:
    """One step. SUMMARY is what the outcome calls it; by default the sentence up to its first stop.
    AT_TURN: its preview runs only when its turn comes (it is slow), though nothing before it changes it."""
    summary = summary or sentence.split(". ")[0].split(": ")[0].rstrip(".")
    return {"kind": kind, "sentence": sentence, "summary": summary, "command": command, "preview": preview,
            "facts": facts,
            "authority": authority, "preview_now": bool(preview) and now and not at_turn,
            "projected": bool(preview) and not now and not at_turn,
            "shown": shown(command, list(entries))}


def compose(cluster: Cluster, target: str, *, previews: dict | None = None, archive_first: bool = False) -> dict:
    """The plan for TARGET (an option of Free up space…, Keep, Back up or Check), from the scan and the
    previews given by name (the step's index as text)."""
    previews = previews or {}
    kind, _, rest = target.partition(":")
    plan = {"kind": PLAN_KIND, "target": target, "intent": kind, "title": "", "steps": [], "blocked": [],
            "notes": [], "footer": [], "choices": [], "done": "", "archive_first": archive_first}
    if cluster.unread and kind in ("purge", "cluster", "move", "clear", "keep", "unkeep", "files", "move-files"):
        plan["blocked"].append(f"{cluster.hosts(cluster.unread)} could not be checked, and this needs every node "
                               "to answer. Choose Look again once it does.")
    builder = {"purge": _purge_plan, "clear": _purge_plan, "cluster": _cluster_plan, "move": _move_plan,
               "keep": _pin_plan, "unkeep": _pin_plan, "backup": _backup_plan, "check": _check_plan,
               "find": _find_plan, "files": _files_plan, "move-files": _files_move_plan,
               "backup-files": _files_backup_plan, "check-files": _files_check_plan}.get(kind)
    if builder is None:
        raise ValueError(f"no plan for {target}")
    builder(cluster, plan, rest)
    # Fold in the previews that ran: each says whether its step still does what the plan says.
    for index, step in enumerate(list(plan["steps"])):
        name = str(index)
        if not step["preview_now"] or name not in previews:
            continue
        document, rc = previews[name]
        verdict, reason = assess(step, document, rc, cluster.names)
        if verdict == "skip":
            step["skip"] = reason
        elif verdict != "same":
            plan["blocked"].append(f"Step {index + 1} cannot run as planned: {reason}.")
        else:
            refine(step, document, cluster)
    kept = [step for step in plan["steps"] if not step.get("skip")]
    for step in plan["steps"]:
        if step.get("skip"):
            plan["notes"].append(f"Not needed: {step['sentence'].split('. ')[0].rstrip('.')}; {step['skip']}.")
    plan["steps"] = kept
    plan["projected"] = [index for index, step in enumerate(kept) if step["projected"]]
    return plan


def _spec(cluster: Cluster, spec: str) -> dict:
    if spec not in cluster.catalog:
        raise ValueError("that recipe is not in the catalog")
    return cluster.catalog[spec]


def _purge_plan(cluster: Cluster, plan: dict, spec: str) -> None:
    _spec(cluster, spec)
    fact = purge_facts(cluster, spec) or {"freed": {}, "total": 0, "unfinished": [], "homes": [],
                                          "pinned_on": [], "in_use_on": []}
    name = cluster.name(spec)
    clear = plan["intent"] == "clear"
    unfinished = fact["unfinished"]
    if clear:
        where = cluster.hosts(item["node_id"] for item in unfinished)
        plan["title"] = f"Clear unfinished copies of {name}"
        sentence = (f"Clear {_count(len(unfinished), 'unfinished copy', 'unfinished copies')} of {name} on {where} "
                    f"({STATUS_WORDS['size-unknown']}). Pulsar does not serve from a copy that never finished, and "
                    "Purge, which clears it, deletes nothing else for this recipe.")
        changes = f"deletes unfinished copies on {where}"
    else:
        where = cluster.hosts(fact["freed"])
        plan["title"] = f"Remove {name} from {where}"
        sentence = (f"Delete the copies of {name} on {where} ({_size(fact['total'])})"
                    + (f" and clear {_count(len(unfinished), 'unfinished copy', 'unfinished copies')}"
                       if unfinished else "") + ".")
        changes = f"deletes copies on {where}"
    plan["steps"].append(_step("purge", sentence, ["./pulsar", "model", "purge", spec, "--yes"],
                               ["./pulsar", "model", "purge", spec, "--plan", "--json"],
                               {"spec": spec, "nodes": list(fact["freed"]), "clear": clear, "changes": changes},
                               authority=True, entries=cluster.entries))
    if fact["pinned_on"]:
        plan["blocked"].append(f"{name} is kept on {cluster.hosts(fact['pinned_on'])}. Stop keeping it "
                               "first; this plan never does that on its own.")
    if fact.get("unknown_on") and not cluster.unread:
        plan["blocked"].append(f"Its files on {cluster.hosts(fact['unknown_on'])} could not be fully checked, so what "
                               "this would delete there is unknown. Choose Look again.")
    for text, _ in _users_text(cluster, spec, [item for item in fact.get("users") or []]):
        plan["blocked"].append(text)
    home = (f"Its home on {cluster.hosts(fact['homes'])}, the copy Pulsar keeps, stays, so serving it again copies "
            "from there, with no download." if fact["homes"] else
            "It has no home on the cluster, so serving it again needs its archive or a download.")
    plan["footer"] = [("Nothing else is deleted. " if clear else home + " ") + "Its archive is kept."]
    plan["done"] = (f"the unfinished copies of {name} are cleared" if clear else f"{name} is removed from {where}")


def _after_purges(cluster: Cluster, specs: list[str]) -> Cluster:
    """CLUSTER as the scan would read once Purge ran for SPECS: their bindings are gone, and so is
    each prepared copy only they used. A copy another recipe shared with them alone is then that
    recipe's own, which its Purge deletes. CLUSTER itself is not changed."""
    if not specs:
        return cluster
    snapshots = []
    for entry in cluster.snapshots:
        locations = []
        for location in entry.get("locations") or []:
            bindings = location.get("bindings") or []
            left = [binding for binding in bindings if binding.get("spec_id") not in specs]
            if bindings and not left and location.get("kind") == "prepared":
                continue
            locations.append({**location, "bindings": left})
        snapshots.append({**entry, "locations": locations})
    later = copy.copy(cluster)
    later.snapshots = snapshots
    later.by_manifest = {entry["snapshot_manifest_id"]: entry for entry in snapshots}
    return later


def _cluster_plan(cluster: Cluster, plan: dict, spec: str) -> None:
    row = _spec(cluster, spec)
    facts = {other: purge_facts(cluster, other) for other in cluster.catalog}
    purged, why = cluster_set(cluster, spec, facts)
    homes = cluster.homes(spec)
    manifests = frozenset(cluster.manifests(spec).values())
    recipes = sorted(set(purged or []) | {other for other in cluster.catalog
                                          if frozenset(cluster.manifests(other).values()) == manifests})
    name = cluster.model_name(recipes)
    plan["title"] = f"Remove {name} from the cluster"
    if purged is None:
        plan["blocked"].append(f"Its home cannot go: {why or 'a recipe that uses these files is kept or in use'}.")
        purged = []
    if not all(homes.values()):
        plan["blocked"].append("Not every set of its files has a home on the cluster.")
    archives = cluster.archives(spec)
    unarchived = [snapshot for snapshot, (kind, _) in archives.items() if kind != "verified"]
    if archive_first := plan["archive_first"]:
        if cluster.archive_location != "configured":
            plan["blocked"].append("Backing it up needs the archive location; Cluster… → Settings… sets it.")
        for snapshot in unarchived:
            location = homes.get(snapshot)
            if not location:
                continue
            size = _size(cluster.snapshot_size(spec, snapshot))
            args = _snapshot_args(row, snapshot)
            plan["steps"].append(_step(
                "archive", f"Copy {_what(row, snapshot)} from its home on {cluster.host(location['node_id'])} into the "
                           f"archive ({size}). An archive already there is verified, never replaced.",
                ["./pulsar", "model", "archive", "create", spec, *args, "--yes"],
                ["./pulsar", "model", "archive", "create", spec, *args, "--plan", "--json"],
                {"spec": spec, "snapshot": snapshot, "node": location["node_id"], "changes": "writes the archive"},
                authority=False, entries=cluster.entries))
    for index, other in enumerate(purged):
        # Each Purge finds the bindings the steps before it left. A copy two of these recipes share
        # is released by the first and deleted by the last, so the last one's step names that node.
        fact = purge_facts(_after_purges(cluster, purged[:index]), other)
        if not fact:
            continue
        where = cluster.hosts(fact["freed"]) if fact["freed"] else "its nodes"
        clear = (f" and clear {_count(len(fact['unfinished']), 'unfinished copy', 'unfinished copies')}"
                 if fact["unfinished"] else "")
        if fact["freed"]:
            sentence = f"Delete the copies of {cluster.name(other)} on {where} ({_size(fact['total'])}){clear}."
        else:
            # Its copies are the home or shared with another recipe here: Purge only lets go of them.
            sentence = (f"Release the copies of {cluster.name(other)}{clear}; their files stay for the steps after "
                        "it, and this step deletes none.")
        plan["steps"].append(_step(
            "purge", sentence,
            ["./pulsar", "model", "purge", other, "--yes"], ["./pulsar", "model", "purge", other, "--plan", "--json"],
            {"spec": other, "nodes": list(fact["freed"]), "clear": False, "changes": f"deletes copies on {where}"},
            authority=True, entries=cluster.entries))
    later = bool(plan["steps"])
    for snapshot, location in homes.items():
        if not location:
            continue
        size = _size(cluster.snapshot_size(spec, snapshot))
        args = _snapshot_args(row, snapshot)
        what = _what(row, snapshot)
        plan["steps"].append(_step(
            "remove", f"Delete the home of {what} on {cluster.host(location['node_id'])} ({size}), the copy Pulsar "
                      "downloaded or restored and keeps.",
            ["./pulsar", "model", "remove", spec, *args, "--yes"],
            ["./pulsar", "model", "remove", spec, *args, "--plan", "--json"],
            {"spec": spec, "snapshot": snapshot, "node": location["node_id"], "changes": "deletes the home"},
            authority=True, now=not later, entries=cluster.entries,
            summary=f"Delete the home of {what} on {cluster.host(location['node_id'])} ({size})"))
    plan["footer"].append(archive_after(cluster, spec, plan=True, archive_first=archive_first))
    if unarchived and not archive_first:
        if cluster.archive_location == "configured":
            plan["choices"].append(("archive-first", "Back it up first, then remove it"))
        else:
            plan["notes"].append("Backing it up first needs the archive location; Cluster… → Settings… sets it.")
    plan["footer"].append("No archive is deleted. If a step fails, the steps before it stay done and the plan "
                          "stops there.")
    plan["done"] = f"{name} is removed from the cluster"


def _move_plan(cluster: Cluster, plan: dict, rest: str) -> None:
    manifest, _, node = rest.partition(":")
    entry = cluster.entry(manifest)
    if entry is None:
        raise ValueError("those files are not in the scan")
    home = next((location for location in entry.get("locations") or [] if location.get("registered_home")), None)
    choice = move_spec(cluster, entry, node)
    if home is None or choice is None:
        raise ValueError("those files cannot be moved there")
    spec, snapshot = choice
    row = cluster.catalog[spec]
    name = cluster.name(spec)
    source, size = home["node_id"], _size(entry.get("payload_bytes"))
    plan["title"] = f"Move the files of {name} to {cluster.host(node)}"
    args = _snapshot_args(row, snapshot)
    plan["steps"].append(_step(
        "move", f"Move {_what(row, snapshot)} of {name} from {cluster.host(source)} to {cluster.host(node)} ({size}). "
                f"Pulsar copies them to {cluster.host(node)}, checks them there, then deletes them on "
                f"{cluster.host(source)}. They stay its home, the copy Pulsar keeps.",
        ["./pulsar", "model", "move", spec, *args, "--node", cluster.host(node), "--yes"],
        ["./pulsar", "model", "move", spec, *args, "--node", cluster.host(node), "--plan", "--json"],
        {"spec": spec, "snapshot": snapshot, "from": source, "to": node, "changes": f"frees {size} on "
         f"{cluster.host(source)}"}, authority=True, entries=cluster.entries))
    plan["footer"] = [f"Nothing is lost: the files on {cluster.host(source)} are deleted only after the copy on "
                      f"{cluster.host(node)} verifies. If the move stops, the files stay on {cluster.host(source)}."]
    plan["done"] = f"the files of {name} are on {cluster.host(node)}"


def _files_command(action: list[str], item: dict, *extra: str) -> tuple[list[str], list[str]]:
    """(command, preview) of ACTION for a home no recipe uses: its saved manifest file selects it."""
    command = ["./pulsar", "model", *action, "--manifest", item["file"], *extra]
    return [*command, "--yes"], [*command, "--plan", "--json"]


def _files_archive_step(cluster: Cluster, item: dict) -> dict:
    command, preview = _files_command(["archive", "create"], item)
    return _step("archive", f"Copy the files of {item['name']} from its home on {cluster.host(item['node'])} into "
                            f"the archive ({_size(item['size'])}). An archive already there is verified, never "
                            "replaced.", command, preview,
                 {"manifest": item["id"], "node": item["node"], "changes": "writes the archive"},
                 authority=False, entries=cluster.entries)


def _files_plan(cluster: Cluster, plan: dict, manifest: str) -> None:
    """Remove a home no recipe in the catalog uses, with its archive made first when asked."""
    item = loose_home(cluster, manifest)
    host, size = cluster.host(item["node"]), _size(item["size"])
    plan["title"] = f"Remove {item['name']} from the cluster"
    if item["stays"]:
        plan["blocked"].append(f"Its home cannot go: {item['stays']}.")
    archive_first = plan["archive_first"] and not item["archived"]
    if archive_first:
        if cluster.archive_location != "configured":
            plan["blocked"].append("Backing it up needs the archive location; Cluster… → Settings… sets it.")
        plan["steps"].append(_files_archive_step(cluster, item))
    command, preview = _files_command(["remove"], item)
    plan["steps"].append(_step(
        "remove", f"Delete the home of {item['name']} on {host} ({size}), the copy Pulsar downloaded or restored "
                  "and keeps.", command, preview,
        {"manifest": item["id"], "node": item["node"], "changes": "deletes the home"},
        authority=True, entries=cluster.entries, summary=f"Delete the home of {item['name']} on {host} ({size})"))
    plan["footer"].append(files_archive_after(cluster, item, plan=True, archive_first=archive_first))
    if not item["archived"] and not archive_first:
        if cluster.archive_location == "configured":
            plan["choices"].append(("archive-first", "Back it up first, then remove it"))
        else:
            plan["notes"].append("Backing it up first needs the archive location; Cluster… → Settings… sets it.")
    plan["footer"].append("No archive is deleted, and the saved manifest file stays. If a step fails, the steps "
                          "before it stay done and the plan stops there.")
    plan["done"] = f"{item['name']} is removed from the cluster"


def _files_move_plan(cluster: Cluster, plan: dict, rest: str) -> None:
    manifest, _, node = rest.partition(":")
    item = loose_home(cluster, manifest)
    source, size = item["node"], _size(item["size"])
    if not any(known["node_id"] == node for known in cluster.nodes) or node == source:
        raise ValueError("those files cannot be moved there")
    plan["title"] = f"Move the files of {item['name']} to {cluster.host(node)}"
    if item["stays"]:
        plan["blocked"].append(f"Its home cannot move: {item['stays']}.")
    command, preview = _files_command(["move"], item, "--node", cluster.host(node))
    plan["steps"].append(_step(
        "move", f"Move the files of {item['name']} from {cluster.host(source)} to {cluster.host(node)} ({size}). "
                f"Pulsar copies them to {cluster.host(node)}, checks them there, then deletes them on "
                f"{cluster.host(source)}. They stay its home, the copy Pulsar keeps.", command, preview,
        {"manifest": item["id"], "from": source, "to": node, "changes": f"frees {size} on {cluster.host(source)}"},
        authority=True, entries=cluster.entries))
    plan["footer"] = [f"Nothing is lost: the files on {cluster.host(source)} are deleted only after the copy on "
                      f"{cluster.host(node)} verifies. If the move stops, the files stay on {cluster.host(source)}."]
    plan["done"] = f"the files of {item['name']} are on {cluster.host(node)}"


def _files_backup_plan(cluster: Cluster, plan: dict, manifest: str) -> None:
    item = loose_home(cluster, manifest)
    plan["title"] = f"Back up {item['name']}"
    if cluster.archive_location != "configured":
        plan["blocked"].append("Backups need the archive location; Cluster… → Settings… sets it.")
    if item["archived"]:
        plan["notes"].append(f"Archived already; {_archive_words(item)}. {CHECK_LABEL} checks it again.")
    else:
        plan["steps"].append(_files_archive_step(cluster, item))
    plan["footer"] = ["Nothing on the nodes changes. No archive is replaced or deleted."]
    plan["done"] = f"{item['name']} is backed up"


def _files_check_plan(cluster: Cluster, plan: dict, manifest: str) -> None:
    item = loose_home(cluster, manifest)
    plan["title"] = f"Check the archive of {item['name']}"
    if cluster.archive_location != "configured":
        plan["blocked"].append("Checking archives needs the archive location; Cluster… → Settings… sets it.")
    if not item["archived"]:
        plan["notes"].append(f"No archive recorded; {BACKUP_LABEL} makes one.")
    else:
        plan["steps"].append(_step(
            "verify", f"Read the archive of {item['name']} ({_size(item['size'])}) and compare it with its expected "
                      "file hashes. This can take a while; nothing is changed.",
            ["./pulsar", "model", "archive", "verify", "--manifest", item["file"]], None,
            {"manifest": item["id"], "changes": "nothing"}, authority=False, entries=cluster.entries))
    plan["footer"] = ["Nothing is changed. If the archive does not match, Pulsar says so and the plan stops there."]
    plan["done"] = f"the archive of {item['name']} matched its expected file hashes"


def _pin_plan(cluster: Cluster, plan: dict, spec: str) -> None:
    _spec(cluster, spec)
    name = cluster.name(spec)
    pin = plan["intent"] == "keep"
    pinned, loose = cluster.kept(spec)
    nodes = loose if pin else pinned
    where = cluster.hosts(nodes) or "its serving nodes"
    if pin:
        plan["title"] = f"Keep {name} on the cluster"
        sentence = (f"Keep the copies of {name} on {where}: Pulsar marks them as kept (pins them), so Free up "
                    "space… never offers them and Purge refuses them until you stop keeping it. It checks them "
                    "first; nothing is copied or deleted.")
    else:
        plan["title"] = f"Stop keeping {name}"
        sentence = (f"Stop keeping the copies of {name} on {where}: Pulsar unmarks them (unpins them), so Free "
                    "up space… can offer to remove them. Nothing is deleted now.")
    verb = "pin" if pin else "unpin"
    plan["steps"].append(_step(verb, sentence, ["./pulsar", "model", verb, spec, "--yes"],
                               ["./pulsar", "model", verb, spec, "--plan", "--json"],
                               {"spec": spec, "nodes": nodes, "changes": f"marks copies on {where}"},
                               authority=False, entries=cluster.entries))
    if not pinned and not loose:
        plan["blocked"].append(f"{name} has no copies on its serving nodes to keep; {READY_LABEL} "
                               "makes them.")
    plan["footer"] = ["Nothing is copied or deleted."]
    plan["done"] = f"{name} is kept on the cluster" if pin else f"{name} is no longer kept"


def _backup_plan(cluster: Cluster, plan: dict, spec: str) -> None:
    row = _spec(cluster, spec)
    name = cluster.name(spec)
    plan["title"] = f"Back up {name}"
    if cluster.archive_location != "configured":
        plan["blocked"].append("Backups need the archive location; Cluster… → Settings… sets it.")
    homes = cluster.homes(spec)
    for snapshot, (kind, age) in cluster.archives(spec).items():
        what = _what(row, snapshot)
        if kind == "verified":
            plan["notes"].append(f"{what[0].upper() + what[1:]}: archived already, verified "
                                 f"{age_text(age) if age is not None else '(age unknown)'}; {CHECK_LABEL} checks "
                                 "it again.")
            continue
        location = homes.get(snapshot)
        if not location:
            plan["blocked"].append(f"{what[0].upper() + what[1:]}: no home on the cluster to copy from, so this "
                                   "model cannot be backed up whole.")
            continue
        size = _size(cluster.snapshot_size(spec, snapshot))
        args = _snapshot_args(row, snapshot)
        plan["steps"].append(_step(
            "archive", f"Copy {what} from its home on {cluster.host(location['node_id'])} into the archive "
                       f"({size}). An archive already there is verified, never replaced.",
            ["./pulsar", "model", "archive", "create", spec, *args, "--yes"],
            ["./pulsar", "model", "archive", "create", spec, *args, "--plan", "--json"],
            {"spec": spec, "snapshot": snapshot, "node": location["node_id"], "changes": "writes the archive"},
            authority=False, entries=cluster.entries))
    plan["footer"] = ["Nothing on the nodes changes. No archive is replaced or deleted. If a step fails, the steps "
                      "before it stay done and the plan stops there."]
    plan["done"] = f"{name} is backed up"


def _check_plan(cluster: Cluster, plan: dict, spec: str) -> None:
    row = _spec(cluster, spec)
    name = cluster.name(spec)
    plan["title"] = f"Check the archives of {name}"
    if cluster.archive_location != "configured":
        plan["blocked"].append("Checking archives needs the archive location; Cluster… → Settings… sets it.")
    for snapshot, (kind, _) in cluster.archives(spec).items():
        what = _what(row, snapshot)
        if kind not in ("verified", "present"):
            plan["notes"].append(f"{what[0].upper() + what[1:]}: no archive recorded; {BACKUP_LABEL} makes one.")
            continue
        size = _size(cluster.snapshot_size(spec, snapshot))
        args = _snapshot_args(row, snapshot)
        plan["steps"].append(_step(
            "verify", f"Read the archive of {what} ({size}) and compare it with its expected file hashes. This can take "
                      "a while; nothing is changed.",
            ["./pulsar", "model", "archive", "verify", spec, *args], None,
            {"spec": spec, "snapshot": snapshot, "changes": "nothing"}, authority=False, entries=cluster.entries))
    plan["footer"] = ["Nothing is changed. If an archive does not match, Pulsar says so and the plan stops there."]
    plan["done"] = f"every archive of {name} matched its expected file hashes"


def _find_plan(cluster: Cluster, plan: dict, _: str) -> None:
    """Record what the scan found that no record names: each home folder through Download's own reuse,
    then each recipe's prepared copies through Prepare's --adopt-unrecorded, the only place the menu
    passes it (D7). What neither can record is listed with why and what gets the model back."""
    items = find_items(cluster)
    plan["title"] = FIND_LABEL.rstrip("…")
    plan["cannot"] = items["cannot"]
    if items["unknown"]:
        hosts = cluster.hosts(items["unknown"])
        plan["unknown"] = (f"{hosts} could not be checked, so the files there are unknown, not absent, and nothing "
                           f"is offered there. Choose Look again once {'it answers' if len(items['unknown']) == 1 else 'they answer'}.")
    for item in items["register"]:
        row = cluster.catalog[item["spec"]]
        host = cluster.host(item["node"])
        args = _snapshot_args(row, item["snapshot"])
        command = ["./pulsar", "model", "acquire", item["spec"], *args, "--node", host]
        # Download's own preview hashes every file of a folder no record names: it runs at the step's turn.
        plan["steps"].append(_step(
            "register", f"Record the files of {item['name']} found on {host} ({_size(item['size'])}) as its home, "
                        "the copy Pulsar keeps. Download checks them first; nothing is downloaded.",
            [*command, "--yes"], [*command, "--plan", "--json"],
            {"spec": item["spec"], "snapshot": item["snapshot"], "node": item["node"],
             "changes": f"records the files on {host}"},
            authority=False, at_turn=True, entries=cluster.entries,
            summary=f"Record the files of {item['name']} found on {host}"))
    for item in items["adopt"]:
        spec = item["spec"]
        name = cluster.name(spec)
        where = cluster.hosts(item["nodes"])
        copies = "the copy" if len(item["nodes"]) == 1 else "the copies"
        placement = ["--node", cluster.host(item["nodes"][0])] if item["one_node"] else []
        command = ["./pulsar", "model", "prepare", spec, *placement, "--adopt-unrecorded"]
        # Prepare has no preview before the model's home is recorded: an estimate until the steps before it ran.
        later = item["projected"]
        plan["steps"].append(_step(
            "adopt", f"Take over {copies} of {name} found on {where} after checking every file "
                     f"({_size(item['size'])}{' each' if len(item['nodes']) > 1 else ''}). Pulsar records "
                     f"{'it' if len(item['nodes']) == 1 else 'each'} only if every file matches; nothing is copied "
                     "or deleted.",
            [*command, "--yes"], [*command, "--plan", "--json"],
            {"spec": spec, "nodes": list(item["nodes"]), "copy_to": [] if later else None,
             "changes": f"records copies on {where}"},
            authority=False, now=not later, entries=cluster.entries,
            summary=f"Take over {copies} of {name} found on {where}"))
    plan["footer"] = ["Every file is hashed when no record names the folder, so each step can take a while. "
                      "Nothing is downloaded, deleted or moved, and a folder whose files do not match stays as it is, "
                      "unrecorded. Each step checks first when its turn comes; if it is blocked or would do more than "
                      "it says, Pulsar stops and asks again. If a step fails, the steps before it stay done and the "
                      "plan stops there."]
    plan["done"] = "the files found are recorded"


# ---------------------------------------------------------------------------
# A step's own preview
# ---------------------------------------------------------------------------

def assess(step: dict, preview: Any, rc: int = 0, names: NodeNames | None = None) -> tuple[str, str]:
    """Whether a step's own preview still does what the plan says: ("same" | "skip" | "differs" |
    "blocked", reason)."""
    names = names or NodeNames()
    document, error = envelope_result(preview)
    kind, facts = step["kind"], step.get("facts") or {}
    if rc != 0 or document is None:
        return "blocked", clean((error or {}).get("message") or (f"its preview exited {rc}" if rc else
                                                                 "its preview was unreadable"))
    action = {"purge": "purge", "remove": "remove", "move": "move", "pin": "pin", "unpin": "unpin",
              "archive": "archive"}.get(kind)
    if action and plan_blocked(document):
        lines = plan_summary(action, {"model_id": "it"}, document, names=names)
        return "blocked", " ".join(line.removeprefix("Blocked: ").rstrip(".") for line in lines)
    inner = _plan_document(document)
    if facts.get("manifest") and kind in ("remove", "move", "archive") \
            and document.get("snapshot_manifest_id") != facts["manifest"]:
        # The file selects the files; it was read again for this preview and may have changed.
        return "differs", "its manifest file names other files now than when the plan was made"
    if kind == "purge":
        if inner.get("kind") != "pulsar-purge-plan":
            return "blocked", "Purge's preview did not return its plan"
        effect = plan_effect("purge", document)
        if effect["nothing"]:
            return "skip", "nothing is recorded for it any more"
        removed = {str(action["node_id"]) for action in _plan_actions(document) if action.get("action") == "remove-copy"}
        if facts.get("clear") and _plan_actions(document):
            return "differs", "it would remove copies too, not only unfinished ones"
        extra = sorted(removed - set(facts.get("nodes") or []))
        if extra:
            return "differs", f"it would also delete copies on {_list([names(node) for node in extra])}"
        return "same", ""
    if kind == "remove":
        if document.get("kind") != "pulsar-home-removal-plan":
            return "blocked", "Remove home's preview did not return its plan"
        if (document.get("home") or {}).get("node_id") != facts.get("node"):
            return "differs", f"the home is on {names((document.get('home') or {}).get('node_id'))} now"
        return "same", ""
    if kind == "move":
        if document.get("kind") != "pulsar-home-move-plan":
            return "blocked", "Move home's preview did not return its plan"
        if document.get("transfer_route") == "already-home":
            return "skip", f"the files are already on {names(facts.get('to'))}"
        if document.get("source_node") != facts.get("from") or document.get("destination_node") != facts.get("to"):
            return "differs", "it would move the files between other nodes than the plan said"
        return "same", ""
    if kind in ("pin", "unpin"):
        if document.get("kind") != "pulsar-retention-plan":
            return "blocked", "its preview did not return its plan"
        effect = plan_effect(kind, document)
        if effect["nothing"]:
            return "skip", "its copies are already " + ("kept" if kind == "pin" else "not kept")
        return "same", ""
    if kind == "archive":
        if document.get("kind") != "pulsar-archive-plan":
            return "blocked", "the archive preview did not return its plan"
        if (document.get("home") or {}).get("node_id") != facts.get("node"):
            return "differs", f"it would copy from {names((document.get('home') or {}).get('node_id'))}"
        return "same", ""
    if kind == "register":
        # The step holds only if Download's own plan reuses the files found: it never downloads.
        if document.get("kind") != "pulsar-acquisition-plan":
            return "blocked", "Download's preview did not return its plan"
        if document.get("action") != "reuse":
            return "differs", (f"the files found on {names(facts.get('node'))} do not match the recipe, so Download "
                               "would fetch them from Hugging Face; this plan never downloads")
        node = (document.get("home") or {}).get("node_id")
        if node != facts.get("node"):
            return "differs", f"Download would record the files on {names(node)}, not {names(facts.get('node'))}"
        return "same", ""
    if kind == "adopt":
        if plan_blocked(document):
            lines = plan_summary("prepare", {"model_id": "it"}, document, names=names)
            return "blocked", " ".join(line.removeprefix("Blocked: ").rstrip(".") for line in lines)
        inner = _plan_document(document)
        if inner.get("kind") not in ("pulsar-preparation-plan", "pulsar-preparation-set-plan"):
            return "blocked", "Prepare's preview did not return its plan"
        actions = _plan_actions(document)
        adopted = {str(action["node_id"]) for action in actions if action.get("action") == "adopt"}
        copied = {str(action["node_id"]) for action in actions if action.get("action") == "copy"}
        wanted = set(facts.get("nodes") or [])
        if not adopted:
            return "skip", "no copy is left that no record names"
        extra = sorted(adopted - wanted)
        if extra:
            return "differs", f"it would also take over a copy on {_list([names(node) for node in extra])}"
        if facts.get("copy_to") is not None:
            more = sorted(copied - set(facts["copy_to"]))
            if more:
                return "differs", (f"it would also copy the files to {_list([names(node) for node in more])}, "
                                   "which the plan did not say")
        return "same", ""
    return "same", ""


def refine(step: dict, preview: Any, cluster: Cluster) -> None:
    """Put the size a step's own preview gives in its sentence, where the estimate had none."""
    document, _ = envelope_result(preview)
    if step["kind"] == "purge" and isinstance(document, dict):
        size = plan_effect("purge", document).get("removed_bytes")
        if type(size) is int and STATUS_WORDS["size-unknown"] in step["sentence"]:
            step["sentence"] = step["sentence"].replace(STATUS_WORDS["size-unknown"], _size(size), 1)
    if step["kind"] == "adopt" and isinstance(document, dict) and step["facts"].get("copy_to") is None:
        # Prepare acts on every serving node at once: a node with neither files nor a copy to take over
        # gets a copy in the same run. The plan says so, so the run never copies what it did not say.
        copied = cluster.ordered(str(action["node_id"]) for action in _plan_actions(document)
                                 if action.get("action") == "copy")
        step["facts"]["copy_to"] = copied
        if copied:
            where = cluster.hosts(copied)
            step["sentence"] = step["sentence"].replace("; nothing is copied or deleted.", "; nothing is deleted.")
            step["sentence"] += (f" Prepare also copies the files to {where}, which has no copy, since every "
                                 "serving node needs them before any copy is recorded." if len(copied) == 1 else
                                 f" Prepare also copies the files to {where}, which have no copy, since every "
                                 "serving node needs them before any copy is recorded.")
            step["facts"]["changes"] += f" and writes a copy on {where}"


# ---------------------------------------------------------------------------
# The plan screen and its outcome
# ---------------------------------------------------------------------------

def render(plan: dict, *, width: int | None = None) -> str:
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    out.emit(plan["title"], role="heading")
    out.blank()
    steps = plan["steps"]
    if plan.get("unknown"):
        out.emit(plan["unknown"], "Not checked: ", "  ", prefix_role="warn")
        out.blank()
    if not steps and plan["intent"] == "find":
        # Only what the nodes that answered hold: never that a model has no files.
        out.emit("Nothing was found that Pulsar can record" + (" on the nodes that answered." if plan.get("unknown")
                                                              else "."))
    elif not steps:
        out.emit("Nothing to do: " + ("; ".join(note.rstrip(".") for note in plan["notes"]) or "nothing is left to change")
                 + ".")
    else:
        out.emit("Pulsar will:")
        for index, step in enumerate(steps, 1):
            mark = f"  {index}. "
            out.emit(step["sentence"], mark, " " * len(mark))
        out.blank()
        for line in plan["footer"]:
            out.emit(line)
        projected = [index + 1 for index in plan.get("projected") or []]
        if projected:
            # Record model files' footer already says that each step checks first.
            checks = ("." if plan["intent"] == "find" else ": each step checks first, and Pulsar stops and asks "
                      "again if one is blocked or would do more than it says.")
            out.emit(f"{'Step' if len(projected) == 1 else 'Steps'} {_list([str(n) for n in projected])} "
                     f"{'is an estimate' if len(projected) == 1 else 'are estimates'} until the steps before "
                     f"{'it run' if len(projected) == 1 else 'them run'}{checks}")
        for note in plan["notes"]:
            out.emit(note)
    if plan.get("cannot"):
        out.blank()
        out.emit("Found, but Pulsar cannot record it here:")
        for text in plan["cannot"]:
            out.emit(text, "  • ", "    ")
    if plan["blocked"]:
        out.blank()
        for reason in plan["blocked"]:
            out.emit(reason, "Blocked: ", "  ", prefix_role="fail")
    return output.getvalue().replace(KEEP, " ")


def plan_choices(plan: dict) -> list[tuple[str, str]]:
    """Cancel first (D1), then Go ahead only when something runs and nothing blocks."""
    found = [("cancel", "Cancel")]
    if plan["steps"] and not plan["blocked"]:
        found.append(("go", "Go ahead"))
    return found + [choice for choice in plan.get("choices") or [] if not plan["blocked"]]


def outcome(plan: dict, *, done: int, failed: int | None = None, rc: int = 0, ran: list[str] = (),
            halted: str | None = None, width: int | None = None) -> str:
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    steps = plan["steps"]
    if halted is not None and failed is not None:
        out.emit(f"Step {failed + 1} did not run: {halted}. Pulsar stopped before it, so it can ask again.",
                 "✗ ", "  ", prefix_role="fail")
        out.emit(f"Done: {_count(done, 'step')} before it." if done else "Nothing was changed.")
    elif failed is None:
        out.emit(f"Done: {plan['done']}.", "✓ ", "  ")
    else:
        what = steps[failed].get("summary") or steps[failed]["sentence"].split(". ")[0]
        if rc in (3, 130) or 128 < rc < 160:
            out.emit(f"Step {failed + 1} was interrupted: {what}. Its outcome is not confirmed; this page looks at "
                     "the files again before it offers anything.", "✗ ", "  ", prefix_role="fail")
        elif rc == 2:
            out.emit(f"Step {failed + 1} was refused before it changed anything: {what}.", "✗ ", "  ",
                     prefix_role="fail")
        else:
            out.emit(f"Step {failed + 1} did not finish: {what}. The reason is above.", "✗ ", "  ",
                     prefix_role="fail")
        out.emit(f"Done: {_count(done, 'step')} before it." if done else "No step before it ran.")
    if ran:
        out.blank()
        out.emit("Same as:")
        for command in ran:
            out.emit_command(command, "  ", "    ")
    return output.getvalue().replace(KEEP, " ")


# ---------------------------------------------------------------------------

def _entries(stream) -> list[dict]:
    try:
        return json.load(stream).get("entries") or []
    except (ValueError, AttributeError):
        raise ValueError("the catalog document could not be read")


def _cluster(args, entries: list[dict]) -> Cluster:
    files = files_document(args.files)
    if files is None:
        raise ValueError("the model files scan could not be read")
    location = args.archive_location
    if location is None:
        from scripts.serve_plan import _archive_location
        location = _archive_location(args.repo_root)
    names = NodeNames.saved(args.repo_root)
    ring = {}
    if names.ring:
        from scripts.serve_plan import _saved_topology, _topology_id, ring_pairs
        ring = {"pairs": ring_pairs(_saved_topology(args.repo_root)), "topology_id": _topology_id(args.repo_root)}
    return Cluster(entries, files, budget_nodes(args.budget), names=names, archive_location=location,
                   saved=saved_manifests(args.repo_root), **ring)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    readers = []
    for name in ("page", "choices"):
        readers.append(sub.add_parser(name))
    for name in ("screen", "options"):
        command = sub.add_parser(name)
        command.add_argument("intent", choices=("free", "keep", "backup", "check", "move"))
        readers.append(command)
    destinations = sub.add_parser("destinations")
    destinations.add_argument("--manifest", required=True)
    readers.append(destinations)
    for name in ("previews", "compose"):
        command = sub.add_parser(name)
        command.add_argument("--target", required=True)
        command.add_argument("--archive-first", action="store_true")
        command.add_argument("--preview", action="append", default=[], help="INDEX=FILE: a step's preview output")
        command.add_argument("--preview-rc", action="append", default=[], help="INDEX=RC: its exit status")
        command.add_argument("--out")
        readers.append(command)
    for command in readers:
        command.add_argument("--files", required=True)
        command.add_argument("--budget")
        command.add_argument("--catalog", help="the catalog list JSON; standard input when omitted")
        command.add_argument("--archive-location", choices=sorted(ARCHIVE_LOCATION))
    for name in ("render", "steps", "choices-of"):
        sub.add_parser(name).add_argument("--plan", required=True)
    command_ = sub.add_parser("command")
    command_.add_argument("--plan", required=True)
    command_.add_argument("--step", type=int, required=True)
    command_.add_argument("--preview", action="store_true")
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
    result.add_argument("--ran-step", type=int, action="append", default=[])
    result.add_argument("--halted")
    for command in sub.choices.values():
        command.add_argument("--repo-root", type=Path, default=ROOT)
        command.add_argument("--width", type=int)
    args = parser.parse_args(argv)
    width = max(MIN_WIDTH, min(MAX_WIDTH, args.width or terminal_width()))
    try:
        if args.command in ("page", "choices", "screen", "options", "previews", "compose", "destinations"):
            if args.catalog:
                with open(args.catalog, encoding="utf-8") as stream:
                    entries = _entries(stream)
            else:
                entries = _entries(sys.stdin)
            cluster = _cluster(args, entries)
            if args.command == "page":
                sys.stdout.write(page(cluster, width=width))
            elif args.command == "choices":
                # ACTION, the label alone, then its card. Only the card's label carries styles
                # (PULSAR_COLOR=always in menus), and the menu passes it to Gum without parsing it.
                writer = TerminalWriter(width=width, stream=sys.stdout)
                for action, label, card in page_cards(cluster, width,
                                                      lambda line: writer.paint(line, ["choice"] * len(line))):
                    print(f"{action}\t{label}\t{card}")
            elif args.command == "destinations":
                for target, label in move_destinations(cluster, args.manifest):
                    print(f"{target}\t{label}")
            elif args.command == "screen":
                sys.stdout.write(intent_screen(cluster, args.intent, width=width))
            elif args.command == "options":
                for action, label in intent_choices(cluster, args.intent):
                    print(f"{action}\t{choice_label(label, width)}")
            else:
                loaded, codes = {}, {}
                for value in args.preview_rc:
                    name, _, rc = value.partition("=")
                    codes[name] = int(rc)
                for value in args.preview:
                    name, separator, path = value.partition("=")
                    if not separator:
                        raise ValueError("previews must use INDEX=FILE")
                    loaded[name] = (load_json(path), codes.get(name, 0))
                plan = compose(cluster, args.target, previews=loaded, archive_first=args.archive_first)
                if args.command == "previews":
                    # INDEX, a tab, then the preview's arguments, each after a tab.
                    for index, step in enumerate(compose(cluster, args.target,
                                                         archive_first=args.archive_first)["steps"]):
                        if step["preview_now"]:
                            print("\t".join([str(index), *step["preview"]]))
                    return 0
                if not args.out:
                    raise ValueError("compose needs --out")
                Path(args.out).write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
            return 0
        plan = load_json(args.plan)
        if not isinstance(plan, dict) or plan.get("kind") != PLAN_KIND:
            raise ValueError("the plan could not be read")
        if args.command == "render":
            sys.stdout.write(render(plan, width=width))
        elif args.command == "steps":
            # INDEX, KIND, whether it has a preview, and its sentence.
            for index, step in enumerate(plan["steps"]):
                print(f"{index}\t{step['kind']}\t{int(bool(step.get('preview')))}\t"
                      f"{clean(step['sentence'].replace(KEEP, ' '))}")
        elif args.command == "choices-of":
            for action, label in plan_choices(plan):
                print(f"{action}\t{label}")
        elif args.command == "command":
            step = plan["steps"][args.step]
            command = step["preview"] if args.preview else step["command"]
            if not command:
                raise ValueError("that step has no preview command")
            print("\n".join(command))
        elif args.command == "check-step":
            verdict, reason = assess(plan["steps"][args.step], load_json(args.preview_file), args.preview_rc,
                                     NodeNames.saved(args.repo_root))
            print(f"{verdict}\t{clean(reason)}")
        elif args.command == "outcome":
            ran = [plan["steps"][index].get("shown") or " ".join(plan["steps"][index]["command"])
                   for index in args.ran_step]
            sys.stdout.write(outcome(plan, done=args.done, failed=args.failed, rc=args.rc, ran=ran,
                                     halted=args.halted, width=width))
        return 0
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        from scripts.terminal_format import error_line
        error_line(exc, tag="models and disk space")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
