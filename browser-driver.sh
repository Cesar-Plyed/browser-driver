#!/usr/bin/env bash
# browser-driver.sh — manage the persistent browser-automation driver.
#
#   browser-driver.sh start              start the server if it isn't running
#   browser-driver.sh status             is it listening? which helpers?
#   browser-driver.sh send '<json>' [t]  send one command, print the result
#   browser-driver.sh stop               stop the server (browser stays open)
#   browser-driver.sh restart            stop + start
#
# Examples:
#   browser-driver.sh send '{"fn":"state","kwargs":{}}'
#   browser-driver.sh send '{"fn":"screenshot","kwargs":{"path":"/tmp/opencode/s.png"}}'
#   browser-driver.sh send '{"fn":"goto_and_wait","kwargs":{"url":"https://www.canva.com/","settle":8}}' 120
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${BROWSER_DRIVER_PORT:-8765}"
OUT="${BROWSER_DRIVER_OUT:-/tmp/opencode/driver.out}"
PIDFILE="/tmp/opencode/driver.pid"

pid_on_port() { ss -lptnH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1; }

start() {
  if [ -n "$(pid_on_port)" ]; then echo "driver already running on :$PORT"; return 0; fi
  mkdir -p "$(dirname "$OUT")"
  setsid nohup python3 "$HERE/driver.py" --port "$PORT" --log "${OUT%.out}.log" \
    > "$OUT" 2>&1 < /dev/null &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 20); do
    if [ -n "$(pid_on_port)" ]; then echo "driver started on :$PORT (pid $(pid_on_port))"; return 0; fi
    sleep 0.5
  done
  echo "FAILED to start; last output:"; tail -5 "$OUT"; return 1
}

stop() {
  local pid; pid="$(pid_on_port)"
  if [ -z "$pid" ]; then echo "driver not running"; return 0; fi
  kill "$pid"; sleep 1
  echo "driver stopped (pid $pid). The browser window stays open."
}

status() {
  local pid; pid="$(pid_on_port)"
  if [ -z "$pid" ]; then echo "driver NOT running on :$PORT (use: browser-driver.sh start)"; return 1; fi
  echo "driver running on :$PORT (pid $pid)"
}

send() {
  [ -n "$(pid_on_port)" ] || start >/dev/null || return 1
  python3 - "$1" "${2:-300}" "$PORT" <<'PY'
import json, socket, sys, time
cmd, timeout, port = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
s = socket.create_connection(("127.0.0.1", port), timeout=10)
s.settimeout(timeout)
s.sendall((cmd + "\n").encode())
buf = b""
while b"\n" not in buf:
    chunk = s.recv(65536)
    if not chunk:
        print(json.dumps("DRIVER CLOSED CONNECTION")); sys.exit(1)
    buf += chunk
print(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
PY
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  send) shift; send "$@" ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
