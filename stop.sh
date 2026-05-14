#!/bin/bash
# Stop the backtest platform (Linux/macOS)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/app.pid"

stop_by_pid() {
    local PID=$1
    if kill -0 "$PID" 2>/dev/null; then
        # Kill the entire process group (covers uvicorn reload workers)
        kill -- -"$PID" 2>/dev/null || kill "$PID"
        # Wait up to 5s for clean exit
        for i in $(seq 1 5); do
            sleep 1
            kill -0 "$PID" 2>/dev/null || break
        done
        if kill -0 "$PID" 2>/dev/null; then
            echo "[WARN] Process $PID did not exit cleanly, forcing..."
            kill -9 "$PID" 2>/dev/null
        fi
        echo "[OK] Service stopped (PID: $PID)"
    else
        echo "[WARN] Process $PID is not running"
    fi
    rm -f "$PID_FILE"
}

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    stop_by_pid "$PID"
else
    echo "[WARN] PID file not found, searching for process..."
    PID=$(pgrep -f "python3?.*run\.py" | head -1)
    if [ -n "$PID" ]; then
        stop_by_pid "$PID"
    else
        echo "[INFO] No running service found."
    fi
fi
