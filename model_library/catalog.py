"""Catalog projection from released specs and saved managed-file observations.

Reading this module never probes hardware, scans cache directories or mutates
controller state. Only an explicit Check now refreshes operational observations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from release_spec import load_spec
from .integrity import StorageError
from .state import Store, checked_id
from scripts.terminal_format import TerminalWriter

ROOT = Path(__file__).resolve().parents[1]


def age_seconds(value, now=None):
    if value is None:
        return None
    if not isinstance(value, str):
        raise StorageError("saved observation time must be UTC text")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StorageError("saved observation time is invalid") from exc
    if stamp.tzinfo is None:
        raise StorageError("saved observation time must include a timezone")
    current = now or datetime.now(timezone.utc)
    if stamp > current:
        return None
    return int((current - stamp).total_seconds())


def age_text(age):
    if age is None:
        return "age unknown"
    if age < 60:
        return "less than a minute ago"
    if age < 3600:
        return f"{age // 60} minutes ago"
    if age < 86400:
        return f"{age // 3600} hours ago"
    return f"{age // 86400} days ago"


def project(spec, store, *, now=None):
    spec_id = spec["spec_id"]
    manifest_id = spec["identity"]["snapshot_manifest"]["manifest_id"]
    home = store.home(manifest_id)
    views = store.views(spec_id=spec_id)
    archive = store.get("archives", manifest_id)
    if any(view["snapshot_manifest_id"] != manifest_id for view in views):
        raise StorageError("prepared-copy record differs from the selected snapshot")
    if archive is not None and (archive.get("snapshot_manifest_id") != manifest_id or archive.get("verified") is not True):
        raise StorageError("archive record differs from the selected snapshot")
    observation = store.get("observations", spec_id)
    if observation is not None:
        if (observation.get("kind") != "pulsar-saved-observation" or type(observation.get("schema_version")) is not int or observation.get("schema_version") != 1
                or observation.get("spec_id") != spec_id):
            raise StorageError("saved observation does not name the selected spec")
    observed = observation or {}
    local_state = observed.get("local_state", "unknown")
    archive_state = observed.get("archive_state", "unknown")
    if local_state not in {"unknown", "missing", "ready", "changed"}:
        raise StorageError("saved local preparation state is invalid")
    if archive_state not in {"unknown", "missing", "present", "verified", "unavailable", "not-configured"}:
        raise StorageError("saved archive state is invalid")
    blockers = observed.get("blockers", [])
    if not isinstance(blockers, list) or any(not isinstance(x, str) for x in blockers):
        raise StorageError("saved blockers must be a list of explanations")
    checked_at = observed.get("checked_at")
    return {"spec_id": spec_id, "model_id": spec["identity"]["model_id"],
        "snapshot_revision": spec["identity"]["snapshot_revision"], "snapshot_manifest_id": manifest_id,
        "geometry": spec["identity"]["geometry"], "image": spec["identity"]["image"],
        "engine_args": spec["identity"]["engine_args"], "review": spec["review"],
        "local_state": local_state, "archive_state": archive_state,
        "checked_at": checked_at, "observation_age_seconds": age_seconds(checked_at, now),
        "blockers": blockers, "home": home, "prepared_copies": views,
        "archive": archive, "archive_age_seconds": age_seconds((archive or {}).get("verified_at"), now)}


def entries(repo, store, *, spec_id=None, now=None):
    repo = Path(repo)
    if spec_id:
        paths = [repo / "releases" / f"{checked_id(spec_id)}.json"]
    else:
        paths = sorted((repo / "releases").glob("*.json"))
    result = []
    for path in paths:
        spec = load_spec(path)
        if spec["state"] != "released" or path.name != f"{spec['spec_id']}.json":
            raise StorageError("catalog contains a candidate or incorrectly named spec")
        result.append(project(spec, store, now=now))
    return sorted(result, key=lambda row: (row["review"]["status"] == "withdrawn", row["model_id"], row["spec_id"]))


LOCAL_LABELS = {"unknown": "unknown; choose Check now", "missing": "required files missing at last check",
    "ready": "files prepared at last check", "changed": "files changed; verification is required"}
ARCHIVE_LABELS = {"unknown": "unknown", "missing": "not found at last check",
    "present": "present; verify before restore",
    "verified": "verified at last check", "unavailable": "could not be checked",
    "not-configured": "storage is not configured"}


def render(rows, *, details=False, writer=None):
    out = writer or TerminalWriter()
    if not rows:
        out.emit("The catalog is empty.")
        out.emit("A qualifying recipe enters the catalog after review and merge. Model experiments belong in the private workbench.")
        return
    out.emit("Catalog review, prepared files and running service are separate states.")
    out.emit("These are saved observations. Start rechecks its prerequisites.")
    for row in rows:
        out.blank()
        out.emit(row["model_id"])
        out.field("Spec", row["spec_id"] if details else row["spec_id"][:12])
        geometry = row["geometry"]
        out.field("Recipe", f"{geometry['nodes']} node(s); tensor parallel {geometry['tp']}; pipeline parallel {geometry['pp']}")
        out.field("Review", row["review"]["status"])
        if row["review"]["status"] == "withdrawn":
            out.field("Reason", row["review"].get("reason", "No reason recorded"))
            out.emit("Withdrawn recipes are not recommended. Exact serving remains possible when operational checks pass.")
        out.field("Files", LOCAL_LABELS[row["local_state"]])
        out.field("Archive", ARCHIVE_LABELS[row["archive_state"]])
        out.field("Checked", f"{row['checked_at']} ({age_text(row['observation_age_seconds'])})" if row["checked_at"] else "not observed")
        if row["archive"]:
            out.field("Last archive verification", f"{row['archive'].get('verified_at', 'unknown')} ({age_text(row['archive_age_seconds'])})")
        if details:
            out.field("Commit", row["snapshot_revision"])
            out.field("Snapshot", row["snapshot_manifest_id"])
            out.field("Image", row["image"]["digest"])
            out.field("Arguments", " ".join(row["engine_args"]))
            home = row["home"]
            out.field("Home record", f"{home['node_id']}: {home['path']}" if home else "none recorded")
            out.field("Copy records", str(len(row["prepared_copies"])))
            for view in sorted(row["prepared_copies"], key=lambda v: v["rank"]):
                out.field(f"Rank {view['rank']}", f"{view['node_id']}; {'pinned' if view['pinned'] else 'not pinned'}; {view['path']}")
            for blocker in row["blockers"]:
                out.field("Blocker", blocker)
            out.emit("Saved location records describe known managed files; they are not proof that those files are currently intact.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pulsar models", description="Read catalog specs and saved storage observations")
    parser.add_argument("command", choices=("list", "show"), nargs="?", default="list")
    parser.add_argument("spec_id", nargs="?")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--state-root", default=os.environ.get("PULSAR_MODEL_LIBRARY_DIR", os.environ.get("MODEL_LIBRARY_DIR", str(ROOT / ".model-library"))))
    args = parser.parse_args(argv)
    try:
        if args.command == "show" and not args.spec_id:
            raise StorageError("show requires one complete spec id")
        rows = entries(args.repo_root, Store(args.state_root), spec_id=args.spec_id)
        if args.json:
            print(json.dumps({"schema_version": 1, "kind": "pulsar-model-catalog", "entries": rows}, sort_keys=True))
        else:
            render(rows, details=args.command == "show")
        return 0
    except (StorageError, ValueError, OSError, KeyError, TypeError) as exc:
        print(f"catalog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
