#!/usr/bin/env bash
set -euo pipefail
# manage.sh — bot & dashboard process manager
# Usage:
#   ./manage.sh start              — start bot + dashboard
#   ./manage.sh start bot          — start bot only
#   ./manage.sh start dashboard    — start dashboard only
#   ./manage.sh stop               — stop bot + dashboard
#   ./manage.sh stop bot           — stop bot only
#   ./manage.sh stop dashboard     — stop dashboard only
#   ./manage.sh restart            — restart bot + dashboard
#   ./manage.sh restart bot        — restart bot only
#   ./manage.sh restart dashboard  — restart dashboard only
#   ./manage.sh status             — show running status + last 10 log lines
#   ./manage.sh watchdog           — start bot if not running (for cron)

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-/home/dylannguyen/anaconda3/envs/bmo_venv/bin/python}"
LOG_FILE="$BOT_DIR/logs/paper_$(date +%Y%m%d).log"
DASH_LOG="$BOT_DIR/logs/dashboard_$(date +%Y%m%d).log"
PIDFILE="$BOT_DIR/logs/bot.pid"
DASH_PIDFILE="$BOT_DIR/logs/dashboard.pid"

is_running() {
    local pidfile="$1"
    if [ -f "$pidfile" ]; then
        local pid
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$pidfile"  # stale PID file — clean up
    fi
    return 1
}

start_bot() {
    if is_running "$PIDFILE"; then
        echo "Bot already running (PID $(cat $PIDFILE))"
        return
    fi
    cd "$BOT_DIR" || exit 1
    nohup "$PYTHON" main.py --mode paper >> "$LOG_FILE" 2>&1 &
    echo $! > "$PIDFILE"
    echo "Bot started (PID $!), logging to $LOG_FILE"
}

stop_bot() {
    if is_running "$PIDFILE"; then
        kill "$(cat $PIDFILE)" && rm -f "$PIDFILE"
        echo "Bot stopped."
    else
        echo "Bot not running."
    fi
}

start_dashboard() {
    if is_running "$DASH_PIDFILE"; then
        echo "Dashboard already running (PID $(cat $DASH_PIDFILE))"
        return
    fi
    cd "$BOT_DIR" || exit 1
    nohup "$PYTHON" -m uvicorn dashboard_v2:app --host 0.0.0.0 --port 8050 >> "$DASH_LOG" 2>&1 &
    echo $! > "$DASH_PIDFILE"
    echo "Dashboard started (PID $!), logging to $DASH_LOG"
    echo "Open http://localhost:8050"
}

stop_dashboard() {
    if is_running "$DASH_PIDFILE"; then
        kill "$(cat $DASH_PIDFILE)" && rm -f "$DASH_PIDFILE"
        echo "Dashboard stopped."
    else
        echo "Dashboard not running."
    fi
}

case "$1" in
  start)
    case "${2:-}" in
      bot)        start_bot ;;
      dashboard)  start_dashboard ;;
      "")         start_bot; start_dashboard ;;
      *)          echo "Usage: $0 start [bot|dashboard]"; exit 1 ;;
    esac
    ;;
  stop)
    case "${2:-}" in
      bot)        stop_bot ;;
      dashboard)  stop_dashboard ;;
      "")         stop_bot; stop_dashboard ;;
      *)          echo "Usage: $0 stop [bot|dashboard]"; exit 1 ;;
    esac
    ;;
  restart)
    case "${2:-}" in
      bot)        stop_bot;       sleep 1; start_bot ;;
      dashboard)  stop_dashboard; sleep 1; start_dashboard ;;
      "")         stop_bot; stop_dashboard; sleep 1; start_bot; start_dashboard ;;
      *)          echo "Usage: $0 restart [bot|dashboard]"; exit 1 ;;
    esac
    ;;
  status)
    if is_running "$PIDFILE"; then
        echo "Bot is RUNNING (PID $(cat $PIDFILE))"
    else
        echo "Bot is STOPPED"
    fi
    if is_running "$DASH_PIDFILE"; then
        echo "Dashboard is RUNNING (PID $(cat $DASH_PIDFILE)) — http://localhost:8050"
    else
        echo "Dashboard is STOPPED"
    fi
    echo "--- Last 10 bot log lines ---"
    tail -10 "$LOG_FILE" 2>/dev/null
    ;;
  watchdog)
    if ! is_running "$PIDFILE"; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Bot not running — restarting..." >> "$BOT_DIR/logs/watchdog.log"
        start_bot
    fi
    if ! is_running "$DASH_PIDFILE"; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Dashboard not running — restarting..." >> "$BOT_DIR/logs/watchdog.log"
        start_dashboard
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|watchdog} [bot|dashboard]"
    exit 1
    ;;
esac
