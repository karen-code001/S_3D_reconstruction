#!/usr/bin/env bash
# Usage: ./control_start.sh [start|stop|restart|status]
# Runs the control plane (uvicorn control_plane.main:app) in the background.
# Env overrides: CONTROL_PYTHON, CONTROL_HOST (default 0.0.0.0), CONTROL_PORT (default 13000),
#                CONTROL_RELOAD=0 to disable auto-reload on code changes.
set -euo pipefail

ACTION="${1:-start}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${CONTROL_HOST:-0.0.0.0}"
PORT="${CONTROL_PORT:-13000}"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/control.log"
PIDFILE="$LOG_DIR/control.pid"

cd "$ROOT"
mkdir -p "$LOG_DIR"

find_python() {
    local candidate
    for candidate in "${CONTROL_PYTHON:-}" "$ROOT/.venv/bin/python" "$HOME/miniconda3/envs/control_plane_3Dr/bin/python"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    echo "找不到 Python，请设置 CONTROL_PYTHON" >&2
    exit 1
}

running_pid() {
    local pid
    if [[ -f "$PIDFILE" ]] && pid="$(cat "$PIDFILE")" && kill -0 "$pid" 2>/dev/null; then
        echo "$pid"
        return 0
    fi
    for pid in $(pgrep -f "uvicorn control_plane.main:app" || true); do
        if [[ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" == "$ROOT" ]]; then
            echo "$pid" > "$PIDFILE"
            echo "$pid"
            return 0
        fi
    done
    return 1
}

health_ok() {
    curl -s --noproxy '*' -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '^200$'
}

address() {
    echo "http://$(hostname -I | awk '{print $1}'):$PORT"
}

start() {
    local pid python
    if pid="$(running_pid)"; then
        echo "主控已在运行 (pid $pid): $(address)"
        return 0
    fi
    if health_ok; then
        echo "端口 $PORT 已被其他进程占用" >&2
        exit 1
    fi
    python="$(find_python)"
    local args=(-m uvicorn control_plane.main:app --host "$HOST" --port "$PORT" --no-access-log)
    [[ "${CONTROL_RELOAD:-1}" == "0" ]] || args+=(--reload)

    if [[ -f "$LOG" ]] && (( $(stat -c %s "$LOG") > 50 * 1024 * 1024 )); then
        mv -f "$LOG" "$LOG.1"
    fi
    echo "==== $(date '+%F %T') start ====" >> "$LOG"
    setsid nohup "$python" "${args[@]}" >> "$LOG" 2>&1 < /dev/null &
    echo "$!" > "$PIDFILE"

    for _ in $(seq 1 30); do
        sleep 1
        if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "启动失败，日志最后 20 行:" >&2
            tail -n 20 "$LOG" >&2
            rm -f "$PIDFILE"
            exit 1
        fi
        if health_ok; then
            echo "主控已启动 (pid $(cat "$PIDFILE")): $(address)"
            echo "日志: $LOG"
            return 0
        fi
    done
    echo "30 秒内 /health 未响应，请查看日志: $LOG" >&2
    exit 1
}

stop() {
    local pid
    if ! pid="$(running_pid)"; then
        echo "主控未在运行"
        rm -f "$PIDFILE"
        return 0
    fi
    # setsid made the server a process-group leader; signal the group so the --reload worker exits too.
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
    echo "主控已停止"
}

status() {
    local pid
    if pid="$(running_pid)"; then
        if health_ok; then
            echo "主控运行中 (pid $pid): $(address)"
        else
            echo "主控进程在 (pid $pid)，但 /health 无响应，请查看日志: $LOG"
        fi
    else
        echo "主控未在运行"
    fi
}

case "$ACTION" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) status ;;
    *) echo "用法: $0 [start|stop|restart|status]" >&2; exit 2 ;;
esac
