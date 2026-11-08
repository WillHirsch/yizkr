<?php
require_once __DIR__ . '/Guard.php';
$state = YizkrMutationGuard::initialise('/config/yizkr-mutation-guard');
echo $state['owner'] === null ? "idle\n" : "active\n";
