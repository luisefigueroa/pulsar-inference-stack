#!/usr/bin/env python3
"""Running services…: what runs, how to reach it, and what the page offers.

    python3 -m scripts.services_page services|names --inventory FILE < CATALOG_JSON
    python3 -m scripts.services_page screen --inventory FILE [--conf SPEC] [--status FILE --status-rc N
            --status-at EPOCH] [--width N] < CATALOG_JSON
    python3 -m scripts.services_page choices --inventory FILE [--conf SPEC] [--status FILE] < CATALOG_JSON
    python3 -m scripts.services_page command --inventory FILE --conf SPEC --action ACTION [--shown] < CATALOG_JSON
    python3 -m scripts.services_page question --inventory FILE (--conf SPEC | --all) < CATALOG_JSON
    python3 -m scripts.services_page outcome --action stop|stop-all --rc N [--name NAME] --shown TEXT
    python3 -m scripts.services_page fresh --inventory FILE [--seconds N]

Every fact comes from one service inventory (`./pulsar inventory --json`),
taken on entering the page or reused while it is under a minute old, and from
the last **Check it now** of a service (`./pulsar status`), each with its
source and age. The inventory holds each service's model, state, nodes, port
and served model name; it holds no API address, key requirement or health,
so those read "not checked here" until Check it now, and nothing is guessed.
A node the inventory did not observe makes what runs there unknown, never
absent. Nothing here contacts a node; the menu (scripts/services-menu.sh)
runs every command.

`services` prints `CONF<TAB>KIND` lines (KIND `catalog` or `outside`), in the
order the page lists them: services with a running container first. `choices`
prints `ACTION<TAB>LABEL` lines; `command` prints a command one argument per
line, or how the outcome names it with --shown; `question` prints a Stop
question.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sys
import textwrap
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_library.catalog import age_seconds, age_text  # noqa: E402
from model_library.catalog_menu import OUTSIDE_CATALOG, start_unsupported, stop_question  # noqa: E402
from model_library.node_names import NodeNames  # noqa: E402
from model_library.render import short_size  # noqa: E402
from scripts.cluster_status import ACTIVE_STATES, KEEP, service_name  # noqa: E402
from scripts.inventory_menu import (gpu_process_parts, project, project_others, read_only_line,  # noqa: E402
                                    stop_all_question, unobserved_hosts)
from scripts.terminal_format import MAX_WIDTH, MIN_WIDTH, TerminalWriter, terminal_width  # noqa: E402

LABEL_WIDTH = 10
# Reused while younger than this, as Serve a model… reuses its capture.
FRESH_SECONDS = 60
# Containers Pulsar did not start are named up to this many, then counted.
READ_ONLY_ROWS = 3

# The words this page prints about a service. Each has a row under Catalog
# labels in docs/OPERATIONS.md (Services: …); tests/test_help.py checks it.
STATUS_WORDS = {
    "seen": "by the service inventory",
    "not-checked": "not checked here; choose Check it now",
    "not-in-inventory": "not in the service inventory; choose Check it now",
    "served-name": "the model name requests use",
    "no-key": "none: the API accepts requests without a key",
    "key": "required: requests send the key the service was started with",
    "key-not-recorded": "not recorded: add the key header if the service was started with one",
    "answered": "answered /health",
    "did-not-answer": "did not answer /health",
    "no-address": "running, with no API address to ask",
    "not-found": "Check it now found no service",
    "could-not-tell": "Check it now could not tell",
    "not-observed": "did not answer the service inventory",
    "none-running": "No Pulsar service is running",
    "none-seen": "No Pulsar service was seen",
    "gpu": "processes Pulsar did not start",
    "read-only": "read-only",
    "outside": "Started outside this catalog, for example by an experiment",
    "not-offered": "not offered",
}
# A no-break space keeps a phrase together when a line wraps; it prints as a space.


def _plain(text: str) -> str:
    return text.replace(KEEP, " ")


def _list(names: list[str]) -> str:
    names = [str(name) for name in names]
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def clean(value: Any) -> str:
    return " ".join(str(value).split())


def load_json(path: str | None) -> Any:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The services one inventory shows
# ---------------------------------------------------------------------------

class Page:
    """One service inventory joined to the catalog, with the last Check it now of one service."""

    def __init__(self, inventory: dict, entries: list[dict], *, names: NodeNames | None = None,
                 now: float | None = None, status: dict | None = None):
        self.inventory = inventory
        self.entries = entries
        self.names = names or NodeNames()
        self.now = now if now is not None else time.time()
        self.status = status or {}
        catalog = [{"spec_id": row["spec_id"], "model_id": row["model_id"], "nodes": row["geometry"]["nodes"]}
                   for row in entries]
        self.catalog = catalog
        # project validates the document; a malformed inventory raises ValueError.
        self.services = project(inventory, catalog)
        self.others = project_others(inventory, catalog)
        self.unobserved = unobserved_hosts(inventory)
        worker = inventory.get("worker") or {}
        self.blind = not self.unobserved and worker.get("status") not in ("ok", "unset")

    @property
    def age(self) -> str:
        return age_text(age_seconds(self.inventory.get("generated_at"), _instant(self.now)))

    def order(self) -> list[tuple[str, str]]:
        """(CONF, KIND) of every Pulsar service, those with a running container first."""
        rows = [(conf, "catalog", service) for conf, service in self.services.items()]
        rows += [(conf, "outside", service) for conf, service in self.others.items()]
        return ([(conf, kind) for conf, kind, service in rows if service["state"] in ACTIVE_STATES]
                + [(conf, kind) for conf, kind, service in rows if service["state"] not in ACTIVE_STATES])

    def service(self, conf: str) -> tuple[dict, bool]:
        """The projected service and whether it is outside the catalog."""
        if conf in self.services:
            return self.services[conf], False
        if conf in self.others:
            return self.others[conf], True
        raise ValueError("that service is no longer in the service inventory")

    def raw(self, conf: str) -> dict:
        return next((item for item in self.inventory["services"] if isinstance(item, dict)
                     and item.get("conf") == conf), {})

    def row(self, conf: str) -> dict | None:
        return next((row for row in self.entries if row["spec_id"] == conf), None)

    def name(self, conf: str, width: int) -> list[str]:
        """The service's one name, as home's Serving line has it, in lines of WIDTH:
        a model longer than the line is cut, keeping its end, and the rest wraps
        after it."""
        service, outside = self.service(conf)
        room = max(12, width - 4)
        text = service_name(service, room, outside=outside and bool(self.entries))
        lines = textwrap.wrap(text, width, subsequent_indent="  ", break_long_words=False,
                              break_on_hyphens=False)
        return [_plain(line) for line in lines] or [_plain(text)]

    def label(self, conf: str, width: int) -> str:
        """The name as a list choice: one line when it fits WIDTH (the row after
        Gum's cursor), else a card whose lines are given at screen columns,
        joined by the unit separator for the menu to turn into newlines."""
        lines = self.name(conf, width - 2)
        if len(lines) == 1:
            return lines[0]
        return "\x1f".join("  " + line for line in lines)

    # -- what the page can do for one service ---------------------------------

    def restart_blocker(self, conf: str) -> str | None:
        """Why Restart… is not offered, or None: it needs a catalog recipe this
        Stack can start, unmodified, that Stop can remove, on the nodes Start
        would use again."""
        service, outside = self.service(conf)
        row = self.row(conf)
        if outside or row is None:
            return "it was started outside this catalog"
        if service["stop_reason"]:
            return "Pulsar cannot stop it"
        if service.get("modified"):
            return "it was started from a modified recipe"
        if start_unsupported(row):
            return "this Stack cannot start its recipe"
        nodes = row["geometry"]["nodes"]
        if nodes == 1:
            if not service.get("node_id"):
                return "the node it runs on was not identified"
            return None
        order = self.names.order
        serving = [self.names(node) for node in order[:nodes]] if len(order) >= nodes else []
        if list(service["hosts"]) != serving:
            return (f"it runs on {_list(service['hosts'])}, but Start serves it on "
                    f"{_list(serving) or 'nodes the cluster does not have'}")
        return None

    def choices(self, conf: str | None) -> list[tuple[str, str]]:
        """The page's choices: for one service, or for the list or nothing."""
        everything = ([("stop-all", "Stop every service…")]
                      if stop_all_question(self.inventory, {row["spec_id"]: row for row in self.catalog}) else [])
        if conf is None:
            # The list's services come first, by their names (label).
            if not self.order():
                return [("serve", "Serve a model…"), ("again", "Look again"), ("back", "Back")]
            return everything + [("back", "Back")]
        service, outside = self.service(conf)
        found = []
        if not outside:
            found.append(("check", "Check it now"))
            if service["state"] == "running":
                found.append(("connect", "How to connect (a first request to copy)"))
        found.append(("logs", "Logs"))
        if not outside and self.restart_blocker(conf) is None:
            found.append(("restart", "Restart…"))
        if not outside and not service["stop_reason"]:
            found.append(("switch", "Switch to another model…"))
        if not service["stop_reason"]:
            found.append(("stop", "Stop…"))
        return found + everything + [("back", "Back")]

    def command(self, conf: str, action: str, *, shown: bool = False) -> list[str]:
        """The command behind ACTION; SHOWN names a catalog spec by its 12-character
        prefix and a node by its hostname, as the outcome's Same as does."""
        service, outside = self.service(conf)
        spec = conf[:12] if shown and not outside else conf
        node = service.get("node_id") if not outside else None
        place = ["--node", self.names(node) if shown else node] if node else []
        if action == "check":
            return ["./pulsar", "status", spec, *place]
        if action == "logs":
            return ["./pulsar", "logs", spec]
        if action == "stop":
            return ["./pulsar", "stop", spec, *place]
        if action == "restart":
            # Its one node, when it serves on one: Start uses the saved order otherwise.
            return ["--restart", conf, *(["--node", node] if node else [])]
        if action == "switch":
            return ["--switch-from", conf]
        if action == "model":
            return [clean(service["model_id"])]
        raise ValueError(f"no command for {action}")

    def question(self, conf: str | None) -> str | None:
        """Stop's question for one service, or stop --all's naming each service it removes."""
        if conf is None:
            return stop_all_question(self.inventory, {row["spec_id"]: row for row in self.catalog})
        service, outside = self.service(conf)
        if service["stop_reason"]:
            return None
        question = stop_question(clean(service["model_id"]), conf, service["where"])
        return f"{question} {OUTSIDE_CATALOG}" if outside else question

    # -- the screens ----------------------------------------------------------

    def screen(self, conf: str | None, *, width: int | None = None, heading: bool = True) -> str:
        output = io.StringIO()
        out = TerminalWriter(width=width, stream=output)

        def field(label: str, value: str, role: str | None = None) -> None:
            out.field(label, value, label_width=LABEL_WIDTH, label_role=role, markup=True)

        listed = self.order()
        if conf is None and not listed:
            # Only a complete inventory can say that nothing runs.
            if self.unobserved:
                out.emit(f"{STATUS_WORDS['none-seen']} on the nodes that answered. "
                         f"{_list(self.unobserved)} {STATUS_WORDS['not-observed']}, so what runs there is unknown, "
                         "not absent.", role="heading")
            elif self.blind:
                out.emit(f"{STATUS_WORDS['none-seen']}, but a node could not be observed, so what runs there is "
                         "unknown, not absent.", role="heading")
            else:
                out.emit(f"{STATUS_WORDS['none-running']}.", role="heading")
            field("Seen", f"{STATUS_WORDS['seen']} {self.age}")
        elif conf is None:
            count = len(listed)
            out.emit(f"{count} Pulsar services", role="heading")
            field("Seen", f"{STATUS_WORDS['seen']} {self.age}")
            self._unknown(field)
        else:
            self._card(conf, out, field, heading=heading)
            self._unknown(field)
        self._others(field)
        return _plain(output.getvalue())

    def _unknown(self, field) -> None:
        if self.unobserved:
            field("Not seen", f"{_list(self.unobserved)} {STATUS_WORDS['not-observed']}, so what runs there is "
                              "unknown, not absent", "warn")
        elif self.blind:
            field("Not seen", "a node could not be observed, so what runs there is unknown, not absent", "warn")

    def _others(self, field) -> None:
        """GPU work and containers Pulsar did not start: named, read-only, never offered an action."""
        count, hosts = gpu_process_parts(self.inventory)
        if count:
            parts = [f"{count} {STATUS_WORDS['gpu'] if count != 1 else 'process Pulsar did not start'}"]
            parts += [f"{short_size(size)} on {host}" for host, size in hosts]
            field("GPUs", f"also used by {' · '.join(parts)} ({STATUS_WORDS['read-only']})")
        mine = set(self.services) | set(self.others)
        read_only = [item for item in self.inventory["services"] if isinstance(item, dict)
                     and item.get("conf") not in mine]
        for index, item in enumerate(read_only[:READ_ONLY_ROWS]):
            field("Others" if index == 0 else "", read_only_line(item))
        if len(read_only) > READ_ONLY_ROWS:
            field("", f"and {len(read_only) - READ_ONLY_ROWS} more; ./pulsar inventory lists them")

    def _card(self, conf: str, out: TerminalWriter, field, *, heading: bool) -> None:
        service, outside = self.service(conf)
        if heading:
            for line in self.name(conf, out.width):
                out.emit(line, role="heading")
        field("Seen", f"{STATUS_WORDS['seen']} {self.age}")
        active = service["state"] in ACTIVE_STATES
        raw = self.raw(conf)
        result, rc, at = self.status.get("result"), self.status.get("rc"), self.status.get("at")
        checked = rc is not None and at is not None
        when = age_text(max(0, int(self.now - at))) if checked else None
        if outside:
            field("Model", f"`{clean(raw['served_name'])}`, {STATUS_WORDS['served-name']}"
                  if raw.get("served_name") else "served name not recorded in the service inventory")
            field("", f"{STATUS_WORDS['outside']}. Pulsar can show its logs and stop it; it has no recipe here to "
                      "check, restart or switch from.")
        elif not active:
            field("State", f"its containers exist, but none is running. **Stop…** removes them; model files are "
                           "kept.")
        else:
            from scripts.connect_card import recorded_api_auth, served_name
            name = (served_name(result) if isinstance(result, dict) and rc == 0 else None) or raw.get("served_name")
            field("Model", f"`{clean(name)}`, {STATUS_WORDS['served-name']}" if name
                  else STATUS_WORDS["not-in-inventory"])
            url = result.get("api_url") if isinstance(result, dict) and rc == 0 else None
            if url:
                field("API", f"`{url.rstrip('/')}/v1` · checked {when}")
                auth = result.get("api_auth") if type(result.get("api_auth")) is bool \
                    else recorded_api_auth(result.get("service_id"))
                words = {True: STATUS_WORDS["key"], False: STATUS_WORDS["no-key"],
                         None: STATUS_WORDS["key-not-recorded"]}[auth]
                field("Key", f"{words} · checked {when}")
            else:
                field("API", STATUS_WORDS["not-checked"] if not checked else
                      f"not found by Check it now {when}")
                field("Key", STATUS_WORDS["not-checked"] if not checked else
                      f"not known: Check it now found no API {when}")
            field("Health", self._health(result, rc, when) if checked else STATUS_WORDS["not-checked"])
        if not outside:
            blocker = self.restart_blocker(conf)
            if blocker and blocker != "Pulsar cannot stop it":
                field("Restart", f"{STATUS_WORDS['not-offered']}: {blocker}")
        if service["stop_reason"]:
            field("Stop", f"{STATUS_WORDS['not-offered']}: {clean(service['stop_reason'])}")

    @staticmethod
    def _health(result: Any, rc: int, when: str) -> str:
        if rc == 1:
            return f"{STATUS_WORDS['not-found']} {when}; it may have stopped"
        if rc == 3:
            return f"{STATUS_WORDS['could-not-tell']} {when}: a node did not answer"
        if rc != 0 or not isinstance(result, dict):
            return f"Check it now did not finish {when}; the reason was shown"
        state = result.get("state")
        if state != "running":
            from scripts.service_status import STATE_WORDS
            return f"{STATE_WORDS.get(state, clean(state))} when checked {when}"
        return {True: f"{STATUS_WORDS['answered']} {when}", False: f"{STATUS_WORDS['did-not-answer']} {when}; "
                "**Logs** shows its output", None: f"{STATUS_WORDS['no-address']} {when}"}[result.get("healthy")]


def _instant(epoch: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc)


def outcome(action: str, rc: int, name: str, shown: str, *, width: int | None = None) -> str:
    """The line after a Stop or Stop every service, then the command under Same as."""
    output = io.StringIO()
    out = TerminalWriter(width=width, stream=output)
    subject = name if action == "stop" else "every Pulsar service"
    if rc == 0:
        out.emit(f"Stop finished for {subject}. Model files, pins and archives are kept.", "✓ ", "  ")
    elif rc in (130, 143) or rc > 128:
        out.emit(f"Stop was interrupted; whether {subject} still runs is not confirmed. This page looks again "
                 "before it offers anything.", "✗ ", "  ", prefix_role="fail")
    elif rc == 3:
        out.emit(f"Stop could not confirm what it removed for {subject}; the reason is above. This page looks "
                 "again.", "✗ ", "  ", prefix_role="fail")
    else:
        out.emit(f"Stop was refused or failed for {subject}; the reason is above. Nothing else was changed.",
                 "✗ ", "  ", prefix_role="fail")
    out.emit(shown, "Same as: ", "  ", role="command")
    return output.getvalue()


# ---------------------------------------------------------------------------

def _entries(stream) -> list[dict]:
    try:
        return json.load(stream).get("entries") or []
    except (ValueError, AttributeError):
        return []


def fresh(path: str, seconds: int = FRESH_SECONDS, now: float | None = None) -> int | None:
    """The epoch a readable inventory at PATH was taken, when under SECONDS old; else None."""
    document = load_json(path)
    if not isinstance(document, dict):
        return None
    try:
        project(document, [])
        moment = _parse(document.get("generated_at"))
    except (ValueError, TypeError, AttributeError):
        return None
    current = now if now is not None else time.time()
    if moment is None or not 0 <= current - moment < seconds:
        return None
    return int(moment)


def _parse(value: Any) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("services", "names", "screen", "choices", "command", "question"):
        command = sub.add_parser(name)
        command.add_argument("--inventory", required=True)
        command.add_argument("--conf")
        command.add_argument("--status", help="Check it now's result for --conf (PULSAR_SERVICE_RESULT_FILE)")
        command.add_argument("--status-rc", type=int)
        command.add_argument("--status-at", type=float)
        command.add_argument("--now", type=float)
    sub.choices["command"].add_argument("--action", required=True)
    sub.choices["command"].add_argument("--shown", action="store_true")
    sub.choices["question"].add_argument("--all", action="store_true")
    result = sub.add_parser("outcome")
    result.add_argument("--action", choices=("stop", "stop-all"), required=True)
    result.add_argument("--rc", type=int, required=True)
    result.add_argument("--name", default="")
    result.add_argument("--shown", required=True)
    check = sub.add_parser("fresh")
    check.add_argument("--inventory", required=True)
    check.add_argument("--seconds", type=int, default=FRESH_SECONDS)
    for command in sub.choices.values():
        command.add_argument("--repo-root", type=Path, default=ROOT)
        command.add_argument("--width", type=int)
    args = parser.parse_args(argv)
    width = max(MIN_WIDTH, min(MAX_WIDTH, args.width or terminal_width()))
    try:
        if args.command == "fresh":
            moment = fresh(args.inventory, args.seconds)
            if moment is None:
                return 1
            print(moment)
            return 0
        if args.command == "outcome":
            sys.stdout.write(outcome(args.action, args.rc, args.name, args.shown, width=width))
            return 0
        inventory = load_json(args.inventory)
        if not isinstance(inventory, dict):
            raise ValueError("the service inventory could not be read")
        status = {}
        if args.command in ("screen", "choices") and args.status_rc is not None:
            status = {"result": load_json(args.status), "rc": args.status_rc, "at": args.status_at}
        page = Page(inventory, _entries(sys.stdin), names=NodeNames.saved(args.repo_root), now=args.now,
                    status=status)
        if args.command == "services":
            for conf, kind in page.order():
                print(f"{conf}\t{kind}")
        elif args.command == "names":
            # CONF<TAB>LABEL: each service's one name as a list choice.
            for conf, _ in page.order():
                print(f"{conf}\t{page.label(conf, width)}")
        elif args.command == "screen":
            sys.stdout.write(page.screen(args.conf, width=width))
        elif args.command == "choices":
            for action, label in page.choices(args.conf):
                print(f"{action}\t{label}")
        elif args.command == "command":
            command = page.command(args.conf, args.action, shown=args.shown)
            print(" ".join(command) if args.shown else "\n".join(command))
        elif args.command == "question":
            question = page.question(None if args.all else args.conf)
            if question is None:
                return 1
            print(clean(question))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        from scripts.terminal_format import error_line
        error_line(exc, tag="running services")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
