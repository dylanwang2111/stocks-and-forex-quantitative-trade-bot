#!/usr/bin/env bash
# manage.sh — start/stop/restart the trade bot and dashboard
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
DATE="$(date +%Y%m%d)"
BOT_LOG="$LOG_DIR/paper_${DATE}.log"
DASH_LOG="$LOG_DIR/dashboard_${DATE}.log"

BOT_PATTERN="main.py --mode paper"
DASH_PATTERN="uvicorn dashboard_v2:app"

_pids() { pgrep -f "$1" 2>/dev/null || true; }

_stop() {
    local label="$1" pattern="$2"
    local pids
    pids=$(_pids "$pattern")
    if [ -n "$pids" ]; then
        echo "$pids" | xargs kill
        echo "Stopped $label (PIDs: $pids)"
    else
        echo "$label is not running"
    fi
}

_start_bot() {
    if [ -n "$(_pids "$BOT_PATTERN")" ]; then
        echo "Bot is already running"
        return
    fi
    nohup python "$SCRIPT_DIR/main.py" --mode paper >> "$BOT_LOG" 2>&1 &
    echo "Bot started (PID $!) — logging to $BOT_LOG"
}

_start_dash() {
    if [ -n "$(_pids "$DASH_PATTERN")" ]; then
        echo "Dashboard is already running"
        return
    fi
    nohup uvicorn dashboard_v2:app --host 0.0.0.0 --port 8050 >> "$DASH_LOG" 2>&1 &
    echo "Dashboard started (PID $!) — http://localhost:8050 — logging to $DASH_LOG"
}

_status() {
    local bot_pids dash_pids
    bot_pids=$(_pids "$BOT_PATTERN")
    dash_pids=$(_pids "$DASH_PATTERN")
    echo "Bot:       ${bot_pids:-not running}"
    echo "Dashboard: ${dash_pids:-not running}"
}

cmd="${1:-help}"
target="${2:-all}"   # all | bot | dashboard

case "$cmd" in
    start)
        case "$target" in
            all)       _start_bot; _start_dash ;;
            bot)       _start_bot ;;
            dashboard) _start_dash ;;
            *) echo "Unknown target: $target"; exit 1 ;;
        esac ;;
    stop)
        case "$target" in
            all)       _stop "Bot" "$BOT_PATTERN"; _stop "Dashboard" "$DASH_PATTERN" ;;
            bot)       _stop "Bot" "$BOT_PATTERN" ;;
            dashboard) _stop "Dashboard" "$DASH_PATTERN" ;;
            *) echo "Unknown target: $target"; exit 1 ;;
        esac ;;
    restart)
        case "$target" in
            all)
                _stop "Bot" "$BOT_PATTERN"; _stop "Dashboard" "$DASH_PATTERN"
                sleep 1
                _start_bot; _start_dash ;;
            bot)
                _stop "Bot" "$BOT_PATTERN"; sleep 1; _start_bot ;;
            dashboard)
                _stop "Dashboard" "$DASH_PATTERN"; sleep 1; _start_dash ;;
            *) echo "Unknown target: $target"; exit 1 ;;
        esac ;;
    status)
        _status ;;
    *)
        echo "Usage: $0 {start|stop|restart|status} [all|bot|dashboard]"
        echo ""
        echo "  $0 start            # start bot + dashboard"
        echo "  $0 stop             # stop  bot + dashboard"
        echo "  $0 restart          # restart both"
        echo "  $0 restart dashboard"
        echo "  $0 restart bot"
        echo "  $0 status"
        ;;
esac
