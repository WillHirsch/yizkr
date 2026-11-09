#!/usr/bin/env bash
set -uo pipefail
child=
trap '[ -z "$child" ] || kill "$child" 2>/dev/null; exit 0' TERM INT
while true; do
    # Do not keep an idle PHP process holding the shared request lock.
    php /var/www/html/maintenance/runJobs.php --maxjobs=5 --conf /config/LocalSettings.php &
    child=$!
    wait "$child" || true
    child=
    sleep 1 &
    child=$!
    wait "$child" || true
    child=
done
