"""Catalog projection from published specs and saved managed-file observations.

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
from .node_names import NodeNames
from .state import Store, checked_id
from scripts.terminal_format import TerminalWriter

ROOT = Path(__file__).resolve().parents[1]


from release_spec.serving import identity_fields, required_snapshots


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
    for seconds, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if age >= seconds:
            count = age // seconds
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "less than a minute ago"


def combined_observation(members):
    """Aggregate complete per-snapshot checks without turning unknown into absent."""
    def state(field, precedence):
        values={m.get(field,'unknown') for m in members.values()}
        return next(v for v in precedence if v in values)
    return {'snapshots':members,
        'local_state':state('local_state',('unknown','changed','missing','ready')),
        'archive_state':state('archive_state',('unknown','unavailable','not-configured','missing','present','verified')),
        'prepared':{'verified':sum(m.get('prepared',{}).get('verified',0) for m in members.values()),
                    'required':sum(m.get('prepared',{}).get('required',0) for m in members.values())},
        'blockers':[name+': '+b for name,m in members.items() for b in m.get('blockers',[])]}


# Why this Stack cannot start a spec, as the start blocker code a start would
# report, and the human wording for it.
START_UNSUPPORTED_LABELS = {"guard_unsupported": "serving guard", "historical_spec": "historical schema-1 spec"}


def start_support(spec):
    """Whether this Stack can start the spec, judged from the spec alone.

    Returns (start_supported, start_unsupported_reason). A serving guard in
    recipe.container needs enforcement this Stack does not implement, so every
    launcher refuses it as the guard_unsupported start blocker. Historical
    schema-1 specs have no launch compiler (historical_spec). This is not a
    readiness check and never gates catalog membership.
    """
    if spec.get("schema_version") not in (2, 3):
        return False, "historical_spec"
    recipe = spec.get("recipe")
    container = recipe.get("container") if isinstance(recipe, dict) else None
    if isinstance(container, dict) and "guard" in container:
        return False, "guard_unsupported"
    return True, None


def project(spec, store, *, now=None):
    spec_id = spec["spec_id"]
    manifest_id = identity_fields(spec)["snapshot_manifest"]["manifest_id"]
    home = store.home(manifest_id)
    views = store.views(spec_id=spec_id)
    archive = store.get("archives", manifest_id)
    snapshots=required_snapshots(spec)
    manifest_ids={m["snapshot_manifest"]["manifest_id"] for m in snapshots.values()}
    if any(view["snapshot_manifest_id"] not in manifest_ids for view in views):
        raise StorageError("prepared-copy record differs from the selected snapshot")
    if archive is not None and (archive.get("snapshot_manifest_id") != manifest_id or archive.get("verified") is not True):
        raise StorageError("archive record differs from the selected snapshot")
    observation = store.get("observations", spec_id)
    if observation is not None:
        if (observation.get("kind") != "pulsar-saved-observation" or type(observation.get("schema_version")) is not int or observation.get("schema_version") != 1
                or observation.get("spec_id") != spec_id):
            raise StorageError("saved observation does not name the selected spec")
    observed = observation or {}
    members = {}
    if spec.get('schema_version') == 3:
        saved = observed.get('snapshots', {})
        if observation is not None and set(saved) != set(snapshots):
            raise StorageError('saved observation does not cover the required snapshot set')
        for name,model in snapshots.items():
            mid=model['snapshot_manifest']['manifest_id']
            recovery=store.get('archives',mid)
            if recovery is not None and (recovery.get('snapshot_manifest_id')!=mid or recovery.get('verified') is not True):
                raise StorageError('archive record differs from required snapshot')
            members[name]={'snapshot_manifest_id':mid,'model_id':model['model_id'],'model_commit':model['model_commit'],
                'home':store.home(mid),'archive':recovery,'observation':saved.get(name,{}),
                'prepared_copies':[v for v in views if v['snapshot_manifest_id']==mid]}
        # Only complete saved checks establish aggregate readiness.
        if observation is not None:
            combined=combined_observation(saved)
            if any(observed.get(k)!=combined[k] for k in ('local_state','archive_state','prepared','blockers')):
                raise StorageError('saved aggregate disagrees with snapshot observations')
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
    start_supported, start_unsupported_reason = start_support(spec)
    return {"historical": spec.get("schema_version") not in (2,3), "spec_id": spec_id, "model_id": identity_fields(spec)["model_id"],
        "snapshot_revision": identity_fields(spec)["snapshot_revision"], "snapshot_manifest_id": manifest_id,
        "geometry": identity_fields(spec)["geometry"], "image": identity_fields(spec)["image"],
        "engine_args": identity_fields(spec)["engine_args"], "state": spec["state"],
        "review": spec["review"],
        "start_supported": start_supported, "start_unsupported_reason": start_unsupported_reason,
        "local_state": local_state, "archive_state": archive_state,
        "checked_at": checked_at, "observation_age_seconds": age_seconds(checked_at, now),
        "blockers": blockers, "home": home, "prepared_copies": views,
        **({"snapshots":members} if spec.get("schema_version")==3 else {}),
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
        if path.name != f"{spec['spec_id']}.json":
            raise StorageError("catalog contains an incorrectly named spec")
        result.append(project(spec, store, now=now))
    return sorted(result, key=lambda row: (
        (row.get("review") or {}).get("status") == "withdrawn",
        row["model_id"], row["spec_id"],
    ))


LOCAL_LABELS = {"unknown": "unknown; choose Check now", "missing": "required files missing at last check",
    "ready": "files prepared at last check", "changed": "files changed; verification is required"}
ARCHIVE_LABELS = {"unknown": "unknown", "missing": "not found at last check",
    "present": "present; verify before restore",
    "verified": "verified at last check", "unavailable": "could not be checked",
    "not-configured": "storage is not configured"}


def render(rows, *, details=False, writer=None, names=None):
    out = writer or TerminalWriter()
    names = names or NodeNames()
    if not rows:
        out.emit("The catalog is empty.")
        out.emit("A spec enters the catalog when the maintainer publishes it under releases/. Interactive ./pulsar confirms cluster membership first. ./pulsar models still lists the catalog without topology.")
        return
    out.emit("Catalog review, prepared files and running service are separate states.")
    out.emit("These are saved observations. Start rechecks its prerequisites.")
    for row in rows:
        out.blank()
        out.emit(row["model_id"])
        out.field("Spec", row["spec_id"] if details else row["spec_id"][:12])
        if row.get("historical"):
            out.emit("Historical spec: create a schema-2 spec for future operations.")
        geometry = row["geometry"]
        out.field("Recipe", f"{geometry['nodes']} node(s); tensor parallel {geometry['tp']}; pipeline parallel {geometry['pp']}")
        startable = row.get("start_supported") is not False
        if not startable:
            reason = row.get("start_unsupported_reason")
            out.field("Start", f"not supported by this Stack ({START_UNSUPPORTED_LABELS.get(reason, reason)})")
        review = row.get("review") or {}
        out.field("State", row.get("state") or "not specified")
        out.field("Review", review.get("status") or "not specified")
        if review.get("status") == "withdrawn":
            out.field("Reason", review.get("reason", "No reason recorded"))
            out.emit("Withdrawn recipes are not recommended."
                     + (" Exact serving remains possible when operational checks pass." if startable else ""))
        out.field("Files", LOCAL_LABELS[row["local_state"]])
        out.field("Archive", ARCHIVE_LABELS[row["archive_state"]])
        out.field("Checked", f"{row['checked_at']} ({age_text(row['observation_age_seconds'])})" if row["checked_at"] else "not observed")
        if row["archive"]:
            out.field("Last archive verification", f"{row['archive'].get('verified_at', 'unknown')} ({age_text(row['archive_age_seconds'])})")
        for name, member in row.get('snapshots',{}).items():
            status=member.get('observation',{})
            out.field('Snapshot '+name, member['model_id']+' @ '+member['model_commit'][:12])
            out.field('Files / archive',status.get('local_state','unknown')+' / '+status.get('archive_state','unknown'),indent=2)
        if details:
            out.field("Commit", row["snapshot_revision"])
            out.field("Snapshot", row["snapshot_manifest_id"])
            out.field("Image", row["image"]["digest"])
            out.field("Arguments", " ".join(row["engine_args"]))
            home = row["home"]
            out.field("Home record", f"{names(home['node_id'])}: {home['path']}" if home else "none recorded")
            out.field("Copy records", str(len(row["prepared_copies"])))
            for view in sorted(row["prepared_copies"], key=lambda v: v["rank"]):
                out.field(f"Rank {view['rank']}", f"{names(view['node_id'])}; {'pinned' if view['pinned'] else 'not pinned'}; {view['path']}")
            for blocker in row["blockers"]:
                out.field("Blocker", names.prefixed(blocker) if isinstance(blocker, str) else blocker)
            out.emit("Saved location records describe known managed files; they are not proof that those files are currently intact.")


def prefix_hint(repo, spec_id):
    """Name the complete catalog IDs a shortened spec ID matches; never select one."""
    if len(spec_id) >= 64 or not spec_id or any(c not in "0123456789abcdef" for c in spec_id):
        return
    from scripts.spec_selector import matches as catalog_matches
    matches = catalog_matches(repo, spec_id)
    if not matches:
        raise StorageError(f"no catalog spec ID starts with {spec_id}; see ./pulsar models list")
    raise StorageError("expected the complete 64-character spec ID; " + spec_id + " matches "
                       + ", ".join(matches))


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
        if args.spec_id:
            prefix_hint(args.repo_root, args.spec_id)
        rows = entries(args.repo_root, Store(args.state_root), spec_id=args.spec_id)
        if args.json:
            print(json.dumps({"schema_version": 1, "kind": "pulsar-model-catalog", "entries": rows}, sort_keys=True))
        else:
            render(rows, details=args.command == "show", names=NodeNames.saved(args.repo_root))
        return 0
    except (StorageError, ValueError, OSError, KeyError, TypeError) as exc:
        print(f"error: catalog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
