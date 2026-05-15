#!/usr/bin/env bash
# Job Tracker startup script.
# Clears port conflicts, checks the database, then starts backend + frontend.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
DB_PATH="$SCRIPT_DIR/.job-tracker/applications.db"
LOG_DIR="$SCRIPT_DIR/.job-tracker/logs"
PID_DIR="$SCRIPT_DIR/.job-tracker"
PLIST_API="$HOME/Library/LaunchAgents/com.jobtracker.api.plist"
API_PORT=8000
FRONTEND_PORT=5173

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

step() { printf "\n${BLUE}==>  ${BOLD}%s${NC}\n" "$*"; }
ok()   { printf "  ${GREEN}✓${NC}  %s\n" "$*"; }
warn() { printf "  ${YELLOW}!${NC}  %s\n" "$*"; }
die()  { printf "  ${RED}✗${NC}  %s\n" "$*" >&2; exit 1; }

# ── Unload the launchd API service so KeepAlive can't race us back onto the port
unload_launchd_api() {
    if [[ -f "$PLIST_API" ]] && launchctl list com.jobtracker.api &>/dev/null; then
        launchctl unload "$PLIST_API" 2>/dev/null || true
        warn "Unloaded com.jobtracker.api launchd service (run 'launchctl load $PLIST_API' to re-enable it)"
    fi
}

# ── Kill every process holding a given port (SIGTERM, then SIGKILL after 1 s)
free_port() {
    local port=$1
    local pids pid cmd
    pids=$(lsof -ti :"$port" 2>/dev/null || true)
    if [[ -z "$pids" ]]; then
        ok "Port $port is free"; return
    fi
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        cmd=$(ps -p "$pid" -o command= 2>/dev/null | cut -c1-72 || echo "unknown")
        warn "Port $port occupied by PID $pid: $cmd"
        kill -TERM "$pid" 2>/dev/null || true
    done <<< "$pids"
    sleep 1
    pids=$(lsof -ti :"$port" 2>/dev/null || true)
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        warn "PID $pid still alive — sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
    done <<< "$pids"
    ok "Port $port freed"
}

# ── SQLite structural integrity check
check_db() {
    if [[ ! -f "$DB_PATH" ]]; then
        warn "No database file found — will be created on first start"; return
    fi
    local result
    result=$(sqlite3 "$DB_PATH" "PRAGMA integrity_check;" 2>&1)
    if [[ "$result" == "ok" ]]; then
        ok "Database integrity: OK"
    else
        die "Database integrity check FAILED:\n$result"
    fi
}

# ── Ensure all tables and the PollerState seed row exist
init_db() {
    local err
    if ! err=$(PYTHONPATH="$SCRIPT_DIR" "$VENV_PYTHON" -c \
            "from backend.db.data_store import DataStore; DataStore()" 2>&1); then
        die "Database init failed: $err"
    fi
    ok "Database tables ensured"
}

# ── Start uvicorn in the background; fail fast if it crashes within 2 s
start_backend() {
    mkdir -p "$LOG_DIR" "$PID_DIR"
    PYTHONPATH="$SCRIPT_DIR" "$VENV_PYTHON" -m uvicorn backend.main:app \
        --host localhost --port "$API_PORT" \
        >> "$LOG_DIR/api.log" 2>> "$LOG_DIR/api_error.log" &
    local pid=$!
    echo "$pid" > "$PID_DIR/api.pid"
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
        die "Backend exited immediately — check $LOG_DIR/api_error.log"
    fi
    ok "Backend   PID=$pid   http://localhost:$API_PORT"
}

# ── Start Vite dev server in the background
start_frontend() {
    mkdir -p "$LOG_DIR" "$PID_DIR"
    (cd "$SCRIPT_DIR/frontend" && npm run dev \
        >> "$LOG_DIR/frontend.log" 2>> "$LOG_DIR/frontend_error.log") &
    local pid=$!
    echo "$pid" > "$PID_DIR/frontend.pid"
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
        die "Frontend exited immediately — check $LOG_DIR/frontend_error.log"
    fi
    ok "Frontend  PID=$pid   http://localhost:$FRONTEND_PORT"
}

# ─────────────────────────────────────────────────────────────────────────────

printf "\n${BOLD}Job Tracker — startup${NC}\n"

step "Clearing port conflicts"
unload_launchd_api
free_port "$API_PORT"
free_port "$FRONTEND_PORT"

step "Database"
check_db
init_db

step "Starting services"
start_backend
start_frontend

printf "\n${GREEN}${BOLD}All systems up.${NC}\n"
printf "  API      →  http://localhost:%s\n" "$API_PORT"
printf "  Frontend →  http://localhost:%s\n" "$FRONTEND_PORT"
printf "  Logs     →  %s\n" "$LOG_DIR"
printf "  PIDs     →  %s/{api,frontend}.pid\n\n" "$PID_DIR"
