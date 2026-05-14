#!/bin/bash
# Start the backtest platform (Linux/macOS)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/app.pid"
LOG_FILE="$SCRIPT_DIR/output.log"

# Check if already running
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "[INFO] Service is already running (PID: $PID)"
        echo "[INFO] Log: $LOG_FILE"
        exit 0
    else
        echo "[WARN] Stale PID file found, cleaning up..."
        rm -f "$PID_FILE"
    fi
fi

cd "$SCRIPT_DIR" || exit 1

# Prefer python3, fall back to python
PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)
if [ -z "$PYTHON" ]; then
    echo "[ERR] Python not found in PATH"
    exit 1
fi

# Start server in background, append to log
nohup "$PYTHON" run.py >> "$LOG_FILE" 2>&1 &
APP_PID=$!
echo $APP_PID > "$PID_FILE"

# Brief wait to confirm process is alive
sleep 1
if kill -0 "$APP_PID" 2>/dev/null; then
    echo "[OK] Service started (PID: $APP_PID)"
    echo "[OK] Log: $LOG_FILE"
else
    echo "[ERR] Service failed to start. Check log: $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
