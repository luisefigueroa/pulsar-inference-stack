"""Catalog projection from published specs and saved managed-file observations.

Reading this module never probes hardware, scans cache directories or mutates
controller state. Only an explicit models check refreshes operational observations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import sys

from release_spec import load_spec
from .integrity import StorageError
from .node_names import NodeNames
from .state import Store, checked_id
from scripts.terminal_format import TerminalWriter, emit_help

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


# Human wording for saved observations. Saved records show their age; a spec
# that was never checked is unknown, never absent.
FILE_STATES = {"ready": "prepared on every rank", "missing": "not prepared on every rank",
    "changed": "changed since verification", "unknown": "unknown"}
SNAPSHOT_FILE_STATES = {"ready": "prepared", "missing": "not prepared", "changed": "changed",
    "unknown": "unknown"}
# A flag token: one or two dashes and a letter, so "-1" can still be a value.
FLAG = re.compile(r"^--?[A-Za-z]")
LIST_LABEL_WIDTH = 11
DETAILS_LABEL_WIDTH = 13


def _checked(age):
    return f"checked {age_text(age)}" if age is not None else "check time unknown"


def _verified(age):
    return f"verified {age_text(age)}" if age is not None else "verified (age unknown)"


def files_text(row):
    """The saved preparation state of the complete recipe with the check's age."""
    if not row.get("checked_at"):
        return "unknown: never checked"
    return f"{FILE_STATES[row['local_state']]} ({_checked(row.get('observation_age_seconds'))})"


def archive_fact(state, check_age, record, record_age, *, checked=True):
    """The strongest established archive fact as (kind, age, wording).

    ``state`` is the saved check's archive_state and ``check_age`` its age;
    ``record`` is the archive verification record and ``record_age`` the age
    of its verified_at. A verification outranks a check that only saw the
    archive present or learned nothing, and any check older than it. An older
    verification stays visible next to what a later check found. Unobserved
    state is unknown, never absent.
    """
    newer = (record is not None and record_age is not None and check_age is not None
             and record_age < check_age)
    if record is not None and (state in ("verified", "present", "unknown") or newer or not checked):
        age = record_age
        if state == "verified" and check_age is not None and (age is None or check_age < age):
            age = check_age
        return "verified", age, _verified(age)
    if state == "verified":
        return "verified", check_age, _verified(check_age)
    before = ""
    if record is not None:
        order = (" before that" if record_age is not None and check_age is not None and record_age > check_age
                 else "; order relative to check unknown")
        before = f" ({_verified(record_age)}{order})"
    if state == "present":
        return "present", check_age, "present at last check, not verified"
    if state == "missing":
        return "missing", check_age, "not found at last check" + before
    if state == "unavailable":
        return "unavailable", check_age, "unavailable at last check" + before
    if state == "not-configured":
        return "not-configured", check_age, "archive location not configured at last check" + before
    if not checked:
        return "unknown", None, "unknown: never checked"
    return "unknown", check_age, f"unknown ({_checked(check_age)})"


def archive_facts(row, now=None):
    """The archive fact of every required snapshot; schema 2 has one unnamed snapshot."""
    checked = bool(row.get("checked_at"))
    check_age = row.get("observation_age_seconds")
    if not isinstance(row.get("snapshots"), dict):
        return {None: archive_fact(row.get("archive_state", "unknown"), check_age, row.get("archive"),
                                   row.get("archive_age_seconds"), checked=checked)}
    facts = {}
    for name, member in row["snapshots"].items():
        observation = member.get("observation") or {}
        record = member.get("archive")
        record_age = age_seconds(record.get("verified_at"), now) if record else None
        facts[name] = archive_fact(observation.get("archive_state", "unknown"), check_age, record,
                                   record_age, checked=checked and bool(observation))
    return facts


def archive_text(row, now=None):
    """One archive line for the recipe; snapshots that differ are named."""
    facts = archive_facts(row, now)
    wordings = [text for _, _, text in facts.values()]
    if len(set(wordings)) == 1:
        return wordings[0]
    if all(kind == "verified" for kind, _, _ in facts.values()):
        ages = [age for _, age, _ in facts.values()]
        # The oldest verification is the weakest link.
        return _verified(None if None in ages else max(ages))
    return "; ".join(f"{name} {text}" for name, (_, _, text) in facts.items())


def recipe_text(geometry):
    nodes = geometry["nodes"]
    text = f"{nodes} node{'' if nodes == 1 else 's'} · tensor parallel {geometry['tp']}"
    if geometry.get("pp", 1) != 1:
        text += f" · pipeline parallel {geometry['pp']}"
    return text


def archive_location_status(environ=None):
    """Archive location status as model-storage.sh reports it from PULSAR_COLD_ROOT."""
    environ = os.environ if environ is None else environ
    if "PULSAR_COLD_ROOT" not in environ:
        return "not-configured"
    return "configured" if environ["PULSAR_COLD_ROOT"] else "disabled"


def argument_groups(tokens):
    """Shell-quoted tokens with each --flag paired with its value."""
    tokens = [str(token) for token in tokens]
    groups, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        if (FLAG.match(token) and "=" not in token and index + 1 < len(tokens)
                and not FLAG.match(tokens[index + 1])):
            groups.append(f"{shlex.quote(token)} {shlex.quote(tokens[index + 1])}")
            index += 2
        else:
            groups.append(shlex.quote(token))
            index += 1
    return groups


def group_lines(groups, first_width, next_width):
    """Fill lines with whole groups: a flag and its value share a line."""
    lines = []
    for group in groups:
        width = first_width if not lines else next_width
        if lines and len(lines[-1]) + 1 + len(group) <= width:
            lines[-1] += " " + group
        else:
            lines.append(group)
    return lines


def _groups_field(out, label, groups, *, label_width):
    prefix = "  " + label.ljust(label_width)
    pad = " " * len(prefix)
    available = max(1, out.width - len(prefix))
    first = True
    for line in group_lines(groups, available, available):
        # Only a group longer than a whole line breaks, at the width.
        for start in range(0, len(line), available):
            print((prefix if first else pad) + line[start:start + available], file=out.stream)
            first = False


def _command_field(out, label, tokens, *, label_width):
    """A pasteable command; a wrapped line ends with a shell continuation."""
    prefix = "  " + label.ljust(label_width)
    continuation = " " * (len(prefix) + 2)
    lines = group_lines(argument_groups(tokens), out.width - len(prefix) - 2,
                        out.width - len(continuation) - 2)
    for index, line in enumerate(lines):
        end = " \\" if index < len(lines) - 1 else ""
        print((prefix if index == 0 else continuation) + line + end, file=out.stream)


def _heading(out, row):
    title = f"{row['model_id']}   spec {row['spec_id'][:12]}"
    if len(title) <= out.width:
        out.emit(title)
    else:
        out.emit(row["model_id"], break_on_hyphens=True)
        out.emit(f"spec {row['spec_id'][:12]}", initial_indent="  ")


def _details(out, row, names, label_width):
    def field(label, value):
        out.field(label, value, indent=2, label_width=label_width)

    pad = " " * (2 + label_width)
    field("Spec ID", row["spec_id"])
    field("Model commit", row["snapshot_revision"])
    field("Manifest", row["snapshot_manifest_id"])
    out.emit("Image", initial_indent="  ")
    # An identifier to copy whole: never wrapped, even past the width.
    print("    " + str(row["image"].get("digest")), file=out.stream)
    _groups_field(out, "Arguments", argument_groups(row["engine_args"]), label_width=label_width)
    snapshots = row["snapshots"] if isinstance(row.get("snapshots"), dict) else {}
    homes = ([(f"{name}: ", member.get("home")) for name, member in snapshots.items()]
             or [("", row.get("home"))])
    if not any(home for _, home in homes):
        field("Home", "none recorded")
    else:
        for index, (name, home) in enumerate(homes):
            where = name + (names(home["node_id"]) if home else "none recorded")
            if index == 0:
                field("Home", where)
            else:
                out.emit(where, initial_indent=pad, subsequent_indent=pad)
            if home:
                out.emit(home["path"], initial_indent=pad, subsequent_indent=pad)
    copies = sorted(row.get("prepared_copies") or [], key=lambda view: view["rank"])
    if not copies:
        out.field("Prepared copies", "none recorded", indent=2, label_width=label_width)
        return
    out.emit("Prepared copies", initial_indent="  ")
    snapshot_names = {member["snapshot_manifest_id"]: name for name, member in snapshots.items()}
    for view in copies:
        parts = [names(view["node_id"])]
        if view.get("snapshot_manifest_id") in snapshot_names:
            parts.append(snapshot_names[view["snapshot_manifest_id"]])
        parts.append("pinned" if view.get("pinned") else "not pinned")
        rank = f"rank {view['rank']}  "
        hanging = " " * (4 + len(rank))
        out.emit(" · ".join(parts), initial_indent="    " + rank, subsequent_indent=hanging)
        out.emit(view["path"], initial_indent=hanging, subsequent_indent=hanging)


def render(rows, *, details=False, writer=None, names=None, location=None, now=None):
    """One compact block per spec that leads with saved state.

    ``location`` is the archive location status (configured, disabled or
    not-configured; default from PULSAR_COLD_ROOT) that decides the suggested
    next step. Details add identity, arguments, homes and prepared copies.
    """
    from .catalog_menu import suggested_command
    out = writer or TerminalWriter()
    names = names or NodeNames()
    location = location or archive_location_status()
    label_width = DETAILS_LABEL_WIDTH if details else LIST_LABEL_WIDTH

    def field(label, value):
        out.field(label, value, indent=2, label_width=label_width)

    if not rows:
        out.emit("The catalog is empty.")
        out.emit("Specs appear when the maintainer publishes them under releases/.")
        return
    for number, row in enumerate(rows):
        if number:
            out.blank()
        _heading(out, row)
        field("Recipe", recipe_text(row["geometry"]))
        startable = row.get("start_supported") is not False
        if not startable:
            reason = row.get("start_unsupported_reason")
            field("Start", f"not supported by this Stack ({START_UNSUPPORTED_LABELS.get(reason, reason)})")
        if row.get("historical"):
            out.emit("Historical spec: create a schema-2 spec for future operations.",
                     initial_indent="  ", subsequent_indent="  ")
        review = row.get("review") or {}
        if row.get("state"):
            field("State", row["state"])
        if review.get("status"):
            field("Review", review["status"])
        if review.get("status") == "withdrawn":
            field("Reason", review.get("reason") or "No reason recorded")
            out.emit("Withdrawn recipes are not recommended."
                     + (" Exact serving remains possible when operational checks pass." if startable else ""),
                     initial_indent="  ", subsequent_indent="  ")
        field("Files", files_text(row))
        field("Archive", archive_text(row, now))
        if details and isinstance(row.get("snapshots"), dict):
            facts = archive_facts(row, now)
            out.emit("Snapshots", initial_indent="  ")
            for name, member in row["snapshots"].items():
                files = SNAPSHOT_FILE_STATES[(member.get("observation") or {}).get("local_state", "unknown")]
                out.emit(f"{name} snapshot {member['model_id']} @ {str(member['model_commit'])[:8]}: "
                         f"files {files} · archive {facts[name][2]}",
                         initial_indent="    ", subsequent_indent="      ", break_on_hyphens=True)
        if details:
            for blocker in row["blockers"]:
                field("Blocker", names.prefixed(blocker) if isinstance(blocker, str) else blocker)
        command = suggested_command(row, location, names, now=now)
        if command:
            _command_field(out, "Suggested", command, label_width=label_width)
        if details:
            _details(out, row, names, label_width)
    out.blank()
    if details and len(rows) == 1:
        out.emit("Saved records: locations are not proof that files are intact now. "
                 f"./pulsar models check {rows[0]['spec_id'][:12]} refreshes them; start rechecks everything.")
    else:
        out.emit("Saved records. ./pulsar models check SPEC refreshes them; start rechecks everything.")


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


HELP = """\
usage: pulsar models list [--json]
       pulsar models menu [--read-only]
       pulsar models show SPEC [--json]
       pulsar models check SPEC [--node NODE]

Browse catalog specs with their saved file and archive state; only check contacts nodes.

  list        Every catalog spec: recipe, files, archive and the suggested next step
  show SPEC   One spec in detail: identity, image, engine arguments, home, prepared copies and blockers
  check SPEC  Check the spec's managed files and archive and save the result; see pulsar model --help
  menu        Open the catalog menu; it needs an interactive terminal with Gum
  --read-only  Browse saved catalog details without offering operations (menu only)
  --json      Print list or show as JSON

Without a command, a terminal opens the menu and other callers get the list. SPEC is a catalog spec ID; people may type a unique prefix of at least 12 characters.
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        emit_help(HELP)
        return 0
    parser = argparse.ArgumentParser(prog="pulsar models", usage="pulsar models [list|show SPEC] [--json]",
                                     add_help=False)
    parser.add_argument("command", choices=("list", "show"), nargs="?", default="list")
    parser.add_argument("spec_id", nargs="?")
    parser.add_argument("--json", action="store_true")
    # Internal: model-storage.sh and tests select the catalog and state roots.
    parser.add_argument("--repo-root", default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--state-root", default=os.environ.get("PULSAR_MODEL_LIBRARY_DIR", os.environ.get("MODEL_LIBRARY_DIR", str(ROOT / ".model-library"))),
                        help=argparse.SUPPRESS)
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
