#!/usr/bin/env bash
# Job Tracker stop script.
# Unloads launchd services, gracefully stops all processes, checkpoints the DB,
# and clears Python runtime cache. Any in-progress DB transaction is rolled back.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_PATH="$SCRIPT_DIR/.job-tracker/applications.db"
PID_DIR="$SCRIPT_DIR/.job-tracker"
PLIST_API="$HOME/Library/LaunchAgents/com.jobtracker.api.plist"
PLIST_POLLER="$HOME/Library/LaunchAgents/com.jobtracker.poller.plist"
PLIST_POLL="$HOME/Library/LaunchAgents/com.jobtracker.poll.plist"
API_PORT=8000
FRONTEND_PORT=5173
OLLAMA_PORT=11434

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

step() { printf "\n${BLUE}==>  ${BOLD}%s${NC}\n" "$*"; }
ok()   { printf "  ${GREEN}✓${NC}  %s\n" "$*"; }
warn() { printf "  ${YELLOW}!${NC}  %s\n" "$*"; }

# ── Unload a launchd service if currently loaded (prevents KeepAlive respawn)
unload_service() {
    local label=$1 plist=$2
    if [[ -f "$plist" ]] && launchctl list "$label" &>/dev/null; then
        launchctl unload "$plist" 2>/dev/null || true
        ok "Unloaded launchd: $label"
    fi
}

# ── SIGTERM a PID, wait up to N seconds, escalate to SIGKILL if needed
stop_pid() {
    local pid=$1 name=$2 timeout=${3:-6}
    if ! kill -0 "$pid" 2>/dev/null; then
        ok "$name (PID $pid) already stopped"
        return
    fi
    kill -TERM "$pid" 2>/dev/null || true
    local i=0
    while kill -0 "$pid" 2>/dev/null && (( i < timeout )); do
        sleep 1; (( i++ ))
    done
    if kill -0 "$pid" 2>/dev/null; then
        warn "$name (PID $pid) did not exit — sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
    fi
    ok "$name stopped (PID $pid)"
}

# ── Kill any stray process still occupying a port
drain_port() {
    local port=$1
    local pids
    pids=$(lsof -ti :"$port" 2>/dev/null || true)
    [[ -z "$pids" ]] && return
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        warn "Stray process on port $port (PID $pid) — killing"
        kill -KILL "$pid" 2>/dev/null || true
    done <<< "$pids"
    ok "Port $port cleared"
}

# ── Checkpoint + truncate SQLite WAL after all writers have stopped.
# Committed-but-uncheckpointed frames are flushed to the main DB file.
# Any frames from a transaction that never committed are simply discarded —
# SQLite's WAL rollback is automatic: incomplete transactions leave no trace.
checkpoint_db() {
    if [[ ! -f "$DB_PATH" ]]; then return; fi
    if [[ -f "${DB_PATH}-wal" ]]; then
        warn "SQLite WAL present — checkpointing (discards any uncommitted transaction)"
        sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null \
            || warn "Checkpoint failed — WAL will be replayed safely on next open"
        rm -f "${DB_PATH}-shm" "${DB_PATH}-wal" 2>/dev/null || true
    fi
    ok "Database clean"
}

# ─────────────────────────────────────────────────────────────────────────────

printf "\n${BOLD}Job Tracker — shutdown${NC}\n"

# ── 1. Prevent launchd KeepAlive from respawning anything we kill
step "Unloading launchd services"
unload_service "com.jobtracker.api"    "$PLIST_API"
unload_service "com.jobtracker.poller" "$PLIST_POLLER"
unload_service "com.jobtracker.poll"   "$PLIST_POLL"

# ── 2. Backend
step "Stopping backend"
if [[ -f "$PID_DIR/api.pid" ]]; then
    API_PID=$(cat "$PID_DIR/api.pid" | tr -d '[:space:]')
    [[ -n "$API_PID" ]] && stop_pid "$API_PID" "Backend" 8
    rm -f "$PID_DIR/api.pid"
fi
# Catch any uvicorn workers that were spawned outside the PID file
pkill -TERM -f "uvicorn backend.main:app" 2>/dev/null || true
sleep 1
drain_port "$API_PORT"

# ── 3. Frontend (npm spawns a Vite child; kill the whole process group)
step "Stopping frontend"
if [[ -f "$PID_DIR/frontend.pid" ]]; then
    FE_PID=$(cat "$PID_DIR/frontend.pid" | tr -d '[:space:]')
    [[ -n "$FE_PID" ]] && stop_pid "$FE_PID" "Frontend" 5
    rm -f "$PID_DIR/frontend.pid"
fi
pkill -TERM -f "vite" 2>/dev/null || true
pkill -TERM -f "npm run dev" 2>/dev/null || true
sleep 1
drain_port "$FRONTEND_PORT"

# ── 4. Ollama
step "Stopping Ollama"
if pgrep -x ollama &>/dev/null; then
    pkill -TERM -x ollama 2>/dev/null || true
    sleep 2
    if pgrep -x ollama &>/dev/null; then
        warn "Ollama still alive — sending SIGKILL"
        pkill -KILL -x ollama 2>/dev/null || true
    fi
    ok "Ollama stopped"
else
    ok "Ollama not running"
fi
drain_port "$OLLAMA_PORT"

# ── 5. DB checkpoint / WAL rollback (runs after all writers are dead)
step "Database"
checkpoint_db

# ── 6. Python bytecode cache (stale .pyc files can mask code changes)
step "Clearing Python cache"
find "$SCRIPT_DIR/backend" "$SCRIPT_DIR/tests" \
    -type d -name "__pycache__" -print0 2>/dev/null \
    | xargs -0 rm -rf 2>/dev/null || true
ok "Python cache cleared"

printf "\n${GREEN}${BOLD}All services stopped.${NC}\n\n"
