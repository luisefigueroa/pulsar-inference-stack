#!/usr/bin/env python3
"""Resolve one Hugging Face selector and emit its complete Git/LFS inventory.

This helper is streamed to the selected rank and run with the Python
interpreter that owns that rank's modern ``hf`` command. Authentication stays
inside the Hugging Face client on that rank; this program accepts no token.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def fail(message: str) -> None:
    raise ValueError(message)


def repo_file_row(item: Any) -> dict[str, Any]:
    path = getattr(item, "path", None) or getattr(item, "rfilename", None)
    size = getattr(item, "size", None)
    blob_id = getattr(item, "blob_id", None)
    if not isinstance(path, str) or not path:
        fail("Hugging Face returned a file without a path")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        fail(f"Hugging Face returned an invalid size for {path}")
    if not isinstance(blob_id, str) or not blob_id:
        fail(f"Hugging Face returned no Git object ID for {path}")
    row: dict[str, Any] = {
        "type": "file",
        "path": path,
        "size": size,
        "blob_id": blob_id,
    }
    lfs = getattr(item, "lfs", None)
    if lfs is not None:
        lfs_size = getattr(lfs, "size", None)
        lfs_sha256 = getattr(lfs, "sha256", None)
        if isinstance(lfs_size, bool) or not isinstance(lfs_size, int) or lfs_size < 0:
            fail(f"Hugging Face returned an invalid LFS size for {path}")
        if not isinstance(lfs_sha256, str) or not lfs_sha256:
            fail(f"Hugging Face returned no LFS SHA-256 for {path}")
        row["lfs"] = {"size": lfs_size, "sha256": lfs_sha256}
    return row


def fetch_inventory(model_id: str, selector: str) -> dict[str, Any]:
    try:
        from huggingface_hub import HfApi
        from huggingface_hub.hf_api import RepoFile
    except ImportError as exc:
        fail(f"the selected hf installation cannot import huggingface_hub: {exc}")

    api = HfApi()
    # expand= returns only the requested fields; private must be requested
    # explicitly so a missing attribute is not mistaken for a public repo.
    info = api.model_info(model_id, revision=selector, expand=["sha", "private"])
    if getattr(info, "private", None) is not False:
        fail("Hugging Face repository is not public")
    commit = getattr(info, "sha", None)
    if not isinstance(commit, str) or not commit:
        fail("Hugging Face did not resolve the selector to a commit")
    rows = [
        repo_file_row(item)
        for item in api.list_repo_tree(
            model_id,
            repo_type="model",
            revision=commit,
            recursive=True,
            expand=True,
        )
        if isinstance(item, RepoFile)
    ]
    if not rows:
        fail("Hugging Face returned an empty model inventory")
    rows.sort(key=lambda item: item["path"])
    return {
        "id": getattr(info, "id", None) or model_id,
        "sha": commit,
        "siblings": rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve a Hugging Face model selector and inventory"
    )
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--selector", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = fetch_inventory(args.model_id, args.selector)
    except Exception as exc:
        print(f"hf-source-inventory: ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
