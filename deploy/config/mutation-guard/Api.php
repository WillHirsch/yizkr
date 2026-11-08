<?php
require_once __DIR__ . '/DatabaseDrain.php';
/** The control path runs after normal MediaWiki authentication and CSRF checks. */
class YizkrMutationGuardApi extends ApiBase {
    public function execute() {
        $this->checkUserRightsAny('yizkr-mutation-guard');
        $params = $this->extractRequestParams();
        try {
            $result = YizkrMutationGuard::transition(
                $params['mode'], $params['operation'],
                $_SERVER[YizkrMutationGuard::HEADER] ?? '',
                [$this, 'drainDatabase']
            );
        } catch (Throwable $error) {
            // Never include bearer credentials, SQL text, or connection details.
            $this->dieWithError('apierror-unknownerror', 'yizkr-mutation-guard-refused');
        }
        $this->getResult()->addValue(null, 'yizkrmutationguard', $result);
    }

    public function drainDatabase(): void {
        $services = MediaWiki\MediaWikiServices::getInstance();
        $db = $services->getDBLoadBalancer()->getConnection(DB_PRIMARY);
        YizkrMutationDatabaseDrain::run($db);
    }

    public function getAllowedParams() {
        return [
            'mode' => [self::PARAM_TYPE => ['inspect', 'begin', 'status', 'end'], self::PARAM_REQUIRED => true],
            'operation' => [self::PARAM_TYPE => 'string', self::PARAM_REQUIRED => true],
        ];
    }
    public function mustBePosted() { return true; }
    public function needsToken() { return 'csrf'; }
    public function isWriteMode() { return true; }
}
