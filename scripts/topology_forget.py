#!/usr/bin/env python3
"""Plan, back up and remove this checkout's saved cluster state (`topology forget`).

    topology_forget.py plan --repo-root R --topology F --ssh-config F --overlay F
    topology_forget.py render PLAN [--services FILE]
    topology_forget.py apply PLAN --backup-root DIR

What is removed: the saved membership, the generated SSH configuration and
the saved observations that describe the cluster (service inventory,
storage budget, cluster changes noticed). What is kept: `.env`, the site
overlay, the model-library records, model files on the nodes, archives and
OpenSSH's own known_hosts. The removed files are first copied into a private
backup folder, named by time, under the backup root (`.pulsar/` by default).

Nothing here contacts a node; `scripts/topology-forget.sh` asks the nodes
whether a Pulsar service runs and passes the answers to `render` and `apply`.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import saved_observations  # noqa: E402
from scripts.terminal_format import TerminalWriter, error_line  # noqa: E402
from scripts.topology_manifest import TopologyError, extract_topology, load_json, validate_manifest  # noqa: E402

KIND = "pulsar-topology-forget"
SCHEMA_VERSION = 1
BACKUP_ENV = "PULSAR_CLUSTER_BACKUP_DIR"
BACKUP_PREFIX = "forgotten-cluster-"

# What each removed file is, in the order people read them.
REMOVED = (
    ("membership", "Saved membership"),
    ("ssh-config", "Generated SSH configuration"),
    (saved_observations.INVENTORY_NAME, "Saved service inventory"),
    (saved_observations.BUDGET_NAME, "Saved storage budget"),
    (saved_observations.CHANGES_NAME, "Cluster changes noticed"),
)
KEPT = (
    ".env settings, such as the API key and the archive location",
    "the site overlay",
    "model-library records, which stay valid while the nodes keep their OS install",
    "model files on the nodes, and archives",
    "OpenSSH's own known_hosts",
)


def backup_root(repo_root: Path) -> Path:
    override = os.environ.get(BACKUP_ENV)
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError(f"{BACKUP_ENV} must be an absolute path")
        return path
    return Path(os.path.realpath(repo_root)) / ".pulsar"


def _digest(path: Path) -> str | None:
    """The file's SHA-256, or None when it is absent. A path that is not a
    regular file is refused: forget removes only the files it backed up."""
    if not os.path.lexists(path):
        return None
    mode = os.lstat(path).st_mode
    if not stat.S_ISREG(mode):
        raise ValueError(f"{path} is not a regular file; inspect it before forgetting this cluster")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _membership(path: Path) -> tuple[str, list[dict]]:
    if not os.path.lexists(path):
        return "missing", []
    try:
        topology = extract_topology(load_json(path))
        validate_manifest(topology, require_verified=True)
    except (TopologyError, KeyError, TypeError, ValueError, AttributeError, OSError):
        return "invalid", []
    return "saved", [{key: node[key] for key in ("rank", "node_id", "hostname", "ssh_host")}
                     for node in sorted(topology["nodes"], key=lambda node: node["rank"])]


def _overlay_nodes(path: Path, nodes: list[dict]) -> dict:
    """Which nodes the site overlay names for placement. The overlay can name
    a node by its node_id, in its defaults and per spec; forget keeps it."""
    if not os.path.lexists(path):
        return {"path": str(path), "present": False, "names": []}
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = [document.get("defaults") or {}, *((document.get("specs") or {}).values())]
        named = sorted({str(entry["placement"]["node_id"]) for entry in entries
                        if isinstance(entry, dict) and isinstance(entry.get("placement"), dict)
                        and entry["placement"].get("node_id")})
    except (OSError, ValueError, AttributeError, TypeError):
        return {"path": str(path), "present": True, "names": None}
    hostnames = {node["node_id"]: node["hostname"] for node in nodes}
    return {"path": str(path), "present": True,
            "names": [{"node_id": node_id, "hostname": hostnames.get(node_id)} for node_id in named]}


def plan(repo_root: Path, topology: Path, ssh_config: Path, overlay: Path) -> dict:
    paths = {"membership": topology, "ssh-config": ssh_config}
    for name in (saved_observations.INVENTORY_NAME, saved_observations.BUDGET_NAME,
                 saved_observations.CHANGES_NAME):
        paths[name] = saved_observations.record_path(repo_root, name)
    status, nodes = _membership(topology)
    remove = []
    for name, label in REMOVED:
        path = Path(paths[name])
        digest = _digest(path)
        remove.append({"name": name, "label": label, "path": str(path), "present": digest is not None,
                       "sha256": digest})
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "result": "planned" if any(item["present"] for item in remove) else "nothing",
        "membership": status,
        "nodes": nodes,
        "remove": remove,
        "keep": list(KEPT),
        "overlay": _overlay_nodes(overlay, nodes),
        "backup_root": str(backup_root(repo_root)),
    }


def _services(path: str | None) -> list[dict]:
    """Rows `hostname<TAB>state` from the shell: running, none or not-observed."""
    rows = []
    if path and Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            hostname, _, state = line.partition("\t")
            if hostname:
                rows.append({"hostname": hostname, "state": state or "not-observed"})
    return rows


def _joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def render(document: dict, writer: TerminalWriter | None = None) -> None:
    out = writer or TerminalWriter()

    def field(label: str, value: str, markup: bool = False) -> None:
        out.field(label, value, indent=2, label_width=12, markup=markup)

    out.emit("FORGET THIS CLUSTER")
    membership = document["membership"]
    if membership == "saved":
        field("Membership", "saved: " + ", ".join(node["hostname"] for node in document["nodes"]))
    elif membership == "invalid":
        field("Membership", "saved but invalid; forget removes it so setup can start again")
    else:
        field("Membership", "not saved")
    services = document.get("services") or []
    running = [row["hostname"] for row in services if row["state"] == "running"]
    quiet = [row["hostname"] for row in services if row["state"] == "none"]
    unseen = [row["hostname"] for row in services if row["state"] == "not-observed"]
    if running:
        field("Services", f"a Pulsar service runs on {_joined(running)}; stop it first with "
                          "`./pulsar stop --all --yes`", markup=True)
    elif quiet:
        field("Services", f"none running on {_joined(quiet)}")
    for hostname in unseen:
        field("" if running or quiet or hostname != unseen[0] else "Services",
              f"{hostname} not observed: it could not be asked; it does not block forgetting")
    if document.get("checked_only_here"):
        field("", "Only this machine was checked: the saved membership or its SSH configuration cannot be used.")
    out.blank()
    out.emit("Removes")
    for item in document["remove"]:
        # A path hangs under its name, so a narrow terminal keeps both readable.
        out.emit(item["label"] + ("" if item["present"] else ": not present"), initial_indent="  ",
                 subsequent_indent="    ")
        if item["present"]:
            out.emit(item["path"], initial_indent="    ", subsequent_indent="    ")
    out.blank()
    out.emit("Keeps")
    for line in document["keep"]:
        out.emit(line, initial_indent="  ", subsequent_indent="    ")
    overlay = document["overlay"]
    if overlay.get("present"):
        names = overlay.get("names")
        if names is None:
            out.emit("The site overlay could not be read, so the nodes it names are not known.",
                     initial_indent="  ", subsequent_indent="    ")
        elif names:
            shown = [item["hostname"] or item["node_id"][:12] for item in names]
            out.emit(f"The site overlay names {_joined(shown)} for placement; it still does after forgetting.",
                     initial_indent="  ", subsequent_indent="    ")
    out.blank()
    out.emit("Backup")
    out.emit("The removed files are first copied to a private folder under:", initial_indent="  ",
             subsequent_indent="  ")
    out.emit(document["backup_root"], initial_indent="    ", subsequent_indent="    ")


def apply(document: dict, root: Path, clock=None) -> dict:
    """Copy every planned file into a new private folder under ROOT, then
    remove it. A file that changed since the plan stops before anything is
    removed. Saved observations are removed under their own locks.

    A copy is named by what the file is (its name in REMOVED), never by the
    file's own name: configured paths can share a file name, or have the name
    of the backup's list, `forgotten.json`. REMOVED's names are distinct, so
    no copy replaces another. The list gives each copy's original path."""
    present = [item for item in document["remove"] if item["present"]]
    for item in present:
        if _digest(Path(item["path"])) != item["sha256"]:
            raise ValueError(f"{item['path']} changed since the plan; review it and forget again")
    stamp = (clock or (lambda: datetime.now(timezone.utc)))().strftime("%Y%m%dT%H%M%SZ")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder, suffix = root / f"{BACKUP_PREFIX}{stamp}", 1
    while True:
        try:
            folder.mkdir(mode=0o700)
            break
        except FileExistsError:
            suffix += 1
            folder = root / f"{BACKUP_PREFIX}{stamp}-{suffix}"
    os.chmod(folder, 0o700)
    files = [{"name": item["name"], "path": item["path"], "file": item["name"], "sha256": item["sha256"]}
             for item in present]
    for copy in files:
        target = folder / copy["file"]
        shutil.copyfile(copy["path"], target, follow_symlinks=False)
        os.chmod(target, 0o600)
    manifest = {"schema_version": SCHEMA_VERSION, "kind": f"{KIND}-backup", "files": files}
    (folder / "forgotten.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.chmod(folder / "forgotten.json", 0o600)
    records = {saved_observations.INVENTORY_NAME, saved_observations.BUDGET_NAME, saved_observations.CHANGES_NAME}
    for item in present:
        path = Path(item["path"])
        if item["name"] in records:
            with saved_observations.record_lock(path):
                path.unlink(missing_ok=True)
        else:
            path.unlink(missing_ok=True)
    return {**document, "result": "forgotten", "backup": str(folder),
            "removed": [item["path"] for item in present]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    planning = sub.add_parser("plan")
    planning.add_argument("--repo-root", type=Path, default=ROOT)
    for name in ("--topology", "--ssh-config", "--overlay"):
        planning.add_argument(name, type=Path, required=True)
    rendering = sub.add_parser("render")
    rendering.add_argument("plan", type=Path)
    rendering.add_argument("--services")
    rendering.add_argument("--checked-only-here", action="store_true")
    rendering.add_argument("--json", action="store_true")
    applying = sub.add_parser("apply")
    applying.add_argument("plan", type=Path)
    applying.add_argument("--services")
    applying.add_argument("--checked-only-here", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            document = plan(args.repo_root, args.topology, args.ssh_config, args.overlay)
            print(json.dumps(document, sort_keys=True))
            return 0
        document = json.loads(args.plan.read_text(encoding="utf-8"))
        document["services"] = _services(args.services)
        document["checked_only_here"] = bool(args.checked_only_here)
        if args.command == "render":
            if args.json:
                print(json.dumps(document, sort_keys=True))
            else:
                render(document)
            return 0
        result = apply(document, Path(document["backup_root"]))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        error_line(exc, tag="topology forget")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
