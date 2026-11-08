#!/usr/bin/env bash
# Run Agent Forge locally: the TypeScript actor host + the Python API, then print every URL.
#
#   ./run.sh              full stack: real TS actors (with alarms) on :8787 + API on :8080
#   ./run.sh --memory     API only, actors emulated in-process (no Node needed)
#   ./run.sh test         workflow validator + Python tests + TS tests + contract test
#   ./run.sh smoke        start the stack, run scripts/smoke.py against it, stop
#
# Env: API_PORT (8080), STATE_PORT (8787). Settings come from .env (copy .env.example);
# with no TELNYX_API_KEY everything runs offline (fake Telnyx API, stub chat model).
# You can also paste keys/tokens into the console's Settings tab: they override .env.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
API_PORT="${API_PORT:-8080}"
STATE_PORT="${STATE_PORT:-8787}"
MODE="${1:-full}"
LOG_DIR="${TMPDIR:-/tmp}"; LOG_DIR="${LOG_DIR%/}/agent-forge"
mkdir -p "$LOG_DIR"
PIDS=()

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

# Kill a background job and everything it started (npm -> tsx -> node, python -> uvicorn).
kill_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$child"; done
  kill "$pid" 2>/dev/null || true
}

cleanup() {
  for p in "${PIDS[@]:-}"; do [[ -n "$p" ]] && kill_tree "$p"; done
}
trap cleanup EXIT INT TERM

need_python() {
  command -v python3 >/dev/null || { echo "python3 (3.11+) is required"; exit 1; }
  if ! python3 -c "import uvicorn" 2>/dev/null; then
    say "installing uvicorn (local dev server only)"
    python3 -m pip install --quiet uvicorn
  fi
  if [[ "$MODE" == "test" ]] && ! python3 -c "import pytest, pytest_asyncio" 2>/dev/null; then
    say "installing pytest + pytest-asyncio (tests only)"
    python3 -m pip install --quiet pytest pytest-asyncio
  fi
}

need_node() {
  command -v node >/dev/null || { warn "Node.js not found: use ./run.sh --memory"; exit 1; }
  if [[ ! -d state/node_modules ]]; then say "npm install (state/)"; (cd state && npm install --silent); fi
}

port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

wait_http() {
  for _ in $(seq 1 60); do curl -fsS "$1" >/dev/null 2>&1 && return 0; sleep 0.25; done
  return 1
}

# Read .env for the tokens we print (the API loads .env itself).
dotenv() { [[ -f .env ]] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true; }

start_state() {
  port_free "$STATE_PORT" || { warn "port $STATE_PORT is busy (set STATE_PORT=...)"; exit 1; }
  say "starting actor host (forge-state) on :$STATE_PORT"
  STATE_TOKEN="${STATE_TOKEN:-$(dotenv STATE_TOKEN)}"
  export STATE_TOKEN="${STATE_TOKEN:-dev-token}"
  (cd state && PORT="$STATE_PORT" API_URL="http://localhost:$API_PORT" npm run --silent dev >"$LOG_DIR/state.log" 2>&1) &
  PIDS+=($!)
  wait_http "http://localhost:$STATE_PORT/health" || { warn "actor host didn't start; see $LOG_DIR/state.log"; exit 1; }
}

start_api() {
  port_free "$API_PORT" || { warn "port $API_PORT is busy (set API_PORT=...)"; exit 1; }
  say "starting API (forge-api) on :$API_PORT"
  local extra=()
  if [[ "$1" == "full" ]]; then extra=(STATE_URL="http://localhost:$STATE_PORT" STATE_TOKEN="$STATE_TOKEN"); fi
  (cd api && env API_URL="${API_URL_OVERRIDE:-http://localhost:$API_PORT}" "${extra[@]}" \
      python3 -m uvicorn app:app --port "$API_PORT" --interface asgi3 --lifespan off --log-level warning \
      >"$LOG_DIR/api.log" 2>&1) &
  PIDS+=($!)
  wait_http "http://localhost:$API_PORT/health" || { warn "API didn't start; see $LOG_DIR/api.log"; tail -20 "$LOG_DIR/api.log"; exit 1; }
}

banner() {
  local b="http://localhost:$API_PORT" admin
  admin="$(dotenv ADMIN_TOKEN)"
  cat <<EOF

  Agent Forge is running ($1)

  Console (UI)        $b/ui
  API docs            $b/docs
  OpenAPI JSON        $b/openapi.json
  Health              $b/health        deep: $b/ops/health
  Metrics             $b/ops/metrics?window=5
  Forge MCP           $b/mcp                      (POST, JSON-RPC)
  Clinic MCP          $b/clinic/mcp               (POST, JSON-RPC)
  Forge variables     $b/webhook/dynamic-variables
  Clinic variables    $b/clinic/webhook/dynamic-variables
EOF
  [[ "$1" == "full stack" ]] && echo "  Actor host          http://localhost:$STATE_PORT/health   (RPC: /rpc, token STATE_TOKEN)"
  cat <<EOF

  Admin token         ${admin:-<none set: console is open locally>}
  Mode                $( [[ -n "$(dotenv TELNYX_API_KEY)" ]] && echo "live Telnyx API key from .env" || echo "offline: fake Telnyx API, stub chat model (add a key in Settings to chat)" )
  Logs                $LOG_DIR/api.log  $LOG_DIR/state.log

  Try:  python3 scripts/smoke.py --base $b
        python3 scripts/ops_watch.py --base $b ${admin:+--admin-token $admin}
  Ctrl+C stops everything.

EOF
}

case "$MODE" in
  test)
    need_python; need_node
    say "workflow validator"; python3 scripts/validate_workflow.py
    say "python tests"; (cd api && python3 -m pytest -q)
    say "typescript tests"; (cd state && npm test --silent && npm run --silent typecheck)
    say "contract test: python suite against the real TypeScript actors"
    export STATE_TOKEN=dev-token
    STATE_PORT=$((STATE_PORT + 11)) ; start_state
    (cd api && STATE_URL_TEST="http://localhost:$STATE_PORT" python3 -m pytest -q)
    say "all green"
    ;;
  smoke)
    need_python; need_node; start_state; start_api full
    python3 scripts/smoke.py --base "http://localhost:$API_PORT" --admin-token "$(dotenv ADMIN_TOKEN)" --mcp-token "$(dotenv MCP_TOKEN)"
    ;;
  --memory|memory)
    need_python; start_api memory; banner "in-memory"
    wait
    ;;
  full|"")
    need_python; need_node; start_state; start_api full; banner "full stack"
    wait
    ;;
  *) echo "usage: ./run.sh [--memory|test|smoke]"; exit 2 ;;
esac
