<?php
require_once __DIR__ . '/Guard.php';

$wgJobRunRate = 0;
$wgAutoloadClasses['YizkrMutationGuardApi'] = __DIR__ . '/Api.php';
$wgAPIModules['yizkrmutationguard'] = 'YizkrMutationGuardApi';
$wgAvailableRights[] = 'yizkr-mutation-guard';
$wgGroupPermissions['sysop']['yizkr-mutation-guard'] = true;

try {
    $api = PHP_SAPI !== 'cli' &&
        realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === realpath('/var/www/html/api.php');
    // Reject ambiguous control selectors instead of guessing the API's parser.
    $action = $_POST['action'] ?? $_GET['action'] ?? '';
    if (!is_string($action) || (isset($_POST['action'], $_GET['action']) && $_POST['action'] !== $_GET['action'])) {
        throw new RuntimeException('Mutation guard action is invalid');
    }
    $state = YizkrMutationGuard::enter('/config/yizkr-mutation-guard', $api && $action === 'yizkrmutationguard');
    $capability = $_SERVER[YizkrMutationGuard::HEADER] ?? '';
    if (!is_string($capability) || !YizkrMutationGuard::admit($state, $capability, $api, $action)) {
        throw new RuntimeException('Mutations are excluded by an active or stale capability');
    }
} catch (Throwable $error) {
    if (PHP_SAPI !== 'cli') {
        http_response_code(423);
        header('Content-Type: application/json');
    }
    echo json_encode(['error' => ['code' => 'yizkr-mutation-excluded', 'info' => 'Mutation exclusion could not admit this request.']]);
    exit(75);
}
