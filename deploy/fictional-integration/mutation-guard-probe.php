<?php
/** Fictional-only subprocess evidence; never loaded as a web/API extension. */
require_once '/yizkr-mutation-guard/Guard.php';
require_once '/yizkr-mutation-guard/DatabaseDrain.php';
if (PHP_SAPI !== 'cli' || getenv('DB_NAME') !== 'fictional') {
    throw new RuntimeException('The guard probe requires the isolated fictional service');
}

function database(): mysqli {
    [$host, $port] = explode(':', getenv('DB_SERVER') . ':3306');
    return new mysqli($host, getenv('DB_USER'), getenv('DB_PASS'), 'fictional', (int)$port);
}
function require_true(bool $condition, string $message): void {
    if (!$condition) { throw new RuntimeException($message); }
}
function child(string $mode, string $directory): array {
    $process = proc_open([PHP_BINARY, __FILE__, $mode, $directory],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    require_true(is_resource($process), 'Probe child could not start');
    return [$process, $pipes];
}
function line(array $child): string {
    $read = [$child[1][1]]; $write = null; $except = null;
    require_true(stream_select($read, $write, $except, 30) === 1, 'Probe child did not reach its barrier');
    $line = fgets($child[1][1]);
    if ($line === false) {
        throw new RuntimeException('Probe child failed: ' . stream_get_contents($child[1][2]));
    }
    return trim($line);
}
function blocked(array $child): void {
    $read = [$child[1][1]]; $write = null; $except = null;
    require_true(stream_select($read, $write, $except, 0, 150000) === 0, 'Exclusive admission crossed an active PHP request');
}
function finish(array $child): void {
    fclose($child[1][0]); fclose($child[1][1]);
    $error = stream_get_contents($child[1][2]); fclose($child[1][2]);
    require_true(proc_close($child[0]) === 0, 'Probe child failed: ' . $error);
}

$mode = $argv[1] ?? 'probe';
$directory = $argv[2] ?? '';
if ($mode === 'holder') {
    YizkrMutationGuard::enter($directory, false);
    register_shutdown_function(function () {
        echo "shutdown\n"; flush(); fgets(STDIN);
    });
    echo "ready\n"; flush(); fgets(STDIN); exit;
}
if ($mode === 'exclusive') {
    $state = YizkrMutationGuard::enter($directory, true);
    $capability = $state['instance'] . '.1.' . str_repeat('a', 64);
    $state = YizkrMutationGuard::transition('begin', 'fictional-probe', $capability, static function () {});
    require_true($state['active'], 'Exclusive operation was not acquired');
    echo "exclusive\n"; exit;
}
if ($mode === 'restart') {
    $state = YizkrMutationGuard::initialise($directory);
    require_true($state['owner'] !== null, 'Restart discarded the active owner');
    require_true(!YizkrMutationGuard::admit($state, '', false, ''), 'CLI mutation was admitted while fenced');
    echo "preserved\n"; exit;
}
if ($mode === 'admission') {
    $state = YizkrMutationGuard::enter($directory, false);
    $capability = $state['instance'] . '.1.' . str_repeat('a', 64);
    require_true(YizkrMutationGuard::admit($state, $capability, true, 'wbeditentity'), 'Owner request was refused');
    foreach (['wbeditentity', 'wbsetclaim', 'delete', 'undelete', 'move', 'revisiondelete', 'edit'] as $action) {
        require_true(!YizkrMutationGuard::admit($state, '', true, $action), 'Outsider mutation was admitted');
    }
    require_true(!YizkrMutationGuard::admit($state, '', false, ''), 'Browser/REST mutation was admitted');
    require_true(YizkrMutationGuard::admit($state, '', true, 'clientlogin'), 'Recovery login was refused');
    require_true(YizkrMutationGuard::admit($state, '', true, 'wbgetentities'), 'Read was refused');
    $rdfServer = ['REQUEST_METHOD' => 'GET', 'REQUEST_URI' => '/wiki/Special:EntityData/Q90.ttl?flavor=dump&nocache=1794144878680'];
    $rdfQuery = ['flavor' => 'dump', 'nocache' => '1794144878680'];
    require_true(YizkrMutationGuard::readOnlyEntityDataRequest($rdfServer, $rdfQuery, []), 'Canonical updater RDF read was refused');
    require_true(YizkrMutationGuard::admit($state, '', false, '', true), 'Protected RDF read was refused');
    require_true(!YizkrMutationGuard::admit($state, 'stale', false, '', true), 'RDF read bypassed stale capability refusal');
    foreach ([
        [$rdfServer, $rdfQuery + ['action' => 'edit'], []],
        [$rdfServer, $rdfQuery + ['action' => 'view'], []],
        [$rdfServer, $rdfQuery + ['title' => 'Special:NewItem'], []],
        [$rdfServer, $rdfQuery + ['format' => 'json'], []],
        [$rdfServer, ['flavor' => ['dump']], []],
        [$rdfServer, ['nocache' => ['1']], []],
        [$rdfServer, ['nocache' => '1;action=edit'], []],
        [$rdfServer, ['flavor' => 'unknown'], []],
        [$rdfServer, $rdfQuery, ['action' => 'edit']],
        [array_replace($rdfServer, ['REQUEST_METHOD' => 'POST']), $rdfQuery, []],
        [array_replace($rdfServer, ['REQUEST_METHOD' => 'HEAD']), $rdfQuery, []],
        [array_replace($rdfServer, ['REQUEST_URI' => '/wiki/Special:NewItem']), [], []],
        [array_replace($rdfServer, ['REQUEST_URI' => '/wiki/Special:EntityData/Q90.json']), [], []],
        [array_replace($rdfServer, ['REQUEST_URI' => '/w/index.php?title=Special:EntityData/Q90.ttl']), [], []],
        [array_replace($rdfServer, ['REQUEST_URI' => '/wiki/Special:EntityData/Q90.ttl/../Special:NewItem']), [], []],
    ] as [$server, $query, $post]) {
        require_true(!YizkrMutationGuard::readOnlyEntityDataRequest($server, $query, $post), 'RDF admission accepted a different request');
    }
    echo "admitted\n"; exit;
}
if ($mode === 'release') {
    $state = YizkrMutationGuard::enter($directory, true);
    $capability = $state['instance'] . '.1.' . str_repeat('a', 64);
    $state = YizkrMutationGuard::transition('end', 'fictional-probe', $capability, static function () {});
    require_true(!$state['active'], 'Release did not complete');
    try {
        YizkrMutationGuard::transition('begin', 'fictional-probe', $capability, static function () {});
        throw new LogicException('Stale capability reacquired the guard');
    } catch (RuntimeException $error) {
        require_true(!($error instanceof LogicException), 'Stale capability reacquired the guard');
    }
    echo "released\n"; exit;
}
if ($mode === 'stale') {
    $state = YizkrMutationGuard::enter($directory, false);
    require_true(!YizkrMutationGuard::admit($state, $state['instance'] . '.1.' . str_repeat('a', 64), true, 'wbeditentity'), 'Released capability was admitted');
    echo "stale\n"; exit;
}
if ($mode === 'invalid') {
    try {
        YizkrMutationGuard::initialise($directory);
    } catch (RuntimeException $error) {
        echo "refused\n"; exit;
    }
    throw new RuntimeException('Missing or corrupt state was accepted');
}
if ($mode === 'sql') {
    $db = database();
    $db->query('START TRANSACTION');
    $db->query('INSERT INTO yizkr_fictional_guard_probe VALUES (1)');
    // Once the parent kills this PHP process, this server-side query can still
    // be running. Production draining must settle the entire connection.
    $db->query('SELECT SLEEP(60)', MYSQLI_ASYNC);
    echo $db->thread_id . "\n"; flush();
    fgets(STDIN);
    exit;
}

$directory = sys_get_temp_dir() . '/yizkr-fictional-guard-' . bin2hex(random_bytes(12));
$init = proc_open([PHP_BINARY, '-r', 'require "/yizkr-mutation-guard/Guard.php"; YizkrMutationGuard::initialise($argv[1]);', $directory], [], $unused);
require_true(proc_close($init) === 0, 'Guard initialisation failed');
$holder = child('holder', $directory);
require_true(line($holder) === 'ready', 'Holder did not acquire');
$exclusive = child('exclusive', $directory);
blocked($exclusive);
fwrite($holder[1][0], "finish\n"); fflush($holder[1][0]);
require_true(line($holder) === 'shutdown', 'Shutdown callback was not reached');
blocked($exclusive);
fwrite($holder[1][0], "finish\n"); fflush($holder[1][0]);
finish($holder);
require_true(line($exclusive) === 'exclusive', 'Exclusive admission did not drain');
finish($exclusive);
foreach (['restart' => 'preserved', 'admission' => 'admitted', 'release' => 'released', 'stale' => 'stale'] as $step => $expected) {
    $process = child($step, $directory);
    require_true(line($process) === $expected, 'Guard lifecycle probe failed');
    finish($process);
}
file_put_contents($directory . '/state.json', '{}');
foreach (['corrupt', 'missing'] as $invalid) {
    if ($invalid === 'missing') { unlink($directory . '/state.json'); }
    $process = child('invalid', $directory);
    require_true(line($process) === 'refused', 'Invalid state did not fail closed');
    finish($process);
}

// No unrelated requests can enter the wiki while the probe drains SQL.
YizkrMutationGuard::enter('/config/yizkr-mutation-guard', true);
$connection = database();
$connection->query('CREATE TABLE yizkr_fictional_guard_probe (id INT PRIMARY KEY) ENGINE=InnoDB');
try {
    $sql = child('sql', $directory);
    $id = (int)line($sql);
    require_true($id > 0, 'SQL child did not establish its transaction');
    $running = false;
    $deadline = microtime(true) + 5;
    do {
        foreach ($connection->query('SHOW PROCESSLIST') as $row) {
            if ((int)$row['Id'] === $id && $row['Info'] === 'SELECT SLEEP(60)') {
                $running = true;
            }
        }
        if (!$running) { usleep(10000); }
    } while (!$running && microtime(true) < $deadline);
    require_true($running, 'SQL statement did not enter the server');
    proc_terminate($sql[0], 9);
    foreach ($sql[1] as $pipe) { fclose($pipe); }
    proc_close($sql[0]);
    $adapter = new class($connection) {
        private mysqli $db;
        public int $kills = 0;
        public function __construct(mysqli $db) { $this->db = $db; }
        public function query(string $sql, string $caller) {
            if (str_starts_with($sql, 'KILL CONNECTION ')) { $this->kills++; }
            $result = $this->db->query($sql);
            return $result === true ? [] : $result->fetch_all(MYSQLI_ASSOC);
        }
        public function lastErrno(): int { return $this->db->errno; }
    };
    // Match MediaWiki's object-row result interface.
    $objects = new class($adapter) {
        private $adapter;
        public function __construct($adapter) { $this->adapter = $adapter; }
        public function query(string $sql, string $caller): array {
            return array_map(static fn($row) => (object)$row, $this->adapter->query($sql, $caller));
        }
        public function lastErrno(): int { return $this->adapter->lastErrno(); }
    };
    YizkrMutationDatabaseDrain::run($objects);
    require_true($adapter->kills > 0, 'The probe did not exercise a surviving SQL connection');
    foreach ($connection->query('SHOW PROCESSLIST') as $row) {
        require_true((int)$row['Id'] !== $id, 'Killed SQL connection was not drained');
    }
    require_true((int)$connection->query('SELECT COUNT(*) AS n FROM yizkr_fictional_guard_probe')->fetch_assoc()['n'] === 0, 'Abandoned transaction was not rolled back');
} finally {
    $connection->query('DROP TABLE yizkr_fictional_guard_probe');
    foreach (glob($directory . '/*') as $path) { unlink($path); }
    rmdir($directory);
}
echo json_encode(['protocol' => YizkrMutationGuard::PROTOCOL, 'request_drain' => true, 'shutdown_drain' => true, 'restart' => true, 'stale_capability' => true, 'invalid_state' => true, 'database_drain' => true]) . "\n";
