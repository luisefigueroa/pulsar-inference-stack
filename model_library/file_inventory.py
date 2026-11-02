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


def _words(location: dict) -> str:
    kind = "home" if location["kind"] == "home" else "prepared copy"
    state = location["state"]
    parts = [kind if state == "present" else kind + " " + ("not found" if state == "missing" else "unknown")]
    if not location.get("bindings_observed"):
        parts.append("retention unknown")
    if location.get("shared"):
        parts.append("shared" if location.get("bindings_observed") else "saved shared binding")
    if location.get("pinned"):
        parts.append("pinned" if location.get("bindings_observed") else "saved pin")
    references = location.get("container_references")
    if references is None:
        parts.append("container use unknown")
    elif references:
        parts.append("container referenced")
    if not location.get("registered_home") and not location.get("bindings"):
        parts.append("unregistered")
    return ", ".join(parts)


def placement(entry: dict, node: dict, *, compact: bool = False) -> str:
    locations = [item for item in entry["locations"] if item["node_id"] == node["node_id"]]
    if not locations:
        unknown = (node["status"] == "unknown" or node["issues"]
                   or any(scope["status"] in ("unknown", "partial") for scope in node["scopes"]))
        return ("?" if compact else "unknown") if unknown else ("—" if compact else "not seen in scanned roots")
    if not compact:
        return "; ".join(_words(item) for item in locations)
    parts = []
    for item in locations:
        kind = "H" if item["kind"] == "home" else "P"
        parts.append(kind if item["state"] == "present" else kind + (" missing" if item["state"] == "missing" else "?"))
    for field, word in (("pinned", "pin"), ("shared", "shared")):
        if any(item.get(field) and item.get("bindings_observed") for item in locations):
            parts.append(word)
    if any(not item.get("bindings_observed") for item in locations):
        parts.append("pins?")
    if any(item.get("container_references") for item in locations):
        parts.append("used")
    elif any(item.get("container_references") is None for item in locations):
        parts.append("use?")
    return " ".join(parts)


def _table(entries: list[dict], nodes: list[dict], width: int) -> list[str]:
    from .render import short_size
    labels = [entry["model_id"] + " @" + (entry["model_commit"] or "unknown")[:8] for entry in entries]
    headers = ["Model snapshot", "Payload est.", *[node["hostname"] for node in nodes]]
    cells = [[labels[index], short_size(entry["payload_bytes"]),
              *[placement(entry, node, compact=True) for node in nodes]] for index, entry in enumerate(entries)]
    if not cells:
        return []
    widths = [max(len(row[column]) for row in [headers, *cells]) for column in range(len(headers))]
    widths[0] = min(widths[0], width - sum(widths[1:]) - 2 * (len(widths) - 1))
    if widths[0] < 16:
        return []
    table = []
    for row in [headers, *cells]:
        first = row[0] if len(row[0]) <= widths[0] else row[0][:widths[0] - 1] + "…"
        table.append("  ".join([first.ljust(widths[0]), *[cell.ljust(widths[column])
                                                        for column, cell in enumerate(row[1:], 1)]]).rstrip())
    return table


def render(document: dict, *, writer=None, snapshot: str | None = None) -> None:
    from scripts.terminal_format import TerminalWriter
    from .render import short_size
    from .catalog import age_seconds, age_text
    def observed(value):
        return (value + " (" + age_text(age_seconds(value)) + ")") if value else "not observed"
    out = writer or TerminalWriter()
    out.emit("Model files and disk space", role="heading")
    out.field("Collected", observed(document["observed_at"]))
    out.emit("Captured observations: Refresh checks again. Discovery checks declared file names and sizes, "
             "without hashing contents. Unknown means not observed. Cleanup previews recheck all protections.")
    for issue in document.get("issues", []):
        out.field("Warning", issue, label_role="warn")
    nodes = document["nodes"]
    if snapshot is None:
        for node in nodes:
            out.blank()
            out.emit(node["hostname"], role="heading")
            out.field("Files checked", observed(node.get("observed_at")))
            if node.get("error"):
                out.field("Files", "unknown: " + node["error"])
            for disk in node["disks"]:
                out.field("Disk free", short_size(disk.get("available")) + " at " + disk["path"])
                if disk.get("error"):
                    out.field("Not observed", disk["error"])
            for scope in node["scopes"]:
                out.field("Scope", scope["path"] + " — " + scope["status"])
                if scope.get("error"):
                    out.field("Not observed", scope["error"])
            for issue in node["issues"]:
                out.field("Warning", issue, label_role="warn")
            if node.get("container_error"):
                out.field("Container use", "unknown: " + node["container_error"])
            for item in node["unidentified"]:
                out.field("Inspect only", item["path"] + " — " + item["reason"])
    entries = [entry for entry in document["snapshots"] if snapshot is None or entry["snapshot_manifest_id"] == snapshot]
    if not entries:
        out.blank()
        out.emit("No model snapshots were identified in the observed managed roots or saved records.")
    table = _table(entries, nodes, out.width) if snapshot is None else []
    if table:
        out.blank()
        for index, line in enumerate(table):
            out.emit(line, role="heading" if index == 0 else None)
        out.emit("H home; P prepared copy; pin pinned; shared reused by recipes; used container reference "
                 "(including stopped); pins? retention unknown; use? container use unknown; "
                 "? unknown; — not seen in scanned roots.")
        out.emit("Select a snapshot for paths, saved archive records and cleanup choices. Archives are not scanned.")
        out.emit("Payload estimates are manifest sizes per complete copy, not allocated or reclaimable space.")
        return
    for entry in entries:
        out.blank()
        out.emit(entry["model_id"] + " @ " + (entry["model_commit"] or "unknown")[:12], role="heading")
        out.field("Snapshot", entry["snapshot_manifest_id"][:12])
        out.field("Payload estimate", short_size(entry["payload_bytes"]) + " per complete copy (manifest sizes)")
        for node in nodes:
            locations = [location for location in entry["locations"] if location["node_id"] == node["node_id"]]
            out.field(node["hostname"], placement(entry, node))
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
                             + "; not checked now") if archive else "no saved archive record; not checked now")
        if snapshot is not None and archive and archive.get("root"):
            out.field("Archive location", archive["root"])
        if snapshot is not None and entry["cleanup_spec_ids"]:
            out.emit("Cleanup opens a recipe. Purge covers its prepared copies; Remove home selects a snapshot.")
        if not entry["cleanup_spec_ids"]:
            out.emit("Inspection only: no registered catalog cleanup route. Files were not registered or changed.")
    out.blank()
    out.emit("Payload estimates are not allocated disk usage or reclaimable space. Shared bindings and home views "
             "reuse the same files. Archives are separate and never deleted here.")


def read_inventory(path: Path) -> dict:
    document = json.loads(path.read_text())
    if document.get("ok") is True:
        document = document["result"]
    if document.get("kind") != "pulsar-model-file-inventory":
        raise StorageError((document.get("error") or {}).get("message") or "model files returned no inventory")
    return document


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
                render(document, snapshot=args.snapshot)
            elif args.command == "choices":
                from scripts.terminal_format import terminal_width
                for entry in document["snapshots"]:
                    label = entry["model_id"] + " @ " + (entry["model_commit"] or "unknown")[:8]
                    if not entry["cleanup_spec_ids"]:
                        label += " — inspect only"
                    label = " ".join(label.split())
                    print(entry["snapshot_manifest_id"] + "\t" + label[:terminal_width() - 2])
            else:
                for entry in document["snapshots"]:
                    if entry["snapshot_manifest_id"] == args.snapshot:
                        for spec in sorted(entry["cleanup_spec_ids"]):
                            members = [value["snapshot"] for value in entry["catalog_specs"] if value["spec_id"] == spec]
                            print(spec + "\t" + "Cleanup recipe [" + spec[:12] + "] — " + ", ".join(members))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        from scripts.terminal_format import error_line
        error_line(exc, tag="model files")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
