#!/usr/bin/env bash
# One-shot start: Gateway + worker daemon
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PID_DIR="${PID_DIR:-$ROOT/tmp}"
LOG_DIR="${LOG_DIR:-$ROOT/tmp}"
mkdir -p "$PID_DIR" "$LOG_DIR"

GATEWAY_PID_FILE="$PID_DIR/gateway.pid"
WORKER_PID_FILE="$PID_DIR/worker.pid"
GATEWAY_LOG="$LOG_DIR/gateway.log"
WORKER_LOG="$LOG_DIR/worker.log"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
  else
    PYTHON="$(command -v python3 || command -v python)"
  fi
fi

PORT="${PORT:-8000}"
HOST_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:${PORT}}"
export LARKSUITE_CLI_NO_UPDATE_NOTIFIER="${LARKSUITE_CLI_NO_UPDATE_NOTIFIER:-1}"
export LARKSUITE_CLI_NO_SKILLS_NOTIFIER="${LARKSUITE_CLI_NO_SKILLS_NOTIFIER:-1}"
export GATEWAY_BASE_URL="${GATEWAY_BASE_URL:-$HOST_URL}"

is_running() {
  local pid_file="$1"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

wait_health() {
  local url="$1/health"
  local i
  for i in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

if is_running "$GATEWAY_PID_FILE"; then
  echo "[start] gateway already running pid=$(cat "$GATEWAY_PID_FILE")"
else
  echo "[start] starting gateway → $GATEWAY_LOG"
  nohup "$PYTHON" "$ROOT/run.py" >>"$GATEWAY_LOG" 2>&1 &
  echo $! >"$GATEWAY_PID_FILE"
fi

echo "[start] waiting for $GATEWAY_BASE_URL/health ..."
if ! wait_health "$GATEWAY_BASE_URL"; then
  echo "[start] gateway health check failed; see $GATEWAY_LOG" >&2
  exit 1
fi
echo "[start] gateway ok"

if is_running "$WORKER_PID_FILE"; then
  echo "[start] worker already running pid=$(cat "$WORKER_PID_FILE")"
else
  echo "[start] starting worker → $WORKER_LOG"
  nohup "$PYTHON" "$ROOT/run_worker.py" >>"$WORKER_LOG" 2>&1 &
  echo $! >"$WORKER_PID_FILE"
fi

echo "[start] done"
echo "  gateway pid=$(cat "$GATEWAY_PID_FILE")  log=$GATEWAY_LOG"
echo "  worker  pid=$(cat "$WORKER_PID_FILE")  log=$WORKER_LOG"
echo "  stop:   $ROOT/stop.sh"
