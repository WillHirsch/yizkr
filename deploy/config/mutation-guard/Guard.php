<?php
/** Deployment-scoped exclusion for the single-host Wikibase services. */
final class YizkrMutationGuard {
    public const PROTOCOL = 'yizkr-mutation-guard-v1';
    public const HEADER = 'HTTP_X_YIZKR_MUTATION_CAPABILITY';
    private static $handle;
    private static string $directory;
    private static bool $exclusive = false;

    public static function enter(string $directory, bool $exclusive): array {
        if (PHP_OS_FAMILY !== 'Linux' || PHP_ZTS || PHP_VERSION_ID < 80100 || PHP_VERSION_ID >= 90000) {
            throw new RuntimeException('Mutation guard runtime is unsupported');
        }
        if (self::$handle !== null) {
            throw new RuntimeException('Mutation guard entered twice');
        }
        self::$directory = $directory;
        $handle = fopen($directory . '/requests.lock', 'r+');
        if ($handle === false || !flock($handle, ($exclusive ? LOCK_EX : LOCK_SH))) {
            throw new RuntimeException('Mutation guard lock unavailable');
        }
        // Never unlock in a shutdown callback: later callbacks/destructors may
        // still commit. PHP owns this resource until request resource teardown.
        self::$handle = $handle;
        self::$exclusive = $exclusive;
        return self::read();
    }

    public static function read(): array {
        $text = @file_get_contents(self::$directory . '/state.json');
        $state = $text === false ? null : json_decode($text, true);
        if (!is_array($state) || array_keys($state) !== ['protocol', 'instance', 'epoch', 'owner'] ||
            $state['protocol'] !== self::PROTOCOL ||
            !is_string($state['instance']) || !preg_match('/^[a-f0-9]{32}$/D', $state['instance']) ||
            !is_int($state['epoch']) || $state['epoch'] < 0) {
            throw new RuntimeException('Mutation guard state is missing or invalid');
        }
        $owner = $state['owner'];
        if ($owner !== null && (!is_array($owner) ||
            array_keys($owner) !== ['operation_id', 'digest'] ||
            !self::validOperation($owner['operation_id']) ||
            !is_string($owner['digest']) || !preg_match('/^[a-f0-9]{64}$/D', $owner['digest']))) {
            throw new RuntimeException('Mutation guard owner is invalid');
        }
        return $state;
    }

    public static function validOperation($value): bool {
        return is_string($value) && (bool)preg_match('/^[A-Za-z0-9_.:-]{1,200}$/D', $value);
    }

    public static function owns(array $state, string $capability): bool {
        return $state['owner'] !== null &&
            hash_equals($state['owner']['digest'], hash('sha256', $capability));
    }

    public static function admit(array $state, string $capability, bool $api, string $action): bool {
        if (self::$exclusive) {
            return $api && $action === 'yizkrmutationguard';
        }
        if ($capability !== '') {
            return $api && self::owns($state, $capability);
        }
        if ($state['owner'] === null) {
            return true;
        }
        // Authentication must remain available for recovery with a new client.
        // Jobs on web requests are disabled; these fixed modules do not edit
        // entities, registry pages, or their revision visibility.
        return $api && in_array($action, ['query', 'wbgetentities', 'paraminfo', 'login', 'clientlogin'], true);
    }

    public static function publicState(array $state): array {
        return [
            'protocol' => self::PROTOCOL,
            'instance' => $state['instance'],
            'epoch' => $state['epoch'],
            'active' => $state['owner'] !== null,
            'operation_id' => $state['owner']['operation_id'] ?? null,
        ];
    }

    public static function transition(string $mode, string $operation, string $capability, callable $drain): array {
        if (!self::$exclusive || !self::validOperation($operation)) {
            throw new RuntimeException('Mutation guard control request is invalid');
        }
        $state = self::read();
        if ($mode === 'inspect' && $capability === '') {
            return self::publicState($state);
        }
        if ($state['owner'] !== null) {
            if (!self::owns($state, $capability) || $state['owner']['operation_id'] !== $operation) {
                throw new RuntimeException('Mutation guard belongs to another operation');
            }
            if ($mode === 'begin' || $mode === 'status') {
                // Recovery must settle a previous request whose response was lost.
                $drain();
                return self::publicState($state);
            }
            if ($mode === 'end') {
                $drain();
                $state['owner'] = null;
                self::write($state);
                return self::publicState($state);
            }
        } elseif ($mode === 'begin') {
            $next = $state['epoch'] + 1;
            $prefix = $state['instance'] . '.' . $next . '.';
            if (!preg_match('/^' . preg_quote($prefix, '/') . '[a-f0-9]{64}$/D', $capability)) {
                throw new RuntimeException('Mutation capability is stale or invalid');
            }
            $drain();
            $state['epoch'] = $next;
            $state['owner'] = ['operation_id' => $operation, 'digest' => hash('sha256', $capability)];
            self::write($state);
            return self::publicState($state);
        }
        throw new RuntimeException('Mutation capability is stale or invalid');
    }

    private static function write(array $state): void {
        $temporary = self::$directory . '/state.' . bin2hex(random_bytes(16));
        $stream = fopen($temporary, 'x');
        if ($stream === false) {
            throw new RuntimeException('Mutation guard state cannot be persisted');
        }
        chmod($temporary, 0600);
        $text = json_encode($state, JSON_THROW_ON_ERROR) . "\n";
        if (fwrite($stream, $text) !== strlen($text) || !fflush($stream) || !fsync($stream)) {
            fclose($stream);
            throw new RuntimeException('Mutation guard state cannot be persisted');
        }
        fclose($stream);
        if (!rename($temporary, self::$directory . '/state.json')) {
            throw new RuntimeException('Mutation guard state cannot be installed');
        }
        $directory = fopen(self::$directory, 'r');
        if ($directory === false || !fsync($directory)) {
            throw new RuntimeException('Mutation guard directory cannot be synced');
        }
        fclose($directory);
    }

    public static function initialise(string $directory): array {
        // Called only by the deployment entrypoint, before it starts PHP workers.
        // The separate marker means a missing state file is never an idle fence.
        if (!is_dir($directory) && !mkdir($directory, 0700, true)) {
            throw new RuntimeException('Mutation guard directory cannot be created');
        }
        self::$directory = $directory;
        if (!file_exists($directory . '/initialised')) {
            if (file_exists($directory . '/state.json') || file_exists($directory . '/requests.lock')) {
                throw new RuntimeException('Mutation guard initialisation is incomplete');
            }
            $handle = fopen($directory . '/requests.lock', 'x');
            if ($handle === false || !flock($handle, LOCK_EX)) {
                throw new RuntimeException('Mutation guard initialisation failed');
            }
            self::$handle = $handle;
            self::$exclusive = true;
            chmod($directory . '/requests.lock', 0600);
            self::write(['protocol' => self::PROTOCOL, 'instance' => bin2hex(random_bytes(16)), 'epoch' => 0, 'owner' => null]);
            $marker = fopen($directory . '/initialised', 'x');
            if ($marker === false || !fflush($marker) || !fsync($marker)) {
                throw new RuntimeException('Mutation guard marker cannot be persisted');
            }
            fclose($marker);
            return self::read();
        }
        return self::enter($directory, true);
    }
}
