#!/usr/bin/env bash
# Fast read-only system overview for the workflow menu.
#   scripts/quick-status.sh [--json]
#
# Consumes scripts/inventory.sh --json (ownership classifier is not reimplemented).
# Probes GET /v1/models only — never submits a completion / inference smoke.
# Narrow test hooks (selftests only):
#   QUICK_STATUS_INVENTORY_JSON=path
#   QUICK_STATUS_INVENTORY_CMD=path   executable → inventory JSON on stdout
#   QUICK_STATUS_API_JSON=path        fixed /v1/models body (skip network)
#   QUICK_STATUS_API_CMD=path         executable → models JSON on stdout; fail=unavailable
#   QUICK_STATUS_PORT=8000
set -euo pipefail
SCRIPT_NAME=quick-status
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
SCRIPT_NAME=quick-status

JSON=0
usage() {
  cat <<'EOF'
usage: scripts/quick-status.sh [--json]

  Fast read-only overview: active managed service, API model advertisement
  (not inference health), memory on every confirmed node, managed GPU
  allocation per node when measured, reachability of other cluster nodes,
  unmanaged GPU aggregate, and stale managed container count.

  Never calls /v1/completions or any other inference endpoint.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
  shift
done

cmd_inventory_json() {
  if [ -n "${QUICK_STATUS_INVENTORY_JSON:-}" ]; then
    cat "$QUICK_STATUS_INVENTORY_JSON"
  elif [ -n "${QUICK_STATUS_INVENTORY_CMD:-}" ]; then
    "$QUICK_STATUS_INVENTORY_CMD"
  else
    "$REPO_DIR/scripts/inventory.sh" --json
  fi
}

# Probe /v1/models only. Prints body to stdout; exit 0 if parseable, 1 if unavailable.
cmd_api_models() {
  local port="${QUICK_STATUS_PORT:-${PORT:-8000}}"
  if [ -n "${QUICK_STATUS_API_JSON:-}" ]; then
    if [ ! -f "$QUICK_STATUS_API_JSON" ]; then
      return 1
    fi
    cat "$QUICK_STATUS_API_JSON"
    return 0
  fi
  if [ -n "${QUICK_STATUS_API_CMD:-}" ]; then
    "$QUICK_STATUS_API_CMD" "$port"
    return $?
  fi
  local -a auth_args=()
  api_auth_curl_args auth_args
  [ -n "${QUICK_STATUS_API_BASE:-}" ] || return 1
  curl -fsS --max-time 2 "${auth_args[@]}" "$QUICK_STATUS_API_BASE/v1/models" 2>/dev/null
}

if ! inv=$(cmd_inventory_json); then
  die "inventory collection failed — status is unavailable; run ./pulsar inventory"
fi
if ! inventory_json_is_valid "$inv"; then
  die "inventory returned invalid data — status is unavailable; run ./pulsar inventory"
fi

# Probe only an unambiguous complete managed API node, never an unrelated
# listener on localhost. This uses inventory membership and saved control IPs.
QUICK_STATUS_API_BASE=$(printf '%s' "$inv" | python3 -c '
import ipaddress,json,sys
inv=json.load(sys.stdin)
services=[s for s in inv.get("services",[]) if s.get("ownership")=="managed" and s.get("complete") and s.get("state")=="running"]
if len(services)!=1:raise SystemExit(0)
s=services[0];api=[r for r in s.get("ranks",[]) if str(r.get("rank")) in ("0","single")]
if len(api)!=1:raise SystemExit(0)
node=inv.get("nodes",{}).get(api[0]["node"],{});host=node.get("control_ip")
try:
 address=ipaddress.ip_address(host);port=int(s.get("api_port"))
 if not 1<=port<=65535:raise ValueError()
except (TypeError,ValueError):raise SystemExit(0)
print("http://"+("["+str(address)+"]" if address.version==6 else str(address))+":"+str(port))
')
api_body=""
api_rc=1
set +e
api_body=$(cmd_api_models)
api_rc=$?
set -e

PORT_SCAN="${QUICK_STATUS_PORT:-${PORT:-8000}}" \
INV_JSON="$inv" \
API_BODY="$api_body" \
API_RC="$api_rc" \
API_BASE_V="$QUICK_STATUS_API_BASE" \
WANT_JSON="$JSON" \
python3 - <<'PY'
import json
import os
import sys
from urllib.parse import urlsplit

from scripts.terminal_format import TerminalWriter

inv = json.loads(os.environ.get("INV_JSON") or "{}")
api_rc = int(os.environ.get("API_RC") or "1")
api_raw = (os.environ.get("API_BODY") or "").strip()
port = urlsplit(os.environ.get("API_BASE_V") or "").port or int(os.environ.get("PORT_SCAN") or "8000")
want_json = os.environ.get("WANT_JSON") == "1"

def fmt_gib(v):
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)

def pct_available(avail, total):
    if avail is None or total is None:
        return None
    try:
        t = float(total)
        a = float(avail)
        if t <= 0:
            return None
        return round(100.0 * a / t, 1)
    except (TypeError, ValueError):
        return None

def service_active(s):
    st = s.get("state")
    if st in ("running", "partial", "degraded"):
        return True
    return any(r.get("running") for r in (s.get("ranks") or []))

services = inv.get("services") or []
worker = inv.get("worker") or {}
nodes = inv.get("nodes") or {}
head = nodes.get("head") or {}
worker_node = nodes.get("worker") or {}
unmanaged = inv.get("unmanaged_gpu_processes") or []


def node_order(item):
    name = item[0]
    if name == "head":
        return (0, name)
    if name == "worker":
        return (1, name)
    if name.startswith("rank-"):
        try:
            return (int(name.split("-", 1)[1]), name)
        except ValueError:
            pass
    return (1_000_000, name)

def node_label(name):
    hostname = str((nodes.get(name) or {}).get("hostname") or "").strip()
    if hostname:
        return f"{hostname} (this node)" if name == "head" else hostname
    if name == "head":
        return "this node"
    return "host unavailable"



memory_nodes = []
for node_name, block in sorted(nodes.items(), key=node_order):
    if (
        node_name == "worker"
        and worker.get("status") == "unset"
        and not block.get("hostname")
        and block.get("mem_available_gib") is None
        and block.get("mem_total_gib") is None
    ):
        continue
    avail = block.get("mem_available_gib")
    total = block.get("mem_total_gib")
    memory_nodes.append({
        "name": node_name,
        "hostname": block.get("hostname"),
        "mem_available_gib": avail,
        "mem_total_gib": total,
        "available_percent": pct_available(avail, total),
        "mem_status": block.get("mem_status"),
    })

active_managed = []
stale_managed = []
for s in services:
    if s.get("ownership") != "managed":
        continue
    st = s.get("state") or ""
    if st == "stale":
        stale_managed.append(s)
    elif service_active(s):
        active_managed.append(s)

# Prefer complete running; otherwise first active managed
primary = None
for s in active_managed:
    if s.get("complete") and (s.get("state") == "running"):
        primary = s
        break
if primary is None and active_managed:
    primary = active_managed[0]

# API advertisement
api_status = "unavailable"
api_models = []
if api_rc == 0 and api_raw:
    try:
        d = json.loads(api_raw)
        api_models = [x.get("id", "") for x in (d.get("data") or []) if x.get("id")]
        api_status = "ok" if api_models else "empty"
    except Exception:
        api_status = "unavailable"
        api_models = []

if not os.environ.get("API_BASE_V") and api_rc != 0:
    api_status = "not-checked"

unmanaged_mib = 0
unmanaged_known = 0
for u in unmanaged:
    m = u.get("used_memory_mib")
    if m is not None:
        try:
            unmanaged_mib += int(m)
            unmanaged_known += 1
        except (TypeError, ValueError):
            pass

rank_gpu = []
if primary:
    for r in primary.get("ranks") or []:
        g = r.get("gpu_memory") or {}
        rank_gpu.append({
            "node": r.get("node"),
            "rank": r.get("rank"),
            "measured_mib": g.get("measured_mib"),
            "status": g.get("status"),
        })

head_avail = head.get("mem_available_gib")
head_total = head.get("mem_total_gib")
worker_avail = worker_node.get("mem_available_gib")
worker_total = worker_node.get("mem_total_gib")
head_pct = pct_available(head_avail, head_total)
worker_pct = pct_available(worker_avail, worker_total)

overview = {
    "schema_version": 1,
    "kind": "quick_status",
    "port": port,
    "active_managed_count": len(active_managed),
    "primary_service": None,
    "api": {
        "status": api_status,
        "port": port,
        "models": api_models,
        "endpoint": os.environ.get("API_BASE_V") or None,
        "note": "model advertisement only — not an inference smoke test",
    },
    "memory": {
        "nodes": memory_nodes,
        "head": {
            "mem_available_gib": head_avail,
            "mem_total_gib": head_total,
            "available_percent": head_pct,
            "mem_status": head.get("mem_status"),
        },
        "worker": {
            "mem_available_gib": worker_avail,
            "mem_total_gib": worker_total,
            "available_percent": worker_pct,
            "mem_status": worker_node.get("mem_status"),
        },
    },
    "worker": {
        "ip": worker.get("ip"),
        "status": worker.get("status"),
        "reason": worker.get("reason"),
    },
    "remote_ranks": {
        "status": worker.get("status"),
        "reason": worker.get("reason"),
    },
    "managed_gpu_per_rank": rank_gpu,
    "unmanaged_gpu": {
        "count": len(unmanaged),
        "measured_mib_aggregate": unmanaged_mib if unmanaged else 0,
        "measured_process_count": unmanaged_known,
    },
    "stale_managed": {
        "count": len(stale_managed),
        "nonblocking": False,
        "note": "stopped containers retain mount references; cleanup remains explicit",
        "confs": [s.get("conf") for s in stale_managed if s.get("conf")],
    },
    "inference_smoke": False,
}

if primary:
    exp = primary.get("expected_ranks") or []
    obs = primary.get("observed_ranks") or []
    overview["primary_service"] = {
        "conf": primary.get("conf"),
        "served_name": primary.get("served_name"),
        "state": primary.get("state"),
        "ownership": primary.get("ownership"),
        "safe_to_stop": primary.get("safe_to_stop"),
        "complete": primary.get("complete"),
        "expected_ranks": exp,
        "observed_ranks": obs,
        "api_port": primary.get("api_port"),
        "estimated_footprint_gib_per_rank": primary.get("estimated_footprint_gib_per_rank"),
    }

if want_json:
    json.dump(overview, sys.stdout, indent=2)
    print()
    raise SystemExit(0)

# Human concise output
term = TerminalWriter()
term.emit("QUICK STATUS  read-only · no inference smoke")
ps = overview["primary_service"]
if ps:
    expected_count = len(ps.get("expected_ranks") or [])
    observed_count = len(ps.get("observed_ranks") or [])
    state = str(ps.get("state") or "?").upper()
    complete = "complete" if ps.get("complete") else "incomplete"
    term.field("Model", f"{state} · {ps.get('conf') or '?'}")
    api_port = ps.get("api_port") if ps.get("api_port") is not None else port
    term.field("Serves", f"{ps.get('served_name') or '?'} on :{api_port}")
    term.field("Nodes", f"{observed_count}/{expected_count} observed · {complete}")
    if overview["active_managed_count"] > 1:
        term.field(
            "Note",
            f"{overview['active_managed_count']} active managed services · showing primary",
        )
else:
    term.field("Model", "none active")

api = overview["api"]
if api["status"] == "ok":
    models_s = ", ".join(api["models"]) or "none"
    term.field("API", f"advertised {models_s} on :{port} · model-list check only")
elif api["status"] == "empty":
    term.field("API", f"reachable on :{port} · no models advertised")
else:
    term.field("API", "not checked; no unambiguous managed endpoint" if not api.get("endpoint") else f"unavailable at {api['endpoint']}")


def mem_summary(block):
    avail = block.get("mem_available_gib")
    total = block.get("mem_total_gib")
    pct = block.get("available_percent")
    if avail is None and total is None:
        return "n/a"
    pct_s = f"{pct:.1f}% free" if pct is not None else "free% n/a"
    return f"{fmt_gib(avail)} / {fmt_gib(total)} GiB · {pct_s}"


memory_rows = overview["memory"].get("nodes") or []
if memory_rows:
    for index, block in enumerate(memory_rows):
        name = block.get("name") or "?"
        label = node_label(name)
        detail = mem_summary(block)
        if detail == "n/a" and block.get("mem_status"):
            detail += f" · {block['mem_status']}"
        term.field("Memory" if index == 0 else "", f"{label}  {detail}")
else:
    term.field("Memory", "n/a")


if rank_gpu:
    parts = []
    for r in rank_gpu:
        m = r.get("measured_mib")
        m_s = f"{float(m) / 1024:.1f} GiB" if m is not None else "n/a"
        parts.append(f"{node_label(r.get('node') or '?')} {m_s}")
    term.field("GPU", " · ".join(parts))
else:
    term.field("GPU", "n/a")

remote = overview["remote_ranks"]
remote_reason = remote.get("reason") or ""
if remote.get("status") == "unreachable":
    detail = f"UNREACHABLE{(' · ' + remote_reason) if remote_reason else ''}"
elif remote.get("status") == "unset":
    detail = "none confirmed"
elif remote.get("status") == "ok":
    detail = "all observable"
else:
    detail = str(remote.get("status") or "?").upper()
    if remote_reason:
        detail += f" · {remote_reason}"
term.field("Nodes", detail)

ug = overview["unmanaged_gpu"]
if ug["count"]:
    unmanaged_s = f"{ug['count']} unmanaged GPU"
    if ug["measured_process_count"]:
        unmanaged_s += f" ({ug['measured_mib_aggregate']:,} MiB)"
else:
    unmanaged_s = "no unmanaged GPU"

st = overview["stale_managed"]
if st["count"]:
    confs = ", ".join(st.get("confs") or []) or "?"
    stale_s = f"{st['count']} stale ({confs}) · stopped; mount references retained"
else:
    stale_s = "0 stale"
term.field("Other", f"{unmanaged_s} · {stale_s}")
PY
