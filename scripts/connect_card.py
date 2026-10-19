#!/usr/bin/env python3
"""Connect card: how to send a first request to a running service.

The menu prints it after a successful Start or Live status, from the result
those commands write when PULSAR_SERVICE_RESULT_FILE is set: the API URL
(without /v1), the served model name and, when known, whether the service was
started with an API key. A result that does not name a running service's API
prints nothing. The card never prints the key: the request reads it from the
client's VLLM_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


def recorded_api_auth(service_id: object) -> bool | None:
    """Whether the recorded launch plan started the service with an API key; None when unreadable."""
    if not isinstance(service_id, str) or not service_id:
        return None
    try:
        from model_library.state import Store
        root = os.environ.get("PULSAR_MODEL_LIBRARY_DIR") or os.environ.get("MODEL_LIBRARY_DIR") \
            or ROOT / ".model-library"
        store = Store(root)
        value = store.get("service-plans", store.get("services", service_id)["plan_id"]).get("api_auth")
    except Exception:  # noqa: BLE001 - the card says the requirement is unknown
        return None
    return value if type(value) is bool else None


def served_name(result: dict) -> str | None:
    if isinstance(result.get("served_name"), str) and result["served_name"]:
        return result["served_name"]
    for service in result.get("services") or []:
        if isinstance(service, dict) and isinstance(service.get("served_name"), str) and service["served_name"]:
            return service["served_name"]
    return None


def card(result: dict) -> list[str]:
    """The card's lines, or none when the result names no running service's API."""
    api_url = result.get("api_url")
    if result.get("state", "running") != "running" or not isinstance(api_url, str) or not api_url:
        return []
    base = api_url.rstrip("/") + "/v1"
    model = served_name(result)
    auth = result.get("api_auth") if type(result.get("api_auth")) is bool \
        else recorded_api_auth(result.get("service_id"))
    lines = ["Connect", f"  API:    {base}", f"  Model:  {model or 'not recorded; replace SERVED_MODEL in the request'}"]
    lines.append({True: "  Auth:   API key required; the request reads it from VLLM_API_KEY",
                  False: "  Auth:   none; the API accepts requests without a key",
                  None: "  Auth:   not recorded; add the key header if the service was started with one"}[auth])
    if any(f"//{host}" in base for host in LOOPBACK):
        lines.append("  This loopback address works only on the API node; from another machine,")
        lines.append("  use an address that reaches that node.")
    body = json.dumps({"model": model or "SERVED_MODEL", "prompt": "Write one sentence about the Moon.",
                       "max_tokens": 32}, separators=(",", ":"))
    request = ["", "First request:", f"curl --fail-with-body --silent --show-error {shlex.quote(base + '/completions')} \\",
               "  -H 'Content-Type: application/json' \\"]
    if auth is not False:
        request.append('  -H "Authorization: Bearer ${VLLM_API_KEY:?Set the service API key in this shell}" \\')
    request.append(f"  -d {shlex.quote(body)}")
    return lines + request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True, help="the result JSON Start or Live status wrote")
    args = parser.parse_args(argv)
    try:
        result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0  # no card; the command's own output stands
    lines = card(result) if isinstance(result, dict) else []
    if lines:
        # Printed as is: wrapping would break the copyable request. On a
        # terminal the card's title and the request take their styles.
        from scripts.terminal_format import TerminalWriter
        writer, request = TerminalWriter(), lines.index("First request:") if "First request:" in lines else len(lines)
        shown = [writer.paint(line, ["heading"] * len(line)) if index in (0, request)
                 else writer.paint(line, ["literal"] * len(line)) if index > request else line
                 for index, line in enumerate(lines)]
        print("\n" + "\n".join(shown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
