<?php
/** Settle SQL left behind by a PHP worker that died before request teardown. */
final class YizkrMutationDatabaseDrain {
    public static function run($db): void {
        $current = null;
        foreach ($db->query('SELECT CONNECTION_ID() AS id, CURRENT_USER() AS account, VERSION() AS version', __METHOD__) as $row) {
            $current = $row;
        }
        if ($current === null || !preg_match('/^10\.11\.[0-9]+-MariaDB/', $current->version)) {
            throw new RuntimeException('Database connection identity unavailable');
        }
        $id = (int)$current->id;
        $separator = strrpos($current->account, '@');
        if ($separator === false) {
            throw new RuntimeException('Database account identity unavailable');
        }
        $user = substr($current->account, 0, $separator);
        $deadline = microtime(true) + 20;
        do {
            $others = [];
            $selfSeen = false;
            foreach ($db->query('SHOW PROCESSLIST', __METHOD__) as $row) {
                if ((int)$row->Id === $id) {
                    $selfSeen = $row->User === $user;
                }
                if ($row->User === $user && (int)$row->Id !== $id) {
                    $others[] = (int)$row->Id;
                }
            }
            if (!$selfSeen) {
                throw new RuntimeException('Database connection scope could not be verified');
            }
            if (!$others) {
                return;
            }
            foreach ($others as $other) {
                try {
                    $db->query('KILL CONNECTION ' . $other, __METHOD__);
                } catch (Throwable $error) {
                    // A connection can finish between enumeration and KILL.
                    if ((int)$db->lastErrno() !== 1094) {
                        throw $error;
                    }
                }
            }
            // MariaDB removes a connection from its process list after its
            // transaction cleanup; KILL acknowledgement alone is insufficient.
            usleep(10000);
        } while (microtime(true) < $deadline);
        throw new RuntimeException('Database requests did not drain');
    }
}
