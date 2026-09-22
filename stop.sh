#!/usr/bin/env bash
# Stop Gateway + worker daemons started by start.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="${PID_DIR:-$ROOT/tmp}"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
  else
    PYTHON="$(command -v python3 || command -v python)"
  fi
fi

reap_agent_locks() {
  # Kill matching agent process groups and clear DB locks before gateway dies.
  echo "[stop] reconciling agent process groups..."
  (
    cd "$ROOT"
    "$PYTHON" - <<'PY'
from app.core.settings import load_dotenv
load_dotenv()

from app.infra.db import init_db_sync
from app.services.agent.reconcile import reconcile_agent_locks
from app.services.tasks.store import TaskStore
from app.services.tasks.workspace import workspace_root

p = init_db_sync()
store = TaskStore(str(p), workspace_root=str(workspace_root()))
summary = reconcile_agent_locks(store, kill_orphans=True)
print("[stop] agent reap:", summary, flush=True)
if summary.get("errors") or summary.get("unknown"):
    raise SystemExit(1)
PY
  )
}

stop_one() {
  local name="$1"
  local pid_file="$2"
  if [[ ! -f "$pid_file" ]]; then
    echo "[stop] $name: no pid file"
    return 0
  fi
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -z "${pid:-}" ]]; then
    rm -f "$pid_file"
    echo "[stop] $name: empty pid file removed"
    return 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    echo "[stop] $name: pid $pid not running (stale pid removed)"
    return 0
  fi
  echo "[stop] $name pid=$pid"
  kill "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "[stop] $name still alive, kill -9" >&2
    if ! kill -9 "$pid" 2>/dev/null; then
      echo "[stop] $name kill -9 failed pid=$pid" >&2
    fi
    sleep 0.2
  fi
  if kill -0 "$pid" 2>/dev/null; then
    echo "[stop] $name pid=$pid still alive after kill -9; keeping $pid_file" >&2
    return 1
  fi
  rm -f "$pid_file"
}

rc=0
stop_one "worker" "$PID_DIR/worker.pid" || rc=1
stop_one "agent_worker" "$PID_DIR/agent_worker.pid" || rc=1
reap_agent_locks || rc=1
stop_one "gateway" "$PID_DIR/gateway.pid" || rc=1
if [[ "$rc" -ne 0 ]]; then
  echo "[stop] completed with errors" >&2
  exit "$rc"
fi
echo "[stop] done"
