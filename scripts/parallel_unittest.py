#!/usr/bin/env python3
"""Run this checkout's unit tests in parallel processes.

The two discovery passes match scripts/selftest.sh: release_spec/tests as its
own top level, then tests from the repository root. Each test runs in a fresh
process, so tests share no interpreter state; tests whose class or module
defines fixtures run together in one process, so those fixtures run once.
Processes start longest first, using durations saved by earlier runs, so the
slowest tests never start last.

Names (modules, classes or test IDs, as `python3 -m unittest` takes them) run
only those tests, loaded from the repository root.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
# (start directory, top-level directory) under the root, in selftest.sh order.
PASSES = (("release_spec/tests", "release_spec/tests"), ("tests", "."))
DURATIONS = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pulsar" / "selftest-durations.json"
# A test without a saved duration starts first: it may be the slowest.
UNKNOWN_SECONDS = float("inf")


def _cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _cases(test)
        else:
            yield test


def _group(test):
    """The key of the process a test runs in: its module or class when they define fixtures."""
    cls = type(test)
    if cls.__module__ == "unittest.loader":
        # A module that failed to import: loading it by name reports the same error.
        return test._testMethodName, test._testMethodName
    module = sys.modules.get(cls.__module__)
    if hasattr(module, "setUpModule") or hasattr(module, "tearDownModule"):
        return cls.__module__, test.id()
    if (cls.setUpClass.__func__ is not unittest.TestCase.setUpClass.__func__
            or cls.tearDownClass.__func__ is not unittest.TestCase.tearDownClass.__func__):
        return cls.__module__ + "." + cls.__qualname__, test.id()
    return test.id(), test.id()


def _import_path(root, top):
    # Match `python3 -m unittest` run from the root: the root, not this script's
    # directory, is first on the path, with the top-level directory before it.
    sys.path[0] = str(root)
    if Path(top).resolve() != Path(root).resolve():
        sys.path.insert(0, str(top))


def list_jobs(root, top, start, names):
    """Print the test IDs of each process as JSON: [[id, ...], ...]."""
    _import_path(root, top)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(names) if names else loader.discover(start, top_level_dir=top, pattern="test_*.py")
    jobs = {}
    for test in _cases(suite):
        key, test_id = _group(test)
        if test_id not in jobs.setdefault(key, []):
            jobs[key].append(test_id)
    print(json.dumps(list(jobs.values())))


class Recorder(unittest.TestResult):
    """Keeps each test's outcome and duration; output of passing tests is dropped."""

    def __init__(self):
        super().__init__()
        self.buffer = True
        self.durations = {}
        self.problems = []

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        self.durations[test.id()] = round(time.monotonic() - self.started, 3)

    def addError(self, test, err):
        super().addError(test, err)
        self.problems.append(("ERROR", str(test), self.errors[-1][1]))

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.problems.append(("FAIL", str(test), self.failures[-1][1]))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            kind = "FAIL" if issubclass(err[0], test.failureException) else "ERROR"
            found = self.failures if kind == "FAIL" else self.errors
            self.problems.append((kind, str(subtest), found[-1][1]))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.problems.append(("UNEXPECTED SUCCESS", str(test), ""))


def run_job(root, top, output, ids):
    _import_path(root, top)
    result = Recorder()
    unittest.TestLoader().loadTestsFromNames(ids).run(result)
    Path(output).write_text(json.dumps({
        "tests_run": result.testsRun, "skipped": len(result.skipped),
        "durations": result.durations, "problems": result.problems}))


class Pool:
    """Worker processes, stopped together on interruption."""

    def __init__(self, root):
        self.root = root
        self.running = set()
        self.lock = threading.Lock()
        self.stopping = False

    def run(self, args):
        with self.lock:
            if self.stopping:
                raise KeyboardInterrupt
            process = subprocess.Popen([sys.executable, __file__, "--root", str(self.root), *args],
                                       cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            self.running.add(process)
        try:
            output = process.communicate()[0]
        finally:
            with self.lock:
                self.running.discard(process)
        return process.returncode, output

    def stop(self):
        with self.lock:
            self.stopping = True
            for process in self.running:
                process.terminate()


def load_durations(path):
    try:
        found = json.loads(Path(path).read_text())
        return {key: float(value) for key, value in found.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_durations(path, durations):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
            json.dump(dict(sorted(durations.items())), handle, indent=0)
        os.replace(handle.name, path)
    except OSError as exc:
        print(f"Test durations were not saved to {path}: {exc}", file=sys.stderr)


def minutes(seconds):
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s" if seconds >= 60 else f"{seconds:.1f}s"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="scripts/parallel_unittest.py", description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", "-j", type=int, default=os.cpu_count() or 1,
                        help="processes at once (default: the number of CPUs)")
    parser.add_argument("--durations-file", default=str(DURATIONS),
                        help="saved test durations that set the start order (default: %(default)s)")
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    parser.add_argument("--list", nargs=2, metavar=("TOP", "START"), help=argparse.SUPPRESS)
    parser.add_argument("--run", nargs=2, metavar=("TOP", "OUTPUT"), help=argparse.SUPPRESS)
    parser.add_argument("names", nargs="*", help="modules, classes or test IDs to run instead of discovery")
    options = parser.parse_args(argv)
    root = Path(options.root).resolve()
    if options.list:
        return list_jobs(root, *options.list, options.names)
    if options.run:
        return run_job(root, *options.run, options.names)
    if options.jobs < 1:
        parser.error("--jobs must be at least 1")

    began = time.monotonic()
    pool = Pool(root)
    passes = [(".", None)] if options.names else [(top, start) for start, top in PASSES]
    jobs = []
    for top, start in passes:
        status, output = pool.run(["--list", str(root / top), str(root / start) if start else "-", *options.names])
        if status:
            print(output, end="", file=sys.stderr)
            print(f"Tests could not be listed from {start or 'the names given'}.", file=sys.stderr)
            return 1
        jobs += [(top, ids) for ids in json.loads(output.splitlines()[-1])]
    durations = load_durations(options.durations_file)
    jobs.sort(key=lambda job: -sum(durations.get(test_id, UNKNOWN_SECONDS) for test_id in job[1]))
    total = sum(len(ids) for _, ids in jobs)
    print(f"Running {total} tests in {len(jobs)} processes, {options.jobs} at a time.", flush=True)

    def run(job):
        top, ids = job
        with tempfile.TemporaryDirectory(prefix="pulsar-unittest-") as scratch:
            output = Path(scratch) / "result.json"
            status, printed = pool.run(["--run", str(root / top), str(output), *ids])
            try:
                result = json.loads(output.read_text())
            except (OSError, ValueError):
                detail = f"The test process exited with status {status} before reporting.\n{printed[-4000:]}"
                return {"tests_run": 0, "skipped": 0, "durations": {},
                        "problems": [("ERROR", ", ".join(ids), detail)]}
            if result["problems"] and printed.strip():
                # Output written straight to the process's descriptors, such as a child process's.
                kind, name, detail = result["problems"][-1]
                result["problems"][-1] = (kind, name, detail + "\nOutput of the test process:\n" + printed[-4000:])
            return result

    tests_run = skipped = finished = 0
    problems, measured = [], {}
    reported = 0.0
    executor = ThreadPoolExecutor(max_workers=options.jobs)
    try:
        for future in as_completed([executor.submit(run, job) for job in jobs]):
            result = future.result()
            tests_run += result["tests_run"]
            skipped += result["skipped"]
            finished += len(result["durations"])
            measured.update(result["durations"])
            for kind, name, _ in result["problems"]:
                print(f"{kind}: {name}", flush=True)
            problems += result["problems"]
            if finished >= reported + total / 10:
                reported = finished
                print(f"{finished}/{total} tests finished, {len(problems)} problems, {minutes(time.monotonic() - began)}",
                      flush=True)
    except KeyboardInterrupt:
        pool.stop()
        executor.shutdown(wait=True, cancel_futures=True)
        print("Interrupted; the remaining tests were not run.", file=sys.stderr)
        return 130
    executor.shutdown()
    save_durations(options.durations_file, {**durations, **measured})

    for kind, name, detail in sorted(problems, key=lambda problem: problem[1]):
        print("=" * 70 + f"\n{kind}: {name}\n" + "-" * 70 + f"\n{detail}")
    slowest = sorted(measured.items(), key=lambda item: -item[1])[:10]
    if slowest:
        print("Slowest tests:")
        for test_id, seconds in slowest:
            print(f"  {seconds:7.1f}s  {test_id}")
    print("-" * 70 + f"\nRan {tests_run} tests in {minutes(time.monotonic() - began)} with {options.jobs} processes")
    counts = {"failures": sum(kind == "FAIL" for kind, _, _ in problems),
              "errors": sum(kind == "ERROR" for kind, _, _ in problems),
              "unexpected successes": sum(kind == "UNEXPECTED SUCCESS" for kind, _, _ in problems),
              "skipped": skipped}
    summary = ", ".join(f"{name}={count}" for name, count in counts.items() if count)
    print(("FAILED" if problems else "OK") + (f" ({summary})" if summary else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
