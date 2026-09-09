#!/usr/bin/env python3
"""Reject private identity in author, committer, or message metadata."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

from check_publishable_privacy import scan_bytes


class CommitPrivacyError(RuntimeError):
    pass


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ['git', '-C', str(root), *args], text=True, capture_output=True)
    if result.returncode:
        raise CommitPrivacyError(result.stderr.strip() or 'Git inspection failed')
    return result.stdout


def commits(root: Path, revision_range: str) -> list[str]:
    rows = [row for row in git(root, 'rev-list', '--reverse', revision_range).splitlines() if row]
    if not rows:
        raise CommitPrivacyError('commit range is empty')
    return rows


def metadata(root: Path, commit: str) -> bytes:
    fields = git(
        root,
        'show', '-s',
        '--format=author_name=%an%nauthor_email=%ae%ncommitter_name=%cn%ncommitter_email=%ce%n%n%B',
        commit,
    )
    return fields.encode('utf-8')


def check(root: Path, revision_range: str) -> tuple[int, list[tuple[str, str, str]]]:
    checked = 0
    findings: list[tuple[str, str, str]] = []
    for commit in commits(root, revision_range):
        checked += 1
        identity, _, message = metadata(root, commit).partition(b'\n\n')
        findings_for_commit = scan_bytes(f'commit/{commit}/identity.txt', identity)
        findings_for_commit += scan_bytes(f'commit/{commit}/message.txt', message,
                                         network_context_only=True)
        for finding in findings_for_commit:
            findings.append((commit, finding.rule, finding.message))
    return checked, findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', default=ROOT)
    parser.add_argument('--range', dest='revision_range', required=True,
                        help='Git revision range, for example BASE..HEAD')
    args = parser.parse_args(argv)
    try:
        checked, findings = check(Path(args.repo_root).resolve(), args.revision_range)
    except (CommitPrivacyError, OSError, UnicodeError) as exc:
        print(f'commit privacy: ERROR: {exc}', file=sys.stderr)
        return 2
    if findings:
        print(f'commit privacy: FAIL ({len(findings)} finding(s))', file=sys.stderr)
        for commit, rule, message in findings:
            print(f'  {commit}: [{rule}] {message}', file=sys.stderr)
        return 1
    print(f'commit privacy: OK ({checked} commit(s))')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
