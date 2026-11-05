"""Model-file placement across confirmed nodes, with read-only discovery.

Node discovery lives in file_discovery; this module joins saved identities and
current observations, renders human output and serves the interactive overview.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from release_spec import load_spec
from release_spec.serving import required_snapshots
from .integrity import StorageError, verify_manifest
from .state import Store, now, validate_home, validate_view


def context(repo: Path, store: Store, overlay: Path) -> dict:
    """Read controller records, catalog association and explicitly configured roots."""
    from scripts.release_consumer import load_overlay
    result = {"homes": [], "views": [], "manifests": {}, "archives": {}, "specs": {},
              "home_roots": [], "issues": []}
    for namespace in ("homes", "views", "manifests", "archives"):
        try:
            values = store.records(namespace)
            for value in values:
                if namespace == "homes":
                    result[namespace].append(validate_home(value))
                elif namespace == "views":
                    result[namespace].append(validate_view(value))
                elif namespace == "manifests":
                    manifest = verify_manifest(value)
                    result[namespace][manifest["manifest_id"]] = manifest
                else:
                    result[namespace][value["snapshot_manifest_id"]] = value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["issues"].append("Saved " + namespace + " could not be fully read: " + str(exc))
    for path in sorted((repo / "releases").glob("*.json")):
        try:
            spec = load_spec(path)
            if path.name != spec["spec_id"] + ".json":
                raise StorageError("catalog filename differs from spec identity")
            for name, member in required_snapshots(spec).items():
                manifest = member["snapshot_manifest"]
                key = manifest["manifest_id"]
                result["manifests"][key] = manifest
                result["specs"].setdefault(key, []).append({"spec_id": spec["spec_id"],
                                                           "model_id": member["model_id"], "snapshot": name})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["issues"].append("Catalog association could not be read: " + str(exc))
    try:
        document = load_overlay(overlay)
        result["home_roots"] = sorted({value["cache_root"] for value in
                                      [document["defaults"], *document["specs"].values()] if value.get("cache_root")})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["issues"].append("Additional configured home roots could not be read: " + str(exc))
    return result


def node_request(saved: dict, node_id: str) -> dict:
    return {"homes": [row for row in saved["homes"] if row["node_id"] == node_id],
            "views": [row for row in saved["views"] if row["node_id"] == node_id],
            "home_roots": saved["home_roots"]}


def combine(saved: dict, observations: list[dict]) -> dict:
    """Keep successful nodes even when another node or Docker is unobservable."""
    from .planning import _covering_mount
    nodes = []
    rows = {}
    def row(key):
        if key not in rows:
            manifest = saved["manifests"].get(key, {})
            rows[key] = {"snapshot_manifest_id": key, "model_id": manifest.get("model_id", "Unknown model"),
                         "model_commit": manifest.get("snapshot_revision"), "payload_bytes": manifest.get("total_bytes"),
                         "locations": [], "catalog_specs": saved["specs"].get(key, []),
                         "archive": saved["archives"].get(key), "cleanup_spec_ids": []}
        return rows[key]
    known = {}
    for home in saved["homes"]:
        item = {"kind": "home", "hub_path": home["hub_path"], "path": home["path"], "state": "unknown",
                "snapshot_manifest_id": home["snapshot_manifest_id"], "registered_home": True,
                "verified_at": home["verified_at"], "bindings": []}
        known[(home["node_id"], home["hub_path"])] = item
    for view in saved["views"]:
        key = (view["node_id"], view["hub_path"])
        item = known.setdefault(key, {"kind": "home" if view["is_home_view"] else "prepared",
                                     "hub_path": view["hub_path"], "path": view["path"], "state": "unknown",
                                     "snapshot_manifest_id": view["snapshot_manifest_id"],
                                     "registered_home": False, "verified_at": None, "bindings": []})
        item["bindings"].append(view)
    observed_ids = {item["node_id"] for item in observations}
    for node_id, _ in known:
        if node_id not in observed_ids:
            observations.append({"node_id": node_id, "hostname": node_id,
                                 "error": "outside confirmed membership", "containers": None})
            observed_ids.add(node_id)
    for observation in observations:
        node_id = observation["node_id"]
        files = observation.get("files")
        node = {"node_id": node_id, "hostname": observation["hostname"], "observed_at": (files or {}).get("observed_at"),
                "status": "observed" if files is not None else "unknown",
                "error": observation.get("error"), "scopes": (files or {}).get("scopes", []),
                "disks": (files or {}).get("disks", []), "issues": (files or {}).get("issues", []),
                "unidentified": (files or {}).get("unidentified", []),
                "container_error": observation.get("container_error")}
        nodes.append(node)
        found = {path: item for (identity, path), item in known.items() if identity == node_id}
        for item in (files or {}).get("locations", []):
            found[item["hub_path"]] = item
        for location in found.values():
            entry = row(location["snapshot_manifest_id"])
            manifest = location.get("manifest")
            if manifest:
                entry.update(model_id=manifest["model_id"], model_commit=manifest["snapshot_revision"],
                             payload_bytes=manifest["total_bytes"])
            bindings = location.get("bindings", [])
            containers = observation.get("containers")
            references = None
            if containers is not None:
                references = [container for container in containers if _covering_mount(
                    {"hub_path": location["hub_path"], "path": location.get("path", location["hub_path"])},
                    container["mounts"]) is not None]
            item = {**location, "node_id": node_id, "container_references": references,
                    "bindings_observed": bool(files and files.get("bindings_observed")),
                    "pinned": any(binding["pinned"] for binding in bindings),
                    "shared": len({binding["spec_id"] for binding in bindings}) > 1}
            entry["locations"].append(item)
            catalog_ids = {spec["spec_id"] for spec in entry["catalog_specs"]}
            candidates = catalog_ids if item.get("registered_home") else {
                binding["spec_id"] for binding in bindings} & catalog_ids
            entry["cleanup_spec_ids"] = sorted(set(entry["cleanup_spec_ids"]) | candidates)
    return {"schema_version": 1, "kind": "pulsar-model-file-inventory", "observed_at": now(),
            "nodes": nodes, "snapshots": sorted(rows.values(), key=lambda item: (item["model_id"], item["model_commit"] or "")),
            "issues": saved["issues"]}


# Words the page prints for a model's files. Tables use words when they fit and short codes
# when they do not; both share these keys. docs/OPERATIONS.md#catalog-labels explains each
# one, and tests/test_help.py fails when a printable word has no row there.
FOLDER_STATES = {"observed": "checked", "not-found": "none on this node",
                 "partial": "partly checked", "unknown": "could not be checked"}
CELL_WORDS = {"home": "home", "copy": "copy", "home missing": "home missing", "copy missing": "copy missing",
              "home unknown": "home unknown", "copy unknown": "copy unknown", "shared": "shared",
              "pinned": "pinned", "in use": "in use", "pins unknown": "pins unknown",
              "use unknown": "use unknown", "none": "—", "node unknown": "?"}
CELL_CODES = {"home": "H", "copy": "P", "home missing": "H missing", "copy missing": "P missing",
              "home unknown": "H?", "copy unknown": "P?", "shared": "shared", "pinned": "pin",
              "in use": "used", "pins unknown": "pins?", "use unknown": "use?", "none": "—",
              "node unknown": "?"}
# What the key under a table says about a cell word, shown only for words on screen.
CELL_HINTS = {"home": "home", "copy": "copy", "home missing": "home not found", "copy missing": "copy not found",
              "home unknown": "home could not be checked", "copy unknown": "copy could not be checked",
              "none": "none on this node", "node unknown": "the node could not be fully checked",
              "shared": "more than one spec uses it", "pinned": "kept from Purge",
              "in use": "a container mounts it, even stopped",
              "pins unknown": "pins could not be read on that node",
              "use unknown": "containers could not be read on that node"}
# Words that explain themselves, so only the codes table needs a key for them.
SELF_EXPLAINING = ("home", "copy", "home missing", "copy missing", "home unknown", "copy unknown")
# Detail lines have room, so they use the longer words.
PLACEMENT_WORDS = {"home": "home", "copy": "prepared copy", "missing": "not found", "unknown": "unknown",
                   "none": "none in Pulsar's folders on this node", "node unknown": "unknown",
                   "pins unknown": "pins unknown", "use unknown": "container use unknown",
                   "in use": "in use by a container", "shared saved": "shared (saved record)",
                   "pinned saved": "pinned (saved record)", "unregistered": "not in Pulsar's records"}
UNFINISHED = ("a download or preparation that has not finished: still running, or interrupted. "
              "Pulsar doesn't serve from it, and no Pulsar command removes it.")
UNFINISHED_PREPARATION = ("a preparation for {spec} that has not finished: still running, or interrupted. "
                          "Pulsar doesn't serve from it; Purge on that spec removes it.")
FOOTER = ("Size per copy is the model's listed size. Shared copies are stored once, so it is not what "
          "deleting would free.")
NODE_LABEL_WIDTH = 16
# The words a snapshot page uses for what Purge through one spec would free;
# docs/OPERATIONS.md#catalog-labels has the "Model files: would free" row.
WOULD_FREE = "would free"


def purge_estimate(entry: dict, spec_id: str, nodes: list[dict]) -> dict:
    """What Purge through SPEC_ID would delete from this snapshot, from the observed locations.

    bytes_by_node counts the listed size of each present prepared copy that only this spec uses,
    with observed pins and no container mounting it: the copies Purge deletes. A home used as a
    prepared copy and a shared copy are only released, so they free nothing. Purge refuses while
    any of the spec's copies is pinned or mounted, so pinned_on and in_use_on name those nodes;
    unknown_on names nodes where pins, container use or the files could not be read.
    """
    order = [node["node_id"] for node in nodes]
    found = {"bytes_by_node": {}, "pinned_on": [], "in_use_on": [], "unknown_on": []}
    size = entry.get("payload_bytes")
    for location in entry["locations"]:
        bindings = location.get("bindings") or []
        own = [binding for binding in bindings if binding.get("spec_id") == spec_id]
        if not own:
            continue
        node = location["node_id"]
        references = location.get("container_references")
        if any(binding.get("pinned") for binding in own):
            found["pinned_on"].append(node)
        if references:
            found["in_use_on"].append(node)
        if not location.get("bindings_observed") or references is None or location.get("state") == "unknown":
            found["unknown_on"].append(node)
            continue
        if (location["kind"] == "prepared" and location.get("state") == "present" and not references
                and not any(binding.get("pinned") for binding in own)
                and {binding.get("spec_id") for binding in bindings} == {spec_id}):
            previous = found["bytes_by_node"].get(node, 0)
            found["bytes_by_node"][node] = (previous + size if type(size) is int and previous is not None
                                            else None)
    def ordered(values):
        return sorted(dict.fromkeys(values), key=lambda node: order.index(node) if node in order else len(order))
    for key in ("pinned_on", "in_use_on", "unknown_on"):
        found[key] = ordered(found[key])
    found["bytes_by_node"] = {node: found["bytes_by_node"][node] for node in ordered(found["bytes_by_node"])}
    return found


def _freed(estimate: dict) -> int | None:
    """The total Purge would free when it can run and every node was read; else None."""
    if estimate["pinned_on"] or estimate["in_use_on"] or estimate["unknown_on"]:
        return None
    values = list(estimate["bytes_by_node"].values())
    return sum(values) if all(type(value) is int for value in values) else None


def purge_line(entry: dict, spec_id: str, nodes: list[dict]) -> str:
    """One sentence on what Purge through SPEC_ID would free from this snapshot, or why it would not run."""
    from .render import short_size
    hostnames = {node["node_id"]: node["hostname"] for node in nodes}
    def named(identities):
        return ", ".join(hostnames.get(node, node) for node in identities)
    estimate = purge_estimate(entry, spec_id, nodes)
    subject = "Purge via [" + spec_id[:12] + "]"
    # Purge refuses the whole spec while any copy is mounted or pinned: say so, and the choice that clears it.
    causes = [(word + named(estimate[key]), fix) for key, word, fix in
              (("in_use_on", "in use on ", "Stop"), ("pinned_on", "pinned on ", "Unpin prepared copies"))
              if estimate[key]]
    if causes:
        fixes = [fix for _, fix in causes]
        return (subject + " is blocked: " + "; ".join(cause for cause, _ in causes) + ". Choose " + fixes[0]
                + " first" + "".join(", then " + fix for fix in fixes[1:]) + ".")
    freeing = estimate["bytes_by_node"]
    values = list(freeing.values())
    total = sum(values) if values and all(type(value) is int for value in values) else None
    amount = (short_size(total) + (" in all" if len(freeing) > 1 else "")) if total is not None else "an unknown amount"
    if estimate["unknown_on"]:
        unknown = "not known on " + named(estimate["unknown_on"]) + ", which could not be fully checked"
        if freeing:
            return subject + " " + WOULD_FREE + " at least " + amount + " on " + named(freeing) + "; " + unknown + "."
        return subject + ": what it " + WOULD_FREE + " is " + unknown + "."
    if freeing:
        return subject + " " + WOULD_FREE + " " + amount + " on " + named(freeing) + "."
    if not any(binding.get("spec_id") == spec_id for location in entry["locations"]
               for binding in location.get("bindings") or []):
        return subject + ": no prepared copies of this snapshot are recorded for it."
    return (subject + " " + WOULD_FREE + " no space: it only releases the home or copies other specs share, "
            "and their files stay.")


def _words(location: dict) -> str:
    kind = PLACEMENT_WORDS["home" if location["kind"] == "home" else "copy"]
    state = location["state"]
    parts = [kind if state == "present" else kind + " " + PLACEMENT_WORDS["missing" if state == "missing" else "unknown"]]
    observed = location.get("bindings_observed")
    if not observed:
        parts.append(PLACEMENT_WORDS["pins unknown"])
    if location.get("shared"):
        parts.append(CELL_WORDS["shared"] if observed else PLACEMENT_WORDS["shared saved"])
    if location.get("pinned"):
        parts.append(CELL_WORDS["pinned"] if observed else PLACEMENT_WORDS["pinned saved"])
    references = location.get("container_references")
    if references is None:
        parts.append(PLACEMENT_WORDS["use unknown"])
    elif references:
        parts.append(PLACEMENT_WORDS["in use"])
    if not location.get("registered_home") and not location.get("bindings"):
        parts.append(PLACEMENT_WORDS["unregistered"])
    return ", ".join(parts)


def _cell_keys(entry: dict, node: dict) -> list[str]:
    """The CELL_WORDS keys for a model's files on one node, in reading order."""
    locations = [item for item in entry["locations"] if item["node_id"] == node["node_id"]]
    if not locations:
        unknown = (node["status"] == "unknown" or node["issues"]
                   or any(scope["status"] in ("unknown", "partial") for scope in node["scopes"]))
        return ["node unknown" if unknown else "none"]
    keys = []
    for item in locations:
        kind = "home" if item["kind"] == "home" else "copy"
        keys.append(kind if item["state"] == "present" else
                    kind + (" missing" if item["state"] == "missing" else " unknown"))
    for field in ("pinned", "shared"):
        if any(item.get(field) and item.get("bindings_observed") for item in locations):
            keys.append(field)
    if any(not item.get("bindings_observed") for item in locations):
        keys.append("pins unknown")
    if any(item.get("container_references") for item in locations):
        keys.append("in use")
    elif any(item.get("container_references") is None for item in locations):
        keys.append("use unknown")
    return keys


def placement(entry: dict, node: dict, *, compact: bool = False, codes: bool = False) -> str:
    """A model's files on one node: a table cell when COMPACT (short codes when CODES), else detail words."""
    if compact:
        words = CELL_CODES if codes else CELL_WORDS
        return (" " if codes else ", ").join(words[key] for key in _cell_keys(entry, node))
    locations = [item for item in entry["locations"] if item["node_id"] == node["node_id"]]
    if not locations:
        return PLACEMENT_WORDS[_cell_keys(entry, node)[0]]
    return "; ".join(_words(item) for item in locations)


def _labels(entries: list[dict]) -> list[str]:
    """Row labels without the org, which every row of a table shares in practice; a label keeps it
    when another row would otherwise read the same."""
    def label(entry, named):
        return (entry["model_id"] if named else entry["model_id"].rpartition("/")[2]) \
            + " @" + (entry["model_commit"] or "unknown")[:8]
    short = [label(entry, False) for entry in entries]
    return [label(entry, True) if short.count(text) > 1 else text for entry, text in zip(entries, short)]


def _elide(label: str, width: int) -> str:
    """Cut the middle of a name, keeping its end and the commit: rows differ at the end."""
    if len(label) <= width:
        return label
    name, at, commit = label.rpartition(" @")
    room = width - len(at + commit) - 1
    head = room // 2
    return name[:head] + "…" + name[len(name) - (room - head):] + at + commit


def _key(keys: list[str], codes: bool) -> str:
    """One line saying what the words, or the codes, on screen mean."""
    words = CELL_CODES if codes else CELL_WORDS
    entries = []
    for key in CELL_HINTS:
        if key in keys and (codes or key not in SELF_EXPLAINING):
            word = words[key]
            entries.append(word + (" " if word in ("H", "P", "—", "?") else ": ") + CELL_HINTS[key])
    return ("; " if codes else " · ").join(entries)


def _table(entries: list[dict], nodes: list[dict], width: int) -> tuple[list[str], str]:
    """The model-by-node table and its key: words when they fit, short codes when they do not,
    and ([], "") when even the codes leave the first column under 16 columns."""
    from .render import short_size
    if not entries:
        return [], ""
    labels = _labels(entries)
    headers = ["Model snapshot", "Size per copy", *[node["hostname"] for node in nodes]]
    for codes in (False, True):
        cells = [[short_size(entry["payload_bytes"]), *[placement(entry, node, compact=True, codes=codes)
                                                         for node in nodes]] for entry in entries]
        rows = [headers, *[[label, *row] for label, row in zip(labels, cells)]]
        widths = [max(len(row[column]) for row in rows) for column in range(len(headers))]
        room = width - sum(widths[1:]) - 2 * (len(widths) - 1)
        if room < min(widths[0], 16):
            continue
        widths[0] = min(widths[0], room)
        table = ["  ".join([_elide(row[0], widths[0]).ljust(widths[0]), *[cell.ljust(widths[column])
                                                                         for column, cell in enumerate(row[1:], 1)]]).rstrip()
                 for row in rows]
        keys = [key for entry in entries for node in nodes for key in _cell_keys(entry, node)]
        return table, _key(keys, codes)
    return [], ""


def _free_space(node: dict) -> list[str]:
    """One line per distinct free-space figure, naming what lives on that disk."""
    from .render import short_size
    kinds = {"home": "homes", "prepared": "prepared copies"}
    groups, errors = {}, []
    for disk in node["disks"]:
        if disk.get("error"):
            errors.append("could not be checked at " + disk["path"] + ": " + disk["error"])
        else:
            groups.setdefault((disk.get("available"), disk.get("total")), []).append(disk["path"])
    named = []
    for (available, _), paths in groups.items():
        found = {scope["kind"] for scope in node["scopes"] if scope["path"] in paths}
        named.append((short_size(available), " and ".join(name for kind, name in kinds.items() if kind in found), paths))
    # Two disks holding the same kind of folder keep their paths, so each figure names its disk.
    repeated = {name for _, name, _ in named if name and [other for _, other, _ in named].count(name) > 1}
    lines = [size + (" (" + name + ")" if name else "") + (" at " + ", ".join(paths) if not name or name in repeated else "")
             for size, name, paths in named]
    return [*lines, *errors]


def _budget(out, budget: tuple[list | None, str | None], nodes: list[dict]) -> None:
    """The storage budget table from model budget, or why it was not observed."""
    from .render import budget_table
    budgets, reason = budget
    out.blank()
    if budgets is None:
        out.field("Storage budget", "not observed; " + (reason or "model budget returned no result"))
        return
    out.emit("Storage budget", role="heading")
    if not budgets:
        out.emit("No node reported a storage budget.")
        return
    hostnames = {node["node_id"]: node["hostname"] for node in nodes}
    lines = budget_table(budgets, lambda node_id: hostnames.get(node_id, str(node_id)), out.width)
    # The table rows stay aligned; the notes after them wrap.
    for index, text in enumerate(lines[:len(budgets) + 1]):
        out.emit(text, role="heading" if index == 0 else None)
    for text in lines[len(budgets) + 1:]:
        out.emit(text, subsequent_indent="  ")
    out.emit("Observed prepared-copy storage only; space is not reserved.")


def render(document: dict, *, writer=None, snapshot: str | None = None, menu: bool = False,
           budget: tuple[list | None, str | None] | None = None) -> None:
    from scripts.terminal_format import TerminalWriter
    from .render import short_size
    from .catalog import age_seconds, age_text
    def observed(value):
        return (value + " (" + age_text(age_seconds(value)) + ")") if value else "not observed"
    out = writer or TerminalWriter()
    nodes = document["nodes"]
    out.emit("Model files and disk space", role="heading")
    count = str(len(nodes)) + (" node" if len(nodes) == 1 else " nodes")
    out.field("Checked", (age_text(age_seconds(document["observed_at"])) + " (" + count + ")")
              if document["observed_at"] else "not observed")
    out.emit("Names and sizes were compared, not file contents. Archives are not looked at here.")
    if menu and snapshot is None:
        out.emit("Close this page, then choose a snapshot for its paths and cleanup choices.")
    for issue in document.get("issues", []):
        out.field("Warning", issue, label_role="warn")
    if snapshot is None:
        def line(label, value, **style):
            out.field(label, value, label_width=NODE_LABEL_WIDTH, **style)
        for node in nodes:
            out.blank()
            out.emit(node["hostname"], role="heading")
            if node.get("error"):
                line("Files", "could not be checked: " + node["error"])
            for text in _free_space(node):
                line("Free space", text)
            for scope in node["scopes"]:
                line("Homes folder" if scope["kind"] == "home" else "Copies folder",
                     FOLDER_STATES.get(scope["status"], scope["status"]) + " — " + scope["path"])
                if scope.get("error"):
                    line("Not observed", scope["error"])
            for issue in node["issues"]:
                line("Warning", issue, label_role="warn")
            if node.get("container_error"):
                line("Container use", "unknown: " + node["container_error"])
            for item in node["unidentified"]:
                if item["kind"] == "incomplete":
                    spec = item.get("preparation_spec_id")
                    line("Unfinished copy", item["path"] + " — "
                         + (UNFINISHED_PREPARATION.format(spec=spec) if spec else UNFINISHED))
                else:
                    line("Not recognized", item["path"] + " — " + item["reason"])
        if budget is not None:
            _budget(out, budget, nodes)
    entries = [entry for entry in document["snapshots"] if snapshot is None or entry["snapshot_manifest_id"] == snapshot]
    if not entries:
        out.blank()
        out.emit("No model files were found in Pulsar's folders or saved records.")
    table, key = _table(entries, nodes, out.width) if snapshot is None else ([], "")
    if table:
        out.blank()
        for index, text in enumerate(table):
            out.emit(text, role="heading" if index == 0 else None)
        out.emit(key)
        out.emit(FOOTER)
        return
    # One label column for an entry's fields, wide enough for its longest label.
    width = max([len("Archive location") if snapshot is not None else len("Size per copy"),
                 *(len(node["hostname"]) for node in nodes)]) + 1
    for entry in entries:
        out.blank()
        out.emit(entry["model_id"] + " @ " + (entry["model_commit"] or "unknown")[:12], role="heading")
        out.field("Snapshot", entry["snapshot_manifest_id"][:12], label_width=width)
        out.field("Size per copy", short_size(entry["payload_bytes"]), label_width=width)
        for node in nodes:
            locations = [location for location in entry["locations"] if location["node_id"] == node["node_id"]]
            out.field(node["hostname"], placement(entry, node), label_width=width)
            if snapshot is not None:
                out.field("Files checked", observed(node.get("observed_at")), indent=2)
                for location in locations:
                    out.field("Path", location["hub_path"], indent=2)
                    times = {binding.get("verified_at") for binding in location.get("bindings", [])}
                    times.add(location.get("verified_at"))
                    times.discard(None)
                    if times:
                        out.field("Saved verification", ", ".join(sorted(times)) + " (not reverified now)", indent=2)
                    if location.get("error"):
                        out.field("Not observed", location["error"], indent=2)
        archive = entry.get("archive")
        out.field("Archive", ("saved verification " + str(archive.get("verified_at") or "time unknown")
                             + "; not checked now") if archive else "no saved archive record; not checked now",
                  label_width=width)
        if snapshot is not None and archive and archive.get("root"):
            out.field("Archive location", archive["root"], label_width=width)
        if snapshot is not None and entry["cleanup_spec_ids"]:
            out.emit("Cleanup opens a spec's own menu: Purge removes its prepared copies, and Remove home "
                     "removes this snapshot's home.")
            for spec_id in sorted(entry["cleanup_spec_ids"]):
                out.emit(purge_line(entry, spec_id, nodes), subsequent_indent="  ")
        if not entry["cleanup_spec_ids"]:
            out.emit("No catalog spec uses these files, so Pulsar has no cleanup for them. Nothing was changed.")
    out.blank()
    purge_shown = snapshot is not None and any(entry["cleanup_spec_ids"] for entry in entries)
    out.emit(FOOTER + (" The Purge lines above say what it would free." if purge_shown else ""))


def read_inventory(path: Path) -> dict:
    document = json.loads(path.read_text())
    if document.get("ok") is True:
        document = document["result"]
    if document.get("kind") != "pulsar-model-file-inventory":
        raise StorageError((document.get("error") or {}).get("message") or "model files returned no inventory")
    return document


def read_budget(path: Path) -> tuple[list | None, str | None]:
    """A model budget --json result's nodes, or why it gave none: the JSON error, else the last
    line of its stderr saved beside it as NAME.err, as the cluster overview keeps its sources."""
    try:
        document = json.loads(path.read_text() or "null")
    except (OSError, ValueError):
        document = None
    if isinstance(document, dict):
        result = document.get("result") if document.get("ok") is True else document
        if (isinstance(result, dict) and result.get("kind") == "pulsar-storage-budget"
                and isinstance(result.get("nodes"), list)):
            return result["nodes"], None
        error = document.get("error")
        if isinstance(error, dict) and error.get("message"):
            return None, str(error["message"])
    try:
        lines = path.with_suffix(".err").read_text(errors="replace").strip().splitlines()
    except OSError:
        lines = []
    return None, lines[-1].removeprefix("error: ") if lines else "model budget returned no result"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("context", "request", "combine", "render", "choices", "recipes"))
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--node-id")
    parser.add_argument("--snapshot")
    parser.add_argument("--menu", action="store_true", help="name the menu choice that opens a snapshot")
    parser.add_argument("--budget", type=Path, help="model budget --json output to show under the nodes")
    args = parser.parse_args(argv)
    try:
        if args.command == "context":
            print(json.dumps(context(args.repo_root, Store(args.state_root), args.overlay)))
        elif args.command == "request":
            print(json.dumps(node_request(json.loads(args.context.read_text()), args.node_id)))
        elif args.command == "combine":
            print(json.dumps(combine(json.loads(args.context.read_text()),
                                     [json.loads(line) for line in args.observations.read_text().splitlines()])))
        else:
            document = read_inventory(args.file)
            if args.command == "render":
                render(document, snapshot=args.snapshot, menu=args.menu,
                       budget=read_budget(args.budget) if args.budget else None)
            elif args.command == "choices":
                from scripts.terminal_format import terminal_width
                from .render import short_size
                for entry in document["snapshots"]:
                    label = entry["model_id"] + " @ " + (entry["model_commit"] or "unknown")[:8]
                    if not entry["cleanup_spec_ids"]:
                        label += " — view only"
                    elif len(entry["cleanup_spec_ids"]) == 1:
                        # Several specs each need their own Purge, so only one spec's total is shown.
                        freed = _freed(purge_estimate(entry, entry["cleanup_spec_ids"][0], document["nodes"]))
                        suffix = " — frees " + short_size(freed) if freed else ""
                        # A cut size would misread, so the suffix shows whole or not at all.
                        if len(" ".join((label + suffix).split())) <= terminal_width() - 2:
                            label += suffix
                    label = " ".join(label.split())
                    print(entry["snapshot_manifest_id"] + "\t" + label[:terminal_width() - 2])
            else:
                for entry in document["snapshots"]:
                    if entry["snapshot_manifest_id"] == args.snapshot:
                        for spec in sorted(entry["cleanup_spec_ids"]):
                            members = [value["snapshot"] for value in entry["catalog_specs"] if value["spec_id"] == spec]
                            print(spec + "\t" + "Spec [" + spec[:12] + "] — " + ", ".join(members))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        from scripts.terminal_format import error_line
        error_line(exc, tag="model files")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
