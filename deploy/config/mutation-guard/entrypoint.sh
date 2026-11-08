#!/usr/bin/env bash
set -euo pipefail
guard_state=$(php /yizkr-mutation-guard/initialise.php)
chown -R www-data:www-data /config/yizkr-mutation-guard
if [ "$guard_state" = active ]; then
    # Recovery must be available after a restart. Upgrades remain excluded
    # until the owning operation explicitly releases its persistent fence.
    test -f /config/LocalSettings.php
    cp /config/LocalSettings.php /var/www/html/LocalSettings.php
    exec docker-php-entrypoint apache2-foreground
fi
exec bash /entrypoint.sh
