"""Read-only storage view. Each home session can share the same local observations."""
from __future__ import annotations

import curses
import fcntl
import hashlib
import json
import os
from pathlib import Path
import textwrap
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library.catalog import age_seconds
from model_library.file_inventory import _unread
from model_library.integrity import StorageError, atomic_json, read_json
from model_library.node_names import topology_path
from model_library.state import ensure_directory
from scripts.models_page import observed_cluster, page
from scripts.saved_observations import check_budget_nodes
from scripts.serve_plan import envelope_result
from scripts.terminal_format import TerminalWriter

MAX_AGE = 60
RETRY = 15
SECTIONS = ("files", "budget")


def stamp(path):
    try:
        st = path.stat()
        return [st.st_ino, st.st_mtime_ns]
    except FileNotFoundError:
        return None


class StorageCache:
    def __init__(self, repo):
        self.repo = repo
        self.library = Path(os.environ.get("PULSAR_MODEL_LIBRARY_DIR", repo / ".model-library")).resolve()
        self.directory = ensure_directory(self.library / ".storage-overview")
        for name in ("storage", "service"):
            fd = os.open(self.directory / name, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        self.loaded = {}

    def scope(self):
        topology = topology_path(self.repo)
        if not stamp(topology):
            return None
        # A replacement of the membership or settings must never reuse the old view.
        settings = [os.environ.get(key) for key in (
            "PULSAR_COLD_ROOT", "PULSAR_HOT_ROOT", "PULSAR_HOT_BUDGET_BYTES", "PULSAR_HOT_RESERVE_BYTES",
            "PULSAR_RELEASES_ROOT", "PULSAR_OVERLAY_PATH", "PULSAR_HOME_ROOT", "HF_HOME", "HF_CACHE")]
        overlay = Path(os.environ.get("PULSAR_OVERLAY_PATH", self.repo / ".pulsar-overlay.json"))
        identity = [str(self.repo), str(topology), stamp(topology), stamp(self.repo / ".env"), stamp(overlay), settings]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def generation(self, section):
        # Atomic record changes are visible even to a different Stack checkout
        # reading the same library. The markers also cover partial node writes.
        marks = [stamp(self.directory / "storage"),
                 *(stamp(self.library / name) for name in ("homes", "views", "archives"))]
        if section == "files":
            marks.extend((stamp(self.directory / "service"), stamp(self.library / "services"),
                          stamp(self.library / "observations")))
        return marks

    def record(self, section, scope):
        path = self.directory / f"{scope}-{section}.json"
        version = stamp(path)
        if self.loaded.get(section, ())[:1] != (version,):
            try:
                value = read_json(path) if version else {}
            except (OSError, ValueError):
                value = {}
            self.loaded[section] = version, value
        value = self.loaded[section][1]
        return value if isinstance(value, dict) and value.get("scope") == scope else {}

    def snapshot(self, scope):
        # Writers replace each document atomically. If files stayed the same across
        # the budget read, both results were current at that read. No probe lock is needed.
        while True:
            files = self.record("files", scope)
            budget = self.record("budget", scope)
            if files == self.record("files", scope):
                return files, budget


def due(record, generation, now, *, since=0):
    age = now - record.get("attempt", 0)
    return (record.get("generation") != generation or record.get("attempt", 0) < since or age < 0
            or age >= (RETRY if record.get("error") else MAX_AGE))


def probe(repo, *args):
    proc = subprocess.run([str(repo / "pulsar"), *args, "--json"], cwd=repo,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode:
        raise ValueError(f"check exited {proc.returncode}")
    value, _ = envelope_result(json.loads(proc.stdout))
    if value is None:
        raise ValueError("check returned no observation")
    return value


def refresh(cache, section, scope, *, since=0, cancelled=lambda: False):
    previous = cache.record(section, scope)
    # Capture the generation before the first command. A later change stays due.
    generation = cache.generation(section)
    start = time.time()
    if not due(previous, generation, start, since=since):
        return
    update = dict(previous, scope=scope, generation=generation, attempt=start)
    try:
        if section == "files":
            catalog = probe(cache.repo, "models", "list")
            if cancelled():
                return
            data = probe(cache.repo, "model", "files")
            if (not isinstance(catalog.get("entries"), list) or data.get("kind") != "pulsar-model-file-inventory"
                    or not isinstance(data.get("nodes"), list) or not data["nodes"]):
                raise ValueError("file scan returned no usable inventory")
            last_seen = {n["node_id"]: (previous.get("last_seen", {}).get(n["node_id"]) if _unread(n)
                                        else n.get("observed_at")) for n in data["nodes"]}
            update.update(entries=catalog["entries"], last_seen=last_seen)
        else:
            data = probe(cache.repo, "model", "budget")
            if data.get("kind") != "pulsar-storage-budget":
                raise ValueError("budget check returned no usable result")
            check_budget_nodes(data.get("nodes"))
        update.update(data=data, checked=time.time(), error=None)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Keep the last observation and its time. An unsuccessful check never renews it.
        update["error"] = str(exc)
    if not cancelled() and cache.scope() == scope:
        update["attempt"] = time.time()
        atomic_json(cache.directory / f"{scope}-{section}.json", update)


def try_refresh(cache, section, scope, *, since=0, cancelled=lambda: False):
    """One writer probes. A caller that finds the lock busy can read published sections."""
    fd = os.open(cache.directory / "refresh.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if not cancelled() and cache.scope() == scope:
            # Recheck the record under the lock. Another process may have just published it.
            refresh(cache, section, scope, since=since, cancelled=cancelled)
        return True
    finally:
        os.close(fd)


def collect(cache):
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        scope = cache.scope()
        for section in SECTIONS:
            if stopping or not scope:
                break
            if due(cache.record(section, scope), cache.generation(section), time.time()):
                if not try_refresh(cache, section, scope, cancelled=lambda: stopping):
                    break
        time.sleep(.5)


def capture_for_menu(cache, work, *, since=0):
    """Export observations to the menu's private directory. Only action previews authorize a change."""
    scope = cache.scope()
    if not scope:
        return 10  # No shared observations here. Use the menu's direct checks.
    progress = TerminalWriter()
    changed = "Storage or cluster settings changed during the check. Choose Look again."
    for section, title in zip(SECTIONS, ("Checking model files on every node…", "Reading storage budgets…")):
        announced = False
        while due(cache.record(section, scope), cache.generation(section), time.time(), since=since):
            if cache.scope() != scope:
                raise ValueError(changed)
            if not announced:
                progress.emit(title)
                announced = True
            try:
                finished = try_refresh(cache, section, scope, since=since)
            except (OSError, StorageError):
                # The shared lock or record could not be written. Node errors are
                # recorded by refresh() and still follow the ordinary failure path.
                return 10
            if finished:
                break
            time.sleep(.1)
        if section == "files" and (error := cache.record(section, scope).get("error")):
            raise ValueError(error)
    files, budget = cache.snapshot(scope)
    # Age decides whether to start a probe. Only an actual invalidation supersedes
    # an observation already accepted for this visit while the other probe runs.
    if (cache.scope() != scope or files.get("generation") != cache.generation("files")
            or budget.get("generation") != cache.generation("budget")):
        raise ValueError(changed)
    if files.get("error"):
        raise ValueError(files["error"])
    atomic_json(work / "catalog.json", {"entries": files["entries"]})
    atomic_json(work / "files.json", files["data"])
    # A failed budget can retain an older good value for the overview. Controls must show unknown.
    report = None
    if not budget.get("error"):
        checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(budget["checked"]))
        report = dict(budget["data"], observed_at=checked_at)
    atomic_json(work / "budget.json", report)
    return 0


def status(label, record, generation, now):
    if not record.get("checked"):
        return f"{label} unknown · {record['error']}" if record.get("error") else f"{label} · waiting for first check"
    age = max(0, int(now - record["checked"]))
    detail = (f" · unknown now ({record['error']})" if record.get("error") else
              " · refresh due" if due(record, generation, now) else "")
    return f"{label} · last checked {age}s ago{detail}"


def view(screen, session):
    os.environ["PULSAR_COLOR"] = "never"  # curses owns the screen
    try:
        cache = StorageCache(ROOT)
    except (ValueError, OSError):
        cache = None
    else:
        (session / "storage-request").touch()
    screen.keypad(True)
    screen.timeout(300)
    offset = 0
    rendered = None
    content = []
    while True:
        height, width = screen.getmaxyx()
        cols = max(1, width - 1)
        scope = cache.scope() if cache else None
        rows = []
        if scope:
            records = {s: cache.record(s, scope) for s in SECTIONS}
            files, budget = records["files"], records["budget"]
            for section, label in zip(SECTIONS, ("Model files", "Storage budget")):
                rows.append(status(label, records[section], cache.generation(section), time.time()))
            identity = (scope, files.get("attempt"), budget.get("attempt"), cols)
            if identity != rendered:
                content = []
                if files.get("data"):
                    nodes = (budget.get("data") or {}).get("nodes") if not budget.get("error") else None
                    cluster = observed_cluster(ROOT, files["entries"], files["data"], nodes)
                    content = page(cluster, width=cols).splitlines()[1:]
                rendered = identity
            for node in (files.get("data") or {}).get("nodes", []):
                seen = files["last_seen"].get(node["node_id"])
                age = age_seconds(seen) if seen else None
                words = f"last checked {age}s ago" if age is not None else "check time unknown" if seen else "no successful check"
                if _unread(node) or files.get("error"):
                    words = "unknown now · " + words
                rows.append(f"{node['hostname']} · {words}")
            rows.extend(content)
        else:
            rows.append("Storage observations unavailable. Check and manage reads the nodes.")
        body = [part for line in rows for part in (textwrap.wrap(line, cols, replace_whitespace=False) or [""])]
        header = ["Models and disk space", *textwrap.wrap("q Back   m Check and manage   Up/down Scroll", cols)]
        room = max(0, height - len(header))
        offset = min(max(0, offset), max(0, len(body) - room))
        screen.erase()
        for y, line in enumerate((header + body[offset:offset+room])[:height]):
            if width > 1:
                screen.addnstr(y, 0, "".join(c if c.isprintable() else " " for c in line), cols)
        screen.refresh()
        key = screen.getch()
        if key in (27, ord("q"), ord("Q")):
            return 0
        if key == ord("m"):
            return 10
        if key in (curses.KEY_DOWN, ord("j")):
            offset += 1
        if key in (curses.KEY_UP, ord("k")):
            offset -= 1


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["collect"]:
            try:
                collect(StorageCache(ROOT))
            except (OSError, StorageError):
                sys.exit(10)  # Direct checks remain available to the menu.
        elif sys.argv[1:2] == ["manage"]:
            try:
                cache = StorageCache(ROOT)
            except (OSError, ValueError):
                sys.exit(10)
            try:
                sys.exit(capture_for_menu(cache, Path(sys.argv[2]).resolve(),
                                          since=time.time() if sys.argv[3:] == ["--again"] else 0))
            except (OSError, ValueError, KeyError) as exc:
                print(exc, file=sys.stderr)
                sys.exit(1)
        else:
            sys.exit(curses.wrapper(view, Path(os.environ["PULSAR_MENU_SESSION"])))
    except KeyboardInterrupt:
        sys.exit(130)
