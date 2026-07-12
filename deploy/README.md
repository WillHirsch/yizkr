# Wikibase Suite deployment runbook

Runs the project's system of record (ADR-0001) on a home server with
Docker Compose: MediaWiki + Wikibase, the WDQS SPARQL query service
with its updater and frontend, Elasticsearch, QuickStatements, MariaDB,
and a traefik reverse proxy.

Adapted from [Wikibase Suite Deploy] 7.0.0. Differences: LAN/HTTP-first
(no TLS by default — this archive is privacy-first and nothing is
public by default), and the anonymous usage-metadata callback to
Wikimedia Deutschland is off by default.

[Wikibase Suite Deploy]: https://github.com/wmde/wikibase-release-pipeline/tree/main/deploy

## Prerequisites

- Docker Engine with Compose v2 on a 64-bit Linux host.
- ~4 GB free RAM for the stack (Elasticsearch and Blazegraph are the
  hungry ones) and a few GB of disk to start; artifact media grows over
  time.
- Two hostnames that resolve to the server, one for the wiki and one
  for the query service — traefik routes by `Host` header, so an IP
  address alone is not enough. On a LAN, add entries to your router's
  DNS/Pi-hole, or to each client's `/etc/hosts`:

  ```
  192.168.1.50  wikibase.lan query.wikibase.lan
  ```

## First boot

```sh
cd deploy
cp .env.example .env
$EDITOR .env      # set hosts, admin account, DB_PASS — see comments
docker compose up -d
```

The first boot installs MediaWiki and takes a few minutes. Follow along
with `docker compose ps` (wait for `wikibase` to be `healthy`) or
`docker compose logs -f wikibase`.

Once healthy:

- Wiki: `http://WIKIBASE_PUBLIC_HOST/` (log in as `MW_ADMIN_NAME`)
- Query service UI: `http://WDQS_PUBLIC_HOST/`
- SPARQL endpoint: `http://WDQS_PUBLIC_HOST/sparql`
- QuickStatements: `http://WIKIBASE_PUBLIC_HOST/tools/quickstatements/`

> **Warning:** `.env` values are *initial setup* values. After first
> boot, changing hosts, scheme, admin or DB credentials requires a
> reset (below). Pick hostnames you can keep.

## Smoke test

Verifies item creation via the API and the SPARQL round-trip through
the wdqs-updater. Idempotent — re-runs reuse the marker item it
creates. It reads `deploy/.env` for hosts and admin credentials.

From the repo root (see the root README for uv setup):

```sh
make smoke            # = uv run yizkr smoke-test
```

Expected output ends with `SMOKE TEST PASSED`. The WDQS round-trip
normally completes in well under a minute; the test waits up to
5 minutes (`--timeout` to change).

## HTTPS / public exposure (optional, off by default)

Nothing in this archive is public by default (it is privacy-first) —
only do this deliberately. Requirements: public DNS for both hosts pointing
at this machine, ports 80/443 reachable from the internet.

1. In `.env`, set `PUBLIC_SCHEME=https` **before first boot** (the
   scheme is baked into WDQS entity URIs; changing it later needs a
   WDQS reset).
2. Start with the overlay:

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
   ```

Certificates come from Let's Encrypt via traefik (HTTP-01 challenge).
Test against the staging CA first by uncommenting the `caserver` line
in `docker-compose.https.yml` to avoid rate limits.

## Operations

- **Status / logs:** `docker compose ps`,
  `docker compose logs -f <service>`
- **Stop / start:** `docker compose down` / `docker compose up -d`
  (data lives in named volumes and survives `down`)
- **Upgrades:** images are pinned to major versions (`wikibase:7`,
  `wdqs:2`, ...). Before upgrading majors, read the upstream release
  notes — MediaWiki upgrades can require `update.php` runs. Take a
  backup first (backup/export job is W1 task 7).
- **Generated config:** first boot writes `config/LocalSettings.php`,
  `config/wdqs-frontend-config.json`, and `config/wikibase-php.ini`
  (all gitignored). Local settings tweaks go in
  `config/Extensions.php` / `LocalSettings.d` overrides, not upstream
  files.

### Resetting

Destroys all wiki content and rebuilds from `.env`. Only for a broken
first install or a deliberate start-over — **this deletes the graph**.

```sh
docker compose down --volumes
rm -f config/LocalSettings.php
docker compose up -d
```

To reset only the query service (e.g. after changing the concept URI —
re-indexes from the wiki's RecentChanges, so only recent edits are
replayed; a full reload needs an RDF dump import):

```sh
docker compose stop wdqs wdqs-updater
docker volume rm yizkr-wikibase_wdqs-data
docker compose up -d
```

## Troubleshooting

- **`wikibase` never becomes healthy:** `docker compose logs wikibase`.
  Most commonly a missing required `.env` value (empty
  `MW_ADMIN_PASS`, `DB_PASS`) or a password shorter than MediaWiki's
  10-character minimum.
- **Elasticsearch restarts / exits 78:** the host may need
  `sysctl -w vm.max_map_count=262144` (persist in
  `/etc/sysctl.d/99-elasticsearch.conf`).
- **Blazegraph aborts with "unable to allocate file descriptor
  table":** the compose file already raises `nofile` ulimits; check
  your Docker daemon doesn't override them lower.
- **Items never appear in SPARQL:** `docker compose logs wdqs-updater`.
  The updater must be able to reach the `wikibase` service and its
  `WIKIBASE_CONCEPT_URI` must match `MW_WG_SERVER` (both are derived
  from the same `.env` values here, so drift means `.env` was changed
  after first boot).
- **404s or wrong site answered:** the `Host` header didn't match
  either configured hostname — check DNS/hosts entries point at the
  server and match `.env` exactly.
