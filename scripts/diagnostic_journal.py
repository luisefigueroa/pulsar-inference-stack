"""Development-only journal capture parsing; Bash owns all journalctl clients.

The inclusive cursor is checked, not inferred from a successful seek. A final
bounded query reconciles journal-delivered records; this cannot prove lossless
kernel delivery, retention between queries, or absence of later errors.
"""
import hashlib
import json
import re
import time

MAX_CAPTURE = 8 * 1024**2
MAX_RECORD = 65536
LOSS = re.compile(r"(?:missed|lost|dropped|suppressed)\s+\d+\s+(?:kernel\s+)?(?:messages|records)|\d+\s+(?:messages|callbacks)\s+(?:suppressed|dropped)|journal.*(?:corrupt|overrun)", re.I)


def record(raw, boot):
    if not raw.endswith(b"\n") or len(raw) > MAX_RECORD:
        raise ValueError("incomplete or oversized journal record")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("_BOOT_ID") != boot or value.get("_TRANSPORT") != "kernel":
        raise ValueError("journal boot or kernel transport mismatch")
    cursor = value.get("__CURSOR")
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024 or not re.fullmatch(r"[A-Za-z0-9;=:_+./-]+", cursor):
        raise ValueError("journal cursor missing or malformed")
    stamps = []
    for key in ("__MONOTONIC_TIMESTAMP", "__REALTIME_TIMESTAMP"):
        stamp = value.get(key)
        if not isinstance(stamp, str) or not stamp.isascii() or not stamp.isdigit():
            raise ValueError("journal timestamp missing or malformed")
        stamps.append(int(stamp))
    if stamps[0] * 1000 > time.monotonic_ns() or stamps[1] <= 0:
        raise ValueError("journal time is outside the current boot window")
    message = value.get("MESSAGE")
    if not isinstance(message, str):
        raise ValueError("journal message is missing, binary, repeated or truncated")
    if LOSS.search(message):
        raise ValueError("journal reports known message loss or corruption")
    return {"cursor": cursor, "timestamp_us": stamps[0], "realtime_us": stamps[1],
            "message": message,
            "record_sha256": hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def anchor(path, boot):
    data = path.read_bytes()
    if len(data) > MAX_RECORD or len(data.splitlines()) != 1:
        raise ValueError("journal readiness query must return one bounded kernel record")
    return record(data, boot)


class Capture:
    """One followed record stream, reconciled with one finite final query."""
    def __init__(self, attempt, boot, anchor_record):
        self.attempt, self.boot, self.anchor = attempt, boot, anchor_record
        self.stream = (attempt / "journal-follow.stdout").open("rb")
        self.pending = b""
        self.final = False
        self.first = True
        self.seen = {}
        self.follow_cursors = set()
        self.final_cursors = set()
        self.last_time = None
        self.final_boundary = None

    def next(self, final_facts=None):
        if (self.attempt / "journal-follow.stderr").stat().st_size:
            raise ValueError("journal follow reported an access/query warning or error")
        if self.stream.seek(0, 1) > MAX_CAPTURE:
            raise ValueError("journal capture bound exhausted")
        line = self.stream.readline(MAX_RECORD + 1)
        if line:
            self.pending += line
            if len(self.pending) > MAX_RECORD:
                raise ValueError("journal record bound exhausted")
            if not self.pending.endswith(b"\n"):
                return "idle", None
            row = record(self.pending, self.boot)
            self.pending = b""
            if self.first:
                if row != self.anchor:
                    raise ValueError("inclusive journal cursor is absent or changed; retention unknown")
                self.first = False
            if self.last_time is not None and row["timestamp_us"] < self.last_time:
                raise ValueError("journal monotonic record time regressed")
            self.last_time = row["timestamp_us"]
            cursors = self.final_cursors if self.final else self.follow_cursors
            if row["cursor"] in cursors:
                raise ValueError("journal cursor repeated within one query")
            cursors.add(row["cursor"])
            previous = self.seen.get(row["cursor"])
            if previous is not None and previous != row["record_sha256"]:
                raise ValueError("journal cursor changed contents")
            self.seen[row["cursor"]] = row["record_sha256"]
            if len(self.seen) > 32768:
                raise ValueError("journal cursor inventory bound exhausted")
            # The retained anchor is a location, not a new in-window event.
            if previous is not None or row["cursor"] == self.anchor["cursor"]:
                return "anchor" if len(self.follow_cursors) == 1 and not self.final else "duplicate", row
            return "record", row
        if final_facts is None:
            return "idle", None  # Regular-file EOF means only no bytes yet.
        if self.pending or self.first:
            raise ValueError("closed journal capture is incomplete")
        if not self.final:
            self.stream.close()
            self.stream = (self.attempt / "journal-final.stdout").open("rb")
            self.final, self.first, self.last_time = True, True, None
            self.final_boundary = time.monotonic_ns()
            return "duplicate", None
        if not self.follow_cursors <= self.final_cursors:
            raise ValueError("final journal query omitted followed cursors; retention/window uncertain")
        return "complete", None

    def close(self):
        self.stream.close()
