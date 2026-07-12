<?php
// ************************************************************************
// Upload settings for the family archive (local addition, not upstream).
// ************************************************************************
//
// The MediaWiki default permits only web image formats; digitised
// archival material is routinely PDF or TIFF. Loaded via LocalSettings.d
// (mounted in docker-compose.yml).

$wgFileExtensions = array_merge(
    $wgFileExtensions,
    [ 'pdf', 'tif', 'tiff' ]
);
