#!/bin/sh
# RapidGlasses eye tracker launcher (QNX Pi).
# Runs eye_tracker.py forever, restarting it 3s after any exit/crash.
# Logs to eye_tracker.log next to this script. Safe to run by hand;
# hooked into boot via /etc/rc.d/rc.local (see SETUP.md).
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/eye_tracker.log"

echo "=== start_tracker $(date) ===" >> "$LOG"
while true; do
    python3 "$DIR/eye_tracker.py" >> "$LOG" 2>&1
    echo "eye_tracker exited, restarting in 3s" >> "$LOG"
    sleep 3
done
