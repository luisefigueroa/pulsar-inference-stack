#!/usr/bin/env python3
"""Models and disk space… → Download a model that has no recipe yet…: the
nodes it can go to, its plan from the command's own preview, and its outcome.

    python3 -m scripts.download_page nodes --files FILE
    python3 -m scripts.download_page plan --model ORG/NAME --ref REF --host HOST
            --preview FILE [--preview-rc N] --out FACTS
    python3 -m scripts.download_page outcome --model ORG/NAME --commit COMMIT --host HOST
            --action download|reuse --rc N [--manifest FILE] [--kept] COMMAND...

The plan is what `./pulsar model acquire --model-id ORG/NAME --model-commit REF
--node HOST --plan --json` answered: this module contacts nothing and decides
nothing about the files. The command refuses a second copy before it downloads
(a home on another node, or an archive that holds the files), and its refusal
is shown as it is. `plan` prints the screen and writes FACTS, one line of
ACTION, COMMIT, MANIFEST, SAVE (1 when the run saves the manifest) and the
choice's label, split by unit separators (0x1F); ACTION is `blocked` when
nothing can run.

The manifest of the files (their list with sizes and hashes) is saved under
`.pulsar/manifests/` of the checkout, which Git ignores. A recipe written later
for the model takes it as its snapshot manifest, and `model remove` and `model
archive create` select the files with it while no recipe names them. A file
already there is kept, never replaced.
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_library.catalog_menu import clean  # noqa: E402
from model_library.file_inventory import _unread  # noqa: E402
from model_library.render import short_size  # noqa: E402
from scripts.serve_plan import envelope_result, load_json  # noqa: E402
from scripts.terminal_format import MAX_WIDTH, MIN_WIDTH, TerminalWriter, terminal_width  # noqa: E402

TITLE = "Download a model that has no recipe yet"
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
COMMIT = re.compile(r"[0-9a-f]{40}")
# A no-break space keeps a size together when a sentence wraps; it prints as a space.
KEEP = " "


def manifest_path(repo_root: Path, model: str, commit: str) -> Path:
    """Where this menu saves the manifest of MODEL at COMMIT."""
    return Path(repo_root) / ".pulsar" / "manifests" / f"{model.replace('/', '--')}@{commit[:12]}.json"


def nodes(files: dict | None) -> list[str]:
    """The hostnames of the nodes that answered the page's scan, in its order."""
    found = []
    for node in (files or {}).get("nodes") or []:
        if isinstance(node, dict) and node.get("hostname") and not _unread(node):
            found.append(clean(node["hostname"]))
    return found


def _source(document: dict) -> tuple[int | None, int | None]:
    files = (document.get("source") or {}).get("files")
    if not isinstance(files, list):
        return None, None
    sizes = [item.get("size") for item in files if isinstance(item, dict)]
    whole = len(sizes) == len(files) and all(type(size) is int and size >= 0 for size in sizes)
    return len(files), sum(sizes) if whole else None


def plan(preview, rc: int, *, model: str, ref: str, host: str, repo_root: Path, width: int | None = None
         ) -> tuple[str, dict]:
    """(screen, facts) from the command's own preview. facts: action, commit, manifest, save, label."""
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    out.emit(TITLE, role="heading")
    out.blank()
    facts = {"action": "blocked", "commit": "", "manifest": "", "save": 0, "label": ""}
    document, error = envelope_result(preview)
    commit = str((document or {}).get("snapshot_revision") or "")
    action = (document or {}).get("action")
    if rc != 0 or not isinstance(document, dict) or document.get("kind") != "pulsar-acquisition-plan" \
            or action not in ("download", "reuse") or not COMMIT.fullmatch(commit):
        reason = clean((error or {}).get("message") or (f"its preview exited {rc}" if rc else
                                                        "its preview could not be read"))
        out.emit(f"{model} at {ref} was not downloaded to {host}: {reason}.".replace("..", "."),
                 "✗ ", "  ", prefix_role="fail")
        out.emit("Nothing was changed.", "  ", "  ")
        return output.getvalue().replace(KEEP, " "), facts
    count, total = _source(document)
    size = short_size(total).replace(" ", KEEP) if total is not None else "size unknown"
    amount = f"{count:,} files, {size}" if count is not None else size
    what = f"{model} at commit {commit[:12]}"
    saved = manifest_path(repo_root, model, commit)
    kept = saved.exists() or saved.is_symlink()
    out.emit("Pulsar will:")
    if action == "download":
        out.emit(f"Download {what} from Hugging Face to {host} ({amount}) and check the SHA-256 of every file. "
                 "The files become its home, the copy Pulsar keeps.", "  1. ", "     ")
        facts["label"] = f"Download {size.replace(KEEP, ' ')} to {host}"
    else:
        out.emit(f"Check the files of {what} that are already on {host} against the file list on Hugging Face "
                 "and keep them as its home, the copy Pulsar keeps. Nothing is downloaded.", "  1. ", "     ")
        facts["label"] = f"Keep the files found on {host}"
    if kept:
        out.emit("Leave the list of its files and their hashes (its manifest) as it is. This file exists and is "
                 "never replaced:", "  2. ", "     ")
    else:
        out.emit("Save the list of its files and their hashes (its manifest). A recipe written later for this "
                 "model starts from this file:", "  2. ", "     ")
    # A path on a line of its own is never broken inside, so it can be copied.
    out.emit_command(shlex.quote(str(saved)), "     ", "       ")
    out.blank()
    search = document.get("archive_search")
    if isinstance(search, dict) and search.get("state") == "searched":
        out.emit("The archive was searched and holds no copy of these files.")
    elif isinstance(search, dict) and search.get("state") == "unreadable":
        out.emit("The archive location could not be read, so whether it holds these files is unknown.",
                 "Not checked: ", "  ", prefix_role="warn")
    out.emit("Nothing is prepared for serving and nothing starts.")
    facts.update(action=action, commit=commit, manifest=str(saved), save=0 if kept else 1)
    return output.getvalue().replace(KEEP, " "), facts


def outcome(*, model: str, commit: str, host: str, action: str, rc: int, manifest: str | None, kept: bool,
            command: list[str], width: int | None = None) -> str:
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    what = f"{model} at commit {commit[:12]}"
    if rc == 0:
        out.emit(f"Done: {what} is on {host}" + (", downloaded and checked." if action == "download" else
                                                 "; nothing was downloaded."), "✓ ", "  ")
        if manifest:
            out.emit(f"Its manifest {'was already' if kept else 'is'} in this file:", "  ", "  ")
            out.emit_command(shlex.quote(manifest), "    ", "      ")
            out.blank()
            out.emit("No recipe uses these files yet, so this page cannot back them up or remove them. These "
                     "commands preview that:")
            chosen = shlex.quote(manifest)
            out.emit_command(f"./pulsar model archive create --manifest {chosen} --plan", "  ", "    ")
            out.emit_command(f"./pulsar model remove --manifest {chosen} --plan", "  ", "    ")
    elif rc in (3, 130) or 128 < rc < 160:
        out.emit(f"The download of {what} to {host} was interrupted. Its outcome is not confirmed; choose this "
                 "again to see what is there before any retry.", "✗ ", "  ", prefix_role="fail")
    else:
        out.emit(f"{what} was not put on {host}. The reason is above.", "✗ ", "  ", prefix_role="fail")
    out.blank()
    out.emit("Same as:")
    out.emit_command(" ".join(shlex.quote(part) for part in command), "  ", "    ")
    return output.getvalue().replace(KEEP, " ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("nodes").add_argument("--files", required=True)
    for name in ("plan", "outcome"):
        command = sub.add_parser(name)
        command.add_argument("--model", required=True)
        command.add_argument("--host", required=True)
    planner, result = sub.choices["plan"], sub.choices["outcome"]
    planner.add_argument("--ref", required=True)
    planner.add_argument("--preview", required=True)
    planner.add_argument("--preview-rc", type=int, default=0)
    planner.add_argument("--out", required=True)
    result.add_argument("--commit", required=True)
    result.add_argument("--action", choices=("download", "reuse"), required=True)
    result.add_argument("--rc", type=int, required=True)
    result.add_argument("--manifest")
    result.add_argument("--kept", action="store_true")
    result.add_argument("ran", nargs="+")
    for command in sub.choices.values():
        command.add_argument("--repo-root", type=Path, default=ROOT)
        command.add_argument("--width", type=int)
    args = parser.parse_args(argv)
    width = max(MIN_WIDTH, min(MAX_WIDTH, args.width or terminal_width()))
    if args.command == "nodes":
        from scripts.models_page import files_document
        print("\n".join(nodes(files_document(args.files))))
        return 0
    if not MODEL.fullmatch(args.model):
        print("error: the model must be ORG/NAME", file=sys.stderr)
        return 2
    if args.command == "plan":
        if not REF.fullmatch(args.ref):
            print("error: the commit, branch or tag has a character that is not allowed", file=sys.stderr)
            return 2
        screen, facts = plan(load_json(args.preview), args.preview_rc, model=args.model, ref=args.ref,
                             host=args.host, repo_root=args.repo_root, width=width)
        Path(args.out).write_text("\x1f".join(str(facts[key]) for key in ("action", "commit", "manifest", "save",
                                                                        "label")) + "\n", encoding="utf-8")
        sys.stdout.write(screen)
        return 0
    sys.stdout.write(outcome(model=args.model, commit=args.commit, host=args.host, action=args.action, rc=args.rc,
                             manifest=args.manifest, kept=args.kept, command=args.ran, width=width))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
