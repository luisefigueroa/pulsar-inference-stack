#!/usr/bin/env python3
"""Guided setup's numbered steps (`pulsar topology setup`, the menu's Set up
this cluster…): each machine's prerequisites, and what is still to fix.

    python3 scripts/setup_steps.py step N TITLE
    python3 scripts/setup_steps.py machine HOSTNAME [--probe FILE] [--prerequisites FILE]
                                   [--discovered] [--findings FILE]
    python3 scripts/setup_steps.py machines DISCOVERY_JSON
    python3 scripts/setup_steps.py summary --findings FILE

`machine` shows one machine: its platform facts (from the full probe of this
machine, or from discovery for another machine, which verified its Docker,
GPU and links to every other machine), then each prerequisite from
`probe-node.py --prerequisites-only` (scripts/prerequisites.py words them as
`pulsar doctor` does). A prerequisite that is missing is reported with its
fix and never blocks saving: only the platform facts decide whether a machine
can be a member, as discovery decides it. Each warning and blocking issue is
appended to FILE, and `summary` lists them at the end. `machines` prints
`SSH_HOST<TAB>HOSTNAME` for each machine discovery found other than this one.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.prerequisites import rows  # noqa: E402
from scripts.terminal_format import TerminalWriter  # noqa: E402

STEPS = 5
LABEL_WIDTH = 6
SHORT = {"python": None, "flock": "flock", "rsync": "rsync", "hf_cli": "hf CLI", "hf_login": "logged in to the Hub"}


def _load(path: str | None) -> dict | None:
    if not path:
        return None
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def machine(hostname: str, probe: dict | None, prerequisites: dict | None, *, discovered: bool,
            findings: Path | None, out: TerminalWriter, this_machine: bool = False) -> None:
    found: list[tuple[str, str]] = []
    if this_machine:
        # Named as discovery names it: the probe's hostname, when it answered.
        hostname = str((probe or {}).get("hostname") or (prerequisites or {}).get("hostname") or hostname)
    out.emit(f"{hostname} (this machine)" if this_machine else hostname, initial_indent="  ", role="heading")
    if discovered:
        out.emit("Docker and GPU ✓ · links to every other machine ✓", initial_indent="    ", subsequent_indent="    ")
    elif probe is None:
        found.append(("fail", "this machine's platform could not be checked; run `./pulsar doctor` for the reason"))
    elif probe.get("qualified") is True:
        out.emit("Docker and GPU access ✓ · RoCE links ✓", initial_indent="    ", subsequent_indent="    ")
    else:
        for reason in probe.get("reject_reasons") or ["this machine does not meet the platform requirements"]:
            found.append(("fail", f"{reason}; this machine cannot be a member until it is fixed"))
    if prerequisites is None or not isinstance(prerequisites.get("prerequisites"), dict):
        found.append(("warn", "prerequisites not checked: the machine did not answer the check; "
                              "**Check the cluster now** checks them again later"))
    else:
        passed = []
        for level, identifier, message in rows(prerequisites, noun="machine"):
            if level == "ok":
                passed.append(f"{SHORT.get(identifier) or message} ✓")
            else:
                found.append((level, message))
        if passed:
            out.emit(" · ".join(passed), initial_indent="    ", subsequent_indent="    ")
    for level, message in found:
        out.field(level.upper(), message, indent=4, label_width=LABEL_WIDTH, label_role=level, markup=True)
    if findings is not None and found:
        with findings.open("a", encoding="utf-8") as stream:
            for level, message in found:
                stream.write(json.dumps({"machine": hostname, "level": level, "message": message}) + "\n")


def summary(findings: Path, out: TerminalWriter) -> None:
    try:
        items = [json.loads(line) for line in findings.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        items = []
    out.emit("Still to fix", role="heading")
    if not items:
        out.emit("Nothing: every machine has its prerequisites.", initial_indent="  ")
        return
    for item in items:
        out.field(item["level"].upper(), f"{item['machine']}: {item['message']}", indent=2, label_width=LABEL_WIDTH,
                  label_role=item["level"], markup=True)
    out.emit("Warnings do not stop serving: a machine without the hf CLI or a Hub login can still serve, and "
             "only a model downloaded to it needs them. **Check the cluster now**, under **Cluster…**, checks "
             "them again.", markup=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    step = sub.add_parser("step")
    step.add_argument("number", type=int)
    step.add_argument("title")
    one = sub.add_parser("machine")
    one.add_argument("hostname")
    one.add_argument("--probe")
    one.add_argument("--prerequisites")
    one.add_argument("--discovered", action="store_true")
    one.add_argument("--this-machine", action="store_true")
    one.add_argument("--findings", type=Path)
    found = sub.add_parser("machines")
    found.add_argument("discovery")
    last = sub.add_parser("summary")
    last.add_argument("--findings", type=Path, required=True)
    args = parser.parse_args(argv)
    out = TerminalWriter()
    if args.command == "step":
        out.blank()
        out.emit(f"Step {args.number} of {STEPS} · {args.title}", role="heading")
    elif args.command == "machine":
        machine(args.hostname, _load(args.probe), _load(args.prerequisites), discovered=args.discovered,
                findings=args.findings, out=out, this_machine=args.this_machine)
    elif args.command == "machines":
        document = _load(args.discovery) or {}
        topology = document.get("topology") if isinstance(document.get("topology"), dict) else document
        for node in (topology or {}).get("nodes") or []:
            if node.get("ssh_host") and node.get("ssh_host") != "local" and node.get("hostname"):
                print(f"{node['ssh_host']}\t{node['hostname']}")
    else:
        summary(args.findings, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
