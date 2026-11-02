"""Read-only discovery inside explicit Pulsar home and prepared-copy namespaces.

Directory and manifest reads never follow internal symlinks. No model bytes are
hashed, no records are registered and no lock directories or files are created.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from .integrity import StorageError, directory, file_at, read_json, verify_manifest
from .local import payload
from .state import Store, now, reconcile_views, validate_home, validate_view

IDENTITY = re.compile(r"[0-9a-f]{64}")
PENDING = re.compile(r"\.pending-[0-9a-f]{32}")


def _missing(exc: Exception) -> bool:
    return isinstance(exc, FileNotFoundError) or isinstance(exc.__cause__, FileNotFoundError)


def _summary(manifest: dict) -> dict:
    return {key: manifest[key] for key in
            ("manifest_id", "model_id", "snapshot_revision", "total_bytes", "file_count")}


def _disk(path: Path) -> dict:
    """Observe the filesystem containing a configured root, without walking it."""
    current = path
    while True:
        try:
            with directory(current) as fd:
                value = os.fstatvfs(fd)
            return {"path": str(path), "available": value.f_bavail * value.f_frsize,
                    "total": value.f_blocks * value.f_frsize}
        except (OSError, StorageError) as exc:
            if not _missing(exc) or current == current.parent:
                return {"path": str(path), "error": str(exc)}
            current = current.parent


def inspect_files(request: dict, home_root: Path, view_root: Path) -> dict:
    """Inspect immediate managed hubs and explicit known locations on one node."""
    node_id = request["node_id"]
    result = {"node_id": node_id, "observed_at": now(), "scopes": [], "locations": [],
              "unidentified": [], "issues": [], "disks": [], "bindings_observed": True}
    homes = []
    views = []
    for value in request.get("homes", []):
        record = validate_home(value)
        if record["node_id"] != node_id:
            raise StorageError("inventory home belongs to another node")
        homes.append(record)
    for value in request.get("views", []):
        record = validate_view(value)
        if record["node_id"] != node_id:
            raise StorageError("inventory prepared binding belongs to another node")
        views.append(record)
    # Read existing node records without Store.lock(): that would create state.
    try:
        if (view_root / ".pulsar-node").is_symlink():
            raise StorageError("node state must not be an internal symlink")
        local_views = Store(view_root / ".pulsar-node").views()
        if any(record["node_id"] != node_id for record in local_views):
            raise StorageError("node prepared bindings belong to another node")
        views = reconcile_views([*views, *local_views])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["bindings_observed"] = False
        result["issues"].append("Prepared bindings could not be reconciled: " + str(exc))

    namespaces = {(home_root / "pulsar-homes", "home"), (view_root, "prepared")}
    for raw in request.get("home_roots", []):
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts:
            result["issues"].append("Configured home root is not absolute: " + str(path))
            continue
        # A configured root may be a site-selected alias; internal links may not.
        namespaces.add((Path(os.path.realpath(path)) / "pulsar-homes", "home"))
    candidates = {}
    for record in [*homes, *views]:
        kind = "home" if record in homes or record.get("is_home_view") else "prepared"
        hub = Path(record["hub_path"])
        candidates[str(hub)] = kind
        if kind == "home" and hub.name == record["snapshot_manifest_id"] and hub.parent.name == "pulsar-homes":
            namespaces.add((hub.parent, "home"))

    for namespace, kind in sorted(namespaces, key=lambda pair: (str(pair[0]), pair[1])):
        scope = {"path": str(namespace), "kind": kind, "status": "observed"}
        try:
            with directory(namespace) as fd:
                for name in sorted(os.listdir(fd)):
                    if not (IDENTITY.fullmatch(name) or PENDING.fullmatch(name)):
                        continue
                    path = namespace / name
                    value = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISDIR(value.st_mode):
                        scope["status"] = "partial"
                        result["unidentified"].append({"path": str(path), "kind": kind,
                                                        "reason": "not a regular managed directory"})
                    elif PENDING.fullmatch(name):
                        result["unidentified"].append({"path": str(path), "kind": "incomplete",
                                                        "reason": "incomplete staging; not a complete snapshot"})
                    else:
                        candidates[str(path)] = kind
        except (OSError, StorageError) as exc:
            scope["status"] = "not-found" if _missing(exc) else "unknown"
            if scope["status"] == "unknown":
                scope["error"] = str(exc)
        result["scopes"].append(scope)
        result["disks"].append(_disk(namespace))

    for path, kind in sorted(candidates.items()):
        hub = Path(path)
        bindings = [record for record in views if record["hub_path"] == path]
        registered = any(record["hub_path"] == path for record in homes)
        expected = {record["snapshot_manifest_id"] for record in [*homes, *bindings] if record["hub_path"] == path}
        item = {"kind": kind, "hub_path": path, "state": "unknown", "registered_home": registered,
                "bindings": bindings, "verified_at": next((record["verified_at"] for record in homes
                                                         if record["hub_path"] == path), None)}
        if len(expected) == 1:
            item["snapshot_manifest_id"] = next(iter(expected))
        elif kind == "home" and IDENTITY.fullmatch(hub.name):
            # The namespace names an expected identity even if its manifest is unreadable.
            item["snapshot_manifest_id"] = hub.name
        try:
            with directory(hub):
                manifest = verify_manifest(read_json(hub / "manifest.json"))
                if expected and expected != {manifest["manifest_id"]}:
                    raise StorageError("managed manifest differs from saved ownership")
                if kind == "home" and hub.name != manifest["manifest_id"]:
                    raise StorageError("home directory name differs from its manifest")
                item.update(snapshot_manifest_id=manifest["manifest_id"], manifest=_summary(manifest),
                            path=str(payload(hub, manifest)))
                # Check only declared files and their sizes; this is not hash verification.
                with directory(payload(hub, manifest)) as snapshot:
                    for entry in manifest["files"]:
                        with file_at(snapshot, entry["path"]) as source:
                            if os.fstat(source).st_size != entry["size"]:
                                raise StorageError("file size differs from manifest: " + entry["path"])
                item["state"] = "present"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            item["error"] = str(exc)
            # Missing payload files do not mean the whole managed hub is absent.
            try:
                with directory(hub.parent) as parent:
                    os.stat(hub.name, dir_fd=parent, follow_symlinks=False)
            except (OSError, StorageError) as missing:
                if _missing(missing):
                    item["state"] = "missing"
        if "snapshot_manifest_id" in item:
            result["locations"].append(item)
        else:
            result["unidentified"].append({"path": path, "kind": kind,
                                            "reason": item.get("error", "snapshot identity unknown")})
            for scope in result["scopes"]:
                if Path(scope["path"]) == hub.parent and scope["status"] == "observed":
                    scope["status"] = "partial"
    return result
