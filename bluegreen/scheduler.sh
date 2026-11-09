#!/bin/sh
set -eu

stop_scheduler() {
    trap '' TERM INT
    kill -TERM "$cron_pid" 2>/dev/null || true
    wait "$cron_pid" 2>/dev/null || true
    deadline=$(( $(date +%s) + 120 ))
    while pgrep -f 'artisan schedule:run' >/dev/null; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo 'Scheduler invocation did not drain within 120 seconds' >&2
            exit 1
        fi
        sleep 1
    done
    exit 0
}

trap stop_scheduler TERM INT
date +%s > /tmp/scheduler-started-at
cron -f &
cron_pid=$!
echo "$cron_pid" > /tmp/lotusboard-cron.pid
wait "$cron_pid"
