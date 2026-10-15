#!/bin/bash
# fn-mail-assistant 守护启动器：崩溃自动重启（退避 3s→60s）
APPDEST="${TRIM_APPDEST:-/var/apps/fn-mail-assistant/app}"
PKGVAR="${TRIM_PKGVAR:-/var/apps/fn-mail-assistant/var}"
mkdir -p "$PKGVAR/run" "$PKGVAR/logs" "$PKGVAR/tmp"
STOP_FLAG="$PKGVAR/run/stop.flag"
LOG="$PKGVAR/logs/launcher.log"
echo $$ > "$PKGVAR/run/launcher.pid"

BACKOFF=3
trap 'exit 0' INT TERM

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [launcher] $*" >> "$LOG"; }

log "launcher started pid=$$"
while true; do
    if [ -f "$STOP_FLAG" ]; then
        rm -f "$STOP_FLAG"
        break
    fi
    log "starting python server.py"
    python3 "$APPDEST/server.py" >> "$LOG" 2>&1 &
    PID=$!
    echo $PID > "$PKGVAR/run/server.pid"
    wait $PID
    CODE=$?
    log "python exited code=$CODE"
    if [ -f "$STOP_FLAG" ]; then
        rm -f "$STOP_FLAG"
        break
    fi
    log "restart after ${BACKOFF}s (backoff)"
    sleep $BACKOFF
    [ "$BACKOFF" -lt 60 ] && BACKOFF=$((BACKOFF * 2))
done
rm -f "$PKGVAR/run/launcher.pid" "$PKGVAR/run/server.pid"
log "launcher exited"
