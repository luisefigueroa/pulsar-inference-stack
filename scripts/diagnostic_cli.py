"""Closed diagnostic document commands and owner-authorized data helpers."""
from pathlib import Path
import json
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.diagnostic_run import helper_main


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    raw = "--raw" in args
    if raw:
        args.remove("--raw")
    try:
        result = helper_main(args)
        if raw:
            if isinstance(result, list):
                sys.stdout.buffer.write(("\0".join(result) + "\0").encode())
            else:
                print(result if isinstance(result, str) else json.dumps(result))
        else:
            print(json.dumps({"schema_version": 1, "ok": True, "result": result}, sort_keys=True))
        if isinstance(result, dict) and result.get("ok") is False:
            return 3
        return 0
    except Exception as exc:
        print(json.dumps({"schema_version": 1, "ok": False, "error": {"code": "diagnostic_refused", "message": str(exc)[:300]}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
