#!/usr/bin/env bash
# Stop Gateway + worker daemon started by start.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="${PID_DIR:-$ROOT/tmp}"

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
    echo "[stop] $name still alive, kill -9"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
}

stop_one "worker" "$PID_DIR/worker.pid"
stop_one "gateway" "$PID_DIR/gateway.pid"
echo "[stop] done"
