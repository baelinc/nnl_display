#!/bin/bash
# fpp_plugin.sh  —  NNL FPP Plugin v1.0.0 Lifecycle Hook
# ─────────────────────────────────────────────────────────
# Starts a single Python launcher that runs both the display engine
# and the settings web server in the same process — this allows the
# web server to read the shared canvas for on-demand preview rendering.
#
# FPP calls this with:  postStart | preStop | status
# You can also call it manually:  start | stop | restart

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$PLUGIN_DIR/nnl_display/launcher.py"
PIDFILE="/tmp/nnl_launcher.pid"
LOGFILE="/tmp/nnl_display.log"
PYTHON="/usr/bin/python3"

is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; }

install_deps() {
    $PYTHON -c "import PIL, requests" 2>/dev/null || \
        $PYTHON -m pip install --quiet Pillow requests
}

do_start() {
    is_running && { echo "NNL already running (pid $(cat $PIDFILE))"; return 0; }
    echo "Starting NNL Display + Settings UI..."
    install_deps
    sudo $PYTHON "$LAUNCHER" >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 1
    if is_running; then
        local ip; ip=$(hostname -I | awk '{print $1}')
        echo "✅ NNL started (pid $(cat $PIDFILE))"
        echo "   Settings UI: http://${ip}:8180"
        echo "   Log: $LOGFILE"
    else
        echo "❌ NNL failed to start — check $LOGFILE"
    fi
}

do_stop() {
    if is_running; then
        local pid; pid=$(cat "$PIDFILE")
        echo "Stopping NNL (pid $pid)..."
        sudo kill "$pid" 2>/dev/null
        sleep 1
        sudo kill -9 "$pid" 2>/dev/null
        rm -f "$PIDFILE"
        echo "Stopped."
    else
        echo "NNL was not running."
    fi
}

case "$1" in
    postStart|start)   do_start  ;;
    preStop|stop)      do_stop   ;;
    restart)           do_stop; sleep 1; do_start ;;
    status)
        if is_running; then
            ip=$(hostname -I | awk '{print $1}')
            echo "running (pid $(cat $PIDFILE)) — http://${ip}:8180"
        else
            echo "stopped"
        fi
        ;;
    *)  echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
