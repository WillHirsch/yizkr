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
    [ 'pdf', 'tif', 'tiff', 'ged' ]
);

// GEDCOM exports (.ged) are the artifact itself for structured
// genealogy ingestion (ROADMAP item 13). MediaWiki's MIME map does not
// know the extension, so uploads fail the extension/MIME consistency
// check ("does not match the detected MIME type text/plain") unless the
// analyzer is taught that a .ged file is plain text.
$wgHooks['MimeMagicInit'][] = function ( $mimeMagic ) {
    $mimeMagic->addExtraTypes( 'text/plain ged' );
};
